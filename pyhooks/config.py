"""
Configuration loader for neuroloom hook modules.

All settings are read from environment variables. ``load()`` never raises —
missing or malformed values fall back to safe defaults so that hook processes
never crash due to misconfiguration.

Environment variables
---------------------
CLAUDE_PLUGIN_OPTION_API_KEY
    Neuroloom API key (checked first, set by Claude Code plugin system).

NEUROLOOM_API_KEY
    Neuroloom API key fallback (for manual configuration or CI).

NEUROLOOM_API_BASE
    Base URL for the Neuroloom REST API.
    Defaults to ``https://api.neuroloom.dev``.
"""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    api_key: str
    api_base: str
    state_db_path: Path


def load() -> Config:
    """Read configuration from the environment, returning safe defaults for any missing value."""
    # Check CLAUDE_PLUGIN_OPTION_API_KEY first (set by Claude Code plugin system),
    # fall back to NEUROLOOM_API_KEY (manual config / CI environments)
    api_key = (
        os.environ.get("CLAUDE_PLUGIN_OPTION_API_KEY", "").strip()
        or os.environ.get("NEUROLOOM_API_KEY", "").strip()
    )
    api_base = os.environ.get("NEUROLOOM_API_BASE", "https://api.neuroloom.dev")
    state_db_path = Path(os.getcwd()) / ".neuroloom.db"

    return Config(
        api_key=api_key,
        api_base=api_base,
        state_db_path=state_db_path,
    )
