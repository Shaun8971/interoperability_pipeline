"""Module 4 tests: stale events, A13, onboarding Hospital B by config, unknown source, queue."""

from pathlib import Path

from interoperability_pipeline.common.event_log import ERROR_QUEUE_FILE, EVENTS_FILE, EventLog
from interoperability_pipeline.hl7v2.listener import process_message
from interoperability_pipeline.mapping.sources import load_sources
from tests.fake_fhir import InMemoryFhir

SAMPLES = Path(__file__).parent.parent / "samples" / "hl7"
SOURCES = load_sources()  # the real config/sources/*.toml files


def text(name: str) -> str:
    return (SAMPLES / name).read_text().strip().replace("\n", "\r")


def send(fhir: InMemoryFhir, name: str, log: EventLog | None = None) -> str:
    ack, _ = process_message(text(name), fhir, SOURCES, log)  # type: ignore[arg-type]
    return ack


def test_both_hospitals_are_loaded_from_config_files() -> None:
    assert set(SOURCES) == {"MERCY_GENERAL", "ST_LUKES"}


def test_a_late_old_admit_does_not_reopen_a_discharged_visit() -> None:
    fhir = InMemoryFhir()
    send(fhir, "adt_a01.hl7")
    send(fhir, "adt_a03.hl7")
    assert fhir.one("Encounter")["status"] == "finished"
    ack = send(fhir, "adt_a01.hl7")  # the old admit arrives again, late
    encounter = fhir.one("Encounter")
    assert encounter["status"] == "finished" and "end" in encounter["period"]
    assert "MSA|AA|MSG00001|stale event" in ack  # accepted, with a note saying it was not applied


def test_a_plain_resend_of_the_latest_event_is_harmless() -> None:
    fhir = InMemoryFhir()
    send(fhir, "adt_a01.hl7")
    ack = send(fhir, "adt_a01.hl7")  # the very same message again (same event time)
    assert "MSA|AA|MSG00001" in ack and "stale" not in ack  # accepted normally, not flagged stale
    assert fhir.one("Encounter")["status"] == "in-progress"
    assert len([r for r in fhir.resources if r["resourceType"] == "Patient"]) == 1


def test_a13_cancel_discharge_reopens_the_visit() -> None:
    fhir = InMemoryFhir()
    for name in ("adt_a01.hl7", "adt_a03.hl7", "adt_a13.hl7"):
        assert "MSA|AA|" in send(fhir, name)
    encounter = fhir.one("Encounter")
    assert encounter["status"] == "in-progress" and "end" not in encounter["period"]


def test_hospital_b_works_with_no_code_changes() -> None:
    fhir = InMemoryFhir()
    assert "MSA|AA|STL00001" in send(fhir, "hospital_b_adt_a01.hl7")  # MRN in PID-2, by config
    assert "MSA|AA|STL00002" in send(fhir, "hospital_b_oru_r01.hl7")
    patient = fhir.one("Patient")
    assert patient["identifier"][0]["system"] == "http://stlukes.example.org/mrn"
    assert patient["identifier"][0]["value"] == "STL7788"
    observation = fhir.one("Observation")
    coding = observation["code"]["coding"][0]
    assert (coding["system"], coding["code"]) == ("http://loinc.org", "2823-3")  # local K -> LOINC
    assert observation["effectiveDateTime"] == "2026-11-14T09:00:00-06:00"  # Central time
    assert fhir.one("Encounter")["period"]["start"] == "2026-11-14T08:30:00-06:00"


def test_hospital_a_still_refuses_an_unknown_local_code() -> None:
    fhir = InMemoryFhir()
    send(fhir, "adt_a01.hl7")
    assert "MSA|AE|MSG00102|OBX-3 code 'K+'" in send(fhir, "bad_local_lab_code.hl7")
    assert [r for r in fhir.resources if r["resourceType"] == "Observation"] == []


def test_an_unknown_hospital_is_refused_loudly() -> None:
    fhir = InMemoryFhir()
    stranger = text("adt_a01.hl7").replace("MERCY_GENERAL", "MYSTERY_CLINIC")
    ack, _ = process_message(stranger, fhir, SOURCES)  # type: ignore[arg-type]
    assert "MSA|AE|MSG00001|unknown sending facility 'MYSTERY_CLINIC'" in ack
    assert fhir.resources == []


def test_problem_messages_go_to_the_error_queue_with_the_raw_text(tmp_path: Path) -> None:
    log = EventLog(tmp_path)
    fhir = InMemoryFhir()
    send(fhir, "adt_a01.hl7", log)
    send(fhir, "bad_mrn_in_pid2.hl7", log)
    send(fhir, "adt_a03.hl7", log)
    send(fhir, "adt_a01.hl7", log)  # stale
    outcomes = [event["outcome"] for event in log.read(EVENTS_FILE)]
    assert outcomes == ["accepted", "rejected", "accepted", "stale_ignored"]
    queued = log.read(ERROR_QUEUE_FILE)
    assert [q["kind"] for q in queued] == ["rejected", "stale_ignored"]
    assert "MRN12345" in queued[0]["raw_message"]  # the message itself is kept for replay
