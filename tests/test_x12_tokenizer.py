"""Tests for the X12 tokenizer, using a small hand-built interchange."""

import pytest

from interoperability_pipeline.common.errors import X12FormatError
from interoperability_pipeline.x12.tokenizer import (
    ISA_LENGTH,
    read_delimiters,
    split_sub_elements,
    tokenize,
)

# ISA elements are fixed width: IDs are space-padded to 15 characters, qualifiers to 10.
ISA = (
    "ISA*00*"
    + " " * 10
    + "*00*"
    + " " * 10
    + "*ZZ*"
    + "MERCYGENERAL".ljust(15)
    + "*ZZ*"
    + "SYNTHPLAN".ljust(15)
    + "*261116*1100*^*00501*000000001*0*T*:~"
)
BODY = (
    "GS*HC*A*B*20261116*1100*1*X*005010X222A1~"  # functional group opens
    "ST*837*0001~"  # transaction set opens
    "CLM*JANE-0001*1200~"
    "SE*3*0001~"  # transaction set closes
    "GE*1*1~"  # functional group closes
    "IEA*1*000000001~"  # interchange closes
)
SAMPLE = ISA + BODY


def test_isa_is_exactly_106_characters_even_with_short_ids() -> None:
    assert len(ISA) == ISA_LENGTH


def test_delimiters_are_read_from_the_isa() -> None:
    delimiters = read_delimiters(SAMPLE)
    assert (delimiters.element, delimiters.sub_element, delimiters.segment) == ("*", ":", "~")


def test_other_delimiters_are_honoured() -> None:
    odd = SAMPLE.replace("*", "|").replace("~", "!").replace(":", ">")
    delimiters = read_delimiters(odd)
    assert (delimiters.element, delimiters.sub_element, delimiters.segment) == ("|", ">", "!")
    assert tokenize(odd)[2] == ["ST", "837", "0001"]


def test_tokenize_splits_segments_and_elements() -> None:
    segments = tokenize(SAMPLE)
    assert [segment[0] for segment in segments] == ["ISA", "GS", "ST", "CLM", "SE", "GE", "IEA"]
    assert segments[3] == ["CLM", "JANE-0001", "1200"]


def test_newlines_between_segments_are_ignored() -> None:
    assert tokenize(SAMPLE.replace("~", "~\n")) == tokenize(SAMPLE)


def test_sub_elements_split_on_the_declared_character() -> None:
    assert split_sub_elements("HC:99213", read_delimiters(SAMPLE)) == ["HC", "99213"]


def test_file_without_isa_is_refused() -> None:
    with pytest.raises(X12FormatError):
        tokenize("ST*837*0001~")


def test_truncated_isa_is_refused() -> None:
    with pytest.raises(X12FormatError):
        tokenize(ISA[:50])
