"""
PostToolUse hook: code graph sync.

Invoked after Write/Edit tool calls.  Reads the file path from stdin, queues
it in the debounce table, and — if the debounce window has elapsed — fires a
background thread to parse the file and POST structural metadata to the
Neuroloom code-graph API.

Design constraints
------------------
- Must return in under 100 ms.  The background thread does the actual network
  call; the main path only commits debounce records and spawns the thread.
- Never raises.  All failures are traced and swallowed so Claude Code is never
  blocked by a hook crash.
- Uses stdlib only (except the optional ``codeweaver`` import in the sync
  function, which is guarded by a try/except).
- ``BEGIN IMMEDIATE`` for the drain step — not ``with conn:`` — so the
  write lock is acquired up front and no other process can slip in a
  ``DELETE`` between our ``SELECT`` and our own ``DELETE``.

Environment variables
---------------------
NEUROLOOM_CODE_GRAPH_SYNC
    Set to ``"0"`` to disable this hook entirely.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pyhooks.config as _config
import pyhooks.db as _db
import pyhooks.trace as _trace

_SCRIPT = "pyhooks.code_graph_sync"
_ALLOWED_EXTENSIONS = {".ts", ".tsx", ".mts", ".py"}


# ---------------------------------------------------------------------------
# Workspace containment helper
# ---------------------------------------------------------------------------


def _within_workspace(file_path: str, workspace_root: Path) -> bool:
    """Return True only if *file_path* resolves to a path inside *workspace_root*.

    Uses ``Path.is_relative_to()`` rather than ``str.startswith()`` to prevent
    path-traversal bypasses such as ``/workspace-evil/file.py`` matching
    ``/workspace``.
    """
    try:
        return Path(file_path).resolve().is_relative_to(workspace_root)
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Atomic drain of debounce_files
# ---------------------------------------------------------------------------


def _drain_debounce_files(conn: sqlite3.Connection, workspace_key: str) -> list[str]:
    """Remove and return all queued file paths for *workspace_key*.

    Uses ``BEGIN IMMEDIATE`` so the write lock is acquired before the
    ``SELECT``, preventing a race where two concurrent threads both read the
    same rows and both attempt to delete them.  ``with conn:`` (deferred
    begin) is intentionally avoided for this reason.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute(
            "SELECT file_path FROM debounce_files WHERE workspace_key = ?",
            (workspace_key,),
        ).fetchall()
        conn.execute(
            "DELETE FROM debounce_files WHERE workspace_key = ?",
            (workspace_key,),
        )
        conn.execute("COMMIT")
        return [r[0] if isinstance(r, tuple) else r["file_path"] for r in rows]
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        return []


# ---------------------------------------------------------------------------
# Codeweaver sync
# ---------------------------------------------------------------------------


def _run_codeweaver_sync(
    paths: list[Path],
    workspace_root: Path,
    api_base: str,
    api_key: str,
) -> int:
    """Parse *paths* with codeweaver and POST the result to the code-graph API.

    Returns
    -------
    0
        Success.
    42
        HTTP 429 Too Many Requests — signals the caller to increase backoff.
    negative int
        Failure: ``-1`` for network/timeout errors, ``-status_code`` for
        HTTP errors (e.g. ``-400``, ``-500``).
    """
    try:
        from codeweaver import parse_files  # type: ignore[import-untyped,unused-ignore]
    except ImportError:
        return 0  # codeweaver not installed — graceful degradation

    try:
        sync_data: dict[str, Any] = parse_files(paths, workspace_root)
    except Exception:
        return 0

    url = f"{api_base.rstrip('/')}/api/v1/code-graph/sync"
    payload = json.dumps(sync_data).encode("utf-8")

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

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            _ = resp.read()
        return 0
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            return 42
        return -exc.code  # e.g., -400, -500 — distinguishable from success
    except Exception:
        return -1  # network/timeout failure


# ---------------------------------------------------------------------------
# Background sync thread
# ---------------------------------------------------------------------------


def _background_sync(
    workspace_key: str,
    workspace_root: Path,
    backoff_ms: int,
    db_path: Path,
    api_base: str,
    api_key: str,
) -> None:
    """Drain the debounce queue and push a code-graph sync.

    Opened with its own DB connection — background threads must never share a
    connection with the main thread (SQLite connections are not thread-safe).
    """
    bg_conn: sqlite3.Connection | None = None
    try:
        time.sleep(backoff_ms / 1000)

        bg_conn = _db.open_db(db_path)
        if bg_conn is None:
            return

        file_paths = _drain_debounce_files(bg_conn, workspace_key)
        if not file_paths:
            return

        # Deduplicate while preserving order
        seen: set[str] = set()
        unique_paths: list[Path] = []
        for fp in file_paths:
            if fp not in seen:
                seen.add(fp)
                unique_paths.append(Path(fp))

        exit_code = _run_codeweaver_sync(unique_paths, workspace_root, api_base, api_key)

        now_ms = int(time.time() * 1000)
        if exit_code == 42:
            bg_conn.execute(
                "UPDATE debounce SET backoff_ms = MIN(backoff_ms * 2, 60000) WHERE workspace_key = ?",
                (workspace_key,),
            )
            bg_conn.commit()
            _trace.write(bg_conn, _SCRIPT, "sync_rate_limited", detail="backoff_doubled")
        elif exit_code < 0:
            _trace.write(
                bg_conn, _SCRIPT, "sync_failed",
                detail=f"files={len(unique_paths)} exit_code={exit_code}",
            )
        else:
            bg_conn.execute(
                "UPDATE debounce SET backoff_ms = MAX(backoff_ms / 2, 2000), last_sync_ms = ? WHERE workspace_key = ?",
                (now_ms, workspace_key),
            )
            bg_conn.commit()
            _trace.write(bg_conn, _SCRIPT, "sync_completed", detail=f"files={len(unique_paths)}")

    except Exception:
        _trace.write(bg_conn, _SCRIPT, "sync_error")
    finally:
        if bg_conn is not None:
            try:
                bg_conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """PostToolUse hook: queue the edited file and trigger a debounced sync."""

    # 1. Opt-out check
    if os.environ.get("NEUROLOOM_CODE_GRAPH_SYNC") == "0":
        sys.exit(0)

    # 2. Load config
    config = _config.load()
    if not config.api_key:
        sys.exit(0)

    # 3. Read and parse stdin
    try:
        raw = sys.stdin.read()
        event = json.loads(raw)
        tool_input: dict[str, Any] = event.get("tool_input", {})
        file_path_str: str = str(tool_input.get("file_path", ""))
    except Exception:
        sys.exit(0)

    if not file_path_str:
        sys.exit(0)

    # 4. Extension filter
    suffix = Path(file_path_str).suffix.lower()
    if suffix not in _ALLOWED_EXTENSIONS:
        # Open DB only for tracing if we can — but on fast exit, skip it.
        sys.exit(0)

    # 5. Workspace containment
    workspace_root = Path(os.getcwd()).resolve()
    if not _within_workspace(file_path_str, workspace_root):
        sys.exit(0)

    # 6. Open DB
    conn = _db.open_db(config.state_db_path)
    if conn is None:
        sys.exit(0)

    try:
        # 7. Debounce key: SHA-256 of workspace root, first 16 hex chars
        workspace_key = hashlib.sha256(str(workspace_root).encode()).hexdigest()[:16]

        now_ms = int(time.time() * 1000)

        # 8. Insert debounce records
        conn.execute(
            "INSERT OR IGNORE INTO debounce (workspace_key, last_sync_ms, backoff_ms) VALUES (?, 0, 2000)",
            (workspace_key,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO debounce_files (workspace_key, file_path) VALUES (?, ?)",
            (workspace_key, str(Path(file_path_str).resolve())),
        )
        conn.commit()

        # 9. Debounce check
        row = conn.execute(
            "SELECT last_sync_ms, backoff_ms FROM debounce WHERE workspace_key = ?",
            (workspace_key,),
        ).fetchone()

        if row is None:
            sys.exit(0)

        last_sync_ms: int = row[0] if isinstance(row, tuple) else row["last_sync_ms"]
        backoff_ms: int = row[1] if isinstance(row, tuple) else row["backoff_ms"]

        if now_ms - last_sync_ms < backoff_ms:
            _trace.write(conn, _SCRIPT, "debounced", detail=file_path_str)
            sys.exit(0)

        # 10. Update last_sync_ms
        conn.execute(
            "UPDATE debounce SET last_sync_ms = ? WHERE workspace_key = ?",
            (now_ms, workspace_key),
        )
        conn.commit()

        # 11. Trace
        _trace.write(conn, _SCRIPT, "sync_dispatched", detail=file_path_str)

        # 12. Background thread
        t = threading.Thread(
            target=_background_sync,
            args=(
                workspace_key,
                workspace_root,
                backoff_ms,
                config.state_db_path,
                config.api_base,
                config.api_key,
            ),
            daemon=False,
        )
        t.start()
        t.join(timeout=0.090)

    finally:
        # 13. Close main DB
        try:
            conn.close()
        except Exception:
            pass

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)
