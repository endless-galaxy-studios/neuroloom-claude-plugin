"""
Tests for hooks/db.py.

Covers: WAL mode, idempotent schema creation, file permissions, ``open_db``
failure on unwritable paths, foreign-key and unique constraints on
``debounce_files``, and the ``db_conn`` context-manager teardown.
"""

import sqlite3
import stat
import sys
from pathlib import Path

import pytest

import pyhooks.db as _db_mod


class TestOpenDb:
    """Tests for ``open_db``."""

    def test_creates_database_file(self, tmp_path: Path) -> None:
        """``open_db`` creates the database file when it does not exist."""
        path = tmp_path / ".neuroloom.db"
        assert not path.exists()

        conn = _db_mod.open_db(path)
        try:
            assert conn is not None
            assert path.exists()
        finally:
            if conn is not None:
                conn.close()

    def test_wal_mode_active(self, tmp_path: Path) -> None:
        """The journal mode is ``wal`` after ``open_db``."""
        conn = _db_mod.open_db(tmp_path / ".neuroloom.db")
        assert conn is not None
        try:
            row = conn.execute("PRAGMA journal_mode").fetchone()
            assert row is not None
            assert str(row[0]).lower() == "wal"
        finally:
            conn.close()

    def test_file_permissions_0o600(self, tmp_path: Path) -> None:
        """The database file is created with mode ``0o600``."""
        path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(path)
        assert conn is not None
        conn.close()

        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, f"Expected 0o600, got 0o{mode:o}"

    @pytest.mark.skipif(sys.platform == "win32", reason="chmod not meaningful on Windows")
    def test_returns_none_for_unwritable_parent(self, tmp_path: Path) -> None:
        """``open_db`` returns ``None`` when the parent directory is not writable."""
        locked_dir = tmp_path / "locked"
        locked_dir.mkdir()
        path = locked_dir / ".neuroloom.db"

        # Remove write permission from the directory
        locked_dir.chmod(0o555)
        try:
            conn = _db_mod.open_db(path)
            assert conn is None, "Expected None for unwritable parent directory"
        finally:
            # Restore so tmp_path cleanup can delete the directory
            locked_dir.chmod(0o755)


class TestEnsureSchema:
    """Tests for ``ensure_schema``."""

    def test_idempotent_double_call(self, tmp_path: Path) -> None:
        """``ensure_schema`` is safe to call twice on the same connection."""
        conn = _db_mod.open_db(tmp_path / ".neuroloom.db")
        assert conn is not None
        try:
            # First call is already done inside open_db; calling again must not raise.
            _db_mod.ensure_schema(conn)
            _db_mod.ensure_schema(conn)
        finally:
            conn.close()

    def test_all_tables_created(self, tmp_path: Path) -> None:
        """Expected tables exist after ``open_db``."""
        expected_tables = {
            "sessions",
            "circuit_breaker",
            "event_buffer",
            "cache",
            "token_budget",
            "debounce",
            "debounce_files",
            "traces",
        }
        conn = _db_mod.open_db(tmp_path / ".neuroloom.db")
        assert conn is not None
        try:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            actual = {row[0] for row in rows}
            assert expected_tables.issubset(actual)
        finally:
            conn.close()


class TestDebounceFiles:
    """Tests for the ``debounce_files`` table constraints."""

    def _setup_workspace(self, conn: sqlite3.Connection, workspace_key: str) -> None:
        """Insert a ``debounce`` parent row required by the FK constraint."""
        conn.execute(
            "INSERT OR IGNORE INTO debounce (workspace_key, last_sync_ms, backoff_ms) VALUES (?, 0, 2000)",
            (workspace_key,),
        )
        conn.commit()

    def test_fk_constraint_rejects_unknown_workspace(self, tmp_path: Path) -> None:
        """Inserting into ``debounce_files`` with an unknown ``workspace_key`` raises."""
        conn = _db_mod.open_db(tmp_path / ".neuroloom.db")
        assert conn is not None
        try:
            # Enable FK enforcement (SQLite has it off by default)
            conn.execute("PRAGMA foreign_keys = ON")
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO debounce_files (workspace_key, file_path) VALUES (?, ?)",
                    ("nonexistent-workspace", "/tmp/file.py"),
                )
                conn.commit()
        finally:
            conn.close()

    def test_unique_constraint_silently_ignored(self, tmp_path: Path) -> None:
        """Duplicate ``(workspace_key, file_path)`` with ``INSERT OR IGNORE`` does not raise."""
        conn = _db_mod.open_db(tmp_path / ".neuroloom.db")
        assert conn is not None
        try:
            wk = "ws-test-unique"
            self._setup_workspace(conn, wk)

            conn.execute(
                "INSERT OR IGNORE INTO debounce_files (workspace_key, file_path) VALUES (?, ?)",
                (wk, "/project/main.py"),
            )
            conn.execute(
                "INSERT OR IGNORE INTO debounce_files (workspace_key, file_path) VALUES (?, ?)",
                (wk, "/project/main.py"),
            )
            conn.commit()

            count = conn.execute(
                "SELECT COUNT(*) FROM debounce_files WHERE workspace_key = ?",
                (wk,),
            ).fetchone()[0]
            assert count == 1, "Duplicate insert should have been silently ignored"
        finally:
            conn.close()

    def test_multiple_files_accumulated(self, tmp_path: Path) -> None:
        """Different file paths for the same workspace accumulate correctly."""
        conn = _db_mod.open_db(tmp_path / ".neuroloom.db")
        assert conn is not None
        try:
            wk = "ws-multi"
            self._setup_workspace(conn, wk)

            for i in range(5):
                conn.execute(
                    "INSERT OR IGNORE INTO debounce_files (workspace_key, file_path) VALUES (?, ?)",
                    (wk, f"/project/file{i}.py"),
                )
            conn.commit()

            count = conn.execute(
                "SELECT COUNT(*) FROM debounce_files WHERE workspace_key = ?",
                (wk,),
            ).fetchone()[0]
            assert count == 5
        finally:
            conn.close()


class TestDbConnContextManager:
    """Tests for the ``db_conn`` context manager."""

    def test_yields_open_connection(self, tmp_path: Path) -> None:
        """``db_conn`` yields a live ``sqlite3.Connection``."""
        path = tmp_path / ".neuroloom.db"
        with _db_mod.db_conn(path) as conn:
            assert conn is not None
            # A query on a live connection should not raise.
            row = conn.execute("SELECT 1").fetchone()
            assert row is not None

    def test_connection_closed_after_context(self, tmp_path: Path) -> None:
        """The connection is closed when the context manager exits."""
        path = tmp_path / ".neuroloom.db"
        captured_conn: sqlite3.Connection | None = None

        with _db_mod.db_conn(path) as conn:
            captured_conn = conn

        assert captured_conn is not None
        # Attempting to use a closed connection raises ProgrammingError.
        with pytest.raises(Exception):
            captured_conn.execute("SELECT 1")

    def test_yields_none_for_invalid_path(self, tmp_path: Path) -> None:
        """``db_conn`` yields ``None`` when ``open_db`` would fail."""
        # A path whose parent does not exist at all will cause open_db to return None.
        path = tmp_path / "nonexistent" / "deeply" / "nested" / ".neuroloom.db"
        with _db_mod.db_conn(path) as conn:
            assert conn is None
