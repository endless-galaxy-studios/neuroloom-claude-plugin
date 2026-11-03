"""
Tests for scripts/project_root.py (D178d OQ-4): the root resolver skills and
scripts use, because CLAUDE_PROJECT_DIR is set for hooks only and not in the
Bash tool environment.
"""

import os
import re
import shutil
import subprocess
import sqlite3
import sys
import textwrap
from pathlib import Path

import pytest

import pyhooks  # noqa: F401 -- locates the repo root below
import scripts.project_root as _pr
import scripts.seed_code_graph as _seed_mod

_REPO_ROOT = Path(pyhooks.__file__).parent.parent


def _make_db(directory: Path) -> None:
    (directory / ".neuroloom.db").write_bytes(b"")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point $HOME at an empty directory so the host's home never affects a test."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    return home


class TestResolveProjectRoot:
    def test_env_var_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = tmp_path / "root"
        other = tmp_path / "other"
        root.mkdir()
        other.mkdir()
        _make_db(other)
        monkeypatch.chdir(other)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
        assert _pr.resolve_project_root() == root.resolve()

    def test_nonexistent_env_dir_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_db(tmp_path)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "missing"))
        assert _pr.resolve_project_root() == tmp_path.resolve()

    def test_relative_env_dir_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "rel").mkdir()
        _make_db(tmp_path)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "rel")
        assert _pr.resolve_project_root() == tmp_path.resolve()

    def test_nested_cd_finds_db_in_ancestor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_db(tmp_path)
        sub = tmp_path / "a" / "b"
        sub.mkdir(parents=True)
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.chdir(sub)
        assert _pr.resolve_project_root() == tmp_path.resolve()

    def test_nearest_db_beats_git(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / ".git").mkdir()
        inner = tmp_path / "inner"
        inner.mkdir()
        _make_db(inner)
        sub = inner / "sub"
        sub.mkdir()
        monkeypatch.chdir(sub)
        assert _pr.resolve_project_root() == inner.resolve()

    def test_fresh_project_uses_git_toplevel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / ".git").mkdir()
        sub = tmp_path / "pkg" / "src"
        sub.mkdir(parents=True)
        monkeypatch.chdir(sub)
        assert _pr.resolve_project_root() == tmp_path.resolve()

    def test_git_file_counts_as_git_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / ".git").write_text("gitdir: /elsewhere\n")
        sub = tmp_path / "pkg"
        sub.mkdir()
        monkeypatch.chdir(sub)
        assert _pr.resolve_project_root() == tmp_path.resolve()

    def test_nothing_found_falls_back_to_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Guard against a stray marker in a tmp ancestor of the host.
        for ancestor in (tmp_path, *tmp_path.resolve().parents):
            if (ancestor / ".git").exists() or (ancestor / ".neuroloom.db").exists():
                pytest.skip("host tmp ancestry contains a project marker")
        monkeypatch.chdir(tmp_path)
        assert _pr.resolve_project_root() == tmp_path.resolve()

    def test_hint_is_honoured(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        proj = tmp_path / "proj"
        (proj / "sub").mkdir(parents=True)
        monkeypatch.chdir(proj / "sub")
        assert _pr.resolve_project_root(str(proj)) == proj.resolve()

    def test_hint_beats_nearer_stray_db(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = tmp_path / "proj"
        stray = proj / "packages" / "pkg"
        stray.mkdir(parents=True)
        _make_db(proj)
        _make_db(stray)
        monkeypatch.chdir(stray)
        assert _pr.resolve_project_root() == stray.resolve()  # fallback alone is fooled
        assert _pr.resolve_project_root(str(proj)) == proj.resolve()

    @pytest.mark.parametrize(
        "hint", ["", "${CLAUDE_PROJECT_DIR}", "/definitely/not/here", "relative/dir", "$X${Y}"]
    )
    def test_bad_hint_falls_through_to_walk_up(
        self, hint: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for ancestor in tmp_path.resolve().parents:
            if (ancestor / ".git").exists() or (ancestor / ".neuroloom.db").exists():
                pytest.skip("host tmp ancestry contains a project marker")
        _make_db(tmp_path)
        sub = tmp_path / "sub"
        sub.mkdir()
        monkeypatch.chdir(sub)
        assert _pr.resolve_project_root(hint) == tmp_path.resolve()

    def test_symlink_hint_resolves_to_target(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        assert _pr.resolve_project_root(str(link)) == real.resolve()

    def test_hint_may_be_home(self, _isolated_home: Path) -> None:
        assert _pr.resolve_project_root(str(_isolated_home)) == _isolated_home.resolve()

    def test_hint_beats_env_var(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(b))
        assert _pr.resolve_project_root(str(a)) == a.resolve()

    def test_cli_hint_argument(self, tmp_path: Path) -> None:
        proj = tmp_path / "proj"
        other = tmp_path / "other"
        proj.mkdir()
        other.mkdir()
        script = str(_REPO_ROOT / "scripts" / "project_root.py")
        env = {"PATH": "", "HOME": str(tmp_path / "nohome")}
        good = subprocess.run(
            [sys.executable, "-I", script, str(proj)], cwd=other, capture_output=True, text=True, env=env
        )
        assert good.stdout.strip() == str(proj.resolve())
        literal = subprocess.run(
            [sys.executable, "-I", script, "${CLAUDE_PROJECT_DIR}"],
            cwd=other, capture_output=True, text=True, env=env,
        )
        assert literal.stdout.strip() == str(other.resolve())
        assert "chosen by hint" in good.stderr
        bad = subprocess.run(
            [sys.executable, "-I", script, "relative/x"],
            cwd=other, capture_output=True, text=True, env=env,
        )
        assert "ignoring hint 'relative/x'" in bad.stderr
        assert "ignoring hint" not in literal.stderr
        assert bad.stdout.strip() == str(other.resolve())  # stdout stays path-only

    def test_home_marker_is_not_returned(
        self, _isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_db(_isolated_home)
        (_isolated_home / ".git").mkdir()
        sub = _isolated_home / "work" / "proj"
        sub.mkdir(parents=True)
        monkeypatch.chdir(sub)
        assert _pr.resolve_project_root() == sub.resolve()

    def test_real_project_below_home_still_found(
        self, _isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_db(_isolated_home)
        proj = _isolated_home / "proj"
        (proj / "sub").mkdir(parents=True)
        (proj / ".git").mkdir()
        monkeypatch.chdir(proj / "sub")
        assert _pr.resolve_project_root() == proj.resolve()

    def test_filesystem_root_is_never_a_marker_hit(self) -> None:
        # '/' is excluded even though a marker could in principle exist there.
        assert _pr._first_ancestor_with(Path("/"), "anything", want_file=False) is None

    def test_deleted_cwd_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gone = tmp_path / "gone"
        gone.mkdir()
        monkeypatch.chdir(gone)
        gone.rmdir()
        assert _pr.resolve_project_root() is None

    def test_cli_deleted_cwd_exits_nonzero_with_no_stdout(self, tmp_path: Path) -> None:
        gone = tmp_path / "gone"
        gone.mkdir()
        script = str(_REPO_ROOT / "scripts" / "project_root.py")
        code = (
            "import os, subprocess, sys;"
            f"os.chdir({str(gone)!r}); os.rmdir({str(gone)!r});"
            f"r = subprocess.run([sys.executable, '-I', {script!r}], capture_output=True, text=True);"
            "print(r.returncode, repr(r.stdout), bool(r.stderr))"
        )
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
        assert out.stdout.strip() == "1 '' True"

    def test_require_project_root_exits_when_env_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "")
        with pytest.raises(SystemExit):
            _pr.require_project_root()

    def test_cli_prints_root(self, tmp_path: Path) -> None:
        _make_db(tmp_path)
        sub = tmp_path / "sub"
        sub.mkdir()
        out = subprocess.run(
            [sys.executable, "-I", str(_REPO_ROOT / "scripts" / "project_root.py")],
            cwd=sub,
            capture_output=True,
            text=True,
            check=True,
            env={"PATH": "", "HOME": str(tmp_path / "nohome")},
        )
        assert out.stdout.strip() == str(tmp_path.resolve())


def test_seed_code_graph_defaults_to_resolved_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With --workspace-root omitted, discovery runs against the resolved
    root, not the subdirectory the process happens to be in."""
    import types

    _make_db(tmp_path)
    sub = tmp_path / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)
    seen: list[Path] = []

    def fake_discover(root: Path) -> list[Path]:
        seen.append(Path(root))
        return []

    fake = types.ModuleType("codeweaver")
    fake.discover_files = fake_discover  # type: ignore[attr-defined]
    fake.parse_files = lambda *a, **k: {}  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "codeweaver", fake)
    monkeypatch.setattr(sys, "argv", ["seed_code_graph.py"])

    with pytest.raises(SystemExit):
        _seed_mod.main()

    assert seen == [tmp_path.resolve()]
    assert "no supported files" in capsys.readouterr().out


def test_seed_code_graph_fails_cleanly_without_a_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(_seed_mod, "resolve_project_root", lambda: None)
    monkeypatch.setattr(sys, "argv", ["seed_code_graph.py"])
    with pytest.raises(SystemExit) as exc:
        _seed_mod.main()
    assert exc.value.code == 1
    assert "cannot determine project root" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Skill snippets: behavior and static scans
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^( *)```bash\n(.*?)^\1```", re.DOTALL | re.MULTILINE)
_PY_C_RE = re.compile(r'-c "\n(.*?)\n *"$', re.DOTALL | re.MULTILINE)
_SKILL_FILES = sorted((_REPO_ROOT / "skills").glob("**/SKILL.md"))


def _bash_blocks(path: Path) -> list[str]:
    return [m.group(2) for m in _FENCE_RE.finditer(path.read_text(encoding="utf-8"))]


def _snippets() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for path in _SKILL_FILES:
        for block in _bash_blocks(path):
            for m in _PY_C_RE.finditer(block):
                found.append((path.parent.name, textwrap.dedent(m.group(1))))
    return found


def test_every_skill_python_snippet_compiles() -> None:
    snippets = _snippets()
    openers = sum(
        1
        for path in _SKILL_FILES
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.rstrip().endswith('-c "')
    )
    assert openers > 0
    assert len(snippets) == openers, "a python -c snippet is outside a ```bash fence or unmatched"
    for skill, body in snippets:
        code = body.replace("${CLAUDE_PLUGIN_ROOT}", "/plugin").replace('\\"', '"')
        compile(code, f"<{skill} snippet>", "exec")


def test_every_python_snippet_is_prefixed_with_the_root_resolver() -> None:
    prefix = (
        'CLAUDE_PROJECT_DIR="$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/project_root.py" '
        '"${CLAUDE_PROJECT_DIR}")"'
    )
    for path in _SKILL_FILES:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.rstrip().endswith('python -c "') and "subprocess" not in line:
                assert prefix in line, f"{path.parent.name}: unprefixed snippet: {line.strip()}"


def _run_block(
    block: str,
    cwd: Path,
    extra_env: dict[str, str] | None = None,
    path_prefix: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a SKILL.md bash block as the agent would, from ``cwd``.

    No PYTHONPATH: ``pyhooks`` must import the way the venv provides it (the
    editable install), so a snippet that only works because the repo root was
    injected cannot pass.  ``sys.executable`` stands in for the plugin venv.
    """
    script = block.replace(
        "${CLAUDE_PLUGIN_ROOT}/.venv/bin/python", str(Path(sys.executable))
    )
    env = {
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), os.environ.get("PATH", "")]),
        "CLAUDE_PLUGIN_ROOT": str(_REPO_ROOT),
        "HOME": str(cwd.parent / "nohome"),
        **(extra_env or {}),
    }
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}{os.pathsep}{env['PATH']}"
    return subprocess.run(
        ["bash", "-c", script], cwd=cwd, env=env, capture_output=True, text=True, timeout=60
    )


def _keyed_project(tmp_path: Path) -> tuple[Path, Path]:
    proj = tmp_path / "proj"
    sub = proj / "sub"
    sub.mkdir(parents=True)
    conn = sqlite3.connect(str(proj / ".neuroloom.db"))
    conn.execute("CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO config VALUES ('api_key', 'k-project')")
    conn.commit()
    conn.close()
    return proj, sub


def test_workspace_phase0_key_check_from_subdirectory_sees_the_root_key(tmp_path: Path) -> None:
    """The reviewer's two-roots bug: run the real Phase 0 block from proj/sub."""
    proj, sub = _keyed_project(tmp_path)
    block = next(
        b for b in _bash_blocks(_REPO_ROOT / "skills" / "workspace" / "SKILL.md")
        if "KEY_SET" in b
    )
    result = _run_block(block, sub)
    assert result.stdout.strip() == "KEY_SET", result.stderr
    assert f"project root is {proj.resolve()}" in result.stderr


def test_snippet_aborts_when_resolver_prints_nothing(tmp_path: Path) -> None:
    gone = tmp_path / "gone"
    gone.mkdir()
    block = next(
        b for b in _bash_blocks(_REPO_ROOT / "skills" / "workspace" / "SKILL.md")
        if "KEY_SET" in b
    )
    block = f"cd {gone} && rmdir {gone}\n" + block
    result = _run_block(block, tmp_path)
    assert "KEY_SET" not in result.stdout and "KEY_MISSING" not in result.stdout
    assert "could not determine the project root" in result.stderr


def test_status_shell_steps_use_python3_and_guard_empty_root() -> None:
    text = (_REPO_ROOT / "skills" / "status" / "SKILL.md").read_text(encoding="utf-8")
    sqlite_lines = [ln for ln in text.splitlines() if "sqlite3 " in ln and "NL_ROOT" in ln]
    assert len(sqlite_lines) == 2
    for ln in sqlite_lines:
        assert 'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/project_root.py" "${CLAUDE_PROJECT_DIR}"' in ln
        assert 'sqlite3 -readonly "$NL_ROOT/.neuroloom.db"' in ln
        assert '[ -z "$NL_ROOT" ]' in ln
    assert not [ln for ln in text.splitlines() if "sqlite3 .neuroloom.db" in ln]


_BARE_ROOT_RE = re.compile(
    r"Path\.cwd\(\)|os\.getcwd\(\)|Path\(\s*['\"]\.['\"]\s*\)"
    r"|Path\(\s*['\"]\.neuroloom\.db['\"]\s*\)|\bsqlite3\s+\.neuroloom\.db|\{cwd\}"
)


def test_no_bare_cwd_root_derivation_in_skills_or_scripts() -> None:
    """Fitness scan: skills and scripts derive the root only via the resolver."""
    offenders: dict[str, list[str]] = {}
    files = list(_SKILL_FILES) + [
        p for p in sorted((_REPO_ROOT / "scripts").glob("*.py")) if p.name != "project_root.py"
    ]
    assert files
    for path in files:
        hits = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if _BARE_ROOT_RE.search(line)
        ]
        if hits:
            offenders[str(path.relative_to(_REPO_ROOT))] = hits
    assert not offenders, f"bare cwd-derived root found; use the resolver: {offenders}"


def test_require_project_root_precedes_config_load_in_every_snippet() -> None:
    for skill, body in _snippets():
        if "_config.load(" in body:
            assert "require_project_root(" in body, skill
            assert body.index("require_project_root(") < body.index("_config.load("), (
                f"{skill}: require_project_root() must run before _config.load():\n{body}"
            )


def test_no_backticks_inside_python_snippets() -> None:
    """Bash runs backticks inside a double-quoted -c string as command substitution."""
    for skill, body in _snippets():
        assert "`" not in body, f"{skill}: backtick inside a double-quoted python -c string"


def _init_block(marker: str) -> str:
    return next(
        b for b in _bash_blocks(_REPO_ROOT / "skills" / "init" / "SKILL.md") if marker in b
    )


_SEED_ENV = {"CLAUDE_PLUGIN_OPTION_API_KEY": "seed-key-value"}


def _stored_key(db: Path) -> str | None:
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute("SELECT value FROM config WHERE key = 'api_key'").fetchone()
    finally:
        conn.close()
    return None if row is None else str(row[0])


def test_init_use_default_from_subdirectory_writes_the_root_db_only(tmp_path: Path) -> None:
    """Fresh project (only .git): step 1 says KEY_MISSING, use-default writes proj/.neuroloom.db."""
    proj = tmp_path / "proj"
    sub = proj / "sub"
    sub.mkdir(parents=True)
    (proj / ".git").mkdir()

    step1 = _run_block(_init_block("KEY_SET"), sub, _SEED_ENV)
    assert step1.stdout.strip() == "KEY_MISSING", step1.stderr

    write = _run_block(_init_block("failed:no_seed"), sub, _SEED_ENV)
    assert write.stdout.strip() == "ok", write.stderr
    assert _stored_key(proj / ".neuroloom.db") == "seed-key-value"
    assert not (sub / ".neuroloom.db").exists()


def test_init_step1_from_subdirectory_with_existing_key_is_key_set(tmp_path: Path) -> None:
    proj, sub = _keyed_project(tmp_path)
    step1 = _run_block(_init_block("KEY_SET"), sub, _SEED_ENV)
    assert step1.stdout.strip() == "KEY_SET", step1.stderr
    assert not (sub / ".neuroloom.db").exists()
    assert _stored_key(proj / ".neuroloom.db") == "k-project"


def _status_sqlite_lines() -> list[str]:
    text = (_REPO_ROOT / "skills" / "status" / "SKILL.md").read_text(encoding="utf-8")
    return [ln.strip() for ln in text.splitlines() if "NL_ROOT=" in ln]


def test_status_step_reports_unresolved_root_explicitly(tmp_path: Path) -> None:
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "python3").write_text("#!/bin/sh\nexit 0\n")
    (fake / "python3").chmod(0o755)
    lines = _status_sqlite_lines()
    assert len(lines) == 2
    for line in lines:
        result = _run_block(line, tmp_path, path_prefix=fake)
        assert result.stdout.strip() == "ROOT_UNRESOLVED", result.stderr
        assert "No active session" not in result.stdout


@pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 CLI not installed")
def test_status_steps_are_read_only_and_use_the_root_db(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    sub = proj / "sub"
    sub.mkdir(parents=True)
    (proj / ".git").mkdir()  # fresh project: no database anywhere
    for line in _status_sqlite_lines():
        result = _run_block(line, sub)
        assert result.stdout.strip() in {"No active session", "0"}, result.stderr
    assert not (proj / ".neuroloom.db").exists()
    assert not (sub / ".neuroloom.db").exists()


def test_substituted_project_dir_beats_nearer_stray_db_in_a_real_snippet(tmp_path: Path) -> None:
    """The sleeved/packages scenario: Claude Code substitutes the true root into the
    skill text; a stray .neuroloom.db closer to the cwd must not win."""
    proj, _sub = _keyed_project(tmp_path)
    stray = proj / "packages" / "pkg"
    stray.mkdir(parents=True)
    _make_db(stray)
    block = next(
        b for b in _bash_blocks(_REPO_ROOT / "skills" / "workspace" / "SKILL.md") if "KEY_SET" in b
    )
    # Unsubstituted: the walk-up fallback is fooled by the stray db.
    assert _run_block(block, stray).stdout.strip() == "KEY_MISSING"
    # Substituted, as Claude Code does before the agent sees the text.
    substituted = block.replace("${CLAUDE_PROJECT_DIR}", str(proj))
    result = _run_block(substituted, stray)
    assert result.stdout.strip() == "KEY_SET", result.stderr
    assert f"project root is {proj.resolve()}" in result.stderr


def test_every_project_root_call_passes_the_hint() -> None:
    for path in _SKILL_FILES:
        for line in path.read_text(encoding="utf-8").splitlines():
            idx = line.find('scripts/project_root.py"')
            if idx == -1:
                continue
            tail = line[idx + len('scripts/project_root.py"'):]
            assert tail.startswith(' "${CLAUDE_PROJECT_DIR}"'), (
                f"{path.parent.name}: project_root.py call without the hint: {line.strip()}"
            )


def test_no_python_snippet_contains_the_hint_placeholder() -> None:
    for skill, body in _snippets():
        assert "${CLAUDE_PROJECT_DIR}" not in body, skill


def test_env_var_alone_beats_stray_db_with_placeholder_left_literal(tmp_path: Path) -> None:
    proj, _sub = _keyed_project(tmp_path)
    stray = proj / "packages" / "pkg"
    stray.mkdir(parents=True)
    _make_db(stray)
    block = next(
        b for b in _bash_blocks(_REPO_ROOT / "skills" / "workspace" / "SKILL.md") if "KEY_SET" in b
    )
    result = _run_block(block, stray, {"CLAUDE_PROJECT_DIR": str(proj)})
    assert result.stdout.strip() == "KEY_SET", result.stderr
    # The shell expands the literal placeholder to the env var, so it arrives as the hint.
    assert "chosen by hint" in result.stderr


@pytest.mark.parametrize("dirname", ["my proj", "it's"])
def test_substituted_hint_with_awkward_path(tmp_path: Path, dirname: str) -> None:
    base = tmp_path / dirname
    proj, _sub = _keyed_project(base)
    stray = proj / "packages" / "pkg"
    stray.mkdir(parents=True)
    _make_db(stray)
    block = next(
        b for b in _bash_blocks(_REPO_ROOT / "skills" / "workspace" / "SKILL.md") if "KEY_SET" in b
    )
    result = _run_block(block.replace("${CLAUDE_PROJECT_DIR}", str(proj)), stray)
    assert result.stdout.strip() == "KEY_SET", result.stderr
    assert "chosen by hint" in result.stderr
