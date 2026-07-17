"""
Tests for pyhooks/workspace_config.py.

Covers F14 (key-fingerprint cache invalidation) and F15 (result-type
widening for _fetch_workspace_id_from_api / ensure_workspace_configured),
added in D167 Phase 6.

Follows this repo's test conventions:
- SQLite fixtures use ``tmp_path``-based temporary databases (never
  in-memory or shared) — see ``db_path`` in conftest.py.
- HTTP mocking uses a real ``http.server.HTTPServer`` bound to a free port
  (precedent: tests/test_http.py), not a mocking library patching
  ``urllib.request``.
"""

import http.server
import json
import socket
import threading
from pathlib import Path
from typing import Any

import pyhooks.db as _db_mod
import pyhooks.workspace_config as _wc


# ---------------------------------------------------------------------------
# HTTP mocking helpers (mirrors tests/test_http.py's pattern)
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _FixedResponseHandler(http.server.BaseHTTPRequestHandler):
    """Returns a pre-configured status/body attached to the server object."""

    server: Any  # narrowed in tests

    def do_GET(self) -> None:  # noqa: N802
        self.server.received_headers = {k.lower(): v for k, v in self.headers.items()}
        status: int = getattr(self.server, "response_status", 200)
        body: bytes = getattr(self.server, "response_body", b"{}")

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


def _start_mock_server(status: int, body: bytes) -> tuple[http.server.HTTPServer, str]:
    port = _free_port()
    server = http.server.HTTPServer(("127.0.0.1", port), _FixedResponseHandler)
    server.response_status = status  # type: ignore[attr-defined]
    server.response_body = body  # type: ignore[attr-defined]
    server.received_headers = {}  # type: ignore[attr-defined]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    return server, f"http://127.0.0.1:{port}"


def _dead_api_base() -> str:
    """Return an api_base with nothing listening on it."""
    return f"http://127.0.0.1:{_free_port()}"


# ---------------------------------------------------------------------------
# _fetch_workspace_id_from_api — result-type widening (F15)
# ---------------------------------------------------------------------------


class TestFetchWorkspaceIdResolved:
    def test_200_with_workspace_id_resolves(self) -> None:
        body = json.dumps({"workspace_id": "ws-abc-123"}).encode("utf-8")
        server, api_base = _start_mock_server(200, body)
        try:
            result = _wc._fetch_workspace_id_from_api(api_base, "test-key")
            assert result.status is _wc.WorkspaceFetchStatus.RESOLVED
            assert result.workspace_id == "ws-abc-123"
        finally:
            server.shutdown()

    def test_200_with_missing_workspace_id_is_unreachable(self) -> None:
        body = json.dumps({}).encode("utf-8")
        server, api_base = _start_mock_server(200, body)
        try:
            result = _wc._fetch_workspace_id_from_api(api_base, "test-key")
            assert result.status is _wc.WorkspaceFetchStatus.UNREACHABLE
            assert result.workspace_id is None
        finally:
            server.shutdown()


class TestFetchWorkspaceIdAmbiguous:
    def test_400_workspace_not_specified_is_ambiguous(self) -> None:
        body = json.dumps(
            {
                "error": {
                    "code": "workspace_not_specified",
                    "message": "You belong to 2 workspaces.",
                    "workspaces": [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}],
                    "hints": {},
                    "request_id": "req-1",
                    "setup_url": "https://example.com",
                }
            }
        ).encode("utf-8")
        server, api_base = _start_mock_server(400, body)
        try:
            result = _wc._fetch_workspace_id_from_api(api_base, "test-key")
            assert result.status is _wc.WorkspaceFetchStatus.AMBIGUOUS
            assert result.workspace_id is None
        finally:
            server.shutdown()

    def test_400_other_workspace_resolution_code_is_unreachable(self) -> None:
        """
        Only ``workspace_not_specified`` is ambiguous — other
        WorkspaceResolutionError codes (e.g. no_workspace_membership,
        invalid_workspace_id) are not the "2+ memberships, none auto-picked"
        case this feature targets, so they fall through to unreachable.
        """
        body = json.dumps(
            {"error": {"code": "no_workspace_membership", "message": "..."}}
        ).encode("utf-8")
        server, api_base = _start_mock_server(400, body)
        try:
            result = _wc._fetch_workspace_id_from_api(api_base, "test-key")
            assert result.status is _wc.WorkspaceFetchStatus.UNREACHABLE
        finally:
            server.shutdown()


class TestFetchWorkspaceIdDefensiveParsing:
    """
    F15's acceptance-critical requirement: the widened parser must never
    raise on an error-body shape it doesn't recognize, including the
    pre-D167-Phase-1 flat ``{"detail": ...}`` shape that may still be served
    during a deploy-skew window.
    """

    def test_legacy_flat_detail_shape_does_not_raise(self) -> None:
        body = json.dumps({"detail": "Workspace required"}).encode("utf-8")
        server, api_base = _start_mock_server(400, body)
        try:
            result = _wc._fetch_workspace_id_from_api(api_base, "test-key")
            assert result.status is _wc.WorkspaceFetchStatus.UNREACHABLE
            assert result.workspace_id is None
        finally:
            server.shutdown()

    def test_non_json_body_does_not_raise(self) -> None:
        server, api_base = _start_mock_server(400, b"<html>not json</html>")
        try:
            result = _wc._fetch_workspace_id_from_api(api_base, "test-key")
            assert result.status is _wc.WorkspaceFetchStatus.UNREACHABLE
        finally:
            server.shutdown()

    def test_empty_body_does_not_raise(self) -> None:
        server, api_base = _start_mock_server(400, b"")
        try:
            result = _wc._fetch_workspace_id_from_api(api_base, "test-key")
            assert result.status is _wc.WorkspaceFetchStatus.UNREACHABLE
        finally:
            server.shutdown()

    def test_json_array_body_does_not_raise(self) -> None:
        body = json.dumps(["not", "a", "dict"]).encode("utf-8")
        server, api_base = _start_mock_server(400, body)
        try:
            result = _wc._fetch_workspace_id_from_api(api_base, "test-key")
            assert result.status is _wc.WorkspaceFetchStatus.UNREACHABLE
        finally:
            server.shutdown()

    def test_error_field_wrong_type_does_not_raise(self) -> None:
        body = json.dumps({"error": "not a dict"}).encode("utf-8")
        server, api_base = _start_mock_server(400, body)
        try:
            result = _wc._fetch_workspace_id_from_api(api_base, "test-key")
            assert result.status is _wc.WorkspaceFetchStatus.UNREACHABLE
        finally:
            server.shutdown()

    def test_500_server_error_is_unreachable(self) -> None:
        server, api_base = _start_mock_server(500, b'{"detail":"internal error"}')
        try:
            result = _wc._fetch_workspace_id_from_api(api_base, "test-key")
            assert result.status is _wc.WorkspaceFetchStatus.UNREACHABLE
        finally:
            server.shutdown()


class TestFetchWorkspaceIdNetworkFailures:
    def test_connection_refused_is_unreachable(self) -> None:
        result = _wc._fetch_workspace_id_from_api(_dead_api_base(), "test-key")
        assert result.status is _wc.WorkspaceFetchStatus.UNREACHABLE

    def test_bad_host_is_unreachable(self) -> None:
        result = _wc._fetch_workspace_id_from_api(
            "http://invalid.host.that.does.not.exist.neuroloom.internal", "test-key"
        )
        assert result.status is _wc.WorkspaceFetchStatus.UNREACHABLE


# ---------------------------------------------------------------------------
# ensure_workspace_configured — F14 fingerprint invalidation + F15 threading
# ---------------------------------------------------------------------------


def _init_db(db_path: Path) -> None:
    """Apply schema (config table etc.) via the real open_db code path."""
    conn = _db_mod.open_db(db_path)
    assert conn is not None
    conn.close()


class TestEnsureWorkspaceConfiguredFingerprintCaching:
    def test_no_api_key_returns_none(self, tmp_path: Path) -> None:
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        result = _wc.ensure_workspace_configured(
            project_root=str(tmp_path),
            db_path=db_path,
            api_base="http://127.0.0.1:1",
            api_key="",
        )
        assert result is None

    def test_first_call_fetches_and_caches_fingerprint(self, tmp_path: Path) -> None:
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        body = json.dumps({"workspace_id": "ws-first"}).encode("utf-8")
        server, api_base = _start_mock_server(200, body)
        try:
            result = _wc.ensure_workspace_configured(
                project_root=str(tmp_path),
                db_path=db_path,
                api_base=api_base,
                api_key="key-A",
            )
            assert result == "ws-first"

            cached_id = _wc.load_workspace_id_from_db(db_path)
            cached_fp = _wc._load_config_value(db_path, _wc._WORKSPACE_ID_FINGERPRINT_KEY)
            assert cached_id == "ws-first"
            assert cached_fp == _wc._fingerprint_api_key("key-A")
        finally:
            server.shutdown()

    def test_second_call_same_key_uses_cache_no_network(self, tmp_path: Path) -> None:
        """
        A matching fingerprint means the cached workspace_id is trusted
        without a network round-trip — point api_base at a dead port to
        prove no fetch occurs.
        """
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        _wc._save_workspace_id_to_db(db_path, "ws-cached")
        _wc._save_config_value(
            db_path, _wc._WORKSPACE_ID_FINGERPRINT_KEY, _wc._fingerprint_api_key("key-A")
        )

        result = _wc.ensure_workspace_configured(
            project_root=str(tmp_path),
            db_path=db_path,
            api_base=_dead_api_base(),
            api_key="key-A",
        )
        assert result == "ws-cached"

    def test_rotated_key_invalidates_cache_and_refetches(self, tmp_path: Path) -> None:
        """
        A cached workspace_id resolved under an old API key is detected as
        stale (fingerprint mismatch) when a rotated key is used, and the
        server is hit again to re-resolve.
        """
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        _wc._save_workspace_id_to_db(db_path, "ws-old-key")
        _wc._save_config_value(
            db_path, _wc._WORKSPACE_ID_FINGERPRINT_KEY, _wc._fingerprint_api_key("old-key")
        )

        body = json.dumps({"workspace_id": "ws-new-key"}).encode("utf-8")
        server, api_base = _start_mock_server(200, body)
        try:
            result = _wc.ensure_workspace_configured(
                project_root=str(tmp_path),
                db_path=db_path,
                api_base=api_base,
                api_key="rotated-key",
            )
            assert result == "ws-new-key"

            cached_id = _wc.load_workspace_id_from_db(db_path)
            cached_fp = _wc._load_config_value(db_path, _wc._WORKSPACE_ID_FINGERPRINT_KEY)
            assert cached_id == "ws-new-key"
            assert cached_fp == _wc._fingerprint_api_key("rotated-key")
        finally:
            server.shutdown()

    def test_missing_fingerprint_with_present_workspace_id_refetches(
        self, tmp_path: Path
    ) -> None:
        """
        Upgrade-skew case: a workspace_id cached by pre-F14 code has no
        fingerprint row at all. Absence is treated the same as mismatch —
        the cache is invalidated and re-fetched.
        """
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        _wc._save_workspace_id_to_db(db_path, "ws-pre-f14")

        body = json.dumps({"workspace_id": "ws-refetched"}).encode("utf-8")
        server, api_base = _start_mock_server(200, body)
        try:
            result = _wc.ensure_workspace_configured(
                project_root=str(tmp_path),
                db_path=db_path,
                api_base=api_base,
                api_key="any-key",
            )
            assert result == "ws-refetched"
        finally:
            server.shutdown()


class TestEnsureWorkspaceConfiguredResultThreading:
    def test_ambiguous_fetch_returns_sentinel(self, tmp_path: Path) -> None:
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        body = json.dumps(
            {"error": {"code": "workspace_not_specified", "message": "..."}}
        ).encode("utf-8")
        server, api_base = _start_mock_server(400, body)
        try:
            result = _wc.ensure_workspace_configured(
                project_root=str(tmp_path),
                db_path=db_path,
                api_base=api_base,
                api_key="multi-member-key",
            )
            assert result == _wc.WORKSPACE_ID_AMBIGUOUS
        finally:
            server.shutdown()

        # Ambiguity is not cached as a workspace_id — nothing to invalidate,
        # and a subsequent call must re-check rather than "resolve" to the
        # sentinel forever.
        assert _wc.load_workspace_id_from_db(db_path) is None

    def test_unreachable_fetch_returns_none(self, tmp_path: Path) -> None:
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        result = _wc.ensure_workspace_configured(
            project_root=str(tmp_path),
            db_path=db_path,
            api_base=_dead_api_base(),
            api_key="some-key",
        )
        assert result is None
        assert _wc.load_workspace_id_from_db(db_path) is None

    def test_resolved_writes_plugin_config(self, tmp_path: Path) -> None:
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        body = json.dumps({"workspace_id": "ws-plugin-config"}).encode("utf-8")
        server, api_base = _start_mock_server(200, body)
        try:
            _wc.ensure_workspace_configured(
                project_root=str(tmp_path),
                db_path=db_path,
                api_base=api_base,
                api_key="key-B",
            )
        finally:
            server.shutdown()

        settings_path = tmp_path / ".claude" / "settings.json"
        assert settings_path.exists()
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        assert (
            settings["pluginConfigs"]["neuroloom@endless-galaxy-studios"]["options"][
                "workspace_id"
            ]
            == "ws-plugin-config"
        )


# ---------------------------------------------------------------------------
# _parse_ambiguous_error_body — direct unit coverage of the defensive parser
# ---------------------------------------------------------------------------


class TestParseAmbiguousErrorBody:
    def test_matching_shape_returns_true(self) -> None:
        body = json.dumps({"error": {"code": "workspace_not_specified"}}).encode("utf-8")
        assert _wc._parse_ambiguous_error_body(body) is True

    def test_legacy_detail_shape_returns_false(self) -> None:
        body = json.dumps({"detail": "bad request"}).encode("utf-8")
        assert _wc._parse_ambiguous_error_body(body) is False

    def test_non_json_returns_false(self) -> None:
        assert _wc._parse_ambiguous_error_body(b"not json at all") is False

    def test_empty_bytes_returns_false(self) -> None:
        assert _wc._parse_ambiguous_error_body(b"") is False

    def test_wrong_code_returns_false(self) -> None:
        body = json.dumps({"error": {"code": "invalid_workspace_id"}}).encode("utf-8")
        assert _wc._parse_ambiguous_error_body(body) is False
