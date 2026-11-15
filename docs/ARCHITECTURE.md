# Architecture

FHIR R4 is the **hub**. Every other standard is an **adapter (spoke)** at the edge. Nothing
downstream needs to know which format the data arrived in.

```
Hospital simulator --HL7 v2 / MLLP--> listener --> FHIR R4 server (HAPI, Docker)
  (simulators/, run_journey.py)       (hl7v2/)         (hub; fhir/client.py is its front door)
                                                          |
   Payer simulator <---- X12 837P claim (x12/builder_837p) +--- built from Encounter, Condition, Procedure
   Payer simulator ----- X12 835 --> parser_835 + reconcile --> ExplanationOfBenefit (+ Coverage)
                                                          |
   Patient portal <-- SMART on FHIR login (smart/) + FHIR reads (portal/)
   Backend program <-- SMART backend services: signed JWT -> system token
```

## Message flow inside the HL7 v2 spoke

```
bytes --mllp--> text --parser--> segments --pick source config by MSH-4--> validation
                                                                            |
        stale? (older event than the one stored) -- yes --> not applied, ACK AA + note, error queue
                          | no
                          v
        mappers (pure) --> fhir.client (conditional update) --> ack (AA / AE / AR) --> bytes
                                      \--> event log (every message) + error queue (problems)
```

The ACK is sent *after* the FHIR write. AA = stored. AE = the message is wrong (fix, resend).
AR = our side failed (FHIR down), resend later.

## Where things live

| Thing | Location |
|---|---|
| URLs, ports, portal profiles | `config/settings.toml` |
| Per-hospital facts (PID field, time zone, system URIs, local code translations) | `config/sources/<hospital>.toml` |
| Claim, payer and contract rules (synthetic) | `config/claims.toml` |
| Secrets, key locations | `.env` (never committed; see `.env.example`); private keys in `keys/` (git-ignored) |
| HL7 v2 code | `src/interoperability_pipeline/hl7v2/` |
| X12 code | `src/interoperability_pipeline/x12/` |
| SMART code, portal | `src/interoperability_pipeline/smart/`, `.../portal/` |
| Per-hospital config loader | `src/interoperability_pipeline/mapping/` |
| Shared settings, errors, event log, report | `src/interoperability_pipeline/common/` |
| Simulators | `simulators/` (hospital sender, payer, claim data seeder) |
| Synthetic samples | `samples/hl7/`, `samples/x12/` |
| One-command demo | `run_journey.py` |

## Safety rules the code follows

- **Idempotent writes:** FHIR conditional update keyed on identifier (`system|value`).
- **Never fail silently:** an unknown hospital, code system, message type, or date format is
  refused with a reason in the ACK, the log and the error queue. Nothing is guessed.
- **Old events do not overwrite new ones:** each Encounter remembers the time of the event that
  last changed it; an older event is not applied (ACK AA with a note, plus the error queue).
- **Money must balance:** an 835 that does not add up is refused and queued, never stored.
- **Traceable:** every log line and event carries `control_id=<MSH-10>`.
- **Least privilege:** the portal checks granted scopes itself; the patient id comes from the token.

## How to add X

**A new ADT event (for example A08 patient update)**
1. Add the `(code, trigger)` pair to `SUPPORTED_MESSAGE_TYPES` in `hl7v2/validation.py`.
2. If it changes visit state, add it to `TRIGGER_TO_ENCOUNTER_STATUS` in `hl7v2/mappers.py`
   (A13 cancel discharge is already there: it maps back to `in-progress`).
3. Add a sample in `samples/hl7/` and a test in `tests/`.

**A new hospital (no code changes)**
1. Copy `config/sources/hospital_a.toml` to `config/sources/<name>.toml`.
2. Set `sending_facility` (its MSH-4), `timezone`, `patient_id_field` (2 or 3), the identifier
   system URIs, and any `[code_translations]` for its local lab codes.
3. Done. Messages from an unlisted hospital are refused with `unknown sending facility`.
   (`hospital_b.toml` is the worked example: MRN in PID-2, Central time, local potassium code.)

**A new resource from an existing message** (for example allergies from AL1)
1. Write one pure `<resource>_from_hl7` function in `hl7v2/mappers.py`.
2. Call it in `store_message` in `hl7v2/listener.py` using `fhir_client.conditional_update`.

**A new X12 transaction (270/271 eligibility, 276/277 claim status)**
Reuse `x12/tokenizer.py` and `x12/builder_837p.make_segment`; add a builder or parser beside the
existing ones; keep the FHIR reads and writes separate from the X12 text.

**A different FHIR server:** change `[fhir] base_url` in `config/settings.toml`. The client only
uses standard FHIR REST calls. (If it uses SMART, point the portal profile at it.)

**The Epic sandbox:** add a portal profile in `config/settings.toml` with Epic's base URL and a
registered client id; the discovery, PKCE and token code is the same.

**FHIR Subscriptions / Bulk FHIR export:** add methods to `fhir/client.py` (thin wrappers over one
HTTP call each); a backend-services token (`smart/backend_services.py`) is what a bulk export uses.

**An agent that reads the same data:** have it use `smart/backend_services.py` for a system token
and `fhir/client.py` for reads. It then sees exactly what the portal sees, nothing format-specific.

## Not built (on purpose)

An "expected payment" check against the contract (the reconciler only checks the 835's own
arithmetic), a master patient index for matching people across hospitals, error-queue replay,
and token refresh.
