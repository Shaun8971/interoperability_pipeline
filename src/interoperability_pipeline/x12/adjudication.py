"""The payer simulator's brain: read an 837P and decide what to pay (pure functions).

What it does: reads the claim from 837P text, applies three rules per service line, and
returns the outcome as plain objects the 835 builder turns into text.
  1. Pay the contract rate (never more than that; the rest is a contractual write-off).
  2. Apply coinsurance to the allowed amount (the patient pays that share).
  3. Deny a line that needs prior authorization when the claim has none.
Where it sits in the pipeline: 837P --> **adjudication** --> builder_835 --> 835.
Standard and spec: 837P loops/segments https://www.stedi.com/edi/x12-005010/837 ; adjustment
group and reason codes https://x12.org/codes (CO = provider loss, PR = patient owes).
Inputs / outputs: 837P text + claims config -> ClaimAdjudication.
How to extend: add a rule inside ``adjudicate_line`` (e.g. a deductible) and a reason code in
config/claims.toml [payer_codes]. A line denied for any reason pays $0 and carries a CO code.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from interoperability_pipeline.common.errors import X12FormatError
from interoperability_pipeline.x12.tokenizer import tokenize

# --- 837P segment IDs and qualifiers we read -------------------------------------------------
SEG_CLM, SEG_SV1, SEG_DTP, SEG_NM1, SEG_REF = "CLM", "SV1", "DTP", "NM1", "REF"
ENTITY_INSURED = "IL"  # NM101 of the subscriber loop
PRIOR_AUTH_QUALIFIER = "G1"  # REF01: prior authorization number
SUB_ELEMENT = ":"  # SV101 is "HC:99213"
CENTS = Decimal("0.01")
PAYER_CLAIM_PREFIX = "PCN"  # the payer's own claim number = prefix + our claim reference


@dataclass(frozen=True)
class ServiceRequest:
    """One SV1 line from the 837."""

    procedure_code: str
    charge: Decimal
    service_date: str  # YYYYMMDD


@dataclass(frozen=True)
class ClaimRequest:
    """Everything the payer needs from the 837."""

    patient_control_number: str  # CLM01
    total_charge: Decimal  # CLM02
    family_name: str
    given_name: str
    middle_initial: str
    member_id: str
    prior_auth_number: str | None  # REF*G1, if present
    service_lines: list[ServiceRequest]


@dataclass(frozen=True)
class Adjustment:
    """One CAS entry: who bears an amount (group), why (reason), how much."""

    group: str  # CO or PR
    reason: str  # reason code, e.g. "45"
    amount: Decimal


@dataclass(frozen=True)
class LineAdjudication:
    """The payer's decision for one service line."""

    procedure_code: str
    service_date: str
    charge: Decimal
    paid: Decimal
    adjustments: list[Adjustment]


@dataclass(frozen=True)
class ClaimAdjudication:
    """The payer's decision for the whole claim."""

    request: ClaimRequest
    payer_claim_number: str
    lines: list[LineAdjudication]

    @property
    def total_paid(self) -> Decimal:
        return sum((line.paid for line in self.lines), Decimal("0"))

    def total_for_group(self, group: str) -> Decimal:
        return sum(
            (a.amount for line in self.lines for a in line.adjustments if a.group == group),
            Decimal("0"),
        )


def read_claim_837(text: str) -> ClaimRequest:
    """Read the fields the payer needs out of an 837P.

    Raises X12FormatError if the claim has no CLM or no service lines: a payer must never
    "pay" a claim it could not read.

    Example:
        >>> read_claim_837(text).total_charge
        Decimal('1200')
    """
    segments = tokenize(text)
    clm = next((s for s in segments if s[0] == SEG_CLM), None)
    if clm is None:
        raise X12FormatError("837 has no CLM segment")
    insured = next((s for s in segments if s[0] == SEG_NM1 and s[1] == ENTITY_INSURED), None)
    if insured is None:
        raise X12FormatError("837 has no insured (NM1*IL) segment")
    prior_auth = next(
        (s[2] for s in segments if s[0] == SEG_REF and s[1] == PRIOR_AUTH_QUALIFIER and s[2]),
        None,
    )
    lines: list[ServiceRequest] = []
    for position, segment in enumerate(segments):
        if segment[0] != SEG_SV1:
            continue
        # The DTP after this SV1 (before the next LX) is the line's service date.
        date_segment = next(s for s in segments[position + 1 :] if s[0] == SEG_DTP)
        lines.append(
            ServiceRequest(
                procedure_code=segment[1].split(SUB_ELEMENT)[1],  # SV101-2
                charge=Decimal(segment[2]),  # SV102
                service_date=date_segment[3],  # DTP03
            )
        )
    if not lines:
        raise X12FormatError("837 has no SV1 service lines")
    names = insured + [""] * 10
    return ClaimRequest(
        patient_control_number=clm[1],  # CLM01
        total_charge=Decimal(clm[2]),  # CLM02
        family_name=names[3],
        given_name=names[4],
        middle_initial=names[5],  # NM103-105
        member_id=names[9],  # NM109
        prior_auth_number=prior_auth,
        service_lines=lines,
    )


def adjudicate_line(
    line: ServiceRequest, has_prior_auth: bool, claims: dict[str, Any]
) -> LineAdjudication:
    """Apply the three rules to one service line.

    Example (no prior auth): the 72148 MRI billed 1000 -> paid 0, [CO-197 1000].
    Example (99213 billed 200): allowed 150, coinsurance 30 -> paid 120,
    [CO-45 50, PR-2 30]. Always: charge = paid + all adjustments.
    """
    codes = claims["payer_codes"]
    policy = claims["payer_policy"]
    contract_rates = {p["code"]: Decimal(p["contract_rate"]) for p in claims["procedures"]}

    if line.procedure_code not in contract_rates:  # not in the contract: never guess a price
        return _denied(line, codes["group_contractual"], codes["reason_non_covered"])
    needs_auth = line.procedure_code in policy["prior_auth_required_for"]
    if needs_auth and not has_prior_auth:  # rule 3
        return _denied(line, codes["group_contractual"], codes["reason_no_precertification"])

    allowed = min(line.charge, contract_rates[line.procedure_code])  # rule 1
    write_off = line.charge - allowed
    coinsurance = (allowed * Decimal(policy["coinsurance_rate"])).quantize(CENTS, ROUND_HALF_UP)
    paid = allowed - coinsurance  # rule 2: payer pays the allowed amount minus the patient share
    adjustments = []
    if write_off > 0:
        adjustments.append(
            Adjustment(codes["group_contractual"], codes["reason_fee_schedule"], write_off)
        )
    if coinsurance > 0:
        adjustments.append(
            Adjustment(codes["group_patient"], codes["reason_coinsurance"], coinsurance)
        )
    return LineAdjudication(line.procedure_code, line.service_date, line.charge, paid, adjustments)


def _denied(line: ServiceRequest, group: str, reason: str) -> LineAdjudication:
    """A denied line pays nothing; the whole charge is one adjustment."""
    return LineAdjudication(
        line.procedure_code,
        line.service_date,
        line.charge,
        Decimal("0"),
        [Adjustment(group, reason, line.charge)],
    )


def adjudicate(request: ClaimRequest, claims: dict[str, Any]) -> ClaimAdjudication:
    """Decide every line of a claim."""
    has_prior_auth = request.prior_auth_number is not None
    return ClaimAdjudication(
        request=request,
        payer_claim_number=PAYER_CLAIM_PREFIX + request.patient_control_number,
        lines=[adjudicate_line(line, has_prior_auth, claims) for line in request.service_lines],
    )
