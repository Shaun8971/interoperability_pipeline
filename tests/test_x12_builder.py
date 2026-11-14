"""Tests for the 837P builder (pure functions: no FHIR server needed)."""

from datetime import datetime
from decimal import Decimal

import pytest

from interoperability_pipeline.common.errors import X12FormatError
from interoperability_pipeline.common.settings import CLAIMS_SETTINGS_PATH, load_settings
from interoperability_pipeline.x12.builder_837p import build_837p, format_amount
from interoperability_pipeline.x12.claim_data import ClaimData, ServiceLine
from interoperability_pipeline.x12.tokenizer import ISA_LENGTH, tokenize

NOW = datetime(2026, 11, 16, 11, 0)
CONFIG = load_settings(CLAIMS_SETTINGS_PATH)


def make_claim(prior_auth: str | None = None, family_name: str = "DOE") -> ClaimData:
    return ClaimData(
        patient_control_number="V0001",
        family_name=family_name,
        given_name="JANE",
        middle_initial="Q",
        birth_date="19800315",
        gender="F",
        street="123 MAIN ST",
        city="ITHACA",
        state="NY",
        postal_code="14850",
        diagnosis_codes=["I10"],
        service_lines=[
            ServiceLine("99213", Decimal("200.00"), "20261114"),
            ServiceLine("72148", Decimal("1000.00"), "20261114"),
        ],
        prior_auth_number=prior_auth,
    )


def segments_of(text: str) -> dict[str, list[list[str]]]:
    grouped: dict[str, list[list[str]]] = {}
    for segment in tokenize(text):
        grouped.setdefault(segment[0], []).append(segment)
    return grouped


def test_amounts_have_no_trailing_zeros() -> None:
    assert format_amount(Decimal("1200.00")) == "1200"
    assert format_amount(Decimal("12.50")) == "12.5"


def test_envelopes_open_and_close_in_order() -> None:
    ids = [segment[0] for segment in tokenize(build_837p(make_claim(), CONFIG, 1, NOW))]
    assert ids[:3] == ["ISA", "GS", "ST"]
    assert ids[-3:] == ["SE", "GE", "IEA"]


def test_isa_is_106_characters() -> None:
    text = build_837p(make_claim(), CONFIG, 1, NOW)
    assert text.index("~") + 1 == ISA_LENGTH


def test_se_counts_segments_from_st_through_se() -> None:
    segments = tokenize(build_837p(make_claim(), CONFIG, 1, NOW))
    start = next(i for i, segment in enumerate(segments) if segment[0] == "ST")
    end = next(i for i, segment in enumerate(segments) if segment[0] == "SE")
    assert int(segments[end][1]) == end - start + 1  # SE01
    assert segments[end][2] == segments[start][2]  # SE02 matches ST02


def test_control_numbers_match_between_header_and_trailer() -> None:
    grouped = segments_of(build_837p(make_claim(), CONFIG, 7, NOW))
    assert grouped["ISA"][0][13] == grouped["IEA"][0][2] == "000000007"  # ISA13 = IEA02
    assert grouped["GS"][0][6] == grouped["GE"][0][2]  # GS06 = GE02


def test_claim_total_equals_sum_of_service_lines() -> None:
    grouped = segments_of(build_837p(make_claim(), CONFIG, 1, NOW))
    assert grouped["CLM"][0][2] == "1200"  # CLM02
    assert [segment[2] for segment in grouped["SV1"]] == ["200", "1000"]  # SV102


def test_diagnosis_and_procedure_codes() -> None:
    grouped = segments_of(build_837p(make_claim(), CONFIG, 1, NOW))
    assert grouped["HI"][0][1] == "ABK:I10"
    assert [segment[1] for segment in grouped["SV1"]] == ["HC:99213", "HC:72148"]


def test_prior_authorization_only_appears_when_given() -> None:
    without = segments_of(build_837p(make_claim(), CONFIG, 1, NOW))
    with_auth = segments_of(build_837p(make_claim("PA12345"), CONFIG, 1, NOW))
    assert ["REF", "G1", "PA12345"] in with_auth["REF"]
    assert all(segment[1] != "G1" for segment in without["REF"])


def test_value_containing_a_delimiter_is_refused() -> None:
    with pytest.raises(X12FormatError):
        build_837p(make_claim(family_name="DO*E"), CONFIG, 1, NOW)
