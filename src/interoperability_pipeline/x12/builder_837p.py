"""Build an X12 837P (professional claim) from a ClaimData object. Pure functions, no network.

What it does: produces the claim text, one small function per X12 loop, plus the ISA/GS/ST
envelopes around it.
Where it sits in the pipeline: claim_data --> **builder_837p** --> payer simulator.
Standard and spec: ASC X12 837 Professional, version 005010X222A1. Free segment reference:
  https://www.stedi.com/edi/x12-005010/837  (ISA: .../segment/ISA, CLM: .../segment/CLM,
  HI: .../segment/HI, SV1: .../segment/SV1, NM1: .../segment/NM1)
Inputs / outputs: ClaimData + claims config + control numbers + time -> X12 text.
How to extend: add a loop by writing one more ``_loop_*`` function and adding it to
``build_837p``. SE01 (the segment count) is computed, so it stays right automatically.
Run:  python -m interoperability_pipeline.x12.builder_837p [--prior-auth NUMBER] [--output FILE]
"""

import argparse
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from interoperability_pipeline.common.errors import X12FormatError
from interoperability_pipeline.common.settings import (
    CLAIMS_SETTINGS_PATH,
    PROJECT_ROOT,
    load_settings,
)
from interoperability_pipeline.fhir.client import FhirClient
from interoperability_pipeline.x12.claim_data import ClaimData, load_claim_data

# --- Delimiters we write (declared in the ISA, as X12 requires) ----------------------------
ELEMENT = "*"  # separates elements
SUB_ELEMENT = ":"  # separates parts of one element (ISA16)
SEGMENT_END = "~"  # ends a segment
REPETITION = "^"  # ISA11 repetition separator (declared but unused here)

# --- Envelope and version constants ----------------------------------------------------------
IMPLEMENTATION_REFERENCE = "005010X222A1"  # GS08 / ST03: the 837P implementation guide
INTERCHANGE_VERSION = "00501"  # ISA12: X12 version 5010
FUNCTIONAL_ID_CODE = "HC"  # GS01: health care claim
TRANSACTION_SET_ID = "837"  # ST01
ISA_ID_QUALIFIER = "ZZ"  # ISA05/07: "mutually defined" ID qualifier
ISA_ID_WIDTH = 15  # ISA06/08 are fixed 15 characters
# Entity identifier codes (NM101) and ID qualifiers (NM108)
ENTITY_SUBMITTER, ENTITY_RECEIVER, ENTITY_BILLING_PROVIDER = "41", "40", "85"
ENTITY_INSURED, ENTITY_PAYER = "IL", "PR"
QUALIFIER_ETIN, QUALIFIER_NPI, QUALIFIER_MEMBER_ID, QUALIFIER_PAYER_ID = "46", "XX", "MI", "PI"
# Claim-level codes
PLACE_OF_SERVICE_OFFICE = "11"  # CLM05-1: place of service 11 = office
CLAIM_FREQUENCY_ORIGINAL = "1"  # CLM05-3: 1 = original claim
FACILITY_CODE_QUALIFIER = "B"  # CLM05-2: B = place of service code for professional claims
DIAGNOSIS_QUALIFIER_PRINCIPAL = "ABK"  # HI: ICD-10-CM principal diagnosis
DIAGNOSIS_QUALIFIER_OTHER = "ABF"  # HI: ICD-10-CM additional diagnosis
PRIOR_AUTH_QUALIFIER = "G1"  # REF01: prior authorization number
SERVICE_DATE_QUALIFIER = "472"  # DTP01: service date
DATE_FORMAT_QUALIFIER = "D8"  # DTP02: CCYYMMDD
PRODUCT_QUALIFIER_HCPCS = "HC"  # SV101-1: HCPCS/CPT procedure code
UNIT_BASIS = "UN"  # SV103: units
# Where the claim file goes by default (committed as a sample for Checkpoint 2.1).
DEFAULT_OUTPUT = PROJECT_ROOT / "samples" / "x12" / "jane_claim_837p_no_auth.x12"


def format_amount(amount: Decimal) -> str:
    """Format a dollar amount the way X12 expects: no trailing zeros.

    Example:
        >>> format_amount(Decimal("1200.00")), format_amount(Decimal("12.50"))
        ('1200', '12.5')
    """
    text = format(amount.normalize(), "f")
    return text


def make_segment(*elements: str) -> str:
    """Join elements into one segment (without terminator); refuse values that would corrupt it."""
    for element in elements:
        if any(delimiter in element for delimiter in (ELEMENT, SEGMENT_END)):
            raise X12FormatError(f"value {element!r} contains an X12 delimiter character")
    return ELEMENT.join(elements)


# --- Envelope ------------------------------------------------------------------------------------
def build_isa(interchange: dict[str, str], control_number: int, now: datetime) -> str:
    """ISA: the interchange header. Every element is fixed width, so the result is 106 characters.

    Example: ISA*00*          *00*          *ZZ*MERCYGENERAL   *ZZ*SYNTHPLAN      *261116*...~
    """
    isa = ELEMENT.join(
        [
            "ISA",
            "00",
            " " * 10,  # ISA01/02: authorization info qualifier + value (none)
            "00",
            " " * 10,  # ISA03/04: security info qualifier + value (none)
            ISA_ID_QUALIFIER,
            interchange["sender_id"].ljust(ISA_ID_WIDTH),  # ISA05/06 sender
            ISA_ID_QUALIFIER,
            interchange["receiver_id"].ljust(ISA_ID_WIDTH),  # ISA07/08 receiver
            now.strftime("%y%m%d"),  # ISA09: date YYMMDD
            now.strftime("%H%M"),  # ISA10: time HHMM
            REPETITION,  # ISA11: repetition separator
            INTERCHANGE_VERSION,  # ISA12
            f"{control_number:09d}",  # ISA13: interchange control number (must match IEA02)
            "0",  # ISA14: no interchange acknowledgment requested
            interchange["usage_indicator"],  # ISA15: T = test
            SUB_ELEMENT,  # ISA16: sub-element separator
        ]
    )
    return isa + SEGMENT_END


# --- Loops (each returns a list of segment strings) --------------------------------------------
def _loop_header(
    claim: ClaimData, config: dict[str, Any], now: datetime, control: int
) -> list[str]:
    """BHT + loops 1000A (submitter) and 1000B (receiver)."""
    interchange, payer, provider = (
        config["interchange"],
        config["payer"],
        config["billing_provider"],
    )
    return [
        # BHT: beginning of the claim; BHT03 = originator's reference, BHT06 CH = chargeable claim
        make_segment(
            "BHT",
            "0019",
            "00",
            f"{control:010d}",
            now.strftime("%Y%m%d"),
            now.strftime("%H%M"),
            "CH",
        ),
        # 1000A submitter: NM109 is the submitter's ID (ETIN)
        make_segment(
            "NM1",
            ENTITY_SUBMITTER,
            "2",
            provider["name"],
            "",
            "",
            "",
            "",
            QUALIFIER_ETIN,
            interchange["sender_id"],
        ),
        make_segment("PER", "IC", interchange["contact_name"], "TE", interchange["contact_phone"]),
        # 1000B receiver: the payer
        make_segment(
            "NM1",
            ENTITY_RECEIVER,
            "2",
            payer["name"],
            "",
            "",
            "",
            "",
            QUALIFIER_ETIN,
            interchange["receiver_id"],
        ),
    ]


def _loop_billing_provider(config: dict[str, Any]) -> list[str]:
    """Loop 2000A/2010AA: the billing provider (hierarchical level 1)."""
    provider = config["billing_provider"]
    return [
        make_segment(
            "HL", "1", "", "20", "1"
        ),  # HL03 20 = information source; HL04 1 = has children
        make_segment(
            "NM1",
            ENTITY_BILLING_PROVIDER,
            "2",
            provider["name"],
            "",
            "",
            "",
            "",
            QUALIFIER_NPI,
            provider["npi"],
        ),
        make_segment("N3", provider["street"]),  # address line
        make_segment("N4", provider["city"], provider["state"], provider["postal_code"]),
        make_segment("REF", "EI", provider["tax_id"]),  # REF01 EI = employer identification number
    ]


def _loop_subscriber(claim: ClaimData, config: dict[str, Any]) -> list[str]:
    """Loop 2000B/2010BA/2010BB: the insured (Jane, the subscriber) and the payer."""
    subscriber, payer = config["subscriber"], config["payer"]
    return [
        make_segment("HL", "2", "1", "22", "0"),  # HL03 22 = subscriber; HL04 0 = no dependents
        # SBR: P = payer is primary; 18 = patient is the subscriber (self); CI = commercial
        make_segment("SBR", "P", "18", subscriber["group_number"], "", "", "", "", "", "CI"),
        make_segment(
            "NM1",
            ENTITY_INSURED,
            "1",
            claim.family_name,
            claim.given_name,
            claim.middle_initial,
            "",
            "",
            QUALIFIER_MEMBER_ID,
            subscriber["member_id"],
        ),
        make_segment("N3", claim.street),
        make_segment("N4", claim.city, claim.state, claim.postal_code),
        make_segment(
            "DMG", DATE_FORMAT_QUALIFIER, claim.birth_date, claim.gender
        ),  # birth date + sex
        make_segment(
            "NM1",
            ENTITY_PAYER,
            "2",
            payer["name"],
            "",
            "",
            "",
            "",
            QUALIFIER_PAYER_ID,
            payer["payer_id"],
        ),
    ]


def _loop_claim(claim: ClaimData) -> list[str]:
    """Loop 2300: the claim itself (CLM, optional prior-auth REF, HI diagnoses)."""
    segments = [
        make_segment(
            "CLM",
            claim.patient_control_number,  # CLM01: our claim reference
            format_amount(claim.total_charge),  # CLM02: total claim charge
            "",
            "",  # CLM03/04 unused
            SUB_ELEMENT.join(
                [PLACE_OF_SERVICE_OFFICE, FACILITY_CODE_QUALIFIER, CLAIM_FREQUENCY_ORIGINAL]
            ),  # CLM05
            "Y",
            "A",
            "Y",
            "Y",  # CLM06-09: provider signature, assignment of benefits, release of info
        )
    ]
    if claim.prior_auth_number:  # REF*G1: only present when the claim carries a prior authorization
        segments.append(make_segment("REF", PRIOR_AUTH_QUALIFIER, claim.prior_auth_number))
    diagnosis_elements = []
    for position, code in enumerate(claim.diagnosis_codes):
        qualifier = DIAGNOSIS_QUALIFIER_PRINCIPAL if position == 0 else DIAGNOSIS_QUALIFIER_OTHER
        diagnosis_elements.append(
            f"{qualifier}{SUB_ELEMENT}{code.replace('.', '')}"
        )  # ICD-10 has no dot in X12
    segments.append(make_segment("HI", *diagnosis_elements))
    return segments


def _loop_service_lines(claim: ClaimData) -> list[str]:
    """Loop 2400: one LX + SV1 + DTP group per service."""
    segments: list[str] = []
    for line_number, line in enumerate(claim.service_lines, start=1):
        segments += [
            make_segment("LX", str(line_number)),  # LX01: line counter
            make_segment(
                "SV1",
                f"{PRODUCT_QUALIFIER_HCPCS}{SUB_ELEMENT}{line.procedure_code}",  # SV101: CPT code
                format_amount(line.charge),  # SV102: line charge
                UNIT_BASIS,
                "1",  # SV103/04: one unit
                "",
                "",
                "1",  # SV107: diagnosis pointer -> first HI diagnosis
            ),
            make_segment("DTP", SERVICE_DATE_QUALIFIER, DATE_FORMAT_QUALIFIER, line.service_date),
        ]
    return segments


def build_837p(
    claim: ClaimData,
    config: dict[str, Any],
    interchange_control_number: int,
    now: datetime,
) -> str:
    """Build the complete 837P interchange (one segment per line, each ending in ``~``).

    Example (shape only):
        ISA...~ GS*HC...~ ST*837*0001*005010X222A1~ BHT...~ ... CLM*V0001*1200***11:B:1*Y*A*Y*Y~
        HI*ABK:I10~ LX*1~ SV1*HC:99213*200*UN*1***1~ ... SE*<count>*0001~ GE*1*1~ IEA*1*<n>~
    """
    interchange = config["interchange"]
    transaction_set = (
        [make_segment("ST", TRANSACTION_SET_ID, "0001", IMPLEMENTATION_REFERENCE)]
        + _loop_header(claim, config, now, interchange_control_number)
        + _loop_billing_provider(config)
        + _loop_subscriber(claim, config)
        + _loop_claim(claim)
        + _loop_service_lines(claim)
    )
    # SE01 counts every segment from ST through SE inclusive, so add one for SE itself.
    transaction_set.append(make_segment("SE", str(len(transaction_set) + 1), "0001"))
    group_header = make_segment(
        "GS",
        FUNCTIONAL_ID_CODE,
        interchange["sender_id"],
        interchange["receiver_id"],
        now.strftime("%Y%m%d"),
        now.strftime("%H%M"),
        "1",
        "X",
        IMPLEMENTATION_REFERENCE,
    )  # GS06 group control number 1 and GS07 X = X12 responsible agency
    trailer = [
        make_segment("GE", "1", "1"),  # GE01: number of transaction sets; GE02 matches GS06
        make_segment(
            "IEA", "1", f"{interchange_control_number:09d}"
        ),  # IEA01 groups; IEA02 matches ISA13
    ]
    lines = [build_isa(interchange, interchange_control_number, now), group_header + SEGMENT_END]
    lines += [segment + SEGMENT_END for segment in transaction_set + trailer]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Jane's 837P claim from FHIR.")
    parser.add_argument("--prior-auth", default=None, help="prior authorization number to include")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    arguments = parser.parse_args()
    settings = load_settings()
    claims = load_settings(CLAIMS_SETTINGS_PATH)
    client = FhirClient(settings["fhir"]["base_url"], settings["fhir"]["timeout_seconds"])
    claim = load_claim_data(client, settings, claims, arguments.prior_auth)
    text = build_837p(claim, claims, interchange_control_number=1, now=datetime.now())
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(text)
    print(f"wrote {arguments.output}")


if __name__ == "__main__":
    main()
