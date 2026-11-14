"""Tests for the 835 parser, the reconciler and the EOB mapping."""

from pathlib import Path

from interoperability_pipeline.x12.eob_mapper import eob_from_remittance
from interoperability_pipeline.x12.parser_835 import parse_835, reconcile

SAMPLES = Path(__file__).parent.parent / "samples" / "x12"


def load(name: str) -> str:
    return (SAMPLES / name).read_text()


def test_both_scenarios_balance() -> None:
    for name in ("jane_remit_835_prior_auth.x12", "jane_remit_835_no_auth.x12"):
        assert reconcile(parse_835(load(name))) == [], name


def test_parser_reads_amounts_and_adjustments() -> None:
    claim = parse_835(load("jane_remit_835_no_auth.x12")).claims[0]
    assert (claim.charge, claim.paid, claim.patient_responsibility) == (1200, 120, 30)
    mri = claim.lines[1]
    assert mri.procedure_code == "72148" and mri.paid == 0
    assert [(a.group, a.reason, a.amount) for a in mri.adjustments] == [("CO", "197", 1000)]


def test_a_broken_line_is_flagged() -> None:
    tampered = load("jane_remit_835_no_auth.x12").replace("CAS*CO*45*50~", "CAS*CO*45*70~")
    problems = reconcile(parse_835(tampered))
    assert any("line 99213: billed 200" in problem for problem in problems)
    assert any("CLP03" in problem for problem in problems)  # the claim total no longer adds up


def test_a_wrong_payment_total_is_flagged() -> None:
    tampered = load("jane_remit_835_no_auth.x12").replace("BPR*I*120*", "BPR*I*999*")
    assert any("BPR02" in problem for problem in reconcile(parse_835(tampered)))


def test_every_835_amount_lands_in_the_eob() -> None:
    remittance = parse_835(load("jane_remit_835_prior_auth.x12"))
    eob = eob_from_remittance(
        remittance, remittance.claims[0], "1000", "9", "sys", "2026-11-16T11:00:00-05:00"
    )
    assert eob["resourceType"] == "ExplanationOfBenefit"
    assert {t["category"]["coding"][0]["code"]: t["amount"]["value"] for t in eob["total"]} == {
        "submitted": 1200,
        "benefit": 760,
    }
    assert eob["payment"]["amount"]["value"] == 760  # BPR02
    assert eob["extension"][0]["valueMoney"]["value"] == 190  # CLP05
    mri = eob["item"][1]
    codes = [
        (
            a["category"]["coding"][0]["code"],
            a.get("reason", {}).get("coding", [{}])[0].get("code"),
            a["amount"]["value"],
        )
        for a in mri["adjudication"]
    ]
    assert ("submitted", None, 1000) in codes and ("benefit", None, 640) in codes
    assert ("CO", "45", 200) in codes and ("PR", "2", 160) in codes
