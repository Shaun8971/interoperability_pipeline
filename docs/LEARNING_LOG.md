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
