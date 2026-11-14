"""Decide whether an incoming HL7 v2 message is acceptable.

What it does: checks the few things the pipeline cannot work without and returns a
list of human-readable problems. An empty list means "accept" (ACK code AA).
Where it sits in the pipeline: listener --> parser --> **validation** --> ACK builder.
Standard and spec: HL7 v2.5.1 MSH (https://hl7-definition.caristix.com/v2/HL7v2.5.1/Segments/MSH)
and PID (.../Segments/PID). The rules themselves are this project's, not the standard's.
Inputs / outputs: parsed segments (list of field lists) -> list[str] of problems.
How to extend: add a message type to SUPPORTED_MESSAGE_TYPES when a mapper exists
for it (A08, A13 later); add a rule by appending to the problems list.
"""

from interoperability_pipeline.hl7v2.parser import (
    find_segment,
    find_segments,
    get_field,
    split_components,
)

# --- Segment names ---------------------------------------------------------------
SEGMENT_MSH = "MSH"  # message header
SEGMENT_PID = "PID"  # patient identification
SEGMENT_OBX = "OBX"  # one observation result
MESSAGE_CODE_RESULT = "ORU"  # MSH-9.1 for lab/observation results

# --- Supported message types: (MSH-9.1 message code, MSH-9.2 trigger event) ---------
SUPPORTED_MESSAGE_TYPES = {
    ("ADT", "A01"),  # admit / visit notification
    ("ADT", "A03"),  # discharge / end visit
    ("ORU", "R01"),  # unsolicited observation result (lab)
}


def validate_message(segments: list[list[str]]) -> list[str]:
    """Return the reasons to reject this message; an empty list means accept.

    Why a list and not a boolean: the sender (and the logs) learn *everything* wrong
    in one round trip instead of fixing one problem at a time.

    Example:
        >>> validate_message([["MSH", "^~\\\\&", "A", "B", "C", "D", "t", "", "ADT^A01", "ID1"],
        ...                   ["PID", "1", "", ""]])
        ['PID-3 patient identifier is empty']
    """
    msh = find_segment(segments, SEGMENT_MSH)
    if msh is None:
        return ["missing MSH segment"]  # nothing else can be checked without a header

    problems: list[str] = []
    if not get_field(msh, 10).strip():  # MSH-10: message control ID
        problems.append("MSH-10 message control ID is empty")

    # MSH-9: message type, components code^trigger event
    message_type = tuple(split_components(get_field(msh, 9))[:2])
    if message_type not in SUPPORTED_MESSAGE_TYPES:
        problems.append(f"unsupported message type {'^'.join(message_type)!r}")

    pid = find_segment(segments, SEGMENT_PID)
    if pid is None:
        problems.append("missing PID segment")
    elif not get_field(pid, 3).strip():  # PID-3: patient identifier list (the MRN)
        problems.append("PID-3 patient identifier is empty")
    if message_type[0] == MESSAGE_CODE_RESULT and not find_segments(segments, SEGMENT_OBX):
        problems.append("result message has no OBX segment")  # nothing to store would be silent
    return problems
