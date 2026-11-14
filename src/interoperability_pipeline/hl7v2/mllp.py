"""MLLP (Minimal Lower Layer Protocol) framing for HL7 v2 over TCP.

What it does: wraps an HL7 message in start/end marker bytes (``frame``), removes
them (``unframe``), and collects bytes from a socket until one whole frame has
arrived (``MllpFrameReader``).

Why it exists: TCP is a plain byte stream with no message boundaries. Messages can
arrive split across reads or glued together, so the markers say where one ends.

Where it sits in the pipeline:
    hospital --TCP bytes--> **MLLP reader/unframe** --> parser --> mappers --> FHIR

Standard and spec: HL7 MLLP, https://confluence.hl7.org/display/INTEROP/MLLP
(official transport spec: https://www.hl7.org, free with registration).

Wire format:  <0x0B> message text <0x1C><0x0D>

Inputs / outputs:
    frame(str) -> bytes;  unframe(bytes) -> str;  MllpFrameReader.read_frame() -> bytes | None

How to extend: for other transports (HTTP, files) write a sibling module that also
yields message text; the parser and mappers do not care how the text arrived.
"""

import socket

from interoperability_pipeline.common.errors import MllpFramingError

# --- MLLP marker bytes ---------------------------------------------------------
START_BLOCK = b"\x0b"  # vertical tab: begins a frame; never appears in HL7 text
END_BLOCK = b"\x1c"  # file separator: first byte of the end marker
CARRIAGE_RETURN = b"\r"  # second byte of the end marker
FRAME_END = END_BLOCK + CARRIAGE_RETURN  # the full two-byte end marker
MESSAGE_ENCODING = "utf-8"  # HL7 v2.5.1 text is ASCII-compatible; UTF-8 is a safe superset
RECEIVE_CHUNK_SIZE = 4096  # bytes asked of the socket per read; any value works


def frame(message_text: str) -> bytes:
    """Wrap message text in MLLP markers.

    Example:
        >>> frame("MSH|x")
        b'\\x0bMSH|x\\x1c\\r'
    """
    return START_BLOCK + message_text.encode(MESSAGE_ENCODING) + FRAME_END


def unframe(frame_bytes: bytes) -> str:
    """Remove MLLP markers and return the message text.

    Raises MllpFramingError if a marker is missing: a half-framed message must be
    rejected loudly, not parsed as if it were complete.

    Example:
        >>> unframe(b'\\x0bMSH|x\\x1c\\r')
        'MSH|x'
    """
    if not frame_bytes.startswith(START_BLOCK):
        raise MllpFramingError("frame does not start with 0x0B")
    if not frame_bytes.endswith(FRAME_END):
        raise MllpFramingError("frame does not end with 0x1C 0x0D")
    body = frame_bytes[len(START_BLOCK) : -len(FRAME_END)]
    return body.decode(MESSAGE_ENCODING)


class MllpFrameReader:
    """Reads complete frames from a connected socket.

    It keeps a buffer because TCP may deliver half a frame, or two frames at once.
    """

    def __init__(self, connection: socket.socket) -> None:
        self._connection = connection
        self._buffer = b""

    def read_frame(self) -> bytes | None:
        """Return the next complete frame (markers included), or None if the peer
        closed the connection cleanly between frames.

        Raises MllpFramingError if the connection closes in the middle of a frame.
        """
        while FRAME_END not in self._buffer:
            chunk = self._connection.recv(RECEIVE_CHUNK_SIZE)
            if not chunk:  # peer closed the connection
                if self._buffer:
                    raise MllpFramingError("connection closed in the middle of a frame")
                return None
            self._buffer += chunk
        end_position = self._buffer.index(FRAME_END) + len(FRAME_END)
        frame_bytes, self._buffer = self._buffer[:end_position], self._buffer[end_position:]
        return frame_bytes
