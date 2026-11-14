"""HL7 v2 MLLP listener: receives messages, validates them and replies with an ACK.

What it does: runs a TCP server; for each MLLP frame it unframes, parses, validates,
and sends back an ACK (AA good, AE bad). It still prints the raw bytes (Checkpoint 1.1).
Where it sits in the pipeline:
    simulator --> **listener** --> parser --> validation --> ACK --> back to simulator
    (mappers to FHIR are added in Checkpoint 1.3)
Standard and spec: HL7 MLLP (see mllp.py); ACK rules in ack.py; checks in validation.py.
Inputs / outputs: TCP on [mllp] host/port in config/settings.toml -> ACK bytes + log lines.
How to extend: after the ACK decision, hand accepted messages to the mappers.
Run:  python -u -m interoperability_pipeline.hl7v2.listener
"""

import logging
import socket
from datetime import datetime

from interoperability_pipeline.common.errors import MllpFramingError
from interoperability_pipeline.common.settings import load_settings
from interoperability_pipeline.hl7v2.ack import ACK_ACCEPT, build_ack
from interoperability_pipeline.hl7v2.mllp import MllpFrameReader, frame, unframe
from interoperability_pipeline.hl7v2.parser import find_segment, get_field, split_message
from interoperability_pipeline.hl7v2.validation import validate_message

logger = logging.getLogger("hl7_listener")

# Used in log lines when the message has no usable MSH-10.
UNKNOWN_CONTROL_ID = "<none>"


def process_message(message_text: str) -> tuple[str, str]:
    """Parse and validate one message; return (ACK text, control ID) for the reply.

    Never raises: any unexpected failure becomes an AE so the sender is told, instead
    of the connection dying silently.
    """
    try:
        segments = split_message(message_text)
        problems = validate_message(segments)
    except Exception as error:  # noqa: BLE001 - last line of defence, logged below
        logger.exception("unexpected failure while parsing")
        segments, problems = [], [f"internal error: {type(error).__name__}"]
    msh = find_segment(segments, "MSH")
    control_id = (get_field(msh, 10) if msh else "") or UNKNOWN_CONTROL_ID  # MSH-10
    ack_text = build_ack(segments, problems, now=datetime.now())
    if problems:
        logger.warning("control_id=%s REJECTED (AE): %s", control_id, "; ".join(problems))
    else:
        logger.info("control_id=%s accepted (%s)", control_id, ACK_ACCEPT)
    return ack_text, control_id


def handle_connection(connection: socket.socket) -> None:
    """Read frames from one connection, answering each with an ACK, until the sender hangs up."""
    reader = MllpFrameReader(connection)
    while True:
        try:
            frame_bytes = reader.read_frame()
        except MllpFramingError as error:
            logger.error("framing error: %s", error)  # never fail silently
            return
        if frame_bytes is None:
            return
        print("RAW BYTES:", frame_bytes)
        try:
            message_text = unframe(frame_bytes)
        except MllpFramingError as error:
            logger.error("framing error: %s", error)
            return
        ack_text, _control_id = process_message(message_text)
        connection.sendall(frame(ack_text))  # the reply travels in an MLLP frame too


def serve() -> None:
    """Listen forever on the configured host and port."""
    settings = load_settings()["mllp"]
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # allow quick restarts
        server.bind((settings["host"], settings["port"]))
        server.listen()
        logger.info("listening on %s:%s", settings["host"], settings["port"])
        while True:
            connection, address = server.accept()
            logger.info("connection from %s", address)
            with connection:
                handle_connection(connection)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    serve()
