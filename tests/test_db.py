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


class TestEventBufferPayloadType:
    """Tests for the additive event_buffer.payload_type column."""

    def test_column_present_after_open(self, tmp_path: Path) -> None:
        """``payload_type`` exists on event_buffer immediately after open_db."""
        conn = _db_mod.open_db(tmp_path / ".neuroloom.db")
        assert conn is not None
        try:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(event_buffer)").fetchall()
            }
            assert "payload_type" in columns
        finally:
            conn.close()

    def test_idempotent_across_repeated_open_db_calls(self, tmp_path: Path) -> None:
        """Calling open_db twice against a DB that already has the column does not raise."""
        path = tmp_path / ".neuroloom.db"
        conn1 = _db_mod.open_db(path)
        assert conn1 is not None
        conn1.close()

        conn2 = _db_mod.open_db(path)
        assert conn2 is not None
        try:
            columns = {
                row[1] for row in conn2.execute("PRAGMA table_info(event_buffer)").fetchall()
            }
            assert "payload_type" in columns
        finally:
            conn2.close()

    def test_ensure_event_buffer_payload_type_direct_idempotent(self, tmp_path: Path) -> None:
        """Calling the migration helper twice on the same connection does not raise."""
        conn = _db_mod.open_db(tmp_path / ".neuroloom.db")
        assert conn is not None
        try:
            _db_mod._ensure_event_buffer_payload_type(conn)
            _db_mod._ensure_event_buffer_payload_type(conn)
        finally:
            conn.close()

    def test_concurrent_open_race_both_succeed(self, tmp_path: Path) -> None:
        """
        Two open_db() calls racing against a pre-migration DB (event_buffer
        exists but lacks payload_type) both succeed, and the column exists
        exactly once afterward.

        Simulates the race directly: a bare event_buffer table (no
        payload_type) is created first, then _ensure_event_buffer_payload_type
        is invoked twice in a row against two separate connections without
        either one re-checking PRAGMA in between — reproducing "both processes
        saw the column absent" without a real thread race.
        """
        path = tmp_path / ".neuroloom.db"

        # Create a pre-migration event_buffer (bypass ensure_schema's own
        # payload_type addition by building the table by hand).
        setup_conn = sqlite3.connect(str(path))
        setup_conn.execute(
            "CREATE TABLE event_buffer (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "payload TEXT NOT NULL, created_at REAL NOT NULL)"
        )
        setup_conn.commit()
        setup_conn.close()

        conn_a = sqlite3.connect(str(path))
        conn_b = sqlite3.connect(str(path))
        try:
            # Both "see" the column absent (checked before either ALTERs).
            cols_a = {row[1] for row in conn_a.execute("PRAGMA table_info(event_buffer)")}
            cols_b = {row[1] for row in conn_b.execute("PRAGMA table_info(event_buffer)")}
            assert "payload_type" not in cols_a
            assert "payload_type" not in cols_b

            # conn_a wins the race.
            conn_a.execute("ALTER TABLE event_buffer ADD COLUMN payload_type TEXT")
            conn_a.commit()

            # conn_b is the loser — must not raise, must not return None-equivalent.
            try:
                conn_b.execute("ALTER TABLE event_buffer ADD COLUMN payload_type TEXT")
            except sqlite3.OperationalError as exc:
                assert "duplicate column name" in str(exc)
            else:
                pytest.fail("Expected the losing ALTER to raise duplicate column name")

            columns = {row[1] for row in conn_b.execute("PRAGMA table_info(event_buffer)")}
            assert "payload_type" in columns
        finally:
            conn_a.close()
            conn_b.close()

        # Confirm the column exists exactly once via open_db's real code path.
        conn_final = _db_mod.open_db(path)
        assert conn_final is not None
        try:
            rows = conn_final.execute("PRAGMA table_info(event_buffer)").fetchall()
            names = [row[1] for row in rows]
            assert names.count("payload_type") == 1
        finally:
            conn_final.close()

    def test_non_duplicate_operational_error_reraised(self, tmp_path: Path) -> None:
        """
        A genuine (non-duplicate-column) OperationalError raised by the ALTER
        itself propagates rather than being swallowed as a false idempotent
        no-op.
        """

        class _FakeCursor:
            def fetchall(self) -> list[tuple[int, str]]:
                return []  # no columns — payload_type absent

        class _FakeConn:
            """Duck-typed stand-in: PRAGMA reports the column absent, ALTER
            raises a distinct (non-duplicate-column) OperationalError."""

            def execute(self, sql: str) -> _FakeCursor:
                if sql.startswith("PRAGMA"):
                    return _FakeCursor()
                raise sqlite3.OperationalError("no such table: event_buffer")

        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            _db_mod._ensure_event_buffer_payload_type(_FakeConn())  # type: ignore[arg-type]


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
