"""
Configuration loader for neuroloom hook modules.

All settings are read from environment variables. ``load()`` never raises —
missing or malformed values fall back to safe defaults so that hook processes
never crash due to misconfiguration.

Environment variables
---------------------
CLAUDE_PLUGIN_OPTION_API_KEY
    Neuroloom API key.  Defaults to an empty string (unauthenticated).

NEUROLOOM_API_BASE
    Base URL for the Neuroloom REST API.
    Defaults to ``https://api.neuroloom.dev``.

NEUROLOOM_DEBUG
    Set to ``"1"`` or ``"true"`` (case-insensitive) to enable debug logging.
    Any other value (including unset) is treated as disabled.
"""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    api_key: str
    api_base: str
    state_db_path: Path
    debug: bool


def load() -> Config:
    """Read configuration from the environment, returning safe defaults for any missing value."""
    api_key = os.environ.get("CLAUDE_PLUGIN_OPTION_API_KEY", "")
    api_base = os.environ.get("NEUROLOOM_API_BASE", "https://api.neuroloom.dev")
    state_db_path = Path(os.getcwd()) / ".neuroloom.db"
    debug_raw = os.environ.get("NEUROLOOM_DEBUG", "").strip().lower()
    debug = debug_raw in ("1", "true")

    return Config(
        api_key=api_key,
        api_base=api_base,
        state_db_path=state_db_path,
        debug=debug,
    )
