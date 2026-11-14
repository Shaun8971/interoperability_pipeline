"""Shared exception types.

What it does: names the failures the pipeline distinguishes, so callers can react
to each differently (for example, reply AE for a bad message).
Where it sits: used by every package; depends on nothing.
Standard: none (project-specific).
How to extend: add one small exception class per new failure kind.
"""


class PipelineError(Exception):
    """Base class for every error this project raises on purpose."""


class MllpFramingError(PipelineError):
    """Bytes on the wire did not follow MLLP framing (missing or misplaced markers)."""
