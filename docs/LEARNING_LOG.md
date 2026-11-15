# Learning log

One entry per checkpoint: the concept, what we built, what I got wrong and the correction, and sources.

## Checkpoint 0: CapabilityStatement

- **Concept:** every FHIR server publishes a CapabilityStatement at `[base]/metadata`. It lists the FHIR version, the resource types supported, the interactions allowed on each (read, create, update, search...), the search parameters, and features like `conditionalCreate`.
- **Built:** HAPI FHIR running in Docker at `http://localhost:8080/fhir` (FHIR 4.0.1 = R4); Python project, ruff, pytest.
- **What I got wrong:** I thought the extra metadata and links add to the server's capabilities. Correction: the statement only *describes* capabilities, which come from the server's code. It is a published promise an app can check before relying on a feature. Our pipeline needs Patient `identifier` search and `conditionalCreate`.
- **Sources:** https://hl7.org/fhir/R4/capabilitystatement.html , https://hl7.org/fhir/R4/http.html#ccreate

## Checkpoint 1.1: Raw bytes, MLLP framing, parsing

- **Concept:** HL7 v2 text is segments (lines, separated by `\r`) of fields (`|`) of components (`^`). MLLP wraps each *message* as `0x0B <text> 0x1C 0x0D` so a receiver can find message boundaries in a TCP byte stream.
- **Built:** `hl7v2/parser.py` (split segment/components, `get_field` with the MSH offset), `hl7v2/mllp.py` (`frame`, `unframe`, `MllpFrameReader`), a listener that prints raw bytes, and a hospital sender. Read PID-3, PID-5, PID-7 by hand and matched the parser.
- **What I got wrong / terminology:** I said "record"; the HL7 unit is a *message* made of *segments*. The parser never sees the markers: the listener cuts out one message with them and `unframe` strips them first. Back-to-back messages are only half the problem: TCP can also split one message across reads, so the end marker tells the receiver when to stop waiting.
- **Sources:** https://confluence.hl7.org/display/INTEROP/MLLP , https://hl7-definition.caristix.com/v2/HL7v2.5.1/Segments/PID

## Checkpoint 1.2: ACKs (AA / AE / no ACK)

- **Concept:** the receiver answers each message with an ACK: MSH (parties swapped) + MSA (`AA` accept / `AE` error, the original MSH-10 echoed in MSA-2, optional reason in MSA-3).
- **Built:** `hl7v2/validation.py` (rules: MSH present, MSH-10 present, supported type, PID-3 non-empty), `hl7v2/ack.py`, listener replies with a framed ACK and logs `control_id=...`, sender prints the ACK or reports a timeout.
- **What I got wrong / missed:** on AE I said "fix and resend", which is right, but resending the *unchanged* message just repeats the AE, so fix the data first (or send it to a human). On no-ACK I listed possible causes but missed the core problem: the sender cannot tell "never arrived" from "arrived, ACK lost". Resending risks a duplicate; not resending risks loss. Fix: resend with the *same control ID* (bounded retries, then alert) and make the receiver idempotent.
- **Sources:** https://hl7-definition.caristix.com/v2/HL7v2.5.1/Segments/MSA , https://hl7-definition.caristix.com/v2/HL7v2.5.1/Segments/MSH

## Checkpoint 1.3: Into FHIR (Patient, Encounter, Observation)

- **Concept:** FHIR is the hub. HL7 v2 segments map to resources: PID -> Patient, PV1 -> Encounter, OBX -> Observation. Writes use *conditional update* (`PUT /Patient?identifier=system|value`): one atomic server-side create-or-replace, so resends and simultaneous messages can never duplicate.
- **Built:** `fhir/client.py` (conditional_update, count, find_id; uses `requests`), `hl7v2/mappers.py` (pure mappers + `SourceConfig`), listener writes Patient -> Encounter (ADT only) -> Observations, ACKs *after* the write. AA = stored, AE = bad message, AR = our side failed (FHIR down). Counts after resends: 1 Patient, 1 Encounter, 1 Observation.
- **Design lessons:** HL7 times have no zone, so the mapper attaches the source's zone from config (assuming UTC would be wrong but believable). A lab result must not write the Encounter (it says nothing about visit status), so it only looks the visit up. Unknown coding systems are refused, never guessed.
- **What I got wrong / refined:** I framed the collision problem as being about the `id`. It is about identifier *values*: `system|value` is the unique pair, and the `system` is what separates two hospitals' `MRN12345`. The `id` is local to one server (good for references inside it, useless to share). Don't match patients by name; a receiving hospital adds a second identifier to the same patient.
- **Sources:** https://hl7.org/fhir/R4/http.html#cond-update , https://hl7.org/fhir/R4/patient.html , https://hl7.org/fhir/R4/encounter.html , https://hl7.org/fhir/R4/observation.html , https://build.fhir.org/ig/HL7/v2-to-fhir/

## Checkpoint 2.1: Reading the 837P

- **Concept:** an X12 837P is a professional claim. It sits inside three envelopes: ISA/IEA (the interchange: who sends to whom), GS/GE (a functional group of one kind of document), ST/SE (one transaction set, here one claim). Each layer has its own control number and count (ISA13=IEA02, GS06=GE02, ST02=SE02; IEA01 counts groups, GE01 counts documents, SE01 counts segments) so the receiver can tell which level lost data and acknowledge or reject at that level.
- **Built:** `x12/tokenizer.py` (reads delimiters from the fixed-width 106-character ISA, then splits), `x12/claim_data.py` (FHIR -> plain claim object), `x12/builder_837p.py` (one function per loop), `simulators/seed_claim_data.py` (Condition I10 + Procedures 99213 and 72148), `config/claims.toml`. Output: `samples/x12/jane_claim_837p.x12`: CLM total $1,200 = SV1 $200 + $1,000.
- **What I refined:** my answer on the envelopes (ST = document, GS = group of the same kind, ISA = the whole shipment) was right. Missing was *why three*: each is a separate level of control with its own counts and acknowledgments. Also learned: only the ISA is fixed width. It exists so a reader can find the delimiters before knowing what they are; everything else is variable length, which is why delimiters are still needed.
- **Sources:** https://www.stedi.com/edi/x12-005010/837 , https://www.stedi.com/edi/x12-005010/segment/ISA , https://www.stedi.com/edi/x12-005010/segment/CLM

## Checkpoint 2.2: Predicting the payment

- **Concept:** an 835 explains payment. Per service line: billed (SVC02), paid (SVC03) and adjustments (CAS: group, reason, amount). Group **CO** = contractual obligation, the provider's loss, the patient cannot be billed. Group **PR** = patient responsibility, the patient owes it. Reasons: 45 charge exceeds contracted fee, 2 coinsurance, 197 precertification absent.
- **Built:** `x12/adjudication.py` (reads the 837, applies the 3 rules), `x12/builder_835.py`, `simulators/payer.py`. Samples in `samples/x12/`: 837 and 835 for both scenarios.
- **What I got wrong:** I treated billed minus allowed as something Jane pays and applied coinsurance to the *billed* amount. Correct: allowed = min(billed, contract rate); the difference is a CO-45 write-off nobody collects; coinsurance is 20% of the *allowed* amount (PR-2); the payer pays allowed minus coinsurance. Visit: 200 = 120 paid + 50 CO-45 + 30 PR-2. MRI with auth: 1000 = 640 + 200 + 160. I also said Jane would owe the denied MRI; with a CO-197 denial the provider absorbs it, so Jane owes 0 (totals: A = $190, B = $30).
- **Sources:** https://www.stedi.com/edi/x12-005010/835 , https://www.stedi.com/edi/x12-005010/segment/CAS , https://x12.org/codes

## Checkpoint 2.3: Reconcile and the EOB

- **Concept:** the 835 is the payer's remittance to the *provider* (money and reasons). The FHIR ExplanationOfBenefit holds the same facts for the *patient* and apps. The **reconciler** is a bookkeeping check on the hospital's side: it only checks that the numbers inside the 835 add up (line: billed = paid + adjustments; claim totals = sum of lines; BPR02 = sum of CLP04). It does not know contract rates; those live in the payer's rules.
- **Built:** `x12/parser_835.py` (parse + `reconcile`), `x12/eob_mapper.py` (835 -> Coverage + EOB; every amount mapped), `x12/remittance_to_fhir.py` (refuses unbalanced files, writes idempotently). Broken 835 (CO-45 50 -> 70) refused with nothing stored; good 835 stored twice = one EOB.
- **What I got wrong:** I described the EOB as what the hospital sends the insurer for billing. That is the **837 claim**. Direction: 837 hospital -> insurer; 835 insurer -> hospital; EOB for the patient. Also unclear to me at first what the reconciler does: it checks internal arithmetic, not whether the payer applied the correct contract (that "expected payment" check is separate and not built).
- **Gotcha learned:** HAPI caches identical searches for about a minute, so a count can look stale right after a write.
- **Sources:** https://hl7.org/fhir/R4/explanationofbenefit.html , https://www.stedi.com/edi/x12-005010/835 , https://x12.org/codes

## Checkpoint 3.1: SMART discovery and login

- **Concept:** a SMART login splits into two steps. (1) The **authorize endpoint** is a web page where the *person* logs in and approves scopes; it ends by returning a one-time **authorization code** through the browser. (2) The **token endpoint** is an address the *app* calls directly (back channel) to swap the code for the **access token**, proving itself with the PKCE **code verifier**. PKCE: the app sends only the fingerprint (S256 hash, `code_challenge`) at step 1 and the secret (`code_verifier`) at step 2; a hash cannot be reversed, so a stolen code is useless. `state` ties the browser's return trip to our own login attempt.
- **Built:** `smart/pkce.py` (checked against the RFC 7636 example), `smart/discovery` + `smart/client.py` (discovery, authorize URL, code exchange), `smart/tokens.py` (local JWT decode, scope check), `portal/server.py` (standard-library `http.server`; profiles `sandbox` = SMART login, `local` = no login). Local profile shows Jane's Patient, Encounter and the EOB from Module 2.
- **What I got wrong / refined:** I compared it to an OTP. Similar idea (one-time, short-lived code) but the person never types it; it goes to the app through the browser redirect. I guessed the token endpoint was the secret key. It is the address where the code is swapped for a token; the secret is the code verifier.
- **Bug caught by a test:** my scope check treated `patient/Patient.write` as readable because "write" contains the letter `r`. SMART v2 permission letters (`cruds`) only count when the permission is made of those letters alone.
- **Sandbox finding:** the SMART launcher validates a token if one is sent but has no scope check in its proxy (`validateToken(req, false)`), so it will not refuse a read outside the granted scopes. The portal therefore checks granted scopes itself.
- **Sources:** https://hl7.org/fhir/smart-app-launch/app-launch.html , https://datatracker.ietf.org/doc/html/rfc7636 , https://github.com/smart-on-fhir/smart-launcher-v2

## Checkpoint 3.2: The token

- **Concept:** after the code swap the server returns a token response: `access_token` (the wristband), `patient` (who the login is for), `scope` (what was granted), `expires_in` (3600 s here). The portal takes the patient id from this response, never from a search: the server decided at login whose token this is, and a name search could show another person's record.
- **`state` vs PKCE (shown with a narrated run against the sandbox):** `state` = a random tag sent with the login and compared on return, so a forged callback link is rejected. PKCE = the fingerprint goes out first, the secret only at the token swap, so a stolen code is useless: the thief got `401 invalid_grant ... Invalid PKCE Verifier`.
- **Where the code comes from:** the *server* issues it after the person approves, and delivers it via a redirect of the browser to our `redirect_uri` (`/callback?code=...&state=...`). It is one-time and short-lived; the valuable token is fetched separately and directly.
- **Fewer scopes:** requesting only `patient/Patient.read` was granted exactly; the portal's scope check then hides Encounter and EOB and explains why. The sandbox itself does not refuse out-of-scope reads, so the portal must check.
- **What I got wrong / how this went:** I could not answer the three questions in the abstract (Safari hides the redirect, so the flow was invisible). A step-by-step real run made it clear. I should have shown a run before asking.
- **Sources:** https://hl7.org/fhir/smart-app-launch/app-launch.html#step-3-app-exchanges-authorization-code-for-access-token , https://datatracker.ietf.org/doc/html/rfc7636
