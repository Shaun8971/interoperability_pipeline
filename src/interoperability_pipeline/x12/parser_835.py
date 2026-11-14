"""Parse an 835 remittance and check that its money balances (pure functions).

What it does: turns 835 text into plain objects, then verifies the arithmetic the payer
claims: every service line (billed = paid + adjustments), every claim (CLP03 = CLP04 + all
CAS amounts, CLP05 = the PR amounts) and the payment total (BPR02 = sum of CLP04).
Where it sits in the pipeline: payer --835--> **parser_835** --> reconcile --> EOB mapper.
Standard and spec: ASC X12 835 005010X221A1, https://www.stedi.com/edi/x12-005010/835
(BPR, CLP, SVC, CAS pages linked there). Group/reason codes: https://x12.org/codes
Inputs / outputs: 835 text -> Remittance; Remittance -> list of problem strings (empty = balanced).
How to extend: handle several CLP groups (already supported) or claim-level CAS / PLB
adjustments by extending ``_parse_claims`` and ``reconcile``.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from interoperability_pipeline.common.errors import X12FormatError
from interoperability_pipeline.x12.tokenizer import tokenize

# --- Segment IDs and qualifiers -------------------------------------------------------------
SEG_BPR, SEG_N1, SEG_CLP, SEG_NM1, SEG_SVC, SEG_DTM, SEG_CAS = (
    "BPR",
    "N1",
    "CLP",
    "NM1",
    "SVC",
    "DTM",
    "CAS",
)
ENTITY_PAYER, ENTITY_PAYEE, ENTITY_PATIENT = "PR", "PE", "QC"
SUB_ELEMENT = ":"
GROUP_PATIENT_RESPONSIBILITY = "PR"  # CAS group code meaning "the patient owes this"
CAS_TRIPLE_WIDTH = 3  # CAS repeats (reason, amount, quantity) up to six times


@dataclass(frozen=True)
class Cas:
    """One adjustment: group (CO/PR/...), reason code, amount."""

    group: str
    reason: str
    amount: Decimal


@dataclass
class RemittanceLine:
    procedure_code: str
    charge: Decimal  # SVC02
    paid: Decimal  # SVC03
    service_date: str = ""
    adjustments: list[Cas] = field(default_factory=list)


@dataclass
class RemittanceClaim:
    patient_control_number: str  # CLP01: matches CLM01 of the 837
    status: str  # CLP02
    charge: Decimal  # CLP03
    paid: Decimal  # CLP04
    patient_responsibility: Decimal  # CLP05
    payer_claim_number: str  # CLP07
    family_name: str = ""
    given_name: str = ""
    member_id: str = ""
    lines: list[RemittanceLine] = field(default_factory=list)


@dataclass
class Remittance:
    payer_name: str
    payer_id: str
    payee_name: str
    payee_npi: str
    payment_total: Decimal  # BPR02
    payment_date: str  # BPR16
    claims: list[RemittanceClaim] = field(default_factory=list)


def parse_835(text: str) -> Remittance:
    """Parse the 835 into a Remittance. Raises X12FormatError if essential segments are missing.

    Example:
        >>> parse_835(text).claims[0].lines[0].procedure_code
        '99213'
    """
    segments = tokenize(text)
    bpr = _first(segments, SEG_BPR)
    payer = _first(segments, SEG_N1, ENTITY_PAYER)
    payee = _first(segments, SEG_N1, ENTITY_PAYEE)
    remittance = Remittance(
        payer_name=payer[2],
        payer_id=payer[4],  # N102 name, N104 identifier
        payee_name=payee[2],
        payee_npi=payee[4],
        payment_total=Decimal(bpr[2]),  # BPR02
        payment_date=bpr[16],  # BPR16
    )
    current_claim: RemittanceClaim | None = None
    current_line: RemittanceLine | None = None
    for segment in segments:
        padded = segment + [""] * 10
        if segment[0] == SEG_CLP:
            current_claim = RemittanceClaim(
                patient_control_number=padded[1],
                status=padded[2],
                charge=Decimal(padded[3]),
                paid=Decimal(padded[4]),
                patient_responsibility=Decimal(padded[5] or "0"),
                payer_claim_number=padded[7],
            )
            remittance.claims.append(current_claim)
            current_line = None
        elif segment[0] == SEG_NM1 and padded[1] == ENTITY_PATIENT and current_claim:
            current_claim.family_name, current_claim.given_name = padded[3], padded[4]
            current_claim.member_id = padded[9]
        elif segment[0] == SEG_SVC and current_claim:
            current_line = RemittanceLine(
                procedure_code=padded[1].split(SUB_ELEMENT)[1],  # SVC01-2
                charge=Decimal(padded[2]),
                paid=Decimal(padded[3] or "0"),
            )
            current_claim.lines.append(current_line)
        elif segment[0] == SEG_DTM and current_line:
            current_line.service_date = padded[2]
        elif segment[0] == SEG_CAS and current_line:
            for start in range(2, len(segment), CAS_TRIPLE_WIDTH):  # reason, amount, quantity
                if segment[start]:
                    current_line.adjustments.append(
                        Cas(segment[1], segment[start], Decimal(segment[start + 1]))
                    )
    if not remittance.claims:
        raise X12FormatError("835 has no CLP (claim payment) segment")
    return remittance


def reconcile(remittance: Remittance) -> list[str]:
    """Return every way the money fails to balance; an empty list means it all adds up.

    Why: an 835 that does not add up must never be posted to the patient's record. The
    error may be the payer's, ours, or a corrupted file, and we cannot tell which.

    Example (one amount tampered with):
        ['claim V0001 line 99213: billed 200 != paid 120 + adjustments 70']
    """
    problems: list[str] = []
    for claim in remittance.claims:
        label = f"claim {claim.patient_control_number}"
        for line in claim.lines:
            adjusted = sum((a.amount for a in line.adjustments), Decimal("0"))
            if line.charge != line.paid + adjusted:
                problems.append(
                    f"{label} line {line.procedure_code}: billed {line.charge} "
                    f"!= paid {line.paid} + adjustments {adjusted}"
                )
        lines_paid = sum((line.paid for line in claim.lines), Decimal("0"))
        lines_adjusted = sum(
            (a.amount for line in claim.lines for a in line.adjustments), Decimal("0")
        )
        lines_patient = sum(
            (
                a.amount
                for line in claim.lines
                for a in line.adjustments
                if a.group == GROUP_PATIENT_RESPONSIBILITY
            ),
            Decimal("0"),
        )
        if claim.charge != claim.paid + lines_adjusted:
            problems.append(
                f"{label}: CLP03 billed {claim.charge} != CLP04 paid {claim.paid} "
                f"+ adjustments {lines_adjusted}"
            )
        if claim.paid != lines_paid:
            problems.append(
                f"{label}: CLP04 paid {claim.paid} != sum of line payments {lines_paid}"
            )
        if claim.patient_responsibility != lines_patient:
            problems.append(
                f"{label}: CLP05 patient responsibility {claim.patient_responsibility} "
                f"!= sum of PR adjustments {lines_patient}"
            )
    claims_paid = sum((claim.paid for claim in remittance.claims), Decimal("0"))
    if remittance.payment_total != claims_paid:
        problems.append(f"BPR02 payment {remittance.payment_total} != sum of CLP04 {claims_paid}")
    return problems


def _first(segments: list[list[str]], segment_id: str, qualifier: str | None = None) -> list[str]:
    for segment in segments:
        if segment[0] == segment_id and (qualifier is None or segment[1] == qualifier):
            return segment + [""] * 20
    raise X12FormatError(f"835 has no {segment_id} segment")
