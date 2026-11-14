"""Build HL7 v2 acknowledgment (ACK) messages.

What it does: turns "the original message + the list of problems" into ACK text.
AA (application accept) if there are no problems, AE (application error) otherwise.
Where it sits in the pipeline: parser --> validation --> **ACK builder** --> MLLP --> sender.
Standard and spec: HL7 v2.5.1 chapter 2, "Acknowledgment" and the MSA segment:
https://hl7-definition.caristix.com/v2/HL7v2.5.1/Segments/MSA
Inputs / outputs: parsed segments, problem list, time -> ACK message text ('\\r' separated).
How to extend: AR (application reject) is for messages that were never processed at
all; add it to the codes below if a transport-level reject is needed.
"""

from datetime import datetime

from interoperability_pipeline.hl7v2.parser import find_segment, get_field, split_components

# --- Acknowledgment codes (MSA-1) ------------------------------------------------
ACK_ACCEPT = "AA"  # processed; the sender may forget the message
ACK_ERROR = "AE"  # understood but not processed; the sender must fix and resend
ACK_REJECT = "AR"  # not processed for reasons unrelated to content (e.g. FHIR down); resend later

# --- Fixed pieces of the ACK -----------------------------------------------------
SEGMENT_SEPARATOR = "\r"  # HL7 v2 ends every segment with a carriage return
FIELD_SEPARATOR = "|"
ENCODING_CHARACTERS = "^~\\&"  # MSH-2: component, repetition, escape, sub-component
HL7_VERSION = "2.5.1"  # MSH-12
PROCESSING_ID = "P"  # MSH-11: P = production
ACK_MESSAGE_CODE = "ACK"  # MSH-9.1
TIMESTAMP_FORMAT = "%Y%m%d%H%M%S"  # HL7 timestamp YYYYMMDDHHMMSS
ACK_CONTROL_ID_PREFIX = "ACK-"  # our ACK's own MSH-10 = prefix + original control ID
# Characters that would corrupt the ACK if they appeared inside the MSA-3 text.
_RESERVED_CHARACTERS = "|^~\\&\r\n"


def build_ack(
    segments: list[list[str]],
    problems: list[str],
    now: datetime,
    code: str | None = None,
) -> str:
    """Build the ACK for a received message.

    ``now`` is passed in (not read from the clock) so tests are deterministic.
    ``code`` overrides the default (use AR when the failure is not the message's fault).

    Example (segments of Jane's A01, no problems):
        MSH|^~\\&|PIPELINE|FHIRHUB|HOSPSIM|MERCY_GENERAL|20261114093000||ACK^A01|ACK-MSG00001|P|2.5.1
        MSA|AA|MSG00001
    """
    original_msh = find_segment(segments, "MSH") or ["MSH"]
    code = code or (ACK_ERROR if problems else ACK_ACCEPT)  # default: AE if problems, else AA
    original_control_id = get_field(original_msh, 10)  # MSH-10: control ID of the original
    trigger_event = (split_components(get_field(original_msh, 9)) + ["", ""])[1]  # MSH-9.2

    header_fields = [
        "MSH",
        ENCODING_CHARACTERS,  # MSH-2
        get_field(original_msh, 5),  # MSH-3 sending app = original receiving app (swapped)
        get_field(original_msh, 6),  # MSH-4 sending facility = original receiving facility
        get_field(original_msh, 3),  # MSH-5 receiving app = original sending app
        get_field(original_msh, 4),  # MSH-6 receiving facility = original sending facility
        now.strftime(TIMESTAMP_FORMAT),  # MSH-7 date/time of this ACK
        "",  # MSH-8 security (unused)
        f"{ACK_MESSAGE_CODE}^{trigger_event}" if trigger_event else ACK_MESSAGE_CODE,  # MSH-9
        ACK_CONTROL_ID_PREFIX + original_control_id,  # MSH-10 this ACK's own control ID
        PROCESSING_ID,  # MSH-11
        HL7_VERSION,  # MSH-12
    ]
    acknowledgment_fields = [
        "MSA",
        code,  # MSA-1 acknowledgment code
        original_control_id,  # MSA-2 control ID of the message being acknowledged
    ]
    if problems:
        acknowledgment_fields.append(_sanitize("; ".join(problems)))  # MSA-3 text message
    return SEGMENT_SEPARATOR.join(
        [FIELD_SEPARATOR.join(header_fields), FIELD_SEPARATOR.join(acknowledgment_fields)]
    )


def _sanitize(text: str) -> str:
    """Replace HL7 delimiter characters so free text cannot break the ACK's structure."""
    return "".join(" " if character in _RESERVED_CHARACTERS else character for character in text)
