"""HL7 v2 MLLP listener: receives, validates, stores in FHIR, records, then ACKs.

What it does: runs a TCP server; for each MLLP frame it unframes, parses, picks the sending
hospital's config (MSH-4), validates, refuses stale events, maps to FHIR, writes, records
the outcome, and only then replies with an ACK.
Where it sits in the pipeline:
    simulator --> **listener** --> parser --> validation --> mappers --> FHIR --> ACK
Standard and spec: HL7 MLLP (see mllp.py); ACK rules in ack.py; checks in validation.py;
FHIR writes in fhir/client.py; per-hospital config in mapping/sources.py.
Inputs / outputs: TCP on [mllp] host/port -> FHIR writes + ACK + logs + event log + error queue.
How to extend: add a mapper call inside ``store_message`` for each new resource type.

ACK policy (why the order matters): we ACK *after* the FHIR write. Replying AA first
and then failing to store would lose data silently.
  AA = stored (or a stale older event we deliberately did not apply; the note says so).
  AE = the message itself is wrong (fix, then resend).  AR = our side failed; resend later.

Stale events: an old event arriving late (a network retry of the admit after the discharge)
must not overwrite newer information. Each Encounter remembers the time of the event that last
changed it (EVN-2); an older event is not applied, is ACKed AA with a note, and goes to the
error queue so a person can see it.

Run:  python -u -m interoperability_pipeline.hl7v2.listener
"""

import logging
import socket
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from interoperability_pipeline.common.errors import FhirError, MappingError, MllpFramingError
from interoperability_pipeline.common.event_log import (
    OUTCOME_ACCEPTED,
    OUTCOME_REJECTED,
    OUTCOME_RETRY,
    OUTCOME_STALE,
    OUTCOME_UNKNOWN_SOURCE,
    EventLog,
)
from interoperability_pipeline.common.settings import PROJECT_ROOT, load_settings
from interoperability_pipeline.fhir.client import FhirClient
from interoperability_pipeline.hl7v2.ack import ACK_ACCEPT, ACK_ERROR, ACK_REJECT, build_ack
from interoperability_pipeline.hl7v2.mappers import (
    LAST_EVENT_EXTENSION_URL,
    SEGMENT_PV1,
    SourceConfig,
    encounter_from_hl7,
    encounter_identifier,
    identifier_search_value,
    message_event_time,
    observation_from_hl7,
    patient_from_hl7,
    patient_identifier,
)
from interoperability_pipeline.hl7v2.mllp import MllpFrameReader, frame, unframe
from interoperability_pipeline.hl7v2.parser import (
    find_segment,
    find_segments,
    get_field,
    split_components,
    split_message,
)
from interoperability_pipeline.hl7v2.validation import validate_message
from interoperability_pipeline.mapping.sources import load_sources

logger = logging.getLogger("hl7_listener")

UNKNOWN_CONTROL_ID = "<none>"  # used in log lines when the message has no usable MSH-10
UNKNOWN_SOURCE_NAME = "unknown"  # used when no config file matches the sending facility
MESSAGE_CODE_ADT = "ADT"  # MSH-9.1: admit/discharge/transfer events change the visit
DEFAULT_PATIENT_ID_FIELD = 3  # used for validation when the source is unknown
STALE_NOTE = "stale event: a newer event for this visit is already stored; not applied"


@dataclass
class StoreResult:
    """What ``store_message`` did: the resources written, or why nothing was applied."""

    written: list[str] = field(default_factory=list)  # e.g. ["Patient/1000", "Encounter/1001"]
    stale: bool = False


def _message_code(segments: list[list[str]]) -> str:
    """MSH-9.1, e.g. 'ADT' or 'ORU'."""
    msh = find_segment(segments, "MSH")
    return get_field(msh, 9).split("^")[0] if msh else ""


def _message_type(segments: list[list[str]]) -> str:
    """MSH-9 as 'ADT^A01' (first two components), or '' if there is no MSH."""
    msh = find_segment(segments, "MSH")
    return "^".join(split_components(get_field(msh, 9))[:2]) if msh else ""


def _is_stale(fhir_client: FhirClient, visit_search: dict[str, str], incoming_time: str) -> bool:
    """True if the stored Encounter was last changed by a NEWER event than this message."""
    existing = fhir_client.search("Encounter", visit_search)
    if not existing:
        return False
    for extension in existing[0].get("extension", []):
        if extension.get("url") == LAST_EVENT_EXTENSION_URL:
            stored = datetime.fromisoformat(extension["valueDateTime"])
            return stored > datetime.fromisoformat(incoming_time)
    return False  # an Encounter written before we tracked event times: accept the update


def store_message(
    segments: list[list[str]], fhir_client: FhirClient, config: SourceConfig
) -> StoreResult:
    """Map a validated message to FHIR resources and write them (idempotently).

    Raises MappingError (bad content) or FhirError (server problem).
    """
    is_adt = _message_code(segments) == MESSAGE_CODE_ADT
    has_visit = find_segment(segments, SEGMENT_PV1) is not None
    visit_search: dict[str, str] = {}
    if has_visit:
        visit = encounter_identifier(segments, config)
        visit_search = {"identifier": identifier_search_value(visit["system"], visit["value"])}
    if (
        is_adt
        and has_visit
        and _is_stale(fhir_client, visit_search, message_event_time(segments, config))
    ):
        return StoreResult(stale=True)  # write nothing: this event is older than what we hold

    identifier = patient_identifier(segments, config)
    patient = patient_from_hl7(segments, config)
    # Conditional update keyed on the MRN: a resend updates Jane, never duplicates her.
    patient_result = fhir_client.conditional_update(
        "Patient",
        {"identifier": identifier_search_value(identifier["system"], identifier["value"])},
        patient,
    )
    written = [f"Patient/{patient_result.resource_id}"]
    encounter_id: str | None = None
    # Encounter second, and only for ADT messages: ADT events say what happened to the visit.
    # A lab result says nothing about the visit's state, so it only looks the visit up.
    if has_visit:
        if is_adt:
            encounter = encounter_from_hl7(segments, config, patient_result.resource_id)
            encounter_result = fhir_client.conditional_update("Encounter", visit_search, encounter)
            encounter_id = encounter_result.resource_id
            written.append(f"Encounter/{encounter_id}")
        else:
            encounter_id = fhir_client.find_id("Encounter", visit_search)
            if encounter_id is None:
                logger.warning("visit not found in FHIR; storing result without an encounter link")
    # Observations last: each points at the Patient and (if present) the Encounter.
    for obx in find_segments(segments, "OBX"):
        observation = observation_from_hl7(
            segments, obx, config, patient_result.resource_id, encounter_id
        )
        result_identifier = observation["identifier"][0]
        result = fhir_client.conditional_update(
            "Observation",
            {
                "identifier": identifier_search_value(
                    result_identifier["system"], result_identifier["value"]
                )
            },
            observation,
        )
        written.append(f"Observation/{result.resource_id}")
    return StoreResult(written=written)


def process_message(
    message_text: str,
    fhir_client: FhirClient,
    sources: dict[str, SourceConfig],
    event_log: EventLog | None = None,
) -> tuple[str, str]:
    """Run one message through the pipeline; return (ACK text, control ID).

    Never raises: any failure becomes an AE or AR so the sender is told, instead of the
    connection dying silently. Every outcome is logged and (if ``event_log`` is given)
    recorded; problem messages also go to the error queue.
    """
    segments: list[list[str]] = []
    problems: list[str] = []
    code: str | None = None
    result = StoreResult()
    outcome = OUTCOME_ACCEPTED
    source_name = UNKNOWN_SOURCE_NAME
    try:
        segments = split_message(message_text)
        msh = find_segment(segments, "MSH")
        facility = get_field(msh, 4) if msh else ""  # MSH-4: sending facility
        config = sources.get(facility)
        if config is not None:
            source_name = config.name
        problems = validate_message(
            segments, config.patient_id_field if config else DEFAULT_PATIENT_ID_FIELD
        )
        if msh is not None and config is None:  # a hospital we have no config for
            problems = [f"unknown sending facility {facility!r}"] + problems
            outcome, code = OUTCOME_UNKNOWN_SOURCE, ACK_ERROR
        elif problems:
            outcome, code = OUTCOME_REJECTED, ACK_ERROR
        else:
            assert config is not None  # validated above: no problems means a known source
            result = store_message(segments, fhir_client, config)
            if result.stale:
                outcome, code, problems = OUTCOME_STALE, ACK_ACCEPT, [STALE_NOTE]
    except MappingError as error:
        problems, code, outcome = [str(error)], ACK_ERROR, OUTCOME_REJECTED
    except FhirError as error:
        problems = [str(error)]
        # retryable = our side failed, not the message's fault
        code, outcome = (
            (ACK_REJECT, OUTCOME_RETRY) if error.retryable else (ACK_ERROR, OUTCOME_REJECTED)
        )
    except Exception as error:  # noqa: BLE001 - last line of defence, logged below
        logger.exception("unexpected failure")
        problems = [f"internal error: {type(error).__name__}"]
        code, outcome = ACK_REJECT, OUTCOME_RETRY

    msh = find_segment(segments, "MSH")
    control_id = (get_field(msh, 10) if msh else "") or UNKNOWN_CONTROL_ID  # MSH-10
    ack_text = build_ack(segments, problems, now=datetime.now(), code=code)
    if outcome == OUTCOME_ACCEPTED:
        logger.info(
            "control_id=%s source=%s accepted (AA) -> %s",
            control_id,
            source_name,
            ", ".join(result.written),
        )
    else:
        logger.warning(
            "control_id=%s source=%s %s (%s): %s",
            control_id,
            source_name,
            outcome,
            code,
            "; ".join(problems),
        )
    if event_log is not None:
        event_log.record(
            source=source_name,
            message_type=_message_type(segments),
            control_id=control_id,
            outcome=outcome,
            detail="; ".join(problems),
            resources=result.written,
        )
        if outcome != OUTCOME_ACCEPTED and outcome != OUTCOME_RETRY:  # keep it for review
            event_log.enqueue(
                kind=outcome,
                source=source_name,
                control_id=control_id,
                reasons=problems,
                raw_message=message_text.replace("\r", "\n"),
            )
    return ack_text, control_id


def handle_connection(
    connection: socket.socket,
    fhir_client: FhirClient,
    sources: dict[str, SourceConfig],
    event_log: EventLog | None = None,
) -> None:
    """Read frames from one connection, answering each with an ACK, until the sender hangs up."""
    reader = MllpFrameReader(connection)
    while True:
        try:
            frame_bytes = reader.read_frame()
            if frame_bytes is None:
                return
            message_text = unframe(frame_bytes)
        except MllpFramingError as error:
            logger.error("framing error: %s", error)  # never fail silently
            return
        ack_text, _control_id = process_message(message_text, fhir_client, sources, event_log)
        connection.sendall(frame(ack_text))  # the reply travels in an MLLP frame too


def serve(event_log: EventLog | None = None, ready: threading.Event | None = None) -> None:
    """Listen forever on the configured host and port.

    ``event_log`` defaults to the configured data directory; ``ready`` (if given) is set once the
    server is listening, so a caller running this in a thread knows when to start sending.
    """
    settings = load_settings()
    mllp = settings["mllp"]
    fhir_client = FhirClient(settings["fhir"]["base_url"], settings["fhir"]["timeout_seconds"])
    sources = load_sources()
    if event_log is None:
        event_log = EventLog(Path(PROJECT_ROOT / settings["paths"]["data_directory"]))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # allow quick restarts
        server.bind((mllp["host"], mllp["port"]))
        server.listen()
        logger.info("listening on %s:%s; sources: %s", mllp["host"], mllp["port"], sorted(sources))
        if ready is not None:
            ready.set()
        while True:
            connection, address = server.accept()
            logger.info("connection from %s", address)
            with connection:
                handle_connection(connection, fhir_client, sources, event_log)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    serve()
