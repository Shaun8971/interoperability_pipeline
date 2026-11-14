"""Hospital simulator: sends one HL7 v2 sample file to the listener over MLLP.

What it does: reads a sample .hl7 file, converts line endings to HL7's carriage
return, frames it with MLLP and sends it over TCP.
Where it sits in the pipeline: the start (simulator --> listener --> ...).
Standard and spec: HL7 v2 + MLLP (see src/interoperability_pipeline/hl7v2/mllp.py).
Inputs / outputs: a file path argument -> bytes on the TCP connection.
How to extend: Checkpoint 1.2 will also read the ACK; later, send all three messages.
Run:  python simulators/hospital_sender.py samples/hl7/adt_a01.hl7
"""

import socket
import sys
from pathlib import Path

from interoperability_pipeline.common.settings import load_settings
from interoperability_pipeline.hl7v2.mllp import frame

SEGMENT_SEPARATOR = "\r"  # HL7 v2 ends every segment with a carriage return


def send_file(sample_path: Path) -> None:
    """Send one sample message.

    Example:
        send_file(Path("samples/hl7/adt_a01.hl7"))
    """
    settings = load_settings()["mllp"]
    message_text = sample_path.read_text().strip().replace("\n", SEGMENT_SEPARATOR)
    with socket.create_connection((settings["host"], settings["port"])) as connection:
        connection.sendall(frame(message_text))
    print(f"sent {sample_path.name}")


if __name__ == "__main__":
    send_file(Path(sys.argv[1]))
