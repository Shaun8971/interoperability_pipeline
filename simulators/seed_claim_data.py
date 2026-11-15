"""Seed Jane's diagnosis and procedures into FHIR so the 837P builder has something to bill.

What it does: writes one Condition (ICD-10) and one Procedure (CPT) per configured service,
all pointing at Jane's existing Patient and Encounter, using idempotent conditional updates.
Where it sits in the pipeline: before Module 2's claim builder. In real life diagnoses and
procedures come from the billing/clinical system, not from ADT messages, so we simulate that.
Standard and spec: FHIR Condition https://hl7.org/fhir/R4/condition.html and
Procedure https://hl7.org/fhir/R4/procedure.html. ICD-10-CM: https://www.cdc.gov/nchs/icd/icd-10-cm/.
Inputs / outputs: config/claims.toml + config/settings.toml + Jane's Patient/Encounter in FHIR
(run the HL7 listener and send the A01 first) -> Condition and Procedure resources in FHIR.
How to extend: add [[diagnoses]] or [[procedures]] tables to config/claims.toml and rerun.
Run:  python simulators/seed_claim_data.py
"""

from typing import Any

from interoperability_pipeline.common.errors import PipelineError
from interoperability_pipeline.common.settings import CLAIMS_SETTINGS_PATH, load_settings
from interoperability_pipeline.fhir.client import FhirClient
from interoperability_pipeline.hl7v2.mappers import (
    identifier_search_value,
)
from interoperability_pipeline.mapping.sources import load_source

# --- Constants ------------------------------------------------------------------------
CONDITION_CATEGORY_SYSTEM = "http://terminology.hl7.org/CodeSystem/condition-category"
CONDITION_CLINICAL_SYSTEM = "http://terminology.hl7.org/CodeSystem/condition-clinical"
CATEGORY_ENCOUNTER_DIAGNOSIS = "encounter-diagnosis"  # a diagnosis made during this visit
CLINICAL_STATUS_ACTIVE = "active"
PROCEDURE_STATUS_COMPLETED = "completed"
# When the services were performed (synthetic; inside Jane's admit-to-discharge window).
PERFORMED_AT = "2026-11-14T09:45:00-05:00"


def build_condition(
    diagnosis: dict[str, str], icd10_system: str, patient_id: str, encounter_id: str
) -> dict[str, Any]:
    """Build a FHIR Condition for one diagnosis.

    Example: {"code": "I10", "text": "Essential hypertension"} -> Condition with ICD-10 I10.
    """
    return {
        "resourceType": "Condition",
        "clinicalStatus": {
            "coding": [{"system": CONDITION_CLINICAL_SYSTEM, "code": CLINICAL_STATUS_ACTIVE}]
        },
        "category": [
            {
                "coding": [
                    {"system": CONDITION_CATEGORY_SYSTEM, "code": CATEGORY_ENCOUNTER_DIAGNOSIS}
                ]
            }
        ],
        "code": {  # diagnosis code -> Condition.code
            "coding": [
                {"system": icd10_system, "code": diagnosis["code"], "display": diagnosis["text"]}
            ],
            "text": diagnosis["text"],
        },
        "subject": {"reference": f"Patient/{patient_id}"},
        "encounter": {"reference": f"Encounter/{encounter_id}"},
    }


def build_procedure(
    procedure: dict[str, str], cpt_system: str, patient_id: str, encounter_id: str
) -> dict[str, Any]:
    """Build a FHIR Procedure for one billable service.

    Example: {"code": "99213", ...} -> Procedure with CPT 99213, status completed.
    """
    return {
        "resourceType": "Procedure",
        "status": PROCEDURE_STATUS_COMPLETED,
        "code": {  # service code -> Procedure.code
            "coding": [
                {"system": cpt_system, "code": procedure["code"], "display": procedure["text"]}
            ],
            "text": procedure["text"],
        },
        "subject": {"reference": f"Patient/{patient_id}"},
        "encounter": {"reference": f"Encounter/{encounter_id}"},
        "performedDateTime": PERFORMED_AT,
    }


def seed() -> None:
    """Find Jane's Patient and Encounter, then write the Condition and Procedures."""
    settings = load_settings()
    claims = load_settings(CLAIMS_SETTINGS_PATH)
    config = load_source("hospital_a")
    client = FhirClient(settings["fhir"]["base_url"], settings["fhir"]["timeout_seconds"])

    patient_system = config.patient_identifier_systems["MERCY"]
    visit_system = config.visit_identifier_systems["MERCY"]
    patient_id = client.find_id(
        "Patient", {"identifier": identifier_search_value(patient_system, "MRN12345")}
    )
    encounter_id = client.find_id(
        "Encounter", {"identifier": identifier_search_value(visit_system, "V0001")}
    )
    if patient_id is None or encounter_id is None:
        raise PipelineError(
            "Jane's Patient/Encounter not found: send the A01 through the listener first"
        )

    systems = claims["identifier_systems"]
    for diagnosis in claims["diagnoses"]:
        resource = build_condition(
            diagnosis, claims["code_systems"]["icd10"], patient_id, encounter_id
        )
        resource["identifier"] = [{"system": systems["condition"], "value": diagnosis["code"]}]
        _write(client, "Condition", systems["condition"], diagnosis["code"], resource)
    for procedure in claims["procedures"]:
        resource = build_procedure(
            procedure, claims["code_systems"]["cpt"], patient_id, encounter_id
        )
        resource["identifier"] = [{"system": systems["procedure"], "value": procedure["code"]}]
        _write(client, "Procedure", systems["procedure"], procedure["code"], resource)


def _write(
    client: FhirClient, resource_type: str, system: str, value: str, resource: dict[str, Any]
) -> None:
    result = client.conditional_update(
        resource_type, {"identifier": identifier_search_value(system, value)}, resource
    )
    outcome = "created" if result.created else "updated"
    print(f"{resource_type} {value}: {resource_type}/{result.resource_id} {outcome}")


if __name__ == "__main__":
    seed()
