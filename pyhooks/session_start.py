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
8. Ensure ``.neuroloom.db`` is listed in the project ``.gitignore``.
9. Inject the memory-first reminder block into ``CLAUDE.md`` if absent.
10. Launch a background thread to bootstrap/upgrade ``neuroloom-codeweaver``.
11. Print the Neuroloom tool catalog to stdout so Claude Code sees it in context.
12. Close the database in a ``finally`` block.

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

# Timeout (seconds) for the PyPI version check.
_PYPI_TIMEOUT = 5.0

# Marker used to detect an already-injected CLAUDE.md block.
_CLAUDEMD_MARKER = "<!-- neuroloom:memory-first -->"

# Filename written next to the .venv directory after a successful install.
# Contains the installed neuroloom-codeweaver version so we can skip
# redundant pip-install calls when the version has not changed.
_CODEWEAVER_VERSION_MARKER = ".codeweaver-version"

# The setup-instructions text printed when no API key is configured.
_NO_KEY_MESSAGE = """\
[Neuroloom plugin] No API key configured.

To activate persistent memory, run:

  /plugins configure neuroloom

and enter your Neuroloom API key when prompted.

Don't have a key? Get one at https://app.neuroloom.dev/settings/api-keys

Restart your Claude Code session after configuring to activate memory.
"""

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


def _flush_event_buffer(
    db_path: Path,
    api_base: str,
    api_key: str,
) -> None:
    """
    Step 7 — flush buffered observation events to the API.

    Opens its own SQLite connection so it is safe to run in a background thread
    (SQLite connections must not be shared across threads).

    If the buffer has grown past _EVENT_BUFFER_MAX rows, trim it to
    _EVENT_BUFFER_TRIM rows first (dropping the oldest entries) to prevent
    unbounded growth when the API is persistently unavailable.

    Rows are sent in a single batch POST.  On success they are deleted from the
    buffer.  On failure they are left in place for the next startup to retry.
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

        rows = conn.execute("SELECT id, payload FROM event_buffer ORDER BY id ASC").fetchall()

        if not rows:
            return

        observations: list[object] = []
        row_ids: list[int] = []

        for row in rows:
            row_ids.append(row["id"])
            try:
                observations.append(json.loads(row["payload"]))
            except Exception:
                # Malformed payload — skip it but still delete the row on success
                # so it does not block future flushes.
                observations.append({"raw": row["payload"]})

        payload = json.dumps({"observations": observations}).encode("utf-8")
        url = f"{api_base}/api/v1/observations/batch"
        result = _http.post_json(url, _auth_headers(api_key), payload, timeout=_FLUSH_TIMEOUT)

        if result is not None and 200 <= result[0] < 300:
            # Delete only the rows we successfully sent.
            placeholders = ",".join("?" * len(row_ids))
            conn.execute(
                "DELETE FROM event_buffer WHERE id IN (" + placeholders + ")",
                row_ids,
            )
            conn.commit()
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

    Follows the 12-step sequence documented in the module docstring.  The
    database connection is always closed in a ``finally`` block even if an
    unexpected exception occurs mid-way through.
    """
    # Step 1 — load config.
    cfg = _config.load()

    # Step 2 — open DB.
    conn = _db.open_db(cfg.state_db_path)

    try:
        # Step 3 — guard: no API key configured.
        if not cfg.api_key:
            print(_NO_KEY_MESSAGE, end="")
            _trace.write(conn, _SCRIPT, "no_api_key")
            return

        cwd = str(Path(os.getcwd()).resolve())
        workspace_key = cwd

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

        # Step 8 — .gitignore management.
        _ensure_gitignore(cwd)

        # Step 9 — CLAUDE.md injection.
        _inject_claudemd(cwd)

        # Step 10 — bootstrap/upgrade codeweaver (background thread with short join).
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

        # Step 11 — print tool catalog.
        print(_TOOL_CATALOG)

        _trace.write(conn, _SCRIPT, "started", session_id=session_id)

    finally:
        # Step 12 — close DB.
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
