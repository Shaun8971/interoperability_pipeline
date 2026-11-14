"""Tests for the HL7 v2 parser, using the synthetic messages in samples/hl7/."""

from pathlib import Path

from interoperability_pipeline.hl7v2.parser import (
    get_field,
    split_components,
    split_message,
    split_segment,
)

SAMPLES_DIRECTORY = Path(__file__).parent.parent / "samples" / "hl7"


def load_sample(file_name: str) -> list[list[str]]:
    return split_message((SAMPLES_DIRECTORY / file_name).read_text())


def find_segment(segments: list[list[str]], name: str) -> list[str]:
    return next(segment for segment in segments if segment[0] == name)


def test_empty_fields_are_kept() -> None:
    assert split_segment("PID|1||MRN1") == ["PID", "1", "", "MRN1"]


def test_components_split_on_caret() -> None:
    assert split_components("DOE^JANE^Q") == ["DOE", "JANE", "Q"]


def test_pid_fields_for_jane() -> None:
    pid = find_segment(load_sample("adt_a01.hl7"), "PID")
    assert get_field(pid, 3) == "MRN12345^^^MERCY^MR"  # PID-3 identifier
    assert split_components(get_field(pid, 5))[:2] == ["DOE", "JANE"]  # PID-5 name
    assert get_field(pid, 7) == "19800315"  # PID-7 birth date


def test_msh_numbering_is_shifted_correctly() -> None:
    msh = find_segment(load_sample("adt_a01.hl7"), "MSH")
    assert get_field(msh, 2) == "^~\\&"  # MSH-2 encoding characters
    assert get_field(msh, 9).split("^")[:2] == ["ADT", "A01"]  # MSH-9 message type
    assert get_field(msh, 10) == "MSG00001"  # MSH-10 message control ID


def test_missing_field_returns_empty_string() -> None:
    assert get_field(["PID", "1"], 20) == ""
