"""
Tests for project-root path containment (D178e item 13).

Covers ``pyhooks.containment`` directly, and the hooks that forward a path,
search target, or tool event to the Neuroloom API:

- ``pyhooks.preload_context`` -- Read, Glob, Grep, and Bash bfs / ugrep.
- ``pyhooks.cache_prefetch`` -- the CwdChanged target directory.
- ``pyhooks.capture`` -- PostToolUse events (Read, Write, Edit, NotebookEdit,
  Glob, Grep dropped when the target is outside; Bash left alone).
- ``pyhooks.code_graph_sync._within_workspace`` -- delegates to the shared rule.

The model these tests encode (verified against the Claude Code hooks docs):

- ``CLAUDE_PROJECT_DIR`` is the project root and does NOT move on ``cd``.
- The hook PROCESS's cwd and the event's ``cwd`` field both FOLLOW ``cd``.
  So the fixtures set ``CLAUDE_PROJECT_DIR`` to the root and, to simulate a
  ``cd``, move the process cwd and the event ``cwd`` together.

A "skipped" test asserts a quiet no-op: ``{}`` out, no API call, no cache write
-- and the ``outside_project`` trace, so a test cannot pass merely because the
hook bailed out earlier for an unrelated reason.  Every skip test has an
inside-root control proving the same harness DOES send when it should.

Layout used by the fixtures (all under pytest's ``tmp_path``)::

    tmp_path/
        proj/            <- project root (CLAUDE_PROJECT_DIR); holds .neuroloom.db
            src/auth_handler.py
            link -> ../other   (symlink pointing out of the root)
            inner_link -> src  (symlink staying inside the root)
        other/           <- a sibling directory, outside the root
            secret.py

The existing ``_run_preload`` helper in test_preload_context.py is deliberately
not reused: it builds ``Config(..., debug=False)``, which raises TypeError (the
cause of that file's baseline failures in known_failures.txt), and its fake
``_fetch_context`` lacks the ``session_id`` parameter the hook passes, so a
skip test built on it would pass vacuously.
"""

import glob
import io
import itertools
import json
import os
import sqlite3
import threading
import types
from pathlib import Path
from typing import Any, NamedTuple
from unittest.mock import MagicMock

import pytest

import pyhooks.cache_prefetch as _prefetch_mod
import pyhooks.capture as _capture_mod
import pyhooks.code_graph_sync as _cgs_mod
import pyhooks.config as _config_mod
import pyhooks.containment as _containment
import pyhooks.db as _db_mod
import pyhooks.preload_context as _pc_mod
from pyhooks.containment import glob_within_root, within_root

SESSION_ID = "sess-1700000000-abcdef12"

# A backslash followed by a newline: a shell line continuation.  Built from chr() so no
# escape sequence in this file can be misread.
BS_NL = chr(92) + chr(10)
BS = chr(92)


class Project(NamedTuple):
    root: Path
    other: Path
    db_path: Path


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Project:
    """Create the proj/ and other/ layout; root is CLAUDE_PROJECT_DIR and the cwd."""
    root = (tmp_path / "proj").resolve()
    other = (tmp_path / "other").resolve()
    (root / "src").mkdir(parents=True)
    other.mkdir()
    (root / "src" / "auth_handler.py").write_text("x = 1\n")
    (other / "secret.py").write_text("y = 2\n")
    (root / "link").symlink_to(other, target_is_directory=True)
    (root / "inner_link").symlink_to(root / "src", target_is_directory=True)
    # A wildcard can match an in-project symlink that points out (round 3):
    (root / "src" / "escape").symlink_to(other, target_is_directory=True)
    (other / "x").write_text("z = 3\n")
    (other / "secret.txt").write_text("w = 4\n")
    # ...and a benign wildcard target that stays inside.
    (root / "src" / "pkg").mkdir()
    (root / "src" / "pkg" / "mod.py").write_text("m = 5\n")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    monkeypatch.chdir(root)
    return Project(root=root, other=other, db_path=root / ".neuroloom.db")


def _config(project: Project, db_at: Path | None = None) -> _config_mod.Config:
    """A Config whose key "came from" ``db_at`` (default: the project root's db).

    ``config.load()`` reads the key from ``<process cwd>/.neuroloom.db``, and the
    process cwd follows ``cd``.  Pass ``db_at=<sibling>/.neuroloom.db`` to model a
    key that belongs to a DIFFERENT directory than CLAUDE_PROJECT_DIR.
    """
    return _config_mod.Config(
        api_key="test-key",
        api_base="http://localhost:19999",
        state_db_path=db_at or project.db_path,
    )


def _query(project: Project, sql: str, db_at: Path | None = None) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(db_at or project.db_path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _seed_session(
    project: Project, session_id: str = SESSION_ID, db_at: Path | None = None
) -> None:
    """Insert a sessions row keyed by the hook process's (resolved) cwd."""
    conn = _db_mod.open_db(db_at or project.db_path)
    assert conn is not None
    conn.execute(
        "INSERT INTO sessions (session_key, session_id, started_at, last_submit_ms) "
        "VALUES (?, ?, datetime('now'), 0)",
        (str(Path(os.getcwd()).resolve()), session_id),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# project_root / session_cwd
# ---------------------------------------------------------------------------


class TestProjectRootAndSessionCwd:
    def test_root_is_claude_project_dir_even_after_cd(
        self, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(project.other)  # the process cwd follows `cd`; the root must not
        assert _containment.project_root() == project.root

    def test_root_falls_back_to_process_cwd_when_unset(
        self, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CLAUDE_PROJECT_DIR")
        monkeypatch.chdir(project.root / "src")
        assert _containment.project_root() == project.root / "src"

    def test_relative_project_dir_is_ignored(
        self, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "relative/dir")
        assert _containment.project_root() == project.root  # fell back to the cwd

    def test_project_dir_spelled_through_a_symlink_is_resolved(
        self, project: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        alias = tmp_path / "alias"
        alias.symlink_to(project.root, target_is_directory=True)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(alias))
        assert _containment.project_root() == project.root
        assert within_root(str(project.root / "src" / "auth_handler.py"), _containment.project_root())

    def test_project_root_contract_for_downstream_users(
        self, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`project_root()` is the single root resolver other work builds on (D178d
        pins the db path, session keys, cache keys and debounce keys to it).  Its
        name, module, signature and rule are a contract: CLAUDE_PROJECT_DIR when set
        and absolute, otherwise the cwd -- and nothing else, in particular no
        CI-specific behaviour."""
        import inspect

        from pyhooks.containment import project_root

        assert project_root is _containment.project_root
        assert list(inspect.signature(project_root).parameters) == []
        assert project_root() == project.root  # env set and absolute

        # No environment variable other than CLAUDE_PROJECT_DIR may change the answer.
        for name in ("CI", "GITHUB_ACTIONS", "CLAUDE_CODE_REMOTE", "NEUROLOOM_CI"):
            monkeypatch.setenv(name, "true")
        assert project_root() == project.root
        monkeypatch.delenv("CLAUDE_PROJECT_DIR")
        monkeypatch.chdir(project.other)
        assert project_root() == project.other  # unset -> the cwd, CI variables or not

    def test_session_cwd_is_the_event_cwd(self, project: Project) -> None:
        assert _containment.session_cwd({"cwd": str(project.other)}) == project.other

    @pytest.mark.parametrize("event", [{}, {"cwd": ""}, {"cwd": 5}, {"cwd": None}])
    def test_session_cwd_falls_back_to_process_cwd(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, event: dict[str, Any]
    ) -> None:
        monkeypatch.chdir(project.other)
        assert _containment.session_cwd(event) == project.other


# ---------------------------------------------------------------------------
# within_root (literal paths)
# ---------------------------------------------------------------------------


class TestWithinRoot:
    def test_file_inside_root(self, project: Project) -> None:
        assert within_root(str(project.root / "src" / "auth_handler.py"), project.root)

    def test_root_itself_is_inside(self, project: Project) -> None:
        assert within_root(str(project.root), project.root)

    def test_nonexistent_path_inside_root(self, project: Project) -> None:
        assert within_root(str(project.root / "not" / "there.py"), project.root)

    def test_sibling_outside_root(self, project: Project) -> None:
        assert not within_root(str(project.other / "secret.py"), project.root)

    def test_absolute_path_elsewhere(self, project: Project) -> None:
        assert not within_root("/etc/hosts", project.root)

    def test_parent_traversal_outside(self, project: Project) -> None:
        assert not within_root(str(project.root / ".." / "other" / "secret.py"), project.root)

    def test_traversal_that_returns_inside(self, project: Project) -> None:
        assert within_root(
            str(project.root / "src" / ".." / "src" / "auth_handler.py"), project.root
        )

    def test_symlink_pointing_out_is_outside(self, project: Project) -> None:
        assert not within_root(str(project.root / "link" / "secret.py"), project.root)

    def test_symlink_staying_inside_is_inside(self, project: Project) -> None:
        assert within_root(str(project.root / "inner_link" / "auth_handler.py"), project.root)

    def test_prefix_sibling_is_not_inside(self, project: Project) -> None:
        """/…/proj-evil must not pass as inside /…/proj (str.startswith trap)."""
        evil = project.root.parent / "proj-evil"
        evil.mkdir()
        assert not within_root(str(evil / "x.py"), project.root)

    def test_relative_path_uses_base(self, project: Project) -> None:
        assert within_root("auth_handler.py", project.root, base=project.root / "src")
        assert not within_root("../../other/secret.py", project.root, base=project.root / "src")

    def test_home_is_expanded(self, project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(project.other))
        assert not within_root("~/secret.py", project.root)
        monkeypatch.setenv("HOME", str(project.root / "src"))
        assert within_root("~/auth_handler.py", project.root)

    @pytest.mark.parametrize("raw", ["~nosuchuser/x", "~+/..", "~-", "~+", "~nosuchuser"])
    def test_tilde_that_cannot_be_expanded_is_outside(self, project: Project, raw: str) -> None:
        """`~+` / `~-` are shell dir-stack forms, `~name` an unknown user; none can
        be resolved here, so none may pass as a plain relative path."""
        assert not within_root(raw, project.root)
        assert not within_root(raw, project.root, base=project.root)

    def test_unresolvable_path_fails_closed(self, project: Project) -> None:
        assert not within_root("bad\x00path", project.root)

    def test_case_mismatched_spelling_over_skips_on_case_insensitive_fs(
        self, tmp_path: Path
    ) -> None:
        """Documented: resolve() keeps the case it is given, so on a case-insensitive
        filesystem (macOS default) a mis-cased spelling of an inside path is judged
        outside.  Fails closed.  Skipped on case-sensitive filesystems."""
        root = (tmp_path / "CaseRoot").resolve()
        root.mkdir()
        (root / "a.py").write_text("x")
        alias = tmp_path / "caseroot" / "a.py"
        if not alias.exists():
            pytest.skip("case-sensitive filesystem")
        assert not within_root(str(alias), root)


# ---------------------------------------------------------------------------
# glob_within_root (a Glob tool pattern: braces enumerated, [..] a wildcard)
# ---------------------------------------------------------------------------


class TestGlobWithinRoot:
    @pytest.mark.parametrize(
        "raw",
        [
            '..$X',  # `.."$X"` after quote removal: X could be anything, e.g. "/../x"
            "{..,.}",  # brace expansion yields `..`
            "{..,src}/x",
            "$HOME/.ssh",
            "`echo /etc`",
            "x$(echo /etc)",
            "[.][.]/x",  # bracket class
            ".*/x",  # Bash < 5.2 expands `.*` to include `..`
            ".?/x",
            "./.*",
            "..*",
            "*/..",  # wildcard may match a symlink out; `..` then climbs from there
            "*/../x",
            "src/*/../../x",
            "**/../x",  # `**` is never expanded against the filesystem: only the rule catches this
            "src/**/../../x",
            "~nosuchuser/x",
            "~+/..",
            "~-",
        ],
    )
    def test_unprovable_construct_is_outside(self, project: Project, raw: str) -> None:
        assert not glob_within_root(raw, project.root, base=project.root)

    @pytest.mark.parametrize(
        "raw",
        ["src/*.py", "**/auth_*.py", "src/auth_handler.py", "*.py", "./src", "src/.hidden", "src"],
    )
    def test_plain_pattern_inside_is_inside(self, project: Project, raw: str) -> None:
        assert glob_within_root(raw, project.root, base=project.root)

    @pytest.mark.parametrize("raw", ["/etc/**", "../other/*.py", f"{'/etc'}/hosts"])
    def test_plain_pattern_outside_is_outside(self, project: Project, raw: str) -> None:
        assert not glob_within_root(raw, project.root, base=project.root)


# ---------------------------------------------------------------------------
# preload_context helpers
# ---------------------------------------------------------------------------

_UNSET: Any = object()


def _run_preload(
    project: Project,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    event: dict[str, Any],
    *,
    cwd: Path | None = None,
    process_cwd: Path | None = None,
    send_cwd: bool = True,
    db_at: Path | None = None,
) -> tuple[dict[str, Any], MagicMock]:
    """Run preload_context.main() against a real state DB with a mocked API call.

    ``cwd`` is where Claude is working (defaults to the project root).  It is
    written to the event's ``cwd`` and, because the hook process follows ``cd``,
    also becomes the process cwd -- unless ``process_cwd`` overrides it.

    Returns (parsed stdout, the _fetch_context mock).
    """
    cwd = cwd or project.root
    monkeypatch.chdir(process_cwd or cwd)
    if send_cwd:
        event = {**event, "cwd": str(cwd)}
    fetch = MagicMock(return_value=("Relevant context", 300))
    cfg = _config(project, db_at)
    monkeypatch.setattr(_pc_mod._config_mod, "load", lambda: cfg)
    monkeypatch.setattr(_pc_mod, "_fetch_context", fetch)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    _pc_mod.main()
    return json.loads(capsys.readouterr().out), fetch


def _assert_skipped(
    project: Project,
    out: dict[str, Any],
    fetch: MagicMock,
    *,
    db_at: Path | None = None,
    reason: str = "outside_project",
) -> None:
    """A skip is a quiet no-op: `{}` out, no API call, no cache write."""
    assert out == {}
    fetch.assert_not_called()
    assert _query(project, "SELECT COUNT(*) FROM cache", db_at) == [(0,)]
    assert (reason,) in _query(
        project, "SELECT decision FROM traces WHERE script = 'preload_context'", db_at
    )


def _read(path: str) -> dict[str, Any]:
    return {"tool_name": "Read", "tool_input": {"file_path": path}}


def _glob(pattern: str, path: str | None = None) -> dict[str, Any]:
    tool_input: dict[str, Any] = {"pattern": pattern}
    if path is not None:
        tool_input["path"] = path
    return {"tool_name": "Glob", "tool_input": tool_input}


def _grep(pattern: str, path: str | None = None) -> dict[str, Any]:
    tool_input: dict[str, Any] = {"pattern": pattern}
    if path is not None:
        tool_input["path"] = path
    return {"tool_name": "Grep", "tool_input": tool_input}


def _bash(command: str) -> dict[str, Any]:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


# ---------------------------------------------------------------------------
# preload_context -- Read
# ---------------------------------------------------------------------------


class TestPreloadRead:
    def test_inside_root_sends(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = str(project.root / "src" / "auth_handler.py")
        out, fetch = _run_preload(project, monkeypatch, capsys, _read(target))

        fetch.assert_called_once()
        # The API gets the path RELATIVE to the project root (it matches symbol paths
        # stored that way); the absolute `target` never leaves the machine.
        assert fetch.call_args.args[2] == "src/auth_handler.py"
        assert out == {"additionalContext": "Relevant context"}
        assert _query(project, "SELECT COUNT(*) FROM cache") == [(1,)]

    def test_inside_root_via_inside_symlink_sends(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out, fetch = _run_preload(
            project, monkeypatch, capsys, _read(str(project.root / "inner_link" / "auth_handler.py"))
        )
        fetch.assert_called_once()
        assert "additionalContext" in out

    @pytest.mark.parametrize(
        "target",
        [
            "other_secret",  # replaced below with the sibling's real path
            "/etc/hosts",
            "traversal",
            "symlink_out",
        ],
    )
    def test_outside_root_skipped(
        self,
        project: Project,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        target: str,
    ) -> None:
        paths = {
            "other_secret": str(project.other / "secret.py"),
            "/etc/hosts": "/etc/hosts",
            "traversal": f"{project.root}/src/../../other/secret.py",
            "symlink_out": str(project.root / "link" / "secret.py"),
        }
        out, fetch = _run_preload(project, monkeypatch, capsys, _read(paths[target]))
        _assert_skipped(project, out, fetch)


# ---------------------------------------------------------------------------
# preload_context -- Glob / Grep
# ---------------------------------------------------------------------------


class TestPreloadGlobGrep:
    def test_glob_inside_root_sends(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out, fetch = _run_preload(project, monkeypatch, capsys, _glob("src/**/auth_handler.py"))
        fetch.assert_called_once()
        assert fetch.call_args.kwargs["query"] == "auth_handler"
        assert fetch.call_args.kwargs["fmt"] == "nudge"
        assert "additionalContext" in out

    def test_glob_inside_path_sends(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, fetch = _run_preload(
            project, monkeypatch, capsys, _glob("auth_handler.py", str(project.root / "src"))
        )
        fetch.assert_called_once()

    def test_glob_path_outside_skipped(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out, fetch = _run_preload(
            project, monkeypatch, capsys, _glob("secret.py", str(project.other))
        )
        _assert_skipped(project, out, fetch)

    def test_glob_absolute_pattern_outside_skipped(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out, fetch = _run_preload(
            project, monkeypatch, capsys, _glob(f"{project.other}/**/secret*.py")
        )
        _assert_skipped(project, out, fetch)

    def test_glob_relative_traversal_pattern_skipped(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out, fetch = _run_preload(project, monkeypatch, capsys, _glob("../other/secret.py"))
        _assert_skipped(project, out, fetch)

    def test_glob_traversal_relative_to_path_skipped(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`path` is inside, but the pattern climbs out of the root from it."""
        out, fetch = _run_preload(
            project, monkeypatch, capsys, _glob("../../other/secret.py", str(project.root / "src"))
        )
        _assert_skipped(project, out, fetch)

    def test_glob_through_outbound_symlink_skipped(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out, fetch = _run_preload(project, monkeypatch, capsys, _glob("link/secret.py"))
        _assert_skipped(project, out, fetch)

    @pytest.mark.parametrize(
        "pattern",
        [
            "{..,src}/auth_handler.py",  # brace expansion yields `../auth_handler.py`
            "[.][.]/auth_handler.py",
            ".*/auth_handler.py",
            ".?/auth_handler.py",
            "*/../auth_handler.py",
            "$HOME/auth_handler.py",
            "~nosuchuser/auth_handler.py",
        ],
    )
    def test_glob_unprovable_pattern_skipped(
        self,
        project: Project,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        pattern: str,
    ) -> None:
        out, fetch = _run_preload(project, monkeypatch, capsys, _glob(pattern))
        _assert_skipped(project, out, fetch)

    def test_grep_inside_root_sends(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, fetch = _run_preload(
            project, monkeypatch, capsys, _grep("def auth_handler", str(project.root / "src"))
        )
        fetch.assert_called_once()
        assert fetch.call_args.kwargs["fmt"] == "nudge"

    def test_grep_without_path_in_project_sends(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, fetch = _run_preload(project, monkeypatch, capsys, _grep("def auth_handler"))
        fetch.assert_called_once()

    def test_grep_path_outside_skipped(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out, fetch = _run_preload(
            project, monkeypatch, capsys, _grep("def auth_handler", str(project.other))
        )
        _assert_skipped(project, out, fetch)

    def test_grep_path_through_outbound_symlink_skipped(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out, fetch = _run_preload(
            project, monkeypatch, capsys, _grep("def auth_handler", str(project.root / "link"))
        )
        _assert_skipped(project, out, fetch)

    def test_grep_regex_that_looks_like_a_path_is_not_a_location(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A Grep pattern is a content regex, not a place: searching the project
        for the text `/etc/passwd_handler` is still a project search."""
        _, fetch = _run_preload(
            project, monkeypatch, capsys, _grep("/etc/passwd_handler", str(project.root / "src"))
        )
        fetch.assert_called_once()


# ---------------------------------------------------------------------------
# preload_context -- Bash bfs / ugrep
# ---------------------------------------------------------------------------

# Each of these passed the round-1 check as "inside" but is not.  They all
# carry a real pattern (`auth_handler`) so that, were the containment check
# absent, the call WOULD reach the API -- a skip cannot be an accident of the
# earlier "no extractable query" guard.
BASH_BYPASSES = [
    'bfs .."$X" -name auth_handler.py',  # variable glued to `..`
    "bfs {..,.} -name auth_handler.py",  # brace expansion yields `..`
    "bfs ~+/.. -name auth_handler.py",  # $PWD/..
    "bfs ~- -name auth_handler.py",  # $OLDPWD
    "bfs ~nosuchuser/x -name auth_handler.py",
    'bfs . -name auth_handler.py "x$(echo /etc)"',  # command substitution
    "ugrep -r auth_handler `echo /etc`",  # backtick substitution
    "ugrep -r auth_handler -f/etc/passwd",  # flag with attached path value
    "ugrep -r auth_handler --include=/etc/x",  # --long=path value
    "ugrep -r auth_handler -rf/etc/passwd",  # value hidden in a short-flag cluster
    "ugrep -r auth_handler src >/etc/x",  # redirection glued to a path
    "ugrep -r auth_handler src;bfs /etc",  # `;` glued to the next command's path
    "bfs [.][.] -name auth_handler.py",  # bracket class
    "bfs ./.* -name auth_handler.py",  # Bash < 5.2: `.*` includes `..`
    "bfs .? -name auth_handler.py",
    "bfs */.. -name auth_handler.py",  # wildcard may be a symlink out
    "bfs . -name auth_handler.py ../other",  # plain traversal
    "ugrep -r auth_handler $HOME/.ssh",
    "bfs . -name 'auth_handler.py",  # unbalanced quote: unparseable
    # Round 4: Bash deletes backslash-newline BEFORE it looks at the word.
    "bfs ." + BS_NL + "./x -name auth_handler.py",  # `.` `.` -> `..`
    'bfs ".' + BS_NL + '." -name auth_handler.py',  # ...also inside double quotes
    "ugrep -r auth_handler ." + BS_NL + ".",
    "ugrep auth_handler " + BS_NL + "/etc",
    "ugrep auth_handler .." + BS_NL + "/..",
    "ugrep -r auth_handler **/secret.txt",  # Bash: `**` is `*`; `link` is a symlink out
    "bfs =ls -name auth_handler.py",  # zsh expands `=ls` to /bin/ls
    "ugrep -r auth_handler =ls",
    # Round 5: quoted/escaped glob characters beside a live wildcard; zsh recursion on `**/`.
    "ugrep auth_handler 'a[b]'*",
    'ugrep auth_handler "a[b]"*',
    "ugrep auth_handler a" + BS + "[b" + BS + "]*",
    "ugrep -r auth_handler src/**/etclink",
    "bfs src/**/etclink -name auth_handler.py",
]


class TestPreloadBash:
    @pytest.mark.parametrize(
        ("command", "expected_query"),
        [
            # bfs patterns go through the Glob extractor (keeps the stem)...
            ("bfs . -name 'auth_handler.py'", "auth_handler"),
            ("bfs src -type f -name auth_handler.py", "auth_handler"),
            # ...ugrep patterns through the Grep extractor (splits on `_`).
            ("ugrep -r 'auth_handler' src", "auth handler"),
            ("ugrep -r auth_handler .", "auth handler"),
            ("ugrep -rn auth_handler src --include=*.py", "auth handler"),
            ("ugrep -r auth_handler -m5 src", "auth handler"),
        ],
    )
    def test_inside_root_sends(
        self,
        project: Project,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        command: str,
        expected_query: str,
    ) -> None:
        _, fetch = _run_preload(project, monkeypatch, capsys, _bash(command))
        fetch.assert_called_once()
        assert fetch.call_args.kwargs["query"] == expected_query

    def test_bfs_outside_path_skipped(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out, fetch = _run_preload(
            project, monkeypatch, capsys, _bash(f"bfs {project.other} -name auth_handler.py")
        )
        _assert_skipped(project, out, fetch)

    def test_ugrep_outside_path_skipped(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out, fetch = _run_preload(
            project, monkeypatch, capsys, _bash(f"ugrep -r auth_handler {project.other}")
        )
        _assert_skipped(project, out, fetch)

    def test_ugrep_through_outbound_symlink_skipped(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out, fetch = _run_preload(project, monkeypatch, capsys, _bash("ugrep -r auth_handler link"))
        _assert_skipped(project, out, fetch)

    @pytest.mark.parametrize("command", BASH_BYPASSES)
    def test_expansion_and_parsing_bypasses_skipped(
        self,
        project: Project,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        command: str,
    ) -> None:
        out, fetch = _run_preload(project, monkeypatch, capsys, _bash(command))
        _assert_skipped(project, out, fetch)

    @pytest.mark.parametrize(
        "command",
        [
            "ugrep -r 'auth_[a-z]+' src",  # regex character class
            "ugrep -r 'auth_handler$' src",  # regex end anchor
            r"ugrep -rE 'auth_handler\d{1,2}' src",  # regex quantifier
            "ugrep -rE 'auth_handler{1,2}' src",
            "bfs src -name 'auth_handler.{ts,tsx}'",  # brace list in single quotes
            'ugrep -r "auth_[a-z]+" src',  # double quotes: only `$` and backtick expand
            "ugrep -rE 'auth_handler|/etc/passwd' src",  # `|` and `/` inside quotes are literal
            r"ugrep -r auth_handler \$HOME",  # an escaped `$` is a literal, not an expansion
        ],
    )
    def test_quoted_regex_with_expansion_characters_sends(
        self,
        project: Project,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        command: str,
    ) -> None:
        """Round 2 skipped these because shlex cannot tell a quoted regex from an
        unquoted glob.  The scanner tracks quoting: single quotes expand nothing,
        double quotes only `$` and backtick, so a quoted regex is judged as a path
        only."""
        _, fetch = _run_preload(project, monkeypatch, capsys, _bash(command))
        fetch.assert_called_once()
        assert "auth" in fetch.call_args.kwargs["query"]

    @pytest.mark.parametrize(
        "command",
        [
            "ugrep -r auth_[a-z]+ src",  # unquoted: the shell would glob it
            "ugrep -r auth_handler{a,/etc}",  # unquoted brace expansion
            "bfs {a,/etc} -name auth_handler.py",
            'ugrep -r "auth_handler$" src',  # double quotes still expand `$`
            'ugrep -r "auth_`id`" src',  # ...and backticks
            'ugrep -r auth_handler "$X"',
            "ugrep -r auth_handler src/{a,b}",
            "ugrep -r auth_handler 'a'{b,c}",  # a quoted prefix does not quote the brace
        ],
    )
    def test_unquoted_or_double_quoted_expansion_still_skipped(
        self,
        project: Project,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        command: str,
    ) -> None:
        out, fetch = _run_preload(project, monkeypatch, capsys, _bash(command))
        _assert_skipped(project, out, fetch)

    @pytest.mark.parametrize(
        "command",
        [
            "ugrep -r auth_handler src 2>/dev/null",
            "ugrep -r auth_handler src 2> /dev/null",
            "ugrep -r auth_handler src >/dev/null 2>&1",
            "ugrep -r auth_handler src 1>&2",
            "ugrep -r auth_handler src &>/dev/null",
            "ugrep -r auth_handler src < /dev/null",
            "ugrep -r auth_handler src 2>/dev/null | head -5",
            "bfs src -name auth_handler.py 2>/dev/null",
        ],
    )
    def test_redirects_to_dev_null_and_fds_send(
        self,
        project: Project,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        command: str,
    ) -> None:
        """Claude writes `2>/dev/null` constantly; it must not cost a nudge."""
        _, fetch = _run_preload(project, monkeypatch, capsys, _bash(command))
        fetch.assert_called_once()

    @pytest.mark.parametrize(
        "command",
        [
            "ugrep -r auth_handler src 2>/etc/x",
            "ugrep -r auth_handler src > /etc/x",
            "ugrep -r auth_handler src >/dev/nulll",  # not /dev/null
            "ugrep -r auth_handler src 2>/dev/null >/etc/x",  # one allowed, one not
            "ugrep -r auth_handler src 2>&1 >/etc/x",
        ],
    )
    def test_other_redirect_targets_still_skip(
        self,
        project: Project,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        command: str,
    ) -> None:
        out, fetch = _run_preload(project, monkeypatch, capsys, _bash(command))
        _assert_skipped(project, out, fetch)

    def test_non_search_bash_is_untouched(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Bash commands that are not bfs/ugrep never reach the containment check."""
        out, fetch = _run_preload(project, monkeypatch, capsys, _bash("cat /etc/hosts"))
        assert out == {}
        fetch.assert_not_called()


# ---------------------------------------------------------------------------
# preload_context -- session cwd differs from the project root
# ---------------------------------------------------------------------------


class TestBashSearchOutsideRoot:
    """Direct tests of the Bash rule where the end-to-end path cannot isolate it.

    End to end, a search after `cd` out of the project is also caught word by
    word (the pattern and flag-cluster letters resolve against the outside cwd),
    which masks the explicit rule that a Bash search needs its cwd inside the
    root.  These commands have only absolute-inside words and 2-character flags,
    so that rule is the only thing that can reject them.
    """

    def test_all_inside_words_with_cwd_outside_is_outside(self, project: Project) -> None:
        command = f"bfs {project.root / 'src'} -L"
        assert _containment.bash_search_outside_root(command, project.root, project.other)

    def test_same_command_with_cwd_inside_is_inside(self, project: Project) -> None:
        command = f"bfs {project.root / 'src'} -L"
        assert not _containment.bash_search_outside_root(command, project.root, project.root)

    def test_cwd_inside_a_subdirectory_is_inside(self, project: Project) -> None:
        assert not _containment.bash_search_outside_root("bfs -L", project.root, project.root / "src")

    def test_program_name_alone_is_not_a_target(self, project: Project) -> None:
        assert not _containment.bash_search_outside_root("bfs", project.root, project.root)


class TestPreloadSessionCwd:
    """After `cd`, Claude's cwd (event `cwd`) and the hook process cwd both move;
    CLAUDE_PROJECT_DIR does not.  A search that names no path runs in the cwd.

    These tests supply the key from the PROJECT ROOT's db (``_config`` default) to
    isolate the path containment rule.  In a real session a `cd` also changes
    which ``.neuroloom.db`` the key comes from -- that interaction, and the skip
    it causes, is tested in ``TestCredentialMatchesRoot``."""

    def test_grep_without_path_after_cd_into_sibling_skipped(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The reported scenario: `cd ../other`, then Grep(pattern='api_key')."""
        out, fetch = _run_preload(
            project, monkeypatch, capsys, _grep("def auth_handler"), cwd=project.other
        )
        _assert_skipped(project, out, fetch)

    def test_glob_without_path_after_cd_into_sibling_skipped(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out, fetch = _run_preload(
            project, monkeypatch, capsys, _glob("src/**/auth_handler.py"), cwd=project.other
        )
        _assert_skipped(project, out, fetch)

    def test_bash_search_without_path_after_cd_into_sibling_skipped(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out, fetch = _run_preload(
            project, monkeypatch, capsys, _bash("ugrep -r auth_handler"), cwd=project.other
        )
        _assert_skipped(project, out, fetch)

    def test_grep_with_explicit_inside_path_after_cd_out_still_sends(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, fetch = _run_preload(
            project,
            monkeypatch,
            capsys,
            _grep("def auth_handler", str(project.root / "src")),
            cwd=project.other,
        )
        fetch.assert_called_once()

    def test_read_of_absolute_inside_path_after_cd_out_still_sends(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, fetch = _run_preload(
            project,
            monkeypatch,
            capsys,
            _read(str(project.root / "src" / "auth_handler.py")),
            cwd=project.other,
        )
        fetch.assert_called_once()

    def test_grep_without_path_after_cd_into_subdirectory_sends(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, fetch = _run_preload(
            project, monkeypatch, capsys, _grep("def auth_handler"), cwd=project.root / "src"
        )
        fetch.assert_called_once()

    def test_relative_read_resolves_against_event_cwd_not_process_cwd(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Event cwd is proj/src, process cwd is proj: `auth_handler.py` is only
        inside if it is resolved against the EVENT cwd."""
        _, fetch = _run_preload(
            project,
            monkeypatch,
            capsys,
            _read("auth_handler.py"),
            cwd=project.root / "src",
            process_cwd=project.root,
        )
        fetch.assert_called_once()

    def test_relative_read_outside_by_event_cwd_skipped(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Process cwd is proj (where `secret.py` would be 'inside'), but the event
        cwd is other/, which is where the relative path actually points."""
        out, fetch = _run_preload(
            project,
            monkeypatch,
            capsys,
            _read("secret.py"),
            cwd=project.other,
            process_cwd=project.root,
        )
        _assert_skipped(project, out, fetch)

    def test_relative_glob_pattern_resolves_against_event_cwd(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, fetch = _run_preload(
            project,
            monkeypatch,
            capsys,
            _glob("../src/auth_handler.py"),
            cwd=project.root / "src",
            process_cwd=project.root,
        )
        fetch.assert_called_once()

    def test_missing_event_cwd_falls_back_to_process_cwd_inside(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, fetch = _run_preload(
            project, monkeypatch, capsys, _grep("def auth_handler"), send_cwd=False
        )
        fetch.assert_called_once()

    def test_missing_event_cwd_falls_back_to_process_cwd_outside(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out, fetch = _run_preload(
            project,
            monkeypatch,
            capsys,
            _grep("def auth_handler"),
            cwd=project.other,  # becomes the process cwd; no `cwd` key is sent
            send_cwd=False,
        )
        _assert_skipped(project, out, fetch)

    def test_unset_project_dir_degrades_to_process_cwd(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Documents the known weakness of the fallback: with CLAUDE_PROJECT_DIR
        unset, the root is the process cwd, which follows `cd`, so a search after
        `cd ../other` is judged against `other` itself and passes.  Containment is
        only as strong as CLAUDE_PROJECT_DIR being set.  (Targets named explicitly
        are still checked: see the next test.)"""
        monkeypatch.delenv("CLAUDE_PROJECT_DIR")
        _, fetch = _run_preload(
            project,
            monkeypatch,
            capsys,
            _grep("def auth_handler"),
            cwd=project.other,
            db_at=project.other / ".neuroloom.db",  # the key is read from the process cwd
        )
        fetch.assert_called_once()

    def test_unset_project_dir_still_checks_explicit_targets_against_cwd(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("CLAUDE_PROJECT_DIR")
        out, fetch = _run_preload(
            project, monkeypatch, capsys, _read(str(project.other / "secret.py")), cwd=project.root
        )
        _assert_skipped(project, out, fetch)


# ---------------------------------------------------------------------------
# preload_context -- session_id sent to the API
# ---------------------------------------------------------------------------


class TestPreloadSessionId:
    def test_no_sessions_row_sends_no_session_id(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Previously the fallback sent the absolute project path as session_id."""
        _, fetch = _run_preload(
            project, monkeypatch, capsys, _read(str(project.root / "src" / "auth_handler.py"))
        )
        fetch.assert_called_once()
        assert fetch.call_args.kwargs["session_id"] is None
        assert str(project.root) not in repr(fetch.call_args.kwargs)

    def test_valid_sessions_row_sends_its_session_id(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed_session(project)
        _, fetch = _run_preload(
            project, monkeypatch, capsys, _read(str(project.root / "src" / "auth_handler.py"))
        )
        assert fetch.call_args.kwargs["session_id"] == SESSION_ID

    def test_corrupt_sessions_row_sends_no_session_id(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed_session(project, session_id="/Users/someone/not-a-session-id")
        _, fetch = _run_preload(
            project, monkeypatch, capsys, _read(str(project.root / "src" / "auth_handler.py"))
        )
        fetch.assert_called_once()
        assert fetch.call_args.kwargs["session_id"] is None
        assert "someone" not in repr(fetch.call_args)
        assert ("corrupt_session",) in _query(
            project, "SELECT decision FROM traces WHERE script = 'preload_context'"
        )


# ---------------------------------------------------------------------------
# cache_prefetch
# ---------------------------------------------------------------------------


def _run_prefetch(
    project: Project,
    monkeypatch: pytest.MonkeyPatch,
    new_cwd: str,
    *,
    process_cwd: Path | None = None,
    db_at: Path | None = None,
) -> tuple[MagicMock, MagicMock, threading.Event]:
    """Run cache_prefetch.main() for a CwdChanged event.

    The hook process runs in Claude's current directory, which Claude Code has
    already moved to ``new_cwd`` by the time the hook fires -- so the process cwd
    defaults to ``new_cwd`` (resolved).

    Returns (the _prefetch mock, the open_db mock, an Event set when _prefetch ran).
    """
    monkeypatch.chdir(process_cwd or Path(new_cwd).resolve())
    called = threading.Event()
    prefetch = MagicMock(side_effect=lambda *a, **k: called.set())
    open_db = MagicMock(side_effect=_db_mod.open_db)
    cfg = _config(project, db_at)
    monkeypatch.setattr(_prefetch_mod, "load", lambda: cfg)
    monkeypatch.setattr(_prefetch_mod, "_prefetch", prefetch)
    monkeypatch.setattr(_prefetch_mod, "open_db", open_db)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"cwd": new_cwd})))
    try:
        _prefetch_mod.main()
    except SystemExit:
        pass
    return prefetch, open_db, called


class TestCachePrefetch:
    def test_directory_inside_root_is_prefetched(
        self, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        new_cwd = str(project.root / "src")
        prefetch, _, called = _run_prefetch(project, monkeypatch, new_cwd)

        assert called.wait(timeout=2), "prefetch should run for an in-root directory"
        prefetch.assert_called_once()
        assert prefetch.call_args.args[3] == new_cwd

    def test_project_root_itself_is_prefetched(
        self, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        prefetch, _, called = _run_prefetch(project, monkeypatch, str(project.root))
        assert called.wait(timeout=2)
        prefetch.assert_called_once()

    @pytest.mark.parametrize(
        "relative",
        [
            "../other",  # sibling directory
            "src/../../other",  # `..` traversal that starts inside
            "link",  # symlink inside the root pointing out
        ],
    )
    def test_directory_outside_root_is_skipped_even_though_process_cwd_followed_the_cd(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, relative: str
    ) -> None:
        """The hook process runs in the new directory, so comparing the target with
        the process cwd would compare it with itself.  CLAUDE_PROJECT_DIR makes the
        check real."""
        new_cwd = os.path.join(project.root, relative)
        prefetch, open_db, called = _run_prefetch(project, monkeypatch, new_cwd)

        assert not called.wait(timeout=0.3)
        prefetch.assert_not_called()
        # Quiet no-op: the hook exits before touching the state DB at all.
        open_db.assert_not_called()
        assert not project.db_path.exists()

    def test_absolute_system_directory_is_skipped(
        self, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        prefetch, open_db, _ = _run_prefetch(project, monkeypatch, "/etc")
        prefetch.assert_not_called()
        open_db.assert_not_called()

    def test_unset_project_dir_falls_back_to_process_cwd(
        self, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Documented weakness: with CLAUDE_PROJECT_DIR unset the root is the hook's
        own cwd.  If that cwd is still the project root the check works; once the
        process cwd has followed the `cd` it compares the target with itself and
        passes (the API key lookup is then the only protection)."""
        monkeypatch.delenv("CLAUDE_PROJECT_DIR")

        # Process cwd still at the project root: the sibling is rejected.
        prefetch, _, called = _run_prefetch(
            project, monkeypatch, str(project.other), process_cwd=project.root
        )
        assert not called.wait(timeout=0.3)
        prefetch.assert_not_called()

        # Process cwd followed the cd: the check is vacuous.  (The key now comes
        # from the new directory's own .neuroloom.db, so root and key still agree.)
        prefetch, _, called = _run_prefetch(
            project,
            monkeypatch,
            str(project.other),
            process_cwd=project.other,
            db_at=project.other / ".neuroloom.db",
        )
        assert called.wait(timeout=2)
        prefetch.assert_called_once()


# ---------------------------------------------------------------------------
# capture (PostToolUse): events about outside-project targets are dropped
# ---------------------------------------------------------------------------

SECRET = "TOP-SECRET-FILE-CONTENT"


def _run_capture(
    project: Project,
    monkeypatch: pytest.MonkeyPatch,
    event: dict[str, Any],
    *,
    cwd: Path | None = None,
    process_cwd: Path | None = None,
    db_at: Path | None = None,
) -> tuple[MagicMock, threading.Event]:
    """Run capture.main() with a real state DB and a mocked HTTP POST.

    Drives the real ``_submit`` thread, so a dropped event is proven absent from
    both the network call AND the replay buffer.  Returns (post_json mock, an
    Event set when a POST happened).
    """
    cwd = cwd or project.root
    monkeypatch.chdir(process_cwd or cwd)
    _seed_session(project, db_at=db_at)
    sent = threading.Event()

    def _fake_post(url: str, headers: dict[str, str], payload: bytes, timeout: float) -> Any:
        sent.set()
        return (200, b"{}")

    post = MagicMock(side_effect=_fake_post)
    cfg = _config(project, db_at)
    monkeypatch.setattr(_capture_mod, "load", lambda: cfg)
    monkeypatch.setattr(_capture_mod, "post_json", post)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({**event, "cwd": str(cwd)})))
    try:
        _capture_mod.main()
    except SystemExit:
        pass
    return post, sent


def _assert_dropped(
    project: Project,
    post: MagicMock,
    sent: threading.Event,
    *,
    db_at: Path | None = None,
    reason: str = "outside_project",
) -> None:
    assert not sent.wait(timeout=0.3), "a dropped event must not be POSTed"
    post.assert_not_called()
    assert _query(project, "SELECT COUNT(*) FROM event_buffer", db_at) == [(0,)]
    # The throttle window was not consumed either.
    assert _query(project, "SELECT last_submit_ms FROM sessions", db_at) == [(0,)]
    assert (reason,) in _query(
        project, "SELECT decision FROM traces WHERE script = 'pyhooks.capture'", db_at
    )


def _read_event(path: str) -> dict[str, Any]:
    return {
        "tool_name": "Read",
        "tool_input": {"file_path": path},
        "tool_response": {"type": "text", "file": {"filePath": path, "content": SECRET}},
    }


def _outside_events(project: Project) -> dict[str, dict[str, Any]]:
    secret = str(project.other / "secret.py")
    return {
        "read_sibling": _read_event(secret),
        "read_system": _read_event("/etc/hosts"),
        "read_traversal": _read_event(f"{project.root}/src/../../other/secret.py"),
        "read_symlink_out": _read_event(str(project.root / "link" / "secret.py")),
        "write": {"tool_name": "Write", "tool_input": {"file_path": secret, "content": SECRET}},
        "edit": {
            "tool_name": "Edit",
            "tool_input": {"file_path": secret, "old_string": SECRET, "new_string": "x"},
        },
        "notebook_edit": {
            "tool_name": "NotebookEdit",
            "tool_input": {"notebook_path": str(project.other / "n.ipynb"), "new_source": SECRET},
        },
        "glob_path": {
            "tool_name": "Glob",
            "tool_input": {"pattern": "*.py", "path": str(project.other)},
        },
        "glob_unprovable": {"tool_name": "Glob", "tool_input": {"pattern": "{..,src}/x.py"}},
        "grep_path": {
            "tool_name": "Grep",
            "tool_input": {"pattern": "api_key", "path": str(project.other)},
        },
        "read_without_target": {"tool_name": "Read", "tool_input": {}},
    }


class TestCapture:
    @pytest.mark.parametrize(
        "name",
        [
            "read_sibling",
            "read_system",
            "read_traversal",
            "read_symlink_out",
            "write",
            "edit",
            "notebook_edit",
            "glob_path",
            "glob_unprovable",
            "grep_path",
            "read_without_target",
        ],
    )
    def test_event_with_outside_target_is_dropped(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, name: str
    ) -> None:
        post, sent = _run_capture(project, monkeypatch, _outside_events(project)[name])
        _assert_dropped(project, post, sent)

    def test_grep_without_path_after_cd_out_is_dropped(
        self, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        post, sent = _run_capture(
            project,
            monkeypatch,
            {"tool_name": "Grep", "tool_input": {"pattern": "api_key"}},
            cwd=project.other,
        )
        _assert_dropped(project, post, sent)

    def test_relative_read_resolves_against_event_cwd(
        self, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        post, sent = _run_capture(
            project, monkeypatch, _read_event("secret.py"), cwd=project.other, process_cwd=project.other
        )
        _assert_dropped(project, post, sent)

    def test_read_inside_root_is_captured_with_its_content(
        self, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        event = _read_event(str(project.root / "src" / "auth_handler.py"))
        post, sent = _run_capture(project, monkeypatch, event)

        assert sent.wait(timeout=2)
        post.assert_called_once()
        assert SECRET.encode() in post.call_args.args[2]

    @pytest.mark.parametrize(
        "event",
        [
            {"tool_name": "Write", "tool_input": {"file_path": "{root}/src/new.py", "content": "x"}},
            {"tool_name": "Grep", "tool_input": {"pattern": "api_key", "path": "{root}/src"}},
            {"tool_name": "Grep", "tool_input": {"pattern": "api_key"}},
            {"tool_name": "Glob", "tool_input": {"pattern": "src/**/*.py"}},
        ],
    )
    def test_inside_root_events_are_captured(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, event: dict[str, Any]
    ) -> None:
        event = json.loads(json.dumps(event).replace("{root}", str(project.root)))
        post, sent = _run_capture(project, monkeypatch, event)
        assert sent.wait(timeout=2)
        post.assert_called_once()

    def test_inside_absolute_read_after_cd_out_is_still_captured(
        self, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        event = _read_event(str(project.root / "src" / "auth_handler.py"))
        post, sent = _run_capture(project, monkeypatch, event, cwd=project.other)
        assert sent.wait(timeout=2)
        post.assert_called_once()

    def test_bash_is_not_contained(
        self, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Documented limit: a command's output cannot be attributed to a path, so
        Bash events are captured as before -- including output from outside the
        project.  Do not read this test as a goal."""
        event = {
            "tool_name": "Bash",
            "tool_input": {"command": "cat /etc/hosts"},
            "tool_response": {"stdout": SECRET},
        }
        post, sent = _run_capture(project, monkeypatch, event)
        assert sent.wait(timeout=2)
        post.assert_called_once()
        assert SECRET.encode() in post.call_args.args[2]

    def test_non_path_tools_are_unchanged(
        self, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        event = {"tool_name": "WebFetch", "tool_input": {"url": "https://example.com", "prompt": "x"}}
        post, sent = _run_capture(project, monkeypatch, event)
        assert sent.wait(timeout=2)
        post.assert_called_once()


# ---------------------------------------------------------------------------
# code_graph_sync._within_workspace delegates to the shared rule
# ---------------------------------------------------------------------------


class TestCodeGraphSyncDelegation:
    def test_delegates_to_containment(
        self, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        within = MagicMock(return_value=True)
        monkeypatch.setattr(_containment, "within_root", within)
        assert _cgs_mod._within_workspace("/some/file.py", project.root) is True
        within.assert_called_once_with("/some/file.py", project.root)

    @pytest.mark.parametrize(
        "name",
        ["inside", "sibling", "traversal", "symlink_out", "inner_symlink", "prefix_sibling"],
    )
    def test_same_answer_as_the_shared_rule(self, project: Project, name: str) -> None:
        evil = project.root.parent / "proj-evil"
        evil.mkdir(exist_ok=True)
        raw = {
            "inside": str(project.root / "src" / "auth_handler.py"),
            "sibling": str(project.other / "secret.py"),
            "traversal": f"{project.root}/src/../../other/secret.py",
            "symlink_out": str(project.root / "link" / "secret.py"),
            "inner_symlink": str(project.root / "inner_link" / "auth_handler.py"),
            "prefix_sibling": str(evil / "x.py"),
        }[name]
        assert _cgs_mod._within_workspace(raw, project.root) == within_root(raw, project.root)
        assert _cgs_mod._within_workspace(raw, project.root) == (
            name in {"inside", "inner_symlink"}
        )


# ---------------------------------------------------------------------------
# Round 3, item 1: the project root and the API key's directory must agree
# ---------------------------------------------------------------------------


class TestCredentialMatchesRoot:
    """`config.load()` reads the key from `<process cwd>/.neuroloom.db`, and the
    process cwd follows `cd`.  After a `cd` into a sibling or nested project that
    has its OWN key, reading a file in the original project would send it with
    THAT key to THAT workspace.  Every sending hook must skip."""

    def test_unit_same_directory_matches(self, project: Project) -> None:
        assert _containment.credential_matches_root(project.root / ".neuroloom.db", project.root)

    def test_unit_sibling_nested_and_missing_do_not_match(self, project: Project) -> None:
        for db in (
            project.other / ".neuroloom.db",
            project.root / "src" / ".neuroloom.db",
            project.root.parent / ".neuroloom.db",
        ):
            assert not _containment.credential_matches_root(db, project.root)

    def test_unit_symlinked_spelling_of_the_root_matches(
        self, project: Project, tmp_path: Path
    ) -> None:
        alias = tmp_path / "alias"
        alias.symlink_to(project.root, target_is_directory=True)
        assert _containment.credential_matches_root(alias / ".neuroloom.db", project.root)

    def test_unit_unresolvable_path_fails_closed(self, project: Project) -> None:
        assert not _containment.credential_matches_root(Path("bad\x00dir/.neuroloom.db"), project.root)

    # -- preload_context ---------------------------------------------------

    def test_preload_sibling_key_with_inside_project_read_is_skipped(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The reproduced bug: cwd=sibling (own db + key), CLAUDE_PROJECT_DIR=project,
        and the file read IS inside the project.  Containment alone passes it."""
        db = project.other / ".neuroloom.db"
        out, fetch = _run_preload(
            project,
            monkeypatch,
            capsys,
            _read(str(project.root / "src" / "auth_handler.py")),
            cwd=project.other,
            db_at=db,
        )
        _assert_skipped(project, out, fetch, db_at=db, reason="credential_root_mismatch")

    def test_preload_same_call_with_the_projects_own_key_sends(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, fetch = _run_preload(
            project, monkeypatch, capsys, _read(str(project.root / "src" / "auth_handler.py"))
        )
        fetch.assert_called_once()

    def test_preload_nested_project_key_is_skipped(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db = project.root / "src" / ".neuroloom.db"
        out, fetch = _run_preload(
            project,
            monkeypatch,
            capsys,
            _grep("def auth_handler"),
            cwd=project.root / "src",
            db_at=db,
        )
        _assert_skipped(project, out, fetch, db_at=db, reason="credential_root_mismatch")

    def test_preload_mismatch_precedes_the_target_check(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Even a target the containment rule would reject records the mismatch."""
        db = project.other / ".neuroloom.db"
        out, fetch = _run_preload(
            project,
            monkeypatch,
            capsys,
            _read(str(project.other / "secret.py")),
            cwd=project.other,
            db_at=db,
        )
        _assert_skipped(project, out, fetch, db_at=db, reason="credential_root_mismatch")

    def test_preload_unset_project_dir_is_not_a_regression(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """With CLAUDE_PROJECT_DIR unset the root IS the process cwd, which is where
        the key came from, so the rule is a no-op there."""
        monkeypatch.delenv("CLAUDE_PROJECT_DIR")
        _, fetch = _run_preload(
            project, monkeypatch, capsys, _read(str(project.root / "src" / "auth_handler.py"))
        )
        fetch.assert_called_once()

    # -- capture -----------------------------------------------------------

    def test_capture_sibling_key_with_inside_project_read_is_dropped(
        self, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = project.other / ".neuroloom.db"
        post, sent = _run_capture(
            project,
            monkeypatch,
            _read_event(str(project.root / "src" / "auth_handler.py")),
            cwd=project.other,
            db_at=db,
        )
        _assert_dropped(project, post, sent, db_at=db, reason="credential_root_mismatch")

    def test_capture_nested_project_key_is_dropped(
        self, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = project.root / "src" / ".neuroloom.db"
        post, sent = _run_capture(
            project,
            monkeypatch,
            {"tool_name": "Grep", "tool_input": {"pattern": "api_key"}},
            cwd=project.root / "src",
            db_at=db,
        )
        _assert_dropped(project, post, sent, db_at=db, reason="credential_root_mismatch")

    def test_capture_projects_own_key_still_captures(
        self, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        post, sent = _run_capture(
            project, monkeypatch, _read_event(str(project.root / "src" / "auth_handler.py"))
        )
        assert sent.wait(timeout=2)
        post.assert_called_once()

    def test_capture_unset_project_dir_is_not_a_regression(
        self, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CLAUDE_PROJECT_DIR")
        post, sent = _run_capture(
            project, monkeypatch, _read_event(str(project.root / "src" / "auth_handler.py"))
        )
        assert sent.wait(timeout=2)
        post.assert_called_once()

    # -- cache_prefetch ----------------------------------------------------

    def test_prefetch_nested_project_key_is_skipped_with_no_io(
        self, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`cd src` where src has its own .neuroloom.db: the target is inside the
        project, but the key belongs to the nested project."""
        nested_db = project.root / "src" / ".neuroloom.db"
        prefetch, open_db, called = _run_prefetch(
            project, monkeypatch, str(project.root / "src"), db_at=nested_db
        )
        assert not called.wait(timeout=0.3)
        prefetch.assert_not_called()
        open_db.assert_not_called()  # exits before touching any DB
        assert not nested_db.exists()

    def test_prefetch_sibling_key_is_skipped(
        self, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        prefetch, open_db, _ = _run_prefetch(
            project, monkeypatch, str(project.root), process_cwd=project.other,
            db_at=project.other / ".neuroloom.db",
        )
        prefetch.assert_not_called()
        open_db.assert_not_called()

    def test_prefetch_projects_own_key_still_prefetches(
        self, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        prefetch, _, called = _run_prefetch(project, monkeypatch, str(project.root))
        assert called.wait(timeout=2)
        prefetch.assert_called_once()


# ---------------------------------------------------------------------------
# Round 3, item 5: trace when CLAUDE_PROJECT_DIR is unset
# ---------------------------------------------------------------------------


class TestProjectRootFallbackTrace:
    FALLBACK = ("project_root_fallback_cwd",)

    def test_unit_is_fallback_reflects_the_environment(
        self, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert not _containment.project_root_is_fallback()
        monkeypatch.delenv("CLAUDE_PROJECT_DIR")
        assert _containment.project_root_is_fallback()
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "relative/dir")  # unusable -> fallback
        assert _containment.project_root_is_fallback()

    @pytest.mark.parametrize("env_set", [True, False])
    def test_preload(
        self,
        project: Project,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        env_set: bool,
    ) -> None:
        if not env_set:
            monkeypatch.delenv("CLAUDE_PROJECT_DIR")
        _run_preload(
            project, monkeypatch, capsys, _read(str(project.root / "src" / "auth_handler.py"))
        )
        traces = _query(project, "SELECT decision FROM traces WHERE script = 'preload_context'")
        assert (self.FALLBACK in traces) is (not env_set)

    @pytest.mark.parametrize("env_set", [True, False])
    def test_capture(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, env_set: bool
    ) -> None:
        if not env_set:
            monkeypatch.delenv("CLAUDE_PROJECT_DIR")
        _run_capture(
            project, monkeypatch, _read_event(str(project.root / "src" / "auth_handler.py"))
        )
        traces = _query(project, "SELECT decision FROM traces WHERE script = 'pyhooks.capture'")
        assert (self.FALLBACK in traces) is (not env_set)

    @pytest.mark.parametrize("env_set", [True, False])
    def test_cache_prefetch(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, env_set: bool
    ) -> None:
        if not env_set:
            monkeypatch.delenv("CLAUDE_PROJECT_DIR")
        _, _, called = _run_prefetch(project, monkeypatch, str(project.root))
        assert called.wait(timeout=2)
        traces = _query(
            project, "SELECT decision FROM traces WHERE script = 'pyhooks.cache_prefetch'"
        )
        assert (self.FALLBACK in traces) is (not env_set)


# ---------------------------------------------------------------------------
# Round 3, items 2 and 4: wildcard expansion, braces, and the quote-aware scanner
# ---------------------------------------------------------------------------


def _make_dirs(base: Path, count: int) -> None:
    for number in range(count):
        (base / f"d{number:03d}").mkdir(parents=True)


class TestWildcardExpansion:
    """A wildcard can match an in-project symlink that points out, so a pattern
    that looks relative is not safe until its matches are checked."""

    @pytest.mark.parametrize(
        "command",
        [
            "bfs src/e*/x -name auth_handler.py",  # `src/escape` -> other/
            "ugrep -r auth_handler l?nk",  # `link` -> other/
            "bfs lin* -name secret.txt",  # `link` -> other/
            "ugrep -r auth_handler src/esc*",  # last-part wildcard matching a symlink dir
        ],
    )
    def test_bash_wildcard_matching_an_escaping_symlink_is_skipped(
        self,
        project: Project,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        command: str,
    ) -> None:
        out, fetch = _run_preload(project, monkeypatch, capsys, _bash(command))
        _assert_skipped(project, out, fetch)

    def test_glob_wildcard_directory_matching_an_escaping_symlink_is_skipped(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out, fetch = _run_preload(project, monkeypatch, capsys, _glob("lin*/secret.txt"))
        _assert_skipped(project, out, fetch)

    @pytest.mark.parametrize(
        "command",
        [
            "ugrep -r auth_handler src/p*",  # wildcard matching an in-project directory
            "bfs src/p*/mod.py -name auth_handler.py",
            "ugrep -r auth_handler inner_l*",  # a symlink that stays inside
            "ugrep -r auth_handler nomatch*",  # no match: nothing exists to leak
            "ugrep -r auth_handler 'l?nk'",  # single-quoted: the shell does not glob it
            'ugrep -r auth_handler "l?nk"',  # ...nor does a double-quoted word
        ],
    )
    def test_benign_wildcards_send(
        self,
        project: Project,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        command: str,
    ) -> None:
        _, fetch = _run_preload(project, monkeypatch, capsys, _bash(command))
        fetch.assert_called_once()

    def test_glob_benign_wildcard_directory_sends(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, fetch = _run_preload(project, monkeypatch, capsys, _glob("src/p*/mod.py"))
        fetch.assert_called_once()
        assert fetch.call_args.kwargs["query"] == "mod"

    def test_more_than_the_cap_of_matches_is_skipped_and_exactly_the_cap_is_not(
        self, project: Project
    ) -> None:
        _make_dirs(project.root / "caps" / "ok", 64)
        _make_dirs(project.root / "caps" / "over", 65)
        assert glob_within_root("caps/ok/*/x.py", project.root, base=project.root)
        assert not glob_within_root("caps/over/*/x.py", project.root, base=project.root)

    def test_bash_cap_applies_to_a_last_part_wildcard(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        many = project.root / "src" / "many"
        many.mkdir()
        for number in range(70):
            (many / f"f{number:03d}").write_text("")
        out, fetch = _run_preload(
            project, monkeypatch, capsys, _bash("ugrep -r auth_handler src/many/f*")
        )
        _assert_skipped(project, out, fetch)

    def test_only_the_directory_prefix_is_globbed(self, project: Project) -> None:
        """`src/*/f*.py` globs the directories at `src/*`, not every file."""
        many = project.root / "lib" / "many"  # not under src/, which holds the escaping symlink
        many.mkdir(parents=True)
        for number in range(70):
            (many / f"f{number:03d}.py").write_text("")
        assert glob_within_root("lib/*/f*.py", project.root, base=project.root)

    def test_remainder_after_the_wildcard_is_checked_too(
        self, project: Project, tmp_path: Path
    ) -> None:
        """`src/in*/leak` -- `inner` is a real directory, but `leak` inside it is a
        symlink out.  The match alone is inside; match + remainder is not."""
        inner = project.root / "src" / "inner"
        inner.mkdir()
        (inner / "leak").symlink_to(project.other, target_is_directory=True)
        assert not glob_within_root("src/in*/leak/secret.py", project.root, base=project.root)
        assert glob_within_root("src/in*/other.py", project.root, base=project.root)

    def test_known_gap_double_star_is_not_expanded(self, project: Project) -> None:
        """Documented, not a goal: `**` ends the expandable prefix (expanding it
        against a cap would skip every repo-root `**/*.ts`; this repo has 663
        directories), so a symlink reached through `**` is not detected."""
        (project.root / "src" / "deep").mkdir()
        (project.root / "src" / "deep" / "leak").symlink_to(project.other, target_is_directory=True)
        assert glob_within_root("**/leak/secret.py", project.root, base=project.root)
        assert glob_within_root("src/**/leak/secret.py", project.root, base=project.root)

    def test_wildcard_before_double_star_is_still_expanded(self, project: Project) -> None:
        assert not glob_within_root("lin*/**/secret.py", project.root, base=project.root)

    @pytest.mark.parametrize(
        "command",
        ["bfs -L . -name auth_handler.py", "ugrep -R auth_handler src", "ugrep -rR auth_handler ."],
    )
    def test_known_gap_symlink_following_flags_are_not_modelled(
        self,
        project: Project,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        command: str,
    ) -> None:
        """Documented, not a goal: `bfs -L` and `ugrep -R` follow symlinks inside the
        tree they search.  Only the search term crosses the boundary, never file
        content."""
        _, fetch = _run_preload(project, monkeypatch, capsys, _bash(command))
        fetch.assert_called_once()


class TestGlobBraces:
    @pytest.mark.parametrize(
        "pattern",
        [
            "src/**/auth_handler.{ts,tsx}",
            "{src,pkg}/auth_handler.py",
            "src/{auth_handler,other}.py",
            "{a}/auth_handler.py",  # no comma: read as its one alternative
            "src/{a,{b,c}}/x.py",  # nested
            "src/auth_handler.p[xy]",  # `[..]` is a single-character wildcard
            "src/p[k]g/mod.py",
        ],
    )
    def test_well_formed_inside_braces_and_classes_send(
        self,
        project: Project,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        pattern: str,
    ) -> None:
        assert glob_within_root(pattern, project.root, base=project.root)
        _, fetch = _run_preload(project, monkeypatch, capsys, _glob(pattern))
        if "auth_handler" in pattern or "mod" in pattern:
            fetch.assert_called_once()

    @pytest.mark.parametrize(
        "pattern",
        [
            "{..,src}/auth_handler.py",  # braces can carry `..`
            "{src,..}/auth_handler.py",
            "src/{x,../../other}/secret.py",
            "{src,{..,x}}/auth_handler.py",  # nested
            "{,x}/auth_handler.py",  # empty alternative -> absolute `/auth_handler.py`
            "{/etc,src}/auth_handler.py",
            "{..}/auth_handler.py",  # single alternative
            "src/{a,b/auth_handler.py",  # unbalanced
            "src/a,b}/auth_handler.py",  # stray close
            "{1,2,3,4,5,6}{1,2,3,4,5,6}/auth_handler.py",  # 36 > 32 alternatives
            "{[.][.],x}/auth_handler.py",  # a class that can match a dot, inside braces
        ],
    )
    def test_bad_braces_are_skipped(self, project: Project, pattern: str) -> None:
        assert not glob_within_root(pattern, project.root, base=project.root)

    def test_exactly_the_alternative_cap_is_allowed(self, project: Project) -> None:
        within_cap = "{" + ",".join(f"a{n}" for n in range(32)) + "}/x.py"
        over_cap = "{" + ",".join(f"a{n}" for n in range(33)) + "}/x.py"
        assert glob_within_root(within_cap, project.root, base=project.root)
        assert not glob_within_root(over_cap, project.root, base=project.root)

    @pytest.mark.parametrize("pattern", ["[.][.]/x", "[.]*/x", "[!a]*/x", "src/.[.]/x"])
    def test_a_class_that_can_match_a_dot_counts_as_leading_dot(
        self, project: Project, pattern: str
    ) -> None:
        """`[.]` and `[!a]` can match `.`/`..` the way a literal leading dot can."""
        assert not glob_within_root(pattern, project.root, base=project.root)

    def test_glob_tool_brace_pattern_end_to_end_sends_with_its_query(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """capture drops the WHOLE Glob event on a skip, so `*.{ts,tsx}` globs were
        vanishing from the observation stream."""
        _, fetch = _run_preload(
            project, monkeypatch, capsys, _glob("src/**/auth_handler.{ts,tsx}")
        )
        fetch.assert_called_once()
        assert fetch.call_args.kwargs["query"] == "auth_handler"

    def test_glob_tool_brace_event_is_captured(
        self, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        post, sent = _run_capture(
            project,
            monkeypatch,
            {"tool_name": "Glob", "tool_input": {"pattern": "src/**/*.{ts,tsx}"}},
        )
        assert sent.wait(timeout=2)
        post.assert_called_once()

    def test_glob_tool_brace_event_with_a_dotdot_alternative_is_dropped(
        self, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        post, sent = _run_capture(
            project,
            monkeypatch,
            {"tool_name": "Glob", "tool_input": {"pattern": "{..,src}/*.py"}},
        )
        _assert_dropped(project, post, sent)


class TestBashScanner:
    """`bash_search_outside_root` as a table: (command, is_outside)."""

    CASES = [
        # -- quoting decides what can still expand ---------------------------
        ("bfs '$HOME'", False),  # single quotes: literal
        ("bfs '`id`' src", False),
        ("bfs '{a,/etc}'", False),
        ("bfs '[a-z]+'", False),
        ('bfs "[a-z]+" "{a,b}"', False),  # double quotes: only $ and backtick expand
        ('bfs "a\\$b"', False),  # backslash-escaped `$` inside double quotes is a literal
        ("bfs a\\$b", False),  # ...and outside quotes
        ("bfs 'a;b|c>d'", False),  # operators inside quotes are literal
        (r"bfs a\;b", False),  # an escaped `;` is a literal
        ('bfs "$X"', True),
        ('bfs "a`id`"', True),
        ("bfs $X", True),
        ("bfs `id`", True),
        ("bfs {a,b}", True),
        ("bfs {a,/etc}", True),
        ("bfs [ab]", True),
        ("bfs 'a'{b,c}", True),  # a quoted prefix does not quote the brace
        ('bfs "unterminated', True),
        ("bfs 'unterminated", True),
        # -- operators and redirections ---------------------------------------
        ("bfs . -name x;echo /etc", True),
        ("bfs . | xargs cat", False),
        ("bfs . 2>/dev/null", False),
        ("bfs . >/dev/null 2>&1", False),
        ("bfs . 1>&2", False),
        ("bfs . >/etc/x", True),
        ("bfs . > /etc/x", True),
        ("bfs . 2>&3", False),  # an fd other than 1/2 is just a relative word
        ("bfs . >out.txt", False),
        # -- flags ---------------------------------------------------------------
        ("bfs -f/etc", True),
        ("bfs --include=/etc/x", True),
        ("bfs -rf/etc/passwd", True),
        ("bfs --include=*.py", False),
        ("bfs -L .", False),
        ("bfs -name '*.{ts,tsx}'", False),
        # -- plain words -----------------------------------------------------------
        ("bfs", False),
        ("bfs src", False),
        ("bfs **/..", True),  # zsh recurses on `**`; never expanded here, so the rule must reject
        ("bfs **/../x", True),
        ("bfs ../other", True),
        ("bfs /etc", True),
        ("bfs ~", True),
        ("bfs ~+/..", True),
        ("bfs link", True),
        # -- round 4: Bash deletes backslash-newline before it looks at the word ----
        ("bfs ." + BS_NL + "./x", True),
        ('bfs ".' + BS_NL + '."', True),  # ...also inside double quotes
        ("ugrep -r foo ." + BS_NL + ".", True),
        ("ugrep foo " + BS_NL + "/etc", True),
        ("ugrep foo .." + BS_NL + "/..", True),
        ("ugrep -r foo sr" + BS_NL + "c", False),  # joins to `src`
        ('ugrep -r foo "sr' + BS_NL + 'c"', False),
        ("bfs src " + BS_NL + "-name x", False),  # a continuation between words
        ("bfs '." + BS_NL + ".'", False),  # single quotes: the backslash is literal
        # -- round 4: zsh expands an unquoted word starting with `=` ---------------
        ("bfs =ls", True),
        ("bfs src =ls", True),
        ("bfs '=ls'", False),
        ('bfs "=ls"', False),
        ("bfs " + chr(92) + "=ls", False),  # escaped
        ("bfs a=b", False),  # `=` not at the start of the word
        ("bfs --x=y", False),
        # -- round 4: in Bash, `**` is just `*` (globstar off by default) ------------
        ("bfs **/secret.txt", True),  # `*` matches `link`, a symlink out
        ("bfs **", True),
        # -- round 5: zsh recurses on `**/`; a trailing `**` is still just `*` --------
        ("bfs src/**/", True),
        ("bfs src/**/x", True),
        ("bfs src/***/x", True),
        ("bfs **/x", True),
        ("bfs clean/**", False),
        ("bfs src/**x/y", False),
        # -- round 5: a quoted or escaped glob character beside a live wildcard -------
        ("ugrep foo 'a[b]'*", True),
        ('ugrep foo "a[b]"*', True),
        ("ugrep foo a" + BS + "[b" + BS + "]*", True),
        ("bfs 'a*'?", True),
        ("bfs 'x'*", False),  # quoted, but nothing quoted is a glob character
        ("bfs '[a-z]+' src", False),
    ]

    @pytest.mark.parametrize(("command", "outside"), CASES)
    def test_command(self, project: Project, command: str, outside: bool) -> None:
        assert _containment.bash_search_outside_root(command, project.root, project.root) is outside


# ---------------------------------------------------------------------------
# Round 4: Bash `**`, dotfiles, our own directory scan, and the brace work bound
# ---------------------------------------------------------------------------


class TestBashDoubleStar:
    """Bash has globstar off by default, so `**` is just `*` -- but zsh (this machine's
    $SHELL) recurses on `**/` and follows symlinks on `***/`.  So a TRAILING `**` keeps
    the `*` treatment and an unquoted `**` with more path parts is failed closed."""

    def _zsh_tree(self, project: Project) -> None:
        """src/a/b/etclink -> a directory outside the project (the reviewers used /etc)."""
        (project.root / "src" / "a" / "b").mkdir(parents=True)
        (project.root / "src" / "a" / "b" / "etclink").symlink_to(
            project.other, target_is_directory=True
        )
        (project.other / "passwd").write_text("root:x" + chr(10))

    @pytest.mark.parametrize(
        "command",
        [
            "bfs src/**/etclink/passwd",  # the reviewers' first reproduction
            "ugrep -r root src/**/etclink",  # ...and the second
            "ugrep -r x **/secret.txt",
            "bfs src/**/",  # a trailing slash makes it `**/`
            "bfs src/***/etclink",  # zsh `***/` recurses AND follows symlinks
            "bfs **/x",
        ],
    )
    def test_double_star_followed_by_more_parts_is_failed_closed(
        self, project: Project, command: str
    ) -> None:
        self._zsh_tree(project)
        assert _containment.bash_search_outside_root(command, project.root, project.root)

    def test_failed_closed_even_when_nothing_would_escape(self, project: Project) -> None:
        """Deliberate: the rule is syntactic, not a walk, so it does not depend on the tree."""
        (project.root / "clean" / "sub").mkdir(parents=True)
        (project.root / "clean" / "sub" / "mod.py").write_text("")
        assert _containment.bash_search_outside_root(
            "ugrep -r x **/mod.py", project.root, project.root / "clean"
        )

    @pytest.mark.parametrize(
        "command",
        [
            "bfs deep/**/etclink",
            "bfs deep/***/etclink",  # `***/` is the form that also follows symlinks
            "ugrep -r x deep/**/etclink/passwd",
        ],
    )
    def test_an_escape_below_the_first_level_is_only_caught_by_the_recursion_rule(
        self, project: Project, command: str
    ) -> None:
        """`deep/*` (one level) matches only `deep/a`, which is inside, and `deep/a/etclink`
        does not exist -- so single-level expansion of `**` passes.  zsh recurses to
        `deep/a/b/etclink`, a symlink out.  Only the syntactic rule fails this closed."""
        (project.root / "deep" / "a" / "b").mkdir(parents=True)
        (project.root / "deep" / "a" / "b" / "etclink").symlink_to(
            project.other, target_is_directory=True
        )
        assert _containment.bash_search_outside_root(command, project.root, project.root)

    @pytest.mark.parametrize("command", ["bfs clean/**", "bfs src/**x/y", "bfs a**b/x"])
    def test_a_trailing_or_non_bare_double_star_keeps_the_star_treatment(
        self, project: Project, command: str
    ) -> None:
        (project.root / "clean" / "sub").mkdir(parents=True)
        assert not _containment.bash_search_outside_root(command, project.root, project.root)

    def test_a_trailing_double_star_is_still_expanded_like_a_star(self, project: Project) -> None:
        """`bfs **` matches the symlink `link` at the top of the project."""
        assert _containment.bash_search_outside_root("bfs **", project.root, project.root)

    @pytest.mark.parametrize(
        "command",
        [
            "ugrep -r auth_handler src/**/etclink",
            "bfs src/**/etclink -name auth_handler.py",
        ],
    )
    def test_end_to_end_the_reproductions_are_skipped(
        self,
        project: Project,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        command: str,
    ) -> None:
        self._zsh_tree(project)
        out, fetch = _run_preload(project, monkeypatch, capsys, _bash(command))
        _assert_skipped(project, out, fetch)

    def test_the_glob_tool_keeps_its_documented_double_star_gap(self, project: Project) -> None:
        """The same text in a Glob tool pattern is NOT expanded: that engine recurses on
        `**` and is assumed (by review, not verified here) not to follow symlinks."""
        assert glob_within_root("**/secret.txt", project.root, base=project.root)


class TestQuotedGlobCharacters:
    """The shell matches a quoted or escaped glob character literally -- `'a[b]'*` matches
    the file `a[b]x` -- but the filesystem scan cannot tell which characters were quoted
    and would read `[b]` as a class.  A word with a live wildcard AND a quoted or escaped
    `[ ] * ?` is failed closed."""

    def _tree(self, project: Project) -> None:
        """`a[b]x` is an in-project symlink pointing outside."""
        (project.root / "a[b]x").symlink_to(project.other, target_is_directory=True)

    @pytest.mark.parametrize(
        "command",
        [
            "ugrep foo 'a[b]'*",  # the reviewers' three reproductions
            "ugrep foo a" + BS + "[b" + BS + "]*",
            'ugrep foo "a[b]"*',
            "ugrep foo 'a*'?",  # a quoted `*` beside a live `?`
            'ugrep foo "a?"*',
            "ugrep foo a" + BS + "?*",
        ],
    )
    def test_quoted_or_escaped_glob_character_beside_a_live_wildcard_is_failed_closed(
        self, project: Project, command: str
    ) -> None:
        self._tree(project)
        assert _containment.bash_search_outside_root(command, project.root, project.root)

    @pytest.mark.parametrize(
        "command",
        [
            "ugrep auth_handler 'a[b]'*",
            'ugrep auth_handler "a[b]"*',
            "ugrep auth_handler a" + BS + "[b" + BS + "]*",
        ],
    )
    def test_end_to_end_the_reproductions_are_skipped(
        self,
        project: Project,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        command: str,
    ) -> None:
        self._tree(project)
        out, fetch = _run_preload(project, monkeypatch, capsys, _bash(command))
        _assert_skipped(project, out, fetch)

    @pytest.mark.parametrize(
        "command",
        [
            "ugrep -r foo '[a-z]+' src",  # a quoted regex has no live wildcard
            "bfs src -name '*.{ts,tsx}'",  # ...nor does a program-side pattern
            'ugrep -r foo "src"/p*',  # a live wildcard, but nothing quoted is a glob character
            "ugrep -r foo 'src'/p*",
            "ugrep -r foo src/" + BS + "p" + BS + "kg*",  # escaped letters are not glob characters
        ],
    )
    def test_ordinary_quoting_is_unaffected(self, project: Project, command: str) -> None:
        self._tree(project)
        assert not _containment.bash_search_outside_root(command, project.root, project.root)

    @pytest.mark.parametrize(
        ("command", "expected_query"),
        [
            ("ugrep -r auth_handler 'src'/p*", "auth handler"),
            ("ugrep -r 'auth_[a-z]+' src", "auth"),
            ("bfs src -name 'auth_handler.{ts,tsx}'", "auth_handler"),
        ],
    )
    def test_end_to_end_ordinary_quoting_still_sends(
        self,
        project: Project,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        command: str,
        expected_query: str,
    ) -> None:
        self._tree(project)
        _, fetch = _run_preload(project, monkeypatch, capsys, _bash(command))
        fetch.assert_called_once()
        assert fetch.call_args.kwargs["query"] == expected_query

    def test_the_scan_uses_the_callers_dialect_never_the_glob_tool_table(
        self, project: Project
    ) -> None:
        """For Bash a bracket is just a character: the part `a[b]x` is the literal name.
        For the Glob tool it is a class matching `abx`, which does not exist."""
        self._tree(project)
        bash = _containment._glob_matches("a[b]x", project.root, dialect=_containment._BASH)
        glob_tool = _containment._glob_matches(
            "a[b]x", project.root, dialect=_containment._GLOB_TOOL
        )
        assert bash == ["a[b]x"]
        assert glob_tool == []


class TestHiddenEntries:
    def _tree(self, project: Project) -> None:
        (project.root / "lib2" / "a").mkdir(parents=True)
        (project.root / "lib2" / ".hid").symlink_to(project.other, target_is_directory=True)

    def test_glob_tool_expansion_includes_dotfiles(self, project: Project) -> None:
        """The Glob tool's engine may match `.hid`, a symlink out."""
        self._tree(project)
        assert not glob_within_root("lib2/*/secret.txt", project.root, base=project.root)

    def test_glob_tool_dotfile_end_to_end_is_skipped(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._tree(project)
        out, fetch = _run_preload(project, monkeypatch, capsys, _glob("lib2/*/secret.txt"))
        _assert_skipped(project, out, fetch)

    def test_bash_expansion_skips_dotfiles_like_bash(self, project: Project) -> None:
        """Bash's `*` never matches a leading dot, so the same word is inside there."""
        self._tree(project)
        assert not _containment.bash_search_outside_root(
            "ugrep -r x lib2/*/secret.txt", project.root, project.root
        )


# A scan dialect that treats `[` as a class and skips dotfiles: Bash semantics, plus the
# class support the parity tests below exercise.
_NO_DOTFILES = _containment._Dialect(wildcards="*?[", double_star_is_star=True, hidden=False)


class TestDirectoryScan:
    """`_glob_matches` is our own scan (glob.iglob has no dotfile switch before 3.11)."""

    PATTERNS_ENDING_IN_A_WILDCARD = [
        "*", "a*", "?", "*/*", "a?/*", "[ab]", "[!a]*", ".*", ".h*", "dir*", "./*", "a/*",
        "a//*", "[ab]*/*", "nomatch*",
    ]

    @pytest.fixture()
    def tree(self, tmp_path: Path) -> Path:
        root = (tmp_path / "scan").resolve()
        for name in (
            "a/x.py", "a/.h", "b/y.py", "b/.hid/z", "c", "a2/x.py", ".dot/q", "dir with space/f",
        ):
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("")
        return root

    @pytest.mark.parametrize("pattern", PATTERNS_ENDING_IN_A_WILDCARD)
    def test_agrees_with_glob_when_hidden_entries_are_excluded(
        self, tree: Path, pattern: str
    ) -> None:
        mine = _containment._glob_matches(pattern, tree, dialect=_NO_DOTFILES)
        assert mine is not None
        expected = sorted(m.rstrip("/") for m in glob.glob(pattern, root_dir=str(tree)))
        assert sorted(m.rstrip("/") for m in mine) == expected

    def test_a_trailing_slash_is_ignored_so_the_result_is_a_superset(self, tree: Path) -> None:
        """`*/` means directories only to a shell; the scan also returns the file `c`.
        Over-checking is the safe direction."""
        mine = {m.rstrip("/") for m in _containment._glob_matches("*/", tree, dialect=_NO_DOTFILES) or []}
        shell = {m.rstrip("/") for m in glob.glob("*/", root_dir=str(tree))}
        assert shell < mine
        assert "c" in mine - shell

    def test_hidden_adds_dotfiles_and_nothing_else(self, tree: Path) -> None:
        plain = _containment._glob_matches("a/*", tree, dialect=_NO_DOTFILES)
        hidden = _containment._glob_matches("a/*", tree, dialect=_containment._GLOB_TOOL)
        assert plain == ["a/x.py"]
        assert hidden == ["a/.h", "a/x.py"]

    def test_a_dot_prefixed_pattern_matches_dotfiles_in_both_modes(self, tree: Path) -> None:
        assert _containment._glob_matches(".d*", tree, dialect=_NO_DOTFILES) == [".dot"]

    def test_absolute_prefix(self, tree: Path) -> None:
        assert _containment._glob_matches(f"{tree}/a*", None, dialect=_NO_DOTFILES) == [
            f"{tree}/a",
            f"{tree}/a2",
        ]

    def test_over_the_cap_at_any_level_is_none(self, tmp_path: Path) -> None:
        base = (tmp_path / "many").resolve()
        _make_dirs(base, 64)
        assert len(_containment._glob_matches("*", base, dialect=_NO_DOTFILES) or []) == 64
        _make_dirs(base.parent / "many2", 65)
        assert _containment._glob_matches("*", base.parent / "many2", dialect=_NO_DOTFILES) is None
        # ...at an intermediate level too, even if the last level would match little.
        assert _containment._glob_matches("*/x", base.parent / "many2", dialect=_NO_DOTFILES) is None

    def test_unreadable_or_missing_directories_match_nothing(self, tmp_path: Path) -> None:
        (tmp_path / "file").write_text("")
        assert _containment._glob_matches("file/*", tmp_path, dialect=_NO_DOTFILES) == []
        assert _containment._glob_matches("nope/*", tmp_path, dialect=_NO_DOTFILES) == []


class TestBraceWorkBound:
    def test_a_huge_single_group_is_rejected_before_any_alternative_is_built(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Counting alternatives AFTER splitting a 5000-way group builds 5000 strings
        first.  The cap must stop it before the first one."""
        calls: list[int] = []
        real = itertools.pairwise
        spy = types.SimpleNamespace(pairwise=lambda items: calls.append(1) or real(items))
        monkeypatch.setattr(_containment, "itertools", spy)
        assert _containment._expand_braces("{" + ",".join(["a"] * 5000) + "}/x") is None
        assert calls == [], "no alternative may be built for a group over the cap"

    def test_the_cap_still_counts_alternatives_already_pending(self) -> None:
        """Two groups of 6 and 6 is 36 results; no single group is over the cap."""
        assert _containment._expand_braces("{1,2,3,4,5,6}{1,2,3,4,5,6}") is None
        assert len(_containment._expand_braces("{1,2,3,4,5,6}{1,2,3,4,5}") or []) == 30

    def test_deeply_nested_single_alternative_groups_are_bounded(self) -> None:
        assert _containment._expand_braces("{" * 200 + "a" + "}" * 200) is None

    def test_ordinary_nesting_still_expands(self) -> None:
        assert sorted(_containment._expand_braces("{a,{b,{c,d}}}") or []) == ["a", "b", "c", "d"]


# ---------------------------------------------------------------------------
# Round 6: a malformed event `cwd` fails closed in every hook
# ---------------------------------------------------------------------------

UNUSABLE_CWD_KINDS = ["symlink_loop", "nul_byte", "missing", "not_a_directory"]


def _unusable_cwd(project: Project, kind: str) -> Path:
    """A `cwd` an event could carry but no real working directory ever is.

    The loop and the missing/file paths sit UNDER the project root on purpose: they
    must be refused because they are unusable, not merely because they are outside.
    """
    root = project.root
    if kind == "symlink_loop":
        (root / "loop_a").symlink_to(root / "loop_b")
        (root / "loop_b").symlink_to(root / "loop_a")
        return root / "loop_a"
    if kind == "nul_byte":
        return Path("bad" + chr(0) + "dir")
    if kind == "missing":
        return root / "does_not_exist"
    (root / "a_file").write_text("")
    return root / "a_file"


class TestSessionCwdFailsClosed:
    @pytest.mark.parametrize("kind", UNUSABLE_CWD_KINDS)
    def test_unusable_cwd_is_none(self, project: Project, kind: str) -> None:
        cwd = _unusable_cwd(project, kind)
        assert _containment.session_cwd({"cwd": str(cwd)}) is None

    def test_a_good_cwd_is_still_resolved(self, project: Project) -> None:
        assert _containment.session_cwd({"cwd": str(project.root / "inner_link")}) == (
            project.root / "src"
        )

    def test_a_deleted_process_cwd_is_none(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """With no `cwd` in the event the process cwd is used; if it was deleted the hook
        cannot know where it is."""
        doomed = tmp_path / "doomed"
        doomed.mkdir()
        monkeypatch.chdir(doomed)
        doomed.rmdir()
        assert _containment.session_cwd({}) is None

    def test_a_symlink_loop_is_caught_on_every_python_version(self, project: Project) -> None:
        """resolve() raises on a loop on older Pythons and silently returns the loop path on
        newer ones (observed on 3.14), where it would pass as 'inside' under the root."""
        loop = _unusable_cwd(project, "symlink_loop")
        try:
            non_strict = loop.resolve()
        except (OSError, RuntimeError):
            pass  # an older Python: resolve() raises, and session_cwd catches it
        else:
            # A newer Python: no error, and the loop path is "inside" -- the case that
            # session_cwd's is_dir() check exists to refuse.
            assert non_strict.is_relative_to(project.root)
        assert _containment.session_cwd({"cwd": str(loop)}) is None

    @pytest.mark.parametrize("tool_name", ["Read", "Write", "Glob", "Grep", "Agent", "WebFetch"])
    def test_tool_check_treats_none_as_outside_for_every_tool(
        self, project: Project, tool_name: str
    ) -> None:
        inside = str(project.root / "src" / "auth_handler.py")
        tool_input = {"file_path": inside, "path": str(project.root / "src"), "pattern": "x"}
        assert _containment.tool_targets_outside_root(tool_name, tool_input, project.root, None)
        # ...whereas the same call with a usable cwd is judged on its target.
        assert not _containment.tool_targets_outside_root(
            "Read", {"file_path": inside}, project.root, project.root
        )

    def test_bash_check_treats_none_as_outside(self, project: Project) -> None:
        assert _containment.bash_search_outside_root("ugrep -r x src", project.root, None)
        assert not _containment.bash_search_outside_root("ugrep -r x src", project.root, project.root)


class TestUnusableCwdPreload:
    EVENTS = {
        "read_inside": _read("{root}/src/auth_handler.py"),
        "grep_inside_path": _grep("def auth_handler", "{root}/src"),
        "bash_ugrep": _bash("ugrep -r auth_handler src"),
    }

    @staticmethod
    def _event(project: Project, name: str) -> dict[str, Any]:
        return json.loads(json.dumps(TestUnusableCwdPreload.EVENTS[name]).replace("{root}", str(project.root)))

    @pytest.mark.parametrize("name", ["read_inside", "grep_inside_path", "bash_ugrep"])
    def test_control_a_usable_cwd_sends(
        self,
        project: Project,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        name: str,
    ) -> None:
        _, fetch = _run_preload(project, monkeypatch, capsys, self._event(project, name))
        fetch.assert_called_once()

    @pytest.mark.parametrize("kind", UNUSABLE_CWD_KINDS)
    @pytest.mark.parametrize("name", ["read_inside", "grep_inside_path", "bash_ugrep"])
    def test_unusable_cwd_is_skipped(
        self,
        project: Project,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        name: str,
        kind: str,
    ) -> None:
        """The targets are inside the project and absolute (or the command is a plain
        search), so only the malformed `cwd` can make the hook skip."""
        out, fetch = _run_preload(
            project,
            monkeypatch,
            capsys,
            self._event(project, name),
            cwd=_unusable_cwd(project, kind),
            process_cwd=project.root,
        )
        _assert_skipped(project, out, fetch)


class TestUnusableCwdCapture:
    @pytest.mark.parametrize("kind", UNUSABLE_CWD_KINDS)
    def test_unusable_cwd_drops_a_read_inside_the_project(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, kind: str
    ) -> None:
        post, sent = _run_capture(
            project,
            monkeypatch,
            _read_event(str(project.root / "src" / "auth_handler.py")),
            cwd=_unusable_cwd(project, kind),
            process_cwd=project.root,
        )
        _assert_dropped(project, post, sent)

    @pytest.mark.parametrize("kind", UNUSABLE_CWD_KINDS)
    def test_unusable_cwd_drops_every_event_even_a_non_path_one(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, kind: str
    ) -> None:
        """A malformed event is not trusted at all, whatever tool it names."""
        post, sent = _run_capture(
            project,
            monkeypatch,
            {"tool_name": "WebFetch", "tool_input": {"url": "https://example.com", "prompt": "x"}},
            cwd=_unusable_cwd(project, kind),
            process_cwd=project.root,
        )
        _assert_dropped(project, post, sent)

    def test_control_a_usable_cwd_captures_the_same_events(
        self, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        post, sent = _run_capture(
            project, monkeypatch, _read_event(str(project.root / "src" / "auth_handler.py"))
        )
        assert sent.wait(timeout=2)
        post.assert_called_once()


class TestUnusableCwdPrefetch:
    """cache_prefetch does not call session_cwd() for its target, so it needs its own guard:
    `within_root` alone resolves a loop under the project to an 'inside' path."""

    @pytest.mark.parametrize("kind", UNUSABLE_CWD_KINDS)
    def test_unusable_cd_target_is_skipped_with_no_io(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, kind: str
    ) -> None:
        cwd = _unusable_cwd(project, kind)
        prefetch, open_db, called = _run_prefetch(
            project, monkeypatch, str(cwd), process_cwd=project.root
        )
        assert not called.wait(timeout=0.3)
        prefetch.assert_not_called()
        open_db.assert_not_called()

    def test_control_a_usable_cd_target_is_prefetched(
        self, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        prefetch, _, called = _run_prefetch(
            project, monkeypatch, str(project.root), process_cwd=project.root
        )
        assert called.wait(timeout=2)
        prefetch.assert_called_once()


# ---------------------------------------------------------------------------
# Round 7: /context gets the file's path RELATIVE to the project root
# ---------------------------------------------------------------------------


class _FakeHttpResponse:
    """Stands in for the object `urllib.request.urlopen` returns."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeHttpResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _run_preload_over_the_wire(
    project: Project,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    event: dict[str, Any],
    *,
    cwd: Path | None = None,
    process_cwd: Path | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run preload_context.main() with the REAL `_fetch_context`; only `urlopen` is faked.

    ``cwd`` is the event's `cwd`; the hook process runs in ``process_cwd`` (default: the same).
    Returns (parsed stdout, the JSON request bodies actually POSTed to /api/v1/context).
    """
    cwd = cwd or project.root
    monkeypatch.chdir(process_cwd or cwd)
    bodies: list[dict[str, Any]] = []

    def fake_urlopen(request: Any, timeout: float | None = None) -> _FakeHttpResponse:
        assert request.full_url.endswith("/api/v1/context")
        bodies.append(json.loads(request.data.decode("utf-8")))
        return _FakeHttpResponse(
            json.dumps({"injection_text": "CTX", "nudge_text": "NUDGE"}).encode("utf-8")
        )

    cfg = _config(project)
    monkeypatch.setattr(_pc_mod._config_mod, "load", lambda: cfg)
    monkeypatch.setattr(_pc_mod.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({**event, "cwd": str(cwd)})))
    _pc_mod.main()
    return json.loads(capsys.readouterr().out), bodies


class TestRootRelativePath:
    """Unit tests of `containment.root_relative_path`."""

    def test_file_at_the_project_root(self, project: Project) -> None:
        (project.root / "x.py").write_text("")
        assert _containment.root_relative_path(str(project.root / "x.py"), project.root) == "x.py"

    def test_nested_file(self, project: Project) -> None:
        nested = project.root / "src" / "a"
        nested.mkdir(parents=True, exist_ok=True)
        (nested / "b.py").write_text("")
        assert (
            _containment.root_relative_path(str(nested / "b.py"), project.root) == "src/a/b.py"
        )

    def test_the_project_root_itself_is_dot(self, project: Project) -> None:
        assert _containment.root_relative_path(str(project.root), project.root) == "."

    def test_outside_or_unresolvable_is_none(self, project: Project) -> None:
        for raw in (
            str(project.other / "secret.py"),
            "/etc/hosts",
            str(project.root / ".." / "other" / "secret.py"),
            str(project.root / "link" / "secret.py"),  # a symlink out
            "~nosuchuser/x",
            "bad" + chr(0) + "path",
        ):
            assert _containment.root_relative_path(raw, project.root) is None, raw

    def test_dotdot_that_returns_inside_is_normalised(self, project: Project) -> None:
        raw = str(project.root / "src" / ".." / "src" / "auth_handler.py")
        assert _containment.root_relative_path(raw, project.root) == "src/auth_handler.py"

    def test_a_symlink_inside_reports_the_real_file_the_code_graph_holds(
        self, project: Project
    ) -> None:
        """codeweaver resolves symlinks before relativising, so the stored path is the target's."""
        raw = str(project.root / "inner_link" / "auth_handler.py")
        assert _containment.root_relative_path(raw, project.root) == "src/auth_handler.py"

    def test_a_relative_input_hangs_off_the_base(self, project: Project) -> None:
        assert (
            _containment.root_relative_path("auth_handler.py", project.root, project.root / "src")
            == "src/auth_handler.py"
        )

    def test_posix_separators_a_backslash_is_an_ordinary_filename_character(
        self, project: Project
    ) -> None:
        """On POSIX `a<backslash>b.py` is ONE file name, so it must not be turned into a
        path separator.  (On Windows the hook gets `as_posix()` separators instead.)"""
        name = "a" + chr(92) + "b.py"
        (project.root / name).write_text("")
        assert _containment.root_relative_path(str(project.root / name), project.root) == name

    def test_windows_style_separators_come_out_as_slashes_via_as_posix(self) -> None:
        """The mechanism the hook relies on for Windows (stdlib behaviour, shown on a pure path)."""
        import pathlib

        relative = pathlib.PureWindowsPath("C:" + chr(92) + "proj" + chr(92) + "src" + chr(92) + "a.py")
        assert relative.relative_to(pathlib.PureWindowsPath("C:" + chr(92) + "proj")).as_posix() == "src/a.py"


class TestPreloadSendsRootRelativePath:
    def _make(self, project: Project, relative: str) -> Path:
        target = project.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("")
        return target

    @pytest.mark.parametrize(
        ("relative", "expected"),
        [("x.py", "x.py"), ("src/a/b.py", "src/a/b.py"), ("src/auth_handler.py", "src/auth_handler.py")],
    )
    def test_the_request_body_carries_the_relative_path(
        self,
        project: Project,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        relative: str,
        expected: str,
    ) -> None:
        target = self._make(project, relative)
        out, bodies = _run_preload_over_the_wire(project, monkeypatch, capsys, _read(str(target)))

        assert bodies == [{"file_path": expected, "format": "inject"}]
        assert out == {"additionalContext": "CTX"}

    def test_the_absolute_project_path_is_nowhere_in_the_request(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = self._make(project, "src/a/b.py")
        _, bodies = _run_preload_over_the_wire(project, monkeypatch, capsys, _read(str(target)))
        assert str(project.root) not in json.dumps(bodies)
        assert not Path(bodies[0]["file_path"]).is_absolute()

    def test_a_session_id_is_still_sent_alongside(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = self._make(project, "src/a/b.py")
        _seed_session(project)
        _, bodies = _run_preload_over_the_wire(project, monkeypatch, capsys, _read(str(target)))
        assert bodies == [{"file_path": "src/a/b.py", "format": "inject", "session_id": SESSION_ID}]

    def test_a_relative_read_path_resolves_against_the_session_cwd(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The event cwd is src/a but the hook process runs at the project root, so `b.py` is
        only `src/a/b.py` if it is resolved against the EVENT cwd."""
        self._make(project, "src/a/b.py")
        _, bodies = _run_preload_over_the_wire(
            project,
            monkeypatch,
            capsys,
            _read("b.py"),
            cwd=project.root / "src" / "a",
            process_cwd=project.root,
        )
        assert bodies == [{"file_path": "src/a/b.py", "format": "inject"}]

    def test_a_symlink_inside_the_project_sends_the_real_file(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, bodies = _run_preload_over_the_wire(
            project, monkeypatch, capsys, _read(str(project.root / "inner_link" / "auth_handler.py"))
        )
        assert bodies == [{"file_path": "src/auth_handler.py", "format": "inject"}]

    def test_reading_the_project_root_itself_sends_nothing(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """There is no file to look up, and the absolute path is never a fallback."""
        out, bodies = _run_preload_over_the_wire(project, monkeypatch, capsys, _read(str(project.root)))
        assert out == {} and bodies == []
        assert ("no_relative_path",) in _query(
            project, "SELECT decision FROM traces WHERE script = 'preload_context'"
        )

    def test_without_a_relative_form_nothing_is_sent_never_the_absolute_path(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Containment passed, but the relative form is unavailable (the file moved or a
        symlink changed in between).  Fail closed: do not fall back to the absolute path."""
        target = self._make(project, "src/a/b.py")
        monkeypatch.setattr(_containment, "root_relative_path", lambda *a, **k: None)
        out, bodies = _run_preload_over_the_wire(project, monkeypatch, capsys, _read(str(target)))
        assert out == {} and bodies == []
        assert ("no_relative_path",) in _query(
            project, "SELECT decision FROM traces WHERE script = 'preload_context'"
        )

    def test_nudge_requests_are_unchanged(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Glob/Grep send a `query`, never a `file_path`."""
        _, bodies = _run_preload_over_the_wire(project, monkeypatch, capsys, _grep("def auth_handler"))
        assert bodies == [{"query": "def auth handler", "format": "nudge"}]
        assert "file_path" not in bodies[0]

    def test_the_cache_key_stays_absolute_and_still_works(
        self, project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The key is local only.  It must still identify the file, and a repeat read must hit it."""
        target = self._make(project, "src/a/b.py")
        _run_preload_over_the_wire(project, monkeypatch, capsys, _read(str(target)))

        expected_key = _pc_mod._cache_key(str(target), str(project.root))
        assert _query(project, "SELECT cache_key FROM cache") == [(expected_key,)]

        out, bodies = _run_preload_over_the_wire(project, monkeypatch, capsys, _read(str(target)))
        assert bodies == []  # served from the cache: no second request
        assert out == {"additionalContext": "CTX"}


class TestRelativePathMatchesTheCodeGraph:
    """The reason for the change: the path the hook now sends matches what is stored.

    The stored path comes from the REAL parser (codeweaver.parse_files, which is what
    code_graph_sync and the seed script run).  The match uses the same SQL the API's
    `search_symbols` builds -- `file_path LIKE :pattern`, with `*` turned into `%` -- run on
    SQLite.  SQLite's LIKE is case-insensitive and has no default escape character, unlike
    PostgreSQL's, so this checks the wildcard rules both share; the PostgreSQL behaviour was
    verified separately against the real `search_symbols` (see the CHANGELOG).
    """

    @staticmethod
    def _match(stored: list[str], pattern: str) -> list[str]:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE TABLE code_symbols (file_path TEXT)")
            conn.executemany("INSERT INTO code_symbols VALUES (?)", [(p,) for p in stored])
            # Exactly what CodeGraphService.search_symbols does to the pattern:
            sql_pattern = pattern.replace("*", "%")
            rows = conn.execute(
                "SELECT file_path FROM code_symbols WHERE file_path LIKE ?", (sql_pattern,)
            ).fetchall()
            return sorted(row[0] for row in rows)
        finally:
            conn.close()

    def test_the_relative_path_matches_what_the_real_parser_stored_and_the_absolute_does_not(
        self, project: Project
    ) -> None:
        codeweaver = pytest.importorskip("codeweaver")
        source = project.root / "src" / "a" / "b.py"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("def hello():" + chr(10) + "    return 1" + chr(10))

        payload = codeweaver.parse_files([source], project.root)
        stored = sorted({symbol["file_path"] for symbol in payload["symbols"]})
        assert stored == ["src/a/b.py"]  # root-relative, as the real parser stores it

        sent_now = _containment.root_relative_path(str(source), project.root)
        sent_before = str(source)
        assert self._match(stored, sent_now or "") == stored
        assert self._match(stored, sent_before) == []  # the bug: it matched nothing

    def test_a_wildcard_character_in_a_name_only_ever_widens_the_match(
        self, project: Project
    ) -> None:
        """`_` is a single-character LIKE wildcard, so `auth_service.py` also matches a file
        that differs only at that position.  Over-matching, never under-matching: the
        file's own row always matches."""
        stored = ["src/auth_service.py", "src/authXservice.py", "src/other.py"]
        assert self._match(stored, "src/auth_service.py") == [
            "src/authXservice.py",
            "src/auth_service.py",
        ]
