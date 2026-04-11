"""
Integration test fixtures.

Provides a stdlib mock HTTP server that records every request and allows tests
to configure per-path responses.  No third-party dependencies.
"""

from __future__ import annotations

import http.server
import socket
import threading
from pathlib import Path
from typing import Any

import pytest

import pyhooks.config as _config_mod
import pyhooks.db as _db_mod


# ---------------------------------------------------------------------------
# Mock HTTP server
# ---------------------------------------------------------------------------


class RecordingServer(http.server.HTTPServer):
    """An ``HTTPServer`` that records all received requests and serves
    configured responses.

    Attributes
    ----------
    requests:
        List of ``{"method", "path", "body", "headers"}`` dicts, one per
        request received.
    responses:
        Dict mapping URL *path* strings to ``(status_code, body_bytes)`` pairs.
        If a path has no entry, the server returns ``200 {"ok": true}``.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.requests: list[dict[str, Any]] = []
        self.responses: dict[str, tuple[int, bytes]] = {}


class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    """Handler that records the request and returns the configured response."""

    server: RecordingServer

    def _handle(self) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b""

        self.server.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "body": body,
                "headers": dict(self.headers),
            }
        )

        status, resp_body = self.server.responses.get(self.path, (200, b'{"ok":true}'))
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        self.wfile.write(resp_body)

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress server log output in test runs
        pass


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture()
def mock_server() -> Any:
    """Start a ``RecordingServer`` on an ephemeral port.

    Yields the server object so tests can:
    - Read ``server.requests`` to inspect received calls.
    - Write ``server.responses["/some/path"] = (status, body)`` to configure
      responses before running the hook under test.

    The ``base_url`` attribute is set for convenience.
    """
    port = _free_port()
    server = RecordingServer(("127.0.0.1", port), _RecordingHandler)
    server.base_url = f"http://127.0.0.1:{port}"  # type: ignore[attr-defined]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()


@pytest.fixture()
def integration_db(tmp_path: Path) -> Path:
    """Create and return the path to a fresh integration test database."""
    db_path = tmp_path / ".neuroloom.db"
    conn = _db_mod.open_db(db_path)
    assert conn is not None
    conn.close()
    return db_path


@pytest.fixture()
def integration_config(integration_db: Path, mock_server: RecordingServer) -> _config_mod.Config:
    """Return a ``Config`` pointed at the integration DB and mock server."""
    return _config_mod.Config(
        api_key="integration-test-key",
        api_base=mock_server.base_url,  # type: ignore[attr-defined]
        state_db_path=integration_db,
        debug=False,
    )
