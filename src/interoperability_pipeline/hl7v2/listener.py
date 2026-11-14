"""HL7 v2 MLLP listener: accepts connections and prints what it receives.

What it does: runs a TCP server, reads MLLP frames, prints the raw bytes, then the
unframed text and a few parsed fields. (Checkpoint 1.1 version: no ACK yet, no FHIR.)
Where it sits in the pipeline:
    simulator --> **listener** --> parser --> (ACK builder, mappers: later steps)
Standard and spec: HL7 MLLP (see mllp.py); field numbers from HL7 v2.5.1 PID/MSH.
Inputs / outputs: TCP connections on [mllp] host/port in config/settings.toml -> stdout.
How to extend: Checkpoint 1.2 replies with an ACK; 1.3 hands each message to mappers.
Run:  python -m interoperability_pipeline.hl7v2.listener
"""

import logging
import socket

from interoperability_pipeline.common.errors import MllpFramingError
from interoperability_pipeline.common.settings import load_settings
from interoperability_pipeline.hl7v2.mllp import MllpFrameReader, unframe
from interoperability_pipeline.hl7v2.parser import get_field, split_message

logger = logging.getLogger("hl7_listener")

# Segment names we read fields from.
SEGMENT_MSH = "MSH"  # message header
SEGMENT_PID = "PID"  # patient identification


def handle_connection(connection: socket.socket) -> None:
    """Read frames from one connection until the sender hangs up."""
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
        message_text = unframe(frame_bytes)
        segments = {segment[0]: segment for segment in split_message(message_text)}
        msh, pid = segments[SEGMENT_MSH], segments.get(SEGMENT_PID, [SEGMENT_PID])
        print("MSH-10 control ID :", get_field(msh, 10))  # MSH-10: message control ID
        print("PID-3 identifier  :", get_field(pid, 3))  # PID-3: patient identifier list
        print("PID-5 name        :", get_field(pid, 5))  # PID-5: name, family^given^middle
        print("PID-7 birth date  :", get_field(pid, 7))  # PID-7: date of birth
        print()


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
