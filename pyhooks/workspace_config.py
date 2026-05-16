"""
Workspace ID auto-configuration for neuroloom-claude-plugin.

Fetches the workspace_id from the Neuroloom API and writes it into the
project's plugin config so that every MCP request includes the correct
X-Workspace-Id header via ${user_config.workspace_id} substitution.

--- Why this matters ---

The Neuroloom MCP server issues OAuth JWTs that embed a single workspace_id
in the token claim. When a developer works on multiple projects that each need
their own workspace, the JWT claim alone is insufficient — they all share the
same token and therefore the same workspace. D144 adds support for an
X-Workspace-Id HTTP header that overrides the JWT claim, enabling per-project
workspace routing.

This module handles the Claude Code side of that story: after authentication,
the plugin fetches the workspace_id for the current API key and writes it
into the project's pluginConfigs in .claude/settings.json. Claude Code's
${user_config.workspace_id} substitution in the plugin's .mcp.json then
injects the header on every MCP request. No manual setup required.

--- What this module does ---

1. Calls GET /api/v1/workspaces/mine/insight to resolve the workspace_id for
   the authenticated API key. This endpoint resolves the workspace from the
   token/key itself — the caller does not need to know the workspace_id
   beforehand.

2. Stores the workspace_id in the .neuroloom.db config table (same key/value
   store used for api_key).

3. Writes the workspace_id into .claude/settings.json under
   pluginConfigs["neuroloom@endless-galaxy-studios"].options.workspace_id.
   Claude Code reads this value and substitutes it into the plugin's .mcp.json
   header template at runtime.

4. Returns the workspace_id on success, None on any failure. This module
   never raises — callers rely on it being non-fatal.

--- When this is called ---

session_start.py calls _ensure_workspace_configured() after successfully
starting a session. The workspace_id fetch is skipped if one is already
stored in .neuroloom.db, so subsequent sessions incur zero network cost.
"""

import json
import logging
import os
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

_WORKSPACE_ID_KEY = "workspace_id"

_INSIGHT_PATH = "/api/v1/workspaces/mine/insight"

_FETCH_TIMEOUT = 5.0

_PLUGIN_ID = "neuroloom@endless-galaxy-studios"


def load_workspace_id_from_db(db_path: Path) -> str | None:
    """
    Read the stored workspace_id from .neuroloom.db config table.

    Returns None if the file does not exist, the table has no entry, or
    reading fails for any reason.
    """
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute(
            "SELECT value FROM config WHERE key = ? LIMIT 1",
            (_WORKSPACE_ID_KEY,),
        )
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        logger.debug("Failed to read workspace_id from .neuroloom.db", exc_info=True)
        return None


def _save_workspace_id_to_db(db_path: Path, workspace_id: str) -> None:
    """
    Persist workspace_id into the config table of .neuroloom.db.

    Uses INSERT OR REPLACE so this is safe to call on every session start
    when the workspace changes (rare, but possible when an admin re-assigns
    a key to a different workspace).
    """
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
            (_WORKSPACE_ID_KEY, workspace_id),
        )
        conn.commit()
        conn.close()
    except Exception:
        logger.debug("Failed to save workspace_id to .neuroloom.db", exc_info=True)


def _fetch_workspace_id_from_api(api_base: str, api_key: str) -> str | None:
    """
    Call GET /api/v1/workspaces/mine/insight and return the workspace_id.

    The insight endpoint resolves the workspace from the API key itself —
    the caller does not need to supply the workspace_id. This is the
    "bootstrap" call: from zero information (just an API key), discover
    which workspace to route to.

    Returns None on any network or parse failure.
    """
    url = f"{api_base}{_INSIGHT_PATH}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Token {api_key}",
            "User-Agent": "neuroloom-plugin/0.1.0",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            body = json.loads(resp.read().decode("utf-8"))
            workspace_id: str | None = body.get("workspace_id")
            return workspace_id if workspace_id else None
    except urllib.error.HTTPError as exc:
        logger.debug("workspace insight fetch returned HTTP %s", exc.code)
        return None
    except Exception:
        logger.debug("workspace insight fetch failed", exc_info=True)
        return None


def _update_plugin_config(project_root: str, workspace_id: str) -> bool:
    """
    Write workspace_id into the project's .claude/settings.json under
    pluginConfigs so Claude Code substitutes it via ${user_config.workspace_id}.

    The settings.json path: .claude/settings.json
    The key path: pluginConfigs["neuroloom@endless-galaxy-studios"].options.workspace_id

    Strategy:
    - Read existing settings.json (create if missing).
    - Merge workspace_id into the pluginConfigs section.
    - Write back only if the value changed.

    Returns True on success, False on any failure.
    """
    settings_path = Path(project_root) / ".claude" / "settings.json"

    try:
        if settings_path.exists():
            try:
                with settings_path.open("r", encoding="utf-8") as fh:
                    settings: dict = json.load(fh)
            except (json.JSONDecodeError, OSError):
                logger.debug(".claude/settings.json is malformed, skipping")
                return False
        else:
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings = {}

        plugin_configs: dict = settings.setdefault("pluginConfigs", {})
        plugin_entry: dict = plugin_configs.setdefault(_PLUGIN_ID, {})
        options: dict = plugin_entry.setdefault("options", {})

        if options.get(_WORKSPACE_ID_KEY) == workspace_id:
            return True

        options[_WORKSPACE_ID_KEY] = workspace_id

        with settings_path.open("w", encoding="utf-8") as fh:
            json.dump(settings, fh, indent=2)
            fh.write("\n")

        return True

    except Exception:
        logger.debug("Failed to update .claude/settings.json", exc_info=True)
        return False


def ensure_workspace_configured(
    project_root: str,
    db_path: Path,
    api_base: str,
    api_key: str,
) -> str | None:
    """
    Ensure the workspace_id is configured for this project.

    Resolution order:
    1. Read from .neuroloom.db config table (fast path — no network call).
    2. If not stored, fetch from the API and cache it in .neuroloom.db.
    3. Write workspace_id into .claude/settings.json pluginConfigs.

    Returns the workspace_id on success, None if it cannot be determined.
    Always non-fatal.
    """
    if not api_key:
        return None

    workspace_id = load_workspace_id_from_db(db_path)

    if not workspace_id:
        workspace_id = _fetch_workspace_id_from_api(api_base, api_key)
        if workspace_id:
            _save_workspace_id_to_db(db_path, workspace_id)

    if not workspace_id:
        return None

    _update_plugin_config(project_root, workspace_id)

    return workspace_id
