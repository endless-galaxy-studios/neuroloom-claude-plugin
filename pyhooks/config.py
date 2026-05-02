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

Resolution order for api_key
-----------------------------
1. ``CLAUDE_PLUGIN_OPTION_API_KEY`` environment variable (set by Claude Code
   plugin system when the user configures an API key via ``/plugins configure``).
2. ``NEUROLOOM_API_KEY`` environment variable (manual config or CI).
3. ``config`` table in ``.neuroloom.db`` in the current working directory
   (written by the OAuth flow so that non-env callers can authenticate).
"""

import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Config:
    api_key: str
    api_base: str
    state_db_path: Path


def _load_from_state_db(cwd: str) -> str | None:
    """Load api_key from .neuroloom.db config table. Returns None if not found."""
    db_path = os.path.join(cwd, ".neuroloom.db")
    if not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("SELECT value FROM config WHERE key = 'api_key' LIMIT 1")
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        logger.warning("Failed to load api_key from .neuroloom.db", exc_info=True)
        return None


def load() -> Config:
    """Read configuration from the environment, returning safe defaults for any missing value."""
    cwd = os.getcwd()

    # Resolution order:
    # 1. CLAUDE_PLUGIN_OPTION_API_KEY — set by Claude Code plugin system
    # 2. NEUROLOOM_API_KEY — manual config or CI
    # 3. .neuroloom.db config table — written by the OAuth flow
    api_key = (
        os.environ.get("CLAUDE_PLUGIN_OPTION_API_KEY", "").strip()
        or os.environ.get("NEUROLOOM_API_KEY", "").strip()
        or _load_from_state_db(cwd)
        or ""
    )
    api_base = os.environ.get("NEUROLOOM_API_BASE", "https://api.neuroloom.dev")
    state_db_path = Path(cwd) / ".neuroloom.db"

    return Config(
        api_key=api_key,
        api_base=api_base,
        state_db_path=state_db_path,
    )
