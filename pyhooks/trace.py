"""
Structured trace writer for neuroloom hook modules.

Every hook decision is recorded in the ``traces`` SQLite table for post-hoc
debugging and audit.  Think of traces as a flight recorder — they capture what
each hook decided and why, so that when something behaves unexpectedly you can
replay the decision log instead of guessing.

Design constraints
------------------
- Always non-fatal.  If the database is unavailable or the insert fails for any
  reason, the exception is silently swallowed.  Hooks must never crash because
  of trace writes.
- No-ops when *conn* is ``None`` (database open failed upstream).
- Uses UTC timestamps in ISO-8601 format for unambiguous cross-timezone replay.
"""

import sqlite3
from datetime import datetime, timezone


def write(
    conn: sqlite3.Connection | None,
    script: str,
    decision: str,
    session_id: str | None = None,
    tool_name: str | None = None,
    elapsed_ms: int | None = None,
    detail: str | None = None,
) -> None:
    """
    Insert one row into the ``traces`` table.

    Parameters
    ----------
    conn:
        Open SQLite connection.  If ``None`` the function returns immediately.
    script:
        Name of the hook script or module that produced this trace
        (e.g. ``"hooks.session_start"``).
    decision:
        Short label describing what the hook decided to do
        (e.g. ``"inject"``, ``"skip"``, ``"circuit_open"``).
    session_id:
        Claude session identifier, if available.
    tool_name:
        The Claude tool that triggered this hook, if applicable.
    elapsed_ms:
        Wall-clock time the hook spent on its main work, in milliseconds.
    detail:
        Free-form string carrying any extra structured information
        (e.g. a JSON snippet, a file path, an error message).
    """
    if conn is None:
        return

    try:
        ts = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO traces (ts, script, decision, session_id, tool_name, elapsed_ms, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (ts, script, decision, session_id, tool_name, elapsed_ms, detail),
        )
        conn.commit()
    except Exception:
        # Trace failures must never propagate — swallow silently.
        pass
