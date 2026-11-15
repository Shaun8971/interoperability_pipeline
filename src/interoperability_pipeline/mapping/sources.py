"""Load per-hospital configuration (config/sources/*.toml).

What it does: reads one TOML file per sending hospital into a ``SourceConfig`` and finds the
right one for an incoming message by its sending facility (MSH-4).
Where it sits in the pipeline: before validation and mapping; every hospital-specific fact
comes through here, so onboarding a hospital means adding a file, not changing code.
Standard and spec: TOML https://toml.io/en/ ; MSH-4 sending facility
https://hl7-definition.caristix.com/v2/HL7v2.5.1/Fields/MSH.4
Inputs / outputs: a directory of TOML files -> dict of sending facility -> SourceConfig.
How to extend: add a field to the TOML files and to ``SourceConfig``; give it a default so
existing hospital files keep working.
"""

import tomllib
from pathlib import Path
from typing import Any

from interoperability_pipeline.common.errors import PipelineError
from interoperability_pipeline.common.settings import PROJECT_ROOT
from interoperability_pipeline.hl7v2.mappers import SourceConfig

SOURCES_DIRECTORY = PROJECT_ROOT / "config" / "sources"
DEFAULT_PATIENT_ID_FIELD = 3  # PID-3 is where the HL7 standard puts the patient identifier


def _read(path: Path) -> SourceConfig:
    with path.open("rb") as handle:
        raw: dict[str, Any] = tomllib.load(handle)
    source = raw["source"]
    return SourceConfig(
        patient_identifier_systems=raw["patient_identifier_systems"],
        visit_identifier_systems=raw["visit_identifier_systems"],
        observation_identifier_systems=raw["observation_identifier_systems"],
        timezone=source["timezone"],
        patient_id_field=source.get("patient_id_field", DEFAULT_PATIENT_ID_FIELD),
        coding_systems=raw.get("coding_systems", {}),
        code_translations=raw.get("code_translations", {}),
        name=source["name"],
        sending_facility=source["sending_facility"],
    )


def load_source(name: str, directory: Path = SOURCES_DIRECTORY) -> SourceConfig:
    """Load one hospital's config by file name (without .toml).

    Example: load_source("hospital_a").timezone -> 'America/New_York'
    """
    path = directory / f"{name}.toml"
    if not path.exists():
        raise PipelineError(f"no source config file {path}")
    return _read(path)


def load_sources(directory: Path = SOURCES_DIRECTORY) -> dict[str, SourceConfig]:
    """Load every hospital file, keyed by sending facility (MSH-4).

    Example: load_sources().keys() -> dict_keys(['MERCY_GENERAL', 'ST_LUKES'])
    """
    sources: dict[str, SourceConfig] = {}
    for path in sorted(directory.glob("*.toml")):
        config = _read(path)
        if config.sending_facility in sources:
            raise PipelineError(f"two source files claim facility {config.sending_facility!r}")
        sources[config.sending_facility] = config
    return sources
