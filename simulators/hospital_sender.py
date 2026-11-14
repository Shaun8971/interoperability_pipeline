"""Hospital simulator: sends one HL7 v2 sample file over MLLP and prints the ACK.

What it does: reads a sample .hl7 file, converts line endings to HL7's carriage
return, frames it with MLLP, sends it over TCP, then waits for the ACK.
Where it sits in the pipeline: the start (simulator --> listener --> ...).
Standard and spec: HL7 v2 + MLLP (see src/interoperability_pipeline/hl7v2/mllp.py).
Inputs / outputs: a file path argument -> bytes on TCP; prints the ACK's MSA segment.
How to extend: later, send all three messages in order and retry on AE/no ACK.
Run:  python simulators/hospital_sender.py samples/hl7/adt_a01.hl7
"""

import socket
import sys
from pathlib import Path

from interoperability_pipeline.common.settings import load_settings
from interoperability_pipeline.hl7v2.mllp import MllpFrameReader, frame, unframe
from interoperability_pipeline.hl7v2.parser import find_segment, split_message

SEGMENT_SEPARATOR = "\r"  # HL7 v2 ends every segment with a carriage return


def send_file(sample_path: Path) -> None:
    """Send one sample message and print the ACK (or say that none arrived).

    Example:
        send_file(Path("samples/hl7/adt_a01.hl7"))
        -> sent adt_a01.hl7 / ACK: MSA|AA|MSG00001
    """
    settings = load_settings()["mllp"]
    message_text = sample_path.read_text().strip().replace("\n", SEGMENT_SEPARATOR)
    with socket.create_connection((settings["host"], settings["port"])) as connection:
        connection.settimeout(settings["ack_timeout_seconds"])
        connection.sendall(frame(message_text))
        print(f"sent {sample_path.name}")
        try:
            ack_frame = MllpFrameReader(connection).read_frame()
        except TimeoutError:
            print("NO ACK received before the timeout: the sender cannot know if it arrived")
            return
    if ack_frame is None:
        print("connection closed without an ACK")
        return
    msa = find_segment(split_message(unframe(ack_frame)), "MSA")
    print("ACK:", "|".join(msa) if msa else "(no MSA segment)")


if __name__ == "__main__":
    send_file(Path(sys.argv[1]))
