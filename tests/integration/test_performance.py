"""
Integration: performance tests for each hook module.

Each hook's ``main()`` function must complete in under 100 ms when:
- The circuit breaker is pre-tripped so no real network calls occur.
- A valid session row exists in the DB.
- The API key env var is set.

Timing is measured with ``time.perf_counter()`` wrapping a direct call to
``main()`` (not a subprocess) so we avoid Python startup overhead that is
outside our control.  For subprocess-based timing, cold-start overhead is
inherent and tested separately as a smoke test.
"""

from __future__ import annotations

import io
import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import pyhooks.capture as _capture_mod
import pyhooks.code_graph_sync as _cgs_mod
import pyhooks.config as _config_mod
import pyhooks.db as _db_mod
import pyhooks.preload_context as _pc_mod
import pyhooks.session_start as _ss_mod

# Save reference to real open_db before any patches shadow it.
_real_open_db = _db_mod.open_db

_PERF_LIMIT_S = 0.100  # 100 ms budget per hook


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_db(db_path: Path, tmp_path: Path) -> str:
    """Pre-seed the DB with a session row, tripped circuit breaker, and debounce
    record so hooks take the fast path (no network I/O)."""
    conn = _db_mod.open_db(db_path)
    assert conn is not None
    try:
        workspace_key = str(tmp_path.resolve())
        session_id = "sess-1000000000-aabbccdd"

        conn.execute(
            """
            INSERT OR REPLACE INTO sessions
                (session_key, session_id, started_at, last_submit_ms)
            VALUES (?, ?, datetime('now'), 0)
            """,
            (workspace_key, session_id),
        )
        conn.execute(
            "INSERT OR REPLACE INTO circuit_breaker (id, tripped_at) VALUES (1, ?)",
            (time.time(),),
        )
        conn.commit()
    finally:
        conn.close()
    return session_id


def _make_config(db_path: Path) -> _config_mod.Config:
    return _config_mod.Config(
        api_key="perf-test-key",
        api_base="http://127.0.0.1:19999",
        state_db_path=db_path,
        debug=False,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSessionStartPerformance:
    """session_start.main() must complete within 100 ms."""

    def test_session_start_under_100ms(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / ".neuroloom.db"
        _seed_db(db_path, tmp_path)
        monkeypatch.chdir(tmp_path)
        cfg = _make_config(db_path)

        with (
            patch("pyhooks.session_start._config.load", return_value=cfg),
            patch(
                "pyhooks.session_start._db.open_db",
                side_effect=_real_open_db,
            ),
            # Return success for session start; no network call needed
            patch(
                "pyhooks.session_start._http.post_json",
                return_value=(200, b'{"session_id":"sess-ok"}'),
            ),
            patch("pyhooks.session_start.sys.stdout", io.StringIO()),
            patch("pyhooks.session_start._codeweaver_bootstrap_and_upgrade", return_value=None),
        ):
            start = time.perf_counter()
            _ss_mod.main()
            elapsed = time.perf_counter() - start

        assert elapsed < _PERF_LIMIT_S, (
            f"session_start.main() took {elapsed * 1000:.1f} ms, limit is {_PERF_LIMIT_S * 1000:.0f} ms"
        )


class TestCapturePerformance:
    """capture.main() must complete within 100 ms."""

    def test_capture_under_100ms(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db_path = tmp_path / ".neuroloom.db"
        _seed_db(db_path, tmp_path)
        monkeypatch.chdir(tmp_path)
        cfg = _make_config(db_path)

        stdin_data = json.dumps({"tool_name": "Write", "tool_input": {"file_path": "/p/f.py"}})

        with (
            patch("pyhooks.capture.load", return_value=cfg),
            patch("pyhooks.capture.open_db", side_effect=lambda p: _db_mod.open_db(p)),
            patch("pyhooks.capture.post_json", return_value=(200, b'{"ok":true}')),
            patch("pyhooks.capture.sys.stdin", io.StringIO(stdin_data)),
        ):
            start = time.perf_counter()
            try:
                _capture_mod.main()
            except SystemExit:
                pass
            elapsed = time.perf_counter() - start

        # Join any background thread
        import threading

        for t in list(threading.enumerate()):
            if t is not threading.current_thread() and not t.daemon:
                t.join(timeout=0.200)

        assert elapsed < _PERF_LIMIT_S, (
            f"capture.main() took {elapsed * 1000:.1f} ms, limit is {_PERF_LIMIT_S * 1000:.0f} ms"
        )


class TestPreloadContextPerformance:
    """preload_context.main() must complete within 100 ms."""

    def test_preload_inject_under_100ms(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / ".neuroloom.db"
        _seed_db(db_path, tmp_path)
        monkeypatch.chdir(tmp_path)
        cfg = _make_config(db_path)

        stdin_data = json.dumps(
            {"tool_name": "Read", "tool_input": {"file_path": "/project/api.py"}}
        )

        # Circuit breaker is tripped → no API call → fast path
        with (
            patch("pyhooks.preload_context._config_mod.load", return_value=cfg),
            patch(
                "pyhooks.preload_context._db_mod.open_db",
                side_effect=_real_open_db,
            ),
            patch("pyhooks.preload_context.sys.stdin", io.StringIO(stdin_data)),
            patch("pyhooks.preload_context.sys.stdout", io.StringIO()),
        ):
            start = time.perf_counter()
            _pc_mod.main()
            elapsed = time.perf_counter() - start

        assert elapsed < _PERF_LIMIT_S, (
            f"preload_context.main() took {elapsed * 1000:.1f} ms, limit is {_PERF_LIMIT_S * 1000:.0f} ms"
        )

    def test_preload_cache_hit_under_100ms(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cache hit path (no API call) should also be under 100 ms."""
        db_path = tmp_path / ".neuroloom.db"
        _seed_db(db_path, tmp_path)
        monkeypatch.chdir(tmp_path)
        cfg = _make_config(db_path)

        workspace_root = str(tmp_path.resolve())
        file_path = "/project/fast.py"
        cache_key = _pc_mod._cache_key(file_path, workspace_root)

        conn = _db_mod.open_db(db_path)
        assert conn is not None
        conn.execute(
            "INSERT OR REPLACE INTO cache (cache_key, context, created_at) VALUES (?, ?, ?)",
            (cache_key, "Cached context", time.time()),
        )
        conn.commit()
        conn.close()

        stdin_data = json.dumps({"tool_name": "Read", "tool_input": {"file_path": file_path}})

        with (
            patch("pyhooks.preload_context._config_mod.load", return_value=cfg),
            patch(
                "pyhooks.preload_context._db_mod.open_db",
                side_effect=_real_open_db,
            ),
            patch("pyhooks.preload_context.sys.stdin", io.StringIO(stdin_data)),
            patch("pyhooks.preload_context.sys.stdout", io.StringIO()),
        ):
            start = time.perf_counter()
            _pc_mod.main()
            elapsed = time.perf_counter() - start

        assert elapsed < _PERF_LIMIT_S, (
            f"preload_context cache-hit took {elapsed * 1000:.1f} ms, limit is {_PERF_LIMIT_S * 1000:.0f} ms"
        )


class TestCodeGraphSyncPerformance:
    """code_graph_sync.main() must complete within 100 ms."""

    def test_code_graph_sync_under_100ms(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / ".neuroloom.db"
        _seed_db(db_path, tmp_path)
        monkeypatch.chdir(tmp_path)
        cfg = _make_config(db_path)

        stdin_data = json.dumps({"tool_input": {"file_path": str(tmp_path / "module.py")}})

        with (
            patch("pyhooks.code_graph_sync._config.load", return_value=cfg),
            patch(
                "pyhooks.code_graph_sync._db.open_db",
                side_effect=_real_open_db,
            ),
            patch("pyhooks.code_graph_sync._run_codeweaver_sync", return_value=0),
            patch("pyhooks.code_graph_sync.sys.stdin", io.StringIO(stdin_data)),
        ):
            start = time.perf_counter()
            try:
                _cgs_mod.main()
            except SystemExit:
                pass
            elapsed = time.perf_counter() - start

        # Join the background sync thread
        import threading

        for t in list(threading.enumerate()):
            if t is not threading.current_thread() and not t.daemon:
                t.join(timeout=0.200)

        assert elapsed < _PERF_LIMIT_S, (
            f"code_graph_sync.main() took {elapsed * 1000:.1f} ms, limit is {_PERF_LIMIT_S * 1000:.0f} ms"
        )
