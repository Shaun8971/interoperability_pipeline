"""Gather everything an 837P claim needs from FHIR into one plain object.

What it does: reads Jane's Patient, Encounter, Condition and Procedure resources and the
fee schedule, and returns a ``ClaimData``. This is the only part of claim building that
touches the network; the X12 text is produced elsewhere by pure functions.
Where it sits in the pipeline: FHIR (hub) --> **claim_data** --> builder_837p --> 837P file.
Standard and spec: FHIR Patient/Encounter/Condition/Procedure (R4), see https://hl7.org/fhir/R4/.
Inputs / outputs: FhirClient + settings + claims config -> ClaimData.
How to extend: for another patient or visit, pass a different MRN/visit number; for another
payer, give the config a second payer table (Module 4 style, per-source config).
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from interoperability_pipeline.common.errors import MappingError
from interoperability_pipeline.fhir.client import FhirClient
from interoperability_pipeline.hl7v2.mappers import (
    identifier_search_value,
)
from interoperability_pipeline.mapping.sources import load_source

# --- Constants --------------------------------------------------------------------------
HOSPITAL_AUTHORITY = "MERCY"  # PID-3.4 authority whose identifier systems we search with
JANE_MRN = "MRN12345"  # the synthetic patient's medical record number
JANE_VISIT_NUMBER = "V0001"  # the visit being billed
# FHIR Patient.gender -> X12 DMG03 gender code
GENDER_TO_X12 = {"female": "F", "male": "M"}
X12_GENDER_UNKNOWN = "U"


@dataclass(frozen=True)
class ServiceLine:
    """One billable service (becomes an LX + SV1 + DTP group)."""

    procedure_code: str  # CPT code, e.g. "99213"
    charge: Decimal  # what the hospital bills for it
    service_date: str  # YYYYMMDD


@dataclass(frozen=True)
class ClaimData:
    """All inputs for one professional claim."""

    patient_control_number: str  # CLM01: our own reference for this claim (the visit number)
    family_name: str
    given_name: str
    middle_initial: str
    birth_date: str  # YYYYMMDD
    gender: str  # F / M / U
    street: str
    city: str
    state: str
    postal_code: str
    diagnosis_codes: list[str]  # ICD-10-CM codes, principal diagnosis first
    service_lines: list[ServiceLine]
    prior_auth_number: str | None  # None = the claim carries no prior authorization

    @property
    def total_charge(self) -> Decimal:
        """Sum of the line charges (CLM02 must equal this)."""
        return sum((line.charge for line in self.service_lines), Decimal("0"))


def load_claim_data(
    client: FhirClient,
    settings: dict[str, Any],
    claims: dict[str, Any],
    prior_auth_number: str | None = None,
) -> ClaimData:
    """Read Jane's visit from FHIR and return the claim inputs.

    Raises MappingError if a needed resource is missing or a procedure has no fee in config:
    a claim with a made-up price or a missing diagnosis would be rejected (or worse, paid wrongly).

    Example:
        load_claim_data(client, settings, claims).total_charge -> Decimal("1200.00")
    """
    config = load_source("hospital_a")
    patient_system = config.patient_identifier_systems[HOSPITAL_AUTHORITY]
    visit_system = config.visit_identifier_systems[HOSPITAL_AUTHORITY]
    patients = client.search(
        "Patient", {"identifier": identifier_search_value(patient_system, JANE_MRN)}
    )
    encounters = client.search(
        "Encounter", {"identifier": identifier_search_value(visit_system, JANE_VISIT_NUMBER)}
    )
    if not patients or not encounters:
        raise MappingError(
            "Jane's Patient or Encounter is not in FHIR; send the HL7 messages first"
        )
    patient, encounter = patients[0], encounters[0]
    encounter_reference = {"encounter": f"Encounter/{encounter['id']}"}
    conditions = client.search("Condition", encounter_reference)
    procedures = client.search("Procedure", encounter_reference)
    if not conditions:
        raise MappingError("no Condition found for the visit; run simulators/seed_claim_data.py")
    if not procedures:
        raise MappingError("no Procedure found for the visit; run simulators/seed_claim_data.py")

    charges = {entry["code"]: Decimal(entry["charge"]) for entry in claims["procedures"]}
    fee_schedule_order = {
        entry["code"]: position for position, entry in enumerate(claims["procedures"])
    }
    service_lines: list[ServiceLine] = []
    for procedure in sorted(
        procedures, key=lambda item: fee_schedule_order.get(_procedure_code(item), len(charges))
    ):
        code = _procedure_code(procedure)
        if code not in charges:
            raise MappingError(f"procedure {code} has no charge in the fee schedule")
        performed = procedure["performedDateTime"][:10]  # FHIR date part: YYYY-MM-DD
        service_lines.append(ServiceLine(code, charges[code], performed.replace("-", "")))

    name = patient["name"][0]  # Patient.name (mapped from PID-5)
    given = name.get("given", [])
    address = patient["address"][0]  # Patient.address (mapped from PID-11)
    return ClaimData(
        patient_control_number=encounter["identifier"][0]["value"],
        family_name=name["family"],
        given_name=given[0] if given else "",
        middle_initial=given[1][:1] if len(given) > 1 else "",
        birth_date=patient["birthDate"].replace("-", ""),
        gender=GENDER_TO_X12.get(patient.get("gender", ""), X12_GENDER_UNKNOWN),
        street=address["line"][0],
        city=address["city"],
        state=address["state"],
        postal_code=address["postalCode"],
        diagnosis_codes=[condition["code"]["coding"][0]["code"] for condition in conditions],
        service_lines=service_lines,
        prior_auth_number=prior_auth_number,
    )


def _procedure_code(procedure: dict[str, Any]) -> str:
    return procedure["code"]["coding"][0]["code"]
