"""
Self-tests for ``tests/ci_support.py`` (D178d Phase 1).

Each shared fixture is proven here against *today's* hooks, so later phases can
trust it: a trap that has never been sprung proves nothing.  These tests only
use public hook entry points and record names, never headers or environments.
"""

from __future__ import annotations

import io
import os
import socket
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

import pyhooks.session_start as _ss_mod
from tests import ci_support

_FLOOR_PYTHON = Path("/usr/local/bin/python3.10")
_CLOSED_LOOPBACK = "http://127.0.0.1:9"


# ---------------------------------------------------------------------------
# credential_env_names and the autouse hygiene fixtures
# ---------------------------------------------------------------------------


def test_credential_env_names_matches_credential_shaped_names_only() -> None:
    environ = {
        "MY_API_KEY": "x",
        "gh_token": "x",
        "DB_PASSWORD": "x",
        "CLIENT_SECRET": "x",
        "SOME_CREDENTIALS": "x",
        "AUTH_BEARER": "x",
        "PATH": "x",
        "HOME": "x",
        "NEUROLOOM_API_BASE": "x",
    }
    assert ci_support.credential_env_names(environ) == [
        "AUTH_BEARER",
        "CLIENT_SECRET",
        "DB_PASSWORD",
        "MY_API_KEY",
        "SOME_CREDENTIALS",
        "gh_token",
    ]


def test_autouse_fixtures_clear_mode_and_credentials() -> None:
    assert "NEUROLOOM_MODE" not in os.environ
    if os.environ.get("NEUROLOOM_RUN_LIVE_SMOKE") != "1":
        assert ci_support.credential_env_names(os.environ) == []
    else:
        assert set(ci_support.credential_env_names(os.environ)) <= {"NEUROLOOM_SLEEVED_API_KEY"}


# ---------------------------------------------------------------------------
# Black-hole listener
# ---------------------------------------------------------------------------


def test_black_hole_counts_only_after_marker(black_hole: ci_support.BlackHole) -> None:
    assert black_hole.connection_count() == 0

    for _ in range(3):
        with socket.create_connection(("127.0.0.1", black_hole.port), timeout=2) as sock:
            sock.sendall(b"GET / HTTP/1.1\r\n\r\n")
    # Connections are queued before the marker, so the marker read sees all three.
    assert black_hole.connection_count() == 3
    # Markers are never counted: a second read is stable.
    assert black_hole.connection_count() == 3


def test_black_hole_counts_a_silent_connect(black_hole: ci_support.BlackHole) -> None:
    with socket.create_connection(("127.0.0.1", black_hole.port), timeout=2):
        pass
    assert black_hole.connection_count() == 1


# ---------------------------------------------------------------------------
# Ignored git repository
# ---------------------------------------------------------------------------


def test_ignored_git_repo_ignores_db_and_sidecars_and_is_clean(ignored_git_repo: Path) -> None:
    for name in (".neuroloom.db", ".neuroloom.db-wal", ".neuroloom.db-shm"):
        (ignored_git_repo / name).write_bytes(b"x")
        done = ci_support.git(ignored_git_repo, "check-ignore", "-q", "--", name)
        assert done.returncode == 0, f"{name} is not ignored"
    status = ci_support.git(ignored_git_repo, "status", "--porcelain")
    assert status.returncode == 0
    assert status.stdout == ""


# ---------------------------------------------------------------------------
# Tree hash and presence snapshot
# ---------------------------------------------------------------------------


def test_tree_hash_ignores_db_but_presence_snapshot_sees_it(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("one", encoding="utf-8")
    hash_before = ci_support.tree_hash(tmp_path)
    names_before = ci_support.presence_snapshot(tmp_path)

    (tmp_path / ".neuroloom.db").write_bytes(b"db")
    (tmp_path / ".neuroloom.db-wal").write_bytes(b"wal")

    assert ci_support.tree_hash(tmp_path) == hash_before
    names_after = ci_support.presence_snapshot(tmp_path)
    assert names_after != names_before
    assert ".neuroloom.db" in names_after
    # Exclusions are caller-supplied, not baked in.
    assert ci_support.presence_snapshot(tmp_path, exclude=(".neuroloom.db*",)) == names_before


def test_presence_exclusion_matches_the_relative_path_not_the_file_name(tmp_path: Path) -> None:
    """``.neuroloom.db*`` hides the root-level database only, never ``sub/.neuroloom.db``."""
    (tmp_path / "sub").mkdir()
    (tmp_path / ".neuroloom.db").write_bytes(b"db")
    (tmp_path / ".neuroloom.db-wal").write_bytes(b"wal")
    (tmp_path / "sub" / ".neuroloom.db").write_bytes(b"stray")
    names = ci_support.presence_snapshot(tmp_path, exclude=(".neuroloom.db*",))
    assert names == ["sub/.neuroloom.db"]
    assert ci_support.presence_snapshot(tmp_path, exclude=("sub/.neuroloom.db",)) == [
        ".neuroloom.db",
        ".neuroloom.db-wal",
    ]


def test_tree_hash_changes_on_edit_and_on_new_file(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("one", encoding="utf-8")
    base = ci_support.tree_hash(tmp_path)
    (tmp_path / "a.txt").write_text("two", encoding="utf-8")
    edited = ci_support.tree_hash(tmp_path)
    (tmp_path / "b.txt").write_text("new", encoding="utf-8")
    added = ci_support.tree_hash(tmp_path)
    assert len({base, edited, added}) == 3


# ---------------------------------------------------------------------------
# Bait repository: every listed trigger fires against today's session_start
# ---------------------------------------------------------------------------


def _run_session_start_in_process(
    bait: ci_support.Bait, monkeypatch: pytest.MonkeyPatch
) -> ci_support.FakeApi:
    monkeypatch.chdir(bait.root)
    monkeypatch.setenv("NEUROLOOM_API_BASE", _CLOSED_LOOPBACK)
    api = ci_support.FakeApi()
    with (
        patch("pyhooks.session_start._http.post_json", side_effect=api),
        patch("pyhooks.session_start._codeweaver_bootstrap_and_upgrade", return_value=None),
        patch("pyhooks.session_start.sys.stdout", io.StringIO()),
    ):
        _ss_mod.main()
        ci_support.join_hook_threads()
    return api


def test_bait_untouched_before_any_hook_runs(bait_repo: ci_support.Bait) -> None:
    assert bait_repo.fired() == {
        "gitignore_written": False,
        "claudemd_appended": False,
        "buffer_flushed": False,
    }


def test_bait_buffer_trigger_checks_seeded_ids_not_the_row_count(bait_repo: ci_support.Bait) -> None:
    """A flush followed by new rows (as a CI capture adds) must still fire the trigger."""
    assert len(bait_repo.seeded_buffer_ids) == bait_repo.seeded_buffer_rows == 3
    conn = sqlite3.connect(str(bait_repo.db_path))
    try:
        conn.execute("DELETE FROM event_buffer WHERE id = ?", (bait_repo.seeded_buffer_ids[0],))
        for n in range(5):
            conn.execute(
                "INSERT INTO event_buffer (payload, created_at, payload_type) VALUES (?, ?, ?)",
                ("{}", 1.0 + n, "observation"),
            )
        conn.commit()
    finally:
        conn.close()
    assert bait_repo.buffer_rows() >= bait_repo.seeded_buffer_rows, "fixture error: count masks the delete"
    assert bait_repo.fired()["buffer_flushed"] is True


def test_bait_every_trigger_fires_against_todays_session_start(
    bait_repo: ci_support.Bait, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _run_session_start_in_process(bait_repo, monkeypatch)

    fired = bait_repo.fired()
    assert sorted(fired) == ["buffer_flushed", "claudemd_appended", "gitignore_written"]
    assert fired == {name: True for name in fired}, "a bait trigger did not fire"
    # The flush really reached both endpoints.
    assert any(p.endswith("/observations/batch") for p in api.paths)
    assert any(p.endswith("/documents/ingest/batch") for p in api.paths)
    # The unrelated .mcp.json is not a trigger when unarmed and is left alone.
    assert (bait_repo.root / ".mcp.json").read_bytes() == bait_repo.seeded_mcp_json


def test_armed_bait_mcp_json_trigger_fires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bait = ci_support.make_bait_repo(tmp_path / "armed", arm_mcp_json=True)
    assert not (bait.root / ".mcp.json").exists()
    assert bait.fired()["mcp_json_written"] is False

    _run_session_start_in_process(bait, monkeypatch)

    fired = bait.fired()
    assert sorted(fired) == [
        "buffer_flushed",
        "claudemd_appended",
        "gitignore_written",
        "mcp_json_written",
    ]
    assert fired == {name: True for name in fired}, "a bait trigger did not fire"


# ---------------------------------------------------------------------------
# Forbid fixture
# ---------------------------------------------------------------------------


def test_forbid_sentinel_escapes_main_of_todays_session_start(
    bait_repo: ci_support.Bait, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(bait_repo.root)
    monkeypatch.setenv("NEUROLOOM_API_BASE", _CLOSED_LOOPBACK)
    start_before = threading.Thread.start

    raised = False
    with patch("pyhooks.session_start.sys.stdout", io.StringIO()):
        with ci_support.forbid_side_effects() as recorder:
            try:
                _ss_mod.main()
            except ci_support.ForbiddenCallError:
                raised = True

    assert raised, "the sentinel was swallowed before it left main()"
    # The stale session row makes Step 4 start a thread; that is the first forbidden call.
    assert "threading.Thread.start" in recorder.calls
    # Patches are removed on exit.
    assert threading.Thread.start is start_before
    assert subprocess.run.__module__ == "subprocess"


def test_forbid_sentinel_is_not_an_exception_subclass() -> None:
    assert issubclass(ci_support.ForbiddenCallError, BaseException)
    assert not issubclass(ci_support.ForbiddenCallError, Exception)


def test_forbid_records_each_patched_function_and_passes_loopback_through(
    black_hole: ci_support.BlackHole,
) -> None:
    import urllib.request

    with ci_support.forbid_side_effects() as recorder:
        for call in (
            lambda: urllib.request.urlopen("http://example.invalid/x", timeout=1),
            lambda: subprocess.run(["true"]),
            lambda: subprocess.Popen(["true"]),
            lambda: threading.Thread(target=lambda: None).start(),
        ):
            with pytest.raises(ci_support.ForbiddenCallError):
                call()
        # Loopback is allowed through to the real urlopen (the black hole closes at once).
        before = len(recorder.calls)
        with pytest.raises(OSError):
            urllib.request.urlopen(black_hole.url + "/x", timeout=2)
        assert len(recorder.calls) == before

    assert recorder.calls == [
        "urllib.request.urlopen",
        "subprocess.run",
        "subprocess.Popen",
        "threading.Thread.start",
    ]


# ---------------------------------------------------------------------------
# Hook list from hooks/hooks.json
# ---------------------------------------------------------------------------


def test_registered_hook_modules_lists_every_registered_hook() -> None:
    assert ci_support.registered_hook_modules() == [
        "pyhooks.preload_context",
        "pyhooks.session_start",
        "pyhooks.capture",
        "pyhooks.code_graph_sync",
        "pyhooks.cache_prefetch",
    ]


# ---------------------------------------------------------------------------
# Subprocess hook runner
# ---------------------------------------------------------------------------


def test_runner_import_origin_is_this_checkout(tmp_path: Path) -> None:
    env = ci_support._minimal_env(sys.executable, ci_support.PLUGIN_ROOT, tmp_path, "http://127.0.0.1:1", None)
    origin = ci_support.assert_import_origin(sys.executable, ci_support.PLUGIN_ROOT, env, tmp_path)
    assert Path(origin).is_relative_to(ci_support.PLUGIN_ROOT.resolve())


def test_runner_import_origin_rejects_divergent_editable_install(tmp_path: Path) -> None:
    env = ci_support._minimal_env(sys.executable, ci_support.PLUGIN_ROOT, tmp_path, "http://127.0.0.1:1", None)
    env.pop("PYTHONPATH")
    probe = subprocess.run(
        [sys.executable, "-c", "import pyhooks; print(pyhooks.__file__)"],
        capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=30, check=False,
    )
    origin = probe.stdout.strip()
    if probe.returncode != 0:
        pytest.skip("pyhooks not importable without PYTHONPATH")
    if origin and ci_support.PLUGIN_ROOT.resolve() in Path(origin).resolve().parents:
        pytest.skip("no divergent editable install")
    with pytest.raises(ci_support.ImportOriginError):
        ci_support.assert_import_origin(sys.executable, ci_support.PLUGIN_ROOT, env, tmp_path)


def test_runner_refuses_a_decoy_import_before_running_any_hook(
    tmp_path: Path, black_hole: ci_support.BlackHole
) -> None:
    decoy = tmp_path / "decoy"
    (decoy / "pyhooks").mkdir(parents=True)
    (decoy / "pyhooks" / "__init__.py").write_text("", encoding="utf-8")
    marker = tmp_path / "hook-ran"
    (decoy / "run_hook.py").write_text(
        f"open({str(marker)!r}, 'w').close()\n", encoding="utf-8"
    )

    with pytest.raises(ci_support.ImportOriginError):
        ci_support.run_hook(
            "pyhooks.cache_prefetch",
            cwd=tmp_path,
            scratch=tmp_path / "scratch",
            black_hole_url=black_hole.url,
            plugin_root=decoy,
            expected_root=ci_support.PLUGIN_ROOT,
        )
    assert not marker.exists()


def test_runner_uses_a_minimal_environment(tmp_path: Path, black_hole: ci_support.BlackHole) -> None:
    run = ci_support.run_hook(
        "pyhooks.cache_prefetch",
        stdin="{}",
        cwd=tmp_path,
        scratch=tmp_path / "scratch",
        black_hole_url=black_hole.url,
    )
    assert run.returncode == 0
    assert not set(run.env_names) - {
        "PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE",
        "CLAUDE_PLUGIN_DATA", "PIP_INDEX_URL", "PIP_RETRIES", "PIP_TIMEOUT",
        "PIP_DISABLE_PIP_VERSION_CHECK", "NEUROLOOM_API_BASE", "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM", "GIT_TERMINAL_PROMPT",
    }
    assert ci_support.credential_env_names(dict.fromkeys(run.env_names, "")) == []


def test_runner_control_run_springs_the_bait_via_the_shim(
    bait_repo: ci_support.Bait, black_hole: ci_support.BlackHole, tmp_path: Path
) -> None:
    run = ci_support.run_hook(
        "pyhooks.session_start",
        cwd=bait_repo.root,
        scratch=tmp_path / "scratch",
        black_hole_url=black_hole.url,
        control=True,
    )
    assert run.returncode == 0
    fired = bait_repo.fired()
    # The API is a black hole, so the flush cannot succeed; the file edits still happen.
    assert fired["gitignore_written"] is True
    assert fired["claudemd_appended"] is True
    assert fired["buffer_flushed"] is False
    assert black_hole.connection_count() >= 1
    # Codeweaver stayed out (offline flag plus the shim's no-op): nothing was created
    # in the scratch plugin-data dir.
    assert not (tmp_path / "scratch" / "plugin-data" / ".venv").exists()


def test_runner_launcher_run_accepts_an_interpreter_parameter(
    tmp_path: Path, black_hole: ci_support.BlackHole
) -> None:
    run = ci_support.run_hook(
        "pyhooks.cache_prefetch",
        stdin="{}",
        cwd=tmp_path,
        scratch=tmp_path / "scratch",
        black_hole_url=black_hole.url,
        interpreter=sys.executable,
    )
    assert run.returncode == 0
    assert black_hole.connection_count() == 0


@pytest.mark.skipif(not _FLOOR_PYTHON.exists(), reason="floor interpreter not installed")
def test_runner_works_under_the_floor_interpreter(
    tmp_path: Path, black_hole: ci_support.BlackHole
) -> None:
    run = ci_support.run_hook(
        "pyhooks.cache_prefetch",
        stdin="{}",
        cwd=tmp_path,
        scratch=tmp_path / "scratch",
        black_hole_url=black_hole.url,
        interpreter=str(_FLOOR_PYTHON),
    )
    assert run.returncode == 0


def test_runner_control_shim_noop_is_live_for_session_start(
    bait_repo: ci_support.Bait,
    black_hole: ci_support.BlackHole,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shim's own codeweaver no-op works even without the offline flag.

    The no-op is replaced by a marker writer; the marker appearing proves the
    rebinding reaches the code session_start actually runs.
    """
    marker = tmp_path / "bootstrap-replaced"
    monkeypatch.setattr(
        ci_support,
        "_SHIM_SOURCE",
        ci_support._SHIM_SOURCE.replace(
            "lambda *a, **k: None", f"lambda *a, **k: open({str(marker)!r}, 'w').close()"
        ),
    )
    run = ci_support.run_hook(
        "pyhooks.session_start",
        cwd=bait_repo.root,
        scratch=tmp_path / "scratch",
        black_hole_url=black_hole.url,
        control=True,
        env_extra={"NEUROLOOM_CODEWEAVER_OFFLINE": ""},
    )
    assert run.returncode == 0
    assert marker.exists(), "the shim's bootstrap replacement was not the code that ran"


# ---------------------------------------------------------------------------
# SlimRecorder, egress shim, codeweaver stub, interpreter params
# ---------------------------------------------------------------------------


def test_slim_recorder_keeps_bodies_and_a_carried_flag_but_no_headers() -> None:
    import urllib.request

    rec = ci_support.SlimRecorder()
    try:
        for token in (ci_support.SENTINEL_KEY, "someone-else"):
            req = urllib.request.Request(
                rec.url + "/api/v1/x", data=b"body-" + token[:3].encode(), method="POST",
                headers={"Authorization": f"Token {token}"},
            )
            urllib.request.urlopen(req, timeout=5).read()
        assert rec.requests == [("POST", "/api/v1/x", True), ("POST", "/api/v1/x", False)]
        assert rec.bodies == [b"body-e38", b"body-som"]
        assert rec.paths() == [("POST", "/api/v1/x")] * 2
        assert not any(isinstance(v, (str, bytes)) and ci_support.SENTINEL_KEY in v
                       for r in rec.requests for v in r)
    finally:
        rec.close()


def _run_probe(source: str, tmp_path: Path, shim_dir: Path | None, log: Path | None, **extra: str) -> subprocess.CompletedProcess[str]:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)}
    paths = [str(p) for p in (shim_dir,) if p is not None]
    if paths:
        env["PYTHONPATH"] = os.pathsep.join(paths)
    if log is not None:
        env[ci_support.EGRESS_LOG_ENV] = str(log)
    env.update(extra)
    return subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=30, check=False
    )


_EGRESS_PROBE = """
import socket, urllib.request
out = []
for fn in (
    lambda: urllib.request.urlopen("https://pypi.org/pypi/x/json", timeout=1),
    lambda: socket.create_connection(("203.0.113.9", 80), timeout=1),
    lambda: socket.socket().connect(("203.0.113.9", 80)),
):
    try:
        fn()
        out.append("passed")
    except OSError:
        out.append("blocked")
print(",".join(out))
"""


def test_egress_shim_records_and_blocks_external_calls_but_not_loopback(
    tmp_path: Path, black_hole: ci_support.BlackHole
) -> None:
    shim_dir, log = ci_support.write_egress_shim(tmp_path / "shim")
    done = _run_probe(_EGRESS_PROBE, tmp_path, shim_dir, log)
    assert done.stdout.strip() == "blocked,blocked,blocked"
    lines = ci_support.egress_attempts(log)
    assert any(ln.startswith("urlopen pypi.org") for ln in lines)
    assert "connect 203.0.113.9" in lines and "resolve 203.0.113.9" in lines
    # Loopback goes through untouched and leaves no record.
    log.unlink()
    loop = _run_probe(
        f"import socket; socket.create_connection(('127.0.0.1', {black_hole.port}), timeout=2).close(); print('ok')",
        tmp_path, shim_dir, log,
    )
    assert loop.stdout.strip() == "ok"
    assert ci_support.egress_attempts(log) == []


def test_egress_shim_can_hide_the_codeweaver_distribution(tmp_path: Path) -> None:
    shim_dir, _log = ci_support.write_egress_shim(tmp_path / "shim")
    probe = (
        "import importlib.metadata as m\n"
        "try:\n    m.version('neuroloom-codeweaver'); print('visible')\n"
        "except m.PackageNotFoundError:\n    print('hidden')"
    )
    hidden = _run_probe(probe, tmp_path, shim_dir, None, **{ci_support.EGRESS_HIDE_CODEWEAVER_ENV: "1"})
    assert hidden.stdout.strip() == "hidden"


def test_codeweaver_stub_is_importable_through_pythonpath(tmp_path: Path) -> None:
    stub = ci_support.write_codeweaver_stub(tmp_path / "stub")
    code = (
        "import codeweaver, pathlib\n"
        "r = codeweaver.parse_files([pathlib.Path('a.py')], pathlib.Path('.'))\n"
        "print(r['stub'], r['files'])"
    )
    done = _run_probe(code, tmp_path, stub, None)
    assert done.stdout.strip() == "True ['a.py']"


def test_interpreter_params_always_lists_both_interpreters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEUROLOOM_TEST_PY310", "/nonexistent/python3.10")
    params = ci_support.interpreter_params()
    assert [p.id for p in params] == ["suite-python", "py310"]
    assert any(m.name == "skipif" and m.args == (True,) for m in params[1].marks), "missing interpreter must be a SKIP"
