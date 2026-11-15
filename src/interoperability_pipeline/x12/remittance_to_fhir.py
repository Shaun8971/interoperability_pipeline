"""Read an 835 file, check it balances, and store it in FHIR as an ExplanationOfBenefit.

What it does: parse -> reconcile -> (only if balanced) write Coverage and EOB idempotently.
Where it sits in the pipeline: payer --835--> **this** --> FHIR (hub) --> patient portal.
Standard and spec: see parser_835.py and eob_mapper.py.
Inputs / outputs: path to an 835 file -> Coverage + ExplanationOfBenefit in FHIR, or a
refusal that lists every imbalance and writes nothing.
How to extend: send refused files to the error queue (Module 4).
Run:  python -m interoperability_pipeline.x12.remittance_to_fhir <path to an 835 file>
"""

import sys
from datetime import datetime
from pathlib import Path

from interoperability_pipeline.common.errors import MappingError
from interoperability_pipeline.common.event_log import OUTCOME_ACCEPTED, OUTCOME_REJECTED, EventLog
from interoperability_pipeline.common.settings import (
    CLAIMS_SETTINGS_PATH,
    PROJECT_ROOT,
    load_settings,
)
from interoperability_pipeline.fhir.client import FhirClient
from interoperability_pipeline.hl7v2.mappers import (
    identifier_search_value,
)
from interoperability_pipeline.mapping.sources import load_source
from interoperability_pipeline.x12.eob_mapper import coverage_from_remittance, eob_from_remittance
from interoperability_pipeline.x12.parser_835 import parse_835, reconcile

HOSPITAL_AUTHORITY = "MERCY"
JANE_MRN = "MRN12345"
# Identifier systems for Coverage member ids and payer claim numbers (config, not code).
MEMBER_ID_SYSTEM = "http://synthetic-health-plan.example.org/member-id"
PAYER_CLAIM_SYSTEM = "http://synthetic-health-plan.example.org/claim-number"


def store_remittance(text: str, client: FhirClient, settings: dict) -> list[str]:  # type: ignore[type-arg]
    """Store one 835. Returns what was written; raises MappingError if it does not balance.

    Example: returns ['Coverage/1006 created', 'ExplanationOfBenefit/1007 created']
    """
    remittance = parse_835(text)
    problems = reconcile(remittance)
    if problems:  # never post an unbalanced remittance
        raise MappingError("835 does not balance: " + "; ".join(problems))
    config = load_source("hospital_a")
    patient_system = config.patient_identifier_systems[HOSPITAL_AUTHORITY]
    patient_id = client.find_id(
        "Patient", {"identifier": identifier_search_value(patient_system, JANE_MRN)}
    )
    if patient_id is None:
        raise MappingError("Jane's Patient is not in FHIR; send the HL7 messages first")

    written: list[str] = []
    for claim in remittance.claims:
        coverage = coverage_from_remittance(remittance, claim, patient_id, MEMBER_ID_SYSTEM)
        coverage_result = client.conditional_update(
            "Coverage",
            {"identifier": identifier_search_value(MEMBER_ID_SYSTEM, claim.member_id)},
            coverage,
        )
        eob = eob_from_remittance(
            remittance,
            claim,
            patient_id,
            coverage_result.resource_id,
            PAYER_CLAIM_SYSTEM,
            created=datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        eob_result = client.conditional_update(
            "ExplanationOfBenefit",
            {"identifier": identifier_search_value(PAYER_CLAIM_SYSTEM, claim.payer_claim_number)},
            eob,
        )
        for result in (coverage_result, eob_result):
            outcome = "created" if result.created else "updated"
            written.append(f"{result.resource_type}/{result.resource_id} {outcome}")
    return written


def main() -> None:
    settings = load_settings()
    client = FhirClient(settings["fhir"]["base_url"], settings["fhir"]["timeout_seconds"])
    event_log = EventLog(PROJECT_ROOT / settings["paths"]["data_directory"])
    text = Path(sys.argv[1]).read_text()
    try:
        written = store_remittance(text, client, settings)
    except MappingError as error:
        print(f"REFUSED: {error}")
        event_log.record(
            source="payer",
            message_type="835",
            control_id=Path(sys.argv[1]).name,
            outcome=OUTCOME_REJECTED,
            detail=str(error),
        )
        event_log.enqueue(
            kind=OUTCOME_REJECTED,
            source="payer",
            control_id=Path(sys.argv[1]).name,
            reasons=[str(error)],
            raw_message=text,
        )
        raise SystemExit(1) from error
    event_log.record(
        source="payer",
        message_type="835",
        control_id=Path(sys.argv[1]).name,
        outcome=OUTCOME_ACCEPTED,
        resources=written,
    )
    for line in written:
        print(line)


if __name__ == "__main__":
    load_settings(CLAIMS_SETTINGS_PATH)  # fail early if the claims config is missing
    main()
