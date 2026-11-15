"""Tests for validation, ACK building and the listener's ACK round trip."""

import socket
import threading
from datetime import datetime
from pathlib import Path

from interoperability_pipeline.fhir.client import FhirWriteResult
from interoperability_pipeline.hl7v2.ack import build_ack
from interoperability_pipeline.hl7v2.listener import handle_connection
from interoperability_pipeline.hl7v2.mappers import SourceConfig
from interoperability_pipeline.hl7v2.mllp import MllpFrameReader, frame, unframe
from interoperability_pipeline.hl7v2.parser import find_segment, get_field, split_message
from interoperability_pipeline.hl7v2.validation import validate_message

SAMPLES_DIRECTORY = Path(__file__).parent.parent / "samples" / "hl7"
FIXED_TIME = datetime(2026, 11, 14, 9, 30, 0)


def load_text(file_name: str) -> str:
    return (SAMPLES_DIRECTORY / file_name).read_text().strip().replace("\n", "\r")


def test_good_messages_have_no_problems() -> None:
    for name in ("adt_a01.hl7", "oru_r01.hl7", "adt_a03.hl7"):
        assert validate_message(split_message(load_text(name))) == [], name


def test_missing_mrn_is_a_problem() -> None:
    problems = validate_message(split_message(load_text("bad_missing_mrn.hl7")))
    assert problems == ["PID-3 patient identifier is empty"]


def test_unsupported_message_type_is_a_problem() -> None:
    text = load_text("adt_a01.hl7").replace("ADT^A01^ADT_A01", "ADT^A99^ADT_A99")
    assert "unsupported message type 'ADT^A99'" in validate_message(split_message(text))


def test_message_without_msh_is_a_problem() -> None:
    assert validate_message(split_message("PID|1||MRN1")) == ["missing MSH segment"]


def test_accept_ack_echoes_control_id_and_swaps_parties() -> None:
    segments = split_message(load_text("adt_a01.hl7"))
    ack = split_message(build_ack(segments, [], FIXED_TIME))
    msh, msa = find_segment(ack, "MSH"), find_segment(ack, "MSA")
    assert msa[1:3] == ["AA", "MSG00001"]  # MSA-1 code, MSA-2 original control ID
    assert get_field(msh, 3) == "PIPELINE"  # MSH-3: was the original receiver
    assert get_field(msh, 5) == "HOSPSIM"  # MSH-5: was the original sender
    assert get_field(msh, 9) == "ACK^A01"


def test_error_ack_carries_the_reason_without_breaking_structure() -> None:
    segments = split_message(load_text("bad_missing_mrn.hl7"))
    ack = split_message(build_ack(segments, ["PID-3 patient identifier is empty"], FIXED_TIME))
    msa = find_segment(ack, "MSA")
    assert msa[1:3] == ["AE", "MSG00099"]
    assert msa[3] == "PID-3 patient identifier is empty"


class FakeFhirClient:
    """Stands in for the FHIR server so ACK tests need no network."""

    def conditional_update(self, resource_type, search_params, resource):  # type: ignore[no-untyped-def]
        return FhirWriteResult(resource_type, "1", created=True)

    def search(self, resource_type, search_params):  # type: ignore[no-untyped-def]
        return []  # nothing stored yet, so nothing can be stale

    def find_id(self, resource_type, search_params):  # type: ignore[no-untyped-def]
        return None


SOURCE_CONFIG = SourceConfig(
    patient_identifier_systems={"MERCY": "http://mercy-general.example.org/mrn"},
    visit_identifier_systems={"MERCY": "http://mercy-general.example.org/visit"},
    observation_identifier_systems={"MERCY": "http://mercy-general.example.org/lab-result"},
    timezone="America/New_York",
    name="hospital_a",
    sending_facility="MERCY_GENERAL",
)


def _round_trip(message_text: str) -> list[str]:
    """Send a framed message through handle_connection; return the ACK's MSA fields."""
    sender, receiver = socket.socketpair()
    worker = threading.Thread(
        target=handle_connection,
        args=(receiver, FakeFhirClient(), {"MERCY_GENERAL": SOURCE_CONFIG}),
    )
    worker.start()
    sender.sendall(frame(message_text))
    ack_frame = MllpFrameReader(sender).read_frame()
    sender.close()
    worker.join(timeout=2)
    receiver.close()
    return find_segment(split_message(unframe(ack_frame)), "MSA")  # type: ignore[arg-type]


def test_listener_replies_aa_to_a_good_message() -> None:
    assert _round_trip(load_text("adt_a01.hl7"))[1:3] == ["AA", "MSG00001"]


def test_listener_replies_ae_to_a_bad_message() -> None:
    assert _round_trip(load_text("bad_missing_mrn.hl7"))[1:3] == ["AE", "MSG00099"]
