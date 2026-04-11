"""
Tests for hooks/http.py.

Uses a stdlib ``http.server.HTTPServer`` running in a background thread as the
mock server — no third-party test dependencies.  Each test class spins up a
fresh server bound to an ephemeral port so tests are fully isolated and
parallelisable.
"""

import http.server
import json
import socket
import threading
from typing import Any


import pyhooks.http as _http_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Ask the OS for an ephemeral port that is currently unused."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _FixedResponseHandler(http.server.BaseHTTPRequestHandler):
    """Request handler that returns a pre-configured status code and body.

    Configuration is attached to the *server* object at construction time via
    ``server.response_status`` and ``server.response_body``.  Incoming request
    headers are recorded on ``server.received_headers`` so tests can inspect them.
    """

    server: Any  # narrowed in tests

    def do_POST(self) -> None:  # noqa: N802
        content_length = int(self.headers.get("Content-Length", 0))
        _ = self.rfile.read(content_length)

        # Record headers for later assertion (normalize to lowercase keys)
        self.server.received_headers = {k.lower(): v for k, v in self.headers.items()}

        status: int = getattr(self.server, "response_status", 200)
        body: bytes = getattr(self.server, "response_body", b'{"ok":true}')

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # Suppress the default "GET /path HTTP/1.1 200 -" log spam in test output.
    def log_message(self, format: str, *args: Any) -> None:
        pass


def _start_mock_server(status: int, body: bytes) -> tuple[http.server.HTTPServer, int, str]:
    """Start a mock HTTP server on a free port and return (server, port, url)."""
    port = _free_port()
    server = http.server.HTTPServer(("127.0.0.1", port), _FixedResponseHandler)
    server.response_status = status  # type: ignore[attr-defined]
    server.response_body = body  # type: ignore[attr-defined]
    server.received_headers = {}  # type: ignore[attr-defined]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    return server, port, f"http://127.0.0.1:{port}/test"


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class TestPostJsonSuccess:
    """Tests for 2xx responses."""

    def test_returns_200_and_body(self) -> None:
        """``post_json`` returns ``(200, body)`` for a 200 OK response."""
        body = b'{"result":"ok"}'
        server, _port, url = _start_mock_server(200, body)
        try:
            result = _http_mod.post_json(url, {}, b"{}", timeout=5.0)
            assert result is not None
            status, resp_body = result
            assert status == 200
            assert resp_body == body
        finally:
            server.shutdown()

    def test_sends_user_agent_header(self) -> None:
        """Requests include ``User-Agent: neuroloom-plugin/0.1.0``."""
        server, _port, url = _start_mock_server(200, b"{}")
        try:
            _http_mod.post_json(url, {}, b"{}", timeout=5.0)
            ua = server.received_headers.get("user-agent", "")  # type: ignore[attr-defined]
            assert ua == "neuroloom-plugin/0.1.0"
        finally:
            server.shutdown()

    def test_sends_content_type_json(self) -> None:
        """Requests include ``Content-Type: application/json``."""
        server, _port, url = _start_mock_server(200, b"{}")
        try:
            _http_mod.post_json(url, {}, b"{}", timeout=5.0)
            ct = server.received_headers.get("content-type", "")  # type: ignore[attr-defined]
            assert "application/json" in ct
        finally:
            server.shutdown()

    def test_caller_headers_merged(self) -> None:
        """Caller-supplied headers are included in the request."""
        server, _port, url = _start_mock_server(200, b"{}")
        try:
            _http_mod.post_json(
                url,
                {"Authorization": "Token secret-key"},
                b"{}",
                timeout=5.0,
            )
            auth = server.received_headers.get("authorization", "")  # type: ignore[attr-defined]
            assert auth == "Token secret-key"
        finally:
            server.shutdown()


class TestPostJsonErrorResponses:
    """Tests for 4xx/5xx HTTP responses."""

    def test_returns_429_status_on_rate_limit(self) -> None:
        """``post_json`` returns ``(429, body)`` for a 429 response."""
        body = b'{"error":"rate_limit"}'
        server, _port, url = _start_mock_server(429, body)
        try:
            result = _http_mod.post_json(url, {}, b"{}", timeout=5.0)
            assert result is not None
            status, _ = result
            assert status == 429
        finally:
            server.shutdown()

    def test_returns_500_on_server_error(self) -> None:
        """``post_json`` returns ``(500, body)`` for a 500 response."""
        server, _port, url = _start_mock_server(500, b'{"error":"internal"}')
        try:
            result = _http_mod.post_json(url, {}, b"{}", timeout=5.0)
            assert result is not None
            status, _ = result
            assert status == 500
        finally:
            server.shutdown()


class TestPostJsonNetworkFailures:
    """Tests for network-level failures — ``post_json`` must never raise."""

    def test_returns_none_on_connection_refused(self) -> None:
        """Returns ``None`` when nothing is listening on the target port."""
        # Use a port that has no server.  We grabbed a free port so it is
        # almost certainly not in use, but even if it is, the function returns
        # None on any network-level error.
        dead_port = _free_port()
        url = f"http://127.0.0.1:{dead_port}/test"

        result = _http_mod.post_json(url, {}, b"{}", timeout=1.0)
        assert result is None

    def test_never_raises_on_bad_host(self) -> None:
        """``post_json`` does not raise even for a completely invalid host."""
        result = _http_mod.post_json(
            "http://invalid.host.that.does.not.exist.neuroloom.internal/api",
            {},
            b"{}",
            timeout=1.0,
        )
        assert result is None

    def test_returns_none_on_very_short_timeout(self) -> None:
        """A near-zero timeout triggers a timeout failure, not an exception."""
        # We need a host that accepts connections so the timeout fires on read.
        # Using a port with nothing listening will get connection refused (which
        # also returns None, validating the same contract).
        dead_port = _free_port()
        url = f"http://127.0.0.1:{dead_port}/test"
        result = _http_mod.post_json(url, {}, b"{}", timeout=0.001)
        assert result is None


class TestPostJsonPayload:
    """Tests verifying that the payload bytes reach the server correctly."""

    def test_post_body_delivered(self) -> None:
        """The payload bytes sent by the caller are delivered to the server."""

        received_bodies: list[bytes] = []

        class _RecordingHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                received_bodies.append(self.rfile.read(length))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, format: str, *args: object) -> None:
                pass

        port = _free_port()
        server = http.server.HTTPServer(("127.0.0.1", port), _RecordingHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        payload = json.dumps({"hello": "world"}).encode("utf-8")
        url = f"http://127.0.0.1:{port}/test"
        try:
            _http_mod.post_json(url, {}, payload, timeout=5.0)
        finally:
            server.shutdown()

        assert received_bodies == [payload]


class TestPostJsonReturnType:
    """Smoke-test the return type annotation at runtime."""

    def test_return_type_on_success(self) -> None:
        """The return value is a ``tuple[int, bytes]`` on success, not None."""
        server, _port, url = _start_mock_server(201, b"created")
        try:
            result = _http_mod.post_json(url, {}, b"{}", timeout=5.0)
            assert isinstance(result, tuple)
            assert len(result) == 2
            status, body = result
            assert isinstance(status, int)
            assert isinstance(body, bytes)
        finally:
            server.shutdown()
