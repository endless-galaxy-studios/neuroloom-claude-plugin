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
import os
import socket
import threading
from pathlib import Path
from typing import Any

import pytest

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


# ---------------------------------------------------------------------------
# D169 helpers — literal .mcp.json write, residue migration, ownership
# ---------------------------------------------------------------------------


def _write_raw_override(project_root: str, raw_value: object) -> None:
    """
    Write .claude/settings.json with an arbitrary (possibly invalid-shape)
    value at the override key, for UUID-validation tests.
    """
    settings_path = Path(project_root) / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(
            {"pluginConfigs": {_wc._PLUGIN_ID: {"options": {"workspace_id": raw_value}}}}
        ),
        encoding="utf-8",
    )


def _write_settings_override(project_root: str, workspace_id: str) -> None:
    """Write .claude/settings.json with a valid-shape UUID override."""
    _write_raw_override(project_root, workspace_id)


def _write_mcp_json(project_root: str, config: dict[str, Any]) -> None:
    (Path(project_root) / ".mcp.json").write_text(json.dumps(config, indent=2), encoding="utf-8")


def _read_mcp_json(project_root: str) -> dict[str, Any]:
    result: dict[str, Any] = json.loads((Path(project_root) / ".mcp.json").read_text(encoding="utf-8"))
    return result


def _seed_owned_entry(project_root: str, db_path: Path, workspace_id: str | None) -> None:
    """
    Create a real owned .mcp.json "neuroloom" entry by driving the writer
    itself, so the ownership record is set exactly as production code would
    set it (never fabricated directly, except where a test's whole point is
    the ownership check itself).
    """
    result = _wc._write_literal_mcp_json_entry(
        project_root, db_path, workspace_id, create_if_missing=True
    )
    assert result == _wc.WriteResult.SUCCESS


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

    def test_resolved_caches_default_and_creates_headerless_entry(self, tmp_path: Path) -> None:
        """
        D169 (R1): the auto-fetched default is cached in .neuroloom.db (F14)
        but is NEVER written into .claude/settings.json pluginConfigs — that
        collision with the manual-override key (the same key a human would
        use) is the regression this deliverable fixes. Was
        `test_resolved_writes_plugin_config`, asserting the pre-D169
        (buggy) `_update_plugin_config` auto-write behavior; repurposed
        rather than deleted, since the underlying fetch/cache flow being
        tested (F14) is unchanged. Branch C (confirmed — see
        `ensure_workspace_configured_detailed`'s baseline call) still
        ensures an owned, headerless baseline `.mcp.json` entry exists even
        though no literal header is ever written from this auto-fetched
        value.
        """
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        body = json.dumps({"workspace_id": "ws-plugin-config"}).encode("utf-8")
        server, api_base = _start_mock_server(200, body)
        try:
            result = _wc.ensure_workspace_configured(
                project_root=str(tmp_path),
                db_path=db_path,
                api_base=api_base,
                api_key="key-B",
            )
        finally:
            server.shutdown()

        assert result == "ws-plugin-config"
        assert _wc.load_workspace_id_from_db(db_path) == "ws-plugin-config"

        settings_path = tmp_path / ".claude" / "settings.json"
        assert not settings_path.exists()

        entry = _read_mcp_json(str(tmp_path))["mcpServers"]["neuroloom"]
        assert _wc._WORKSPACE_HEADER not in entry.get("headers", {})


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


# ---------------------------------------------------------------------------
# D169 — override read, residue migration, ownership-scoped literal writer
# ---------------------------------------------------------------------------
#
# Branch decision confirmed by reading the actual Phase 2 implementation:
# `ensure_workspace_configured_detailed`'s no-override baseline call always
# passes `create_if_missing=True` (Branch C — uniform, "always ensure an
# owned entry exists"). Ownership is tracked in .neuroloom.db, not as an
# in-entry `managed_by` marker — see workspace_config.py's module docstring
# "Ownership marker design" for why the plan's preferred in-entry marker
# could not be verified as schema-tolerant during Phase 2.


class TestReadManualOverrideUuidValidation:
    """Req 6 — every invalid/non-string shape is treated as "no override",
    never raises."""

    @pytest.mark.parametrize(
        "raw_value",
        ["", "   ", "not-a-uuid-at-all", 12345],
        ids=["empty_string", "whitespace_only", "garbage", "non_string_json_value"],
    )
    def test_invalid_or_non_string_values_treated_as_absent(
        self, tmp_path: Path, raw_value: object
    ) -> None:
        project_root = str(tmp_path)
        _write_raw_override(project_root, raw_value)
        assert _wc._read_manual_workspace_override(project_root) is None

    def test_valid_uuid_accepted(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        valid = "66666666-6666-6666-6666-666666666666"
        _write_raw_override(project_root, valid)
        assert _wc._read_manual_workspace_override(project_root) == valid

    def test_uuid_with_surrounding_whitespace_stripped(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        valid = "77777777-7777-7777-7777-777777777777"
        _write_raw_override(project_root, f"  {valid}  ")
        assert _wc._read_manual_workspace_override(project_root) == valid

    def test_no_settings_file_returns_none(self, tmp_path: Path) -> None:
        assert _wc._read_manual_workspace_override(str(tmp_path)) is None


class TestResidueMigration:
    """
    Req 1 (C1, critical) — protects the upgrade path for every project that
    has had a session under a released version of this plugin: a value at
    the override key that exactly matches this session's fingerprint-matched
    cached default is the plugin's own prior auto-write, not a human's
    choice.
    """

    def test_residue_migrated_on_first_call_clean_on_second(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        residue_uuid = "22222222-2222-2222-2222-222222222222"
        api_key = "key-c1"

        _write_settings_override(project_root, residue_uuid)
        _wc._save_workspace_id_to_db(db_path, residue_uuid)
        _wc._save_config_value(
            db_path, _wc._WORKSPACE_ID_FINGERPRINT_KEY, _wc._fingerprint_api_key(api_key)
        )

        first = _wc.ensure_workspace_configured_detailed(
            project_root=project_root,
            db_path=db_path,
            api_base=_dead_api_base(),
            api_key=api_key,
        )

        # Key deleted from .claude/settings.json (merge-preserving delete).
        settings_path = Path(project_root) / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        options = settings["pluginConfigs"][_wc._PLUGIN_ID]["options"]
        assert "workspace_id" not in options

        # Migration log line data present; override-applied log data absent.
        assert first.migrated_residue_value == residue_uuid
        assert first.override_applied is False

        # No literal header write occurs FROM the residue value — the
        # baseline (headerless) entry is created instead, per Branch C.
        entry = _read_mcp_json(project_root)["mcpServers"]["neuroloom"]
        assert _wc._WORKSPACE_HEADER not in entry.get("headers", {})

        # Second call, clean state: behaves exactly like "no override ever
        # configured" — no further migration, no writes driven by residue.
        second = _wc.ensure_workspace_configured_detailed(
            project_root=project_root,
            db_path=db_path,
            api_base=_dead_api_base(),
            api_key=api_key,
        )
        assert second.migrated_residue_value is None
        assert second.override_applied is False

        # The one-time residue-check marker is now set.
        assert _wc._is_residue_check_done(db_path)

    def test_residue_check_marker_set_on_first_call_with_no_override(
        self, tmp_path: Path
    ) -> None:
        """
        (a) — the marker must be set the first time the C1 block runs even
        when there was no override present at all (the inconclusive/no-op
        case), not only when a migration actually occurred.
        """
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        api_key = "key-no-override"

        assert not _wc._is_residue_check_done(db_path)

        result = _wc.ensure_workspace_configured_detailed(
            project_root=project_root,
            db_path=db_path,
            api_base=_dead_api_base(),
            api_key=api_key,
        )
        assert result.migrated_residue_value is None
        assert result.override_applied is False
        assert _wc._is_residue_check_done(db_path)

    def test_override_matching_cache_survives_after_first_session_migration_check(
        self, tmp_path: Path
    ) -> None:
        """
        Regression test for the live production bug: a user whose account's
        bootstrap-resolved default workspace equals the workspace they want
        to pin their project to. Before this fix, the C1 equality check
        re-ran identically on every session and deleted the override as
        "residue" forever, making it impossible to ever successfully pin a
        project to one's own default workspace.

        Sequence mirrors the real-world (ChronoCore) case: the first
        post-upgrade session runs with no override present yet (so the
        one-time residue check has nothing to migrate and simply marks
        itself done). The user then sets an override equal to their
        account's default. A second session must trust it — not delete it.
        """
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        cached_uuid = "66666666-6666-6666-6666-666666666666"
        api_key = "key-survives"

        # First post-upgrade session: no override yet. Marker gets set,
        # nothing to migrate.
        first = _wc.ensure_workspace_configured_detailed(
            project_root=project_root,
            db_path=db_path,
            api_base=_dead_api_base(),
            api_key=api_key,
        )
        assert first.migrated_residue_value is None
        assert _wc._is_residue_check_done(db_path)
        # cached_default was never fetched (dead api_base) — sanity check
        # that the db cache itself is empty so the next step's equality
        # would have been "inconclusive" had the marker not been set.
        assert _wc.load_workspace_id_from_db(db_path) is None

        # Now seed the db cache to simulate a resolved bootstrap default,
        # and have the user set an override equal to it.
        _wc._save_workspace_id_to_db(db_path, cached_uuid)
        _wc._save_config_value(
            db_path, _wc._WORKSPACE_ID_FINGERPRINT_KEY, _wc._fingerprint_api_key(api_key)
        )
        _write_settings_override(project_root, cached_uuid)

        second = _wc.ensure_workspace_configured_detailed(
            project_root=project_root,
            db_path=db_path,
            api_base=_dead_api_base(),
            api_key=api_key,
        )

        # The override must be trusted and applied, not deleted as residue.
        assert second.migrated_residue_value is None
        assert second.override_applied is True
        assert second.workspace_id == cached_uuid
        assert second.override_write_result == _wc.WriteResult.SUCCESS

        entry = _read_mcp_json(project_root)["mcpServers"]["neuroloom"]
        assert entry["headers"][_wc._WORKSPACE_HEADER] == cached_uuid

        # The override key must still be present in settings.json.
        settings_path = Path(project_root) / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        options = settings["pluginConfigs"][_wc._PLUGIN_ID]["options"]
        assert options["workspace_id"] == cached_uuid

    def test_migrated_residue_then_reapplied_override_survives_second_session(
        self, tmp_path: Path
    ) -> None:
        """
        Variant covering the case where the first call DID migrate genuine
        residue (matching the original C1 scenario), and the user then
        deliberately re-configures the same value as a real override
        afterward — it must survive from that point on, never re-deleted.
        """
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        residue_uuid = "77777777-7777-7777-7777-777777777777"
        api_key = "key-remigrate"

        _write_settings_override(project_root, residue_uuid)
        _wc._save_workspace_id_to_db(db_path, residue_uuid)
        _wc._save_config_value(
            db_path, _wc._WORKSPACE_ID_FINGERPRINT_KEY, _wc._fingerprint_api_key(api_key)
        )

        first = _wc.ensure_workspace_configured_detailed(
            project_root=project_root,
            db_path=db_path,
            api_base=_dead_api_base(),
            api_key=api_key,
        )
        assert first.migrated_residue_value == residue_uuid
        assert _wc._is_residue_check_done(db_path)

        # User deliberately re-adds the same value as a genuine override.
        _write_settings_override(project_root, residue_uuid)

        second = _wc.ensure_workspace_configured_detailed(
            project_root=project_root,
            db_path=db_path,
            api_base=_dead_api_base(),
            api_key=api_key,
        )
        assert second.migrated_residue_value is None
        assert second.override_applied is True
        assert second.workspace_id == residue_uuid

        settings_path = Path(project_root) / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        options = settings["pluginConfigs"][_wc._PLUGIN_ID]["options"]
        assert options["workspace_id"] == residue_uuid

    def test_inconclusive_comparison_treated_as_genuine_override(self, tmp_path: Path) -> None:
        """
        No fingerprint-matched cache value to compare against (never
        fetched, or the API key has since rotated) — the read value cannot
        be confidently classified as residue, so it is trusted as a genuine
        override rather than guessed away.
        """
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        candidate = "23232323-2323-2323-2323-232323232323"
        _write_settings_override(project_root, candidate)

        result = _wc.ensure_workspace_configured_detailed(
            project_root=project_root, db_path=db_path, api_base=_dead_api_base(), api_key=""
        )
        assert result.migrated_residue_value is None
        assert result.override_applied is True
        assert result.workspace_id == candidate

    def test_genuine_override_survives_when_cache_holds_different_value(
        self, tmp_path: Path
    ) -> None:
        """
        The deliverable's primary real-world scenario: an API key that
        bootstraps to workspace A (fingerprint-matched cache), on a project
        a human has deliberately pinned to a *different* workspace B via
        ``.claude/settings.json``.

        Every other test in this class either has the cache and override
        EQUAL (the residue-migration path) or has an empty ``api_key`` so
        ``cached_default`` is always ``None`` (the inconclusive-comparison
        path). Neither exercises the ``override == cached_default``
        equality comparison itself with a populated, *differing*
        ``cached_default`` — so a mutant that weakens C1's guard from
        ``override == cached_default`` to just ``cached_default is not
        None`` (silently discarding any genuine override whenever any
        cache exists, regardless of value) would pass the full suite
        without this test.
        """
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        cached_uuid = "44444444-4444-4444-4444-444444444444"
        override_uuid = "55555555-5555-5555-5555-555555555555"
        api_key = "key-c1-differing"

        _write_settings_override(project_root, override_uuid)
        _wc._save_workspace_id_to_db(db_path, cached_uuid)
        _wc._save_config_value(
            db_path, _wc._WORKSPACE_ID_FINGERPRINT_KEY, _wc._fingerprint_api_key(api_key)
        )

        result = _wc.ensure_workspace_configured_detailed(
            project_root=project_root,
            db_path=db_path,
            api_base=_dead_api_base(),
            api_key=api_key,
        )

        assert result.migrated_residue_value is None
        assert result.override_applied is True
        assert result.workspace_id == override_uuid
        assert result.override_write_result == _wc.WriteResult.SUCCESS

        entry = _read_mcp_json(project_root)["mcpServers"]["neuroloom"]
        assert entry["headers"][_wc._WORKSPACE_HEADER] == override_uuid

        # The genuine override key must remain in settings.json — it was
        # never treated as residue, so it must never be deleted.
        settings_path = Path(project_root) / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        options = settings["pluginConfigs"][_wc._PLUGIN_ID]["options"]
        assert options["workspace_id"] == override_uuid


class TestUpgradePathNonClobber:
    """Req 2 (R1) — the auto-fetched default must never be written to the
    override key, from a clean state, across repeated calls."""

    def test_first_and_second_call_never_write_pluginconfigs(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        settings_path = Path(project_root) / ".claude" / "settings.json"

        body = json.dumps({"workspace_id": "ws-r1"}).encode("utf-8")
        server, api_base = _start_mock_server(200, body)
        try:
            first = _wc.ensure_workspace_configured_detailed(
                project_root=project_root, db_path=db_path, api_base=api_base, api_key="key-r1"
            )
        finally:
            server.shutdown()

        assert first.workspace_id == "ws-r1"
        assert first.override_applied is False
        assert not settings_path.exists()

        second = _wc.ensure_workspace_configured_detailed(
            project_root=project_root,
            db_path=db_path,
            api_base=_dead_api_base(),
            api_key="key-r1",
        )
        assert second.override_applied is False
        assert not settings_path.exists()


class TestUnmanagedEntryUntouched:
    """
    Req 3 (C4, critical) — protects real projects (~/Projects/sleeved,
    ~/Projects/ChronoCore) whose existing `.mcp.json` "neuroloom" entries
    match this module's expected url/type but were never created by it.
    Contrast directly against TestSelfHealOwnedEntryOnly below: an *owned*
    entry self-heals; an *unowned* entry (this class) never does, under any
    override state.
    """

    @staticmethod
    def _seed_unowned_entry(project_root: str) -> bytes:
        config = {
            "mcpServers": {
                "neuroloom": {
                    "type": _wc._MCP_SERVER_TYPE,
                    "url": _wc._MCP_SERVER_URL,
                    "headers": {_wc._WORKSPACE_HEADER: "hand-configured-value"},
                }
            }
        }
        _write_mcp_json(project_root, config)
        return (Path(project_root) / ".mcp.json").read_bytes()

    def test_override_present_does_not_adopt_unowned_entry(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        original = self._seed_unowned_entry(project_root)

        result = _wc._write_literal_mcp_json_entry(
            project_root,
            db_path,
            "33333333-3333-3333-3333-333333333333",
            create_if_missing=True,
        )
        assert result == _wc.WriteResult.SKIPPED_UNMANAGED
        assert (Path(project_root) / ".mcp.json").read_bytes() == original

    def test_no_override_self_heal_does_not_blank_unowned_entry(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        original = self._seed_unowned_entry(project_root)

        result = _wc._write_literal_mcp_json_entry(
            project_root, db_path, None, create_if_missing=True
        )
        assert result == _wc.WriteResult.SKIPPED_UNMANAGED
        assert (Path(project_root) / ".mcp.json").read_bytes() == original

    def test_integration_level_override_present_never_adopts_unowned(
        self, tmp_path: Path
    ) -> None:
        """Same scenario, driven through the public resolution function."""
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        original = self._seed_unowned_entry(project_root)
        override_uuid = "34343434-3434-3434-3434-343434343434"
        _write_settings_override(project_root, override_uuid)

        result = _wc.ensure_workspace_configured_detailed(
            project_root=project_root, db_path=db_path, api_base=_dead_api_base(), api_key=""
        )
        assert result.override_applied is False
        assert result.workspace_id is None
        assert result.override_write_result == _wc.WriteResult.SKIPPED_UNMANAGED
        assert (Path(project_root) / ".mcp.json").read_bytes() == original


class TestSelfHealOwnedEntryOnly:
    """
    Req 4 (R3) — self-heal, scoped correctly. Contrast directly against
    TestUnmanagedEntryUntouched above: an *owned* entry's header IS blanked
    when its driving override is removed; an unowned entry never is,
    regardless of override state.
    """

    def test_owned_entry_header_blanked_when_override_removed(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        override_uuid = "44444444-4444-4444-4444-444444444444"

        _write_settings_override(project_root, override_uuid)
        session_n = _wc.ensure_workspace_configured_detailed(
            project_root=project_root, db_path=db_path, api_base=_dead_api_base(), api_key=""
        )
        assert session_n.override_applied is True
        entry = _read_mcp_json(project_root)["mcpServers"]["neuroloom"]
        assert entry["headers"][_wc._WORKSPACE_HEADER] == override_uuid
        assert _wc._is_mcp_entry_owned(db_path) is True

        settings_path = Path(project_root) / ".claude" / "settings.json"
        settings_path.unlink()

        session_n_plus_1 = _wc.ensure_workspace_configured_detailed(
            project_root=project_root, db_path=db_path, api_base=_dead_api_base(), api_key=""
        )
        assert session_n_plus_1.override_applied is False
        entry = _read_mcp_json(project_root)["mcpServers"]["neuroloom"]
        assert _wc._WORKSPACE_HEADER not in entry.get("headers", {})
        # Ownership record survives the self-heal — it's a "blank the
        # header" operation, not a "forget we own this" operation.
        assert _wc._is_mcp_entry_owned(db_path) is True


class TestKeylessCallerWritePathIndependentOfFetch:
    """Req 5 (C3) — the override read, the literal writer, and the
    headerless baseline-ensure all run unconditionally; only the
    cache-fingerprint-fast-path -> fetch sub-flow is gated on api_key."""

    def test_override_write_happens_with_empty_api_key(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        override_uuid = "55555555-5555-5555-5555-555555555555"
        _write_settings_override(project_root, override_uuid)

        result = _wc.ensure_workspace_configured_detailed(
            project_root=project_root, db_path=db_path, api_base=_dead_api_base(), api_key=""
        )
        assert result.override_applied is True
        assert result.workspace_id == override_uuid
        entry = _read_mcp_json(project_root)["mcpServers"]["neuroloom"]
        assert entry["headers"][_wc._WORKSPACE_HEADER] == override_uuid

    def test_headerless_baseline_ensured_with_empty_api_key_no_override(
        self, tmp_path: Path
    ) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)

        result = _wc.ensure_workspace_configured_detailed(
            project_root=project_root, db_path=db_path, api_base=_dead_api_base(), api_key=""
        )
        assert result.baseline_write_result == _wc.WriteResult.SUCCESS
        entry = _read_mcp_json(project_root)["mcpServers"]["neuroloom"]
        assert _wc._WORKSPACE_HEADER not in entry.get("headers", {})


class TestWriterStateMachine:
    """Req 7 — full WriteResult state-machine coverage of
    _write_literal_mcp_json_entry. SKIPPED_UNMANAGED is covered by
    TestUnmanagedEntryUntouched above."""

    def test_success_creates_new_owned_entry_with_header(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        target = "88888888-8888-8888-8888-888888888888"

        result = _wc._write_literal_mcp_json_entry(
            project_root, db_path, target, create_if_missing=True
        )
        assert result == _wc.WriteResult.SUCCESS
        entry = _read_mcp_json(project_root)["mcpServers"]["neuroloom"]
        assert entry["type"] == _wc._MCP_SERVER_TYPE
        assert entry["url"] == _wc._MCP_SERVER_URL
        assert entry["headers"][_wc._WORKSPACE_HEADER] == target
        assert _wc._is_mcp_entry_owned(db_path) is True

    def test_missing_entry_and_create_if_missing_false_is_true_zero_write(
        self, tmp_path: Path
    ) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)

        result = _wc._write_literal_mcp_json_entry(
            project_root, db_path, None, create_if_missing=False
        )
        assert result == _wc.WriteResult.SUCCESS
        assert not (Path(project_root) / ".mcp.json").exists()

    def test_success_idempotent_repeated_call_same_target(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        target = "99999999-9999-9999-9999-999999999999"
        _seed_owned_entry(project_root, db_path, target)

        result = _wc._write_literal_mcp_json_entry(
            project_root, db_path, target, create_if_missing=True
        )
        assert result == _wc.WriteResult.SUCCESS

    def test_success_transitions_headered_to_headerless(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        target = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        _seed_owned_entry(project_root, db_path, target)

        result = _wc._write_literal_mcp_json_entry(
            project_root, db_path, None, create_if_missing=True
        )
        assert result == _wc.WriteResult.SUCCESS
        entry = _read_mcp_json(project_root)["mcpServers"]["neuroloom"]
        assert _wc._WORKSPACE_HEADER not in entry.get("headers", {})

    def test_success_transitions_headerless_to_headered(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        _seed_owned_entry(project_root, db_path, None)
        target = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

        result = _wc._write_literal_mcp_json_entry(
            project_root, db_path, target, create_if_missing=True
        )
        assert result == _wc.WriteResult.SUCCESS
        entry = _read_mcp_json(project_root)["mcpServers"]["neuroloom"]
        assert entry["headers"][_wc._WORKSPACE_HEADER] == target

    def test_failed_on_malformed_top_level_json_file_untouched(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        mcp_path = Path(project_root) / ".mcp.json"
        mcp_path.write_text("{not valid json", encoding="utf-8")
        original = mcp_path.read_bytes()

        result = _wc._write_literal_mcp_json_entry(
            project_root,
            db_path,
            "cccccccc-cccc-cccc-cccc-cccccccccccc",
            create_if_missing=True,
        )
        assert result == _wc.WriteResult.FAILED
        assert mcp_path.read_bytes() == original

    def test_skipped_conflict_on_url_type_mismatch(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        _write_mcp_json(
            project_root,
            {"mcpServers": {"neuroloom": {"type": "sse", "url": "https://evil.example/mcp"}}},
        )

        result = _wc._write_literal_mcp_json_entry(
            project_root, db_path, None, create_if_missing=True
        )
        assert result == _wc.WriteResult.SKIPPED_CONFLICT

    def test_skipped_conflict_mcp_servers_as_list(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        _write_mcp_json(project_root, {"mcpServers": []})

        result = _wc._write_literal_mcp_json_entry(
            project_root, db_path, None, create_if_missing=True
        )
        assert result == _wc.WriteResult.SKIPPED_CONFLICT

    def test_skipped_conflict_neuroloom_entry_non_dict(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        _write_mcp_json(project_root, {"mcpServers": {"neuroloom": "not-a-dict"}})

        result = _wc._write_literal_mcp_json_entry(
            project_root, db_path, None, create_if_missing=True
        )
        assert result == _wc.WriteResult.SKIPPED_CONFLICT

    def test_skipped_conflict_headers_non_dict_on_owned_entry(self, tmp_path: Path) -> None:
        """
        The headers-non-dict guard runs AFTER the ownership check (see
        _write_literal_mcp_json_entry's state machine), so this entry must
        be marked owned first — an unowned entry in this same shape would
        (correctly) short-circuit to SKIPPED_UNMANAGED before ever reaching
        this guard.
        """
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        _write_mcp_json(
            project_root,
            {
                "mcpServers": {
                    "neuroloom": {
                        "type": _wc._MCP_SERVER_TYPE,
                        "url": _wc._MCP_SERVER_URL,
                        "headers": "not-a-dict",
                    }
                }
            },
        )
        _wc._mark_mcp_entry_owned(db_path)

        result = _wc._write_literal_mcp_json_entry(
            project_root, db_path, None, create_if_missing=True
        )
        assert result == _wc.WriteResult.SKIPPED_CONFLICT


class TestAtomicWriteAndSymlinkSafety:
    """Req 8 (C5)."""

    def test_no_orphan_temp_file_after_success(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)

        result = _wc._write_literal_mcp_json_entry(
            project_root, db_path, None, create_if_missing=True
        )
        assert result == _wc.WriteResult.SUCCESS
        assert list(Path(project_root).glob(f"{_wc._MCP_JSON_TMP_PREFIX}*")) == []

    def test_leftover_orphan_swept_at_start_of_next_write(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        orphan = Path(project_root) / f"{_wc._MCP_JSON_TMP_PREFIX}crashed"
        orphan.write_text("leftover from a crashed write", encoding="utf-8")

        result = _wc._write_literal_mcp_json_entry(
            project_root, db_path, None, create_if_missing=True
        )
        assert result == _wc.WriteResult.SUCCESS
        assert not orphan.exists()

    def test_os_replace_failure_cleans_up_temp_and_leaves_original_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        _seed_owned_entry(project_root, db_path, None)
        mcp_path = Path(project_root) / ".mcp.json"
        original = mcp_path.read_bytes()

        def _raise(*args: object, **kwargs: object) -> None:
            raise OSError("simulated os.replace failure")

        # workspace_config.py's `os.replace` call resolves through the same
        # `os` module object this test file imports — patching it here
        # reaches the writer's call site without reaching into the
        # module's internals via a re-export mypy --strict would flag.
        monkeypatch.setattr(os, "replace", _raise)

        result = _wc._write_literal_mcp_json_entry(
            project_root,
            db_path,
            "dddddddd-dddd-dddd-dddd-dddddddddddd",
            create_if_missing=True,
        )
        assert result == _wc.WriteResult.FAILED
        assert mcp_path.read_bytes() == original
        assert list(Path(project_root).glob(f"{_wc._MCP_JSON_TMP_PREFIX}*")) == []

    def test_permission_mode_preserved_on_write(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        # A same-target call would idempotent-skip without ever touching
        # disk, which would pass this assertion for the wrong reason — the
        # target must actually change (headerless -> headered) to force a
        # real write and exercise the chmod branch.
        _seed_owned_entry(project_root, db_path, None)
        mcp_path = Path(project_root) / ".mcp.json"
        os.chmod(mcp_path, 0o640)

        result = _wc._write_literal_mcp_json_entry(
            project_root,
            db_path,
            "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
            create_if_missing=True,
        )
        assert result == _wc.WriteResult.SUCCESS
        assert (os.stat(mcp_path).st_mode & 0o777) == 0o640

    def test_symlink_named_like_temp_prefix_is_never_followed_or_removed(
        self, tmp_path: Path
    ) -> None:
        """
        Documents the symlink-attack protection rather than fully
        automating it across every platform: the orphan sweep explicitly
        skips any candidate that `is_symlink()` (never unlinks through it),
        and the write's own temp file is always created via
        `tempfile.mkstemp` with a random, collision-proof suffix — so a
        pre-planted symlink at a *fixed*, guessable name can never be the
        file this module actually writes through.
        """
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        target = tmp_path / "elsewhere.txt"
        target.write_text("do not touch", encoding="utf-8")
        trap = Path(project_root) / f"{_wc._MCP_JSON_TMP_PREFIX}trap"
        trap.symlink_to(target)

        result = _wc._write_literal_mcp_json_entry(
            project_root, db_path, None, create_if_missing=True
        )
        assert result == _wc.WriteResult.SUCCESS
        assert trap.is_symlink()
        assert target.read_text(encoding="utf-8") == "do not touch"


class TestIdempotencyViaMtimeSentinel:
    """
    Req 9 — idempotency proven via an os.utime sentinel, not mtime-diff or
    content-hash (a false-passable method the round-1 test suite used and
    D169's re-review replaced).
    """

    def test_repeated_call_same_target_does_not_touch_mtime(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        target = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        _seed_owned_entry(project_root, db_path, target)
        mcp_path = Path(project_root) / ".mcp.json"

        old_ts = 1_000_000_000.0
        os.utime(mcp_path, (old_ts, old_ts))

        result = _wc._write_literal_mcp_json_entry(
            project_root, db_path, target, create_if_missing=True
        )
        assert result == _wc.WriteResult.SUCCESS
        assert os.stat(mcp_path).st_mtime == old_ts


class TestIntegrationWriteFailurePropagation:
    """Req 10 — a valid, non-residue override present, but the writer layer
    fails: the public function must never report the override UUID as
    resolved."""

    def test_malformed_mcp_json_never_reports_override_success(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        override_uuid = "12121212-1212-1212-1212-121212121212"
        _write_settings_override(project_root, override_uuid)
        (Path(project_root) / ".mcp.json").write_text("{broken", encoding="utf-8")

        result = _wc.ensure_workspace_configured_detailed(
            project_root=project_root, db_path=db_path, api_base=_dead_api_base(), api_key=""
        )
        assert result.workspace_id is None
        assert result.override_applied is False

    def test_unmanaged_conflicting_entry_never_reports_override_success(
        self, tmp_path: Path
    ) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        override_uuid = "13131313-1313-1313-1313-131313131313"
        _write_settings_override(project_root, override_uuid)
        _write_mcp_json(
            project_root,
            {
                "mcpServers": {
                    "neuroloom": {
                        "type": _wc._MCP_SERVER_TYPE,
                        "url": _wc._MCP_SERVER_URL,
                        "headers": {},
                    }
                }
            },
        )
        # Deliberately NOT marked owned.

        result = _wc.ensure_workspace_configured_detailed(
            project_root=project_root, db_path=db_path, api_base=_dead_api_base(), api_key=""
        )
        assert result.workspace_id is None
        assert result.override_applied is False


class TestVisibilityLogFields:
    """
    Req 11 — session_start.py's three print lines are driven directly by
    these WorkspaceConfigResult fields; see its docstrings. Asserting on the
    fields is the real coverage (the prints themselves are trivial
    formatting over already-tested data).
    """

    def test_override_applied_true_only_on_successful_override_write(
        self, tmp_path: Path
    ) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        override_uuid = "14141414-1414-1414-1414-141414141414"
        _write_settings_override(project_root, override_uuid)

        result = _wc.ensure_workspace_configured_detailed(
            project_root=project_root, db_path=db_path, api_base=_dead_api_base(), api_key=""
        )
        assert result.override_applied is True
        assert result.workspace_id == override_uuid
        assert result.migrated_residue_value is None
        assert result.baseline_write_result is None

    def test_migrated_residue_and_override_applied_never_both_set(
        self, tmp_path: Path
    ) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        residue_uuid = "15151515-1515-1515-1515-151515151515"
        api_key = "key-visibility"
        _write_settings_override(project_root, residue_uuid)
        _wc._save_workspace_id_to_db(db_path, residue_uuid)
        _wc._save_config_value(
            db_path, _wc._WORKSPACE_ID_FINGERPRINT_KEY, _wc._fingerprint_api_key(api_key)
        )

        result = _wc.ensure_workspace_configured_detailed(
            project_root=project_root,
            db_path=db_path,
            api_base=_dead_api_base(),
            api_key=api_key,
        )
        assert result.migrated_residue_value == residue_uuid
        assert result.override_applied is False
        assert result.baseline_write_result is not None

    def test_baseline_write_result_populated_only_when_no_override_applied(
        self, tmp_path: Path
    ) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)

        result = _wc.ensure_workspace_configured_detailed(
            project_root=project_root, db_path=db_path, api_base=_dead_api_base(), api_key=""
        )
        assert result.override_applied is False
        assert result.baseline_write_result == _wc.WriteResult.SUCCESS

    def test_baseline_write_result_non_success_on_forced_failure(self, tmp_path: Path) -> None:
        """Drives the C6 Branch-C connection-missing warning condition."""
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        (Path(project_root) / ".mcp.json").write_text("{broken", encoding="utf-8")

        result = _wc.ensure_workspace_configured_detailed(
            project_root=project_root, db_path=db_path, api_base=_dead_api_base(), api_key=""
        )
        assert result.override_applied is False
        assert result.baseline_write_result == _wc.WriteResult.FAILED


class TestBranchCHeaderlessBaselineCreation:
    """
    Req 12 — Branch C (confirmed: the baseline call in
    ensure_workspace_configured_detailed always passes
    create_if_missing=True). Every project gets an owned baseline entry
    even with no override configured; a header appears only once an
    override is added.
    """

    def test_no_override_creates_owned_headerless_entry(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)

        result = _wc.ensure_workspace_configured_detailed(
            project_root=project_root, db_path=db_path, api_base=_dead_api_base(), api_key=""
        )
        assert result.baseline_write_result == _wc.WriteResult.SUCCESS
        entry = _read_mcp_json(project_root)["mcpServers"]["neuroloom"]
        assert _wc._WORKSPACE_HEADER not in entry.get("headers", {})
        assert _wc._is_mcp_entry_owned(db_path) is True

    def test_header_appears_once_override_added_on_next_call(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        _wc.ensure_workspace_configured_detailed(
            project_root=project_root, db_path=db_path, api_base=_dead_api_base(), api_key=""
        )
        override_uuid = "16161616-1616-1616-1616-161616161616"
        _write_settings_override(project_root, override_uuid)

        result = _wc.ensure_workspace_configured_detailed(
            project_root=project_root, db_path=db_path, api_base=_dead_api_base(), api_key=""
        )
        assert result.override_applied is True
        entry = _read_mcp_json(project_root)["mcpServers"]["neuroloom"]
        assert entry["headers"][_wc._WORKSPACE_HEADER] == override_uuid


class TestMergePreservation:
    """Req 13 — an existing owned entry alongside unrelated user-declared
    servers and/or other headers on the matching entry: confirm those keys
    are byte-for-byte (parsed-value) unchanged after any write."""

    def test_unrelated_servers_and_headers_untouched_after_write(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        _write_mcp_json(
            project_root,
            {
                "mcpServers": {
                    "other-server": {"type": "stdio", "command": "some-other-tool"},
                    "neuroloom": {
                        "type": _wc._MCP_SERVER_TYPE,
                        "url": _wc._MCP_SERVER_URL,
                        "headers": {"X-Custom-Header": "keep-me"},
                    },
                }
            },
        )
        _wc._mark_mcp_entry_owned(db_path)

        result = _wc._write_literal_mcp_json_entry(
            project_root,
            db_path,
            "17171717-1717-1717-1717-171717171717",
            create_if_missing=True,
        )
        assert result == _wc.WriteResult.SUCCESS

        config = _read_mcp_json(project_root)
        assert config["mcpServers"]["other-server"] == {
            "type": "stdio",
            "command": "some-other-tool",
        }
        neuroloom_headers = config["mcpServers"]["neuroloom"]["headers"]
        assert neuroloom_headers["X-Custom-Header"] == "keep-me"
        assert (
            neuroloom_headers[_wc._WORKSPACE_HEADER]
            == "17171717-1717-1717-1717-171717171717"
        )


def test_override_writes_literal_header_not_just_pluginconfigs(tmp_path: Path) -> None:
    """
    Req 14 — named regression test for commit dc7226e (2026-05-16), which
    wrote the resolved workspace_id only into `.claude/settings.json`
    pluginConfigs and relied on Claude Code's `${user_config.workspace_id}`
    template substitution to inject it into the MCP connection's header at
    connection time. That substitution deliberately never reads
    project-level pluginConfigs (a security boundary — see
    workspace_config.py's module docstring "The mechanism"), so the header
    stayed an unresolved template string on every request for two months
    (D169). This asserts the literal `.mcp.json` header is written
    directly — the fix this deliverable restores — not merely pluginConfigs.
    """
    project_root = str(tmp_path)
    db_path = tmp_path / ".neuroloom.db"
    _init_db(db_path)
    override_uuid = "18181818-1818-1818-1818-181818181818"
    _write_settings_override(project_root, override_uuid)

    result = _wc.ensure_workspace_configured_detailed(
        project_root=project_root, db_path=db_path, api_base=_dead_api_base(), api_key=""
    )
    assert result.override_applied is True

    entry = _read_mcp_json(project_root)["mcpServers"]["neuroloom"]
    assert entry["headers"][_wc._WORKSPACE_HEADER] == override_uuid

    settings_path = Path(project_root) / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert (
        settings["pluginConfigs"][_wc._PLUGIN_ID]["options"]["workspace_id"] == override_uuid
    )
