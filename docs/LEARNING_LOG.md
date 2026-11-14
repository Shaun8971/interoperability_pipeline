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
