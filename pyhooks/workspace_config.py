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
   store used for api_key), alongside a SHA-256 fingerprint of the API key
   it was resolved for (D167 Phase 6 / F14). The cached workspace_id is only
   trusted while the fingerprint still matches — a rotated API key
   invalidates the cache and forces a re-fetch, since the new key may
   resolve to a different workspace.

3. Writes the workspace_id into .claude/settings.json under
   pluginConfigs["neuroloom@endless-galaxy-studios"].options.workspace_id.
   Claude Code reads this value and substitutes it into the plugin's .mcp.json
   header template at runtime.

4. Returns the workspace_id on success, ``WORKSPACE_ID_AMBIGUOUS`` if the
   caller has 2+ workspace memberships and none can be auto-resolved (a
   structural condition that will not resolve itself on retry — the user
   must configure ``options.workspace_id`` manually), or ``None`` on any
   transient failure (network error, unreachable API, unrecognized error
   shape). This module never raises — callers rely on it being non-fatal.

--- When this is called ---

session_start.py calls ensure_workspace_configured() after successfully
starting a session. The workspace_id fetch is skipped if a cached value is
present and its fingerprint still matches the current API key, so most
sessions incur zero network cost.
"""

import hashlib
import json
import logging
import os
import sqlite3
import urllib.error
import urllib.request
from enum import Enum
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger(__name__)

_WORKSPACE_ID_KEY = "workspace_id"

# F14 (D167 Phase 6): SHA-256 of the API key the cached workspace_id was
# resolved for. Mirrors the MCP server's own token-hash-caching pattern —
# the raw key is never stored or logged, only its digest. Compared against a
# freshly computed fingerprint on every session start so a rotated API key
# (which may now belong to a different workspace) invalidates the cache
# instead of the plugin trusting a stale workspace_id forever.
_WORKSPACE_ID_FINGERPRINT_KEY = "workspace_id_key_fingerprint"

# F15: sentinel returned by ensure_workspace_configured when the API key
# belongs to a caller with 2+ workspace memberships and no workspace_id can
# be auto-resolved (the new `workspace_not_specified` 400). Distinct from
# None, which continues to mean "not resolved — stay silent, retry later."
# Not a valid workspace_id shape (workspace_ids are UUIDs), so it can never
# collide with a real resolved value.
WORKSPACE_ID_AMBIGUOUS = "__ambiguous__"

_INSIGHT_PATH = "/api/v1/workspaces/mine/insight"

_FETCH_TIMEOUT = 5.0

_PLUGIN_ID = "neuroloom@endless-galaxy-studios"


class WorkspaceFetchStatus(Enum):
    """Outcome of a call to ``_fetch_workspace_id_from_api``."""

    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    UNREACHABLE = "unreachable"


class WorkspaceFetchResult(NamedTuple):
    """
    Widened result of a workspace-insight fetch (F15).

    ``status`` distinguishes three outcomes that used to all collapse to
    ``None``: a resolved workspace_id, a structurally ambiguous caller
    (2+ memberships, nothing to auto-pick), and "unreachable" — a bucket
    that covers genuine network failures AND any error-body shape this
    parser doesn't recognize (including the pre-D167-Phase-1 flat
    ``{"detail": ...}`` shape, which may still be served during a
    deploy-skew window). Unreachable is deliberately non-actionable —
    the caller stays silent and retries next session, same as before.
    """

    status: WorkspaceFetchStatus
    workspace_id: str | None = None


def _fingerprint_api_key(api_key: str) -> str:
    """SHA-256 hex digest of the API key — never store or log the raw key."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _load_config_value(db_path: Path, key: str) -> str | None:
    """
    Read a single value from the .neuroloom.db config table.

    Returns None if the file does not exist, the key has no row, or
    reading fails for any reason.
    """
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute(
            "SELECT value FROM config WHERE key = ? LIMIT 1",
            (key,),
        )
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        logger.debug("Failed to read %s from .neuroloom.db", key, exc_info=True)
        return None


def _save_config_value(db_path: Path, key: str, value: str) -> None:
    """
    Persist a single value into the config table of .neuroloom.db.

    Uses INSERT OR REPLACE so this is safe to call on every session start
    when the value changes (e.g. workspace re-assignment, key rotation).
    """
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
            (key, value),
        )
        conn.commit()
        conn.close()
    except Exception:
        logger.debug("Failed to save %s to .neuroloom.db", key, exc_info=True)


def load_workspace_id_from_db(db_path: Path) -> str | None:
    """Read the stored workspace_id from .neuroloom.db config table."""
    return _load_config_value(db_path, _WORKSPACE_ID_KEY)


def _save_workspace_id_to_db(db_path: Path, workspace_id: str) -> None:
    """Persist workspace_id into the config table of .neuroloom.db."""
    _save_config_value(db_path, _WORKSPACE_ID_KEY, workspace_id)


def _parse_ambiguous_error_body(raw_body: bytes) -> bool:
    """
    Return True if *raw_body* is a D167-Phase-1 workspace-resolution error
    body with ``code == "workspace_not_specified"``.

    Defensive by construction: any shape this doesn't recognize — including
    the pre-Phase-1 flat ``{"detail": ...}`` body, an empty body, or
    non-JSON content — returns False rather than raising. False here always
    means "treat as unreachable, not ambiguous"; it never raises up to the
    caller.
    """
    try:
        parsed = json.loads(raw_body.decode("utf-8"))
    except Exception:
        return False
    if not isinstance(parsed, dict):
        return False
    error = parsed.get("error")
    if not isinstance(error, dict):
        return False
    return error.get("code") == "workspace_not_specified"


def _fetch_workspace_id_from_api(api_base: str, api_key: str) -> WorkspaceFetchResult:
    """
    Call GET /api/v1/workspaces/mine/insight and classify the outcome.

    The insight endpoint resolves the workspace from the API key itself —
    the caller does not need to supply the workspace_id. This is the
    "bootstrap" call: from zero information (just an API key), discover
    which workspace to route to.

    Never raises. Any failure mode this function does not specifically
    recognize as AMBIGUOUS classifies as UNREACHABLE.
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
                return WorkspaceFetchResult(WorkspaceFetchStatus.UNREACHABLE)
            body = json.loads(resp.read().decode("utf-8"))
            workspace_id: str | None = body.get("workspace_id")
            if workspace_id:
                return WorkspaceFetchResult(WorkspaceFetchStatus.RESOLVED, workspace_id)
            return WorkspaceFetchResult(WorkspaceFetchStatus.UNREACHABLE)
    except urllib.error.HTTPError as exc:
        logger.debug("workspace insight fetch returned HTTP %s", exc.code)
        try:
            raw_body = exc.read()
        except Exception:
            raw_body = b""
        if exc.code == 400 and _parse_ambiguous_error_body(raw_body):
            return WorkspaceFetchResult(WorkspaceFetchStatus.AMBIGUOUS)
        return WorkspaceFetchResult(WorkspaceFetchStatus.UNREACHABLE)
    except Exception:
        logger.debug("workspace insight fetch failed", exc_info=True)
        return WorkspaceFetchResult(WorkspaceFetchStatus.UNREACHABLE)


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
    1. Read from .neuroloom.db config table (fast path — no network call) —
       but only trust it if the stored key fingerprint (F14) matches the
       current api_key's fingerprint. A rotated key invalidates the cache
       even if a workspace_id row is still present, since the new key may
       resolve to a different workspace.
    2. If not cached (or the cache was invalidated), fetch from the API and
       cache both the workspace_id and the key fingerprint in .neuroloom.db.
    3. Write workspace_id into .claude/settings.json pluginConfigs.

    Returns (F15):
    - the workspace_id string on success
    - ``WORKSPACE_ID_AMBIGUOUS`` if the caller has 2+ workspace memberships
      and none can be auto-resolved — a structural condition that will not
      resolve itself on retry; the user must set
      ``pluginConfigs["neuroloom@endless-galaxy-studios"].options.workspace_id``
      manually
    - ``None`` if the workspace_id cannot be determined for a transient
      reason (network failure, unreachable API, unrecognized error shape) —
      retried silently on the next session start

    Always non-fatal — never raises.
    """
    if not api_key:
        return None

    fingerprint = _fingerprint_api_key(api_key)
    cached_workspace_id = load_workspace_id_from_db(db_path)
    cached_fingerprint = _load_config_value(db_path, _WORKSPACE_ID_FINGERPRINT_KEY)

    workspace_id: str | None = None
    if cached_workspace_id and cached_fingerprint == fingerprint:
        workspace_id = cached_workspace_id

    if not workspace_id:
        result = _fetch_workspace_id_from_api(api_base, api_key)
        if result.status is WorkspaceFetchStatus.RESOLVED and result.workspace_id:
            workspace_id = result.workspace_id
            _save_workspace_id_to_db(db_path, workspace_id)
            _save_config_value(db_path, _WORKSPACE_ID_FINGERPRINT_KEY, fingerprint)
        elif result.status is WorkspaceFetchStatus.AMBIGUOUS:
            return WORKSPACE_ID_AMBIGUOUS
        else:
            return None

    _update_plugin_config(project_root, workspace_id)

    return workspace_id
