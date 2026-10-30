"""
Unit tests for run_hook.py — Phase 1 (D130).

Import strategy: importlib.util.spec_from_file_location loads run_hook as a
module without executing __main__, so _resolve_python() can be tested in
isolation without triggering os.execve (which would replace the test-runner
process on POSIX).

The subprocess smoke test (test_argv_guard_exits_zero) is the only test that
invokes the launcher as __main__; it exercises only the argv-guard path, which
exits before any execve/subprocess.run call is made.

For the degraded-stderr gate tests we re-execute the launcher source with
__name__ == "__main__" in-process. Rather than patching _resolve_python (which
gets overwritten when exec_module re-runs the file's top-level definitions),
we control Path.exists so the real _resolve_python returns the desired
(path, degraded) tuple. os.execve and subprocess.run are mocked to prevent the
dispatch from firing.
"""

import importlib.util
import io
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests import ci_support

# ---------------------------------------------------------------------------
# Module loader — loads run_hook without running __main__
# ---------------------------------------------------------------------------

_LAUNCHER = Path(__file__).resolve().parent.parent / "run_hook.py"


def _load_run_hook() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("run_hook", _LAUNCHER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------------------------------------------------------------------------
# _resolve_python() unit tests
# ---------------------------------------------------------------------------


def test_venv_present_returns_venv_path() -> None:
    """_resolve_python() returns the venv interpreter and degraded=False when the
    venv binary exists."""
    mod = _load_run_hook()
    expected_suffix = "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    venv_py = mod.VENV / expected_suffix  # type: ignore[attr-defined]

    with patch.object(Path, "exists", lambda self: self == venv_py):
        result_path, degraded = mod._resolve_python()  # type: ignore[attr-defined]

    assert result_path == venv_py
    assert degraded is False


def test_venv_absent_posix_falls_back_to_sys_executable() -> None:
    """When the venv binary does not exist on POSIX, _resolve_python() falls
    back to sys.executable with degraded=True."""
    mod = _load_run_hook()

    with patch("sys.platform", "linux"), patch.object(Path, "exists", return_value=False):
        result_path, degraded = mod._resolve_python()  # type: ignore[attr-defined]

    assert result_path == Path(sys.executable)
    assert degraded is True


def test_venv_absent_windows_path_shape() -> None:
    """On Windows (patched), venv binary absence falls back to sys.executable
    with degraded=True, and the venv path uses the Scripts/python.exe shape."""
    mod = _load_run_hook()

    with patch("sys.platform", "win32"), patch.object(Path, "exists", return_value=False):
        result_path, degraded = mod._resolve_python()  # type: ignore[attr-defined]

    assert result_path == Path(sys.executable)
    assert degraded is True


# ---------------------------------------------------------------------------
# Degraded-state stderr gate tests
#
# We re-execute the launcher source with __name__ == "__main__" in-process.
# Path.exists is patched to make venv_py absent (so _resolve_python returns
# degraded=True). os.execve / subprocess.run are mocked so the dispatch never
# fires.
# ---------------------------------------------------------------------------


def _run_main(argv: list[str], *, venv_exists: bool) -> str:
    """Execute the __main__ block of run_hook with controlled argv and venv
    presence, capturing stderr. Returns text written to stderr."""
    fake_stderr = io.StringIO()

    spec = importlib.util.spec_from_file_location(
        "__main__", _LAUNCHER, submodule_search_locations=[]
    )
    assert spec is not None and spec.loader is not None
    exec_mod = importlib.util.module_from_spec(spec)

    class _Exit(Exception):
        pass

    def _fake_exit(code: int = 0) -> None:
        raise _Exit(code)

    # Path.exists controls whether _resolve_python sees the venv binary as
    # present. We cannot patch _resolve_python directly because exec_module
    # re-runs the file's top-level code and overwrites any pre-assignment.
    with (
        patch("sys.argv", argv),
        patch("sys.stderr", fake_stderr),
        patch.object(Path, "exists", return_value=venv_exists),
        patch("os.execve", MagicMock()),
        patch("subprocess.run", MagicMock(return_value=MagicMock(returncode=0))),
        patch("sys.exit", _fake_exit),
    ):
        try:
            spec.loader.exec_module(exec_mod)  # type: ignore[union-attr]
        except _Exit:
            pass

    return fake_stderr.getvalue()


def test_degraded_stderr_fires_only_for_session_start() -> None:
    """When the venv is absent and module is pyhooks.session_start, stderr
    contains the degraded warning."""
    stderr_text = _run_main(
        argv=["run_hook.py", "pyhooks.session_start"],
        venv_exists=False,
    )
    assert "degraded" in stderr_text


def test_degraded_stderr_silent_for_other_modules() -> None:
    """When the venv is absent but module is not pyhooks.session_start, stderr
    is empty — no per-hook spam."""
    stderr_text = _run_main(
        argv=["run_hook.py", "pyhooks.post_tool_use"],
        venv_exists=False,
    )
    assert stderr_text == ""


# ---------------------------------------------------------------------------
# Subprocess smoke test — argv guard path only
# ---------------------------------------------------------------------------


def test_argv_guard_exits_zero() -> None:
    """Invoking run_hook.py with no arguments must exit 0 (argv guard path).
    This smoke test uses a subprocess so __main__ executes for real; the
    argv guard fires before any os.execve / subprocess.run call is reached."""
    import subprocess

    result = subprocess.run(
        [sys.executable, str(_LAUNCHER)],
        capture_output=True,
    )
    assert result.returncode == 0


# ---------------------------------------------------------------------------
# D178d FR-D7 / FR-A2: CI mode in the launcher
#
# The launcher cannot import pyhooks before it decides which interpreter to use,
# so it carries its own tiny copy of the NEUROLOOM_MODE parser.  Its name is not
# pinned by the plan, so these tests pin behaviour instead: which interpreter
# the launcher hands to os.execve.  The os.execve stand-in records only the
# interpreter path; it never keeps the environment it is given.
# ---------------------------------------------------------------------------

# Spec 6.2, the shared mode-parse table: (NEUROLOOM_MODE, CI mode on?).
# None means the variable is unset.
_MODE_TABLE: list[tuple[str | None, bool]] = [
    (None, False),
    ("", False),
    ("   ", False),
    ("ci", True),
    ("CI", True),
    (" ci ", True),
    ("prod", True),
    ("ci2", True),
    ("cii", True),
    ("off", True),
    ("false", True),
]


def _launch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    module: str,
    venv_exists: bool,
    mode: str | None,
) -> tuple[str, list[str]]:
    """Run the launcher's ``__main__`` block; return (stderr, interpreters passed to execve)."""
    if mode is None:
        monkeypatch.delenv("NEUROLOOM_MODE", raising=False)
    else:
        monkeypatch.setenv("NEUROLOOM_MODE", mode)
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)

    fake_stderr = io.StringIO()
    chosen: list[str] = []

    class _Exit(Exception):
        pass

    def _record_execve(path: str, args: list[str], env: dict[str, str]) -> None:
        chosen.append(str(path))
        raise _Exit()

    def _fake_exit(code: int = 0) -> None:
        raise _Exit()

    spec = importlib.util.spec_from_file_location("__main__", _LAUNCHER, submodule_search_locations=[])
    assert spec is not None and spec.loader is not None
    exec_mod = importlib.util.module_from_spec(spec)
    with (
        patch("sys.argv", ["run_hook.py", module]),
        patch("sys.stderr", fake_stderr),
        patch.object(Path, "exists", return_value=venv_exists),
        patch("os.execve", _record_execve),
        patch("subprocess.run", MagicMock(return_value=MagicMock(returncode=0))),
        patch("sys.exit", _fake_exit),
    ):
        try:
            spec.loader.exec_module(exec_mod)  # type: ignore[union-attr]
        except _Exit:
            pass
    return fake_stderr.getvalue(), chosen


@pytest.mark.parametrize(("raw", "ci"), _MODE_TABLE)
def test_launcher_mode_table_picks_interpreter(
    raw: str | None, ci: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec 6.2: CI mode (any of the eleven table inputs that turn it on) uses
    sys.executable even when a venv exists; unset or blank keeps the venv search."""
    _stderr, chosen = _launch(monkeypatch, module="pyhooks.capture", venv_exists=True, mode=raw)
    venv_py = _LAUNCHER.parent / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    expected = sys.executable if ci else str(venv_py)
    assert chosen == [expected], f"launcher mode table: NEUROLOOM_MODE={raw!r} chose the wrong interpreter"


@pytest.mark.parametrize("raw", ["ci", "CI", " ci ", "prod", "false"])
def test_launcher_ci_mode_prints_no_degraded_line(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-D7: with no venv anywhere, CI mode still runs sys.executable and stays silent."""
    stderr_text, chosen = _launch(
        monkeypatch, module="pyhooks.session_start", venv_exists=False, mode=raw
    )
    assert chosen == [sys.executable], f"launcher CI {raw!r}: must hand sys.executable to execve"
    assert "degraded" not in stderr_text, f"launcher CI {raw!r}: printed a degraded-venv line"


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_launcher_normal_mode_keeps_degraded_line(raw: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Outside CI mode the venv search and the one degraded line are unchanged."""
    stderr_text, chosen = _launch(
        monkeypatch, module="pyhooks.session_start", venv_exists=False, mode=raw
    )
    assert chosen == [sys.executable], f"launcher normal {raw!r}: falls back to sys.executable"
    assert "degraded" in stderr_text, f"launcher normal {raw!r}: the degraded line must still print"


# ---------------------------------------------------------------------------
# D178d (security): in CI mode the checkout is the working directory and is
# attacker-controllable.  ``python -m`` would put it first on sys.path, so a
# planted pyhooks/, json.py or runpy.py would be imported (and run) instead of
# the trusted modules.  The CI launcher therefore runs ``python -I -c BOOTSTRAP``.
# These tests use a COPY of the launcher in a temporary plugin root with a stub
# ``pyhooks`` package, so nothing real is imported and no hook logic is involved.
# ---------------------------------------------------------------------------


_SHADOW_CANDIDATES = ("current", "py310", "/usr/local/bin/python3")


def _shadow_python(candidate: str) -> str:
    """Resolve a parametrized candidate to an interpreter path, skipping when absent
    or when it is the same interpreter as the current one (already covered)."""
    import os

    if candidate == "current":
        return sys.executable
    if candidate == "py310":
        candidate = ci_support.require_py310()
    if not os.path.exists(candidate):
        pytest.skip(f"{os.path.basename(candidate)} not installed")
    if os.path.realpath(candidate) == os.path.realpath(sys.executable):
        pytest.skip(f"{candidate} is the interpreter already under test")
    return candidate


def _shadow_layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Return (plugin_root, checkout, markers) with the stub package and planted files."""
    import shutil

    plugin = tmp_path / "plugin"
    (plugin / "pyhooks").mkdir(parents=True)
    shutil.copy(_LAUNCHER, plugin / "run_hook.py")
    markers = tmp_path / "markers"
    markers.mkdir()
    (plugin / "pyhooks" / "__init__.py").write_text("", encoding="utf-8")
    (plugin / "pyhooks" / "probe.py").write_text(
        "import json, runpy, os, sys\n"
        "from pathlib import Path\n"
        "import pyhooks\n"
        "if __name__ == '__main__':\n"
        f"    Path({str(markers / 'main')!r}).write_text('ran')\n"
        f"    Path({str(markers / 'pythonpath')!r}).write_text(os.environ.get('PYTHONPATH', '<unset>'))\n"
        f"    Path({str(markers / 'argv')!r}).write_text(repr(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    for name in ("json.py", "runpy.py", "pyhooks/__init__.py"):
        target = checkout / name
        target.parent.mkdir(parents=True, exist_ok=True)
        label = "planted-" + name.replace("/", "-")
        target.write_text(
            "from pathlib import Path\n"
            f"Path({str(markers / label)!r}).write_text('ran')\n",
            encoding="utf-8",
        )
    return plugin, checkout, markers


def _run_shadow(
    python: str, plugin: Path, checkout: Path, mode: str, extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    import os

    env = {"PATH": os.environ.get("PATH", ""), "NEUROLOOM_MODE": mode, "HOME": str(checkout)}
    env.update(extra or {})
    return subprocess.run(
        [python, str(plugin / "run_hook.py"), "pyhooks.probe", "alpha", "beta"],
        cwd=str(checkout),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize("candidate", _SHADOW_CANDIDATES)
def test_ci_launcher_never_imports_planted_modules_from_cwd(candidate: str, tmp_path: Path) -> None:
    python = _shadow_python(candidate)
    plugin, checkout, markers = _shadow_layout(tmp_path)
    done = _run_shadow(python, plugin, checkout, "ci", {"PYTHONPATH": str(checkout)})
    names = sorted(p.name for p in markers.iterdir())
    # Positive control first: the trusted module ran as __main__ with its argv.
    assert "main" in names, f"CI launcher: the trusted hook did not run (rc={done.returncode})"
    assert (markers / "argv").read_text() == "['alpha', 'beta']", "CI launcher: hook args were not forwarded"
    assert names == ["argv", "main", "pythonpath"], f"CI launcher: planted module(s) ran: {names}"
    assert done.returncode == 0, "CI launcher: nonzero exit code"


@pytest.mark.parametrize("candidate", _SHADOW_CANDIDATES)
def test_ci_launcher_child_pythonpath_is_exactly_the_plugin_root(candidate: str, tmp_path: Path) -> None:
    python = _shadow_python(candidate)
    plugin, checkout, markers = _shadow_layout(tmp_path)
    _run_shadow(python, plugin, checkout, "ci", {"PYTHONPATH": str(checkout)})
    assert (markers / "pythonpath").read_text() == str(plugin.resolve()), (
        "CI launcher: child PYTHONPATH must be exactly the plugin root"
    )


def test_normal_launcher_still_uses_dash_m(tmp_path: Path) -> None:
    """Non-CI is unchanged: python -m with the plugin root first on PYTHONPATH."""
    plugin, checkout, markers = _shadow_layout(tmp_path)
    for planted in ("json.py", "runpy.py", "pyhooks"):  # -m is unprotected by design outside CI
        target = checkout / planted
        if target.is_dir():
            import shutil

            shutil.rmtree(target)
        else:
            target.unlink()
    done = _run_shadow(sys.executable, plugin, checkout, "", {"PYTHONPATH": "/nonexistent-x"})
    assert (markers / "main").exists(), f"normal launcher: hook did not run (rc={done.returncode})"
    assert (markers / "pythonpath").read_text() == str(plugin.resolve()) + __import__("os").pathsep + "/nonexistent-x", (
        "normal launcher: PYTHONPATH must be the plugin root followed by the inherited value"
    )
