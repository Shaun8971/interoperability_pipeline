"""Map a balanced 835 Remittance to FHIR ExplanationOfBenefit (and Coverage). Pure functions.

What it does: expresses what the payer decided as FHIR so the patient portal (Module 3) can
show it. Each 835 amount lands in a FHIR field (see the comments marked "835 -> EOB").
Where it sits in the pipeline: parser_835 + reconcile --> **eob_mapper** --> FHIR (hub).
Standard and spec: FHIR ExplanationOfBenefit https://hl7.org/fhir/R4/explanationofbenefit.html ,
Coverage https://hl7.org/fhir/R4/coverage.html , adjudication codes
http://terminology.hl7.org/CodeSystem/adjudication . CARC/group codes: https://x12.org/codes
Inputs / outputs: RemittanceClaim + ids/config -> FHIR resource dicts.
How to extend: add ``total`` or ``addItem`` entries; for a second payer give it its own
identifier systems in config.

835 vs EOB: the 835 is a business document from payer to *provider* (money and reasons).
The EOB is the FHIR record meant for the *patient* and apps: what was billed, allowed, paid
and owed.
"""

from decimal import Decimal
from typing import Any

from interoperability_pipeline.x12.parser_835 import (
    GROUP_PATIENT_RESPONSIBILITY,
    Remittance,
    RemittanceClaim,
    RemittanceLine,
)

# --- Code systems and codes -----------------------------------------------------------------------
ADJUDICATION_SYSTEM = "http://terminology.hl7.org/CodeSystem/adjudication"
CLAIM_TYPE_SYSTEM = "http://terminology.hl7.org/CodeSystem/claim-type"
CLAIM_TYPE_PROFESSIONAL = "professional"  # an 837P claim is a professional claim
CPT_SYSTEM = "http://www.ama-assn.org/go/cpt"
X12_GROUP_CODE_SYSTEM = "https://x12.org/codes/claim-adjustment-group-codes"  # CO / PR / ...
X12_REASON_CODE_SYSTEM = "https://x12.org/codes/claim-adjustment-reason-codes"  # 45 / 2 / 197 ...
CURRENCY = "USD"
EOB_STATUS_ACTIVE = "active"
EOB_USE_CLAIM = "claim"  # this EOB responds to a claim (not a pre-authorization)
EOB_OUTCOME_COMPLETE = "complete"  # the payer finished processing the claim
COVERAGE_STATUS_ACTIVE = "active"


def _hl7_style_date(compact: str) -> str:
    """YYYYMMDD -> YYYY-MM-DD (X12 dates to FHIR dates).

    Example:
        >>> _hl7_style_date("20261114")
        '2026-11-14'
    """
    return f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"


def _money(amount: Decimal) -> dict[str, Any]:
    return {"value": float(amount), "currency": CURRENCY}


def _adjudication(category: str, amount: Decimal) -> dict[str, Any]:
    return {
        "category": {"coding": [{"system": ADJUDICATION_SYSTEM, "code": category}]},
        "amount": _money(amount),
    }


def coverage_from_remittance(
    remittance: Remittance, claim: RemittanceClaim, patient_id: str, member_system: str
) -> dict[str, Any]:
    """Jane's insurance (FHIR Coverage), needed because EOB.insurance must point at one.

    Example: member W123456789 with SYNTHETIC HEALTH PLAN, beneficiary Patient/1000.
    """
    return {
        "resourceType": "Coverage",
        "identifier": [{"system": member_system, "value": claim.member_id}],  # NM109 (member id)
        "status": COVERAGE_STATUS_ACTIVE,
        "subscriberId": claim.member_id,
        "beneficiary": {"reference": f"Patient/{patient_id}"},
        "payor": [{"display": remittance.payer_name}],  # N102
    }


def _item(sequence: int, line: RemittanceLine) -> dict[str, Any]:
    """One 835 service line (SVC + CAS) -> one EOB item with its adjudication entries."""
    adjudications = [
        _adjudication("submitted", line.charge),  # 835 SVC02 billed -> submitted
        _adjudication("benefit", line.paid),  # 835 SVC03 paid -> benefit
    ]
    for adjustment in line.adjustments:  # 835 CAS group/reason/amount -> one entry each
        entry = _adjudication("unallocated", adjustment.amount)
        entry["category"]["coding"][0] = {
            "system": X12_GROUP_CODE_SYSTEM,
            "code": adjustment.group,  # CO or PR
        }
        entry["reason"] = {  # why: the reason code (45, 2, 197...)
            "coding": [{"system": X12_REASON_CODE_SYSTEM, "code": adjustment.reason}]
        }
        adjudications.append(entry)
    item: dict[str, Any] = {
        "sequence": sequence,
        "productOrService": {"coding": [{"system": CPT_SYSTEM, "code": line.procedure_code}]},
        "adjudication": adjudications,
    }
    if line.service_date:  # 835 DTM*472 -> servicedDate (YYYYMMDD -> YYYY-MM-DD)
        item["servicedDate"] = _hl7_style_date(line.service_date)
    return item


def eob_from_remittance(
    remittance: Remittance,
    claim: RemittanceClaim,
    patient_id: str,
    coverage_id: str,
    claim_identifier_system: str,
    created: str,
) -> dict[str, Any]:
    """Build the ExplanationOfBenefit for one claim.

    ``created`` is the FHIR dateTime this EOB is created at (passed in so tests are exact).

    Example (Scenario B): total billed 1200, paid 120, patient owes 30; the MRI item carries
    benefit 0 and a CO-197 entry of 1000.
    """
    patient_owes = sum(
        (
            a.amount
            for line in claim.lines
            for a in line.adjustments
            if a.group == GROUP_PATIENT_RESPONSIBILITY
        ),
        Decimal("0"),
    )
    return {
        "resourceType": "ExplanationOfBenefit",
        "identifier": [
            {"system": claim_identifier_system, "value": claim.payer_claim_number}
        ],  # CLP07
        "status": EOB_STATUS_ACTIVE,
        "type": {"coding": [{"system": CLAIM_TYPE_SYSTEM, "code": CLAIM_TYPE_PROFESSIONAL}]},
        "use": EOB_USE_CLAIM,
        "patient": {"reference": f"Patient/{patient_id}"},
        "created": created,
        "insurer": {
            "display": remittance.payer_name,  # 835 N1*PR -> insurer
            "identifier": {"value": remittance.payer_id},
        },
        "provider": {
            "display": remittance.payee_name,  # 835 N1*PE -> provider
            "identifier": {"value": remittance.payee_npi},
        },
        "outcome": EOB_OUTCOME_COMPLETE,
        "insurance": [{"focal": True, "coverage": {"reference": f"Coverage/{coverage_id}"}}],
        "item": [_item(number, line) for number, line in enumerate(claim.lines, start=1)],
        "total": [
            _adjudication("submitted", claim.charge),  # 835 CLP03 -> total submitted
            _adjudication("benefit", claim.paid),  # 835 CLP04 -> total benefit
        ],
        "payment": {  # 835 BPR02 / BPR16 -> payment amount / date
            "amount": _money(remittance.payment_total),
            "date": _hl7_style_date(remittance.payment_date),
        },
        "extension": [  # 835 CLP05 patient responsibility: kept visible for the portal
            {
                "url": "http://interop-pipeline.example.org/StructureDefinition/patient-responsibility",
                "valueMoney": _money(patient_owes),
            }
        ],
    }
