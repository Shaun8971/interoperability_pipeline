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


class MappingError(PipelineError):
    """A message was valid HL7 but could not be mapped to FHIR (bad date, unknown authority)."""


class FhirError(PipelineError):
    """The FHIR server refused or could not be reached.

    ``retryable`` is True when the same message might succeed later (server down,
    5xx) and False when resending it unchanged would fail again (4xx).
    """

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class X12FormatError(PipelineError):
    """An X12 file did not follow the structure the tokenizer or parser requires."""
