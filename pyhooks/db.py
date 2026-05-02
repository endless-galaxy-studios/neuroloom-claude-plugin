"""
SQLite state database for neuroloom hook modules.

The database uses WAL (Write-Ahead Logging) mode so that concurrent readers
(multiple hook processes) never block writers and vice versa — think of WAL as
a two-lane road where reading and writing can happen simultaneously instead of
taking turns on a single lane.

The file is created with mode 0o600 via ``os.open`` *before* SQLite touches it
so that no other OS user can read hook state (session keys, cached context,
trace data).

Schema is applied idempotently via ``CREATE TABLE IF NOT EXISTS`` — safe to
run on every startup with no migrations required.

Usage
-----
Pattern 1 — explicit try/finally::

    conn = open_db(config.state_db_path)
    try:
        if conn is not None:
            # ... work with conn ...
            conn.commit()
    finally:
        if conn is not None:
            conn.close()

Pattern 2 — context manager (preferred)::

    with db_conn(config.state_db_path) as conn:
        if conn is not None:
            # ... work with conn ...
            conn.commit()
"""

import os
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

# 1-second busy timeout: if a sibling hook process holds a write lock we wait
# briefly rather than failing immediately.
_BUSY_TIMEOUT_MS = 1_000

_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS sessions (
    session_key    TEXT PRIMARY KEY,
    session_id     TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    last_submit_ms INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS circuit_breaker (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    tripped_at REAL
);

CREATE TABLE IF NOT EXISTS event_buffer (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    payload    TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS cache (
    cache_key  TEXT PRIMARY KEY,
    context    TEXT,
    created_at REAL
);

CREATE TABLE IF NOT EXISTS token_budget (
    session_id  TEXT PRIMARY KEY,
    total_chars INTEGER
);

CREATE TABLE IF NOT EXISTS debounce (
    workspace_key TEXT PRIMARY KEY,
    last_sync_ms  INTEGER DEFAULT 0,
    backoff_ms    INTEGER DEFAULT 2000
);

CREATE TABLE IF NOT EXISTS debounce_files (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_key TEXT NOT NULL,
    file_path     TEXT NOT NULL,
    UNIQUE(workspace_key, file_path),
    FOREIGN KEY (workspace_key) REFERENCES debounce(workspace_key) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS traces (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    script     TEXT NOT NULL,
    decision   TEXT NOT NULL,
    session_id TEXT,
    tool_name  TEXT,
    elapsed_ms INTEGER,
    detail     TEXT
);

CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Apply the full schema idempotently.  Safe to call on every open."""
    conn.executescript(_SCHEMA)


def open_db(path: Path) -> sqlite3.Connection | None:
    """
    Open (or create) the SQLite state database at *path*.

    The file is pre-created with permissions 0o600 so that only the owning OS
    user can access hook state.  Returns ``None`` on any failure so callers
    never need to handle exceptions.
    """
    try:
        # Pre-create the file with restrictive permissions if it does not exist.
        # os.open with O_CREAT | O_WRONLY is a no-op if the file already exists
        # (the fd is opened and immediately closed; we only care about the
        # permission bits set at creation time).
        fd = os.open(str(path), os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)

        conn = sqlite3.connect(str(path), timeout=_BUSY_TIMEOUT_MS / 1000)
        conn.row_factory = sqlite3.Row
        ensure_schema(conn)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn
    except Exception:
        return None


@contextmanager
def db_conn(path: Path) -> Generator[sqlite3.Connection | None, None, None]:
    """
    Context manager that opens the database, yields the connection (or
    ``None`` on failure), and guarantees the connection is closed on exit.

    Example::

        with db_conn(config.state_db_path) as conn:
            if conn is not None:
                conn.execute("INSERT INTO traces ...")
                conn.commit()
    """
    conn = open_db(path)
    try:
        yield conn
    finally:
        if conn is not None:
            conn.close()
