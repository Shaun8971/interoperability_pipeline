"""Payer simulator: reads an 837P claim file and writes the 835 remittance.

What it does: applies the payer's three rules (pay contract rate, apply coinsurance, deny
the MRI without prior authorization) and writes the result as an 835.
Where it sits in the pipeline: claim builder --837P--> **payer** --835--> parser --> FHIR EOB.
Standard and spec: see src/interoperability_pipeline/x12/adjudication.py and builder_835.py.
Inputs / outputs: path to an 837P file -> an 835 file (default: next to it, "remit" name).
How to extend: change the rules in x12/adjudication.py; this script only moves files.
Run:  python simulators/payer.py samples/x12/jane_claim_837p_no_auth.x12 --output OUT.x12
"""

import argparse
from datetime import datetime
from pathlib import Path

from interoperability_pipeline.common.settings import CLAIMS_SETTINGS_PATH, load_settings
from interoperability_pipeline.x12.adjudication import adjudicate, read_claim_837
from interoperability_pipeline.x12.builder_835 import build_835

REMITTANCE_CONTROL_NUMBER = 1  # ISA13/IEA02 of the 835 (one file at a time in this project)


def main() -> None:
    parser = argparse.ArgumentParser(description="Adjudicate an 837P and write the 835.")
    parser.add_argument("claim_file", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()

    claims = load_settings(CLAIMS_SETTINGS_PATH)
    decision = adjudicate(read_claim_837(arguments.claim_file.read_text()), claims)
    arguments.output.write_text(
        build_835(decision, claims, REMITTANCE_CONTROL_NUMBER, datetime.now())
    )
    prior_auth = decision.request.prior_auth_number or "none"
    print(f"claim {decision.request.patient_control_number}, prior auth: {prior_auth}")
    for line in decision.lines:
        adjustments = ", ".join(f"{a.group}-{a.reason} {a.amount}" for a in line.adjustments)
        summary = f"billed {line.charge}, paid {line.paid}, adjustments [{adjustments}]"
        print(f"  {line.procedure_code}: {summary}")
    print(f"wrote {arguments.output}")


if __name__ == "__main__":
    main()
