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
import re
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

import pyhooks.capture as _capture_mod
import pyhooks.config as _config_mod
import pyhooks.db as _db_mod
import pyhooks.preload_context as _pc_mod
import pyhooks.session_start as _ss_mod
from tests import ci_support
from tests.integration.conftest import RecordingServer
from tests.ci_support import SlimRecorder

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

        stdin_data = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "f.py")}})
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

        stdin_data = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "f.py")}})
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
        # Typed, shape-valid observation row: the flush routes by payload_type.
        conn.execute(
            "INSERT INTO event_buffer (payload, created_at, payload_type) VALUES (?, ?, ?)",
            (
                json.dumps(
                    {
                        "observation_id": "obs-buffered",
                        "session_id": "sess-ok",
                        "observed_at": "2026-07-10T00:00:00+00:00",
                        "category": "Write",
                        "content": "{}",
                        "agent_id": None,
                        "agent_type": None,
                    }
                ),
                time.time(),
                "observation",
            ),
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
            {"tool_name": "Read", "tool_input": {"file_path": str(tmp_path / "api.py")}}
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


    @pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason=(
            "Claude Code 2.1.314 ignores the top-level {'additionalContext': ...} shape that "
            "preload_context prints (d178d result doc, 'Phase 6 - Manual QA'); only "
            "{'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'additionalContext': ...}} reaches the model. "
            "Strict: this flips to a failure (XPASS) the day the product is fixed, which is the signal to "
            "delete the marker and update test_preload_context_inject_returns_additional_context."
        ),
    )
    def test_preload_context_uses_the_pretooluse_hook_specific_output_shape(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mock_server: RecordingServer,
        integration_config: _config_mod.Config,
        integration_db: Path,
    ) -> None:
        """The shape Claude Code actually consumes for PreToolUse context injection."""
        mock_server.responses["/api/v1/sessions/start"] = (200, b'{"session_id":"sess-ok"}')
        _run_session_start(integration_config, tmp_path, monkeypatch, mock_server)

        monkeypatch.chdir(tmp_path)
        stdin_data = json.dumps({"tool_name": "Read", "tool_input": {"file_path": str(tmp_path / "api.py")}})
        output = io.StringIO()
        with (
            patch("pyhooks.preload_context._config_mod.load", return_value=integration_config),
            patch("pyhooks.preload_context._db_mod.open_db", side_effect=_real_open_db),
            patch("pyhooks.preload_context._fetch_context", return_value=("Relevant memory for api.py", 300)),
            patch("pyhooks.preload_context.sys.stdin", io.StringIO(stdin_data)),
            patch("pyhooks.preload_context.sys.stdout", output),
        ):
            _pc_mod.main()

        result = json.loads(output.getvalue())
        inner = result.get("hookSpecificOutput")
        inner = inner if isinstance(inner, dict) else {}
        shaped = (
            inner.get("hookEventName") == "PreToolUse"
            and "Relevant memory for api.py" in str(inner.get("additionalContext", ""))
        )
        assert shaped, "preload_context output is not the hookSpecificOutput/PreToolUse shape"


# ===========================================================================
# D178d Phase 6: CI-mode lifecycle proof, with bait and control
# ===========================================================================
#
# Real hook processes, one per step, run through the subprocess runner against
# the bait repository.  The CI run asserts the checkout is untouched and that
# nothing but ``POST /api/v1/context`` left the machine.  A non-CI CONTROL runs
# the identical sequence and evaluates the identical predicates; they must come
# out the other way.  Plain-language picture: the bait repository is a
# mousetrap, the control run is the pencil that proves the trap is set.  A
# trap that stayed shut only means "no mouse" if the pencil sets it off.

_INTERPRETERS = ci_support.interpreter_params()

_SESSION_ID_RE = re.compile(r"^sess-[0-9]+-[a-f0-9]+$")
_ALLOWED_NEW_NAMES = {".neuroloom.db", ".neuroloom.db-wal", ".neuroloom.db-shm"}
_CONTEXT = ("POST", "/api/v1/context")
_EXPECTED_TRIGGERS = {"gitignore_written", "claudemd_appended", "buffer_flushed"}


@dataclass
class _Step:
    label: str
    module: str
    event: dict[str, object]
    proc_cwd: Path


@dataclass
class _Sequence:
    runs: list[ci_support.HookRun]
    after_cd_index: int  # len(recorder.requests) right after the real chdir
    captures: int  # number of capture steps (each should record one local row in CI)


def _make_lifecycle_files(root: Path) -> None:
    """Every file the simulated tools name exists BEFORE the baseline is taken,
    so the only possible diff afterwards comes from a hook."""
    (root / "sub").mkdir(exist_ok=True)
    for rel, text in (
        ("app.py", "def f() -> int:\n    return 1\n"),
        ("notes.txt", "plain notes\n"),
        ("sub/inner.py", "VALUE = 2\n"),
        ("sub/agent.py", "VALUE = 3\n"),
        ("sub/agent_notes.txt", "agent notes\n"),
    ):
        (root / rel).write_text(text, encoding="utf-8")


def _build_steps(root: Path, *, code_write: bool) -> tuple[list[_Step], int]:
    """The tool sequence.  Returns the steps and the index of the first
    post-``cd`` step (the real chdir happens before it)."""
    sub = root / "sub"

    def read(path: Path, **extra: object) -> dict[str, object]:
        return {"tool_name": "Read", "tool_input": {"file_path": str(path)}, "cwd": str(extra.pop("cwd", root)), **extra}

    def write(tool: str, path: Path, cwd: Path, **extra: object) -> dict[str, object]:
        return {"tool_name": tool, "tool_input": {"file_path": str(path)}, "cwd": str(cwd), **extra}

    steps: list[_Step] = [
        _Step("session_start", "pyhooks.session_start", {"hook_event_name": "SessionStart", "cwd": str(root)}, root),
        _Step("read pre", "pyhooks.preload_context", read(root / "app.py"), root),
        _Step("read post", "pyhooks.capture", read(root / "app.py", tool_response={"content": "x"}), root),
    ]
    for tool in ("Write", "Edit"):
        steps.append(_Step(f"{tool} capture", "pyhooks.capture", write(tool, root / "notes.txt", root), root))
        steps.append(_Step(f"{tool} sync", "pyhooks.code_graph_sync", write(tool, root / "notes.txt", root), root))
    if code_write:
        steps.append(_Step("code write capture", "pyhooks.capture", write("Write", root / "app.py", root), root))
        steps.append(_Step("code write sync", "pyhooks.code_graph_sync", write("Write", root / "app.py", root), root))
    steps.append(
        _Step(
            "bash cd",
            "pyhooks.capture",
            {
                "tool_name": "Bash",
                "tool_input": {"command": "cd sub"},
                "tool_response": {"stdout": "", "stderr": ""},
                "cwd": str(root),
            },
            root,
        )
    )
    after_cd = len(steps)  # everything from here runs with the process cwd == sub
    steps.append(
        _Step("cwd changed", "pyhooks.cache_prefetch", {"hook_event_name": "CwdChanged", "cwd": str(sub)}, sub)
    )
    steps.append(_Step("read after cd pre", "pyhooks.preload_context", read(sub / "inner.py", cwd=sub), sub))
    steps.append(
        _Step("read after cd post", "pyhooks.capture", read(sub / "inner.py", cwd=sub, tool_response={"content": "x"}), sub)
    )
    agent = {"agent_id": "agent-7", "agent_type": "general-purpose"}
    steps.append(_Step("subagent read pre", "pyhooks.preload_context", read(sub / "agent.py", cwd=sub, **agent), sub))
    steps.append(
        _Step("subagent read post", "pyhooks.capture", read(sub / "agent.py", cwd=sub, tool_response={"content": "x"}, **agent), sub)
    )
    steps.append(
        _Step("subagent write capture", "pyhooks.capture", write("Write", sub / "agent_notes.txt", sub, **agent), sub)
    )
    return steps, after_cd


def _run_sequence(
    root: Path,
    recorder: SlimRecorder,
    hole: ci_support.BlackHole,
    scratch: Path,
    *,
    ci: bool,
    interpreter: str,
    code_write: bool = False,
) -> _Sequence:
    steps, after_cd = _build_steps(root, code_write=code_write)
    # A stub codeweaver on PYTHONPATH makes code-graph sync LIVE on every
    # interpreter (python3.10 has no codeweaver).  Only the normal-mode launcher
    # reads PYTHONPATH; the CI launcher runs under -I, where the hook exits
    # before any import, so the CI "nothing sent" result is backed by the
    # code-write control below and by the empty debounce tables.
    stub = ci_support.write_codeweaver_stub(scratch.parent / "codeweaver-stub")
    env: dict[str, str] = {"CLAUDE_PROJECT_DIR": str(root), "NEUROLOOM_API_BASE": recorder.url}
    if ci:
        env["NEUROLOOM_MODE"] = "ci"
    runs: list[ci_support.HookRun] = []
    marker = 0
    for index, step in enumerate(steps):
        if index == after_cd:
            marker = len(recorder.requests)
        try:
            runs.append(
                ci_support.run_hook(
                    step.module,
                    stdin=json.dumps(step.event),
                    cwd=step.proc_cwd,
                    scratch=scratch,
                    black_hole_url=hole.url,
                    env_extra=env,
                    interpreter=interpreter,
                    control=not ci,
                    timeout=30.0,
                    pythonpath_extra=(stub,),
                )
            )
        except Exception as exc:
            pytest.fail(f"lifecycle step {step.label!r} failed to run ({type(exc).__name__})")
    captures = sum(1 for s in steps if s.module == "pyhooks.capture")
    return _Sequence(runs, marker, captures)


def _buffer_count(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return int(conn.execute("SELECT COUNT(*) FROM event_buffer").fetchone()[0])
    finally:
        conn.close()


def _lifecycle_report(
    bait: ci_support.Bait,
    recorder: SlimRecorder,
    seq: _Sequence,
    *,
    before_hash: str,
    before_names: list[str],
) -> dict[str, bool]:
    """The "checkout untouched, only the allowlist left the machine" predicates.

    Every value is a precomputed boolean.  The CI test requires all True; the
    control evaluates the same function and must see the checkout-and-traffic
    predicates come out False.
    """
    after_names = ci_support.presence_snapshot(bait.root)
    new_names = set(after_names) - set(before_names)
    paths = recorder.paths()
    post_cd = recorder.requests[seq.after_cd_index :]
    fired = bait.fired()
    return {
        "tree_unchanged": ci_support.tree_hash(bait.root) == before_hash,
        # Only the state database and its sidecars, and only at the root.
        "no_new_files_below_root": new_names <= _ALLOWED_NEW_NAMES,
        "no_db_in_sub": not any(n.startswith("sub/") and ".neuroloom.db" in n for n in after_names),
        "no_trigger_fired": bool(fired) and not any(fired.values()),
        "allowlist_only": bool(paths) and all(p == _CONTEXT for p in paths),
        # Positive minimum: a context read AFTER the cd, carrying the root's key.
        "context_request_after_cd": any((m, p) == _CONTEXT and carried for m, p, carried in post_cd),
        "all_hooks_exit_zero": all(r.returncode == 0 for r in seq.runs),
    }


@pytest.fixture()
def slim() -> Iterator[SlimRecorder]:
    rec = SlimRecorder()
    try:
        yield rec
    finally:
        rec.close()


def _arm_bait(tmp_path: Path) -> tuple[ci_support.Bait, str, list[str]]:
    bait = ci_support.make_bait_repo(tmp_path / "bait")
    _make_lifecycle_files(bait.root)
    return bait, ci_support.tree_hash(bait.root), ci_support.presence_snapshot(bait.root)


def _debounce_rows(db_path: Path) -> int:
    """Rows in the code-graph debounce tables (a hook that did code-graph work leaves some)."""
    conn = sqlite3.connect(str(db_path))
    try:
        return int(
            conn.execute(
                "SELECT (SELECT COUNT(*) FROM debounce) + (SELECT COUNT(*) FROM debounce_files)"
            ).fetchone()[0]
        )
    finally:
        conn.close()


def _session_row(db_path: Path) -> str:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT session_id FROM sessions").fetchall()
    finally:
        conn.close()
    return ",".join(str(r[0]) for r in row)


@pytest.mark.parametrize("interpreter", _INTERPRETERS)
class TestCiLifecycleWithBaitAndControl:
    def test_ci_run_leaves_the_checkout_untouched_and_sends_only_context_reads(
        self, interpreter: str, tmp_path: Path, black_hole: ci_support.BlackHole, slim: SlimRecorder
    ) -> None:
        bait, before_hash, before_names = _arm_bait(tmp_path)
        seed_rows = bait.seeded_buffer_rows

        seq = _run_sequence(
            bait.root, slim, black_hole, tmp_path / "scratch", ci=True, interpreter=interpreter, code_write=True
        )
        report = _lifecycle_report(bait, slim, seq, before_hash=before_hash, before_names=before_names)

        failed = sorted(name for name, ok in report.items() if not ok)
        assert not failed, f"CI lifecycle predicates failed: {failed}"

        # SessionStart printed the ready line, and the stale session was replaced.
        ready = seq.runs[0].stdout.strip() == _ss_mod._CI_READY_LINE
        assert ready, "CI lifecycle: SessionStart stdout is not the ready line"
        sessions = _session_row(bait.db_path)
        single_session = _SESSION_ID_RE.match(sessions) is not None
        replaced = sessions != "sess-1700000000-aabbccdd"
        assert single_session, "CI lifecycle: no single CI session row"
        assert replaced, "CI lifecycle: the stale session row was not replaced"

        # Nothing connected to the dead pip index either, and code_graph_sync (which
        # ran for two code-file writes) left no debounce state: it did no work.
        hole_untouched = black_hole.connection_count() == 0
        assert hole_untouched, "CI lifecycle: something connected to the black hole"
        assert _debounce_rows(bait.db_path) == 0, "CI lifecycle: code_graph_sync left debounce state"

        # The hooks did local work (so "sent nothing" is not "did nothing"): one
        # buffered row per capture event on top of the seeded rows, none flushed.
        assert _buffer_count(bait.db_path) == seed_rows + seq.captures, "CI lifecycle: capture rows not recorded locally"

        # The post-cd Read was keyed to the project root, not to the new cwd.
        post_cd = [
            json.loads(body)
            for (m, p, _c), body in zip(slim.requests[seq.after_cd_index :], slim.bodies[seq.after_cd_index :])
            if (m, p) == _CONTEXT
        ]
        sent = sorted(str(b.get("file_path")) for b in post_cd)
        root_relative = sent == ["sub/agent.py", "sub/inner.py"]
        assert root_relative, "CI lifecycle: post-cd reads were not sent root-relative"

    def test_non_ci_control_fires_every_bait_trigger(
        self, interpreter: str, tmp_path: Path, black_hole: ci_support.BlackHole, slim: SlimRecorder
    ) -> None:
        """Same fixture, same sequence, same predicates -- outside CI mode.

        If this stops failing the CI predicates, the CI test above proves nothing.
        """
        bait, before_hash, before_names = _arm_bait(tmp_path)

        seq = _run_sequence(
            bait.root, slim, black_hole, tmp_path / "scratch", ci=False, interpreter=interpreter, code_write=False
        )
        report = _lifecycle_report(bait, slim, seq, before_hash=before_hash, before_names=before_names)

        # The trigger table: every listed trigger fired.
        fired = bait.fired()
        assert set(fired) >= _EXPECTED_TRIGGERS, f"control: trigger table lost entries: {sorted(fired)}"
        not_fired = sorted(name for name, hit in fired.items() if not hit)
        assert not not_fired, f"control: bait triggers that did NOT fire in normal mode: {not_fired}"
        # A session was started on the server.
        assert ("POST", "/api/v1/sessions/start") in slim.paths(), "control: no session was started on the server"
        # And the CI predicates fail for the reasons they are meant to detect.
        assert report["tree_unchanged"] is False, "control: the tree hash did not change in normal mode"
        assert report["no_trigger_fired"] is False, "control: no_trigger_fired still true in normal mode"
        assert report["allowlist_only"] is False, "control: traffic stayed inside the CI allowlist in normal mode"
        # The control is not broken in the ways that would make the above meaningless.
        assert report["all_hooks_exit_zero"], "control: a hook exited non-zero, the control is unreliable"
        assert report["context_request_after_cd"], "control: no context read after the cd"
        hole_untouched = black_hole.connection_count() == 0
        assert hole_untouched, "control: something connected to the black hole (the offline shim leaked)"

    def test_non_ci_code_write_control_sends_a_code_graph_sync(
        self, interpreter: str, tmp_path: Path, black_hole: ci_support.BlackHole, slim: SlimRecorder
    ) -> None:
        """Proves the CI test's code-write steps are not vacuous.

        In normal mode a code-file write queues the file and a background thread
        POSTs it (through the stub codeweaver, so this holds on python3.10 too).
        The CI hook must therefore be doing nothing on purpose, not by accident
        of the machine it ran on.
        """
        bait, _hash, _names = _arm_bait(tmp_path)
        stub = ci_support.write_codeweaver_stub(tmp_path / "codeweaver-stub")
        event = {"tool_name": "Write", "tool_input": {"file_path": str(bait.root / "app.py")}, "cwd": str(bait.root)}
        run = ci_support.run_hook(
            "pyhooks.code_graph_sync",
            stdin=json.dumps(event),
            cwd=bait.root,
            scratch=tmp_path / "scratch",
            black_hole_url=black_hole.url,
            env_extra={"CLAUDE_PROJECT_DIR": str(bait.root), "NEUROLOOM_API_BASE": slim.url},
            interpreter=interpreter,
            control=True,
            timeout=30.0,
            pythonpath_extra=(stub,),
        )
        sync_calls = [(m, p, c) for m, p, c in slim.requests if p == "/api/v1/code-graph/sync"]
        used_stub = any(b'"stub": true' in body for (_m, p, _c), body in zip(slim.requests, slim.bodies) if p == "/api/v1/code-graph/sync")
        exited_zero = run.returncode == 0
        assert exited_zero, "code-write control: the hook exited non-zero"
        assert sync_calls == [("POST", "/api/v1/code-graph/sync", True)], "code-write control: no keyed code-graph sync was sent"
        assert used_stub, "code-write control: the sync did not come from the stub codeweaver"
        assert _debounce_rows(bait.db_path) >= 1, "code-write control: no debounce state was written"
        hole_untouched = black_hole.connection_count() == 0
        assert hole_untouched, "code-write control: something connected to the black hole"


def _git_internals(root: Path) -> dict[str, bytes]:
    """Bytes of the git files a hook could tamper with to hide itself."""
    out: dict[str, bytes] = {}
    git_dir = root / ".git"
    for rel in ("config", "info/exclude"):
        path = git_dir / rel
        out[rel] = path.read_bytes() if path.is_file() else b"<absent>"
    hooks = git_dir / "hooks"
    if hooks.is_dir():
        for path in sorted(hooks.iterdir()):
            out[f"hooks/{path.name}"] = path.read_bytes() if path.is_file() else b"<dir>"
    return out


@pytest.mark.parametrize("interpreter", _INTERPRETERS)
def test_ci_run_in_an_ignored_repository_keeps_git_status_empty(
    interpreter: str, tmp_path: Path, black_hole: ci_support.BlackHole, slim: SlimRecorder, ignored_git_repo: Path
) -> None:
    """The user-visible promise: a CI job leaves ``git status --porcelain`` empty.

    Limit: the fixture already ignores ``.neuroloom.db*``, so this cannot see a
    hook editing ``.gitignore`` (the bait test and its presence diff cover that).
    It does snapshot ``.git/config``, ``.git/info/exclude`` and ``.git/hooks/*``
    so a hook cannot make the status empty by tampering with git itself.
    """
    root = ignored_git_repo
    conn = _db_mod.open_db(root / ".neuroloom.db")
    assert conn is not None, "fixture error: could not create the state database"
    try:
        conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('api_key', ?)", (ci_support.SENTINEL_KEY,))
        conn.commit()
    finally:
        conn.close()
    _make_lifecycle_files(root)
    assert ci_support.git(root, "add", "-A").returncode == 0, "fixture error: git add failed"
    assert ci_support.git(root, "commit", "-q", "-m", "files").returncode == 0, "fixture error: git commit failed"
    clean_before = ci_support.git(root, "status", "--porcelain").stdout == ""
    assert clean_before, "fixture error: repository not clean before the run"
    internals_before = _git_internals(root)

    seq = _run_sequence(root, slim, black_hole, tmp_path / "scratch", ci=True, interpreter=interpreter, code_write=True)

    status = ci_support.git(root, "status", "--porcelain")
    clean_after = status.stdout == ""
    assert status.returncode == 0, "git status failed"
    assert clean_after, "git status --porcelain is not empty after a CI lifecycle run"
    git_untouched = _git_internals(root) == internals_before
    assert git_untouched, ".git/config, .git/info/exclude or .git/hooks changed during the CI run"
    assert (root / ".neuroloom.db").exists(), "the state database should exist (and be ignored)"
    assert all(r.returncode == 0 for r in seq.runs), "a hook exited non-zero"
    assert any(p == _CONTEXT for p in slim.paths()), "no context read reached the recorder"
    hole_untouched = black_hole.connection_count() == 0
    assert hole_untouched, "something connected to the black hole"
