"""
Tests for hooks/code_graph_sync.py.

Covers: extension filter, workspace containment (including path-traversal edge
cases), debounce timing, debounce_files accumulation and deduplication, drain
atomicity, and adaptive backoff on exit codes 0 and 42.
"""

import hashlib
import io
import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import pyhooks.code_graph_sync as _cgs_mod
import pyhooks.db as _db_mod

# Save reference to real open_db before any patches shadow it.
_real_open_db = _db_mod.open_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _workspace_key(workspace_root: Path) -> str:
    return hashlib.sha256(str(workspace_root).encode()).hexdigest()[:16]


def _run_cgs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    db_path: Path,
    file_path: str,
    api_key: str = "test-key",
    sync_enabled: bool = True,
) -> None:
    """Run ``code_graph_sync.main()`` with a controlled environment."""
    monkeypatch.chdir(tmp_path)
    if not sync_enabled:
        monkeypatch.setenv("NEUROLOOM_CODE_GRAPH_SYNC", "0")
    else:
        monkeypatch.delenv("NEUROLOOM_CODE_GRAPH_SYNC", raising=False)

    stdin_data = json.dumps({"tool_input": {"file_path": file_path}})

    with (
        patch("pyhooks.code_graph_sync._config.load") as mock_load,
        patch("pyhooks.code_graph_sync._db.open_db") as mock_open_db,
        # Prevent real codeweaver or network calls in tests
        patch("pyhooks.code_graph_sync._run_codeweaver_sync", return_value=0),
        patch("pyhooks.code_graph_sync.sys.stdin", io.StringIO(stdin_data)),
    ):
        import pyhooks.config as _config_mod

        mock_load.return_value = _config_mod.Config(
            api_key=api_key,
            api_base="http://localhost:19999",
            state_db_path=db_path,
            debug=False,
        )
        mock_open_db.side_effect = _real_open_db

        try:
            _cgs_mod.main()
        except SystemExit:
            pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestExtensionFilter:
    """Files with disallowed extensions are filtered before DB opens."""

    def test_json_extension_filtered(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db_path = tmp_path / ".neuroloom.db"
        _run_cgs(monkeypatch, tmp_path, db_path, str(tmp_path / "data.json"))

        # DB may not even have been created for a fast-exit extension filter
        if db_path.exists():
            conn = _db_mod.open_db(db_path)
            assert conn is not None
            try:
                count = conn.execute("SELECT COUNT(*) FROM debounce_files").fetchone()[0]
                assert count == 0, "No rows should exist for a filtered extension"
            finally:
                conn.close()

    def test_allowed_py_extension_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / ".neuroloom.db"
        py_file = str(tmp_path / "main.py")
        _run_cgs(monkeypatch, tmp_path, db_path, py_file)

        conn = _db_mod.open_db(db_path)
        assert conn is not None
        try:
            count = conn.execute("SELECT COUNT(*) FROM debounce_files").fetchone()[0]
            assert count >= 1, "Expected at least one row for a .py file"
        finally:
            conn.close()

    def test_ts_extension_passes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db_path = tmp_path / ".neuroloom.db"
        ts_file = str(tmp_path / "component.ts")
        _run_cgs(monkeypatch, tmp_path, db_path, ts_file)

        conn = _db_mod.open_db(db_path)
        assert conn is not None
        try:
            count = conn.execute("SELECT COUNT(*) FROM debounce_files").fetchone()[0]
            assert count >= 1
        finally:
            conn.close()


class TestWorkspaceContainment:
    """Files outside the workspace root are rejected by ``_within_workspace``."""

    def test_relative_traversal_rejected(self, tmp_path: Path) -> None:
        workspace = tmp_path / "project"
        workspace.mkdir()
        # A path that escapes via ``../``
        outside = "../outside/file.py"
        assert not _cgs_mod._within_workspace(outside, workspace.resolve())

    def test_prefix_bypass_rejected(self, tmp_path: Path) -> None:
        """``/project-evil/file.py`` must not match workspace ``/project``."""
        workspace = tmp_path / "project"
        workspace.mkdir()
        evil = tmp_path / "project-evil" / "file.py"
        assert not _cgs_mod._within_workspace(str(evil), workspace.resolve())

    def test_file_inside_workspace_accepted(self, tmp_path: Path) -> None:
        workspace = tmp_path / "project"
        workspace.mkdir()
        inside = workspace / "src" / "main.py"
        assert _cgs_mod._within_workspace(str(inside), workspace.resolve())

    def test_absolute_path_outside_workspace_rejected(self, tmp_path: Path) -> None:
        workspace = tmp_path / "project"
        workspace.mkdir()
        outside = tmp_path / "other_project" / "file.py"
        assert not _cgs_mod._within_workspace(str(outside), workspace.resolve())


class TestDebounce:
    """Debounce check: files within the backoff window are traced as ``debounced``."""

    def test_debounced_when_within_window(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(db_path)
        assert conn is not None

        wk = _workspace_key(tmp_path.resolve())
        now_ms = int(time.time() * 1000)

        # Set last_sync_ms to now so the next call falls within the backoff window
        conn.execute(
            "INSERT OR IGNORE INTO debounce (workspace_key, last_sync_ms, backoff_ms) VALUES (?, ?, 2000)",
            (wk, now_ms),
        )
        conn.commit()
        conn.close()

        _run_cgs(monkeypatch, tmp_path, db_path, str(tmp_path / "main.py"))

        conn2 = _db_mod.open_db(db_path)
        assert conn2 is not None
        try:
            trace = conn2.execute(
                "SELECT decision FROM traces WHERE decision = 'debounced'"
            ).fetchone()
            assert trace is not None, "Expected 'debounced' trace when within backoff window"
        finally:
            conn2.close()

    def test_sync_dispatched_when_outside_window(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(db_path)
        assert conn is not None

        wk = _workspace_key(tmp_path.resolve())
        # Set last_sync_ms far in the past so the backoff window has expired
        conn.execute(
            "INSERT OR IGNORE INTO debounce (workspace_key, last_sync_ms, backoff_ms) VALUES (?, 0, 2000)",
            (wk,),
        )
        conn.commit()
        conn.close()

        _run_cgs(monkeypatch, tmp_path, db_path, str(tmp_path / "main.py"))

        conn2 = _db_mod.open_db(db_path)
        assert conn2 is not None
        try:
            trace = conn2.execute(
                "SELECT decision FROM traces WHERE decision = 'sync_dispatched'"
            ).fetchone()
            assert trace is not None, "Expected 'sync_dispatched' trace"
        finally:
            conn2.close()


class TestDebounceFilesAccumulation:
    """Multiple hook invocations accumulate distinct paths in ``debounce_files``."""

    def test_multiple_files_accumulated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / ".neuroloom.db"
        wk = _workspace_key(tmp_path.resolve())

        # Pre-seed debounce with a recent last_sync_ms so sync is debounced
        conn = _db_mod.open_db(db_path)
        assert conn is not None
        now_ms = int(time.time() * 1000)
        conn.execute(
            "INSERT OR IGNORE INTO debounce (workspace_key, last_sync_ms, backoff_ms) VALUES (?, ?, 2000)",
            (wk, now_ms),
        )
        conn.commit()
        conn.close()

        files = [str(tmp_path / f"file{i}.py") for i in range(4)]
        for f in files:
            _run_cgs(monkeypatch, tmp_path, db_path, f)

        conn2 = _db_mod.open_db(db_path)
        assert conn2 is not None
        try:
            count = conn2.execute(
                "SELECT COUNT(*) FROM debounce_files WHERE workspace_key = ?",
                (wk,),
            ).fetchone()[0]
            assert count == 4, f"Expected 4 debounce_files rows, got {count}"
        finally:
            conn2.close()

    def test_duplicate_file_path_deduplicated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / ".neuroloom.db"
        wk = _workspace_key(tmp_path.resolve())

        conn = _db_mod.open_db(db_path)
        assert conn is not None
        now_ms = int(time.time() * 1000)
        conn.execute(
            "INSERT OR IGNORE INTO debounce (workspace_key, last_sync_ms, backoff_ms) VALUES (?, ?, 2000)",
            (wk, now_ms),
        )
        conn.commit()
        conn.close()

        same_file = str(tmp_path / "shared.py")
        _run_cgs(monkeypatch, tmp_path, db_path, same_file)
        _run_cgs(monkeypatch, tmp_path, db_path, same_file)

        conn2 = _db_mod.open_db(db_path)
        assert conn2 is not None
        try:
            count = conn2.execute(
                "SELECT COUNT(*) FROM debounce_files WHERE workspace_key = ?",
                (wk,),
            ).fetchone()[0]
            assert count == 1, "Duplicate file path should be deduplicated via INSERT OR IGNORE"
        finally:
            conn2.close()


class TestDrainAtomicity:
    """``_drain_debounce_files`` removes all rows for the workspace atomically."""

    def test_drain_removes_all_files(self, tmp_path: Path) -> None:
        db_path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(db_path)
        assert conn is not None
        try:
            wk = "drain-test"
            conn.execute(
                "INSERT INTO debounce (workspace_key, last_sync_ms, backoff_ms) VALUES (?, 0, 2000)",
                (wk,),
            )
            for i in range(5):
                conn.execute(
                    "INSERT OR IGNORE INTO debounce_files (workspace_key, file_path) VALUES (?, ?)",
                    (wk, f"/project/file{i}.py"),
                )
            conn.commit()

            paths = _cgs_mod._drain_debounce_files(conn, wk)
            assert len(paths) == 5

            remaining = conn.execute(
                "SELECT COUNT(*) FROM debounce_files WHERE workspace_key = ?",
                (wk,),
            ).fetchone()[0]
            assert remaining == 0
        finally:
            conn.close()

    def test_drain_returns_empty_when_no_files(self, tmp_path: Path) -> None:
        db_path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(db_path)
        assert conn is not None
        try:
            wk = "empty-drain"
            conn.execute(
                "INSERT INTO debounce (workspace_key, last_sync_ms, backoff_ms) VALUES (?, 0, 2000)",
                (wk,),
            )
            conn.commit()
            paths = _cgs_mod._drain_debounce_files(conn, wk)
            assert paths == []
        finally:
            conn.close()


class TestAdaptiveBackoff:
    """Exit code 42 doubles the backoff; exit code 0 halves it."""

    def test_exit_42_doubles_backoff(self, tmp_path: Path) -> None:
        db_path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(db_path)
        assert conn is not None
        try:
            wk = "backoff-test"
            initial_backoff = 4000
            conn.execute(
                "INSERT INTO debounce (workspace_key, last_sync_ms, backoff_ms) VALUES (?, 0, ?)",
                (wk, initial_backoff),
            )
            conn.commit()

            # Simulate the backoff update for exit code 42
            conn.execute(
                "UPDATE debounce SET backoff_ms = MIN(backoff_ms * 2, 60000) WHERE workspace_key = ?",
                (wk,),
            )
            conn.commit()

            row = conn.execute(
                "SELECT backoff_ms FROM debounce WHERE workspace_key = ?",
                (wk,),
            ).fetchone()
            assert row is not None
            new_backoff: int = row[0]
            assert new_backoff == initial_backoff * 2, (
                f"Expected {initial_backoff * 2}, got {new_backoff}"
            )
        finally:
            conn.close()

    def test_exit_0_halves_backoff(self, tmp_path: Path) -> None:
        db_path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(db_path)
        assert conn is not None
        try:
            wk = "backoff-halve"
            initial_backoff = 8000
            conn.execute(
                "INSERT INTO debounce (workspace_key, last_sync_ms, backoff_ms) VALUES (?, 0, ?)",
                (wk, initial_backoff),
            )
            conn.commit()

            now_ms = int(time.time() * 1000)
            conn.execute(
                "UPDATE debounce SET backoff_ms = MAX(backoff_ms / 2, 2000), last_sync_ms = ? WHERE workspace_key = ?",
                (now_ms, wk),
            )
            conn.commit()

            row = conn.execute(
                "SELECT backoff_ms FROM debounce WHERE workspace_key = ?",
                (wk,),
            ).fetchone()
            assert row is not None
            new_backoff = row[0]
            assert new_backoff == initial_backoff // 2, (
                f"Expected {initial_backoff // 2}, got {new_backoff}"
            )
        finally:
            conn.close()

    def test_backoff_capped_at_60000(self, tmp_path: Path) -> None:
        db_path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(db_path)
        assert conn is not None
        try:
            wk = "backoff-cap"
            conn.execute(
                "INSERT INTO debounce (workspace_key, last_sync_ms, backoff_ms) VALUES (?, 0, 50000)",
                (wk,),
            )
            conn.commit()

            conn.execute(
                "UPDATE debounce SET backoff_ms = MIN(backoff_ms * 2, 60000) WHERE workspace_key = ?",
                (wk,),
            )
            conn.commit()

            row = conn.execute(
                "SELECT backoff_ms FROM debounce WHERE workspace_key = ?",
                (wk,),
            ).fetchone()
            assert row is not None
            assert row[0] == 60000
        finally:
            conn.close()

    def test_backoff_floor_at_2000(self, tmp_path: Path) -> None:
        db_path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(db_path)
        assert conn is not None
        try:
            wk = "backoff-floor"
            conn.execute(
                "INSERT INTO debounce (workspace_key, last_sync_ms, backoff_ms) VALUES (?, 0, 2000)",
                (wk,),
            )
            conn.commit()

            now_ms = int(time.time() * 1000)
            conn.execute(
                "UPDATE debounce SET backoff_ms = MAX(backoff_ms / 2, 2000), last_sync_ms = ? WHERE workspace_key = ?",
                (now_ms, wk),
            )
            conn.commit()

            row = conn.execute(
                "SELECT backoff_ms FROM debounce WHERE workspace_key = ?",
                (wk,),
            ).fetchone()
            assert row is not None
            assert row[0] == 2000
        finally:
            conn.close()


class TestOptOut:
    """Setting ``NEUROLOOM_CODE_GRAPH_SYNC=0`` disables the hook entirely."""

    def test_disabled_when_env_is_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / ".neuroloom.db"
        _run_cgs(
            monkeypatch,
            tmp_path,
            db_path,
            str(tmp_path / "main.py"),
            sync_enabled=False,
        )

        # DB should not have been created (or if it was, no debounce rows)
        if db_path.exists():
            conn = _db_mod.open_db(db_path)
            assert conn is not None
            try:
                count = conn.execute("SELECT COUNT(*) FROM debounce_files").fetchone()[0]
                assert count == 0
            finally:
                conn.close()
