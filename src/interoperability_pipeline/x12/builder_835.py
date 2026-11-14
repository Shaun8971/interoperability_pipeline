"""Build an X12 835 (health care claim payment / remittance advice) from a decision.

What it does: writes the payer's explanation of payment: total paid, then per claim and per
service line what was billed, paid and adjusted (and why).
Where it sits in the pipeline: adjudication --> **builder_835** --> parser_835 --> FHIR EOB.
Standard and spec: ASC X12 835, version 005010X221A1. Free reference:
  https://www.stedi.com/edi/x12-005010/835  (BPR, TRN, N1, CLP, SVC, CAS segment pages linked there)
Inputs / outputs: ClaimAdjudication + claims config + control number + time -> X12 text.
How to extend: add a PLB segment for provider-level adjustments, or more claims (one CLP
group per claim).
"""

from datetime import datetime
from typing import Any

from interoperability_pipeline.x12.adjudication import ClaimAdjudication
from interoperability_pipeline.x12.builder_837p import (
    SEGMENT_END,
    SUB_ELEMENT,
    build_isa,
    format_amount,
    make_segment,
)

# --- Constants ----------------------------------------------------------------------------------
FUNCTIONAL_ID_CODE = "HP"  # GS01: health care claim payment/advice
TRANSACTION_SET_ID = "835"  # ST01
IMPLEMENTATION_REFERENCE = "005010X221A1"  # GS08 / ST03: the 835 implementation guide
PAYMENT_HANDLING_INFO_AND_REMIT = "I"  # BPR01: payment and remittance information together
CREDIT = "C"  # BPR03: money flows from payer to payee
PAYMENT_METHOD_CHECK = "CHK"  # BPR04: paid by check (synthetic; nothing is moved)
BPR_UNUSED_BANK_FIELDS = 11  # BPR05-BPR15 are bank details we leave empty
TRACE_TYPE_CURRENT = "1"  # TRN01: current transaction trace
PAYER_ORIGINATOR = "1999999999"  # TRN03: synthetic originating company identifier
ENTITY_PAYER, ENTITY_PAYEE, ENTITY_PATIENT = "PR", "PE", "QC"
QUALIFIER_PAYER_ID, QUALIFIER_NPI, QUALIFIER_MEMBER_ID = "XV", "XX", "MI"
CLAIM_STATUS_PROCESSED_PRIMARY = "1"  # CLP02: processed as primary
CLAIM_STATUS_DENIED = "4"  # CLP02: denied
CLAIM_FILING_COMMERCIAL = "CI"  # CLP06: commercial insurance
PLACE_OF_SERVICE_OFFICE = "11"  # CLP08
CLAIM_FREQUENCY_ORIGINAL = "1"  # CLP09
SERVICE_DATE_QUALIFIER = "472"  # DTM01: service date
PRODUCT_QUALIFIER_HCPCS = "HC"  # SVC01-1


def build_835(
    adjudication: ClaimAdjudication,
    claims: dict[str, Any],
    interchange_control_number: int,
    now: datetime,
) -> str:
    """Build the full 835 interchange, one segment per line.

    Example (Scenario A shape): BPR*I*760*C*CHK...; CLP*V0001*1*1200*760*190*CI*PCNV0001*11*1;
    SVC*HC:99213*200*120; CAS*CO*45*50; CAS*PR*2*30; SVC*HC:72148*1000*640; ...
    """
    request, payer, provider = adjudication.request, claims["payer"], claims["billing_provider"]
    interchange = claims["interchange"]
    # The payer is the sender of an 835, so sender and receiver swap relative to the 837.
    swapped = {
        **interchange,
        "sender_id": interchange["receiver_id"],
        "receiver_id": interchange["sender_id"],
    }
    paid = adjudication.total_paid
    patient_responsibility = adjudication.total_for_group(claims["payer_codes"]["group_patient"])
    any_paid = any(line.paid > 0 for line in adjudication.lines)

    body = [
        make_segment("ST", TRANSACTION_SET_ID, "0001", IMPLEMENTATION_REFERENCE),
        make_segment(  # BPR: financial information: total paid and how
            "BPR",
            PAYMENT_HANDLING_INFO_AND_REMIT,
            format_amount(paid),
            CREDIT,
            PAYMENT_METHOD_CHECK,
            *[""] * BPR_UNUSED_BANK_FIELDS,
            now.strftime("%Y%m%d"),
        ),
        make_segment(
            "TRN", TRACE_TYPE_CURRENT, f"TRACE{interchange_control_number:07d}", PAYER_ORIGINATOR
        ),
        make_segment(
            "N1", ENTITY_PAYER, payer["name"], QUALIFIER_PAYER_ID, payer["payer_id"]
        ),  # 1000A
        make_segment("N1", ENTITY_PAYEE, provider["name"], QUALIFIER_NPI, provider["npi"]),  # 1000B
        make_segment("LX", "1"),
        make_segment(  # CLP: one claim: billed, paid, patient responsibility
            "CLP",
            request.patient_control_number,  # CLP01: the claim reference we sent in CLM01
            CLAIM_STATUS_PROCESSED_PRIMARY if any_paid else CLAIM_STATUS_DENIED,  # CLP02
            format_amount(request.total_charge),  # CLP03: total billed
            format_amount(paid),  # CLP04: total paid
            format_amount(patient_responsibility),  # CLP05: patient responsibility
            CLAIM_FILING_COMMERCIAL,  # CLP06
            adjudication.payer_claim_number,  # CLP07: payer's own claim number
            PLACE_OF_SERVICE_OFFICE,
            CLAIM_FREQUENCY_ORIGINAL,  # CLP08/09
        ),
        make_segment(  # NM1*QC: the patient
            "NM1",
            ENTITY_PATIENT,
            "1",
            request.family_name,
            request.given_name,
            request.middle_initial,
            "",
            "",
            QUALIFIER_MEMBER_ID,
            request.member_id,
        ),
    ]
    for line in adjudication.lines:
        body.append(
            make_segment(  # SVC: one service line: billed and paid
                "SVC",
                f"{PRODUCT_QUALIFIER_HCPCS}{SUB_ELEMENT}{line.procedure_code}",
                format_amount(line.charge),
                format_amount(line.paid),
            )
        )
        body.append(make_segment("DTM", SERVICE_DATE_QUALIFIER, line.service_date))
        for adjustment in line.adjustments:  # CAS: group, reason, amount
            body.append(
                make_segment(
                    "CAS", adjustment.group, adjustment.reason, format_amount(adjustment.amount)
                )
            )
    body.append(make_segment("SE", str(len(body) + 1), "0001"))  # counts ST..SE inclusive

    group_header = make_segment(
        "GS",
        FUNCTIONAL_ID_CODE,
        swapped["sender_id"],
        swapped["receiver_id"],
        now.strftime("%Y%m%d"),
        now.strftime("%H%M"),
        "1",
        "X",
        IMPLEMENTATION_REFERENCE,
    )
    trailer = [
        make_segment("GE", "1", "1"),
        make_segment("IEA", "1", f"{interchange_control_number:09d}"),
    ]
    segments = [
        build_isa(swapped, interchange_control_number, now).rstrip(SEGMENT_END),
        group_header,
    ]
    segments += body + trailer
    return "\n".join(segment + SEGMENT_END for segment in segments) + "\n"
