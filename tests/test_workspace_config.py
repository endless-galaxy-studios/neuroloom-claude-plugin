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
import inspect
import itertools
import json
import os
import socket
import threading
import urllib.parse
import urllib.request
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
    """
    MIGRATE (D172 Phase 5): F14's fingerprint-cache invalidation behavior,
    originally proven through the now-retired ``ensure_workspace_configured``,
    now proven directly through ``check_resolvability_with_cache`` — the
    function that actually implements the fast-path/invalidate-on-rotation
    contract under D172 (the automatic session-start axis's resolvability
    gate). Same five scenarios, same assertions in spirit; only the entry
    point under test changed.
    """

    def test_no_api_key_returns_unreachable(self, tmp_path: Path) -> None:
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        resolvability, workspace_id = _wc.check_resolvability_with_cache(
            db_path, api_base="http://127.0.0.1:1", api_key=""
        )
        assert resolvability is Resolvability.UNREACHABLE
        assert workspace_id is None

    def test_first_call_fetches_and_caches_fingerprint(self, tmp_path: Path) -> None:
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        body = json.dumps({"workspace_id": "ws-first"}).encode("utf-8")
        server, api_base = _start_mock_server(200, body)
        try:
            resolvability, workspace_id = _wc.check_resolvability_with_cache(
                db_path, api_base=api_base, api_key="key-A"
            )
            assert resolvability is Resolvability.RESOLVED
            assert workspace_id == "ws-first"

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

        resolvability, workspace_id = _wc.check_resolvability_with_cache(
            db_path, api_base=_dead_api_base(), api_key="key-A"
        )
        assert resolvability is Resolvability.RESOLVED
        assert workspace_id == "ws-cached"

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
            resolvability, workspace_id = _wc.check_resolvability_with_cache(
                db_path, api_base=api_base, api_key="rotated-key"
            )
            assert resolvability is Resolvability.RESOLVED
            assert workspace_id == "ws-new-key"

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
            resolvability, workspace_id = _wc.check_resolvability_with_cache(
                db_path, api_base=api_base, api_key="any-key"
            )
            assert resolvability is Resolvability.RESOLVED
            assert workspace_id == "ws-refetched"
        finally:
            server.shutdown()


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


class TestUpgradePathNonClobber:
    """
    MIGRATE (D172 Phase 5) — Req 2 (R1) named regression guard, restated
    against the new API: the plugin must NEVER write `.claude/settings.json`
    as a side effect of the automatic reconciliation path, from a clean
    state, across repeated calls. This is the same "never writes
    settings.json" assertion the old test made — under D172,
    `.claude/settings.json` drops out of the live reconciliation loop
    entirely (module docstring, "D172: mutable desired state +
    reconciliation"); `gated_reconcile`/`reconcile` never construct a
    `Path(...) / "settings.json"` at all, so this is now a true
    zero-references assertion, not merely "wasn't clobbered this run."
    """

    def test_first_and_second_call_never_write_settings_json(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        settings_path = Path(project_root) / ".claude" / "settings.json"
        target = "aaaa1111-0000-0000-0000-000000000001"
        _wc.write_desired_state(db_path, DesiredState(DesiredKind.SET_TO_VALUE, target))

        body = json.dumps({"workspace_id": target}).encode("utf-8")
        server, api_base = _start_mock_server(200, body)
        try:
            first = _wc.gated_reconcile(project_root, db_path, api_base=api_base, api_key="key-r1")
        finally:
            server.shutdown()

        assert first.resolvability is Resolvability.RESOLVED
        assert first.reconcile is not None
        assert first.reconcile.decision is Decision.CREATE_WITH_HEADER
        assert not settings_path.exists()

        # Second call: dead API base, but the F14 fingerprint-cache fast
        # path (already warmed by the first call, same key) still resolves
        # without a network round-trip — matching the original test's
        # "second call, no live server needed" shape.
        second = _wc.gated_reconcile(
            project_root, db_path, api_base=_dead_api_base(), api_key="key-r1"
        )
        assert second.resolvability is Resolvability.RESOLVED
        assert second.reconcile is not None
        assert second.reconcile.decision is Decision.NO_OP
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
        """
        MIGRATE (D172 Phase 5) — same scenario, driven through the full
        automatic-axis entry point (`gated_reconcile`, resolvability gate +
        `reconcile` wired together) rather than the retired
        `ensure_workspace_configured_detailed`. A desired workspace is on
        file (set via a prior `/neuroloom:workspace` run), the key resolves,
        but the `.mcp.json` entry is unowned — `decide_reconciliation`
        answers a STAMP_OWNERSHIP_* decision, and `reconcile` (FR-7) refuses
        to execute either one without explicit human consent. This is
        distinct from, and complements, `TestReconcileBasicTransitions
        ::test_adoption_decision_never_executed_by_reconcile` (Phase 2 —
        exercises `reconcile` directly) by additionally proving the
        resolvability gate above it doesn't somehow bypass FR-7 either.
        """
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        original = self._seed_unowned_entry(project_root)
        override_uuid = "34343434-3434-3434-3434-343434343434"
        _wc.write_desired_state(db_path, DesiredState(DesiredKind.SET_TO_VALUE, override_uuid))

        body = json.dumps({"workspace_id": override_uuid}).encode("utf-8")
        server, api_base = _start_mock_server(200, body)
        try:
            outcome = _wc.gated_reconcile(project_root, db_path, api_base=api_base, api_key="key")
        finally:
            server.shutdown()

        assert outcome.resolvability is Resolvability.RESOLVED
        assert outcome.reconcile is not None
        assert outcome.reconcile.decision in (
            Decision.STAMP_OWNERSHIP_ONLY,
            Decision.STAMP_OWNERSHIP_WRITE_HEADER,
        )
        assert outcome.reconcile.write_result is None
        assert outcome.reconcile.error is not None
        assert (Path(project_root) / ".mcp.json").read_bytes() == original
        assert _wc._is_mcp_entry_owned(db_path) is False


class TestSelfHealOwnedEntryOnly:
    """
    Req 4 (R3) — self-heal, scoped correctly. Contrast directly against
    TestUnmanagedEntryUntouched above: an *owned* entry's header IS blanked
    when its driving override is removed; an unowned entry never is,
    regardless of override state.
    """

    def test_owned_entry_header_blanked_when_override_removed(self, tmp_path: Path) -> None:
        """
        MIGRATE (D172 Phase 5) — same "session N sets a header, session N+1
        clears it" shape, now expressed through the desired-state model:
        "the override is removed" is a human explicitly clearing the
        desired workspace (``write_desired_state(..., EXPLICITLY_CLEARED)``
        — §6's S3/S4 "clear" transition), not a settings.json file
        disappearing (that mechanism no longer exists under D172). Two
        ``reconcile()`` calls stand in for "session N" / "session N+1".
        """
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        override_uuid = "44444444-4444-4444-4444-444444444444"

        _wc.write_desired_state(db_path, DesiredState(DesiredKind.SET_TO_VALUE, override_uuid))
        session_n = _wc.reconcile(
            project_root, db_path, DesiredState(DesiredKind.SET_TO_VALUE, override_uuid)
        )
        assert session_n.decision is Decision.CREATE_WITH_HEADER
        entry = _read_mcp_json(project_root)["mcpServers"]["neuroloom"]
        assert entry["headers"][_wc._WORKSPACE_HEADER] == override_uuid
        assert _wc._is_mcp_entry_owned(db_path) is True

        _wc.write_desired_state(db_path, DesiredState(DesiredKind.EXPLICITLY_CLEARED))
        session_n_plus_1 = _wc.reconcile(
            project_root, db_path, DesiredState(DesiredKind.EXPLICITLY_CLEARED)
        )
        assert session_n_plus_1.decision is Decision.CLEAR_HEADER
        assert session_n_plus_1.write_result == _wc.WriteResult.SUCCESS
        entry = _read_mcp_json(project_root)["mcpServers"]["neuroloom"]
        assert _wc._WORKSPACE_HEADER not in entry.get("headers", {})
        # Ownership record survives the self-heal — it's a "blank the
        # header" operation, not a "forget we own this" operation.
        assert _wc._is_mcp_entry_owned(db_path) is True


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
    """
    MIGRATE (D172 Phase 5) — Req 10 / NFR-Sec-5 at the integration level: a
    desired workspace is on file, but the observed layer can't safely be
    reconciled — a malformed ``.mcp.json``, or an unmanaged conflicting
    entry — and ``reconcile()`` must never report the desired UUID as
    successfully applied. Complements, does not replace, the low-level
    ``TestWriterStateMachine``/`TestUnmanagedEntryUntouched` coverage of
    ``_write_literal_mcp_json_entry`` directly — this class drives the same
    scenarios through the integration-level entry point production code
    actually calls.
    """

    def test_malformed_mcp_json_never_reports_success(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        override_uuid = "12121212-1212-1212-1212-121212121212"
        (Path(project_root) / ".mcp.json").write_text("{broken", encoding="utf-8")

        outcome = _wc.reconcile(
            project_root, db_path, DesiredState(DesiredKind.SET_TO_VALUE, override_uuid)
        )
        assert outcome.decision is Decision.NO_OP
        assert outcome.write_result is None
        assert outcome.error is not None

    def test_unmanaged_conflicting_entry_never_reports_success(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        override_uuid = "13131313-1313-1313-1313-131313131313"
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

        outcome = _wc.reconcile(
            project_root, db_path, DesiredState(DesiredKind.SET_TO_VALUE, override_uuid)
        )
        assert outcome.decision is Decision.WARN_ONLY
        assert outcome.write_result is None
        entry = _read_mcp_json(project_root)["mcpServers"]["neuroloom"]
        assert _wc._WORKSPACE_HEADER not in entry.get("headers", {})


class TestVisibilityLogFields:
    """
    MIGRATE (D172 Phase 5) — Req 11: ``session_start.py``'s Step 8 print
    wiring is driven directly by ``ReconcileOutcome``'s fields (decision /
    write_result / error), the D172 replacement for the retired
    ``WorkspaceConfigResult``. The underlying "right field for the right
    state" contract carries forward: a write outcome is populated only when
    a write actually happened, and mutually-exclusive states never
    co-populate fields that would let a caller print two contradictory
    messages at once. Asserting on the fields is the real coverage — Step
    8's prints themselves are trivial formatting over this already-tested
    data (see ``TestWorkspaceStepEmissionWiring`` in test_session_start.py
    for the print-wiring layer itself).
    """

    def test_write_result_populated_only_on_actual_header_write(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        target = "14141414-1414-1414-1414-141414141414"
        _seed_owned_entry(project_root, db_path, None)  # owned, headerless

        outcome = _wc.reconcile(
            project_root, db_path, DesiredState(DesiredKind.SET_TO_VALUE, target)
        )
        assert outcome.decision is Decision.WRITE_HEADER
        assert outcome.write_result == _wc.WriteResult.SUCCESS

        # Second call is already-converged NO_OP — write_result must be
        # None (no write happened), never a stale SUCCESS carried forward.
        second = _wc.reconcile(
            project_root, db_path, DesiredState(DesiredKind.SET_TO_VALUE, target)
        )
        assert second.decision is Decision.NO_OP
        assert second.write_result is None

    def test_warn_only_never_populates_a_write_result(self, tmp_path: Path) -> None:
        """
        The "underlying contract carries forward" case that replaces the
        old "migrated_residue and override_applied never both set" test:
        WARN_ONLY and a populated write_result are mutually exclusive —
        WARN_ONLY never touches disk, so a caller can trust "write_result is
        None" as proof no write was attempted for this decision.
        """
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        residue_uuid = "15151515-1515-1515-1515-151515151515"
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

        outcome = _wc.reconcile(
            project_root, db_path, DesiredState(DesiredKind.SET_TO_VALUE, residue_uuid)
        )
        assert outcome.decision is Decision.WARN_ONLY
        assert outcome.write_result is None

    def test_write_result_populated_only_when_baseline_write_occurs(
        self, tmp_path: Path
    ) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)

        outcome = _wc.reconcile(project_root, db_path, DesiredState(DesiredKind.UNSET))
        assert outcome.decision is Decision.CREATE_BASELINE
        assert outcome.write_result == _wc.WriteResult.SUCCESS

    def test_write_result_reflects_forced_baseline_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Drives the C6 Branch-C connection-missing warning condition — a
        genuine WriteResult.FAILED (the write is attempted and fails),
        distinct from ``TestIntegrationWriteFailurePropagation``'s
        malformed-.mcp.json case (which never reaches a write attempt at
        all — it fails closed one layer earlier, at ``read_observed_state``).
        Forces the underlying atomic write's ``os.replace`` to fail so the
        CREATE_BASELINE decision is reached and genuinely attempted.
        """
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)

        def _raise(*args: object, **kwargs: object) -> None:
            raise OSError("simulated os.replace failure")

        monkeypatch.setattr(os, "replace", _raise)

        outcome = _wc.reconcile(project_root, db_path, DesiredState(DesiredKind.UNSET))
        assert outcome.decision is Decision.CREATE_BASELINE
        assert outcome.write_result == _wc.WriteResult.FAILED
        assert not (Path(project_root) / ".mcp.json").exists()


class TestBranchCHeaderlessBaselineCreation:
    """
    MIGRATE (D172 Phase 5) — Req 12 / BASELINE-ENSURE lifecycle row,
    header-parameterized (C-4/R2-3): every project gets an owned baseline
    entry even with no desired workspace set (``CREATE_BASELINE``); a
    header appears immediately if one is already desired at creation time
    (``CREATE_WITH_HEADER``), and a header appears on a *later* call once a
    workspace becomes desired after the baseline already exists
    (``WRITE_HEADER`` against the now-owned headerless entry) — the same
    "two-call progression" the original test proved, now expressed through
    ``reconcile()`` twice in a row instead of
    ``ensure_workspace_configured_detailed``.
    """

    def test_no_desired_state_creates_owned_headerless_entry(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)

        outcome = _wc.reconcile(project_root, db_path, DesiredState(DesiredKind.UNSET))
        assert outcome.decision is Decision.CREATE_BASELINE
        assert outcome.write_result == _wc.WriteResult.SUCCESS
        entry = _read_mcp_json(project_root)["mcpServers"]["neuroloom"]
        assert _wc._WORKSPACE_HEADER not in entry.get("headers", {})
        assert _wc._is_mcp_entry_owned(db_path) is True

    def test_header_appears_once_desired_state_set_on_next_call(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        _wc.reconcile(project_root, db_path, DesiredState(DesiredKind.UNSET))
        override_uuid = "16161616-1616-1616-1616-161616161616"
        _wc.write_desired_state(db_path, DesiredState(DesiredKind.SET_TO_VALUE, override_uuid))

        outcome = _wc.reconcile(
            project_root, db_path, DesiredState(DesiredKind.SET_TO_VALUE, override_uuid)
        )
        assert outcome.decision is Decision.WRITE_HEADER
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
    MIGRATE (D172 Phase 5) — named regression test for commit dc7226e
    (2026-05-16), which wrote the resolved workspace_id only into
    `.claude/settings.json` pluginConfigs and relied on Claude Code's
    `${user_config.workspace_id}` template substitution to inject it into
    the MCP connection's header at connection time. That substitution
    deliberately never reads project-level pluginConfigs (a security
    boundary — see workspace_config.py's module docstring "The
    mechanism"), so the header stayed an unresolved template string on
    every request for two months (D169).

    Under D172 the settings.json-sourced value is no longer written back
    into settings.json at all — it flows through `migrate_settings_json_
    override` (which deletes the legacy key once migrated) and then
    `reconcile` (which writes the literal `.mcp.json` header). The regression
    guard's real claim survives verbatim: the literal header ends up on disk
    in `.mcp.json`, never relying on template substitution against
    settings.json's pluginConfigs — proven here by asserting BOTH that the
    literal header is written AND that the legacy settings.json key is gone
    afterward, which is only possible if the value's true destination is
    `.mcp.json`, not a pluginConfigs entry Claude Code would need to
    template-substitute.
    """
    project_root = str(tmp_path)
    db_path = tmp_path / ".neuroloom.db"
    _init_db(db_path)
    override_uuid = "18181818-1818-1818-1818-181818181818"
    _write_settings_override(project_root, override_uuid)

    body = json.dumps({"workspace_id": override_uuid}).encode("utf-8")
    server, api_base = _start_mock_server(200, body)
    try:
        migration = _wc.migrate_settings_json_override(
            project_root, db_path, api_base=api_base, api_key="key"
        )
    finally:
        server.shutdown()
    assert migration.outcome is _wc.MigrationOutcome.MIGRATED

    outcome = _wc.reconcile(
        project_root, db_path, DesiredState(DesiredKind.SET_TO_VALUE, override_uuid)
    )
    assert outcome.decision is Decision.CREATE_WITH_HEADER
    assert outcome.write_result == _wc.WriteResult.SUCCESS

    entry = _read_mcp_json(project_root)["mcpServers"]["neuroloom"]
    assert entry["headers"][_wc._WORKSPACE_HEADER] == override_uuid

    settings_path = Path(project_root) / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert "workspace_id" not in settings["pluginConfigs"][_wc._PLUGIN_ID]["options"]


# ===========================================================================
# D172 Phase 1 — pure reconciliation-decision core (eight-value vocabulary)
# ===========================================================================
#
# This section is a test-first (red) suite for a *pure function* — no
# filesystem, no SQLite, no network — that D172 Phase 2 will implement as a
# real function in ``pyhooks/workspace_config.py``. Per this phase's file
# scope, no production code is touched here: the "decision core" under test
# is a local stub defined below, deliberately unimplemented (it always
# returns ``None``, which can never equal any real ``Decision`` member, so
# every grid-cell assertion below is provably red before Phase 2 lands).
#
# What "pure function" buys us here, in plain terms: think of a vending
# machine that only ever looks at the coins you put in and the button you
# press — never at what happened in previous transactions, never at the
# clock. Same coins + same button, always the same candy bar, forever. That
# property (no hidden memory, no side effects) is exactly what makes a
# function *idempotent* (constraint below) and exactly what makes 17 tiny
# input combinations enough to prove correctness across the whole state
# space, instead of needing a real filesystem/database harness for each one
# (that imperative-shell coverage is Phase 5's job, over the real thing).
#
# Four input primitives (fixed arity — see TestDecisionCoreSignature):
#   - desired-state:   what the human (or a prior migration/adopt) wants —
#                       unset / set-to-a-value / explicitly-cleared.
#   - observed-state:  what ``.mcp.json`` actually contains right now —
#                       no "neuroloom" entry at all / an entry with no
#                       workspace header / an entry with a header value.
#   - ownership:       does *this plugin* own the observed entry (did it
#                       create it), or is it a hand-authored entry the
#                       plugin must never silently touch? Moot when
#                       observed=ABSENT (there's nothing to own yet) — see
#                       TestOwnershipMootWhenAbsent.
#   - resolvability:   RESOLVED or UNREACHABLE only. ``AMBIGUOUS`` (the
#                       "your API key belongs to more than one workspace"
#                       case) is collapsed to UNREACHABLE-equivalent one
#                       layer up, at the caller's resolvability gate,
#                       specifically so this function never has to reason
#                       about a three-valued resolvability — see
#                       TestUnreachableCollapsesToNoOp for why that
#                       collapse matters operationally, not just for a
#                       tidier enum.
#
# Design decision this phase makes explicit (SDET judgment call, to be
# confirmed/adjusted during Phase 2 review): distinguishing "warn-only"
# from "stamp-ownership-only"/"stamp-ownership+write-header" for an
# unowned entry cannot come from the ownership axis alone (both are
# ownership=UNOWNED) — it comes from whether the *observed* entry carries
# a concrete header value to make a decision about:
#   - unowned + headerless + desired=set-to-W: there is no existing value
#     to sensibly "adopt as-is" against, so the core answers warn-only —
#     flag it for a human, don't guess.
#   - unowned + headered-with-value(V) + desired=set-to-W: a concrete
#     adoption target exists, so the core answers stamp-ownership-only
#     (V already equals W — "adopt as-is") or stamp-ownership+write-header
#     (V differs from W — "adopt and change"). The automatic reconciliation
#     caller (Phase 3) is expected to *never actually execute* these two
#     decisions without explicit human consent (ADR-15's "automatic paths
#     never touch unowned entries" invariant, carried forward) — that's a
#     caller-side policy choice, not something this pure function enforces;
#     the function only ever answers "what write would converge," not
#     "am I allowed to do it right now."


# D172 Phase 2 note: the enums/dataclasses and the decision-core function
# below were originally test-file-local scaffolding (Phase 1, sdet) standing
# in for a not-yet-written production implementation. Phase 2 implements the
# real thing in ``pyhooks/workspace_config.py`` — these names are now thin
# aliases onto the production symbols, so every test below now exercises the
# real decision core (previously the always-``None`` stub) with zero changes
# to the tests themselves. This is what makes the grid's red-to-green
# transition an honest proof: the test *bodies* never changed, only what
# they're wired to.
DesiredKind = _wc.DesiredKind
ObservedKind = _wc.ObservedKind
Ownership = _wc.Ownership
Resolvability = _wc.Resolvability
Decision = _wc.Decision
DesiredState = _wc.DesiredState
ObservedState = _wc.ObservedState

# Phase 2 design-call confirmation (see this module's own note above and
# ``decide_reconciliation``'s docstring in production): the SDET's proposed
# split — unowned+headerless+desired=SET -> WARN_ONLY (no adoption target to
# offer); unowned+headered+desired=SET -> STAMP_OWNERSHIP_ONLY or
# STAMP_OWNERSHIP_WRITE_HEADER depending on value equality — is confirmed
# exactly as designed. Implemented verbatim in
# ``workspace_config.decide_reconciliation``.
_decide_reconciliation_stub = _wc.decide_reconciliation


class _CallCountingDecider:
    """
    Wraps ``_decide_reconciliation_stub`` to count invocations. Exists
    purely to prove a *negative* — "this was never called" — which a bare
    return-value assertion can't express. Same idea as a doorbell camera
    that logs every ring: the log being empty is itself the proof nobody
    rang it, distinct from "somebody rang it and nothing happened."
    """

    def __init__(self) -> None:
        self.call_count = 0

    def __call__(
        self,
        desired: DesiredState,
        observed: ObservedState,
        ownership: Ownership,
        resolvability: Resolvability,
    ) -> Decision | None:
        self.call_count += 1
        return _decide_reconciliation_stub(desired, observed, ownership, resolvability)


def _stub_reconcile_gate(
    desired: DesiredState,
    observed: ObservedState,
    ownership: Ownership,
    resolvability: Resolvability,
    decider: _CallCountingDecider,
) -> Decision | None:
    """
    Placeholder for the caller-side short-circuit Phase 2/3 will implement
    in the real reconciliation routine: DORMANT-SKIP and gate-refused
    states never reach the pure decision core at all — the caller returns
    early, the same way a receptionist who sees "office closed" on the
    door never bothers dialing through to anyone inside. This stub
    reproduces exactly that shape so "never invoked" has something
    concrete to assert against, distinct from "invoked, and happened to
    answer no-op."
    """
    if resolvability is Resolvability.UNREACHABLE:
        return None
    return decider(desired, observed, ownership, resolvability)


# ---------------------------------------------------------------------------
# Full reduced-grid coverage (resolvability=RESOLVED only — UNREACHABLE is
# covered separately below as a cross-cutting property, not per-cell, since
# every desired/observed/ownership combination collapses to the same
# answer under UNREACHABLE).
# ---------------------------------------------------------------------------

_UNSET = DesiredState(DesiredKind.UNSET)
_CLEARED = DesiredState(DesiredKind.EXPLICITLY_CLEARED)


def _set_to(value: str) -> DesiredState:
    return DesiredState(DesiredKind.SET_TO_VALUE, value)


_ABSENT = ObservedState(ObservedKind.ABSENT)
_HEADERLESS = ObservedState(ObservedKind.HEADERLESS)


def _headered(value: str) -> ObservedState:
    return ObservedState(ObservedKind.HEADERED_WITH_VALUE, value)


_W = "aaaaaaaa-0000-0000-0000-000000000001"  # the desired workspace value
_V_MATCHING = _W  # observed header already equals desired (adopt-as-is case)
_V_DIFFERENT = "bbbbbbbb-0000-0000-0000-000000000002"  # observed differs from desired

# Each row: (test id, desired, observed, ownership, expected decision).
_GRID_CELLS: list[tuple[str, DesiredState, ObservedState, Ownership, Decision]] = [
    # --- observed=ABSENT (ownership moot — normalized to OWNED here; see
    #     TestOwnershipMootWhenAbsent for the normalization proof) -------
    (
        "absent_unset_creates_headerless_baseline",
        _UNSET,
        _ABSENT,
        Ownership.OWNED,
        Decision.CREATE_BASELINE,
    ),
    (
        "absent_cleared_creates_headerless_baseline",
        _CLEARED,
        _ABSENT,
        Ownership.OWNED,
        Decision.CREATE_BASELINE,
    ),
    (
        "absent_set_creates_entry_with_header_in_one_transition",
        _set_to(_W),
        _ABSENT,
        Ownership.OWNED,
        Decision.CREATE_WITH_HEADER,
    ),
    # --- observed=HEADERLESS, ownership=OWNED --------------------------
    (
        "owned_headerless_unset_is_noop",
        _UNSET,
        _HEADERLESS,
        Ownership.OWNED,
        Decision.NO_OP,
    ),
    (
        "owned_headerless_cleared_is_noop",
        _CLEARED,
        _HEADERLESS,
        Ownership.OWNED,
        Decision.NO_OP,
    ),
    (
        "owned_headerless_set_writes_header",
        _set_to(_W),
        _HEADERLESS,
        Ownership.OWNED,
        Decision.WRITE_HEADER,
    ),
    # --- observed=HEADERLESS, ownership=UNOWNED ------------------------
    (
        "unowned_headerless_unset_is_noop_nothing_desired",
        _UNSET,
        _HEADERLESS,
        Ownership.UNOWNED,
        Decision.NO_OP,
    ),
    (
        "unowned_headerless_cleared_is_noop_nothing_desired",
        _CLEARED,
        _HEADERLESS,
        Ownership.UNOWNED,
        Decision.NO_OP,
    ),
    (
        "unowned_headerless_set_warns_no_adoption_target",
        _set_to(_W),
        _HEADERLESS,
        Ownership.UNOWNED,
        Decision.WARN_ONLY,
    ),
    # --- observed=HEADERED_WITH_VALUE, ownership=OWNED -----------------
    (
        "owned_headered_unset_clears_header",
        _UNSET,
        _headered(_V_DIFFERENT),
        Ownership.OWNED,
        Decision.CLEAR_HEADER,
    ),
    (
        "owned_headered_cleared_clears_header",
        _CLEARED,
        _headered(_V_DIFFERENT),
        Ownership.OWNED,
        Decision.CLEAR_HEADER,
    ),
    (
        "owned_headered_set_matching_value_is_noop",
        _set_to(_W),
        _headered(_V_MATCHING),
        Ownership.OWNED,
        Decision.NO_OP,
    ),
    (
        "owned_headered_set_differing_value_writes_header",
        _set_to(_W),
        _headered(_V_DIFFERENT),
        Ownership.OWNED,
        Decision.WRITE_HEADER,
    ),
    # --- observed=HEADERED_WITH_VALUE, ownership=UNOWNED ---------------
    (
        "unowned_headered_unset_is_noop_nothing_desired",
        _UNSET,
        _headered(_V_DIFFERENT),
        Ownership.UNOWNED,
        Decision.NO_OP,
    ),
    (
        "unowned_headered_cleared_is_noop_nothing_desired",
        _CLEARED,
        _headered(_V_DIFFERENT),
        Ownership.UNOWNED,
        Decision.NO_OP,
    ),
    (
        "unowned_headered_set_matching_value_stamps_ownership_only",
        _set_to(_W),
        _headered(_V_MATCHING),
        Ownership.UNOWNED,
        Decision.STAMP_OWNERSHIP_ONLY,
    ),
    (
        "unowned_headered_set_differing_value_stamps_ownership_and_writes_header",
        _set_to(_W),
        _headered(_V_DIFFERENT),
        Ownership.UNOWNED,
        Decision.STAMP_OWNERSHIP_WRITE_HEADER,
    ),
]

assert len(_GRID_CELLS) == 17, "grid cell count drifted — update the module docstring too"
# All eight decision values must be reachable somewhere in the grid.
assert {cell[4] for cell in _GRID_CELLS} == set(Decision), (
    "every one of the eight decision values must be exercised by at least one grid cell"
)


class TestDecisionCoreGrid:
    """
    Full reduced-grid coverage. Each test asserts one specific, named
    expected decision — never merely "no exception raised" — and is red
    against the Phase 1 stub (which always returns ``None``) until Phase 2
    implements the real function.
    """

    @pytest.mark.parametrize(
        "desired,observed,ownership,expected",
        [cell[1:] for cell in _GRID_CELLS],
        ids=[cell[0] for cell in _GRID_CELLS],
    )
    def test_grid_cell(
        self,
        desired: DesiredState,
        observed: ObservedState,
        ownership: Ownership,
        expected: Decision,
    ) -> None:
        result = _decide_reconciliation_stub(
            desired, observed, ownership, Resolvability.RESOLVED
        )
        assert result == expected


class TestOwnershipMootWhenAbsent:
    """
    Req (fixed-arity grid, R2-3) — when observed=ABSENT there is no entry
    to own or not own yet, so the ownership axis must not change the
    answer. Proven directly by calling with both ownership values and
    comparing the results to each other (not to a fixed expectation — this
    test stays meaningful whether it's run against the Phase 1 stub or
    Phase 2's real implementation, unlike the grid cells above which pin
    an exact value).
    """

    @pytest.mark.parametrize(
        "desired",
        [_UNSET, _CLEARED, _set_to(_W)],
        ids=["unset", "cleared", "set"],
    )
    def test_ownership_does_not_affect_absent_observed_state(
        self, desired: DesiredState
    ) -> None:
        owned_result = _decide_reconciliation_stub(
            desired, _ABSENT, Ownership.OWNED, Resolvability.RESOLVED
        )
        unowned_result = _decide_reconciliation_stub(
            desired, _ABSENT, Ownership.UNOWNED, Resolvability.RESOLVED
        )
        assert owned_result == unowned_result


class TestUnreachableCollapsesToNoOp:
    """
    Cross-cutting property test (MINOR p) — for ANY desired/observed/
    ownership combination, resolvability=UNREACHABLE always yields no-op
    if the core is invoked at all. One property-style test over the full
    input space, not one test per cell (that would just be the grid above
    with an extra axis, and would obscure that this is a *uniform* rule,
    not a per-cell coincidence).

    Real-world reason this matters, not just tidiness: if the plugin can't
    confirm the API key still resolves to a workspace, it must not guess —
    writing *anything* to .mcp.json on unverified data risks pointing a
    project at a workspace nobody confirmed still exists or is still
    reachable by this key. No-op is the only safe answer when the ground
    truth can't be checked, the same way a GPS that's lost satellite lock
    should say "position unknown," never keep dead-reckoning forever as if
    nothing changed.
    """

    @staticmethod
    def _all_input_combinations() -> list[tuple[DesiredState, ObservedState, Ownership]]:
        desired_states = [_UNSET, _CLEARED, _set_to(_W)]
        observed_states = [_ABSENT, _HEADERLESS, _headered(_V_DIFFERENT)]
        return [
            (d, o, ownership)
            for d, o, ownership in itertools.product(
                desired_states, observed_states, list(Ownership)
            )
        ]

    def test_every_combination_yields_noop_under_unreachable(self) -> None:
        combinations = self._all_input_combinations()
        assert len(combinations) == 18  # 3 desired x 3 observed x 2 ownership

        for desired, observed, ownership in combinations:
            result = _decide_reconciliation_stub(
                desired, observed, ownership, Resolvability.UNREACHABLE
            )
            assert result == Decision.NO_OP, (
                f"expected no-op for desired={desired!r} observed={observed!r} "
                f"ownership={ownership!r} under UNREACHABLE, got {result!r}"
            )


class TestUnreachableCoreReturnsNoop:
    """
    Distinct claim (a) from TestUnreachableGateNeverInvokesCore's claim
    (b) below — this one is "if you call the core directly with
    UNREACHABLE, it answers no-op." The other is "the caller never calls
    it at all for UNREACHABLE." Both are true simultaneously in the real
    system: the core is defensively correct even if somehow reached, AND
    the caller's own gate never actually reaches it — belt and suspenders,
    tested as two separate guarantees rather than conflated into one.
    """

    def test_direct_call_with_unreachable_returns_noop(self) -> None:
        result = _decide_reconciliation_stub(
            _set_to(_W), _headered(_V_DIFFERENT), Ownership.OWNED, Resolvability.UNREACHABLE
        )
        assert result == Decision.NO_OP


class TestUnreachableGateNeverInvokesCore:
    """
    Distinct claim (b) — DORMANT-SKIP and gate-refused states are caller
    short-circuits, never core invocations (M-20). This is a different
    claim from "the core returns no-op": here, the core is never called at
    all, proven via the call-counting wrapper.
    """

    def test_gate_short_circuits_before_calling_decider(self) -> None:
        decider = _CallCountingDecider()
        result = _stub_reconcile_gate(
            _set_to(_W),
            _headered(_V_DIFFERENT),
            Ownership.OWNED,
            Resolvability.UNREACHABLE,
            decider,
        )
        assert result is None
        assert decider.call_count == 0

    def test_gate_does_invoke_decider_when_resolved(self) -> None:
        """
        Sanity check on the gate stub itself: RESOLVED must still reach
        the decider exactly once, so the zero-count assertion above is
        proof of a real short-circuit, not an artifact of a gate that
        never calls its decider under any condition.
        """
        decider = _CallCountingDecider()
        _stub_reconcile_gate(
            _set_to(_W), _headered(_V_DIFFERENT), Ownership.OWNED, Resolvability.RESOLVED, decider
        )
        assert decider.call_count == 1


class TestDecisionCoreSignature:
    """
    Fixed-arity guard (MINOR f) — exactly the four primitives: desired,
    observed, ownership, resolvability. No session-identity, timestamp, or
    call-history parameter.

    D172 completion review, Finding MINOR 1: this class predates Phase 2
    and originally pinned a Phase-1 stub's signature so the real
    implementation would be written against the same shape. As of Phase 2,
    ``_decide_reconciliation_stub`` (see its module-level assignment above)
    is aliased directly onto the real, production ``decide_reconciliation``
    function — there is no separate stub left to pin. This class now
    enforces fixed arity against production code itself: a production
    signature change that adds a default-valued or session-identity/
    timestamp/call-history parameter fails here, not just at the original
    stub-drafting stage.
    """

    def test_exactly_four_parameters_named_and_ordered(self) -> None:
        signature = inspect.signature(_decide_reconciliation_stub)
        param_names = list(signature.parameters.keys())
        assert param_names == ["desired", "observed", "ownership", "resolvability"]

    def test_no_default_values_hiding_a_fifth_implicit_axis(self) -> None:
        """
        A parameter with a default value could silently smuggle in a
        session-identity/timestamp/call-history axis without breaking the
        "four parameters" count above — this closes that loophole.
        """
        signature = inspect.signature(_decide_reconciliation_stub)
        for name, param in signature.parameters.items():
            assert param.default is inspect.Parameter.empty, (
                f"parameter {name!r} must not carry a default — every axis must be "
                "explicit at every call site"
            )


class TestDecisionCoreIdempotency:
    """
    Idempotency regression test — genuinely calls the function twice with
    identical inputs and compares. Idempotency here means the same thing
    it means for an elevator call button: pressing it five times in a row
    doesn't summon five elevators — the state ("elevator has been
    called") doesn't accumulate. A reconciliation routine that isn't
    idempotent would, e.g., attempt a second "create the baseline entry"
    write even though the first call already created it — turning a
    routine self-heal pass into a source of spurious writes (and, worse,
    spurious ownership-stamp writes for the stamp-ownership decisions).
    """

    @pytest.mark.parametrize(
        "desired,observed,ownership",
        [cell[1:4] for cell in _GRID_CELLS],
        ids=[cell[0] for cell in _GRID_CELLS],
    )
    def test_repeated_call_same_inputs_yields_same_decision(
        self, desired: DesiredState, observed: ObservedState, ownership: Ownership
    ) -> None:
        first = _decide_reconciliation_stub(desired, observed, ownership, Resolvability.RESOLVED)
        second = _decide_reconciliation_stub(
            desired, observed, ownership, Resolvability.RESOLVED
        )
        assert first == second


class TestDesiredObservedStateShapeGuards:
    """
    Not part of the eight-value grid itself, but protects the grid's own
    fixtures: a SET_TO_VALUE desired-state or a HEADERED_WITH_VALUE
    observed-state without a concrete value (or a non-SET/non-HEADERED
    state carrying a stray value) would silently produce a malformed grid
    cell — these are shape guards on the input dataclasses themselves.
    """

    def test_set_to_value_requires_a_value(self) -> None:
        with pytest.raises(ValueError):
            DesiredState(DesiredKind.SET_TO_VALUE)

    def test_unset_must_not_carry_a_value(self) -> None:
        with pytest.raises(ValueError):
            DesiredState(DesiredKind.UNSET, "should-not-be-here")

    def test_headered_with_value_requires_a_value(self) -> None:
        with pytest.raises(ValueError):
            ObservedState(ObservedKind.HEADERED_WITH_VALUE)

    def test_headerless_must_not_carry_a_value(self) -> None:
        with pytest.raises(ValueError):
            ObservedState(ObservedKind.HEADERLESS, "should-not-be-here")


class TestConfirmedStateFromEnv:
    """
    ``confirmed_state_from_env`` (D172 R2 MAJOR fix) reconstructs the
    ``ObservedState`` a human confirmed in the Phase 5 (S8) adopt flow from
    ``_NL_CONFIRMED_KIND``/``_NL_CONFIRMED_VALUE`` env vars instead of
    string-interpolating an unvalidated ``.mcp.json`` header value into a
    ``python -c`` payload — the prior approach let a header value containing
    a single quote break out of the string literal and inject code. This
    class was added because two independent review rounds flagged the
    function as having zero test coverage despite its security
    significance, and because its one non-obvious branch (an empty
    ``_NL_CONFIRMED_VALUE`` is a valid, distinct state only when the kind is
    ``HEADERED_WITH_VALUE``; every other kind must carry no value at all,
    per ``ObservedState.__post_init__``) is exactly the kind of thing a
    regression could silently invert.
    """

    def test_headered_with_value_round_trips_the_confirmed_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("_NL_CONFIRMED_KIND", "HEADERED_WITH_VALUE")
        monkeypatch.setenv("_NL_CONFIRMED_VALUE", "ws-real-value-123")

        result = _wc.confirmed_state_from_env()

        assert result == ObservedState(ObservedKind.HEADERED_WITH_VALUE, "ws-real-value-123")
        assert result.value == "ws-real-value-123"

    def test_headered_with_value_and_empty_string_value_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("_NL_CONFIRMED_KIND", "HEADERED_WITH_VALUE")
        monkeypatch.setenv("_NL_CONFIRMED_VALUE", "")

        result = _wc.confirmed_state_from_env()

        assert result == ObservedState(ObservedKind.HEADERED_WITH_VALUE, "")
        assert result.value == ""

    def test_headered_with_value_and_missing_value_env_var_defaults_to_empty_string(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("_NL_CONFIRMED_KIND", "HEADERED_WITH_VALUE")
        monkeypatch.delenv("_NL_CONFIRMED_VALUE", raising=False)

        result = _wc.confirmed_state_from_env()

        assert result == ObservedState(ObservedKind.HEADERED_WITH_VALUE, "")
        assert result.value == ""

    def test_headerless_kind_ignores_a_stray_confirmed_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("_NL_CONFIRMED_KIND", "HEADERLESS")
        monkeypatch.setenv("_NL_CONFIRMED_VALUE", "junk-that-must-be-ignored")

        result = _wc.confirmed_state_from_env()

        assert result == ObservedState(ObservedKind.HEADERLESS)
        assert result.value is None

    def test_absent_kind_ignores_a_stray_confirmed_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("_NL_CONFIRMED_KIND", "ABSENT")
        monkeypatch.setenv("_NL_CONFIRMED_VALUE", "junk-that-must-be-ignored")

        result = _wc.confirmed_state_from_env()

        assert result == ObservedState(ObservedKind.ABSENT)
        assert result.value is None

    def test_missing_confirmed_kind_env_var_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("_NL_CONFIRMED_KIND", raising=False)
        monkeypatch.delenv("_NL_CONFIRMED_VALUE", raising=False)

        with pytest.raises(KeyError):
            _wc.confirmed_state_from_env()

    def test_invalid_confirmed_kind_name_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("_NL_CONFIRMED_KIND", "NOT_A_REAL_KIND")
        monkeypatch.delenv("_NL_CONFIRMED_VALUE", raising=False)

        with pytest.raises(KeyError):
            _wc.confirmed_state_from_env()


# ===========================================================================
# D172 Phase 2 — imperative-shell coverage for the new production functions
# ===========================================================================
#
# The tests above (unchanged from Phase 1) exhaustively cover the pure
# decision core with no I/O. Everything below exercises the real filesystem
# / SQLite / HTTP-boundary functions Phase 2 adds around that core:
# inventory, the resolvability gate, the pagination existence check, the
# reconciliation routine, the settings.json migration routine, and the
# network-free comparison function.


def _write_raw_settings_workspace_id(project_root: str, value: str) -> None:
    """Alias kept local to this section for readability at call sites below
    — identical to ``_write_settings_override`` (D169-era helper, reused
    unmodified)."""
    _write_settings_override(project_root, value)


class TestInventoryPinnedShape:
    """FR-1 — one call returns desired state, ownership, observed
    .mcp.json state, settings.json legacy-override state, and a fresh
    resolvability result, together (Phase 2 AC)."""

    def test_single_call_returns_all_fields_together(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        target = "cccccccc-0000-0000-0000-000000000003"
        legacy = "dddddddd-0000-0000-0000-000000000004"
        _wc.write_desired_state(db_path, _wc.DesiredState(DesiredKind.SET_TO_VALUE, target))
        _seed_owned_entry(project_root, db_path, target)
        _write_raw_settings_workspace_id(project_root, legacy)

        body = json.dumps({"workspace_id": target}).encode("utf-8")
        server, api_base = _start_mock_server(200, body)
        try:
            inventory = _wc.inventory_workspace_state(
                project_root, db_path, api_base=api_base, api_key="key"
            )
        finally:
            server.shutdown()

        assert inventory.desired == DesiredState(DesiredKind.SET_TO_VALUE, target)
        assert inventory.ownership is Ownership.OWNED
        assert inventory.observed == ObservedState(ObservedKind.HEADERED_WITH_VALUE, target)
        assert inventory.observed_error is None
        assert inventory.settings_json_legacy_value == legacy
        assert inventory.settings_json_migration_done is False
        assert inventory.resolvability is Resolvability.RESOLVED
        assert inventory.resolved_workspace_id == target

    def test_malformed_mcp_json_surfaces_as_observed_error_not_exception(
        self, tmp_path: Path
    ) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        (Path(project_root) / ".mcp.json").write_text("{not valid json", encoding="utf-8")

        inventory = _wc.inventory_workspace_state(
            project_root, db_path, api_base=_dead_api_base(), api_key=""
        )
        assert inventory.observed is None
        assert inventory.observed_error is not None


class TestResolvabilityGateRefusesWrites:
    """
    Negative tests (Phase 2 AC) — force UNREACHABLE, and separately force
    the defensively-retained AMBIGUOUS branch, and confirm the gate
    refuses every write in both cases: zero filesystem writes, reconcile()
    itself never invoked.
    """

    def test_unreachable_yields_zero_filesystem_writes(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        target = "88880000-0000-0000-0000-000000000088"
        _wc.write_desired_state(db_path, _wc.DesiredState(DesiredKind.SET_TO_VALUE, target))

        outcome = _wc.gated_reconcile(
            project_root, db_path, api_base=_dead_api_base(), api_key="some-key"
        )
        assert outcome.resolvability is Resolvability.UNREACHABLE
        assert outcome.reconcile is None
        assert not (Path(project_root) / ".mcp.json").exists()

    def test_forced_ambiguous_from_bootstrap_still_refuses_write(self, tmp_path: Path) -> None:
        """
        AMBIGUOUS is the branch a Token-authenticated bootstrap call is
        structurally incapable of producing today (see
        ``_fetch_workspace_id_from_api``'s docstring) — forced here via a
        real mock server returning the 400 ``workspace_not_specified``
        shape, proving the defensive collapse in
        ``resolvability_from_fetch_status`` actually holds end-to-end, not
        merely that the common UNREACHABLE path does.
        """
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        target = "99990000-0000-0000-0000-000000000099"
        _wc.write_desired_state(db_path, _wc.DesiredState(DesiredKind.SET_TO_VALUE, target))

        body = json.dumps(
            {"error": {"code": "workspace_not_specified", "message": "2 memberships"}}
        ).encode("utf-8")
        server, api_base = _start_mock_server(400, body)
        try:
            outcome = _wc.gated_reconcile(project_root, db_path, api_base=api_base, api_key="key")
        finally:
            server.shutdown()

        assert outcome.resolvability is Resolvability.UNREACHABLE
        assert outcome.reconcile is None
        assert not (Path(project_root) / ".mcp.json").exists()


class TestReconcileBasicTransitions:
    """Imperative-shell coverage of ``reconcile`` against a real filesystem
    and real SQLite db — the grid's decisions, actually executed."""

    def test_create_baseline_when_absent_and_unset(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)

        outcome = _wc.reconcile(project_root, db_path, DesiredState(DesiredKind.UNSET))
        assert outcome.decision is Decision.CREATE_BASELINE
        assert outcome.write_result == _wc.WriteResult.SUCCESS
        entry = _read_mcp_json(project_root)["mcpServers"]["neuroloom"]
        assert _wc._WORKSPACE_HEADER not in entry.get("headers", {})

    def test_create_with_header_when_absent_and_set(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        target = "44440000-0000-0000-0000-000000000044"

        outcome = _wc.reconcile(
            project_root, db_path, DesiredState(DesiredKind.SET_TO_VALUE, target)
        )
        assert outcome.decision is Decision.CREATE_WITH_HEADER
        entry = _read_mcp_json(project_root)["mcpServers"]["neuroloom"]
        assert entry["headers"][_wc._WORKSPACE_HEADER] == target
        assert _wc._is_mcp_entry_owned(db_path) is True

    def test_idempotent_second_call_is_noop(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        target = "55550000-0000-0000-0000-000000000055"
        desired = DesiredState(DesiredKind.SET_TO_VALUE, target)

        _wc.reconcile(project_root, db_path, desired)
        second = _wc.reconcile(project_root, db_path, desired)
        assert second.decision is Decision.NO_OP
        assert second.write_result is None

    def test_adoption_decision_never_executed_by_reconcile(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        existing = "77770000-0000-0000-0000-000000000077"
        target = "66660000-0000-0000-0000-000000000066"
        _write_mcp_json(
            project_root,
            {
                "mcpServers": {
                    "neuroloom": {
                        "type": _wc._MCP_SERVER_TYPE,
                        "url": _wc._MCP_SERVER_URL,
                        "headers": {_wc._WORKSPACE_HEADER: existing},
                    }
                }
            },
        )
        # Deliberately NOT marked owned.

        outcome = _wc.reconcile(
            project_root, db_path, DesiredState(DesiredKind.SET_TO_VALUE, target)
        )
        assert outcome.decision is Decision.STAMP_OWNERSHIP_WRITE_HEADER
        assert outcome.write_result is None
        assert outcome.error is not None
        entry = _read_mcp_json(project_root)["mcpServers"]["neuroloom"]
        assert entry["headers"][_wc._WORKSPACE_HEADER] == existing
        assert _wc._is_mcp_entry_owned(db_path) is False


class TestMigrateSettingsJsonOverride:
    """FR-8 — the sole settings.json reader/writer for routing purposes."""

    def test_nothing_to_migrate_marks_done(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)

        result = _wc.migrate_settings_json_override(
            project_root, db_path, api_base=_dead_api_base(), api_key=""
        )
        assert result.outcome is _wc.MigrationOutcome.NOTHING_TO_MIGRATE
        assert _wc.is_settings_json_migration_done(db_path) is True

    def test_already_done_short_circuits_without_touching_settings_json(
        self, tmp_path: Path
    ) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        _wc._mark_settings_json_migration_done(db_path)
        legacy = "11110000-0000-0000-0000-000000000011"
        _write_raw_settings_workspace_id(project_root, legacy)

        result = _wc.migrate_settings_json_override(
            project_root, db_path, api_base=_dead_api_base(), api_key="key"
        )
        assert result.outcome is _wc.MigrationOutcome.ALREADY_DONE
        settings_path = Path(project_root) / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        assert settings["pluginConfigs"][_wc._PLUGIN_ID]["options"]["workspace_id"] == legacy

    def test_verify_failed_leaves_settings_json_untouched_and_marker_unset(
        self, tmp_path: Path
    ) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        legacy = "22220000-0000-0000-0000-000000000022"
        _write_raw_settings_workspace_id(project_root, legacy)

        result = _wc.migrate_settings_json_override(
            project_root, db_path, api_base=_dead_api_base(), api_key="key"
        )
        assert result.outcome is _wc.MigrationOutcome.VERIFY_FAILED
        assert _wc.is_settings_json_migration_done(db_path) is False
        settings_path = Path(project_root) / ".claude" / "settings.json"
        assert settings_path.exists()
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        assert settings["pluginConfigs"][_wc._PLUGIN_ID]["options"]["workspace_id"] == legacy

    def test_migrated_writes_desired_state_and_deletes_legacy_key(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        legacy = "33330000-0000-0000-0000-000000000033"
        _write_raw_settings_workspace_id(project_root, legacy)

        body = json.dumps({"workspace_id": legacy}).encode("utf-8")
        server, api_base = _start_mock_server(200, body)
        try:
            result = _wc.migrate_settings_json_override(
                project_root, db_path, api_base=api_base, api_key="key"
            )
        finally:
            server.shutdown()

        assert result.outcome is _wc.MigrationOutcome.MIGRATED
        assert result.workspace_id == legacy
        assert _wc.read_desired_state(db_path) == DesiredState(DesiredKind.SET_TO_VALUE, legacy)
        assert _wc.is_settings_json_migration_done(db_path) is True

        settings_path = Path(project_root) / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        assert "workspace_id" not in settings["pluginConfigs"][_wc._PLUGIN_ID]["options"]


class TestReadSettingsJsonLegacyValueMalformed:
    """
    D172 completion review, Finding #5: the Old-API Test Disposition table
    justified deleting the old ``TestReadManualOverrideUuidValidation``
    test with a promise that ``_read_settings_json_legacy_value`` (a fresh
    D172 function, not a rewrite of the old one) would get its own fresh
    UUID-validation test. That promised test was never written -- this
    class is it. Covers both ``_read_settings_json_legacy_value`` directly
    and the full ``migrate_settings_json_override`` path a malformed
    legacy value flows into.
    """

    def test_non_uuid_legacy_value_returns_none(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        _write_raw_override(project_root, "not-a-uuid-at-all")

        assert _wc._read_settings_json_legacy_value(project_root) is None

    def test_empty_string_legacy_value_returns_none(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        _write_raw_override(project_root, "")

        assert _wc._read_settings_json_legacy_value(project_root) is None

    def test_non_string_legacy_value_returns_none(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        _write_raw_override(project_root, 12345)

        assert _wc._read_settings_json_legacy_value(project_root) is None

    def test_malformed_legacy_value_never_reaches_migration_as_a_desired_state(
        self, tmp_path: Path
    ) -> None:
        """
        A garbage, non-UUID legacy value must not raise, and must not be
        silently accepted as if it were a real workspace id -- the
        migration routine must treat it exactly like "nothing to migrate":
        marks the one-time migration done, writes no desired state, and
        never attempts a resolvability round-trip against the garbage
        value (proven by pointing at a dead API base/key -- if the
        malformed value somehow reached the resolvability check, this
        would return VERIFY_FAILED instead of NOTHING_TO_MIGRATE, and the
        migration-done marker would stay unset).
        """
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        _write_raw_override(project_root, "definitely-not-a-uuid")

        result = _wc.migrate_settings_json_override(
            project_root, db_path, api_base=_dead_api_base(), api_key="key"
        )

        assert result.outcome is _wc.MigrationOutcome.NOTHING_TO_MIGRATE
        assert result.workspace_id is None
        assert _wc.is_settings_json_migration_done(db_path) is True
        assert _wc.read_desired_state(db_path) == DesiredState(DesiredKind.UNSET)
        # The garbage value is left untouched in settings.json (migration
        # never ran the delete step, since NOTHING_TO_MIGRATE is not
        # MIGRATED) -- confirms this took the "nothing found" path, not a
        # silent accept-and-consume of the garbage value.
        settings_path = Path(project_root) / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        assert (
            settings["pluginConfigs"][_wc._PLUGIN_ID]["options"]["workspace_id"]
            == "definitely-not-a-uuid"
        )


class TestNetworkFreeComparison:
    """
    R2-6 — network-free desired-vs-observed comparison. Correctness cases
    plus the load-bearing zero-network-calls proof (networking mocked to
    explode; the function must still return the right answer).
    """

    def test_converged_when_headerless_and_never_set(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        _seed_owned_entry(project_root, db_path, None)

        result = _wc.compare_desired_vs_observed(project_root, db_path)
        assert result.comparison is _wc.RoutingComparison.CONVERGED

    def test_out_of_sync_when_desired_set_but_headerless(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        _seed_owned_entry(project_root, db_path, None)
        target = "eeeeeeee-0000-0000-0000-000000000005"
        _wc.write_desired_state(db_path, DesiredState(DesiredKind.SET_TO_VALUE, target))

        result = _wc.compare_desired_vs_observed(project_root, db_path)
        assert result.comparison is _wc.RoutingComparison.OUT_OF_SYNC

    def test_unowned_conflict_reported_even_if_value_matches(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        target = "ffffffff-0000-0000-0000-000000000006"
        _write_mcp_json(
            project_root,
            {
                "mcpServers": {
                    "neuroloom": {
                        "type": _wc._MCP_SERVER_TYPE,
                        "url": _wc._MCP_SERVER_URL,
                        "headers": {_wc._WORKSPACE_HEADER: target},
                    }
                }
            },
        )
        _wc.write_desired_state(db_path, DesiredState(DesiredKind.SET_TO_VALUE, target))
        # Deliberately NOT marked owned.

        result = _wc.compare_desired_vs_observed(project_root, db_path)
        assert result.comparison is _wc.RoutingComparison.UNOWNED_CONFLICT

    def test_malformed_mcp_json_reported_as_malformed(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        (Path(project_root) / ".mcp.json").write_text("{not valid", encoding="utf-8")

        result = _wc.compare_desired_vs_observed(project_root, db_path)
        assert result.comparison is _wc.RoutingComparison.MALFORMED

    def test_zero_network_calls_even_with_networking_mocked_to_explode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        _seed_owned_entry(project_root, db_path, None)

        def _explode(*args: object, **kwargs: object) -> None:
            raise AssertionError("compare_desired_vs_observed must never touch the network")

        monkeypatch.setattr(urllib.request, "urlopen", _explode)

        result = _wc.compare_desired_vs_observed(project_root, db_path)
        assert result.comparison is _wc.RoutingComparison.CONVERGED


# ---------------------------------------------------------------------------
# verify_target_workspace_exists — pagination existence/name check
# ---------------------------------------------------------------------------
#
# Basic sanity coverage only (Phase 2 AC) — the authoritative multi-page
# correctness proof, using this repo's real-HTTPServer convention with
# genuine per-page offset bookkeeping, is Phase 5's job (R3-1).


class _FullPageHandler(http.server.BaseHTTPRequestHandler):
    """
    Always returns a full page of synthetic (never-matching) workspace
    records, regardless of the requested offset — enough to trigger
    SCAN_TRUNCATED deterministically without this phase's own sanity test
    needing genuine per-offset bookkeeping (that's Phase 5's job).
    """

    server: Any

    def do_GET(self) -> None:  # noqa: N802
        page_size: int = getattr(self.server, "page_size", _wc._WORKSPACES_PAGE_SIZE)
        records = [{"id": f"filler-{i}", "name": f"Filler {i}"} for i in range(page_size)]
        body = json.dumps(records).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass


class TestVerifyTargetWorkspaceExists:
    def test_found_on_first_page(self) -> None:
        body = json.dumps([{"id": "target-id", "name": "Target WS"}]).encode("utf-8")
        server, api_base = _start_mock_server(200, body)
        try:
            result = _wc.verify_target_workspace_exists(api_base, "key", "target-id")
            assert result.status is _wc.WorkspaceExistenceStatus.FOUND
            assert result.name == "Target WS"
        finally:
            server.shutdown()

    def test_not_found_on_short_final_page(self) -> None:
        body = json.dumps([{"id": "other-id", "name": "Other"}]).encode("utf-8")
        server, api_base = _start_mock_server(200, body)
        try:
            result = _wc.verify_target_workspace_exists(api_base, "key", "target-id")
            assert result.status is _wc.WorkspaceExistenceStatus.NOT_FOUND
        finally:
            server.shutdown()

    def test_unreachable_on_dead_server(self) -> None:
        result = _wc.verify_target_workspace_exists(_dead_api_base(), "key", "target-id")
        assert result.status is _wc.WorkspaceExistenceStatus.UNREACHABLE

    def test_scan_truncated_when_cap_hit_with_full_pages(self) -> None:
        port = _free_port()
        server = http.server.HTTPServer(("127.0.0.1", port), _FullPageHandler)
        server.page_size = _wc._WORKSPACES_PAGE_SIZE  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = _wc.verify_target_workspace_exists(
                f"http://127.0.0.1:{port}", "key", "never-present-target"
            )
            assert result.status is _wc.WorkspaceExistenceStatus.SCAN_TRUNCATED
        finally:
            server.shutdown()

    def test_non_dict_records_on_a_page_are_skipped_not_fatal(self) -> None:
        body = json.dumps(["not-a-dict", {"id": "target-id", "name": "Target"}]).encode("utf-8")
        server, api_base = _start_mock_server(200, body)
        try:
            result = _wc.verify_target_workspace_exists(api_base, "key", "target-id")
            assert result.status is _wc.WorkspaceExistenceStatus.FOUND
        finally:
            server.shutdown()


class TestVerifyTargetWorkspaceExistsLiveSmoke:
    """
    Benign live smoke test (Phase 2 AC) — confirms GET /workspaces/'s real
    shape/auth/pagination-parameter contract against the deployed API,
    using this project's own real per-project key to resolve its own
    workspace_id first (a page-1, already-existing target — no seeding, no
    new workspace creation). Skipped entirely when the credential isn't
    present in the environment (CI, or any machine other than this one).
    """

    @pytest.mark.skipif(
        not os.environ.get("NEUROLOOM_SLEEVED_API_KEY"),
        reason="requires NEUROLOOM_SLEEVED_API_KEY for a benign live-API smoke test",
    )
    def test_live_verify_target_workspace_exists_against_deployed_api(self) -> None:
        api_key = os.environ["NEUROLOOM_SLEEVED_API_KEY"]
        api_base = "https://api.neuroloom.dev"

        bootstrap = _wc._fetch_workspace_id_from_api(api_base, api_key)
        if bootstrap.status is not _wc.WorkspaceFetchStatus.RESOLVED:
            pytest.skip(
                "NEUROLOOM_SLEEVED_API_KEY did not resolve against the deployed API "
                f"(status={bootstrap.status.value}) — credential appears stale/invalid "
                "in this environment, not a defect in verify_target_workspace_exists "
                "itself; skipping rather than asserting against a credential this test "
                "cannot fix or fabricate."
            )
        assert bootstrap.workspace_id

        result = _wc.verify_target_workspace_exists(api_base, api_key, bootstrap.workspace_id)
        assert result.status is _wc.WorkspaceExistenceStatus.FOUND
        assert result.name


# ===========================================================================
# D172 Phase 5 — R3-1: synthetic multi-page pagination correctness
# ===========================================================================
#
# Distinct from TestVerifyTargetWorkspaceExists above (Phase 2's basic
# sanity coverage, using a single-page mock server and a same-content-
# regardless-of-offset handler for the cap-hit shape). This section uses a
# handler with *genuine* per-offset bookkeeping — a request for offset=400
# gets records[400:600], not whatever offset=0 would have returned — which
# is the only way to actually prove the scanner walks forward page by page
# instead of, say, silently re-requesting page 1 five times.


class _PaginatedRecordsHandler(http.server.BaseHTTPRequestHandler):
    """Returns a real slice of ``server.records`` based on the request's
    ``offset``/``limit`` query parameters."""

    server: Any  # narrowed in tests

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        offset = int(params.get("offset", ["0"])[0])
        limit = int(params.get("limit", ["200"])[0])
        records: list[dict[str, Any]] = getattr(self.server, "records", [])
        page = records[offset : offset + limit]
        body = json.dumps(page).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass


def _start_paginated_server(records: list[dict[str, Any]]) -> tuple[http.server.HTTPServer, str]:
    port = _free_port()
    server = http.server.HTTPServer(("127.0.0.1", port), _PaginatedRecordsHandler)
    server.records = records  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}"


def _filler_records(count: int, start: int = 0) -> list[dict[str, Any]]:
    return [{"id": f"filler-{i}", "name": f"Filler {i}"} for i in range(start, start + count)]


class TestVerifyTargetWorkspaceExistsPaginationCorrectness:
    """
    R3-1 (Phase 5, MAJOR) — the authoritative multi-page pagination-
    correctness proof, using a real ``http.server.HTTPServer`` with genuine
    per-offset bookkeeping. Three required cases, each its own test.
    """

    def test_target_on_page_3_of_5_resolves_via_paginate_until_found(self) -> None:
        """
        Proves the scan actually continues past page 1 rather than
        stopping early: the target sits in page 3's offset range
        (400-599), with pages 1, 2, 4, and 5 all full of non-matching
        filler that would never be requested if the scanner stopped after
        page 1 or 2.
        """
        page_size = _wc._WORKSPACES_PAGE_SIZE
        records = _filler_records(page_size * 2)  # pages 1-2
        records += _filler_records(page_size - 1)  # page 3, minus one slot
        records.append({"id": "target-id", "name": "Target WS"})  # the target, page 3
        records += _filler_records(page_size * 2)  # pages 4-5
        assert len(records) == page_size * 5

        server, api_base = _start_paginated_server(records)
        try:
            result = _wc.verify_target_workspace_exists(api_base, "key", "target-id")
            assert result.status is _wc.WorkspaceExistenceStatus.FOUND
            assert result.name == "Target WS"
        finally:
            server.shutdown()

    def test_target_absent_across_five_full_pages_is_not_found(self) -> None:
        """
        Every page is fetched successfully, none contains the target, and
        the final page is short (fewer than 200 rows) — a genuine
        end-of-data signal, so the answer is the strong claim NOT_FOUND,
        never the weaker SCAN_TRUNCATED.
        """
        page_size = _wc._WORKSPACES_PAGE_SIZE
        records = _filler_records(page_size * 4)  # 4 full pages, never matching
        records += _filler_records(50, start=page_size * 4)  # short 5th page
        assert len(records) < page_size * 5

        server, api_base = _start_paginated_server(records)
        try:
            result = _wc.verify_target_workspace_exists(api_base, "key", "never-present-target")
            assert result.status is _wc.WorkspaceExistenceStatus.NOT_FOUND
        finally:
            server.shutdown()

    def test_cap_exhausted_with_all_pages_full_is_scan_truncated(self) -> None:
        """
        All 5 pages come back exactly full (200 records each), never
        matching — no signal a 6th page would be empty. This is the case
        that makes the SCAN_TRUNCATED-vs-NOT_FOUND distinction meaningful
        and testable, like searching only the first 1,000 entries of a
        phone book and reaching the cap still finding full pages: "not
        found in the part checked" is not the same claim as "confirmed
        absent." (MINOR 1: this test also **is** the unverified-state
        coverage for the "scan truncated" branch — no separate test is
        needed for that state.)
        """
        page_size = _wc._WORKSPACES_PAGE_SIZE
        records = _filler_records(page_size * 5)  # 5 full pages, never matching
        assert len(records) == page_size * 5

        server, api_base = _start_paginated_server(records)
        try:
            result = _wc.verify_target_workspace_exists(api_base, "key", "never-present-target")
            assert result.status is _wc.WorkspaceExistenceStatus.SCAN_TRUNCATED
        finally:
            server.shutdown()


class TestVerifyTargetWorkspaceExistsUnexpectedShape:
    """
    S11 — AMBIGUOUS_MEMBERSHIP / "unexpected response shape" from the
    workspace-existence lookup (spec §6): treated identically to S10
    (UNREACHABLE) — never distinguished to the user as a different failure
    class, matching ``WorkspaceFetchStatus.UNREACHABLE``'s deliberately
    non-actionable bucket design one layer up. Distinct scenario from
    ``TestVerifyTargetWorkspaceExists::test_unreachable_on_dead_server``
    (a genuine network failure, no server at all) — this is a live 200
    response with a body shape ``_fetch_workspaces_page`` doesn't
    recognize.
    """

    def test_non_list_body_is_unreachable(self) -> None:
        body = json.dumps({"not": "a list, an unexpected shape"}).encode("utf-8")
        server, api_base = _start_mock_server(200, body)
        try:
            result = _wc.verify_target_workspace_exists(api_base, "key", "target-id")
            assert result.status is _wc.WorkspaceExistenceStatus.UNREACHABLE
        finally:
            server.shutdown()


# ===========================================================================
# D172 Phase 5 — one integration test per §5 lifecycle-transition-table row
# ===========================================================================


class TestLifecycleTableRows:
    """
    One explicit, named, traceable test per lifecycle-table row (SET,
    CHANGE, CLEAR, ADOPT, MIGRATE, CONVERGED no-op, UNOWNED-CONFLICT,
    DORMANT-SKIP, BASELINE-ENSURE). Several rows are also exercised
    incidentally by earlier test classes (cross-referenced below) — this
    class exists so every row has its own dedicated assertion, not to
    introduce redundant coverage for its own sake.
    """

    def test_set_row_owned_headerless_to_headered(self, tmp_path: Path) -> None:
        """SET: owned, headerless, desired becomes SET_TO_VALUE -> WRITE_HEADER."""
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        _seed_owned_entry(project_root, db_path, None)
        target = "10000000-0000-0000-0000-00000000se01"

        outcome = _wc.reconcile(
            project_root, db_path, DesiredState(DesiredKind.SET_TO_VALUE, target)
        )
        assert outcome.decision is Decision.WRITE_HEADER
        assert outcome.write_result == _wc.WriteResult.SUCCESS
        entry = _read_mcp_json(project_root)["mcpServers"]["neuroloom"]
        assert entry["headers"][_wc._WORKSPACE_HEADER] == target

    def test_change_row_owned_headered_to_different_value(self, tmp_path: Path) -> None:
        """CHANGE: owned, headered(A), desired becomes SET_TO_VALUE(B) -> WRITE_HEADER."""
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        value_a = "10000000-0000-0000-0000-00000000cga"
        value_b = "10000000-0000-0000-0000-00000000cgb"
        _seed_owned_entry(project_root, db_path, value_a)

        outcome = _wc.reconcile(
            project_root, db_path, DesiredState(DesiredKind.SET_TO_VALUE, value_b)
        )
        assert outcome.decision is Decision.WRITE_HEADER
        entry = _read_mcp_json(project_root)["mcpServers"]["neuroloom"]
        assert entry["headers"][_wc._WORKSPACE_HEADER] == value_b

    def test_clear_row_owned_headered_to_headerless(self, tmp_path: Path) -> None:
        """CLEAR: owned, headered, desired becomes EXPLICITLY_CLEARED -> CLEAR_HEADER."""
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        target = "10000000-0000-0000-0000-00000000cl01"
        _seed_owned_entry(project_root, db_path, target)

        outcome = _wc.reconcile(project_root, db_path, DesiredState(DesiredKind.EXPLICITLY_CLEARED))
        assert outcome.decision is Decision.CLEAR_HEADER
        entry = _read_mcp_json(project_root)["mcpServers"]["neuroloom"]
        assert _wc._WORKSPACE_HEADER not in entry.get("headers", {})

    def test_adopt_row_unowned_headered_stamps_ownership(self, tmp_path: Path) -> None:
        """ADOPT: unowned, headered, human consents (ADOPT_AS_IS) -> ownership
        stamped, header kept as-is (see TestAdoptUnownedEntry for the full
        AdoptionChoice/AdoptionOutcome state machine)."""
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        existing = "10000000-0000-0000-0000-00000000ad01"
        _write_mcp_json(
            project_root,
            {
                "mcpServers": {
                    "neuroloom": {
                        "type": _wc._MCP_SERVER_TYPE,
                        "url": _wc._MCP_SERVER_URL,
                        "headers": {_wc._WORKSPACE_HEADER: existing},
                    }
                }
            },
        )
        observed = _wc.read_observed_state(project_root)

        result = _wc.adopt_unowned_entry(
            project_root, db_path, _wc.AdoptionChoice.ADOPT_AS_IS, observed
        )
        assert result.outcome is _wc.AdoptionOutcome.ADOPTED
        assert _wc._is_mcp_entry_owned(db_path) is True
        entry = _read_mcp_json(project_root)["mcpServers"]["neuroloom"]
        assert entry["headers"][_wc._WORKSPACE_HEADER] == existing

    def test_migrate_row_legacy_value_becomes_desired_state(self, tmp_path: Path) -> None:
        """MIGRATE: see TestMigrateSettingsJsonOverride (Phase 2) for the
        full outcome matrix — this pins the named row explicitly."""
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        legacy = "10000000-0000-0000-0000-000000000001"
        _write_raw_settings_workspace_id(project_root, legacy)

        body = json.dumps({"workspace_id": legacy}).encode("utf-8")
        server, api_base = _start_mock_server(200, body)
        try:
            result = _wc.migrate_settings_json_override(
                project_root, db_path, api_base=api_base, api_key="key"
            )
        finally:
            server.shutdown()
        assert result.outcome is _wc.MigrationOutcome.MIGRATED
        assert _wc.read_desired_state(db_path) == DesiredState(DesiredKind.SET_TO_VALUE, legacy)

    def test_converged_row_is_a_true_noop(self, tmp_path: Path) -> None:
        """CONVERGED no-op: owned, headered(V), desired already SET_TO_VALUE(V) -> NO_OP, zero writes."""
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        target = "10000000-0000-0000-0000-00000000cv01"
        _seed_owned_entry(project_root, db_path, target)
        mcp_path = Path(project_root) / ".mcp.json"
        old_ts = 1_000_000_000.0
        os.utime(mcp_path, (old_ts, old_ts))

        outcome = _wc.reconcile(
            project_root, db_path, DesiredState(DesiredKind.SET_TO_VALUE, target)
        )
        assert outcome.decision is Decision.NO_OP
        assert outcome.write_result is None
        assert os.stat(mcp_path).st_mtime == old_ts  # untouched, not merely unchanged-by-value

    def test_unowned_conflict_row_via_compare_desired_vs_observed(self, tmp_path: Path) -> None:
        """UNOWNED-CONFLICT: see TestNetworkFreeComparison
        ::test_unowned_conflict_reported_even_if_value_matches (Phase 2) for
        the comparison-function angle; this pins it via the reconciliation
        side (WARN_ONLY) instead."""
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        existing = "10000000-0000-0000-0000-00000000uc01"
        _write_mcp_json(
            project_root,
            {
                "mcpServers": {
                    "neuroloom": {
                        "type": _wc._MCP_SERVER_TYPE,
                        "url": _wc._MCP_SERVER_URL,
                        "headers": {_wc._WORKSPACE_HEADER: existing},
                    }
                }
            },
        )
        # Deliberately NOT marked owned.
        different_target = "10000000-0000-0000-0000-00000000uc02"

        outcome = _wc.reconcile(
            project_root, db_path, DesiredState(DesiredKind.SET_TO_VALUE, different_target)
        )
        assert outcome.decision is Decision.STAMP_OWNERSHIP_WRITE_HEADER
        assert outcome.error is not None
        entry = _read_mcp_json(project_root)["mcpServers"]["neuroloom"]
        assert entry["headers"][_wc._WORKSPACE_HEADER] == existing  # untouched

    def test_dormant_skip_row_via_resolvability_gate(self, tmp_path: Path) -> None:
        """DORMANT-SKIP: see TestResolvabilityGateRefusesWrites (Phase 2)
        for the resolvability-gate angle; this pins the caller-side
        short-circuit shape explicitly — reconcile() is never even called."""
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        _wc.write_desired_state(
            db_path, DesiredState(DesiredKind.SET_TO_VALUE, "10000000-0000-0000-0000-00000000ds01")
        )

        outcome = _wc.gated_reconcile(project_root, db_path, api_base=_dead_api_base(), api_key="")
        assert outcome.resolvability is Resolvability.UNREACHABLE
        assert outcome.reconcile is None
        assert not (Path(project_root) / ".mcp.json").exists()

    def test_baseline_ensure_row_both_create_shapes(self, tmp_path: Path) -> None:
        """BASELINE-ENSURE: see TestReconcileBasicTransitions and
        TestBranchCHeaderlessBaselineCreation (Phase 2/5) for CREATE_BASELINE
        and CREATE_WITH_HEADER individually; this pins both outcomes of the
        same row side by side."""
        project_root_a = str(tmp_path / "a")
        project_root_b = str(tmp_path / "b")
        Path(project_root_a).mkdir()
        Path(project_root_b).mkdir()
        db_path_a = Path(project_root_a) / ".neuroloom.db"
        db_path_b = Path(project_root_b) / ".neuroloom.db"
        _init_db(db_path_a)
        _init_db(db_path_b)
        target = "10000000-0000-0000-0000-00000000be01"

        baseline = _wc.reconcile(project_root_a, db_path_a, DesiredState(DesiredKind.UNSET))
        assert baseline.decision is Decision.CREATE_BASELINE

        with_header = _wc.reconcile(
            project_root_b, db_path_b, DesiredState(DesiredKind.SET_TO_VALUE, target)
        )
        assert with_header.decision is Decision.CREATE_WITH_HEADER


# ===========================================================================
# D172 Phase 5 — R2-8: five crash-between-writes simulations
# ===========================================================================


class TestCrashBetweenWrites:
    """
    Five crash-between-writes simulations. Each proves the interrupted
    state is either inert (no partial/corrupt write) or self-healing on the
    next run — never a silently wrong, undetectable state.
    """

    def test_set_crash_after_desired_state_write_self_heals_next_reconcile(
        self, tmp_path: Path
    ) -> None:
        """
        SET: the command flow's own two-step shape is write_desired_state()
        (persist the human's decision) followed by reconcile() (apply it to
        .mcp.json). A crash between them leaves .neuroloom.db updated but
        .mcp.json untouched — the next reconcile() call (automatic session
        start, or a retried command) reads the already-persisted desired
        state fresh and converges .mcp.json to match.
        """
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        _seed_owned_entry(project_root, db_path, None)
        target = "20000000-0000-0000-0000-00000000se01"

        _wc.write_desired_state(db_path, DesiredState(DesiredKind.SET_TO_VALUE, target))
        # ... "crash" here — reconcile() (step 2 of 2) never runs this session.
        entry = _read_mcp_json(project_root)["mcpServers"]["neuroloom"]
        assert _wc._WORKSPACE_HEADER not in entry.get("headers", {})

        recovered_desired = _wc.read_desired_state(db_path)
        assert recovered_desired == DesiredState(DesiredKind.SET_TO_VALUE, target)
        outcome = _wc.reconcile(project_root, db_path, recovered_desired)
        assert outcome.decision is Decision.WRITE_HEADER
        entry = _read_mcp_json(project_root)["mcpServers"]["neuroloom"]
        assert entry["headers"][_wc._WORKSPACE_HEADER] == target

    def test_change_crash_after_desired_state_write_self_heals_next_reconcile(
        self, tmp_path: Path
    ) -> None:
        """CHANGE: same shape as SET, starting from an already-headered entry."""
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        old_value = "20000000-0000-0000-0000-00000000cga"
        new_value = "20000000-0000-0000-0000-00000000cgb"
        _seed_owned_entry(project_root, db_path, old_value)

        _wc.write_desired_state(db_path, DesiredState(DesiredKind.SET_TO_VALUE, new_value))
        # ... "crash" here.
        entry = _read_mcp_json(project_root)["mcpServers"]["neuroloom"]
        assert entry["headers"][_wc._WORKSPACE_HEADER] == old_value  # still the pre-crash value

        recovered_desired = _wc.read_desired_state(db_path)
        outcome = _wc.reconcile(project_root, db_path, recovered_desired)
        assert outcome.decision is Decision.WRITE_HEADER
        entry = _read_mcp_json(project_root)["mcpServers"]["neuroloom"]
        assert entry["headers"][_wc._WORKSPACE_HEADER] == new_value

    def test_clear_crash_after_desired_state_write_self_heals_next_reconcile(
        self, tmp_path: Path
    ) -> None:
        """CLEAR: same shape as SET, converging to headerless instead."""
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        target = "20000000-0000-0000-0000-00000000cl01"
        _seed_owned_entry(project_root, db_path, target)

        _wc.write_desired_state(db_path, DesiredState(DesiredKind.EXPLICITLY_CLEARED))
        # ... "crash" here.
        entry = _read_mcp_json(project_root)["mcpServers"]["neuroloom"]
        assert entry["headers"][_wc._WORKSPACE_HEADER] == target  # still headered

        recovered_desired = _wc.read_desired_state(db_path)
        assert recovered_desired == DesiredState(DesiredKind.EXPLICITLY_CLEARED)
        outcome = _wc.reconcile(project_root, db_path, recovered_desired)
        assert outcome.decision is Decision.CLEAR_HEADER
        entry = _read_mcp_json(project_root)["mcpServers"]["neuroloom"]
        assert _wc._WORKSPACE_HEADER not in entry.get("headers", {})

    def test_adopt_crash_between_mcp_json_write_and_ownership_stamp(self, tmp_path: Path) -> None:
        """
        ADOPT: adopt_unowned_entry's own documented ordering — the
        .mcp.json write happens, THEN ownership is stamped (never the
        reverse — see its docstring). Reproduced here by calling the two
        steps directly (adopt_unowned_entry itself can't be interrupted
        mid-function from a test) to get the exact interrupted state: bytes
        already on disk, ownership not yet recorded — under-reports
        ownership, never over-reports it.
        """
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        existing = "20000000-0000-0000-0000-00000000ad01"
        _write_mcp_json(
            project_root,
            {
                "mcpServers": {
                    "neuroloom": {
                        "type": _wc._MCP_SERVER_TYPE,
                        "url": _wc._MCP_SERVER_URL,
                        "headers": {_wc._WORKSPACE_HEADER: existing},
                    }
                }
            },
        )

        write_result = _wc._write_literal_mcp_json_entry(
            project_root, db_path, existing, create_if_missing=False, bypass_unowned_gate=True
        )
        assert write_result == _wc.WriteResult.SUCCESS
        # ... "crash" here — the ownership stamp never runs this session.
        assert _wc._is_mcp_entry_owned(db_path) is False

        # Recovery: a retried adopt (the human re-confirms against the
        # entry's current — already-correct — state) completes the stamp.
        observed = _wc.read_observed_state(project_root)
        result = _wc.adopt_unowned_entry(
            project_root, db_path, _wc.AdoptionChoice.ADOPT_AS_IS, observed
        )
        assert result.outcome is _wc.AdoptionOutcome.ADOPTED
        assert _wc._is_mcp_entry_owned(db_path) is True

    def test_migrate_crash_between_db_write_and_settings_json_delete(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        MIGRATE: migrate_settings_json_override's documented ordering — the
        .neuroloom.db desired-state write and migration-done marker happen
        FIRST, the .claude/settings.json key delete SECOND (a
        git-committable file — higher stakes than the other four, per
        R2-8). Forces ``_delete_settings_json_legacy_key`` to simulate the
        delete never running, and asserts the documented "harmless
        leftover, not a source of drift" claim actually holds end to end.
        """
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        legacy = "20000000-0000-0000-0000-000000000001"
        _write_raw_settings_workspace_id(project_root, legacy)

        def _simulate_crash(*args: object, **kwargs: object) -> bool:
            return False  # the delete "never ran"

        monkeypatch.setattr(_wc, "_delete_settings_json_legacy_key", _simulate_crash)

        body = json.dumps({"workspace_id": legacy}).encode("utf-8")
        server, api_base = _start_mock_server(200, body)
        try:
            result = _wc.migrate_settings_json_override(
                project_root, db_path, api_base=api_base, api_key="key"
            )
        finally:
            server.shutdown()
        assert result.outcome is _wc.MigrationOutcome.MIGRATED

        # The "crash": settings.json still has the leftover key.
        settings_path = Path(project_root) / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        assert settings["pluginConfigs"][_wc._PLUGIN_ID]["options"]["workspace_id"] == legacy

        # But the leftover is inert: desired state and the migration-done
        # marker are already correct in .neuroloom.db.
        assert _wc.read_desired_state(db_path) == DesiredState(DesiredKind.SET_TO_VALUE, legacy)
        assert _wc.is_settings_json_migration_done(db_path) is True

        # A retried migration call never re-reads the leftover.
        retry = _wc.migrate_settings_json_override(
            project_root, db_path, api_base=_dead_api_base(), api_key=""
        )
        assert retry.outcome is _wc.MigrationOutcome.ALREADY_DONE

        # reconcile() converges .mcp.json from the correct desired state in
        # .neuroloom.db — it has no settings.json code path whatsoever, so
        # the leftover can never be a source of drift.
        outcome = _wc.reconcile(project_root, db_path, _wc.read_desired_state(db_path))
        assert outcome.decision is Decision.CREATE_WITH_HEADER
        entry = _read_mcp_json(project_root)["mcpServers"]["neuroloom"]
        assert entry["headers"][_wc._WORKSPACE_HEADER] == legacy


# ===========================================================================
# D172 Phase 5 — adoption flow: full AdoptionChoice/AdoptionOutcome
# coverage, decline-hash suppress/resurface (R2-9), warning-independence,
# and the TOCTOU regression test
# ===========================================================================


class TestAdoptUnownedEntry:
    """Every currently-unverified AdoptionChoice/AdoptionOutcome combination."""

    def test_adopt_as_is_keeps_header_and_stamps_ownership(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        existing = "30000000-0000-0000-0000-00000000ai01"
        _write_mcp_json(
            project_root,
            {
                "mcpServers": {
                    "neuroloom": {
                        "type": _wc._MCP_SERVER_TYPE,
                        "url": _wc._MCP_SERVER_URL,
                        "headers": {_wc._WORKSPACE_HEADER: existing},
                    }
                }
            },
        )
        observed = _wc.read_observed_state(project_root)

        result = _wc.adopt_unowned_entry(
            project_root, db_path, _wc.AdoptionChoice.ADOPT_AS_IS, observed
        )
        assert result.outcome is _wc.AdoptionOutcome.ADOPTED
        entry = _read_mcp_json(project_root)["mcpServers"]["neuroloom"]
        assert entry["headers"][_wc._WORKSPACE_HEADER] == existing

    def test_adopt_and_change_stamps_ownership_and_writes_new_header(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        existing = "30000000-0000-0000-0000-00000000ac01"
        new_value = "30000000-0000-0000-0000-00000000ac02"
        _write_mcp_json(
            project_root,
            {
                "mcpServers": {
                    "neuroloom": {
                        "type": _wc._MCP_SERVER_TYPE,
                        "url": _wc._MCP_SERVER_URL,
                        "headers": {_wc._WORKSPACE_HEADER: existing},
                    }
                }
            },
        )
        observed = _wc.read_observed_state(project_root)

        result = _wc.adopt_unowned_entry(
            project_root,
            db_path,
            _wc.AdoptionChoice.ADOPT_AND_CHANGE,
            observed,
            desired_value=new_value,
        )
        assert result.outcome is _wc.AdoptionOutcome.ADOPTED
        assert _wc._is_mcp_entry_owned(db_path) is True
        entry = _read_mcp_json(project_root)["mcpServers"]["neuroloom"]
        assert entry["headers"][_wc._WORKSPACE_HEADER] == new_value

    def test_leave_unowned_writes_no_files_and_records_decline(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        existing = "30000000-0000-0000-0000-00000000lu01"
        _write_mcp_json(
            project_root,
            {
                "mcpServers": {
                    "neuroloom": {
                        "type": _wc._MCP_SERVER_TYPE,
                        "url": _wc._MCP_SERVER_URL,
                        "headers": {_wc._WORKSPACE_HEADER: existing},
                    }
                }
            },
        )
        original = (Path(project_root) / ".mcp.json").read_bytes()
        observed = _wc.read_observed_state(project_root)

        result = _wc.adopt_unowned_entry(
            project_root, db_path, _wc.AdoptionChoice.LEAVE_UNOWNED, observed
        )
        assert result.outcome is _wc.AdoptionOutcome.DECLINED
        assert (Path(project_root) / ".mcp.json").read_bytes() == original
        assert _wc._is_mcp_entry_owned(db_path) is False
        assert _wc.should_resurface_adoption_offer(db_path, observed) is False

    def test_entry_vanished_between_confirmation_and_execution_is_not_applicable(
        self, tmp_path: Path
    ) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        existing = "30000000-0000-0000-0000-00000000na01"
        _write_mcp_json(
            project_root,
            {
                "mcpServers": {
                    "neuroloom": {
                        "type": _wc._MCP_SERVER_TYPE,
                        "url": _wc._MCP_SERVER_URL,
                        "headers": {_wc._WORKSPACE_HEADER: existing},
                    }
                }
            },
        )
        confirmed_observed = _wc.read_observed_state(project_root)
        (Path(project_root) / ".mcp.json").write_text(
            json.dumps({"mcpServers": {}}), encoding="utf-8"
        )  # the entry itself vanished

        result = _wc.adopt_unowned_entry(
            project_root, db_path, _wc.AdoptionChoice.ADOPT_AS_IS, confirmed_observed
        )
        assert result.outcome is _wc.AdoptionOutcome.STALE_CONFIRMATION

    def test_write_failure_propagates_as_write_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        existing = "30000000-0000-0000-0000-00000000wf01"
        _write_mcp_json(
            project_root,
            {
                "mcpServers": {
                    "neuroloom": {
                        "type": _wc._MCP_SERVER_TYPE,
                        "url": _wc._MCP_SERVER_URL,
                        "headers": {_wc._WORKSPACE_HEADER: existing},
                    }
                }
            },
        )
        observed = _wc.read_observed_state(project_root)

        def _raise(*args: object, **kwargs: object) -> None:
            raise OSError("simulated os.replace failure")

        monkeypatch.setattr(os, "replace", _raise)

        result = _wc.adopt_unowned_entry(
            project_root,
            db_path,
            _wc.AdoptionChoice.ADOPT_AND_CHANGE,
            observed,
            desired_value="30000000-0000-0000-0000-00000000wf02",
        )
        assert result.outcome is _wc.AdoptionOutcome.WRITE_FAILED
        assert result.write_result == _wc.WriteResult.FAILED
        assert _wc._is_mcp_entry_owned(db_path) is False


class TestAdoptTOCTOU:
    """
    TOCTOU regression test (this phase's ownership — referenced, not
    duplicated, by Phase 3's AC). Concretely: a human is shown the entry's
    current state, thinks it over, then confirms. In that gap, something
    else — a second session, a script, a git pull — could change
    .mcp.json. This proves the re-check immediately before the write
    actually catches that drift.
    """

    def test_stale_confirmation_when_entry_drifts_between_check_and_write(
        self, tmp_path: Path
    ) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        original_value = "40000000-0000-0000-0000-00000000orig"
        _write_mcp_json(
            project_root,
            {
                "mcpServers": {
                    "neuroloom": {
                        "type": _wc._MCP_SERVER_TYPE,
                        "url": _wc._MCP_SERVER_URL,
                        "headers": {_wc._WORKSPACE_HEADER: original_value},
                    }
                }
            },
        )
        confirmed_observed = _wc.read_observed_state(project_root)  # what the human was shown

        # Drift in the gap between confirmation and execution.
        drifted_value = "40000000-0000-0000-0000-00000000drft"
        _write_mcp_json(
            project_root,
            {
                "mcpServers": {
                    "neuroloom": {
                        "type": _wc._MCP_SERVER_TYPE,
                        "url": _wc._MCP_SERVER_URL,
                        "headers": {_wc._WORKSPACE_HEADER: drifted_value},
                    }
                }
            },
        )

        result = _wc.adopt_unowned_entry(
            project_root, db_path, _wc.AdoptionChoice.ADOPT_AS_IS, confirmed_observed
        )
        assert result.outcome is _wc.AdoptionOutcome.STALE_CONFIRMATION
        assert _wc._is_mcp_entry_owned(db_path) is False
        entry = _read_mcp_json(project_root)["mcpServers"]["neuroloom"]
        assert entry["headers"][_wc._WORKSPACE_HEADER] == drifted_value  # untouched

    def test_matching_confirmation_proceeds_normally(self, tmp_path: Path) -> None:
        """Contrast case — no drift, the TOCTOU check passes, adoption proceeds."""
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        value = "40000000-0000-0000-0000-00000000same"
        _write_mcp_json(
            project_root,
            {
                "mcpServers": {
                    "neuroloom": {
                        "type": _wc._MCP_SERVER_TYPE,
                        "url": _wc._MCP_SERVER_URL,
                        "headers": {_wc._WORKSPACE_HEADER: value},
                    }
                }
            },
        )
        confirmed_observed = _wc.read_observed_state(project_root)

        result = _wc.adopt_unowned_entry(
            project_root, db_path, _wc.AdoptionChoice.ADOPT_AS_IS, confirmed_observed
        )
        assert result.outcome is _wc.AdoptionOutcome.ADOPTED
        assert _wc._is_mcp_entry_owned(db_path) is True


class TestDeclineHashSuppressAndResurface:
    """
    R2-9 (dedicated — distinct from TestDeclineHashWarningIndependence
    below): the decline-hash state machine itself. Same idea as a sticky
    note left on a shared whiteboard (see ``_compute_entry_fingerprint``'s
    docstring): the note applies only to the exact state it was left
    against.
    """

    def test_no_decline_recorded_offer_is_not_suppressed(self, tmp_path: Path) -> None:
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        observed = ObservedState(ObservedKind.HEADERED_WITH_VALUE, "50000000-0000-0000-0000-0001")
        assert _wc.should_resurface_adoption_offer(db_path, observed) is True

    def test_offer_suppressed_when_declined_state_unchanged(self, tmp_path: Path) -> None:
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        observed = ObservedState(ObservedKind.HEADERED_WITH_VALUE, "50000000-0000-0000-0000-0002")

        _wc.record_unowned_decline(db_path, observed)
        assert _wc.should_resurface_adoption_offer(db_path, observed) is False

    def test_offer_resurfaces_when_entry_value_changes(self, tmp_path: Path) -> None:
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        declined_against = ObservedState(
            ObservedKind.HEADERED_WITH_VALUE, "50000000-0000-0000-0000-0003"
        )
        _wc.record_unowned_decline(db_path, declined_against)
        assert _wc.should_resurface_adoption_offer(db_path, declined_against) is False

        changed = ObservedState(ObservedKind.HEADERED_WITH_VALUE, "50000000-0000-0000-0000-0004")
        assert _wc.should_resurface_adoption_offer(db_path, changed) is True

    def test_offer_resurfaces_when_entry_kind_changes(self, tmp_path: Path) -> None:
        """Same value-less transition (e.g. the entry became headerless)
        also invalidates a decline hash — kind is part of the fingerprint,
        not just the value."""
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        declined_against = ObservedState(
            ObservedKind.HEADERED_WITH_VALUE, "50000000-0000-0000-0000-0005"
        )
        _wc.record_unowned_decline(db_path, declined_against)

        now_headerless = ObservedState(ObservedKind.HEADERLESS)
        assert _wc.should_resurface_adoption_offer(db_path, now_headerless) is True


class TestDeclineHashWarningIndependence:
    """
    A suppressed adoption offer must NOT suppress the separate FR-9
    disagreement warning. Scenario: declined, entry unchanged, but desired
    state now points at a DIFFERENT workspace than the declined entry's
    value — the offer stays suppressed, but the printed FR-9 disagreement
    warning (``_UNOWNED_CONFLICT_WARNING`` in ``session_start.py``) still
    fires, because a workspace genuinely IS desired and this plugin still
    can't safely apply it to an entry it doesn't own.

    Review finding (D172 completion review, Finding #2): the original
    version of this test asserted ``outcome.decision is
    Decision.STAMP_OWNERSHIP_WRITE_HEADER`` and ``outcome.error is not
    None`` and called that proof the warning "still fires" — but neither
    of those is what ``session_start.py`` actually prints from. This
    rewrite drives the real ``session_start.main()`` entry point (the
    convention ``tests/test_session_start.py``'s
    ``TestWorkspaceStepEmissionWiring`` already establishes for asserting
    printed warning text) and asserts the literal warning text is
    produced, while separately confirming the adoption offer itself stays
    suppressed.
    """

    def test_warning_still_fires_while_offer_stays_suppressed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import io
        from unittest.mock import patch

        import pyhooks.config as _config_mod
        import pyhooks.session_start as _ss_mod

        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        existing_value = "60000000-0000-0000-0000-0000000000ex"
        _write_mcp_json(
            project_root,
            {
                "mcpServers": {
                    "neuroloom": {
                        "type": _wc._MCP_SERVER_TYPE,
                        "url": _wc._MCP_SERVER_URL,
                        "headers": {_wc._WORKSPACE_HEADER: existing_value},
                    }
                }
            },
        )
        observed = _wc.read_observed_state(project_root)
        _wc.record_unowned_decline(db_path, observed)
        assert _wc.should_resurface_adoption_offer(db_path, observed) is False  # offer suppressed

        different_target = "60000000-0000-0000-0000-0000000000df"
        _wc.write_desired_state(db_path, DesiredState(DesiredKind.SET_TO_VALUE, different_target))

        # Confirm the pure decision core answers the would-converge shape
        # this scenario is exercising, before driving it through main().
        direct_outcome = _wc.reconcile(
            project_root, db_path, DesiredState(DesiredKind.SET_TO_VALUE, different_target)
        )
        assert direct_outcome.decision is Decision.STAMP_OWNERSHIP_WRITE_HEADER
        assert direct_outcome.error is not None

        # Pre-seed the F14 resolvability cache so gated_reconcile's
        # check_resolvability_with_cache resolves without a live network
        # round trip (same technique test_session_start.py's own
        # gated-reconcile tests use via a stubbed outcome; here we want
        # the *real* reconcile() to run, so we satisfy the cache instead
        # of stubbing gated_reconcile itself).
        api_key = "test-key-abc123"
        fingerprint = _wc._fingerprint_api_key(api_key)
        _wc._save_workspace_id_to_db(db_path, "70000000-0000-0000-0000-000000000000")
        _wc._save_config_value(db_path, _wc._WORKSPACE_ID_FINGERPRINT_KEY, fingerprint)

        monkeypatch.chdir(tmp_path)
        output = io.StringIO()
        real_open_db = _db_mod.open_db  # capture before patching below

        with (
            patch("pyhooks.session_start._config.load") as mock_load,
            patch("pyhooks.session_start._db.open_db") as mock_open_db,
            patch(
                "pyhooks.session_start._http.post_json",
                return_value=(200, b'{"session_id":"sess-1-aabb"}'),
            ),
            patch("pyhooks.session_start.sys.stdout", output),
            patch("pyhooks.session_start._codeweaver_bootstrap_and_upgrade", return_value=None),
        ):
            mock_load.return_value = _config_mod.Config(
                api_key=api_key,
                api_base="http://localhost:19999",
                state_db_path=db_path,
                api_key_source="project",
            )
            mock_open_db.side_effect = real_open_db
            _ss_mod.main()

        text = output.getvalue()
        assert _ss_mod._UNOWNED_CONFLICT_WARNING in text  # FR-9 disagreement warning printed
        assert "Could not ensure a Neuroloom MCP connection" not in text
        assert "Could not update this project's .mcp.json" not in text

        # The adoption offer itself (the /neuroloom:workspace menu-item-4
        # prompt this decline-hash gates) stays suppressed — the entry on
        # disk never changed, so the sticky note the decline recorded
        # still applies.
        assert _wc.should_resurface_adoption_offer(db_path, observed) is False


# ===========================================================================
# D172 Phase 5 — corrupt-.mcp.json command-surface coverage
# ===========================================================================


class TestCorruptMcpJsonCommandSurface:
    """
    Every human-invocable command surface must refuse to proceed — never
    silently create/adopt/overwrite — when .mcp.json is unparseable.
    ``inventory_workspace_state``'s malformed-handling is already proven by
    ``TestInventoryPinnedShape::test_malformed_mcp_json_surfaces_as_observed_error_not_exception``
    (Phase 2); this class covers the remaining surfaces: set/change (via
    ``reconcile``) and adopt.
    """

    def test_set_change_refuses_via_reconcile(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        (Path(project_root) / ".mcp.json").write_text("{not valid json", encoding="utf-8")

        outcome = _wc.reconcile(
            project_root, db_path, DesiredState(DesiredKind.SET_TO_VALUE, "70000000-0000-0000-0000-0000000000c1")
        )
        assert outcome.decision is Decision.NO_OP
        assert outcome.write_result is None
        assert outcome.error is not None
        assert (
            Path(project_root) / ".mcp.json"
        ).read_text(encoding="utf-8") == "{not valid json"  # genuinely untouched

    def test_adopt_refuses_via_toctou_gate(self, tmp_path: Path) -> None:
        """
        adopt_unowned_entry's own TOCTOU re-check (``read_observed_state``)
        catches a corrupt file at execution time exactly the same way it
        catches drift — ``McpJsonConflictError`` maps to
        STALE_CONFIRMATION, never a silent "proceed anyway."
        """
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        value = "70000000-0000-0000-0000-0000000000ad"
        _write_mcp_json(
            project_root,
            {
                "mcpServers": {
                    "neuroloom": {
                        "type": _wc._MCP_SERVER_TYPE,
                        "url": _wc._MCP_SERVER_URL,
                        "headers": {_wc._WORKSPACE_HEADER: value},
                    }
                }
            },
        )
        confirmed_observed = _wc.read_observed_state(project_root)
        (Path(project_root) / ".mcp.json").write_text("{not valid json", encoding="utf-8")

        result = _wc.adopt_unowned_entry(
            project_root, db_path, _wc.AdoptionChoice.ADOPT_AS_IS, confirmed_observed
        )
        assert result.outcome is _wc.AdoptionOutcome.STALE_CONFIRMATION
        assert _wc._is_mcp_entry_owned(db_path) is False


# ===========================================================================
# D172 Phase 5 — S9 "no": declined settings.json migration
# ===========================================================================


class TestSettingsJsonMigrationDeclined:
    """
    S9 "no" — declined migration: the migration-done marker is set on an
    explicit decline, and the same legacy value is never re-disclosed (or
    re-read) on a subsequent inventory/migrate call.
    """

    def test_decline_sets_marker_and_suppresses_redisclosure(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)
        db_path = tmp_path / ".neuroloom.db"
        _init_db(db_path)
        legacy = "80000000-0000-0000-0000-000000000009"
        _write_raw_settings_workspace_id(project_root, legacy)
        assert _wc.is_settings_json_migration_done(db_path) is False

        _wc.decline_settings_json_migration(db_path)
        assert _wc.is_settings_json_migration_done(db_path) is True

        result = _wc.migrate_settings_json_override(
            project_root, db_path, api_base=_dead_api_base(), api_key=""
        )
        assert result.outcome is _wc.MigrationOutcome.ALREADY_DONE
        # Never re-read: the leftover legacy value is exactly as it was —
        # not touched, not promoted to desired state after the fact.
        settings_path = Path(project_root) / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        assert settings["pluginConfigs"][_wc._PLUGIN_ID]["options"]["workspace_id"] == legacy
        assert _wc.read_desired_state(db_path) == DesiredState(DesiredKind.UNSET)
