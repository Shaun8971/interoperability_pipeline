"""Tests for HL7 v2 -> FHIR mapping (pure functions) and the FHIR write idempotency."""

from pathlib import Path

import pytest

from interoperability_pipeline.common.errors import MappingError
from interoperability_pipeline.common.settings import load_settings
from interoperability_pipeline.fhir.client import FhirClient
from interoperability_pipeline.hl7v2.listener import process_message
from interoperability_pipeline.hl7v2.mappers import (
    SourceConfig,
    encounter_from_hl7,
    observation_from_hl7,
    patient_from_hl7,
)
from interoperability_pipeline.hl7v2.parser import find_segment, split_message

SAMPLES_DIRECTORY = Path(__file__).parent.parent / "samples" / "hl7"
SOURCE_CONFIG = SourceConfig(
    patient_identifier_systems={"MERCY": "http://mercy-general.example.org/mrn"},
    visit_identifier_systems={"MERCY": "http://mercy-general.example.org/visit"},
    observation_identifier_systems={"MERCY": "http://mercy-general.example.org/lab-result"},
    timezone="America/New_York",
)


def load_text(file_name: str) -> str:
    return (SAMPLES_DIRECTORY / file_name).read_text().strip().replace("\n", "\r")


def test_patient_mapping_for_jane() -> None:
    patient = patient_from_hl7(split_message(load_text("adt_a01.hl7")), SOURCE_CONFIG)
    assert patient["resourceType"] == "Patient"
    assert patient["identifier"][0]["system"] == "http://mercy-general.example.org/mrn"
    assert patient["identifier"][0]["value"] == "MRN12345"  # PID-3.1
    assert patient["name"] == [{"family": "DOE", "given": ["JANE", "Q"]}]  # PID-5
    assert patient["birthDate"] == "1980-03-15"  # PID-7
    assert patient["gender"] == "female"  # PID-8
    assert patient["address"][0]["city"] == "ITHACA"  # PID-11.3


def test_encounter_mapping_for_admit_and_discharge() -> None:
    admit = encounter_from_hl7(split_message(load_text("adt_a01.hl7")), SOURCE_CONFIG, "1000")
    assert admit["identifier"][0]["value"] == "V0001"  # PV1-19
    assert admit["status"] == "in-progress"  # MSH-9.2 = A01
    assert admit["class"]["code"] == "IMP"  # PV1-2 = I
    assert admit["subject"] == {"reference": "Patient/1000"}
    assert admit["period"] == {"start": "2026-11-14T09:30:00-05:00"}  # PV1-44, New York time
    discharge = encounter_from_hl7(split_message(load_text("adt_a03.hl7")), SOURCE_CONFIG, "1000")
    assert discharge["status"] == "finished"  # MSH-9.2 = A03
    assert discharge["period"]["end"] == "2026-11-16T11:00:00-05:00"  # PV1-45


def test_observation_mapping_for_potassium() -> None:
    segments = split_message(load_text("oru_r01.hl7"))
    obx = find_segment(segments, "OBX")
    observation = observation_from_hl7(segments, obx, SOURCE_CONFIG, "1000", "1001")
    assert observation["identifier"][0]["value"] == "LAB0001-1"  # OBR-3 + OBX-1
    assert observation["status"] == "final"  # OBX-11 = F
    assert observation["code"]["coding"][0]["system"] == "http://loinc.org"  # OBX-3.3 = LN
    assert observation["code"]["coding"][0]["code"] == "2823-3"  # OBX-3.1
    assert observation["valueQuantity"]["value"] == 4.1  # OBX-5
    assert observation["valueQuantity"]["unit"] == "mmol/L"  # OBX-6
    assert observation["effectiveDateTime"] == "2026-11-14T10:00:00-05:00"  # OBX-14
    assert observation["subject"] == {"reference": "Patient/1000"}
    assert observation["encounter"] == {"reference": "Encounter/1001"}


def test_local_lab_code_is_refused_not_guessed() -> None:
    text = load_text("oru_r01.hl7").replace(
        "2823-3^Potassium [Moles/volume] in Serum or Plasma^LN||", "K+^Potassium^LOCAL||"
    )
    segments = split_message(text)
    with pytest.raises(MappingError):
        observation_from_hl7(segments, find_segment(segments, "OBX"), SOURCE_CONFIG, "1000", None)


def test_unknown_assigning_authority_is_refused() -> None:
    with pytest.raises(MappingError):
        patient_from_hl7(
            split_message(load_text("adt_a01.hl7")),
            SourceConfig({}, {}, {}, "America/New_York"),
        )


def test_bad_birth_date_is_refused() -> None:
    text = load_text("adt_a01.hl7").replace("19800315", "1980-03-15")
    with pytest.raises(MappingError):
        patient_from_hl7(split_message(text), SOURCE_CONFIG)


def test_resending_the_same_message_never_duplicates_jane() -> None:
    """Integration test: needs the HAPI server from docker-compose.yml (skipped if down)."""
    settings = load_settings()
    client = FhirClient(settings["fhir"]["base_url"], settings["fhir"]["timeout_seconds"])
    try:
        client.count("Patient", {})
    except Exception:  # noqa: BLE001
        pytest.skip("FHIR server not running")
    search = {"identifier": "http://mercy-general.example.org/mrn|MRN12345"}
    for _ in range(3):
        ack_text, _ = process_message(load_text("adt_a01.hl7"), client, SOURCE_CONFIG)
        assert "MSA|AA|" in ack_text
    assert client.count("Patient", search) == 1
    visit = {"identifier": "http://mercy-general.example.org/visit|V0001"}
    assert client.count("Encounter", visit) == 1  # the visit is also stored once
    for _ in range(2):
        ack_text, _ = process_message(load_text("oru_r01.hl7"), client, SOURCE_CONFIG)
        assert "MSA|AA|" in ack_text
    result = {"identifier": "http://mercy-general.example.org/lab-result|LAB0001-1"}
    assert client.count("Observation", result) == 1  # the potassium is stored once


def test_fhir_server_down_gives_ar_not_a_silent_drop() -> None:
    dead_client = FhirClient("http://127.0.0.1:9/fhir", timeout_seconds=1)  # nothing listens on 9
    ack_text, _ = process_message(load_text("adt_a01.hl7"), dead_client, SOURCE_CONFIG)
    assert "MSA|AR|MSG00001|" in ack_text
