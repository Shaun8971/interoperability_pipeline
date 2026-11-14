"""Load non-secret configuration from config/settings.toml.

What it does: reads the TOML file once and returns it as a plain dictionary.
Where it sits: shared by the listener, the simulators and the FHIR client.
Standard: none. TOML spec: https://toml.io/en/
Inputs / outputs: path to a TOML file (default config/settings.toml) -> dict.
How to extend: add sections to settings.toml; read them with ``settings["section"]["key"]``.
Secrets never go here; they belong in .env.
"""

import tomllib
from pathlib import Path
from typing import Any

# Project root = four levels up from this file (src/<package>/common/settings.py).
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.toml"


def load_settings(path: Path = DEFAULT_SETTINGS_PATH) -> dict[str, Any]:
    """Read a TOML settings file.

    Example:
        >>> load_settings()["mllp"]["port"]
        2575
    """
    with path.open("rb") as settings_file:  # tomllib requires binary mode
        return tomllib.load(settings_file)
