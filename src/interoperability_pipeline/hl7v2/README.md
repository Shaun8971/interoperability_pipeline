# hl7v2 package (the HL7 v2 spoke)

Takes HL7 v2 messages in and turns them into FHIR resources.

| File | Job |
|---|---|
| `mllp.py` | MLLP framing: wrap/unwrap messages and read whole frames from a TCP socket |
| `parser.py` | Split text into segments, fields, components (standard library only) |
| `validation.py` | Decide accept/reject; returns a list of problems |
| `ack.py` | Build AA / AE / AR acknowledgments |
| `mappers.py` | Pure functions: segments -> FHIR Patient, Encounter, Observation |
| `listener.py` | TCP server that wires it all together and writes to FHIR |

Flow: bytes -> `mllp` -> `parser` -> `validation` -> `mappers` -> `fhir.client` -> `ack` -> bytes.

Key ideas: MLLP gives message boundaries on a byte stream; ACK after the write (AA stored,
AE bad message, AR retry later); idempotent writes keyed on identifiers; nothing is guessed
(unknown authority, coding system or date format is refused loudly).

Connects to the hub through `../fhir/client.py`. Later spokes (`x12`, `smart`) use the same client.
To add an event or message type: add it to `validation.SUPPORTED_MESSAGE_TYPES`, then a mapper.
