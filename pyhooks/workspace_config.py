"""
Workspace ID auto-configuration for neuroloom-claude-plugin.

Fetches the workspace_id from the Neuroloom API and writes it into the
project-level MCP server configuration so that every MCP request is
automatically routed to the correct workspace.

--- Why this matters ---

The Neuroloom MCP server issues OAuth JWTs that embed a single workspace_id
in the token claim. When a developer works on multiple projects that each need
their own workspace, the JWT claim alone is insufficient — they all share the
same token and therefore the same workspace. D144 adds support for an
X-Workspace-Id HTTP header that overrides the JWT claim, enabling per-project
workspace routing.

This module handles the Claude Code side of that story: after authentication,
the plugin fetches the workspace_id for the current API key and writes the
header into the project's .mcp.json file. No manual setup required.

--- What this module does ---

1. Calls GET /api/v1/workspaces/mine/insight to resolve the workspace_id for
   the authenticated API key. This endpoint resolves the workspace from the
   token/key itself — the caller does not need to know the workspace_id
   beforehand.

2. Stores the workspace_id in the .neuroloom.db config table (same key/value
   store used for api_key).

3. Updates (or creates) the project-level .mcp.json file to include:
     "headers": {"X-Workspace-Id": "<workspace_id>"}
   under the "neuroloom" MCP server entry.

4. Returns the workspace_id on success, None on any failure. This module
   never raises — callers rely on it being non-fatal.

--- When this is called ---

session_start.py calls _ensure_workspace_configured() after successfully
starting a session. The workspace_id fetch is skipped if one is already
stored in .neuroloom.db, so subsequent sessions incur zero network cost.

--- MCP config file selection ---

The .mcp.json file is the Claude Code project-level MCP configuration.
It lives in the project root (same directory as CLAUDE.md). This file
takes precedence over the user-level ~/.claude/mcp.json for project-specific
tool configuration.

Claude Code also reads MCP configuration from .claude/settings.json under
the "mcpServers" key, but .mcp.json is the recommended project-level path
and is what the quickstart guide shows. This module writes to .mcp.json.
"""

import json
import logging
import os
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

# Config table key for the stored workspace_id.
_WORKSPACE_ID_KEY = "workspace_id"

# GET endpoint that resolves workspace_id from the current API key/token.
_INSIGHT_PATH = "/api/v1/workspaces/mine/insight"

# HTTP timeout for the workspace fetch (seconds). Kept short — this runs at
# session start. A slow network should not block Claude Code from opening.
_FETCH_TIMEOUT = 5.0

# Name of the MCP server entry written to .mcp.json.
_MCP_SERVER_NAME = "neuroloom"

# Default MCP server URL. Used when creating a new .mcp.json entry.
_MCP_SERVER_URL = "https://mcp.neuroloom.dev/mcp"


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


def _update_mcp_json(project_root: str, workspace_id: str) -> bool:
    """
    Write or update the .mcp.json file in the project root so the neuroloom
    MCP server entry includes X-Workspace-Id in its headers.

    Strategy:
    - If .mcp.json does not exist: create it with a minimal neuroloom entry.
    - If .mcp.json exists and has a "neuroloom" entry: add or update the
      X-Workspace-Id header inside that entry's "headers" dict.
    - If .mcp.json exists but has no "neuroloom" entry: add one.

    The file is written with 2-space indentation and a trailing newline to
    match the project's .mcp.json convention.

    Returns True on success, False on any failure.
    """
    mcp_path = Path(project_root) / ".mcp.json"

    try:
        if mcp_path.exists():
            try:
                with mcp_path.open("r", encoding="utf-8") as fh:
                    config: dict = json.load(fh)
            except (json.JSONDecodeError, OSError):
                # Malformed or unreadable — do not clobber.
                logger.debug(".mcp.json is malformed, skipping workspace header update")
                return False
        else:
            config = {}

        mcp_servers: dict = config.setdefault("mcpServers", {})
        server_entry: dict = mcp_servers.setdefault(
            _MCP_SERVER_NAME,
            {"type": "http", "url": _MCP_SERVER_URL},
        )
        headers: dict = server_entry.setdefault("headers", {})

        # Only write the file if the value would actually change.
        if headers.get("X-Workspace-Id") == workspace_id:
            return True

        headers["X-Workspace-Id"] = workspace_id

        with mcp_path.open("w", encoding="utf-8") as fh:
            json.dump(config, fh, indent=2)
            fh.write("\n")

        return True

    except Exception:
        logger.debug("Failed to update .mcp.json", exc_info=True)
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
    3. Update .mcp.json with the X-Workspace-Id header.

    Returns the workspace_id on success, None if it cannot be determined.
    Always non-fatal.
    """
    if not api_key:
        return None

    # Step 1: check the local cache first.
    workspace_id = load_workspace_id_from_db(db_path)

    # Step 2: if not cached, fetch from the API and persist.
    if not workspace_id:
        workspace_id = _fetch_workspace_id_from_api(api_base, api_key)
        if workspace_id:
            _save_workspace_id_to_db(db_path, workspace_id)

    if not workspace_id:
        return None

    # Step 3: write X-Workspace-Id into the project-level .mcp.json.
    _update_mcp_json(project_root, workspace_id)

    return workspace_id
