"""Split HL7 v2 text into segments, fields and components (standard library only).

What it does:
    Turns the text of one HL7 v2 message into lists you can index, so the rest of
    the pipeline can say "give me PID-5" instead of counting pipes.

Where it sits in the pipeline:
    hospital --MLLP--> [MLLP unframing] --> **parser** --> mappers --> FHIR

Standard and spec:
    HL7 v2.5.1, chapter 2 "Control" (message structure and delimiters).
    Field reference: https://hl7-definition.caristix.com/v2/HL7v2.5.1/Segments
    Free intro to the delimiters: https://www.hl7.org/implement/standards/product_brief.cfm?product_id=185

Inputs / outputs:
    Input: message text (segments separated by carriage return or newline).
    Output: list of segments, each a list of field strings; fields can be split
    further into components.

How to extend:
    Repetitions (``~``), sub-components (``&``) and escape sequences (``\\``) are not
    handled yet. Add ``split_repetitions`` next to ``split_components`` when a
    message needs them. The delimiters are hard-coded to the HL7 defaults; to
    support other senders, read them from MSH-2 instead.
"""

# --- Delimiters (HL7 defaults; the sender declares them in MSH-2) -------------
FIELD_SEPARATOR = "|"  # separates fields inside a segment
COMPONENT_SEPARATOR = "^"  # separates components inside a field
SEGMENT_SEPARATORS = ("\r\n", "\r", "\n")  # HL7 uses CR; files and tests often use LF
MSH_SEGMENT_NAME = "MSH"  # header segment: its field numbering is special (see get_field)


def split_message(message_text: str) -> list[list[str]]:
    """Split a whole message into segments, each segment into fields.

    Example:
        >>> split_message("MSH|^~\\\\&|A\\rPID|1||MRN1")
        [['MSH', '^~\\\\&', 'A'], ['PID', '1', '', 'MRN1']]
    """
    normalized = message_text
    for separator in SEGMENT_SEPARATORS[:-1]:
        normalized = normalized.replace(separator, SEGMENT_SEPARATORS[-1])
    lines = [line for line in normalized.split("\n") if line.strip()]
    return [split_segment(line) for line in lines]


def split_segment(segment_text: str) -> list[str]:
    """Split one segment line at every pipe. Empty fields are kept.

    Why keep empties: ``||`` means "field left blank". Dropping it would shift every
    later field one position left and silently map the wrong data.

    Example:
        >>> split_segment("PID|1||MRN12345^^^MERCY^MR")
        ['PID', '1', '', 'MRN12345^^^MERCY^MR']
    """
    return segment_text.split(FIELD_SEPARATOR)


def split_components(field_text: str) -> list[str]:
    """Split one field at every caret into its components.

    Example:
        >>> split_components("DOE^JANE^Q")
        ['DOE', 'JANE', 'Q']
    """
    return field_text.split(COMPONENT_SEPARATOR)


def get_field(segment_fields: list[str], field_number: int) -> str:
    """Return field ``<SEGMENT>-<field_number>`` as text, or "" if absent.

    List position 0 is the segment name, so for most segments field N sits at
    position N. MSH is the exception: MSH-1 *is* the pipe character itself, so the
    split never produces it and every later MSH field sits one position earlier.

    Example:
        >>> get_field(["PID", "1", "", "MRN1"], 3)      # PID-3
        'MRN1'
        >>> get_field(["MSH", "^~\\\\&", "A"], 2)        # MSH-2
        '^~\\\\&'
    """
    is_header = segment_fields[0] == MSH_SEGMENT_NAME
    position = field_number - 1 if is_header else field_number
    if position < 0 or position >= len(segment_fields):
        return ""  # absent and empty are treated the same, as HL7 intends
    return segment_fields[position]


def find_segment(segments: list[list[str]], segment_name: str) -> list[str] | None:
    """Return the first segment with this name, or None if the message has none.

    Example:
        >>> find_segment([["MSH", "x"], ["PID", "1"]], "PID")
        ['PID', '1']
    """
    return next((segment for segment in segments if segment[0] == segment_name), None)


def find_segments(segments: list[list[str]], segment_name: str) -> list[list[str]]:
    """Return every segment with this name, in message order (a lab message can have many OBX).

    Example:
        >>> find_segments([["OBX", "1"], ["PID"], ["OBX", "2"]], "OBX")
        [['OBX', '1'], ['OBX', '2']]
    """
    return [segment for segment in segments if segment[0] == segment_name]
