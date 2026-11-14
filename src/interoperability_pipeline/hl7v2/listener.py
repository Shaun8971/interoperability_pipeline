"""HL7 v2 MLLP listener: receives, validates, stores in FHIR, then ACKs.

What it does: runs a TCP server; for each MLLP frame it unframes, parses, validates,
maps to FHIR, writes to the FHIR server, and only then replies with an ACK.
Where it sits in the pipeline:
    simulator --> **listener** --> parser --> validation --> mappers --> FHIR --> ACK
Standard and spec: HL7 MLLP (see mllp.py); ACK rules in ack.py; checks in validation.py;
FHIR writes in fhir/client.py.
Inputs / outputs: TCP on [mllp] host/port in config/settings.toml -> FHIR writes + ACK + logs.
How to extend: add a mapper call inside ``store_message`` for each new resource type.

ACK policy (why the order matters): we ACK *after* the FHIR write. Replying AA first
and then failing to store would lose data silently.
  AA = stored.   AE = the message itself is wrong (fix, then resend).
  AR = our side failed (FHIR down); the same message can be resent later.

Run:  python -u -m interoperability_pipeline.hl7v2.listener
"""

import logging
import socket
from datetime import datetime
from typing import Any

from interoperability_pipeline.common.errors import FhirError, MappingError, MllpFramingError
from interoperability_pipeline.common.settings import load_settings
from interoperability_pipeline.fhir.client import FhirClient, FhirWriteResult
from interoperability_pipeline.hl7v2.ack import ACK_ERROR, ACK_REJECT, build_ack
from interoperability_pipeline.hl7v2.mappers import (
    SEGMENT_PV1,
    SourceConfig,
    encounter_from_hl7,
    encounter_identifier,
    identifier_search_value,
    observation_from_hl7,
    patient_from_hl7,
    patient_identifier,
    source_config_from_settings,
)
from interoperability_pipeline.hl7v2.mllp import MllpFrameReader, frame, unframe
from interoperability_pipeline.hl7v2.parser import (
    find_segment,
    find_segments,
    get_field,
    split_message,
)
from interoperability_pipeline.hl7v2.validation import validate_message

logger = logging.getLogger("hl7_listener")

# Used in log lines when the message has no usable MSH-10.
UNKNOWN_CONTROL_ID = "<none>"
MESSAGE_CODE_ADT = "ADT"  # MSH-9.1: admit/discharge/transfer events change the visit


def store_message(
    segments: list[list[str]], fhir_client: FhirClient, config: SourceConfig
) -> list[FhirWriteResult]:
    """Map a validated message to FHIR resources and write them (idempotently).

    Returns what was written. Raises MappingError (bad content) or FhirError (server).
    """
    identifier = patient_identifier(segments, config)
    patient = patient_from_hl7(segments, config)
    # Conditional update keyed on the MRN: a resend updates Jane, never duplicates her.
    patient_result = fhir_client.conditional_update(
        "Patient",
        {"identifier": identifier_search_value(identifier["system"], identifier["value"])},
        patient,
    )
    results = [patient_result]
    encounter_id: str | None = None
    # Encounter second, and only for ADT messages: ADT events say what happened to the visit
    # (A01 started, A03 finished). A lab result says nothing about the visit's state, so it
    # must never overwrite it; it just looks the visit up to link to it.
    if find_segment(segments, SEGMENT_PV1) is not None:
        visit = encounter_identifier(segments, config)
        visit_search = {"identifier": identifier_search_value(visit["system"], visit["value"])}
        if _message_code(segments) == MESSAGE_CODE_ADT:
            encounter = encounter_from_hl7(segments, config, patient_result.resource_id)
            encounter_result = fhir_client.conditional_update("Encounter", visit_search, encounter)
            results.append(encounter_result)
            encounter_id = encounter_result.resource_id
        else:
            encounter_id = fhir_client.find_id("Encounter", visit_search)
            if encounter_id is None:
                logger.warning(
                    "visit %s not found in FHIR; storing result without an encounter link",
                    visit["value"],
                )
    # Observations last: each points at both the Patient and (if present) the Encounter.
    for obx in find_segments(segments, "OBX"):
        observation = observation_from_hl7(
            segments, obx, config, patient_result.resource_id, encounter_id
        )
        result_identifier = observation["identifier"][0]
        results.append(
            fhir_client.conditional_update(
                "Observation",
                {
                    "identifier": identifier_search_value(
                        result_identifier["system"], result_identifier["value"]
                    )
                },
                observation,
            )
        )
    return results


def _message_code(segments: list[list[str]]) -> str:
    """MSH-9.1, e.g. 'ADT' or 'ORU'."""
    msh = find_segment(segments, "MSH")
    return get_field(msh, 9).split("^")[0] if msh else ""


def process_message(
    message_text: str, fhir_client: FhirClient, config: SourceConfig
) -> tuple[str, str]:
    """Run one message through the pipeline; return (ACK text, control ID).

    Never raises: any failure becomes an AE or AR so the sender is told, instead of
    the connection dying silently.
    """
    segments: list[list[str]] = []
    problems: list[str] = []
    code: str | None = None
    written: list[FhirWriteResult] = []
    try:
        segments = split_message(message_text)
        problems = validate_message(segments)
        if not problems:
            written = store_message(segments, fhir_client, config)
    except MappingError as error:
        problems, code = [str(error)], ACK_ERROR
    except FhirError as error:
        problems = [str(error)]
        code = ACK_REJECT if error.retryable else ACK_ERROR  # retryable = not the message's fault
    except Exception as error:  # noqa: BLE001 - last line of defence, logged below
        logger.exception("unexpected failure")
        problems, code = [f"internal error: {type(error).__name__}"], ACK_REJECT

    msh = find_segment(segments, "MSH")
    control_id = (get_field(msh, 10) if msh else "") or UNKNOWN_CONTROL_ID  # MSH-10
    ack_text = build_ack(segments, problems, now=datetime.now(), code=code)
    if problems:
        logger.warning(
            "control_id=%s NOT STORED (%s): %s", control_id, code or ACK_ERROR, "; ".join(problems)
        )
    else:
        for result in written:
            logger.info(
                "control_id=%s accepted (AA) -> %s/%s %s",
                control_id,
                result.resource_type,
                result.resource_id,
                "created" if result.created else "updated",
            )
    return ack_text, control_id


def handle_connection(
    connection: socket.socket, fhir_client: FhirClient, config: SourceConfig
) -> None:
    """Read frames from one connection, answering each with an ACK, until the sender hangs up."""
    reader = MllpFrameReader(connection)
    while True:
        try:
            frame_bytes = reader.read_frame()
            if frame_bytes is None:
                return
            print("RAW BYTES:", frame_bytes)
            message_text = unframe(frame_bytes)
        except MllpFramingError as error:
            logger.error("framing error: %s", error)  # never fail silently
            return
        ack_text, _control_id = process_message(message_text, fhir_client, config)
        connection.sendall(frame(ack_text))  # the reply travels in an MLLP frame too


def serve() -> None:
    """Listen forever on the configured host and port."""
    settings: dict[str, Any] = load_settings()
    mllp = settings["mllp"]
    fhir_client = FhirClient(settings["fhir"]["base_url"], settings["fhir"]["timeout_seconds"])
    config = source_config_from_settings(settings)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # allow quick restarts
        server.bind((mllp["host"], mllp["port"]))
        server.listen()
        logger.info("listening on %s:%s", mllp["host"], mllp["port"])
        while True:
            connection, address = server.accept()
            logger.info("connection from %s", address)
            with connection:
                handle_connection(connection, fhir_client, config)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    serve()
