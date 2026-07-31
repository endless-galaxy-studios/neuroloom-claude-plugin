"""
Configuration loader for neuroloom hook modules.

All settings are read from environment variables or the per-project
``.neuroloom.db`` state file. ``load()`` never raises — missing or
malformed values fall back to safe defaults so that hook processes
never crash due to misconfiguration.

Environment variables
---------------------
CLAUDE_PLUGIN_OPTION_API_KEY
    The global (seed) Neuroloom API key, set by Claude Code's plugin
    system. Never consulted by ``load()`` — read only by
    ``load_global_seed_key()``, whose sole sanctioned caller is
    ``/neuroloom:init`` (D171 / ADR-17). No hook process may read this
    value directly except the one allowlisted presence check in
    ``session_start.py`` — see that module and the isolation tests in
    ``tests/test_config.py``.

NEUROLOOM_API_BASE
    Base URL for the Neuroloom REST API.
    Defaults to ``https://api.neuroloom.dev``.

Resolution order for api_key
-----------------------------
1. ``config`` table in ``.neuroloom.db`` in the current working directory
   (a genuine per-project key) — or nothing at all. There is no second,
   globally-scoped fallback tier (D171 / ADR-17: a credential's storage
   scope must match the scope of the resource it addresses; a
   globally-stored credential may never authenticate project-scoped
   traffic). A project with no per-project key is dormant —
   ``api_key == ""``, ``api_key_source == "none"`` — until
   ``/neuroloom:init`` stores one.

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
    api_key_source: str = "none"   # "project" | "none"


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


def load_project_api_key(project_root: Path) -> str:
    """
    Read the per-project API key from *project_root*'s .neuroloom.db, if
    present. Returns "" if the file doesn't exist, has no api_key row, or
    reading fails for any reason. Never raises.

    Thin, tested wrapper over the same query load()'s own resolution uses
    (_load_from_state_db) -- exposed publicly so setup-flow callers
    outside this module (skills/init/SKILL.md's Step 5 and Phase 4)
    don't each hand-roll the same SQL independently (D171 round 9, M5).
    """
    return _load_from_state_db(str(project_root)) or ""


def load_global_seed_key() -> str:
    """
    Read the global (seed) Neuroloom API key set by Claude Code's plugin
    system. This is the sole sanctioned reader of
    CLAUDE_PLUGIN_OPTION_API_KEY -- it exists only for /neuroloom:init
    (and, transitively, scripts/seed_code_graph.py and skills/**/SKILL.md's
    Steps 2/3, which call it or its wrapper) to offer as a one-time seed a
    user may copy into a project's own dedicated key. Never consulted by
    load() or any hook module's live routing.

    A local regression test (D171 round 7 F-B, round 8 MAJOR-4) enforces
    that no other pyhooks/*.py, scripts/*.py, or skills/**/SKILL.md
    surface reads CLAUDE_PLUGIN_OPTION_API_KEY directly or calls this
    function outside its sanctioned callers -- see
    tests/test_config.py's isolation-scan tests. CI enforcement of that
    scan is D173's, not this module's (D171 round 7, CRITICAL-2).
    """
    return os.environ.get("CLAUDE_PLUGIN_OPTION_API_KEY", "").strip()


def load() -> Config:
    """Read configuration from the environment, returning safe defaults for any missing value."""
    cwd = os.getcwd()

    db_key = _load_from_state_db(cwd)
    api_key = db_key or ""
    api_key_source = "project" if db_key else "none"
    api_base = os.environ.get("NEUROLOOM_API_BASE", "https://api.neuroloom.dev")
    state_db_path = Path(cwd) / ".neuroloom.db"
    workspace_id = _load_workspace_id_from_state_db(cwd)

    return Config(
        api_key=api_key,
        api_base=api_base,
        state_db_path=state_db_path,
        workspace_id=workspace_id,
        api_key_source=api_key_source,
    )
