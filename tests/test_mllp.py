"""Tests for MLLP framing and the frame reader."""

import socket

import pytest

from interoperability_pipeline.common.errors import MllpFramingError
from interoperability_pipeline.hl7v2.mllp import MllpFrameReader, frame, unframe


def test_frame_adds_markers() -> None:
    assert frame("MSH|x") == b"\x0bMSH|x\x1c\r"


def test_unframe_is_the_inverse_of_frame() -> None:
    assert unframe(frame("MSH|x\rPID|1")) == "MSH|x\rPID|1"


def test_unframe_rejects_missing_start() -> None:
    with pytest.raises(MllpFramingError):
        unframe(b"MSH|x\x1c\r")


def test_unframe_rejects_missing_end() -> None:
    with pytest.raises(MllpFramingError):
        unframe(b"\x0bMSH|x")


def test_reader_splits_two_frames_glued_together() -> None:
    sender, receiver = socket.socketpair()
    sender.sendall(frame("ONE") + frame("TWO"))
    sender.close()
    reader = MllpFrameReader(receiver)
    assert unframe(reader.read_frame()) == "ONE"  # type: ignore[arg-type]
    assert unframe(reader.read_frame()) == "TWO"  # type: ignore[arg-type]
    assert reader.read_frame() is None
    receiver.close()


def test_reader_rejects_connection_closed_mid_frame() -> None:
    sender, receiver = socket.socketpair()
    sender.sendall(b"\x0bMSH|half")
    sender.close()
    with pytest.raises(MllpFramingError):
        MllpFrameReader(receiver).read_frame()
    receiver.close()
