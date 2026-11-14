"""Tests for the payer simulator rules and the 835 it writes."""

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from interoperability_pipeline.common.settings import CLAIMS_SETTINGS_PATH, load_settings
from interoperability_pipeline.x12.adjudication import adjudicate, read_claim_837
from interoperability_pipeline.x12.builder_835 import build_835
from interoperability_pipeline.x12.tokenizer import tokenize

SAMPLES = Path(__file__).parent.parent / "samples" / "x12"
CLAIMS = load_settings(CLAIMS_SETTINGS_PATH)
NOW = datetime(2026, 11, 16, 11, 0)


def decide(file_name: str):  # type: ignore[no-untyped-def]
    return adjudicate(read_claim_837((SAMPLES / file_name).read_text()), CLAIMS)


def summary(decision):  # type: ignore[no-untyped-def]
    return {
        line.procedure_code: (
            line.paid,
            [(a.group, a.reason, a.amount) for a in line.adjustments],
        )
        for line in decision.lines
    }


def test_with_prior_auth_both_services_are_paid() -> None:
    result = summary(decide("jane_claim_837p_prior_auth.x12"))
    assert result["99213"] == (
        Decimal("120"),
        [("CO", "45", Decimal("50")), ("PR", "2", Decimal("30.00"))],
    )
    assert result["72148"] == (
        Decimal("640"),
        [("CO", "45", Decimal("200")), ("PR", "2", Decimal("160.00"))],
    )


def test_without_prior_auth_the_mri_is_denied_as_provider_loss() -> None:
    result = summary(decide("jane_claim_837p_no_auth.x12"))
    assert result["72148"] == (Decimal("0"), [("CO", "197", Decimal("1000"))])
    assert result["99213"][0] == Decimal("120")  # the visit is unaffected


def test_every_line_balances_billed_equals_paid_plus_adjustments() -> None:
    for name in ("jane_claim_837p_prior_auth.x12", "jane_claim_837p_no_auth.x12"):
        for line in decide(name).lines:
            assert line.charge == line.paid + sum(a.amount for a in line.adjustments)


def test_patient_responsibility_totals() -> None:
    assert decide("jane_claim_837p_prior_auth.x12").total_for_group("PR") == Decimal("190")
    assert decide("jane_claim_837p_no_auth.x12").total_for_group("PR") == Decimal("30")


def test_835_envelope_and_totals() -> None:
    text = build_835(decide("jane_claim_837p_no_auth.x12"), CLAIMS, 1, NOW)
    segments = tokenize(text)
    ids = [s[0] for s in segments]
    assert ids[:3] == ["ISA", "GS", "ST"] and ids[-3:] == ["SE", "GE", "IEA"]
    bpr = next(s for s in segments if s[0] == "BPR")
    clp = next(s for s in segments if s[0] == "CLP")
    assert bpr[2] == "120" and clp[3:6] == ["1200", "120", "30"]  # BPR02; CLP03-05
    start = ids.index("ST")
    end = ids.index("SE")
    assert int(segments[end][1]) == end - start + 1  # SE01
