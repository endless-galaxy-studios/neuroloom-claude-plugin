"""
D178d Phase 6: the API key never appears in any output (key-hygiene sweep).

A sentinel key is seeded into the project database, then EVERY registered hook
of both plugins is run through its real launcher, in both modes, with
``NEUROLOOM_DEBUG=true``, on four paths:

* ``success``        -- the API answers 2xx;
* ``reflecting_401`` -- the API answers 401 with a body that echoes the request
                        headers (so the key is sitting in the hook's memory);
* ``refused``        -- nothing listens on the API port;
* ``unopenable``     -- the key is readable but the state database cannot be opened.

Seven streams are searched for the key, any 10-character window of it, a prefix
or suffix, its lower/upper-case and whitespace-stripped forms, and the base64 of
the key and of ``Token <key>``:

* ``stdout`` / ``stderr``  -- the hook's output;
* ``files``                -- the BYTES of every file under the project root, the
                              scratch HOME and ``CLAUDE_PLUGIN_DATA`` (only the live
                              database's own files are skipped; the tables stream
                              reads them);
* ``tables``               -- every cell of every table except ``config``;
* ``config``               -- every ``config`` row whose key is not ``api_key``;
* ``tables_before``        -- the same tables as seeded BEFORE the run, which
                              includes bait payloads a normal-mode flush deletes;
* ``bodies``               -- every request body the recorder received (the key
                              travels in a header only).

The assertions compare PRECOMPUTED BOOLEANS and their messages name only the
hook, mode, path and stream: a failure never prints the data (plan,
"Failure-output hygiene").  Normal-mode cases also run with an egress shim on
PYTHONPATH that records and refuses any non-loopback network use; the
``no_external_egress`` boolean is asserted per case.  CI-mode cases run under
``python -I`` where PYTHONPATH is ignored, so the black hole and the
zero-requests assertions cover them.

Plain-language picture: this is a drug-sniffing dog.  A dog that has never found
anything proves nothing, so ``TestSweepCatchesPlantedLeaks`` hides a scrap in
every kind of bag first and checks the dog barks (and that it stays quiet for a
scrap too small to matter, so the threshold is a decision, not an accident).
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests import ci_support
from tests.ci_support import SlimRecorder

BASE_ROOT = ci_support.PLUGIN_ROOT
SDLC_ROOT = BASE_ROOT.parent / "neuroloom-sdlc-plugin"
KEY = ci_support.SENTINEL_KEY
# Needles: the whole key, a prefix, a suffix, EVERY 10-character window of it,
# and the base64 of the key and of the Authorization value.  Ten characters of a
# 40-character hex key is 40 bits: far too much to occur by accident, short
# enough that a truncated or wrapped echo still matches.  Nine characters is
# deliberately NOT caught (see the 'under_window' planted case): the threshold
# is a decision, not an accident.
WINDOW = 10


def _build_needles(key: str) -> tuple[str, ...]:
    forms = {key, key[:16], key[-12:]}
    forms |= {key[i : i + WINDOW] for i in range(len(key) - WINDOW + 1)}
    forms |= {base64.b64encode(raw.encode("utf-8")).decode("ascii") for raw in (key, f"Token {key}")}
    return tuple(sorted(forms))


NEEDLES = _build_needles(KEY)
_WS = re.compile(r"\s+")


def _contains_key(text: str) -> bool:
    """True when *text* holds the key in any form this sweep knows about.

    The stream is searched as-is, lower-cased, upper-cased and with all
    whitespace removed (a wrapped or re-cased echo still matches).
    """
    variants = (text, text.lower(), text.upper(), _WS.sub("", text))
    return any(needle in variant for variant in variants for needle in NEEDLES)


# ---------------------------------------------------------------------------
# Detection (the part the planted-leak self-test exercises)
# ---------------------------------------------------------------------------

_LIVE_DB_NAMES = (".neuroloom.db", ".neuroloom.db-wal", ".neuroloom.db-shm")


def _db_text(db_path: Path, *, config: bool) -> str:
    """Every cell of the selected tables, joined into one string.

    ``config=False``: every table except ``config``.  ``config=True``: only the
    ``config`` rows whose key is not ``api_key`` (the one row that is the key's
    legitimate home).
    """
    if not db_path.exists():
        return ""
    conn = sqlite3.connect(str(db_path))
    try:
        parts: list[str] = []

        def _add(cell: object) -> None:
            if isinstance(cell, bytes):
                parts.append(cell.decode("latin-1"))
            elif cell is not None:
                parts.append(str(cell))

        if config:
            for row in conn.execute("SELECT key, value FROM config WHERE key != 'api_key'"):
                for cell in row:
                    _add(cell)
        else:
            names = [
                str(r[0])
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                    "AND name != 'config'"
                )
            ]
            for name in names:
                for row in conn.execute(f'SELECT * FROM "{name}"'):
                    for cell in row:
                        _add(cell)
        return "\n".join(parts)
    finally:
        conn.close()


def _files_contain_key(project_root: Path, scratch: Path) -> bool:
    """Scan the BYTES of every file under the project root, scratch HOME and
    CLAUDE_PLUGIN_DATA.  Skipped: exactly the live database and its two
    sidecars at the project root (the ``tables``/``config`` streams read those
    through SQL, and the config.api_key row is their legitimate content)."""
    for base in (project_root, scratch / "home", scratch / "plugin-data"):
        if not base.is_dir():
            continue
        for path in ci_support._iter_files(base, skip_dirs=()):
            if base == project_root and path.parent == project_root and path.name in _LIVE_DB_NAMES:
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if _contains_key(data.decode("latin-1")):
                return True
    return False


def leak_report(
    run: ci_support.HookRun,
    project_root: Path,
    scratch: Path,
    *,
    tables_before: str = "",
    bodies: list[bytes] | None = None,
) -> dict[str, bool]:
    """Which streams contain the key (in any searched form).

    Returns precomputed booleans only, so asserting on the result can never
    print the data it was computed from.
    """
    db_path = project_root / ".neuroloom.db"
    return {
        "stdout": _contains_key(run.stdout),
        "stderr": _contains_key(run.stderr),
        "files": _files_contain_key(project_root, scratch),
        "config": _contains_key(_db_text(db_path, config=True)),
        "tables": _contains_key(_db_text(db_path, config=False)),
        "tables_before": _contains_key(tables_before),
        "bodies": _contains_key(b"\n".join(bodies or []).decode("latin-1")),
    }


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------

_BASE_EVENTS = {
    "pyhooks.session_start": "session",
    "pyhooks.capture": "read",
    "pyhooks.preload_context": "read",
    "pyhooks.code_graph_sync": "code_write",
    "pyhooks.cache_prefetch": "cwd_changed",
    "sdlc_pyhooks.session_start": "session",
    "sdlc_pyhooks.post_tool_use": "doc_write",
}

# Hooks that do nothing at all in CI mode (no database, no request).
_NOOP_IN_CI = {"pyhooks.code_graph_sync", "pyhooks.cache_prefetch", "sdlc_pyhooks.post_tool_use"}


def _all_hooks() -> list[tuple[str, str]]:
    hooks = [("base", m) for m in ci_support.registered_hook_modules(BASE_ROOT)]
    if (SDLC_ROOT / "hooks" / "hooks.json").is_file():
        hooks += [("sdlc", m) for m in ci_support.registered_hook_modules(SDLC_ROOT)]
    return hooks


HOOKS = _all_hooks()
MODES = ["normal", "ci"]
PATHS = ["success", "reflecting_401", "refused", "unopenable"]


def _event_json(kind: str, root: Path) -> str:
    if kind == "session":
        return json.dumps({"hook_event_name": "SessionStart", "cwd": str(root)})
    if kind == "read":
        return json.dumps(
            {"tool_name": "Read", "tool_input": {"file_path": str(root / "notes.txt")}, "cwd": str(root)}
        )
    if kind == "code_write":
        return json.dumps(
            {"tool_name": "Write", "tool_input": {"file_path": str(root / "app.py")}, "cwd": str(root)}
        )
    if kind == "cwd_changed":
        return json.dumps({"hook_event_name": "CwdChanged", "cwd": str(root / "sub")})
    if kind == "doc_write":
        return json.dumps(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": str(root / "docs" / "current_work" / "d1_x_spec.md")},
                "cwd": str(root),
            }
        )
    raise AssertionError(f"unknown event kind {kind}")


def _build_root(tmp_path: Path, label: str) -> ci_support.Bait:
    """A bait repository (keyed, stale session, buffered rows) plus files the events name."""
    bait = ci_support.make_bait_repo(tmp_path / label)
    root = bait.root
    (root / "sub").mkdir()
    (root / "notes.txt").write_text("plain notes\n", encoding="utf-8")
    (root / "app.py").write_text("def f() -> int:\n    return 1\n", encoding="utf-8")
    docs = root / "docs" / "current_work"
    docs.mkdir(parents=True)
    (docs / "d1_x_spec.md").write_text("# Spec\n\nSome deliverable text.\n", encoding="utf-8")
    return bait


def _make_unopenable(bait: ci_support.Bait) -> None:
    """Key stays readable; ``open_db`` (which needs write access) fails.

    The journal mode is switched off WAL first, so a reader needs no sidecar
    files, then the file is made read-only.
    """
    conn = sqlite3.connect(str(bait.db_path))
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.commit()
    finally:
        conn.close()
    for suffix in ("-wal", "-shm"):
        side = Path(str(bait.db_path) + suffix)
        if side.exists():
            side.unlink()
    bait.db_path.chmod(0o444)


def _closed_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


@dataclass
class SweepResult:
    run: ci_support.HookRun
    report: dict[str, bool]
    live: bool
    no_external_egress: bool


def run_sweep_case(
    tmp_path: Path,
    black_hole: ci_support.BlackHole,
    *,
    plugin: str,
    module: str,
    mode: str,
    path: str,
) -> SweepResult:
    ci = mode == "ci"
    bait = _build_root(tmp_path, "proj")
    root = bait.root
    recorder: SlimRecorder | None = None
    if path == "refused":
        api_base = f"http://127.0.0.1:{_closed_port()}"
    else:
        recorder = SlimRecorder(reflect_401=(path == "reflecting_401"))
        api_base = recorder.url
    if path == "unopenable":
        _make_unopenable(bait)

    # Hermetic network: a sitecustomize on PYTHONPATH records and refuses any
    # non-loopback use.  Under ``python -I`` (CI mode) it is not loaded, which
    # is why those cases are covered by the black hole instead; the log is then
    # simply empty, so the boolean below is uniform.
    shim_dir, egress_log = ci_support.write_egress_shim(tmp_path / "egress-shim")
    env_extra: dict[str, str] = {
        "CLAUDE_PROJECT_DIR": str(root),
        "NEUROLOOM_DEBUG": "true",
        "NEUROLOOM_API_BASE": api_base,
        # Offline codeweaver: no install thread, whatever the mode.  It does NOT
        # stop the codeweaver upgrade check (a GET to PyPI when codeweaver is
        # importable); the shim hides the codeweaver distribution for that.
        "NEUROLOOM_CODEWEAVER_OFFLINE": "1",
        ci_support.EGRESS_LOG_ENV: str(egress_log),
        ci_support.EGRESS_HIDE_CODEWEAVER_ENV: "1",
    }
    if ci:
        env_extra["NEUROLOOM_MODE"] = "ci"
    plugin_root = BASE_ROOT
    if plugin == "sdlc":
        plugin_root = SDLC_ROOT
        env_extra["PYTHONPATH"] = f"{SDLC_ROOT}{os.pathsep}{BASE_ROOT}"
        env_extra["NEUROLOOM_CLAUDE_PLUGIN_ROOT"] = str(BASE_ROOT)

    scratch = tmp_path / "scratch"
    tables_before = _db_text(bait.db_path, config=False)
    try:
        run = ci_support.run_hook(
            module,
            stdin=_event_json(_BASE_EVENTS[module], root),
            cwd=root,
            scratch=scratch,
            black_hole_url=black_hole.url,
            env_extra=env_extra,
            plugin_root=plugin_root,
            expected_root=BASE_ROOT,
            timeout=30.0,
            pythonpath_extra=(shim_dir,),
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"{module} ({mode}, {path}) did not exit within 30 s")
    finally:
        if path == "unopenable":
            bait.db_path.chmod(0o644)
        if recorder is not None:
            recorder.close()
    after = _db_text(bait.db_path, config=False)
    live = after != tables_before or (recorder is not None and any(c for _m, _p, c in recorder.requests))
    report = leak_report(
        run,
        root,
        scratch,
        tables_before=tables_before,
        bodies=recorder.bodies if recorder is not None else None,
    )
    return SweepResult(run, report, live, not ci_support.egress_attempts(egress_log))


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


def test_every_registered_hook_has_a_sweep_event() -> None:
    missing = [m for _p, m in HOOKS if m not in _BASE_EVENTS]
    assert not missing, f"hooks.json registers hooks the key sweep has no event for: {missing}"
    assert any(p == "base" for p, _ in HOOKS), "the sweep found no base hooks"


@pytest.mark.skipif(not (SDLC_ROOT / "hooks" / "hooks.json").is_file(), reason="sibling SDLC checkout absent")
def test_sweep_covers_both_plugins() -> None:
    assert {p for p, _ in HOOKS} == {"base", "sdlc"}, "the sweep must cover both plugins"
    assert len(HOOKS) >= 7, "the sweep lost a hook"


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(("plugin", "module"), HOOKS)
def test_key_never_appears_in_any_output(
    plugin: str,
    module: str,
    mode: str,
    path: str,
    tmp_path: Path,
    black_hole: ci_support.BlackHole,
) -> None:
    if path == "unopenable" and hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root bypasses file permissions; the unopenable fixture needs a non-root user")
    result = run_sweep_case(tmp_path, black_hole, plugin=plugin, module=module, mode=mode, path=path)
    leaked = sorted(stream for stream, hit in result.report.items() if hit)
    assert not leaked, f"key (in some form) found in {leaked} for {module} / {mode} / {path}"
    assert result.no_external_egress, f"{module} / {mode} / {path} attempted non-loopback network use"


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(("plugin", "module"), HOOKS)
def test_success_path_actually_exercised_the_key(
    plugin: str, module: str, mode: str, tmp_path: Path, black_hole: ci_support.BlackHole
) -> None:
    """The sweep is not vacuous: on the success path each hook either used the key
    (a request carrying it, or a local state change) or, for the hooks that are
    no-ops in CI mode, demonstrably did nothing."""
    result = run_sweep_case(tmp_path, black_hole, plugin=plugin, module=module, mode=mode, path="success")
    if mode == "ci" and module in _NOOP_IN_CI:
        assert not result.live, f"{module} ci: expected a pure no-op, but state or traffic changed"
    else:
        assert result.live, f"{module} {mode}: the hook never used the key, so the sweep proved nothing"


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="needs a non-root user to make a file unwritable"
)
def test_unopenable_fixture_really_is_unopenable_with_a_readable_key(tmp_path: Path) -> None:
    """Precondition of the 'unopenable' path: the key reads back, open_db fails,
    and CI session_start says so (not the 'dormant' line, which would mean the
    key was unreadable and the path under test was never reached)."""
    import pyhooks.db as db_mod
    import pyhooks.session_start as ss

    bait = _build_root(tmp_path, "proj")
    _make_unopenable(bait)
    try:
        conn = sqlite3.connect(str(bait.db_path))
        try:
            readable = conn.execute("SELECT value FROM config WHERE key='api_key'").fetchone() is not None
        finally:
            conn.close()
        opened = db_mod.open_db(bait.db_path)
        if opened is not None:
            opened.close()
        hole = ci_support.BlackHole()
        try:
            run = ci_support.run_hook(
                "pyhooks.session_start",
                cwd=bait.root,
                scratch=tmp_path / "scratch",
                black_hole_url=hole.url,
                env_extra={"CLAUDE_PROJECT_DIR": str(bait.root), "NEUROLOOM_MODE": "ci"},
            )
        finally:
            hole.close()
    finally:
        bait.db_path.chmod(0o644)
    reported_unopenable = run.stdout.strip() == ss._CI_UNOPENABLE_LINE
    assert readable, "unopenable fixture: the key is not readable"
    assert opened is None, "unopenable fixture: open_db succeeded"
    assert reported_unopenable, "unopenable fixture: CI session_start did not report 'unopenable'"


# ---------------------------------------------------------------------------
# Positive control: the sweep must CATCH a planted leak
# ---------------------------------------------------------------------------

_LEAKY_HOOK = '''\
import base64, os, sqlite3, sys
from pathlib import Path

root = Path(os.environ["CLAUDE_PROJECT_DIR"])
conn = sqlite3.connect(str(root / ".neuroloom.db"))
key = conn.execute("SELECT value FROM config WHERE key = 'api_key'").fetchone()[0]
where = os.environ["LEAK_WHERE"]
fragment = {
    "full": key,
    "prefix": key[:20],
    "suffix": key[-14:],
    "window": key[11:21],
    "under_window": key[11:20],
    "upper": key.upper(),
    "b64": base64.b64encode(key.encode()).decode(),
    "b64_token": base64.b64encode(("Token " + key).encode()).decode(),
    "split_ws": key[:20] + "\\n  " + key[20:],
    "none": "clean",
}[os.environ["LEAK_FRAGMENT"]]
inserts = {
    "event_buffer": ("INSERT INTO event_buffer (payload, created_at, payload_type) VALUES (?, 1.0, 'observation')", (fragment,)),
    "cache": ("INSERT INTO cache (cache_key, context, created_at) VALUES ('k', ?, 1.0)", (fragment,)),
    "sessions": ("INSERT INTO sessions (session_key, session_id, started_at) VALUES ('leak', ?, 't')", (fragment,)),
    "traces": ("INSERT INTO traces (ts, script, decision, detail) VALUES ('t', 'leaky', 'leak', ?)", (fragment,)),
    "token_budget": ("INSERT INTO token_budget (session_id, total_chars) VALUES (?, 1)", (fragment,)),
    "config": ("INSERT INTO config (key, value) VALUES ('leaky', ?)", (fragment,)),
}
if where == "stdout":
    print("Authorization: Token " + fragment if fragment != "clean" else fragment)
elif where == "stderr":
    print("debug: " + fragment, file=sys.stderr)
elif where == "file_root":
    (root / "leak.txt").write_text("x " + fragment, encoding="utf-8")
elif where == "file_home":
    (Path(os.environ["HOME"]) / "leak.txt").write_text("x " + fragment, encoding="utf-8")
elif where == "file_data":
    (Path(os.environ["CLAUDE_PLUGIN_DATA"]) / "leak.txt").write_text("x " + fragment, encoding="utf-8")
else:
    sql, args = inserts[where]
    conn.execute(sql, args)
    conn.commit()
conn.close()
'''

_STREAM_OF = {
    "stdout": "stdout",
    "stderr": "stderr",
    "file_root": "files",
    "file_home": "files",
    "file_data": "files",
    "config": "config",
    "event_buffer": "tables",
    "cache": "tables",
    "sessions": "tables",
    "traces": "tables",
    "token_budget": "tables",
}
_STREAMS = ("stdout", "stderr", "files", "config", "tables", "tables_before", "bodies")


def _stub_plugin(tmp_path: Path) -> Path:
    """A throwaway plugin: the real base launcher plus a hook that plants the key."""
    stub = tmp_path / "stub-plugin"
    (stub / "pyhooks").mkdir(parents=True)
    (stub / "pyhooks" / "__init__.py").write_text("", encoding="utf-8")
    (stub / "leakyhooks").mkdir()
    (stub / "leakyhooks" / "__init__.py").write_text("", encoding="utf-8")
    (stub / "leakyhooks" / "bad.py").write_text(_LEAKY_HOOK, encoding="utf-8")
    shutil.copy(BASE_ROOT / "run_hook.py", stub / "run_hook.py")
    return stub


def _run_leaky(
    tmp_path: Path, black_hole: ci_support.BlackHole, *, where: str, fragment: str, mode: str
) -> dict[str, bool]:
    stub = _stub_plugin(tmp_path)
    bait = _build_root(tmp_path, "proj")
    scratch = tmp_path / "scratch"
    extra = {
        "CLAUDE_PROJECT_DIR": str(bait.root),
        "LEAK_WHERE": where,
        "LEAK_FRAGMENT": fragment,
        "NEUROLOOM_DEBUG": "true",
    }
    if mode == "ci":
        extra["NEUROLOOM_MODE"] = "ci"
    tables_before = _db_text(bait.db_path, config=False)
    run = ci_support.run_hook(
        "leakyhooks.bad",
        cwd=bait.root,
        scratch=scratch,
        black_hole_url=black_hole.url,
        env_extra=extra,
        plugin_root=stub,
    )
    return leak_report(run, bait.root, scratch, tables_before=tables_before)


def _expected(stream: str | None) -> dict[str, bool]:
    return {name: name == stream for name in _STREAMS}


class TestSweepCatchesPlantedLeaks:
    @pytest.mark.parametrize("mode", MODES)
    @pytest.mark.parametrize("fragment", ["full", "prefix", "suffix"])
    @pytest.mark.parametrize("where", sorted(_STREAM_OF))
    def test_planted_leak_is_reported_in_the_right_stream(
        self, where: str, fragment: str, mode: str, tmp_path: Path, black_hole: ci_support.BlackHole
    ) -> None:
        report = _run_leaky(tmp_path, black_hole, where=where, fragment=fragment, mode=mode)
        assert report == _expected(_STREAM_OF[where]), (
            f"planted {fragment} leak in {where} ({mode}): sweep reported {report}"
        )

    @pytest.mark.parametrize("mode", MODES)
    @pytest.mark.parametrize("fragment", ["window", "upper", "b64", "b64_token", "split_ws"])
    def test_planted_encoding_is_reported(
        self, fragment: str, mode: str, tmp_path: Path, black_hole: ci_support.BlackHole
    ) -> None:
        """Each encoding the sweep claims to handle is caught on its own."""
        report = _run_leaky(tmp_path, black_hole, where="stdout", fragment=fragment, mode=mode)
        assert report == _expected("stdout"), f"planted {fragment} leak ({mode}): sweep reported {report}"

    @pytest.mark.parametrize("mode", MODES)
    @pytest.mark.parametrize("where", ["stdout", "file_root", "traces"])
    def test_fragment_just_under_the_window_is_not_caught(
        self, where: str, mode: str, tmp_path: Path, black_hole: ci_support.BlackHole
    ) -> None:
        """Boundary: a nine-character slice is below the threshold, on purpose."""
        report = _run_leaky(tmp_path, black_hole, where=where, fragment="under_window", mode=mode)
        assert report == _expected(None), f"a {WINDOW - 1}-char fragment was flagged ({where}, {mode}): {report}"

    @pytest.mark.parametrize("mode", MODES)
    def test_clean_stub_is_not_flagged(
        self, mode: str, tmp_path: Path, black_hole: ci_support.BlackHole
    ) -> None:
        """Negative control for the self-test: the dog does not bark at an empty bag."""
        stub = _stub_plugin(tmp_path)
        bait = _build_root(tmp_path, "proj")
        scratch = tmp_path / "scratch"
        extra = {"CLAUDE_PROJECT_DIR": str(bait.root), "LEAK_WHERE": "stdout", "LEAK_FRAGMENT": "none"}
        if mode == "ci":
            extra["NEUROLOOM_MODE"] = "ci"
        tables_before = _db_text(bait.db_path, config=False)
        run = ci_support.run_hook(
            "leakyhooks.bad",
            cwd=bait.root,
            scratch=scratch,
            black_hole_url=black_hole.url,
            env_extra=extra,
            plugin_root=stub,
        )
        ran = bool(run.stdout.strip())
        report = leak_report(run, bait.root, scratch, tables_before=tables_before)
        assert ran, "stub did not run: the clean control proved nothing"
        assert not any(report.values()), "clean stub was flagged as leaking"

    def test_request_bodies_and_pre_run_tables_are_scanned(self, tmp_path: Path) -> None:
        """The two streams the stub hook cannot plant into: bodies and the seeded tables."""
        bait = _build_root(tmp_path, "proj")
        empty = ci_support.HookRun("", "", 0, [])
        clean = leak_report(empty, bait.root, tmp_path / "scratch", tables_before="seeded text", bodies=[b"{}"])
        assert not any(clean.values())
        dirty = leak_report(
            empty, bait.root, tmp_path / "scratch",
            tables_before="payload " + KEY[3:20], bodies=[b'{"file_path": "x"}', ("t " + KEY).encode()],
        )
        assert dirty == _expected_two(tables_before=True, bodies=True)


def _expected_two(**hits: bool) -> dict[str, bool]:
    return {name: hits.get(name, False) for name in _STREAMS}
