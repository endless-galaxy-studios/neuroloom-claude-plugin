"""
SessionStart hook for neuroloom-claude-plugin.

Runs once at the beginning of each Claude Code session.  Responsibilities, in
order:

1. Load configuration from environment.
2. Open the SQLite state database.
3. Guard: if no API key is configured, print setup instructions and exit 0.
4. End any stale session left over from a previous invocation.
5. Start a new session via the Neuroloom REST API.
6. Prune old trace rows to keep the DB from growing unboundedly.
7. Flush any buffered observation events that did not drain during the last
   session.
8. Ensure workspace routing is configured for this project (D169): read a
   manual, human-provided per-project override from
   ``.claude/settings.json`` (migrating away any residue left by a released
   version's own past auto-write — see ``workspace_config.py``), and write
   (or ensure) a project-scope ``.mcp.json`` "neuroloom" entry the plugin
   can prove it owns. A literal ``X-Workspace-Id`` header is written only
   when a genuine override is configured; otherwise the entry is left
   headerless, deferring to the live MCP connection's own server-side
   auto-resolution (ADR-13). Never touches an entry this module didn't
   create itself.
9. Ensure ``.neuroloom.db`` is listed in the project ``.gitignore``.
10. Inject the memory-first reminder block into ``CLAUDE.md`` if absent.
11. Launch a background thread to bootstrap/upgrade ``neuroloom-codeweaver``.
12. Print the Neuroloom tool catalog to stdout so Claude Code sees it in context.
13. Close the database in a ``finally`` block.

Design constraints
------------------
- stdlib only — no third-party imports.
- ``mypy --strict`` clean.
- All trace writes go through ``pyhooks.trace.write`` and are always non-fatal.
- HTTP calls use ``pyhooks.http.post_json``; network failures are silently skipped.
- The module never raises out of ``main()``; the ``__main__`` guard wraps it in
  a top-level try/except.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pyhooks.config as _config
import pyhooks.db as _db
import pyhooks.http as _http
import pyhooks.trace as _trace
import pyhooks.workspace_config as _workspace_config

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

# Set by the bootstrap thread when both install paths fail; read by main()
# to decide whether to print the degradation banner (Phase 4).
_codeweaver_install_failed: bool = False

# Plugin root: pyhooks/ -> plugin root
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SCRIPT = "pyhooks.session_start"

_SESSION_ID_RE = re.compile(r"^sess-[0-9]+-[a-f0-9]+$")

# Maximum rows kept in the traces table.
_TRACES_KEEP = 10_000

# Event buffer high-water mark and trim target.
_EVENT_BUFFER_MAX = 10_000
_EVENT_BUFFER_TRIM = 8_000

# Timeout (seconds) for the "end stale session" background thread join.
_END_SESSION_JOIN_TIMEOUT = 0.090

# Timeout (seconds) for the HTTP call inside the "end stale session" thread.
# The thread is daemon=False so it can outlive the join; give it a longer
# budget than the join timeout so the request actually has a chance to complete.
_END_SESSION_HTTP_TIMEOUT = 5.0

# Timeout (seconds) for the start-session API call.  Blocking (not threaded),
# so kept tight to avoid adding latency to hook startup.
_START_SESSION_TIMEOUT = 3.0

# Timeout (seconds) for the event-buffer flush API call.
_FLUSH_TIMEOUT = 5.0

# Max items per /documents/ingest/batch request — mirrors the API's
# DocumentIngestBatchRequest.documents max_length=50 /
# document_ingestion_service._MAX_BATCH_ITEMS. Keep in sync with
# api/neuroloom_api/schemas/document.py if that cap ever changes.
_DOCUMENT_BATCH_MAX = 50

# Valid SourceType enum values, mirrored from
# api/neuroloom_api/models/document_source.py. Kept as a plain set here so
# pyhooks does not import from the API package across the process boundary.
_DOCUMENT_SOURCE_TYPES = {
    "wiki",
    "slack",
    "document",
    "sdlc_knowledge",
    "sdlc_deliverable",
    "sdlc_chronicle",
}

# Timeout (seconds) for the PyPI version check.
_PYPI_TIMEOUT = 5.0

# Marker used to detect an already-injected CLAUDE.md block.
_CLAUDEMD_MARKER = "<!-- neuroloom:memory-first -->"

# Filename written next to the .venv directory after a successful install.
# Contains the installed neuroloom-codeweaver version so we can skip
# redundant pip-install calls when the version has not changed.
_CODEWEAVER_VERSION_MARKER = ".codeweaver-version"

# The setup-instructions text printed when no API key is configured and the
# MCP server is not reachable either.
_NO_KEY_MESSAGE = """\
[Neuroloom plugin] No API key configured.

To activate persistent memory, run:

  /plugins configure neuroloom

and enter your Neuroloom API key when prompted.

Don't have a key? Get one at https://app.neuroloom.dev/settings/api-keys

Restart your Claude Code session after configuring to activate memory.
"""

# Printed once per session start when workspace auto-configuration returns
# WORKSPACE_ID_AMBIGUOUS (D167 Phase 6 / F15) — the caller has 2+ workspace
# memberships and none can be auto-picked. Unlike a transient network
# failure, this will not resolve itself on retry, so it gets an explicit,
# actionable message pointing at the one manual-override mechanism that
# actually works: the per-project pluginConfigs.workspace_id option (NOT
# .mcp.json directly — .mcp.json's "neuroloom" entry is written by this
# module itself, from that same option, once validated; editing .mcp.json
# by hand bypasses the ownership/residue-migration machinery entirely).
#
# D169: this sentinel is confirmed unreachable via this module's
# Token-authenticated bootstrap call (see workspace_config.py's
# WORKSPACE_ID_AMBIGUOUS docstring — the bootstrap call resolves via a
# direct, non-nullable API-key-to-workspace join, structurally incapable of
# ambiguity). Retained as defensive dead code only; not given a new live
# trigger by this deliverable.
_AMBIGUOUS_WORKSPACE_MESSAGE = """\
[Neuroloom plugin] Workspace could not be auto-configured.

You belong to more than one Neuroloom workspace, so the plugin can't tell \
which one this project should use.

Set it manually in this project's .claude/settings.json:
  pluginConfigs["neuroloom@endless-galaxy-studios"].options.workspace_id = "<workspace-id>"

Find your workspace IDs at https://app.neuroloom.dev, then restart your \
Claude Code session to pick up the change.
"""

# D169 (F9) — printed once per session start when a validated, non-residue
# per-project override drives a successful literal X-Workspace-Id write.
# Log-only, no confirmation gate (CD decision). Emitted only on
# WriteResult.SUCCESS — never report an override as "applied" when the
# header wasn't actually written.
_OVERRIDE_APPLIED_MESSAGE = (
    "[Neuroloom plugin] Applied X-Workspace-Id override from this "
    "project's .claude/settings.json (workspace {workspace_id})."
)

# D169 (C1) — printed once per session start when a value found at the
# override key is recognized as this plugin's own prior auto-write (a
# released-version artifact matching this session's fingerprint-matched
# cached default), not a human's deliberate choice, and is deleted from
# .claude/settings.json. Distinct from _OVERRIDE_APPLIED_MESSAGE — the two
# are never emitted in the same call, since a migrated value is treated as
# "no override" for the rest of that call.
_RESIDUE_MIGRATED_MESSAGE = (
    "[Neuroloom plugin] Removed a stale workspace_id ({workspace_id}) from "
    "this project's .claude/settings.json — it was this plugin's own prior "
    "auto-write from an earlier version, not a workspace you configured. "
    "Set pluginConfigs[\"neuroloom@endless-galaxy-studios\"].options."
    "workspace_id yourself if you want this project pinned to a specific "
    "workspace."
)

# D169 (C6) — Branch C always ensures an owned, headerless baseline
# .mcp.json "neuroloom" entry exists for every project (the plugin's own
# shipped .mcp.json is emptied under this branch, so this is the project's
# *only* Neuroloom connection). If that baseline-ensure step itself doesn't
# succeed, the project ends up with no Neuroloom connection at all — a
# silent total failure unless surfaced, so (unlike a debug-only log) this
# is a user-visible warning naming the WriteResult and pointing at the file
# to inspect.
_CONNECTION_MISSING_WARNING = (
    "[Neuroloom plugin] Could not ensure a Neuroloom MCP connection for "
    "this project (result: {write_result}). Inspect this project's "
    ".mcp.json — the \"neuroloom\" entry may be malformed, or an existing "
    "entry under that name isn't one this plugin created (in which case "
    "the plugin will not modify it). Memory tools will not be available "
    "until this is resolved."
)

# D169 (review fix, following C6) — printed once per session start when a
# genuine (non-residue) per-project override IS configured in
# .claude/settings.json, but the literal X-Workspace-Id write into
# .mcp.json did not succeed. Distinct from _CONNECTION_MISSING_WARNING
# (which covers the *headerless baseline* ensure step, run only when no
# override is configured) — this covers the override-header write path,
# which previously only reached a debug-level logger.warning call
# (invisible: stderr is suppressed and this hook configures no logging
# handler). This realistically hits a user who follows the documented
# override instructions (skills/init/SKILL.md) on a project whose existing
# .mcp.json entry the plugin doesn't own.
#
# SKIPPED_UNMANAGED gets its own message because the remedy differs from
# FAILED/SKIPPED_CONFLICT: an unmanaged existing entry needs to be removed,
# renamed, or hand-edited by the user, whereas FAILED/SKIPPED_CONFLICT means
# the write itself couldn't complete or the file's shape didn't match what
# this plugin expects.
_OVERRIDE_WRITE_UNMANAGED_WARNING = (
    "[Neuroloom plugin] A workspace_id override is configured in this "
    "project's .claude/settings.json, but it could not be applied: this "
    "project's .mcp.json already has a \"neuroloom\" entry that this "
    "plugin did not create, so it will not be modified. Remove or rename "
    "that entry, or add the X-Workspace-Id header to it by hand, to apply "
    "the override."
)
_OVERRIDE_WRITE_FAILED_WARNING = (
    "[Neuroloom plugin] A workspace_id override is configured in this "
    "project's .claude/settings.json, but writing it into .mcp.json failed "
    "(result: {write_result}). Inspect this project's .mcp.json — it may "
    "be malformed, or its \"neuroloom\" entry may not match what this "
    "plugin expects. The override will not take effect until this is "
    "resolved."
)

# The CLAUDE.md injection block.
_CLAUDEMD_BLOCK = """\

<!-- neuroloom:memory-first -->
## Neuroloom Memory-First Rule

Neuroloom MCP is this project's canonical knowledge system. **You MUST query Neuroloom before falling back to code exploration.**

**`memory_search` — specific lookups.** "What's our pattern for X?", "Why did we choose Y?", "What do we know about Z?"
Call before: answering targeted questions, looking up a decision, checking a convention, dispatching subagents.

**`memory_explore` — understanding a topic area.** "How does our authentication work?", "Tell me about our integrations", "What's the full picture on search?"
Call when: you need the big picture on a subsystem, you want to see how decisions led to implementations, or you're preparing context for a complex change. Returns related memories AND the edges between them.

**When in doubt, use `memory_explore`** — more context is always better than less.

**Before editing any file, call `memory_by_file` first** to check for known gotchas and prior decisions.

**After solving a non-obvious problem or making a design decision, call `memory_store`** to capture it for future sessions.

**Do NOT use Neuroloom for:** general programming concepts (use training data), external library APIs (use Context7), simple file reads where you already know the path.

<!-- /neuroloom-memory-first -->
"""

# Banner printed to stdout (transcript-visible) when the codeweaver bootstrap
# failed.  stdout is intentional — Claude Code renders it as assistant-context
# text.  stderr is suppressed and would not surface to the user.
_CODEWEAVER_DEGRADED_BANNER = """\
<system-reminder>
[Neuroloom] Code graph sync is unavailable — neuroloom-codeweaver could not be installed automatically.
To enable code graph sync, run in a terminal:
  python3 -m pip install neuroloom-codeweaver
Verify with:
  python3 -c 'import codeweaver; print(codeweaver.__version__)'
Then restart your Claude Code session.
</system-reminder>"""

# The tool-catalog block printed at the end of a successful startup.
_TOOL_CATALOG = """\
<system-reminder>
### Neuroloom Tool Catalog

| Tool | Use when |
|------|----------|
| memory_search | **Always loaded** — specific lookups: "What's our pattern for X?", "Why did we choose Y?" |
| memory_explore | **Always loaded** — topic areas: "How does our auth work?", "Tell me about our integrations" — returns memories AND their relationships |
| memory_get_detail | Need the full narrative, relationships, and source files behind a search result |
| memory_get_timeline | Catching up on recent work — what was learned or decided in the last N days |
| memory_get_index | Browsing what knowledge exists — lightweight titles-only overview |
| memory_get_related | Following the thread — find conceptually connected memories |
| memory_by_file | About to edit a file — check for prior decisions and known gotchas |
| memory_store | Just solved a non-obvious problem, made a design decision, or discovered a pattern |
| memory_rate | A memory was helpful or outdated — feedback trains importance scoring |
| session_end | Wrapping up — summarize what was accomplished so future sessions have context |
| document_ingest | Import a doc, spec, or reference into the knowledge graph |

To use a deferred tool, call ToolSearch with its name first (e.g. `select:mcp__neuroloom__memory_store`).
</system-reminder>"""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _auth_headers(api_key: str) -> dict[str, str]:
    """Return the Authorization header dict for a Neuroloom API call."""
    return {"Authorization": f"Token {api_key}"}


def _git_branch() -> str:
    """Return the current git branch name, or 'unknown' on any failure."""
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True,
            text=True,
        )
        branch = result.stdout.strip()
        return branch if branch else "unknown"
    except Exception:
        return "unknown"


def _end_session_call(
    api_base: str,
    api_key: str,
    session_id: str,
) -> None:
    """Fire-and-forget POST to end a stale session.  Run in a background thread."""
    url = f"{api_base}/api/v1/sessions/{session_id}/end"
    _http.post_json(url, _auth_headers(api_key), b"{}", timeout=_END_SESSION_HTTP_TIMEOUT)


def _codeweaver_is_installed() -> bool:
    return importlib.util.find_spec("codeweaver") is not None


def _codeweaver_venv_dir(plugin_root: Path) -> Path:
    """Return the .venv directory path.

    Resolution order:
    1. ${CLAUDE_PLUGIN_DATA}/.venv  — persistent across plugin version bumps (CC v2.1.78+)
    2. plugin_root / ".venv"        — dev-mode fallback (no CLAUDE_PLUGIN_DATA set)
    """
    data_dir = os.environ.get("CLAUDE_PLUGIN_DATA")
    if data_dir:
        return Path(data_dir) / ".venv"
    return plugin_root / ".venv"


def _codeweaver_version_is_current(venv_dir: Path) -> bool:
    """Return True if the installed version matches the version marker file.

    The marker file lives at venv_dir.parent / _CODEWEAVER_VERSION_MARKER.
    If either the installed package or the marker file is missing, returns False
    so that a fresh install is triggered.
    """
    try:
        installed = importlib.metadata.version("neuroloom-codeweaver")
        marker_path = venv_dir.parent / _CODEWEAVER_VERSION_MARKER
        if not marker_path.exists():
            return False
        recorded = marker_path.read_text(encoding="utf-8").strip()
        return installed == recorded
    except Exception:
        return False


def _codeweaver_write_version_marker(venv_dir: Path) -> None:
    """Write the current neuroloom-codeweaver version to the marker file.

    The marker file lives at venv_dir.parent / _CODEWEAVER_VERSION_MARKER.
    Silently no-ops on any error — the marker is an optimisation, not required.
    """
    try:
        version = importlib.metadata.version("neuroloom-codeweaver")
        marker_path = venv_dir.parent / _CODEWEAVER_VERSION_MARKER
        marker_path.write_text(version + "\n", encoding="utf-8")
    except Exception:
        pass


def _codeweaver_ensure_installed(venv_dir: Path) -> bool:
    """Ensure neuroloom-codeweaver is importable; return True on success."""
    global _codeweaver_install_failed

    if os.environ.get("NEUROLOOM_CODEWEAVER_OFFLINE"):
        return _codeweaver_is_installed()

    if _codeweaver_is_installed() and _codeweaver_version_is_current(venv_dir):
        return True

    venv_py = venv_dir / (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    )

    # Path 2: create venv and pip-install into it
    try:
        import venv as _venv

        venv_dir.parent.mkdir(parents=True, exist_ok=True)
        if not venv_py.exists():
            _venv.EnvBuilder(with_pip=True).create(str(venv_dir))
        subprocess.run(
            [str(venv_py), "-m", "pip", "install", "neuroloom-codeweaver"],
            capture_output=True,
            timeout=120,
            check=True,
        )
        _codeweaver_write_version_marker(venv_dir)
        return True
    except Exception:
        pass  # ensurepip stripped on macOS system Python, or venv otherwise broken

    # Path 3: --user fallback
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--user", "neuroloom-codeweaver"],
            capture_output=True,
            timeout=120,
            check=True,
        )
        _codeweaver_write_version_marker(venv_dir)
        return True
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
        pass

    _codeweaver_install_failed = True
    return False


def _codeweaver_upgrade_if_stale(venv_dir: Path) -> None:
    try:
        current = importlib.metadata.version("neuroloom-codeweaver")
    except importlib.metadata.PackageNotFoundError:
        # guard — find_spec and metadata.version use different resolution paths;
        # a broken install can pass one and fail the other.
        return
    except Exception:
        return

    req = urllib.request.Request(
        "https://pypi.org/pypi/neuroloom-codeweaver/json",
        headers={"User-Agent": "neuroloom-plugin/0.1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=_PYPI_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            latest = data.get("info", {}).get("version", "")
    except Exception:
        return  # PyPI unreachable — skip silently

    def _parse_version(v: str) -> tuple[int, ...]:
        try:
            return tuple(int(x) for x in v.split("."))
        except (ValueError, AttributeError):
            return (0,)

    if _parse_version(latest) <= _parse_version(current):
        return

    pip_suffix = "Scripts/pip.exe" if sys.platform == "win32" else "bin/pip"
    pip_path = str(venv_dir / pip_suffix)
    try:
        subprocess.run(
            [pip_path, "install", "--upgrade", "neuroloom-codeweaver"],
            capture_output=True,
        )
        _codeweaver_write_version_marker(venv_dir)
    except Exception:
        pass


def _codeweaver_bootstrap_and_upgrade(plugin_root: Path) -> None:
    venv_dir = _codeweaver_venv_dir(plugin_root)
    installed = _codeweaver_ensure_installed(venv_dir)
    if installed and _codeweaver_is_installed():
        _codeweaver_upgrade_if_stale(venv_dir)


# ---------------------------------------------------------------------------
# Step implementations
# ---------------------------------------------------------------------------


def _end_stale_session(
    conn: sqlite3.Connection,
    api_base: str,
    api_key: str,
    workspace_key: str,
) -> None:
    """
    Step 4 — end any stale session row from the previous Claude Code run.

    The sessions table stores one row per workspace (project directory).  If a
    row exists it means the last session was not cleanly ended (e.g. the process
    was killed).  We validate the stored session_id, fire an async end call,
    then delete the row regardless of whether the API call succeeded.
    """
    row = conn.execute(
        "SELECT session_id FROM sessions WHERE session_key = ?",
        (workspace_key,),
    ).fetchone()

    if row is None:
        return

    session_id: str = row["session_id"]

    if not _SESSION_ID_RE.match(session_id):
        # Corrupt or tampered session_id — delete silently without calling the API.
        # Use a fixed sentinel instead of writing the raw session_id to the trace
        # table to avoid persisting potentially attacker-controlled data.
        conn.execute("DELETE FROM sessions WHERE session_key = ?", (workspace_key,))
        conn.commit()
        _trace.write(conn, _SCRIPT, "corrupt_session", detail="<invalid>")
        return

    # Fire the end call in a background thread with a short join timeout so
    # that a slow network never stalls session startup.
    t = threading.Thread(
        target=_end_session_call,
        args=(api_base, api_key, session_id),
        daemon=False,
    )
    t.start()
    t.join(timeout=_END_SESSION_JOIN_TIMEOUT)

    # Delete the row unconditionally — even if the HTTP call timed out we do
    # not want to leave a stale row behind.
    conn.execute("DELETE FROM sessions WHERE session_key = ?", (workspace_key,))
    conn.commit()


def _start_new_session(
    conn: sqlite3.Connection,
    api_base: str,
    api_key: str,
    workspace_key: str,
    cwd: str,
) -> str | None:
    """
    Step 5 — register a new session with the Neuroloom API.

    Returns the new session_id on success, or None if the API call failed.
    On success the session row is persisted to the DB so downstream hooks can
    reference it.
    """
    sid = f"sess-{int(time.time())}-{secrets.token_hex(4)}"
    branch = _git_branch()
    project_name = Path(cwd).name

    payload = json.dumps(
        {
            "session_id": sid,
            "project_name": project_name,
            "branch_name": branch,
        }
    ).encode("utf-8")

    url = f"{api_base}/api/v1/sessions/start"
    result = _http.post_json(url, _auth_headers(api_key), payload, timeout=_START_SESSION_TIMEOUT)

    if result is None or not (200 <= result[0] < 300):
        _trace.write(
            conn,
            _SCRIPT,
            "session_start_failed",
            detail=f"status={result[0] if result else 'network_error'}",
        )
        return None

    # Persist the new session so hooks that run later in this session can read it.
    conn.execute(
        """
        INSERT INTO sessions (session_key, session_id, started_at, last_submit_ms)
        VALUES (?, ?, datetime('now'), 0)
        ON CONFLICT(session_key) DO UPDATE SET
            session_id     = excluded.session_id,
            started_at     = excluded.started_at,
            last_submit_ms = 0
        """,
        (workspace_key, sid),
    )
    conn.commit()
    return sid


def _prune_traces(conn: sqlite3.Connection) -> None:
    """Step 6 — delete old trace rows, keeping the most recent _TRACES_KEEP entries."""
    conn.execute(
        "DELETE FROM traces WHERE id NOT IN ("
        "SELECT id FROM traces ORDER BY id DESC LIMIT " + str(_TRACES_KEEP) + ")"
    )
    conn.commit()


def _row_payload_kind(payload_type: str | None, parsed: dict[str, object]) -> str | None:
    """
    Classify a buffered row as ``"observation"``, ``"document"``, or ``None``
    (ambiguous — leave in place).

    Rows written by the current capture.py/post_tool_use.py always carry an
    explicit payload_type. Rows written before this marker existed have
    payload_type ``None`` and are classified by structural sniffing instead.
    """
    if payload_type == "observation":
        return "observation"
    if payload_type == "document":
        return "document"

    # Legacy row (payload_type IS NULL) — sniff structurally. Observation
    # payloads always carry these three keys (see capture.py's single_obs
    # dict); document payloads always carry source_type/source_path (see
    # sdlc_pyhooks/post_tool_use.py's payload_dict).
    if "observation_id" in parsed and "session_id" in parsed and "observed_at" in parsed:
        return "observation"
    if "source_type" in parsed and "source_path" in parsed:
        return "document"
    return None


def _document_row_is_valid(doc: dict[str, object]) -> bool:
    """
    Client-side shape check mirroring DocumentIngestRequest's required fields.

    Pydantic validates the entire ``documents`` list before the route
    handler's per-item SAVEPOINT loop runs, so a single row missing a required
    field would 422 the whole sub-batch. Checking shape here keeps a bad row
    from blocking delivery of its siblings. Covers only the three required
    fields (title, content, source_type) — optional fields are not shape
    checked, an accepted low-risk gap since post_tool_use.py only ever emits
    well-formed optional fields.
    """
    title = doc.get("title")
    content = doc.get("content")
    source_type = doc.get("source_type")
    return (
        isinstance(title, str)
        and title != ""
        and isinstance(content, str)
        and content != ""
        and source_type in _DOCUMENT_SOURCE_TYPES
    )


def _flush_observation_group(
    conn: sqlite3.Connection,
    api_base: str,
    api_key: str,
    row_ids: list[int],
    observations: list[object],
) -> None:
    """
    POST the observation group to /observations/batch — behavior unchanged
    from before payload_type existed. All-or-nothing deletion on 2xx;
    ObservationBatch's own item-level dedup handles duplicates safely on retry.
    """
    payload = json.dumps({"observations": observations}).encode("utf-8")
    url = f"{api_base}/api/v1/observations/batch"
    result = _http.post_json(url, _auth_headers(api_key), payload, timeout=_FLUSH_TIMEOUT)

    if result is not None and 200 <= result[0] < 300:
        placeholders = ",".join("?" * len(row_ids))
        conn.execute(
            "DELETE FROM event_buffer WHERE id IN (" + placeholders + ")",
            row_ids,
        )
        conn.commit()


def _flush_document_group(
    conn: sqlite3.Connection,
    api_base: str,
    api_key: str,
    row_ids: list[int],
    documents: list[dict[str, object]],
) -> None:
    """
    POST the document group to /documents/ingest/batch, chunked to
    _DOCUMENT_BATCH_MAX items (the API caps DocumentIngestBatchRequest.documents
    at 50). Only rows whose corresponding batch-response item is not "error"
    are deleted; "error" items remain buffered (bounded by the row-cap trim
    applied earlier in _flush_event_buffer). A chunk that fails outright
    (network error, non-2xx, or an unparseable response body) is left in the
    buffer entirely for the next flush cycle.
    """
    url = f"{api_base}/api/v1/documents/ingest/batch"

    for start in range(0, len(documents), _DOCUMENT_BATCH_MAX):
        chunk_ids = row_ids[start : start + _DOCUMENT_BATCH_MAX]
        chunk_docs = documents[start : start + _DOCUMENT_BATCH_MAX]

        payload = json.dumps({"documents": chunk_docs}).encode("utf-8")
        result = _http.post_json(url, _auth_headers(api_key), payload, timeout=_FLUSH_TIMEOUT)

        if result is None or not (200 <= result[0] < 300):
            continue

        try:
            body = json.loads(result[1].decode("utf-8"))
            results = body.get("results", [])
        except Exception:
            continue

        delete_ids = [
            chunk_ids[item["index"]]
            for item in results
            if isinstance(item, dict)
            and isinstance(item.get("index"), int)
            and 0 <= item["index"] < len(chunk_ids)
            and item.get("status") != "error"
        ]
        if delete_ids:
            placeholders = ",".join("?" * len(delete_ids))
            conn.execute(
                "DELETE FROM event_buffer WHERE id IN (" + placeholders + ")",
                delete_ids,
            )
            conn.commit()


def _flush_event_buffer(
    db_path: Path,
    api_base: str,
    api_key: str,
) -> None:
    """
    Step 7 — flush buffered observation and document events to the API.

    Opens its own SQLite connection so it is safe to run in a background thread
    (SQLite connections must not be shared across threads).

    If the buffer has grown past _EVENT_BUFFER_MAX rows, trim it to
    _EVENT_BUFFER_TRIM rows first (dropping the oldest entries) to prevent
    unbounded growth when the API is persistently unavailable.

    Rows are partitioned by payload_type (observation vs. document, with
    structural sniffing for legacy NULL rows) and routed to their own
    endpoint — a buffered document row previously failed Pydantic validation
    for the entire /observations/batch payload, stranding every legitimate
    buffered observation alongside it. Rows that can't be classified, or that
    fail the document shape check, are left in the buffer and logged rather
    than dropped or allowed to poison the whole flush.
    """
    conn: sqlite3.Connection | None = None
    try:
        conn = _db.open_db(db_path)
        if conn is None:
            return

        count: int = conn.execute("SELECT COUNT(*) FROM event_buffer").fetchone()[0]

        if count > _EVENT_BUFFER_MAX:
            # Delete the oldest rows, keeping only the most recent _EVENT_BUFFER_TRIM.
            conn.execute(
                "DELETE FROM event_buffer WHERE id NOT IN ("
                "SELECT id FROM event_buffer ORDER BY id DESC LIMIT " + str(_EVENT_BUFFER_TRIM) + ")"
            )
            conn.commit()

        rows = conn.execute(
            "SELECT id, payload, payload_type FROM event_buffer ORDER BY id ASC"
        ).fetchall()

        if not rows:
            return

        observation_ids: list[int] = []
        observations: list[object] = []
        document_ids: list[int] = []
        documents: list[dict[str, object]] = []

        for row in rows:
            row_id = int(row["id"])
            try:
                parsed = json.loads(row["payload"])
            except Exception:
                _trace.write(conn, _SCRIPT, "buffer_row_unparseable", detail=f"id={row_id}")
                continue
            if not isinstance(parsed, dict):
                _trace.write(conn, _SCRIPT, "buffer_row_unparseable", detail=f"id={row_id}")
                continue

            kind = _row_payload_kind(row["payload_type"], parsed)
            if kind == "observation":
                observation_ids.append(row_id)
                observations.append(parsed)
            elif kind == "document":
                if _document_row_is_valid(parsed):
                    document_ids.append(row_id)
                    documents.append(parsed)
                else:
                    _trace.write(conn, _SCRIPT, "buffer_row_invalid_document", detail=f"id={row_id}")
            else:
                _trace.write(conn, _SCRIPT, "buffer_row_ambiguous", detail=f"id={row_id}")

        if observations:
            _flush_observation_group(conn, api_base, api_key, observation_ids, observations)

        if documents:
            _flush_document_group(conn, api_base, api_key, document_ids, documents)
    except Exception:
        pass  # never crash — hook design constraint
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _ensure_gitignore(project_root: str) -> None:
    """
    Step 8 — add ``.neuroloom.db`` to ``.gitignore`` if not already present.

    Idempotent: if the entry already exists (even inside a comment or with
    surrounding whitespace) we leave the file untouched.
    """
    gitignore_path = Path(project_root) / ".gitignore"

    entry = ".neuroloom.db"

    if gitignore_path.exists():
        content = gitignore_path.read_text(encoding="utf-8")
        # Check line-by-line so we match the exact entry, not a substring.
        lines = [line.strip() for line in content.splitlines()]
        if entry in lines:
            return
        # Append with a trailing newline.
        with gitignore_path.open("a", encoding="utf-8") as fh:
            if content and not content.endswith("\n"):
                fh.write("\n")
            fh.write(f"{entry}\n")
    else:
        # Create a minimal .gitignore.
        gitignore_path.write_text(f"{entry}\n", encoding="utf-8")


def _inject_claudemd(project_root: str) -> None:
    """
    Step 9 — append the memory-first block to ``CLAUDE.md`` if absent.

    No-op if CLAUDE.md does not exist or if the marker is already present.
    """
    claudemd_path = Path(project_root) / "CLAUDE.md"

    if not claudemd_path.exists():
        return

    content = claudemd_path.read_text(encoding="utf-8")
    if _CLAUDEMD_MARKER in content:
        return

    with claudemd_path.open("a", encoding="utf-8") as fh:
        fh.write(_CLAUDEMD_BLOCK)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """
    Run the SessionStart hook.

    Follows the 13-step sequence documented in the module docstring.  The
    database connection is always closed in a ``finally`` block even if an
    unexpected exception occurs mid-way through.
    """
    # Step 1 — load config.
    cfg = _config.load()

    # Step 2 — open DB.
    conn = _db.open_db(cfg.state_db_path)

    try:
        cwd = str(Path(os.getcwd()).resolve())
        workspace_key = cwd

        # Step 3 — guard: no API key configured.
        if not cfg.api_key:
            print(_NO_KEY_MESSAGE, end="")
            _trace.write(conn, _SCRIPT, "no_api_key")
            return

        # Step 4 — end stale session.
        if conn is not None:
            _end_stale_session(conn, cfg.api_base, cfg.api_key, workspace_key)

        # Step 5 — start new session.
        session_id: str | None = None
        if conn is not None:
            session_id = _start_new_session(conn, cfg.api_base, cfg.api_key, workspace_key, cwd)

        # Step 6 — prune traces.
        if conn is not None:
            _prune_traces(conn)

        # Step 7 — flush event buffer.
        # Flush buffered observations in background — thread outlives the 90 ms join
        # so large batches don't block session startup.
        flush_thread = threading.Thread(
            target=_flush_event_buffer,
            args=(cfg.state_db_path, cfg.api_base, cfg.api_key),
            daemon=False,
        )
        flush_thread.start()
        flush_thread.join(timeout=0.090)

        # Step 8 — workspace routing configuration (D169).
        # Reads a manual, human-provided per-project override from
        # .claude/settings.json (migrating away any residue left by a
        # released version's own past auto-write), and ensures a
        # project-scope .mcp.json "neuroloom" entry this plugin can prove
        # it owns. A literal X-Workspace-Id header is written only when a
        # genuine override is configured; otherwise the entry is left
        # headerless (ADR-13 auto-resolution applies). Non-fatal — a
        # failure here does not block the session. Skip when there is no
        # API key only for the bootstrap-fetch sub-step (guard already
        # returned above for the whole hook) — the override read and both
        # literal-writer calls run unconditionally (C3).
        #
        # WORKSPACE_ID_AMBIGUOUS is the one outcome that is not transient —
        # a multi-membership caller with no configured workspace_id won't
        # resolve itself on the next session start, so (only) this case
        # gets a user-facing message. Plain None (network/unreachable/
        # unrecognized-error-shape) stays silent, matching prior behavior.
        workspace_config_result = _workspace_config.ensure_workspace_configured_detailed(
            project_root=cwd,
            db_path=cfg.state_db_path,
            api_base=cfg.api_base,
            api_key=cfg.api_key,
        )
        workspace_result = workspace_config_result.workspace_id
        if workspace_result == _workspace_config.WORKSPACE_ID_AMBIGUOUS:
            print(_AMBIGUOUS_WORKSPACE_MESSAGE, end="")
            _trace.write(conn, _SCRIPT, "workspace_ambiguous")

        # F9 — override-applied visibility log. Never conflated with the
        # residue-migration log below: an override that was migrated-away
        # residue is treated as "no override" and cannot also be "applied"
        # in the same call.
        if workspace_config_result.override_applied:
            print(
                _OVERRIDE_APPLIED_MESSAGE.format(workspace_id=workspace_config_result.workspace_id),
                end="\n",
            )
            _trace.write(conn, _SCRIPT, "workspace_override_applied")

        # C1 — residue-migration visibility log.
        if workspace_config_result.migrated_residue_value is not None:
            print(
                _RESIDUE_MIGRATED_MESSAGE.format(
                    workspace_id=workspace_config_result.migrated_residue_value
                ),
                end="\n",
            )
            _trace.write(conn, _SCRIPT, "workspace_residue_migrated")

        # Review fix (following C6) — override-configured-but-not-applied
        # warning. Only fires when a genuine override was present (i.e. the
        # override write path ran) and its literal-header write didn't
        # succeed. Mutually exclusive with the C6 warning below: exactly
        # one of override_write_result / baseline_write_result is populated
        # per call, since only one of the two write branches ever runs.
        override_write_result = workspace_config_result.override_write_result
        if (
            override_write_result is not None
            and override_write_result != _workspace_config.WriteResult.SUCCESS
        ):
            if override_write_result is _workspace_config.WriteResult.SKIPPED_UNMANAGED:
                print(_OVERRIDE_WRITE_UNMANAGED_WARNING, end="\n")
            else:
                print(
                    _OVERRIDE_WRITE_FAILED_WARNING.format(
                        write_result=override_write_result.value
                    ),
                    end="\n",
                )
            _trace.write(conn, _SCRIPT, "workspace_override_write_failed")

        # C6 — Branch C connection-missing warning. Only fires when the
        # headerless baseline-ensure step ran (i.e. no override applied)
        # and didn't succeed.
        baseline_result = workspace_config_result.baseline_write_result
        if baseline_result is not None and baseline_result != _workspace_config.WriteResult.SUCCESS:
            print(
                _CONNECTION_MISSING_WARNING.format(write_result=baseline_result.value),
                end="\n",
            )
            _trace.write(conn, _SCRIPT, "workspace_connection_missing")

        # Step 9 — .gitignore management.
        _ensure_gitignore(cwd)

        # Step 10 — CLAUDE.md injection.
        _inject_claudemd(cwd)

        # Step 11 — bootstrap/upgrade codeweaver (background thread with short join).
        updater = threading.Thread(
            target=_codeweaver_bootstrap_and_upgrade,
            args=(_PLUGIN_ROOT,),
            daemon=False,
        )
        updater.start()
        # Join with a short timeout so that a slow PyPI/install (up to 120 s)
        # does not stall the rest of startup.  If the thread is still running
        # after 90 ms we proceed; the non-daemon thread will complete in the
        # background before the process exits.
        updater.join(timeout=0.090)

        # Print degradation banner if the bootstrap thread set the failure flag.
        # Best-effort racy read of a bool — acceptable under CPython's GIL.
        # OFFLINE mode never sets the flag, so the banner is correctly suppressed.
        if _codeweaver_install_failed:
            print(_CODEWEAVER_DEGRADED_BANNER)

        # Step 12 — print tool catalog.
        print(_TOOL_CATALOG)

        _trace.write(conn, _SCRIPT, "started", session_id=session_id)

    finally:
        # Step 13 — close DB.
        if conn is not None:
            conn.close()


# ---------------------------------------------------------------------------
# Module entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Top-level guard: hooks must never crash Claude Code.  Any unhandled
        # exception is silently swallowed here.
        pass
