"""
Configuration loader for neuroloom hook modules.

All settings are read from environment variables. ``load()`` never raises —
missing or malformed values fall back to safe defaults so that hook processes
never crash due to misconfiguration.

Environment variables
---------------------
CLAUDE_PLUGIN_OPTION_API_KEY
    Neuroloom API key (global fallback, set by Claude Code plugin system).

NEUROLOOM_API_KEY
    Neuroloom API key fallback (for manual configuration or CI).

NEUROLOOM_API_BASE
    Base URL for the Neuroloom REST API.
    Defaults to ``https://api.neuroloom.dev``.

Resolution order for api_key
-----------------------------
1. ``config`` table in ``.neuroloom.db`` in the current working directory
   (per-project key — ensures each project routes to its own workspace).
2. ``CLAUDE_PLUGIN_OPTION_API_KEY`` environment variable (set by Claude Code
   plugin system — global fallback when no per-project key exists).
3. ``NEUROLOOM_API_KEY`` environment variable (manual config or CI).

The per-project key takes priority because Claude Code stores the plugin
userConfig API key globally (in the macOS Keychain via ``sensitive: true``),
so all projects share the same key.  Projects that have a per-project key
in .neuroloom.db route to the correct workspace; projects without one fall
through to the global key.

workspace_id
------------
The ``workspace_id`` field is loaded exclusively from the ``config`` table in
``.neuroloom.db``.  It is written there by
``pyhooks.workspace_config.ensure_workspace_configured`` after the first
successful session start.  There is no environment variable override for
workspace_id — the value is always fetched from the Neuroloom API and cached
locally so it stays in sync with the API key's actual workspace assignment.
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
    workspace_id: str = ""


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


def _load_workspace_id_from_state_db(cwd: str) -> str:
    """
    Load workspace_id from .neuroloom.db config table.

    Returns an empty string if the file does not exist, the entry is missing,
    or reading fails.  The workspace_id is populated asynchronously by
    ``pyhooks.workspace_config.ensure_workspace_configured`` after the first
    successful session start.
    """
    db_path = os.path.join(cwd, ".neuroloom.db")
    if not os.path.exists(db_path):
        return ""
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT value FROM config WHERE key = 'workspace_id' LIMIT 1"
        )
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else ""
    except Exception:
        logger.warning("Failed to load workspace_id from .neuroloom.db", exc_info=True)
        return ""


def load() -> Config:
    """Read configuration from the environment, returning safe defaults for any missing value."""
    cwd = os.getcwd()

    # Resolution order (per-project first, global fallback):
    # 1. .neuroloom.db config table — per-project key, correct workspace
    # 2. CLAUDE_PLUGIN_OPTION_API_KEY — global key from macOS Keychain
    # 3. NEUROLOOM_API_KEY — manual config or CI
    api_key = (
        _load_from_state_db(cwd)
        or os.environ.get("CLAUDE_PLUGIN_OPTION_API_KEY", "").strip()
        or os.environ.get("NEUROLOOM_API_KEY", "").strip()
        or ""
    )
    api_base = os.environ.get("NEUROLOOM_API_BASE", "https://api.neuroloom.dev")
    state_db_path = Path(cwd) / ".neuroloom.db"
    workspace_id = _load_workspace_id_from_state_db(cwd)

    return Config(
        api_key=api_key,
        api_base=api_base,
        state_db_path=state_db_path,
        workspace_id=workspace_id,
    )
