# Architecture

FHIR R4 is the **hub**. Every other standard is an **adapter (spoke)** at the edge. Nothing
downstream needs to know which format the data arrived in.

```
Hospital simulator --HL7 v2 / MLLP--> listener --> FHIR R4 server (HAPI, Docker)
   (simulators/)                      (hl7v2/)        (hub; fhir/client.py is its front door)

   [Module 2] Payer simulator <--X12 837P-- claim builder <-- FHIR
              Payer simulator --X12 835----> parser --> FHIR ExplanationOfBenefit
   [Module 3] Patient portal <--SMART on FHIR login + FHIR reads-- FHIR
```

Built so far (Module 1): the HL7 v2 spoke.

## Message flow inside the HL7 v2 spoke

```
bytes --mllp--> text --parser--> segments --validation--> problems?
                                     |                        |
                                     v                        v
                          mappers (pure) --> fhir.client --> ack (AA / AE / AR) --> bytes
```

The ACK is sent *after* the FHIR write. AA = stored. AE = the message is wrong (fix, resend).
AR = our side failed (FHIR down), resend later.

## Where things live

| Thing | Location |
|---|---|
| URLs, ports, code-system URIs, time zone | `config/settings.toml` (per-hospital parts move to `config/sources/` in Module 4) |
| Secrets | `.env` (never committed; see `.env.example`) |
| HL7 v2 code | `src/interoperability_pipeline/hl7v2/` |
| FHIR client | `src/interoperability_pipeline/fhir/` |
| Shared settings, errors | `src/interoperability_pipeline/common/` |
| Sender simulator | `simulators/hospital_sender.py` |
| Synthetic messages | `samples/hl7/` |

## Safety rules the code follows

- **Idempotent writes:** FHIR conditional update keyed on identifier (`system|value`).
- **Never fail silently:** unknown authority, coding system, date format or message type is
  refused with a reason in the ACK and the log. Nothing is guessed.
- **Traceable:** every log line carries `control_id=<MSH-10>`.

## How to add X

**A new ADT event (e.g. A08 patient update, A13 cancel discharge)**
1. Add the `(code, trigger)` pair to `SUPPORTED_MESSAGE_TYPES` in `hl7v2/validation.py`.
2. If it changes visit state, add it to `TRIGGER_TO_ENCOUNTER_STATUS` in `hl7v2/mappers.py`
   (A13 would map back to `in-progress`).
3. Add a sample in `samples/hl7/` and a test in `tests/`.

**A second hospital** (planned for Module 4)
1. Add its assigning authority and system URIs to the config tables (`identifier_systems` etc.).
2. Anything that differs in format goes in a per-source config, not in code.

**A new resource from an existing message** (e.g. allergies from AL1)
1. Write one pure `<resource>_from_hl7` function in `hl7v2/mappers.py`.
2. Call it in `store_message` in `hl7v2/listener.py` using `fhir_client.conditional_update`.

**A different FHIR server**: change `[fhir] base_url` in `config/settings.toml`. The client only
uses standard FHIR REST calls.

**Future extension points (not built):** X12 270/271 and 276/277, FHIR Subscriptions, Bulk FHIR
export, the Epic sandbox, and an agent that reads the same FHIR data.
