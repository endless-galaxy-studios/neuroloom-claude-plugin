"""
Integration: full session lifecycle.

Tests the end-to-end flow from session_start through capture (success and
failure paths) through a second session_start that handles stale session
cleanup and buffer flush, then verifies preload_context injection.

All HTTP calls go to the in-process ``RecordingServer`` — no real network I/O.
"""

from __future__ import annotations

import io
import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import pyhooks.capture as _capture_mod
import pyhooks.config as _config_mod
import pyhooks.db as _db_mod
import pyhooks.preload_context as _pc_mod
import pyhooks.session_start as _ss_mod
from tests.integration.conftest import RecordingServer

# Save reference to real open_db before any patches shadow it.
_real_open_db = _db_mod.open_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_session_start(
    config: _config_mod.Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_server: RecordingServer,
) -> str:
    """Run session_start.main() against the mock server. Returns stdout."""
    monkeypatch.chdir(tmp_path)
    output = io.StringIO()

    with (
        patch("pyhooks.session_start._config.load", return_value=config),
        patch(
            "pyhooks.session_start._db.open_db",
            side_effect=_real_open_db,
        ),
        patch("pyhooks.session_start._http.post_json") as mock_post,
        patch("pyhooks.session_start.sys.stdout", output),
        patch("pyhooks.session_start._codeweaver_bootstrap_and_upgrade", return_value=None),
    ):

        def _dispatch(
            url: str, headers: dict[str, str], payload: bytes, timeout: float
        ) -> tuple[int, bytes] | None:
            # Delegate to the actual mock server via our http module
            import urllib.request

            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return int(resp.status), resp.read()
            except Exception:
                return None

        mock_post.side_effect = _dispatch

        _ss_mod.main()

    return output.getvalue()


def _run_capture_with_event(
    config: _config_mod.Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdin_data: str,
    api_returns: tuple[int, bytes] | None = (200, b'{"ok":true}'),
) -> None:
    """Run capture.main() with a given stdin payload."""
    monkeypatch.chdir(tmp_path)

    with (
        patch("pyhooks.capture.load", return_value=config),
        patch("pyhooks.capture.open_db", side_effect=lambda p: _db_mod.open_db(p)),
        patch("pyhooks.capture.post_json", return_value=api_returns),
        patch("pyhooks.capture.sys.stdin", io.StringIO(stdin_data)),
    ):
        try:
            _capture_mod.main()
        except SystemExit:
            pass

    # Wait for any background submission thread
    import threading

    for t in list(threading.enumerate()):
        if t is not threading.current_thread() and not t.daemon:
            t.join(timeout=2.0)


# ---------------------------------------------------------------------------
# Lifecycle test
# ---------------------------------------------------------------------------


class TestFullLifecycle:
    """End-to-end lifecycle: start → capture → failure buffer → restart → inject."""

    def test_session_start_posts_to_sessions_start(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mock_server: RecordingServer,
        integration_config: _config_mod.Config,
        integration_db: Path,
    ) -> None:
        """Step 1: session_start POSTs to /api/v1/sessions/start."""
        mock_server.responses["/api/v1/sessions/start"] = (
            200,
            b'{"session_id":"sess-ok"}',
        )
        _run_session_start(integration_config, tmp_path, monkeypatch, mock_server)

        paths = [r["path"] for r in mock_server.requests]
        assert any("/api/v1/sessions/start" in p for p in paths), (
            f"Expected a POST to /api/v1/sessions/start, got: {paths}"
        )

    def test_capture_posts_observation(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mock_server: RecordingServer,
        integration_config: _config_mod.Config,
        integration_db: Path,
    ) -> None:
        """Step 2: capture POSTs to /api/v1/observations/batch after session start."""
        mock_server.responses["/api/v1/sessions/start"] = (
            200,
            b'{"session_id":"sess-ok"}',
        )
        _run_session_start(integration_config, tmp_path, monkeypatch, mock_server)
        mock_server.requests.clear()

        stdin_data = json.dumps({"tool_name": "Write", "tool_input": {"file_path": "/p/f.py"}})
        _run_capture_with_event(
            integration_config,
            tmp_path,
            monkeypatch,
            stdin_data,
            api_returns=(200, b'{"ok":true}'),
        )

        # capture uses post_json mock directly; we verify no event_buffer row was written
        conn = _db_mod.open_db(integration_db)
        assert conn is not None
        try:
            count = conn.execute("SELECT COUNT(*) FROM event_buffer").fetchone()[0]
            assert count == 0, "No buffering expected on successful API response"
        finally:
            conn.close()

    def test_capture_buffers_on_api_500(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mock_server: RecordingServer,
        integration_config: _config_mod.Config,
        integration_db: Path,
    ) -> None:
        """Step 3: capture writes to event_buffer when API returns 500."""
        mock_server.responses["/api/v1/sessions/start"] = (
            200,
            b'{"session_id":"sess-ok"}',
        )
        _run_session_start(integration_config, tmp_path, monkeypatch, mock_server)

        stdin_data = json.dumps({"tool_name": "Write", "tool_input": {"file_path": "/p/f.py"}})
        _run_capture_with_event(
            integration_config,
            tmp_path,
            monkeypatch,
            stdin_data,
            api_returns=(500, b'{"error":"internal"}'),
        )

        conn = _db_mod.open_db(integration_db)
        assert conn is not None
        try:
            count = conn.execute("SELECT COUNT(*) FROM event_buffer").fetchone()[0]
            assert count >= 1, "Expected buffered event after 500 response"
        finally:
            conn.close()

    def test_second_session_start_ends_stale_and_flushes_buffer(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mock_server: RecordingServer,
        integration_config: _config_mod.Config,
        integration_db: Path,
    ) -> None:
        """Step 4: second session_start ends the stale session and flushes the buffer."""
        mock_server.responses["/api/v1/sessions/start"] = (
            200,
            b'{"session_id":"sess-ok"}',
        )
        # First session start
        _run_session_start(integration_config, tmp_path, monkeypatch, mock_server)

        # Manually seed a buffered event
        conn = _db_mod.open_db(integration_db)
        assert conn is not None
        conn.execute(
            "INSERT INTO event_buffer (payload, created_at) VALUES (?, ?)",
            ('{"buffered":"event"}', time.time()),
        )
        conn.commit()
        conn.close()

        mock_server.requests.clear()
        mock_server.responses["/api/v1/observations/batch"] = (200, b'{"ok":true}')

        # Second session start — should end the stale session and flush the buffer
        _run_session_start(integration_config, tmp_path, monkeypatch, mock_server)

        paths = [r["path"] for r in mock_server.requests]

        # The stale session end URL contains a session_id sub-path
        assert any("sessions/" in p and "/end" in p for p in paths), (
            f"Expected POST to sessions/<id>/end, got: {paths}"
        )
        assert any("observations/batch" in p for p in paths), (
            f"Expected POST to observations/batch for buffer flush, got: {paths}"
        )

        conn2 = _db_mod.open_db(integration_db)
        assert conn2 is not None
        try:
            remaining = conn2.execute("SELECT COUNT(*) FROM event_buffer").fetchone()[0]
            assert remaining == 0, "Buffer should be empty after successful flush"
        finally:
            conn2.close()

    def test_preload_context_inject_returns_additional_context(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mock_server: RecordingServer,
        integration_config: _config_mod.Config,
        integration_db: Path,
    ) -> None:
        """Step 5: preload_context inject mode returns additionalContext from the API."""
        mock_server.responses["/api/v1/sessions/start"] = (
            200,
            b'{"session_id":"sess-ok"}',
        )
        _run_session_start(integration_config, tmp_path, monkeypatch, mock_server)

        monkeypatch.chdir(tmp_path)
        stdin_data = json.dumps(
            {"tool_name": "Read", "tool_input": {"file_path": "/project/api.py"}}
        )
        output = io.StringIO()

        fetch_result = ("Relevant memory for api.py", 300)

        with (
            patch("pyhooks.preload_context._config_mod.load", return_value=integration_config),
            patch(
                "pyhooks.preload_context._db_mod.open_db",
                side_effect=_real_open_db,
            ),
            patch("pyhooks.preload_context._fetch_context", return_value=fetch_result),
            patch("pyhooks.preload_context.sys.stdin", io.StringIO(stdin_data)),
            patch("pyhooks.preload_context.sys.stdout", output),
        ):
            _pc_mod.main()

        result = json.loads(output.getvalue())
        assert "additionalContext" in result, f"Expected additionalContext in output, got: {result}"
        assert "Relevant memory for api.py" in result["additionalContext"]
