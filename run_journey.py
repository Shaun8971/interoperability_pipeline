"""Run Jane's whole journey in one command and print a trace by message control ID.

What it does: (1) starts the HL7 listener, (2) sends Hospital A's admit, lab result and
discharge for Jane, plus Hospital B's messages for a second patient, (3) feeds in some bad
inputs on purpose, (4) builds the 837P claim from FHIR, has the payer simulator answer with an
835, checks it balances and stores the ExplanationOfBenefit, (5) refuses an unbalanced 835,
(6) prints what happened to every message, and the volume report.
Where it sits in the pipeline: it drives the whole pipeline from the outside.
Standard and spec: see the individual modules (HL7 v2, FHIR R4, X12 837P/835).
Inputs / outputs: needs the HAPI server running (docker compose up -d); writes a fresh
event log to data/journey/ and prints the trace.
How to extend: add a step to ``main`` (and its message file under samples/).
Run:  python run_journey.py
"""

import shutil
import socket
import subprocess
import sys
import threading
from datetime import datetime

import requests

from interoperability_pipeline.common.errors import MappingError
from interoperability_pipeline.common.event_log import EVENTS_FILE, EventLog
from interoperability_pipeline.common.report import build_report
from interoperability_pipeline.common.settings import (
    CLAIMS_SETTINGS_PATH,
    PROJECT_ROOT,
    load_settings,
)
from interoperability_pipeline.fhir.client import FhirClient
from interoperability_pipeline.hl7v2.listener import serve
from interoperability_pipeline.hl7v2.mllp import MllpFrameReader, frame, unframe
from interoperability_pipeline.hl7v2.parser import find_segment, split_message
from interoperability_pipeline.mapping.sources import load_sources
from interoperability_pipeline.x12.builder_837p import build_837p
from interoperability_pipeline.x12.claim_data import load_claim_data
from interoperability_pipeline.x12.parser_835 import parse_835, reconcile
from interoperability_pipeline.x12.remittance_to_fhir import store_remittance

SAMPLES = PROJECT_ROOT / "samples"
JOURNEY_DATA = PROJECT_ROOT / "data" / "journey"  # a fresh log for each run
PRIOR_AUTH_NUMBER = "PA-2026-0042"  # the claim carries a prior authorization in this journey
ACK_TIMEOUT_SECONDS = 5

# What we send, in order: (file under samples/hl7, why we are sending it).
HL7_STEPS = [
    ("adt_a01.hl7", "Hospital A: admit Jane"),
    ("oru_r01.hl7", "Hospital A: potassium result"),
    ("adt_a03.hl7", "Hospital A: discharge Jane"),
    ("hospital_b_adt_a01.hl7", "Hospital B: admit a different patient (MRN in PID-2)"),
    ("hospital_b_oru_r01.hl7", "Hospital B: potassium with a local code (translated by config)"),
    ("bad_mrn_in_pid2.hl7", "BAD: Hospital A message with the MRN in PID-2"),
    ("bad_local_lab_code.hl7", "BAD: Hospital A local lab code 'K+' with no translation"),
    ("adt_a01.hl7", "BAD: the OLD admit arrives again, late (after the discharge)"),
]


def send_hl7(file_name: str, host: str, port: int) -> str:
    """Send one sample over MLLP and return the ACK's MSA segment as text."""
    text = (SAMPLES / "hl7" / file_name).read_text().strip().replace("\n", "\r")
    with socket.create_connection((host, port)) as connection:
        connection.settimeout(ACK_TIMEOUT_SECONDS)
        connection.sendall(frame(text))
        ack = MllpFrameReader(connection).read_frame()
    msa = find_segment(split_message(unframe(ack)), "MSA") if ack else None
    return "|".join(msa) if msa else "(no ACK)"


def run_script(*arguments: str) -> None:
    """Run one of our own scripts with the same Python, failing loudly if it fails."""
    subprocess.run([sys.executable, *arguments], check=True, cwd=PROJECT_ROOT, capture_output=True)


def main() -> None:
    settings = load_settings()
    claims = load_settings(CLAIMS_SETTINGS_PATH)
    fhir_url = settings["fhir"]["base_url"]
    try:
        requests.get(f"{fhir_url}/metadata", timeout=5).raise_for_status()
    except requests.RequestException as error:
        raise SystemExit(
            f"The FHIR server is not reachable at {fhir_url}. Run: docker compose up -d"
        ) from error
    client = FhirClient(fhir_url, settings["fhir"]["timeout_seconds"])
    shutil.rmtree(JOURNEY_DATA, ignore_errors=True)
    event_log = EventLog(JOURNEY_DATA)

    print("== 1. Starting the HL7 listener")
    ready = threading.Event()
    threading.Thread(target=serve, args=(event_log, ready), daemon=True).start()
    ready.wait(timeout=5)
    mllp = settings["mllp"]

    print("\n== 2. Sending HL7 messages (hospital -> listener -> FHIR)")
    for number, (file_name, why) in enumerate(HL7_STEPS, start=1):
        ack = send_hl7(file_name, mllp["host"], mllp["port"])
        print(f"  {number}. {why}\n     {file_name} -> ACK {ack}")

    print("\n== 3. Claim: FHIR -> 837P -> payer -> 835 -> EOB")
    run_script("simulators/seed_claim_data.py")  # diagnosis + procedures for Jane's visit
    claim = load_claim_data(client, settings, claims, PRIOR_AUTH_NUMBER)
    claim_text = build_837p(claim, claims, 1, datetime.now())
    work = JOURNEY_DATA
    work.mkdir(parents=True, exist_ok=True)
    claim_file, remit_file, bad_remit_file = (
        work / "claim.x12",
        work / "remit.x12",
        work / "bad_remit.x12",
    )
    claim_file.write_text(claim_text)
    print(f"  837P built: total charge {claim.total_charge}, prior auth {PRIOR_AUTH_NUMBER}")
    run_script("simulators/payer.py", str(claim_file), "--output", str(remit_file))
    remittance = parse_835(remit_file.read_text())
    paid = remittance.claims[0].paid
    owed = remittance.claims[0].patient_responsibility
    print(
        f"  835 received: payer paid {paid}, Jane owes {owed}; balance check: "
        f"{'OK' if not reconcile(remittance) else 'FAILED'}"
    )
    for line in store_remittance(remit_file.read_text(), client, settings):
        print(f"  stored {line}")

    print("\n== 4. A broken 835 (one adjustment changed)")
    bad_remit_file.write_text(remit_file.read_text().replace("CAS*CO*45*50~", "CAS*CO*45*70~"))
    try:
        store_remittance(bad_remit_file.read_text(), client, settings)
        print("  !! the broken 835 was stored (this would be bad)")
    except MappingError as error:
        print(f"  refused: {str(error)[:150]}")
        event_log.record(
            source="payer",
            message_type="835",
            control_id=bad_remit_file.name,
            outcome="rejected",
            detail=str(error),
        )
        event_log.enqueue(
            kind="rejected",
            source="payer",
            control_id=bad_remit_file.name,
            reasons=[str(error)],
            raw_message=bad_remit_file.read_text(),
        )

    print("\n== 5. Trace by message control ID (what happened to each message)")
    for event in event_log.read(EVENTS_FILE):
        detail = event["detail"] or ", ".join(event["resources"])
        print(
            f"  {event['control_id']:<10} {event['source']:<11} {event['message_type']:<9} "
            f"{event['outcome']:<14} {detail[:80]}"
        )

    print("\n== 6. Report")
    print(build_report(event_log, [source.name for source in load_sources().values()]))


if __name__ == "__main__":
    main()
