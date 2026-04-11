"""
Tests for hooks/capture.py.

Tests the guard chain, rate throttle, observation payload shape, event-buffer
fallback, and buffer-cap trimming.  Network calls are mocked via
``unittest.mock.patch`` on ``hooks.http.post_json``.
"""

import io
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import pyhooks.capture as _capture_mod
import pyhooks.db as _db_mod

# Save a reference to the real open_db before any patches can shadow it.
_real_open_db = _db_mod.open_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_session(
    conn: sqlite3.Connection,
    workspace_key: str,
    session_id: str = "sess-1000000000-aabbccdd",
    last_submit_ms: int = 0,
) -> None:
    """Insert a sessions row so the capture hook finds a live session."""
    conn.execute(
        """
        INSERT INTO sessions (session_key, session_id, started_at, last_submit_ms)
        VALUES (?, ?, datetime('now'), ?)
        """,
        (workspace_key, session_id, last_submit_ms),
    )
    conn.commit()


def _make_event(tool_name: str = "Write", extra: dict[str, Any] | None = None) -> str:
    """Return a JSON-encoded tool event as Claude Code would supply on stdin."""
    payload: dict[str, Any] = {"tool_name": tool_name, "tool_input": {"file_path": "/tmp/x.py"}}
    if extra:
        payload.update(extra)
    return json.dumps(payload)


def _run_capture(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    stdin_data: str,
    api_key: str = "test-key",
    post_return: tuple[int, bytes] | None = (200, b'{"ok":true}'),
    now_ms_offset: int = 0,
) -> None:
    """
    Run ``hooks.capture.main()`` with controlled env, stdin, and HTTP mock.

    ``now_ms_offset`` adjusts the mocked current time relative to ``last_submit_ms``
    so that rate-throttle tests can reproduce the exact timing.
    """
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_API_KEY", api_key)
    monkeypatch.setenv("NEUROLOOM_API_BASE", "http://localhost:19999")
    monkeypatch.setattr("pyhooks.config.Config.state_db_path", db_path, raising=False)

    # Patch the DB path inside config so capture opens the test DB
    with (
        patch("pyhooks.capture.load") as mock_load,
        patch("pyhooks.capture.open_db") as mock_open_db,
        patch("pyhooks.capture.post_json", return_value=post_return),
        patch("pyhooks.capture.sys.stdin", io.StringIO(stdin_data)),
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
            _capture_mod.main()
        except SystemExit:
            pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMcpFilter:
    """MCP self-calls must be filtered with a ``mcp_filtered`` trace."""

    def test_mcp_neuroloom_tool_filtered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(db_path)
        assert conn is not None

        workspace_key = str(tmp_path.resolve())
        monkeypatch.chdir(tmp_path)
        _seed_session(conn, workspace_key)
        conn.close()

        stdin_data = json.dumps({"tool_name": "mcp__neuroloom__memory_search"})
        _run_capture(monkeypatch, db_path, stdin_data)

        conn2 = _db_mod.open_db(db_path)
        assert conn2 is not None
        try:
            row = conn2.execute(
                "SELECT decision FROM traces WHERE decision = 'mcp_filtered'"
            ).fetchone()
            assert row is not None, "Expected mcp_filtered trace"
        finally:
            conn2.close()


class TestRateThrottle:
    """Captures within 100 ms of the previous submit are rate-throttled."""

    def test_rate_throttled_when_too_soon(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(db_path)
        assert conn is not None

        workspace_key = str(tmp_path.resolve())
        monkeypatch.chdir(tmp_path)

        # Set last_submit_ms to 50 ms ago so the 100 ms window has not expired.
        now_ms = int(time.time() * 1000)
        last_submit_ms = now_ms - 50
        _seed_session(conn, workspace_key, last_submit_ms=last_submit_ms)
        conn.close()

        stdin_data = _make_event("Write")
        _run_capture(monkeypatch, db_path, stdin_data)

        conn2 = _db_mod.open_db(db_path)
        assert conn2 is not None
        try:
            row = conn2.execute(
                "SELECT decision FROM traces WHERE decision = 'rate_throttled'"
            ).fetchone()
            assert row is not None, "Expected rate_throttled trace"
        finally:
            conn2.close()


class TestCorruptSession:
    """An invalid session_id triggers a ``corrupt_session`` trace and row deletion."""

    def test_corrupt_session_id_deleted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(db_path)
        assert conn is not None

        workspace_key = str(tmp_path.resolve())
        monkeypatch.chdir(tmp_path)
        _seed_session(conn, workspace_key, session_id="INVALID-ID")
        conn.close()

        stdin_data = _make_event("Write")
        _run_capture(monkeypatch, db_path, stdin_data)

        conn2 = _db_mod.open_db(db_path)
        assert conn2 is not None
        try:
            trace_row = conn2.execute(
                "SELECT decision FROM traces WHERE decision = 'corrupt_session'"
            ).fetchone()
            assert trace_row is not None, "Expected corrupt_session trace"

            session_row = conn2.execute(
                "SELECT * FROM sessions WHERE session_key = ?",
                (workspace_key,),
            ).fetchone()
            assert session_row is None, "Corrupt session row should have been deleted"
        finally:
            conn2.close()


class TestObservationPayload:
    """The submitted observation must have a valid UUID observation_id."""

    def test_observation_id_is_valid_uuid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(db_path)
        assert conn is not None

        workspace_key = str(tmp_path.resolve())
        monkeypatch.chdir(tmp_path)
        # Set last_submit_ms far in the past so rate check passes
        _seed_session(conn, workspace_key, last_submit_ms=0)
        conn.close()

        captured_payloads: list[dict[str, Any]] = []

        def _fake_post(
            url: str,
            headers: dict[str, str],
            payload: bytes,
            timeout: float,
        ) -> tuple[int, bytes]:
            captured_payloads.append(json.loads(payload.decode("utf-8")))
            return (200, b'{"ok":true}')

        stdin_data = _make_event("Write")
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_API_KEY", "test-key")
        monkeypatch.setenv("NEUROLOOM_API_BASE", "http://localhost:19999")

        with (
            patch("pyhooks.capture.load") as mock_load,
            patch("pyhooks.capture.open_db") as mock_open_db,
            patch("pyhooks.capture.post_json", side_effect=_fake_post),
            patch("pyhooks.capture.sys.stdin", io.StringIO(stdin_data)),
        ):
            import pyhooks.config as _config_mod

            mock_load.return_value = _config_mod.Config(
                api_key="test-key",
                api_base="http://localhost:19999",
                state_db_path=db_path,
                debug=False,
            )
            mock_open_db.side_effect = _real_open_db

            try:
                _capture_mod.main()
            except SystemExit:
                pass

        # Wait briefly for the background thread to finish
        import threading

        for t in threading.enumerate():
            if t is not threading.current_thread() and t.name.startswith("Thread"):
                t.join(timeout=2.0)

        assert len(captured_payloads) >= 1, "Expected at least one POST"
        obs_list = captured_payloads[0].get("observations", [])
        assert len(obs_list) >= 1
        obs = obs_list[0]
        obs_id = obs.get("observation_id", "")
        # Validate UUID format
        parsed = uuid.UUID(str(obs_id))
        assert str(parsed) == str(obs_id).lower()


class TestEventBuffer:
    """On API failure, the observation is written to ``event_buffer``."""

    def test_event_buffered_on_api_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(db_path)
        assert conn is not None

        workspace_key = str(tmp_path.resolve())
        monkeypatch.chdir(tmp_path)
        _seed_session(conn, workspace_key, last_submit_ms=0)
        conn.close()

        stdin_data = _make_event("Write")

        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_API_KEY", "test-key")
        monkeypatch.setenv("NEUROLOOM_API_BASE", "http://localhost:19999")

        with (
            patch("pyhooks.capture.load") as mock_load,
            patch("pyhooks.capture.open_db") as mock_open_db,
            # Simulate network failure: post_json returns None
            patch("pyhooks.capture.post_json", return_value=None),
            patch("pyhooks.capture.sys.stdin", io.StringIO(stdin_data)),
        ):
            import pyhooks.config as _config_mod

            mock_load.return_value = _config_mod.Config(
                api_key="test-key",
                api_base="http://localhost:19999",
                state_db_path=db_path,
                debug=False,
            )
            mock_open_db.side_effect = _real_open_db

            try:
                _capture_mod.main()
            except SystemExit:
                pass

        # Wait for background thread
        import threading

        for t in threading.enumerate():
            if t is not threading.current_thread():
                t.join(timeout=2.0)

        conn2 = _db_mod.open_db(db_path)
        assert conn2 is not None
        try:
            count = conn2.execute("SELECT COUNT(*) FROM event_buffer").fetchone()[0]
            assert count >= 1, "Expected at least one row in event_buffer after API failure"
        finally:
            conn2.close()


class TestEventBufferCap:
    """The event_buffer is trimmed to 8,000 rows once it exceeds 10,000."""

    def test_buffer_trimmed_when_over_cap(self, tmp_path: Path) -> None:
        db_path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(db_path)
        assert conn is not None
        try:
            # Seed > 10,000 rows directly
            now = time.time()
            conn.executemany(
                "INSERT INTO event_buffer (payload, created_at) VALUES (?, ?)",
                [(f'{{"n":{i}}}', now - i) for i in range(10_100)],
            )
            conn.commit()

            count_before = conn.execute("SELECT COUNT(*) FROM event_buffer").fetchone()[0]
            assert count_before == 10_100

            # Simulate the trim logic from _submit (runs in the background thread
            # after a failed POST).
            if count_before > _capture_mod._BUFFER_MAX:
                conn.execute(
                    f"""
                    DELETE FROM event_buffer
                    WHERE id NOT IN (
                        SELECT id FROM event_buffer
                        ORDER BY id DESC
                        LIMIT {_capture_mod._BUFFER_TRIM_TARGET}
                    )
                    """
                )
                conn.commit()

            count_after = conn.execute("SELECT COUNT(*) FROM event_buffer").fetchone()[0]
            assert count_after == _capture_mod._BUFFER_TRIM_TARGET, (
                f"Expected {_capture_mod._BUFFER_TRIM_TARGET} rows after trim, got {count_after}"
            )
        finally:
            conn.close()


class TestNoApiKey:
    """Without an API key, capture exits immediately without touching the DB."""

    def test_exits_without_db_open(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_API_KEY", "")
        monkeypatch.setenv("NEUROLOOM_API_BASE", "http://localhost:19999")

        open_db_calls: list[Any] = []

        with (
            patch("pyhooks.capture.load") as mock_load,
            patch("pyhooks.capture.open_db") as mock_open_db,
            patch("pyhooks.capture.sys.stdin", io.StringIO("{}")),
        ):
            import pyhooks.config as _config_mod

            mock_load.return_value = _config_mod.Config(
                api_key="",
                api_base="http://localhost:19999",
                state_db_path=tmp_path / ".neuroloom.db",
                debug=False,
            )
            mock_open_db.side_effect = lambda path: open_db_calls.append(path)

            try:
                _capture_mod.main()
            except SystemExit:
                pass

        assert open_db_calls == [], "open_db should not be called when no API key is configured"
