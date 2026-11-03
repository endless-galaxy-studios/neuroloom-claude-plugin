"""
Tests for scripts/skill_python.py (D178d): the interpreter launcher skills use so
their ``python -c`` snippets resolve the interpreter and PYTHONPATH the way
``run_hook.py`` does, in a dev clone and in an installed copy alike.
"""

import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import pyhooks  # noqa: F401 -- locates the repo root below
import scripts.skill_python as _sp

_REPO_ROOT = Path(pyhooks.__file__).parent.parent
_SKILL_FILES = sorted((_REPO_ROOT / "skills").glob("**/SKILL.md"))
_PY_SUFFIX = "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
_LAUNCHER_PREFIX = (
    'CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" '
    'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/skill_python.py" -c "'
)

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="uses shell-script interpreters"
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "NEUROLOOM_MODE",
        "CLAUDE_PLUGIN_DATA",
        "PYTHONPATH",
        "CLAUDE_PROJECT_DIR",
    ):
        monkeypatch.delenv(name, raising=False)


def _fake_plugin(base: Path, *, with_launcher: bool = False) -> Path:
    """A minimal plugin directory: run_hook.py (the resolver source of truth) only."""
    root = base
    root.mkdir(parents=True, exist_ok=True)
    shutil.copy(_REPO_ROOT / "run_hook.py", root / "run_hook.py")
    if with_launcher:
        (root / "scripts").mkdir()
        shutil.copy(
            _REPO_ROOT / "scripts" / "skill_python.py",
            root / "scripts" / "skill_python.py",
        )
    return root


def _venv_python(venv_parent: Path, body: str | None = None) -> Path:
    """Create ``<venv_parent>/.venv/bin/python``; a shell script when ``body`` is given."""
    py = venv_parent / ".venv" / _PY_SUFFIX
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text(body if body is not None else "")
    py.chmod(py.stat().st_mode | stat.S_IXUSR)
    return py


# ---------------------------------------------------------------------------
# build_command: resolution order, PYTHONPATH, CI mode
# ---------------------------------------------------------------------------


def test_data_venv_preferred_over_plugin_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_plugin(tmp_path / "plugin")
    _venv_python(root)
    data = _venv_python(tmp_path / "data")
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))
    py, args, env, degraded = _sp.build_command(["-c", "pass"], root)
    assert py == data and args[0] == str(data) and not degraded
    assert env["CLAUDE_PLUGIN_DATA"] == str(tmp_path / "data")


def test_plugin_venv_used_when_no_data_dir(tmp_path: Path) -> None:
    root = _fake_plugin(tmp_path / "plugin")
    venv_py = _venv_python(root)
    py, _args, _env, degraded = _sp.build_command(["-c", "pass"], root)
    assert py == venv_py and not degraded


def test_data_dir_without_venv_falls_through_to_plugin_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_plugin(tmp_path / "plugin")
    venv_py = _venv_python(root)
    (tmp_path / "data").mkdir()
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))
    assert _sp.build_command([], root)[0] == venv_py


def test_sys_executable_when_no_venv_anywhere(tmp_path: Path) -> None:
    root = _fake_plugin(tmp_path / "plugin")
    py, args, _env, degraded = _sp.build_command(["-c", "pass"], root)
    assert py == Path(sys.executable) and degraded
    assert args == [sys.executable, "-c", "pass"]


def test_pythonpath_is_plugin_root_then_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_plugin(tmp_path / "plugin")
    assert _sp.build_command([], root)[2]["PYTHONPATH"] == str(root)
    monkeypatch.setenv("PYTHONPATH", "/already/there")
    assert (
        _sp.build_command([], root)[2]["PYTHONPATH"]
        == str(root) + os.pathsep + "/already/there"
    )


def test_ci_mode_uses_sys_executable_isolated_and_ignores_venvs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_plugin(tmp_path / "plugin")
    _venv_python(root)
    _venv_python(tmp_path / "data")
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("NEUROLOOM_MODE", "ci")
    py, args, env, degraded = _sp.build_command(["-c", "print(1)", "x"], root)
    assert py == Path(sys.executable) and not degraded
    assert args[:3] == [sys.executable, "-I", "-c"]
    assert args[4:] == [str(root), "-c", "print(1)", "x"]
    assert env["PYTHONPATH"] == str(root)


def test_build_command_leaves_process_environment_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_plugin(tmp_path / "plugin")
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", "${CLAUDE_PLUGIN_DATA}")
    before = dict(os.environ)
    _sp.build_command([], root)
    assert dict(os.environ) == before


@pytest.mark.parametrize("value", ["", "${CLAUDE_PLUGIN_DATA}", "relative/dir"])
def test_unusable_data_dir_is_treated_as_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    root = _fake_plugin(tmp_path / "plugin")
    venv_py = _venv_python(root)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", value)
    py, _args, env, _deg = _sp.build_command([], root)
    assert py == venv_py
    assert "CLAUDE_PLUGIN_DATA" not in env


def test_data_dir_derived_from_installed_layout(tmp_path: Path) -> None:
    root = _fake_plugin(
        tmp_path / "plugins" / "cache" / "market-place" / "neuroloom" / "0.9.1"
    )
    data = _venv_python(tmp_path / "plugins" / "data" / "neuroloom-market-place")
    py, _args, env, degraded = _sp.build_command(["-c", "pass"], root)
    assert py == data and not degraded
    assert env["CLAUDE_PLUGIN_DATA"] == str(
        tmp_path / "plugins" / "data" / "neuroloom-market-place"
    )


def test_explicit_data_dir_beats_derived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_plugin(tmp_path / "plugins" / "cache" / "m" / "p" / "1")
    _venv_python(tmp_path / "plugins" / "data" / "p-m")
    explicit = _venv_python(tmp_path / "elsewhere")
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "elsewhere"))
    assert _sp.build_command([], root)[0] == explicit


def test_no_derivation_unless_the_cache_dir_sits_under_plugins() -> None:
    assert _sp._derived_data_dir(Path("/h/other/cache/m/p/1")) is None
    assert (
        _sp._derived_data_dir(Path("/h/plugins/cache/m/p/1")) == "/h/plugins/data/p-m"
    )


def test_no_derivation_for_a_dev_clone_layout(tmp_path: Path) -> None:
    assert _sp._derived_data_dir(tmp_path / "Projects" / "neuroloom" / "plugin") is None
    assert _sp._derived_data_dir(Path("/x")) is None


def test_main_without_arguments_is_a_usage_error() -> None:
    assert _sp.main(["skill_python.py"]) == 2


# ---------------------------------------------------------------------------
# Real exec: args and exit code forwarded
# ---------------------------------------------------------------------------


@posix_only
def test_args_exit_code_and_pythonpath_forwarded_through_exec(tmp_path: Path) -> None:
    root = _fake_plugin(tmp_path / "plugin", with_launcher=True)
    _venv_python(
        tmp_path / "data",
        '#!/bin/sh\nfor a in "$@"; do echo "ARG:$a"; done\necho "PP:$PYTHONPATH"\nexit 7\n',
    )
    env = {
        "PATH": os.environ["PATH"],
        "CLAUDE_PLUGIN_DATA": str(tmp_path / "data"),
        "PYTHONPATH": "/old",
    }
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "skill_python.py"),
            "-c",
            "a b",
            "two words",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 7, result.stderr
    assert result.stdout.splitlines() == [
        "ARG:-c",
        "ARG:a b",
        "ARG:two words",
        f"PP:{root}{os.pathsep}/old",
    ]


@posix_only
def test_ci_mode_exec_runs_snippet_with_isolated_path_and_forwards_exit(
    tmp_path: Path,
) -> None:
    root = _fake_plugin(tmp_path / "plugin", with_launcher=True)
    (root / "pyhooks").mkdir()
    (root / "pyhooks" / "__init__.py").write_text("MARK = 'real'\n")
    hostile = tmp_path / "checkout"
    hostile.mkdir()
    (hostile / "pyhooks").mkdir()
    (hostile / "pyhooks" / "__init__.py").write_text("MARK = 'planted'\n")
    planted = tmp_path / "planted-stdlib-module-was-imported"
    for name in ("json", "runpy"):
        (hostile / f"{name}.py").write_text(
            f"open({str(planted)!r}, 'w').write('{name}')\n"
        )
    env = {"PATH": os.environ["PATH"], "NEUROLOOM_MODE": "ci"}
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "skill_python.py"),
            "-c",
            "import json, sys, pyhooks; print(pyhooks.MARK, sys.argv[1:]); sys.exit(3)",
            "arg1",
        ],
        cwd=hostile,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 3, result.stderr
    assert result.stdout.strip() == "real ['arg1']"
    assert not planted.exists(), planted.read_text()


@posix_only
def test_missing_interpreter_reports_and_fails(tmp_path: Path) -> None:
    root = _fake_plugin(tmp_path / "plugin", with_launcher=True)
    py = _venv_python(tmp_path / "data", "#!/nonexistent/interp\n")
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "skill_python.py"), "-c", "pass"],
        env={"PATH": os.environ["PATH"], "CLAUDE_PLUGIN_DATA": str(tmp_path / "data")},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert py.exists()


# ---------------------------------------------------------------------------
# Installed-copy layout: a real skill snippet imports pyhooks without any plugin .venv
# ---------------------------------------------------------------------------


def _install_copy(base: Path) -> tuple[Path, Path, Path]:
    """Copy the plugin (no .venv) to a cache-style path; add a data venv whose
    interpreter records its own path, then delegates to the test interpreter without
    site-packages."""
    plugins = base / "plugins"
    installed = plugins / "cache" / "endless-galaxy-studios" / "neuroloom" / "9.9.9"
    installed.mkdir(parents=True)
    for name in ("pyhooks", "scripts", "skills", "run_hook.py"):
        src = _REPO_ROOT / name
        if src.is_dir():
            shutil.copytree(
                src, installed / name, ignore=shutil.ignore_patterns("__pycache__")
            )
        else:
            shutil.copy(src, installed / name)
    assert not (installed / ".venv").exists()
    data = plugins / "data" / "neuroloom-endless-galaxy-studios"
    marker = base / "data-venv-was-used"
    # -S skips site-packages and .pth files, so the dev checkout's editable install of
    # pyhooks cannot satisfy an import: only the launcher's PYTHONPATH can.
    _venv_python(
        data,
        f'#!/bin/sh\necho "$0" > "{marker}"\nexec "{sys.executable}" -S "$@"\n',
    )
    return installed, data, marker


def _bash_blocks(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [
        m.group(2)
        for m in re.finditer(
            r"^( *)```bash\n(.*?)^\1```", text, re.DOTALL | re.MULTILINE
        )
    ]


def _workspace_phase0(installed: Path) -> str:
    text = (installed / "skills" / "workspace" / "SKILL.md").read_text(encoding="utf-8")
    start = text.index("```bash\n", text.index("Phase 0")) + len("```bash\n")
    return text[start : text.index("```", start)]


def _keyed_project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    conn = sqlite3.connect(str(proj / ".neuroloom.db"))
    conn.execute("CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO config VALUES ('api_key', 'k-project')")
    conn.commit()
    conn.close()
    return proj


@posix_only
@pytest.mark.parametrize("substituted", [True, False], ids=["substituted", "derived"])
def test_installed_copy_phase0_key_check_imports_pyhooks(
    tmp_path: Path, substituted: bool
) -> None:
    installed, data, marker = _install_copy(tmp_path)
    proj = _keyed_project(tmp_path)
    block = _workspace_phase0(installed)
    assert "KEY_SET" in block and "skill_python.py" in block
    if substituted:
        # Claude Code substitutes the data dir into skill text; the env is unset.
        block = block.replace("${CLAUDE_PLUGIN_DATA}", str(data))
    env = {
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), "/usr/bin", "/bin"]),
        "CLAUDE_PLUGIN_ROOT": str(installed),
        "HOME": str(tmp_path / "nohome"),
    }
    result = subprocess.run(
        ["bash", "-c", block],
        cwd=proj,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.stdout.strip() == "KEY_SET", result.stderr
    assert marker.read_text().strip() == str(_data_python(data)), (
        "the data-dir interpreter was not the one launched"
    )


def _data_python(data: Path) -> Path:
    return data / ".venv" / _PY_SUFFIX


@posix_only
@pytest.mark.parametrize("substituted", [True, False], ids=["substituted", "derived"])
def test_installed_copy_pyhooks_comes_from_the_installed_copy(
    tmp_path: Path, substituted: bool
) -> None:
    installed, data, marker = _install_copy(tmp_path)
    prefix = f'CLAUDE_PLUGIN_DATA="{data if substituted else ""}" '
    block = (
        prefix
        + 'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/skill_python.py" -c '
        + "'import sys, pyhooks; print(pyhooks.__file__); print(sys.flags.no_site)'"
    )
    env = {
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), "/usr/bin", "/bin"]),
        "CLAUDE_PLUGIN_ROOT": str(installed),
        "HOME": str(tmp_path / "nohome"),
    }
    result = subprocess.run(
        ["bash", "-c", block],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert Path(lines[0]).resolve().is_relative_to(installed.resolve()), lines
    assert lines[1] == "1", (
        "site-packages was not disabled; the test would not prove PYTHONPATH"
    )
    assert marker.read_text().strip() == str(_data_python(data))


@posix_only
def test_installed_copy_init_seed_wrapper_child_inherits_launcher_environment(
    tmp_path: Path,
) -> None:
    installed, data, marker = _install_copy(tmp_path)
    proj = _keyed_project(tmp_path)
    block = next(
        b
        for b in _bash_blocks(installed / "skills" / "init" / "SKILL.md")
        if "--workspace-root" in b
    )
    assert "--workspace-root" in block
    # Replace only the script path so no network/seed work runs.  The wrapper
    # itself runs under the -S shim, so its own `from pyhooks.config import ...` only works
    # through the launcher's PYTHONPATH.  The child (plain sys.executable, so site is on)
    # inherits that PYTHONPATH, which sorts ahead of the dev checkout's editable install.
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import sys, pyhooks; print('seed-pyhooks', pyhooks.__file__, sys.flags.no_site)\n"
    )
    block = block.replace(
        "${CLAUDE_PLUGIN_ROOT}/scripts/seed_code_graph.py", str(probe)
    )
    env = {
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), "/usr/bin", "/bin"]),
        "CLAUDE_PLUGIN_ROOT": str(installed),
        "HOME": str(tmp_path / "nohome"),
    }
    result = subprocess.run(
        ["bash", "-c", block.replace("${CLAUDE_PLUGIN_DATA}", str(data))],
        cwd=proj,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    line = next(
        ln for ln in result.stdout.splitlines() if ln.startswith("seed-pyhooks")
    )
    _tag, pyhooks_file, _no_site = line.split()
    assert Path(pyhooks_file).resolve().is_relative_to(installed.resolve())
    assert marker.read_text().strip() == str(_data_python(data))


# ---------------------------------------------------------------------------
# Static pins
# ---------------------------------------------------------------------------


def test_no_skill_references_a_dev_clone_venv_interpreter() -> None:
    assert _SKILL_FILES
    for path in _SKILL_FILES:
        assert ".venv/bin/python" not in path.read_text(encoding="utf-8"), path


def test_every_python_c_snippet_goes_through_the_launcher() -> None:
    seen = 0
    for path in _SKILL_FILES:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.rstrip().endswith('-c "'):
                seen += 1
                assert _LAUNCHER_PREFIX in line, f"{path.parent.name}: {line.strip()}"
                # root resolver first, then the launcher, on one command line
                assert line.index("project_root.py") < line.index("skill_python.py")
    assert seen == 20


def test_launcher_is_the_only_interpreter_chooser_in_skills() -> None:
    for path in _SKILL_FILES:
        text = path.read_text(encoding="utf-8")
        assert "bin/python" not in text, path
    seed = (_REPO_ROOT / "skills" / "init" / "SKILL.md").read_text(encoding="utf-8")
    assert (
        "            sys.executable,\n            '${CLAUDE_PLUGIN_ROOT}/scripts/seed_code_graph.py',"
        in seed
    )
