"""
Root-pinning tests for D178d Phase 2 (FR-B1 to FR-B3, spec §5.3 and §6.3).

Every hook must find its database, API key, session row, cache keys and debounce
key from ``containment.project_root()`` (``CLAUDE_PROJECT_DIR``, else the process
cwd) -- never from wherever the hook process happens to be standing after a
``cd``.  Analogy: the project root is the building's front door, the process cwd
is whichever room the visitor wandered into; the mail (key, session, caches)
lives at the front desk, not in each room.

Layout used throughout (all under pytest's ``tmp_path``)::

    tmp_path/
        proj/                   <- project root (CLAUDE_PROJECT_DIR); holds the seeded .neuroloom.db
            sub/a.py            <- where the session "cd"s to
            nested/a.py         <- nested/.neuroloom.db holds a DIFFERENT key (nested-project test)
        other/                  <- a sibling directory, outside the root

Test layering: in-process ``main()`` calls with the four side-effect surfaces
patched (``threading.Thread`` start/join recorded and never run,
``_codeweaver_bootstrap_and_upgrade``, ``_http.post_json``,
``_workspace_config.gated_reconcile``), plus a loopback recording HTTP server
for the tests that must see a request actually arrive.

Output hygiene: recorders store booleans and names (``key_is_root``), never a
header, key or environment, so a failing assertion cannot print a credential.
Every assertion message in this module is distinct so ``-rf`` lines are
unambiguous.  New names are imported inside test bodies, so a missing name is a
red test and not a collection error.
"""

import ast
import hashlib
import http.server
import io
import json
import os
import re
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple

import pytest

import pyhooks.cache_prefetch as _prefetch_mod
import pyhooks.capture as _capture_mod
import pyhooks.code_graph_sync as _cgs_mod
import pyhooks.containment as _containment
import pyhooks.db as _db_mod
import pyhooks.preload_context as _pc_mod
import pyhooks.session_start as _ss_mod

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

ROOT_KEY = "root-key-sentinel-0001"
NESTED_KEY = "nested-key-sentinel-0002"
ROOT_SESSION = "sess-1700000000-aaaaaaaa"

HOOKS = ["session_start", "capture", "preload_context", "cache_prefetch", "code_graph_sync"]
EVENT_HOOKS = ["capture", "preload_context", "cache_prefetch", "code_graph_sync"]


def test_the_hook_list_covers_every_hook_registered_in_hooks_json() -> None:
    """Keeps the explicit HOOKS list honest: a newly registered hook cannot be silently missed."""
    from tests import ci_support

    registered = {m.removeprefix("pyhooks.") for m in ci_support.registered_hook_modules()}
    assert registered == set(HOOKS), "hook-list-guard: HOOKS differs from the modules registered in hooks/hooks.json"


# ---------------------------------------------------------------------------
# Layout and seeding
# ---------------------------------------------------------------------------


class Layout(NamedTuple):
    root: Path
    sub: Path
    other: Path
    db: Path


def _seed_db(directory: Path, key: str, session_id: str | None = None) -> Path:
    """Create ``directory/.neuroloom.db`` with an api_key row (and a session row keyed by directory)."""
    db_path = directory / ".neuroloom.db"
    conn = _db_mod.open_db(db_path)
    assert conn is not None, "open_db failed on a writable tmp directory"
    conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('api_key', ?)", (key,))
    if session_id is not None:
        conn.execute(
            "INSERT OR REPLACE INTO sessions (session_key, session_id, started_at, last_submit_ms) "
            "VALUES (?, ?, datetime('now'), 0)",
            (str(directory), session_id),
        )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture()
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Layout:
    """proj/ (seeded, CLAUDE_PROJECT_DIR) with sub/ and nested/, plus other/; cwd is the root."""
    root = (tmp_path / "proj").resolve()
    sub = root / "sub"
    other = (tmp_path / "other").resolve()
    for d in (sub, root / "nested", other):
        d.mkdir(parents=True)
    for d in (sub, root / "nested"):
        (d / "a.py").write_text("x = 1\n")
    db = _seed_db(root, ROOT_KEY, ROOT_SESSION)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    monkeypatch.setenv("NEUROLOOM_API_BASE", "http://127.0.0.1:9")  # nothing listens; never contacted
    monkeypatch.chdir(root)
    return Layout(root=root, sub=sub, other=other, db=db)


def _rows(db: Path, sql: str) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _digest(directory: Path) -> dict[str, str]:
    """name -> sha256 of every file directly in *directory*: catches an edit, a new row, or a new sidecar."""
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(directory.iterdir()) if p.is_file()
    }


# ---------------------------------------------------------------------------
# Recorders (booleans and names only)
# ---------------------------------------------------------------------------


class Observed:
    """What a hook did, as names and booleans.  Never stores a header or a key."""

    def __init__(self) -> None:
        self.threads: list[tuple[str, tuple[Any, ...]]] = []
        self.posts: list[tuple[str, bool, str]] = []  # (url path, key_is_root, project_name)
        self.fetches: list[tuple[bool, str, str | None]] = []  # (key_is_root, file_path, session_id)
        self.reconcile_roots: list[str] = []  # args[0] of each gated_reconcile call (a path, never a key)
        self.git_cwds: list[str] = []  # the `cwd` keyword of each git subprocess call (never the env)

    def thread_named(self, name: str) -> tuple[Any, ...] | None:
        for target, args in self.threads:
            if target == name:
                return args
        return None

    @property
    def acted(self) -> bool:
        return bool(self.threads or self.posts or self.fetches)


def _install_recorders(monkeypatch: pytest.MonkeyPatch) -> Observed:
    """Patch the four side-effect surfaces so a hook that reaches real behaviour has no real effect."""
    obs = Observed()

    def _start(self: threading.Thread) -> None:
        target = getattr(self, "_target", None)
        obs.threads.append((getattr(target, "__name__", "?"), tuple(getattr(self, "_args", ()))))

    monkeypatch.setattr(threading.Thread, "start", _start)
    monkeypatch.setattr(threading.Thread, "join", lambda self, timeout=None: None)

    def _post_json(url: str, headers: dict[str, str], payload: bytes, timeout: float) -> tuple[int, bytes]:
        try:
            project = str(json.loads(payload.decode("utf-8") or "{}").get("project_name", ""))
        except Exception:
            project = ""
        obs.posts.append(
            (url.split("://", 1)[-1].split("/", 1)[-1], headers.get("Authorization") == f"Token {ROOT_KEY}", project)
        )
        return 200, b'{"session_id":"stub"}'

    monkeypatch.setattr(_ss_mod._http, "post_json", _post_json)
    monkeypatch.setattr(_capture_mod, "post_json", _post_json)
    monkeypatch.setattr(_ss_mod, "_codeweaver_bootstrap_and_upgrade", lambda *a, **k: None)

    def _gated_reconcile(*a: Any, **k: Any) -> SimpleNamespace:
        obs.reconcile_roots.append(str(a[0]))
        return SimpleNamespace(reconcile=None, resolvability=SimpleNamespace(name="STUB"))

    monkeypatch.setattr(_ss_mod._workspace_config, "gated_reconcile", _gated_reconcile)

    real_run = _ss_mod.subprocess.run

    def _run(argv: Any, *a: Any, **k: Any) -> Any:
        if isinstance(argv, list) and argv and argv[0] == "git":
            obs.git_cwds.append(str(k.get("cwd")))
            return SimpleNamespace(stdout="main\n", returncode=0)
        return real_run(argv, *a, **k)

    monkeypatch.setattr(_ss_mod.subprocess, "run", _run)

    def _fetch(
        api_base: str,
        api_key: str,
        file_path: str,
        timeout: float = 3.0,
        fmt: str = "inject",
        query: str | None = None,
        session_id: str | None = None,
    ) -> tuple[str, int]:
        obs.fetches.append((api_key == ROOT_KEY, file_path, session_id))
        return ("CTX", 300)

    monkeypatch.setattr(_pc_mod, "_fetch_context", _fetch)
    return obs


def _stdin(monkeypatch: pytest.MonkeyPatch, event: dict[str, Any]) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))


def _run_main(module: Any) -> None:
    try:
        module.main()
    except SystemExit:
        pass


def _run_hook(
    hook: str, monkeypatch: pytest.MonkeyPatch, obs: Observed, event_cwd: str, file: Path
) -> None:
    """Drive one hook's ``main()`` for a Read/Write of *file*, with the event's cwd set to *event_cwd*."""
    if hook == "session_start":
        _run_main(_ss_mod)
    elif hook == "capture":
        path = str(file)
        _stdin(
            monkeypatch,
            {
                "tool_name": "Read",
                "tool_input": {"file_path": path},
                "tool_response": {"type": "text", "file": {"filePath": path, "content": "x"}},
                "cwd": event_cwd,
            },
        )
        _run_main(_capture_mod)
    elif hook == "preload_context":
        _stdin(monkeypatch, {"tool_name": "Read", "tool_input": {"file_path": str(file)}, "cwd": event_cwd})
        _run_main(_pc_mod)
    elif hook == "cache_prefetch":
        _stdin(monkeypatch, {"cwd": event_cwd})
        _run_main(_prefetch_mod)
    elif hook == "code_graph_sync":
        _stdin(monkeypatch, {"tool_name": "Write", "tool_input": {"file_path": str(file)}, "cwd": event_cwd})
        _run_main(_cgs_mod)
    else:  # pragma: no cover
        raise AssertionError(f"unknown hook {hook}")


def _first_session_id(obs: Observed, hook: str) -> str | None:
    if hook == "capture":
        args = obs.thread_named("_submit")
        if args:
            batch = args[3]
            return str(batch["observations"][0]["session_id"])  # type: ignore[index]
    if hook == "preload_context" and obs.fetches:
        return obs.fetches[0][2]
    return None


# ---------------------------------------------------------------------------
# Root pinning: cd into root/sub, the root's key, session and keys are still used
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hook", HOOKS)
def test_hook_uses_root_state_after_cd_into_a_subdirectory(
    hook: str, layout: Layout, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(layout.sub)  # the process cwd follows `cd`; the root must not
    obs = _install_recorders(monkeypatch)
    if hook == "session_start":
        (layout.root / "CLAUDE.md").write_text("# Project\n")
    _run_hook(hook, monkeypatch, obs, str(layout.sub), layout.sub / "a.py")
    capsys.readouterr()

    if hook == "session_start":
        assert obs.posts and all(is_root for _, is_root, _ in obs.posts), (
            "root-pin/session_start: a session call must go out under the ROOT key after cd sub"
        )
        assert obs.reconcile_roots and obs.reconcile_roots[0] == str(layout.root), (
            "root-pin/session_start: gated_reconcile must be handed the ROOT as its first argument"
        )
        assert _ss_mod._CLAUDEMD_MARKER in (layout.root / "CLAUDE.md").read_text(), (
            "root-pin/session_start: the memory-first block must be appended to the ROOT CLAUDE.md"
        )
        assert not (layout.sub / "CLAUDE.md").exists(), (
            "root-pin/session_start: no CLAUDE.md may be created in the subdirectory"
        )
        assert obs.git_cwds and set(obs.git_cwds) == {str(layout.root)}, (
            "root-pin/session_start: every git subprocess call must run with cwd equal to the ROOT"
        )
        assert any(project == layout.root.name for _, _, project in obs.posts), (
            "root-pin/session_start: project_name must be the root folder name, not the subdirectory name"
        )
        stale = obs.thread_named("_end_session_call")
        assert stale is not None and stale[2] == ROOT_SESSION, (
            "root-pin/session_start: the stale session row seeded under the ROOT key must be the one ended"
        )
        assert (layout.root / ".gitignore").exists(), (
            "root-pin/session_start: the .gitignore edit must land in the ROOT"
        )
        assert not (layout.sub / ".gitignore").exists(), (
            "root-pin/session_start: no .gitignore may be created in the subdirectory"
        )
    elif hook == "capture":
        args = obs.thread_named("_submit")
        assert args is not None, "root-pin/capture: the event must be submitted from the subdirectory cwd"
        assert args[0] == layout.db, "root-pin/capture: the submit thread must be handed the ROOT database path"
        assert args[2] == ROOT_KEY, "root-pin/capture: the submit thread must carry the ROOT key"
        assert _first_session_id(obs, hook) == ROOT_SESSION, (
            "root-pin/capture: the observation must carry the session id from the ROOT sessions row"
        )
    elif hook == "preload_context":
        assert obs.fetches, "root-pin/preload_context: the Read must reach _fetch_context from the subdirectory cwd"
        key_is_root, sent_path, session_id = obs.fetches[0]
        assert key_is_root, "root-pin/preload_context: the request must carry the ROOT key"
        assert session_id == ROOT_SESSION, (
            "root-pin/preload_context: the request must carry the session id of the ROOT sessions row"
        )
        assert sent_path == "sub/a.py", "root-pin/preload_context: the path must be root-relative"
        cache_rows = _rows(layout.db, "SELECT cache_key FROM cache")
        assert len(cache_rows) == 1, "root-pin/preload_context: the cache entry must be written to the ROOT database"
    elif hook == "cache_prefetch":
        args = obs.thread_named("_prefetch")
        assert args is not None, "root-pin/cache_prefetch: the prefetch thread must start for a cd into sub"
        assert args[0] == layout.db, "root-pin/cache_prefetch: the thread must be handed the ROOT database path"
        assert args[2] == ROOT_KEY, "root-pin/cache_prefetch: the thread must carry the ROOT key"
        assert args[4] == str(layout.root), (
            "root-pin/cache_prefetch: the cache-key workspace_root must be the ROOT, not the new cwd"
        )
    elif hook == "code_graph_sync":
        args = obs.thread_named("_background_sync")
        assert args is not None, "root-pin/code_graph_sync: the sync thread must start for an edit under sub"
        expected_key = hashlib.sha256(str(layout.root).encode()).hexdigest()[:16]
        assert args[0] == expected_key, "root-pin/code_graph_sync: the debounce key must be sha256 of the ROOT"
        assert Path(args[1]) == layout.root, "root-pin/code_graph_sync: the code-graph root must be the ROOT"
        assert args[3] == layout.db, "root-pin/code_graph_sync: the thread must be handed the ROOT database path"
        assert args[5] == ROOT_KEY, "root-pin/code_graph_sync: the thread must carry the ROOT key"
        queued = _rows(layout.db, "SELECT workspace_key, file_path FROM debounce_files")
        assert queued == [(expected_key, str(layout.sub / "a.py"))], (
            "root-pin/code_graph_sync: the edited file must be queued in the ROOT database under the root key"
        )

    assert not (layout.sub / ".neuroloom.db").exists(), (
        f"root-pin/{hook}: a .neuroloom.db must not be created in the subdirectory"
    )
    assert not any(layout.sub.glob(".neuroloom.db*")), (
        f"root-pin/{hook}: no .neuroloom.db sidecar may appear in the subdirectory"
    )


@pytest.mark.parametrize("hook", HOOKS)
def test_hook_falls_back_to_the_cwd_when_the_variable_is_unset(
    hook: str, layout: Layout, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Green by nature on today's code (the cwd IS the root); it pins the fallback through the cutover."""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR")
    monkeypatch.chdir(layout.root)
    obs = _install_recorders(monkeypatch)
    _run_hook(hook, monkeypatch, obs, str(layout.root), layout.sub / "a.py")
    capsys.readouterr()

    if hook == "session_start":
        assert obs.posts and all(is_root for _, is_root, _ in obs.posts), (
            "unset-var/session_start: with the variable unset the cwd's own key must be used"
        )
    elif hook == "capture":
        args = obs.thread_named("_submit")
        assert args is not None and args[2] == ROOT_KEY, (
            "unset-var/capture: with the variable unset the cwd's own key must be used"
        )
    elif hook == "preload_context":
        assert obs.fetches and obs.fetches[0][0], (
            "unset-var/preload_context: with the variable unset the cwd's own key must be used"
        )
    elif hook == "cache_prefetch":
        args = obs.thread_named("_prefetch")
        assert args is not None and args[2] == ROOT_KEY, (
            "unset-var/cache_prefetch: with the variable unset the cwd's own key must be used"
        )
    elif hook == "code_graph_sync":
        args = obs.thread_named("_background_sync")
        assert args is not None and args[5] == ROOT_KEY, (
            "unset-var/code_graph_sync: with the variable unset the cwd's own key must be used"
        )


# ---------------------------------------------------------------------------
# Malformed event cwd: the hook skips, in any mode (event-driven hooks only;
# session_start reads no event, so a malformed cwd cannot reach it)
# ---------------------------------------------------------------------------


def _malformed_cwd(layout: Layout, kind: str) -> str:
    if kind == "loop":
        (layout.root / "loop_a").symlink_to(layout.root / "loop_b")
        (layout.root / "loop_b").symlink_to(layout.root / "loop_a")
        return str(layout.root / "loop_a")
    if kind == "nul":
        return str(layout.root) + "/sub\x00evil"
    return str(layout.sub)  # "good": the control


@pytest.mark.parametrize("kind", ["loop", "nul", "good"])
@pytest.mark.parametrize("hook", EVENT_HOOKS)
def test_event_with_a_malformed_cwd_is_skipped_and_a_good_one_is_not(
    hook: str, kind: str, layout: Layout, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The process cwd is the root (it holds the key) so only the event's cwd decides.

    The "good" parametrisation is the positive control: the same harness DOES act.
    """
    obs = _install_recorders(monkeypatch)
    # An absolute target inside the root keeps the path out of the picture: only the cwd varies.
    _run_hook(hook, monkeypatch, obs, _malformed_cwd(layout, kind), layout.sub / "a.py")
    capsys.readouterr()
    if kind == "good":
        assert obs.acted, f"malformed-cwd/{hook}/control: a good event cwd must let the hook act"
    else:
        assert not obs.acted, f"malformed-cwd/{hook}/{kind}: an unusable event cwd must make the hook skip"
        assert _rows(layout.db, "SELECT COUNT(*) FROM debounce_files") == [(0,)], (
            f"malformed-cwd/{hook}/{kind}: nothing may be queued for a sync"
        )


# ---------------------------------------------------------------------------
# Decoy databases: a different key sitting where the process happens to stand
# ---------------------------------------------------------------------------


def _root_key_flags(obs: Observed, hook: str) -> list[bool]:
    """One boolean per outgoing use of a key: True when it was the ROOT's.  Never returns a key."""
    if hook == "session_start":
        return [is_root for _, is_root, _ in obs.posts]
    if hook == "preload_context":
        return [is_root for is_root, _, _ in obs.fetches]
    sites = {"capture": ("_submit", 2), "cache_prefetch": ("_prefetch", 2), "code_graph_sync": ("_background_sync", 5)}
    thread, index = sites[hook]
    args = obs.thread_named(thread)
    return [args[index] == ROOT_KEY] if args else []


def _thread_db_paths(obs: Observed, hook: str) -> list[Path]:
    """The database path each background thread was handed (preload_context starts no thread: it writes inline)."""
    sites = {
        "session_start": ("_flush_event_buffer", 0),
        "capture": ("_submit", 0),
        "cache_prefetch": ("_prefetch", 0),
        "code_graph_sync": ("_background_sync", 3),
    }
    if hook not in sites:
        return []
    thread, index = sites[hook]
    args = obs.thread_named(thread)
    return [args[index]] if args else []


def _assert_threads_use_root_db(obs: Observed, hook: str, layout: Layout, label: str) -> None:
    if hook == "preload_context":
        assert not obs.threads, f"{label}/{hook}: preload_context was expected to start no background thread"
        return
    paths = _thread_db_paths(obs, hook)
    assert paths, f"{label}/{hook}: the background thread was never started, so its database was not exercised"
    assert all(p == layout.db for p in paths), (
        f"{label}/{hook}: the background thread was handed a database other than the ROOT's"
    )


@pytest.mark.parametrize("hook", HOOKS)
def test_decoy_database_in_the_process_cwd_is_neither_read_nor_written(
    hook: str, layout: Layout, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The process stands in root/nested, which holds a database with ANOTHER key: the root's must win."""
    decoy = layout.root / "nested"
    _seed_db(decoy, NESTED_KEY, "sess-1700000000-dddddddd")
    before = _digest(decoy)
    monkeypatch.chdir(decoy)
    obs = _install_recorders(monkeypatch)
    _run_hook(hook, monkeypatch, obs, str(decoy), decoy / "a.py")
    capsys.readouterr()
    flags = _root_key_flags(obs, hook)
    assert flags, f"decoy-nested/{hook}: the hook never reached a send, so the key choice was not exercised"
    assert all(flags), f"decoy-nested/{hook}: a send went out under the decoy's key instead of the ROOT's"
    _assert_threads_use_root_db(obs, hook, layout, "decoy-nested-thread")
    assert _digest(decoy) == before, (
        f"decoy-nested/{hook}: the decoy database changed (a row or a sidecar was written there)"
    )


@pytest.mark.parametrize("hook", HOOKS)
def test_decoy_database_in_a_sibling_cwd_is_neither_read_nor_written(
    hook: str, layout: Layout, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The process stands in other/ (outside the root, with its own database); the event names a path in the root.

    session_start reads no event, so for it this case is only "process cwd outside the root".
    """
    _seed_db(layout.other, NESTED_KEY, "sess-1700000000-ssssssss")
    before = _digest(layout.other)
    monkeypatch.chdir(layout.other)
    obs = _install_recorders(monkeypatch)
    _run_hook(hook, monkeypatch, obs, str(layout.sub), layout.sub / "a.py")
    capsys.readouterr()
    flags = _root_key_flags(obs, hook)
    assert flags, f"decoy-sibling/{hook}: the hook skipped, so the key choice was not exercised"
    assert all(flags), f"decoy-sibling/{hook}: a send went out under the sibling's key instead of the ROOT's"
    _assert_threads_use_root_db(obs, hook, layout, "decoy-sibling-thread")
    assert _digest(layout.other) == before, (
        f"decoy-sibling/{hook}: the sibling database or its directory changed"
    )


# ---------------------------------------------------------------------------
# A loopback recording server: a request must actually ARRIVE
# ---------------------------------------------------------------------------


class _Record(NamedTuple):
    method: str
    path: str
    who: str  # "root" | "nested" | "other" -- never the key itself
    body: dict[str, Any]


class RecordingServer:
    def __init__(self) -> None:
        self.records: list[_Record] = []
        self.labels: dict[str, str] = {ROOT_KEY: "root", NESTED_KEY: "nested"}
        outer = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else {}
                except Exception:
                    body = {}
                token = (self.headers.get("Authorization") or "").removeprefix("Token ")
                outer.records.append(_Record("POST", self.path, outer.labels.get(token, "other"), body))
                payload = json.dumps({"injection_text": "CTX", "nudge_text": "NUDGE"}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: Any) -> None:  # silence
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def context_requests(self) -> list[_Record]:
        return [r for r in self.records if r.path == "/api/v1/context"]


@pytest.fixture()
def server() -> Iterator[RecordingServer]:
    srv = RecordingServer()
    try:
        yield srv
    finally:
        srv.close()


def _preload_over_the_wire(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], event: dict[str, Any]
) -> None:
    _stdin(monkeypatch, event)
    _pc_mod.main()
    capsys.readouterr()


def run_nested_project_read(
    layout: Layout,
    server: RecordingServer,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> list[_Record]:
    """`cd nested` (which holds its OWN db and key), Read a file there; return the /context requests.

    Shared by the nested-project test and the one-off patched-guard run recorded in the result doc.
    """
    nested = layout.root / "nested"
    if not (nested / ".neuroloom.db").exists():
        _seed_db(nested, NESTED_KEY)
    monkeypatch.setenv("NEUROLOOM_API_BASE", server.url)
    monkeypatch.chdir(nested)
    _preload_over_the_wire(
        monkeypatch,
        capsys,
        {"tool_name": "Read", "tool_input": {"file_path": str(nested / "a.py")}, "cwd": str(nested)},
    )
    return server.context_requests()


def test_nested_project_read_goes_out_under_the_roots_key_and_root_relative_path(
    layout: Layout, server: RecordingServer, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Replaces D178e's credential-root tripwire tests (SC-1).

    Once the database is pinned to the root, a nested project's own key is simply
    never read.  Positive assertion: the request ARRIVED, carrying the root's key.
    """
    nested = layout.root / "nested"
    _seed_db(nested, NESTED_KEY, "sess-1700000000-nnnnnnnn")
    nested_before = _digest(nested)
    requests = run_nested_project_read(layout, server, monkeypatch, capsys)
    assert _digest(nested) == nested_before, (
        "nested-project: the nested project's own database must be left byte-identical (and gain no sidecar)"
    )
    assert requests, "nested-project: no /context request reached the server (the hook skipped the send)"
    assert all(r.who == "root" for r in requests), (
        "nested-project: a request went out under a key other than the ROOT's"
    )
    assert [r.body.get("file_path") for r in requests] == ["nested/a.py"], (
        "nested-project: the request must carry the root-relative path nested/a.py"
    )
    assert requests[0].body.get("session_id") == ROOT_SESSION, (
        "nested-project: the request must carry the session id from the ROOT sessions row"
    )


def test_same_relative_name_from_two_cwds_does_not_share_an_inject_cache_entry(
    layout: Layout, server: RecordingServer, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`a.py` read from root/x and root/y are different files; the inject cache must not conflate them."""
    for name in ("x", "y"):
        d = layout.root / name
        d.mkdir()
        (d / "a.py").write_text("v = 1\n")
    monkeypatch.setenv("NEUROLOOM_API_BASE", server.url)
    for name in ("x", "y"):
        _preload_over_the_wire(
            monkeypatch,
            capsys,
            {"tool_name": "Read", "tool_input": {"file_path": "a.py"}, "cwd": str(layout.root / name)},
        )
    assert [r.body.get("file_path") for r in server.context_requests()] == ["x/a.py", "y/a.py"], (
        "inject-cache-key: the second Read (same relative name, other cwd) hit the first one's cache entry"
    )


# ---------------------------------------------------------------------------
# code_graph_sync: a relative file_path resolves against the EVENT's cwd
# ---------------------------------------------------------------------------


def test_relative_edit_path_is_queued_resolved_against_the_event_cwd(
    layout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    obs = _install_recorders(monkeypatch)  # process cwd stays the root; the event says root/sub
    _stdin(
        monkeypatch,
        {"tool_name": "Write", "tool_input": {"file_path": "a.py"}, "cwd": str(layout.sub)},
    )
    _run_main(_cgs_mod)
    queued = [row[0] for row in _rows(layout.db, "SELECT file_path FROM debounce_files")]
    assert queued == [str(layout.sub / "a.py")], (
        "cgs-event-cwd: a relative path from an event in root/sub must queue root/sub/a.py"
    )
    assert obs.thread_named("_background_sync") is not None, (
        "cgs-event-cwd: the inside-root edit must dispatch a sync"
    )


def test_relative_edit_path_from_an_event_outside_the_root_is_skipped(
    layout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    obs = _install_recorders(monkeypatch)
    (layout.other / "a.py").write_text("o = 1\n")
    _stdin(
        monkeypatch,
        {"tool_name": "Write", "tool_input": {"file_path": "a.py"}, "cwd": str(layout.other)},
    )
    _run_main(_cgs_mod)
    assert _rows(layout.db, "SELECT COUNT(*) FROM debounce_files") == [(0,)], (
        "cgs-event-cwd-outside: an event whose cwd is outside the root must queue nothing"
    )
    assert not obs.acted, "cgs-event-cwd-outside: an event whose cwd is outside the root must start no sync"


# ---------------------------------------------------------------------------
# No-getcwd scan (FR-B3): project_root() is the only resolver
# ---------------------------------------------------------------------------

_CWD_PATTERN = re.compile(r"getcwd|\.cwd\b|\babspath\b|curdir|[\"']PWD[\"']")


def _scan_targets() -> list[Path]:
    targets = [p for p in sorted((PLUGIN_ROOT / "pyhooks").glob("*.py")) if p.name != "containment.py"]
    seed = PLUGIN_ROOT / "scripts" / "seed_project_key.py"  # Phase 4 turns "absent" into "scanned"
    if seed.exists():
        targets.append(seed)
    return targets


def test_no_pyhook_outside_containment_names_a_cwd_source() -> None:
    """Docstrings and comments count: a mention is one edit away from a call."""
    offenders = [
        f"{p.name}:{m.group(0)}"
        for p in _scan_targets()
        for m in _CWD_PATTERN.finditer(p.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"no-getcwd-scan: these files still mention a cwd source: {offenders}"


def test_the_cwd_scan_pattern_matches_each_spelling_it_exists_to_catch() -> None:
    """Guards the pattern against vacuity and typos, one spelling at a time."""
    for sample in ("os.getcwd()", "Path.cwd()", "os.path.abspath(x)", "``Path.cwd``",
                   "from os.path import abspath", "os.curdir", 'environ["PWD"]', "environ['PWD']"):
        assert _CWD_PATTERN.search(sample), f"no-getcwd-scan-pattern: the pattern misses {sample!r}"
    assert not _CWD_PATTERN.search("project_root()"), "no-getcwd-scan-pattern: the pattern flags innocent text"


def test_no_getcwd_scan_needles_are_live_in_the_exempt_file() -> None:
    """Guards the scan against vacuity: containment.py (the exemption) really contains a match."""
    text = (PLUGIN_ROOT / "pyhooks" / "containment.py").read_text(encoding="utf-8")
    assert _CWD_PATTERN.search(text), "no-getcwd-scan-vacuity: the pattern is not found where it must be"
    assert os.path.basename(str(_containment.__file__)) == "containment.py", (
        "no-getcwd-scan-vacuity: the exempt module is not the file the scan skips"
    )


def test_config_is_constructed_only_inside_config_py() -> None:
    """One constructor site means one place that decides how a root becomes a Config."""
    ctor = re.compile(r"\bConfig\(")
    offenders = [
        p.name
        for p in sorted((PLUGIN_ROOT / "pyhooks").glob("*.py"))
        if p.name != "config.py" and ctor.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"config-ctor-scan: Config( is built outside config.py in: {offenders}"
    assert ctor.search((PLUGIN_ROOT / "pyhooks" / "config.py").read_text(encoding="utf-8")), (
        "config-ctor-scan-vacuity: config.py no longer contains the constructor the scan looks for"
    )


@pytest.mark.parametrize("name", ["containment.py", "db.py"])
def test_leaf_modules_import_nothing_from_pyhooks(name: str) -> None:
    """Import direction is config -> containment/db only; a back-edge would make a cycle."""
    tree = ast.parse((PLUGIN_ROOT / "pyhooks" / name).read_text(encoding="utf-8"))
    back_edges = [
        ast.unparse(node)
        for node in ast.walk(tree)
        if (isinstance(node, ast.ImportFrom) and (node.level > 0 or (node.module or "").split(".")[0] == "pyhooks"))
        or (isinstance(node, ast.Import) and any(a.name.split(".")[0] == "pyhooks" for a in node.names))
    ]
    assert back_edges == [], f"import-direction/{name}: imports from pyhooks (a cycle risk): {back_edges}"
