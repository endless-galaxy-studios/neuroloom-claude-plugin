"""
CwdChanged cache pre-warmer for neuroloom.

Fires when Claude executes a ``cd`` command (CC v2.1.83+).  Warms the
nudge-mode context cache for the new working directory so that subsequent
Glob/Grep nudge calls hit the cache instead of blocking on a cold API
round-trip.

Scope: warms nudge-mode cache entries only (directory-level queries).
Read-mode (per-file) warming is out of scope — too speculative and expensive
to enumerate files without knowing which ones Claude will read.

Design constraints
------------------
- stdlib only — zero pip dependencies.
- Exit in < 100 ms (daemon=False background thread, join timeout 90 ms).
- Debounce: skip if last ``prefetch_fired`` trace < 2 s ago for this workspace.
- Cache key matches ``pyhooks.preload_context._cache_key_nudge`` exactly:
    workspace_root = str(Path(os.getcwd()).resolve())  — same as preload_context.py
    query         = event["cwd"]                       — new directory from hook JSON
- Only warms a directory inside the project root (see Containment below).
- Always exits 0 — CwdChanged exit code is ignored by CC.

Cache key parameters (IMPORTANT)
---------------------------------
``query`` is the event's ``cwd`` field (the new directory Claude navigated
to).  ``workspace_root`` is the *process* cwd resolved to an absolute path.
Claude Code spawns hooks in its *current* directory and updates that directory
before CwdChanged hooks fire, so after a ``cd`` the process cwd IS the new
directory and ``workspace_root == query``.  Where the cache key and
``.neuroloom.db`` should live is therefore an open question that is
deliberately NOT decided here (it belongs with the base-plugin CI-mode work,
D178d); this module keeps using the process cwd so its keys keep matching
``preload_context.py``'s.

Containment
-----------
The project root for containment is ``CLAUDE_PROJECT_DIR`` (stable across
``cd``), falling back to the process cwd.  The fallback is weak for every hook
that uses ``pyhooks.containment`` (``preload_context`` and ``capture`` as well
as this one), and starkest here: with ``CLAUDE_PROJECT_DIR`` unset the root and
the ``cd`` target are the same directory, so the check passes trivially and the
only thing protecting the upload is that no ``.neuroloom.db`` (hence no API
key) exists in the new directory.  Claude Code sets ``CLAUDE_PROJECT_DIR`` for
hooks, so the fallback matters mainly for tests and older versions; the hook
traces ``project_root_fallback_cwd`` when it takes it.

The project root must also be the directory the API key was read from
(``containment.credential_matches_root``); after a ``cd`` into a nested project
that has its own ``.neuroloom.db`` the hook exits without sending.
"""

import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pyhooks.containment as containment
import pyhooks.trace as trace
from pyhooks.config import Config, load
from pyhooks.db import open_db

_SCRIPT = "pyhooks.cache_prefetch"

# Minimum gap between consecutive prefetch_fired events for the same workspace.
_DEBOUNCE_SECONDS = 2.0

# HTTP timeout for the background API call (seconds).  The background thread
# is daemon=False and may outlive the 90 ms join — this bounds the network
# call itself, not the hook latency.
_API_TIMEOUT = 5.0


# ---------------------------------------------------------------------------
# Cache helpers — inlined from preload_context.py to keep this module
# self-contained.  The key algorithm MUST stay in sync with that module.
# ---------------------------------------------------------------------------


def _cache_key_nudge(query: str, workspace_root: str) -> str:
    """Compute a stable nudge cache key that matches pyhooks.preload_context.

    The null-byte separator prevents boundary-shift collisions (e.g. the pair
    ("a", "bc") must hash differently from ("ab", "c")).  The "\\x00nudge"
    suffix namespaces nudge entries away from inject (per-file) entries so a
    directory path that coincidentally matches a file path cannot collide.
    """
    raw = query + "\x00" + workspace_root + "\x00nudge"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_set(conn: sqlite3.Connection, key: str, context: str) -> None:
    """Insert or replace a cache entry with the current epoch timestamp."""
    try:
        conn.execute(
            "INSERT OR REPLACE INTO cache (cache_key, context, created_at) VALUES (?, ?, ?)",
            (key, context, time.time()),
        )
        conn.commit()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------


def _prefetch(
    db_path: Path,
    api_base: str,
    api_key: str,
    new_cwd: str,
    workspace_root: str,
) -> None:
    """
    Call the Neuroloom API for nudge context and write the result to the cache.

    Runs in a background thread — must open its own DB connection and close it
    in a ``finally`` block.  Must never propagate exceptions.
    """
    conn: sqlite3.Connection | None = None
    try:
        conn = open_db(db_path)

        url = api_base.rstrip("/") + "/api/v1/context"
        payload = json.dumps({"query": new_cwd, "format": "nudge"}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Token {api_key}",
                "User-Agent": "neuroloom",
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
            resp_data = json.loads(resp.read().decode("utf-8"))
            text: str = resp_data.get("nudge_text", "") or ""

        # Write to cache only when the API returned non-empty text.  An empty
        # cache entry would suppress the real nudge for up to the TTL window
        # (bug parity with D83 fix in preload_context.py).
        if text and conn is not None:
            key = _cache_key_nudge(new_cwd, workspace_root)
            _cache_set(conn, key, text)

        trace.write(conn, _SCRIPT, "prefetch_fired", detail=new_cwd)

    except Exception:
        # Network failure, HTTP error, JSON parse error, DB error — all
        # degrade silently.  Trace if we have a connection.
        if conn is not None:
            try:
                trace.write(conn, _SCRIPT, "prefetch_failed", detail=new_cwd)
            except Exception:
                pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Pre-warm the nudge cache for the directory Claude just navigated to."""
    # ------------------------------------------------------------------ 1
    cfg: Config = load()

    # ------------------------------------------------------------------ 2
    # No API key → nothing useful to do; skip even opening the DB.
    if not cfg.api_key:
        sys.exit(0)

    # ------------------------------------------------------------------ 3
    # Parse the CwdChanged event from stdin.
    try:
        raw_stdin = sys.stdin.read()
        data: dict[str, object] = json.loads(raw_stdin) if raw_stdin.strip() else {}
    except Exception:
        data = {}

    # ------------------------------------------------------------------ 4
    # Guard: hook event must carry a ``cwd`` field (the new directory).
    new_cwd = data.get("cwd")
    if not isinstance(new_cwd, str) or not new_cwd:
        sys.exit(0)

    # ------------------------------------------------------------------ 5
    # workspace_root is the hook process's cwd.  Claude Code runs hooks in its
    # *current* directory and has already applied the ``cd`` by the time this
    # hook fires, so this is typically the NEW directory, not the project root.
    # It stays the cache-key component so keys match preload_context.py; it is
    # not used for containment (below).
    workspace_root = str(Path(os.getcwd()).resolve())

    # ------------------------------------------------------------------ 5b
    # Only warm a directory inside the project root.  The directory is sent to
    # the API as a query, so a ``cd`` to ~/.ssh or a sibling repo must not
    # leave the machine.  The root is CLAUDE_PROJECT_DIR, not workspace_root:
    # comparing the new directory with the process cwd compares it with itself.
    # Exit before opening the DB: no API call, no cache write, no I/O.
    project_root = containment.project_root()
    if not containment.within_root(new_cwd, project_root):
        sys.exit(0)

    # A ``cd`` target is an existing directory by definition.  One that cannot be
    # resolved to one -- a symlink loop, a NUL byte -- is a malformed event: skip.
    # (``within_root`` alone is not enough: on newer Pythons it resolves a loop
    # under the project root to an "inside" path instead of raising.)
    if containment.session_cwd(data) is None:
        sys.exit(0)

    # The project root must also be the directory the API key came from: after
    # a `cd` into a nested project with its own .neuroloom.db, the key would
    # belong to a different workspace than this project's.  Same silent,
    # zero-I/O exit.  See pyhooks/containment.py.
    if not containment.credential_matches_root(cfg.state_db_path, project_root):
        sys.exit(0)

    # ------------------------------------------------------------------ 6
    conn = open_db(cfg.state_db_path)

    try:
        # With CLAUDE_PROJECT_DIR unset the root is the process cwd, which
        # follows `cd` -- containment is then only as strong as that cwd.
        if containment.project_root_is_fallback():
            trace.write(conn, _SCRIPT, "project_root_fallback_cwd")

        # -------------------------------------------------------------- 7
        # Debounce: check whether a prefetch_fired trace for this workspace
        # was written within the last _DEBOUNCE_SECONDS.  Uses the traces
        # table (already present in the shared schema) — no new table needed.
        if conn is not None:
            row = conn.execute(
                "SELECT ts FROM traces WHERE script = ? AND decision = ? ORDER BY id DESC LIMIT 1",
                (_SCRIPT, "prefetch_fired"),
            ).fetchone()
            if row is not None:
                try:
                    last_ts = datetime.fromisoformat(str(row[0]))
                    now = datetime.now(timezone.utc)
                    if (now - last_ts).total_seconds() < _DEBOUNCE_SECONDS:
                        trace.write(conn, _SCRIPT, "debounced", detail=new_cwd)
                        sys.exit(0)
                except Exception:
                    # Timestamp parse failure — proceed without debouncing.
                    pass

        # -------------------------------------------------------------- 8
        # Spawn the background thread.  daemon=False so the thread can
        # outlive the main thread if the 90 ms join times out — we prefer a
        # slightly late cache write over silently losing the prefetch.
        t = threading.Thread(
            target=_prefetch,
            args=(
                cfg.state_db_path,
                cfg.api_base,
                cfg.api_key,
                new_cwd,
                workspace_root,
            ),
            daemon=False,
        )
        t.start()
        t.join(timeout=0.090)

    finally:
        # -------------------------------------------------------------- 9
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # Crash safety: hooks must never surface tracebacks to the Claude Code
        # process.  Silently exit with 0 so the directory change completes.
        sys.exit(0)
