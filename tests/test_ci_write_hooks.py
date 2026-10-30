"""
D178d Phase 3, Step A: capture, code_graph_sync, cache_prefetch and
preload_context in CI mode (FR-D2 to FR-D6).

Written before the implementation and run red against the unmodified hooks.
Covers the second implementer group's files only (capture, code_graph_sync,
cache_prefetch, preload_context); base session_start and the launcher are in
``test_ci_session_start.py`` and ``test_run_hook.py``.

Conventions (plan, "Conventions"):

* ``main()`` runs in-process under ``ci_support.forbid_side_effects()``.  The
  sentinel derives from ``BaseException``; a forbidden call becomes this test's
  own assertion message listing only the bare function names.
* Request checks use hand-rolled recorders that keep ``(method, path, body)``
  or a timeout number.  No ``assert_called*`` on anything carrying headers.
* Where a test claims "did nothing", a control proves the same input does
  something outside CI mode or in a sibling case, so a vacuous pass is not
  possible.
"""

from __future__ import annotations

import importlib
import json
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests import ci_support

VALID_SESSION = "sess-1700000000-aabbccdd"
_NOT_PASSED = object()

# Every module registered in hooks/hooks.json must be covered by one of the two
# CI test files.  session_start lives in test_ci_session_start.py.
_COVERED_HERE = {
    "pyhooks.capture",
    "pyhooks.code_graph_sync",
    "pyhooks.cache_prefetch",
    "pyhooks.preload_context",
}
_COVERED_ELSEWHERE = {"pyhooks.session_start"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_root(
    tmp_path: Path,
    *,
    key: str | None = ci_support.SENTINEL_KEY,
    session_id: str | None = VALID_SESSION,
    last_submit_ms: int = 0,
    name: str = "proj",
) -> Path:
    """Create a project root holding a state db with a key and a session row."""
    import pyhooks.db as db_mod

    root = tmp_path / name
    root.mkdir()
    conn = db_mod.open_db(root / ".neuroloom.db")
    assert conn is not None, "fixture error: could not create the state database"
    try:
        if key:
            conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('api_key', ?)", (key,))
        if session_id:
            conn.execute(
                "INSERT OR REPLACE INTO sessions (session_key, session_id, started_at, last_submit_ms) "
                "VALUES (?, ?, datetime('now'), ?)",
                (str(root.resolve()), session_id, last_submit_ms),
            )
        conn.commit()
    finally:
        conn.close()
    return root


def _ci_env(
    monkeypatch: pytest.MonkeyPatch, root: Path, api_base: str, mode: str | None = "ci"
) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    monkeypatch.setenv("NEUROLOOM_API_BASE", api_base)
    if mode is None:
        monkeypatch.delenv("NEUROLOOM_MODE", raising=False)
    else:
        monkeypatch.setenv("NEUROLOOM_MODE", mode)
    monkeypatch.chdir(root)


class _Stdin:
    """stdin stand-in that counts reads (to prove "exits before reading stdin")."""

    def __init__(self, text: str) -> None:
        self._text = text
        self.reads = 0

    def read(self, *args: Any) -> str:
        self.reads += 1
        return self._text


@dataclass
class _HookResult:
    exit_code: object
    forbidden: list[str]
    stdin_reads: int


def _call_hook(module: str, stdin_text: str, monkeypatch: pytest.MonkeyPatch) -> _HookResult:
    """Run ``<module>.main()`` in-process with the forbid patches active."""
    mod = importlib.import_module(module)
    stdin = _Stdin(stdin_text)
    monkeypatch.setattr(sys, "stdin", stdin)
    code: object = None
    with ci_support.forbid_side_effects() as recorder:
        try:
            mod.main()
        except SystemExit as exc:
            code = exc.code
        except ci_support.ForbiddenCallError:
            pass
    return _HookResult(code, list(recorder.calls), stdin.reads)


def _db_count(root: Path, sql: str) -> int:
    conn = sqlite3.connect(str(root / ".neuroloom.db"))
    try:
        return int(conn.execute(sql).fetchone()[0])
    finally:
        conn.close()


def _buffer_rows(root: Path) -> list[tuple[str, str, float]]:
    conn = sqlite3.connect(str(root / ".neuroloom.db"))
    try:
        return [
            (str(r[0]), str(r[1]), float(r[2]))
            for r in conn.execute("SELECT payload, payload_type, created_at FROM event_buffer ORDER BY id")
        ]
    finally:
        conn.close()


def _run_subprocess(
    module: str, stdin: str, root: Path, hole: ci_support.BlackHole, scratch: Path
) -> ci_support.HookRun:
    try:
        return ci_support.run_hook(
            module,
            stdin=stdin,
            cwd=root,
            scratch=scratch,
            black_hole_url=hole.url,
            env_extra={"CLAUDE_PROJECT_DIR": str(root), "NEUROLOOM_MODE": "ci"},
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"{module} did not exit within the 10 s backstop in CI mode")


# ---------------------------------------------------------------------------
# Registered-hook coverage
# ---------------------------------------------------------------------------


def test_every_registered_hook_has_a_ci_test_home() -> None:
    registered = set(ci_support.registered_hook_modules())
    missing = registered - _COVERED_HERE - _COVERED_ELSEWHERE
    assert not missing, f"hooks.json registers hooks with no CI-mode tests: {sorted(missing)}"


# ---------------------------------------------------------------------------
# capture
# ---------------------------------------------------------------------------


def _capture_event(root: Path, **overrides: Any) -> str:
    event: dict[str, Any] = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(root / "x.py"), "content": "hello"},
        "cwd": str(root),
        "agent_id": "agent-1",
        "agent_type": "general-purpose",
    }
    event.update(overrides)
    return json.dumps(event)


class TestCaptureCi:
    def test_records_typed_rows_with_no_throttle(
        self,
        tmp_path: Path,
        black_hole: ci_support.BlackHole,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        now_ms = int(time.time() * 1000)
        root = _make_root(tmp_path, last_submit_ms=now_ms)  # the throttle window is open
        _ci_env(monkeypatch, root, black_hole.url)

        for n in range(3):
            result = _call_hook("pyhooks.capture", _capture_event(root), monkeypatch)
            assert result.forbidden == [], f"capture CI event {n}: forbidden calls {result.forbidden}"

        rows = _buffer_rows(root)
        assert len(rows) == 3, "capture CI: rapid events must all be recorded (no throttle)"
        for payload, payload_type, created_at in rows:
            assert payload_type == "observation", "capture CI: row must carry payload_type='observation'"
            body = json.loads(payload)
            assert set(body) == {
                "observation_id",
                "session_id",
                "observed_at",
                "category",
                "content",
                "agent_id",
                "agent_type",
            }, "capture CI: payload keys differ from today's buffered observation"
            assert body["session_id"] == VALID_SESSION, "capture CI: wrong session id in payload"
            assert body["category"] == "Write", "capture CI: category must be the tool name"
            assert json.loads(body["content"])["tool_name"] == "Write", "capture CI: content is the event"
            assert body["agent_id"] == "agent-1", "capture CI: agent_id lost"
            assert abs(created_at - time.time()) < 120, "capture CI: created_at is not time.time()"
        assert _db_count(root, "SELECT last_submit_ms FROM sessions") == now_ms, (
            "capture CI: last_submit_ms must not be updated"
        )
        assert black_hole.connection_count() == 0, "capture CI: empty request allowlist violated"

    @pytest.mark.parametrize(
        "case", ["no_key", "no_session", "corrupt_session", "mcp_tool", "outside_root", "malformed_cwd"]
    )
    def test_guards_record_nothing(
        self,
        case: str,
        tmp_path: Path,
        black_hole: ci_support.BlackHole,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = _make_root(tmp_path)
        db = root / ".neuroloom.db"
        event = _capture_event(root)
        conn = sqlite3.connect(str(db))
        try:
            if case == "no_key":
                conn.execute("DELETE FROM config WHERE key = 'api_key'")
            elif case == "no_session":
                conn.execute("DELETE FROM sessions")
            elif case == "corrupt_session":
                conn.execute("UPDATE sessions SET session_id = 'not-a-session'")
            elif case == "mcp_tool":
                event = _capture_event(root, tool_name="mcp__neuroloom__memory_search", tool_input={})
            elif case == "outside_root":
                other = tmp_path / "elsewhere"
                other.mkdir()
                event = _capture_event(root, tool_input={"file_path": str(other / "y.py"), "content": "z"})
            elif case == "malformed_cwd":
                event = _capture_event(root, cwd="/nonexistent\x00dir")
            conn.commit()
        finally:
            conn.close()
        _ci_env(monkeypatch, root, black_hole.url)

        result = _call_hook("pyhooks.capture", event, monkeypatch)
        assert result.forbidden == [], f"capture CI guard {case}: forbidden calls {result.forbidden}"
        assert _buffer_rows(root) == [], f"capture CI guard {case}: a row was recorded"
        assert black_hole.connection_count() == 0, f"capture CI guard {case}: a connection was made"

        # Control: restore a valid key and session, replay a valid event, and it IS recorded.
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('api_key', ?)", (ci_support.SENTINEL_KEY,))
            conn.execute("DELETE FROM sessions")
            conn.execute(
                "INSERT INTO sessions (session_key, session_id, started_at, last_submit_ms) "
                "VALUES (?, ?, datetime('now'), 0)",
                (str(root.resolve()), VALID_SESSION),
            )
            conn.commit()
        finally:
            conn.close()
        control = _call_hook("pyhooks.capture", _capture_event(root), monkeypatch)
        assert control.forbidden == [], f"capture CI guard {case} control: forbidden calls {control.forbidden}"
        assert len(_buffer_rows(root)) == 1, f"capture CI guard {case}: the control event was not recorded"

    def test_oversized_event_row_is_capped(
        self,
        tmp_path: Path,
        black_hole: ci_support.BlackHole,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A 5 MB command plus 5 MB stdout must not become a 10 MB row."""
        root = _make_root(tmp_path)
        _ci_env(monkeypatch, root, black_hole.url)
        event = _capture_event(
            root,
            tool_name="Bash",
            tool_input={"command": 'echo "' + "c" * 5_000_000 + '"'},
            tool_response={"stdout": "o" * 5_000_000, "stderr": ""},
        )

        result = _call_hook("pyhooks.capture", event, monkeypatch)
        assert result.forbidden == [], f"capture CI oversize: forbidden calls {result.forbidden}"
        rows = _buffer_rows(root)
        assert len(rows) == 1, "capture CI oversize: the event must still be recorded"
        payload, payload_type, _ = rows[0]
        assert len(payload.encode("utf-8")) <= 64 * 1024, "capture CI oversize: row exceeds the 64 KB cap"
        assert payload_type == "observation", "capture CI oversize: payload_type changed"
        body = json.loads(payload)  # still valid JSON
        assert body["category"] == "Bash", "capture CI oversize: category lost"
        assert "truncated" in body["content"], "capture CI oversize: no truncation marker in content"

    @pytest.mark.parametrize(
        "content",
        [
            "\\" * 500_000 + "a" * 500_000,
            '"' * 300_000,
            "\u00e9" * 300_000,
            "\\" * 100_000 + '"' * 100_000 + "\u00e9" * 100_000 + "\n" * 100_000 + "a" * 100_000,
        ],
        ids=["backslash-mix", "quotes", "non-ascii", "combination"],
    )
    def test_ci_row_json_cap_is_exact_for_escape_heavy_content(self, content: str) -> None:
        """Escaping inflates content, so the re-measure loop must land the FINAL row under the cap."""
        capture = importlib.import_module("pyhooks.capture")
        cap = capture._CI_ROW_MAX_BYTES
        obs: dict[str, object] = {"category": "Bash", "content": content, "file_path": "x.py"}
        row = capture._ci_row_json(obs)
        assert len(row.encode("utf-8")) <= cap, f"row is {len(row.encode('utf-8'))} bytes, cap {cap}"
        body = json.loads(row)
        assert set(body) == set(obs), "capped row changed its keys"
        assert isinstance(body["content"], str), "content must stay a string"
        assert body["content"].endswith("chars]"), "content must end with the truncation marker"
        assert f"original {len(content)} chars" in body["content"], "marker must record the original length"

    def test_long_tool_name_is_cut_before_it_reaches_the_trace(
        self,
        tmp_path: Path,
        black_hole: ci_support.BlackHole,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = _make_root(tmp_path)
        _ci_env(monkeypatch, root, black_hole.url)
        result = _call_hook("pyhooks.capture", _capture_event(root, tool_name="T" * 10_000_000), monkeypatch)
        assert result.forbidden == [], f"long tool_name: forbidden calls {result.forbidden}"
        conn = sqlite3.connect(str(root / ".neuroloom.db"))
        try:
            biggest = conn.execute("SELECT MAX(LENGTH(tool_name)) FROM traces").fetchone()[0]
            total = conn.execute("SELECT SUM(LENGTH(tool_name) + LENGTH(COALESCE(detail, ''))) FROM traces").fetchone()[0]
        finally:
            conn.close()
        assert biggest is not None and biggest <= 256, f"trace tool_name is {biggest} chars"
        assert total < 10_000, f"trace rows total {total} chars"
        for payload, _, _ in _buffer_rows(root):
            assert len(payload) <= 64 * 1024, "buffered row too large"

    def test_buffer_cap_trims_to_8000(
        self,
        tmp_path: Path,
        black_hole: ci_support.BlackHole,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = _make_root(tmp_path)
        conn = sqlite3.connect(str(root / ".neuroloom.db"))
        try:
            conn.executemany(
                "INSERT INTO event_buffer (payload, created_at, payload_type) VALUES (?, ?, 'observation')",
                [("{}", 1_700_000_000.0 + n) for n in range(10_000)],
            )
            conn.commit()
        finally:
            conn.close()
        _ci_env(monkeypatch, root, black_hole.url)

        result = _call_hook("pyhooks.capture", _capture_event(root), monkeypatch)
        assert result.forbidden == [], f"capture CI cap: forbidden calls {result.forbidden}"
        assert _db_count(root, "SELECT COUNT(*) FROM event_buffer") == 8000, (
            "capture CI cap: buffer over 10,000 rows must be trimmed to 8,000"
        )


    def test_subprocess_records_one_row_with_no_connections(
        self,
        tmp_path: Path,
        black_hole: ci_support.BlackHole,
    ) -> None:
        root = _make_root(tmp_path)
        run = _run_subprocess(
            "pyhooks.capture", _capture_event(root), root, black_hole, tmp_path / "scratch"
        )
        assert run.returncode == 0, "capture CI subprocess: exit code must be 0"
        assert _db_count(root, "SELECT COUNT(*) FROM event_buffer") == 1, (
            "capture CI subprocess: exactly one event_buffer row expected"
        )
        assert black_hole.connection_count() == 0, "capture CI subprocess: connections reached the API base"


# ---------------------------------------------------------------------------
# code_graph_sync and cache_prefetch: no-ops in CI
# ---------------------------------------------------------------------------


def _cgs_event(root: Path) -> str:
    (root / "app.py").write_text("x = 1\n", encoding="utf-8")
    return json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(root / "app.py")}, "cwd": str(root)})


def _prefetch_event(root: Path) -> str:
    (root / "sub").mkdir(exist_ok=True)
    return json.dumps({"hook_event_name": "CwdChanged", "cwd": str(root / "sub")})


_NOOP_HOOKS = [
    ("pyhooks.code_graph_sync", _cgs_event, "SELECT COUNT(*) FROM debounce_files"),
    ("pyhooks.cache_prefetch", _prefetch_event, "SELECT COUNT(*) FROM traces"),
]


class TestNoOpHooksCi:
    @pytest.mark.parametrize(("module", "make_event", "effect_sql"), _NOOP_HOOKS)
    def test_normal_mode_control_reaches_the_work(
        self,
        module: str,
        make_event: Any,
        effect_sql: str,
        tmp_path: Path,
        black_hole: ci_support.BlackHole,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The same event, keyed root and guards, outside CI mode, does its work.

        Proves the CI tests below cannot pass merely because a guard rejected the event.
        """
        root = _make_root(tmp_path)
        event = make_event(root)
        _ci_env(monkeypatch, root, black_hole.url, mode=None)
        result = _call_hook(module, event, monkeypatch)
        assert result.stdin_reads >= 1, f"{module} control: normal mode did not read stdin"
        reached_thread = "threading.Thread.start" in result.forbidden
        did_local_work = _db_count(root, effect_sql) > 0
        assert reached_thread or did_local_work, f"{module} control: the event never reached the hook's work"

    @pytest.mark.parametrize(("module", "make_event", "effect_sql"), _NOOP_HOOKS)
    def test_exits_zero_before_stdin_db_or_thread(
        self,
        module: str,
        make_event: Any,
        effect_sql: str,
        tmp_path: Path,
        black_hole: ci_support.BlackHole,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import pyhooks.db as db_mod

        root = _make_root(tmp_path)
        event = make_event(root)
        mod = importlib.import_module(module)
        opened: list[str] = []
        real_open = db_mod.open_db

        def _spy(*args: Any, **kwargs: Any) -> Any:
            opened.append("open_db")
            return real_open(*args, **kwargs)

        monkeypatch.setattr(db_mod, "open_db", _spy)
        if hasattr(mod, "open_db"):
            monkeypatch.setattr(mod, "open_db", _spy)
        before = _db_count(root, effect_sql)
        _ci_env(monkeypatch, root, black_hole.url)

        result = _call_hook(module, event, monkeypatch)

        assert result.exit_code in (0, None), f"{module} CI: exit code must be 0"
        assert result.forbidden == [], f"{module} CI: forbidden calls {result.forbidden}"
        assert result.stdin_reads == 0, f"{module} CI: stdin was read before the CI exit"
        assert opened == [], f"{module} CI: the state database was opened"
        assert _db_count(root, effect_sql) == before, f"{module} CI: local state changed"
        assert black_hole.connection_count() == 0, f"{module} CI: a connection was made"

    @pytest.mark.parametrize(("module", "make_event", "effect_sql"), _NOOP_HOOKS)
    def test_subprocess_makes_zero_connections(
        self,
        module: str,
        make_event: Any,
        effect_sql: str,
        tmp_path: Path,
        black_hole: ci_support.BlackHole,
    ) -> None:
        root = _make_root(tmp_path)
        event = make_event(root)
        before = _db_count(root, effect_sql)
        run = _run_subprocess(module, event, root, black_hole, tmp_path / "scratch")
        assert run.returncode == 0, f"{module} CI subprocess: exit code must be 0"
        assert black_hole.connection_count() == 0, f"{module} CI subprocess: connections reached the API base"
        assert _db_count(root, effect_sql) == before, f"{module} CI subprocess: local state changed"


# ---------------------------------------------------------------------------
# preload_context
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._data = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._data


class _UrlRecorder:
    """Stands in for ``urllib.request.urlopen``; keeps only (method, path, body)."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, req: Any, *args: Any, **kwargs: Any) -> _Resp:
        body = json.loads(req.data) if req.data else {}
        self.requests.append((req.get_method(), urllib.parse.urlsplit(req.full_url).path, body))
        return _Resp({"injection_text": "context text", "cache_hint_ttl_seconds": 300})


def _read_event(root: Path, name: str = "a.py") -> str:
    return json.dumps(
        {"tool_name": "Read", "tool_input": {"file_path": str(root / "src" / name)}, "cwd": str(root)}
    )


def _nudge_event(root: Path) -> str:
    return json.dumps(
        {"tool_name": "Grep", "tool_input": {"pattern": "authentication"}, "cwd": str(root)}
    )


def _closed_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


class TestPreloadContextCi:
    def test_dormant_prints_empty_object_and_creates_no_database(
        self,
        tmp_path: Path,
        black_hole: ci_support.BlackHole,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        _ci_env(monkeypatch, root, black_hole.url)
        before = ci_support.presence_snapshot(root)
        result = _call_hook("pyhooks.preload_context", _read_event(root), monkeypatch)
        out = capsys.readouterr().out
        assert result.forbidden == [], f"preload CI dormant: forbidden calls {result.forbidden}"
        assert out.strip() == "{}", "preload CI dormant: must print {}"
        assert ci_support.presence_snapshot(root) == before, "preload CI dormant: a file was created"
        assert black_hole.connection_count() == 0, "preload CI dormant: a connection was made"

    def test_subprocess_dormant_prints_empty_object(
        self,
        tmp_path: Path,
        black_hole: ci_support.BlackHole,
    ) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        before = ci_support.presence_snapshot(root)
        run = _run_subprocess(
            "pyhooks.preload_context", _read_event(root), root, black_hole, tmp_path / "scratch"
        )
        assert run.returncode == 0, "preload CI subprocess dormant: exit code must be 0"
        assert run.stdout.strip() == "{}", "preload CI subprocess dormant: must print {}"
        assert ci_support.presence_snapshot(root) == before, "preload CI subprocess dormant: a file was created"
        assert black_hole.connection_count() == 0, "preload CI subprocess dormant: a connection was made"

    @pytest.mark.parametrize("ci", [True, False])
    def test_request_bodies_and_allowlist(
        self,
        ci: bool,
        tmp_path: Path,
        black_hole: ci_support.BlackHole,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = _make_root(tmp_path)
        _ci_env(monkeypatch, root, black_hole.url, mode="ci" if ci else None)
        recorder = _UrlRecorder()
        monkeypatch.setattr(urllib.request, "urlopen", recorder)

        _call_hook("pyhooks.preload_context", _read_event(root), monkeypatch)
        _call_hook("pyhooks.preload_context", _nudge_event(root), monkeypatch)

        label = "CI" if ci else "normal"
        assert len(recorder.requests) >= 2, f"preload {label}: positive minimum (Read and nudge requests) not met"
        for method, path, _body in recorder.requests:
            assert (method, path) == ("POST", "/api/v1/context"), (
                f"preload {label}: request outside the allowlist: {method} {path}"
            )
        for index, (_m, _p, body) in enumerate(recorder.requests):
            carries = "session_id" in body
            if ci:
                assert not carries, f"preload CI: request {index} body carries session_id"
            else:
                assert carries, f"preload normal: request {index} body lost its session_id"

    @pytest.mark.parametrize("ci", [True, False])
    @pytest.mark.parametrize("kind", ["read", "nudge"])
    def test_fetch_timeout_is_passed_explicitly(
        self,
        ci: bool,
        kind: str,
        tmp_path: Path,
        black_hole: ci_support.BlackHole,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import pyhooks.preload_context as pc

        root = _make_root(tmp_path)
        _ci_env(monkeypatch, root, black_hole.url, mode="ci" if ci else None)
        seen: list[object] = []

        def _fake_fetch(
            api_base: str,
            api_key: str,
            file_path: str,
            timeout: object = _NOT_PASSED,
            fmt: str = "inject",
            query: str | None = None,
            session_id: object = _NOT_PASSED,
        ) -> tuple[str, int]:
            seen.append(timeout)
            return ("context text", 300)

        monkeypatch.setattr(pc, "_fetch_context", _fake_fetch)
        event = _read_event(root) if kind == "read" else _nudge_event(root)
        _call_hook("pyhooks.preload_context", event, monkeypatch)

        expected = 10.0 if ci else 3.0
        label = f"preload {'CI' if ci else 'normal'} {kind}"
        assert len(seen) == 1, f"{label}: the fetch was not called exactly once"
        assert seen[0] is not _NOT_PASSED, f"{label}: timeout must be passed explicitly"
        assert seen[0] == expected, f"{label}: fetch timeout must be {expected}"

    def test_api_down_first_call_empty_second_skipped_by_breaker(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = _make_root(tmp_path)
        _ci_env(monkeypatch, root, f"http://127.0.0.1:{_closed_port()}")
        attempts: list[str] = []
        real_urlopen = urllib.request.urlopen

        def _counting(*args: Any, **kwargs: Any) -> Any:
            attempts.append("urlopen")
            return real_urlopen(*args, **kwargs)

        monkeypatch.setattr(urllib.request, "urlopen", _counting)

        _call_hook("pyhooks.preload_context", _read_event(root, "a.py"), monkeypatch)
        first = capsys.readouterr().out.strip()
        assert first == "{}", "preload CI API down: first call must print {}"
        assert len(attempts) == 1, "preload CI API down: first call must attempt the API once"

        _call_hook("pyhooks.preload_context", _read_event(root, "b.py"), monkeypatch)
        second = capsys.readouterr().out.strip()
        assert second == "{}", "preload CI API down: second call must print {}"
        assert len(attempts) == 1, "preload CI API down: second call was not skipped by the circuit breaker"


# ---------------------------------------------------------------------------
# Process lifetime (the four hooks in this file; session_start is in its own file)
# ---------------------------------------------------------------------------


class TestProcessLifetimeCi:
    @pytest.mark.parametrize(
        ("module", "make_event"),
        [
            ("pyhooks.capture", _capture_event),
            ("pyhooks.code_graph_sync", _cgs_event),
            ("pyhooks.cache_prefetch", _prefetch_event),
        ],
    )
    def test_write_hooks_start_no_thread_or_subprocess(
        self,
        module: str,
        make_event: Any,
        tmp_path: Path,
        black_hole: ci_support.BlackHole,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = _make_root(tmp_path)
        event = make_event(root)
        _ci_env(monkeypatch, root, black_hole.url)
        result = _call_hook(module, event, monkeypatch)
        assert result.forbidden == [], f"process lifetime: {module} CI called {result.forbidden}"
        assert result.exit_code in (0, None), f"process lifetime: {module} CI did not complete cleanly"
        if module == "pyhooks.capture":
            assert len(_buffer_rows(root)) == 1, "process lifetime: capture CI completed without recording"
