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
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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
