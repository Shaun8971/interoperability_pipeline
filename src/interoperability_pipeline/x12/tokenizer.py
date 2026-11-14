"""Split an X12 file into segments, elements and sub-elements (standard library only).

What it does: reads the delimiters from the fixed-width ISA header, then uses them to turn
the text into lists you can index ("give me element 2 of the CLM segment").
Where it sits in the pipeline: shared by Module 2's 835 parser, the payer simulator (which
reads the 837) and the tests that check the 837 builder's output.
Standard and spec: ASC X12 interchange control structure. The free summary is at
https://www.stedi.com/edi/x12-005010/segment/ISA (fixed-width ISA layout and delimiters).
Inputs / outputs: file text -> ``X12Delimiters`` and ``list[list[str]]`` (first item of each
segment is its ID, e.g. ["CLM", "JANE-0001", "1200", ...]).
How to extend: add helpers like ``find_segments`` next to ``tokenize`` as parsers need them.

Why it differs from HL7 v2: X12 declares its own delimiters inside the ISA segment, at fixed
positions, instead of using the same characters everywhere.
"""

from dataclasses import dataclass

from interoperability_pipeline.common.errors import X12FormatError

# --- ISA layout (positions are 0-based indexes into the file text) ----------------------
ISA_SEGMENT_ID = "ISA"
ISA_LENGTH = 106  # every ISA is exactly this many characters, including the terminator
ELEMENT_SEPARATOR_INDEX = 3  # character 4: the character right after "ISA"
SUB_ELEMENT_SEPARATOR_INDEX = 104  # character 105: ISA16, the sub-element separator
SEGMENT_TERMINATOR_INDEX = 105  # character 106: ends the ISA segment (and every other one)


@dataclass(frozen=True)
class X12Delimiters:
    """The three separator characters this particular file uses."""

    element: str  # separates elements within a segment, usually "*"
    sub_element: str  # separates parts within one element, usually ":"
    segment: str  # ends a segment, usually "~"


def read_delimiters(text: str) -> X12Delimiters:
    """Read the delimiters out of the ISA header.

    Raises X12FormatError if the file does not start with a full-length ISA segment:
    guessing delimiters would mis-split every later segment.

    Example:
        >>> read_delimiters(isa_text).element, read_delimiters(isa_text).segment
        ('*', '~')
    """
    if not text.startswith(ISA_SEGMENT_ID):
        raise X12FormatError("file does not start with an ISA segment")
    if len(text) < ISA_LENGTH:
        raise X12FormatError(f"ISA segment is shorter than {ISA_LENGTH} characters")
    return X12Delimiters(
        element=text[ELEMENT_SEPARATOR_INDEX],
        sub_element=text[SUB_ELEMENT_SEPARATOR_INDEX],
        segment=text[SEGMENT_TERMINATOR_INDEX],
    )


def tokenize(text: str) -> list[list[str]]:
    """Split an X12 file into segments, each segment into elements.

    Line breaks between segments are ignored: some senders add one after every "~" for
    readability, and they are not part of the data.

    Example:
        >>> tokenize(file_text)[2]      # the third segment
        ['ST', '837', '0001', '005010X222A1']
    """
    delimiters = read_delimiters(text)
    segments: list[list[str]] = []
    for raw_segment in text.split(delimiters.segment):
        segment_text = raw_segment.strip("\r\n")
        if segment_text:
            segments.append(segment_text.split(delimiters.element))
    return segments


def split_sub_elements(element: str, delimiters: X12Delimiters) -> list[str]:
    """Split one element at the sub-element separator.

    Example:
        >>> split_sub_elements("HC:99213", delimiters)
        ['HC', '99213']
    """
    return element.split(delimiters.sub_element)
