"""
Platform-agnostic launcher for neuroloom hook modules.

Usage:
    python run_hook.py <module> [args...]

Locates the best available Python interpreter (prefers the plugin's .venv when
present; falls back to sys.executable when not) and re-execs the specified
module with all remaining arguments forwarded. On POSIX the current process is
replaced via os.execve; on Windows a subprocess is spawned and its exit code is
forwarded.

When the venv is absent a single degraded-state warning is written to stderr,
but ONLY when the module being dispatched is pyhooks.session_start. This gates
the message to one occurrence per session and prevents 71x per-hook spam.
"""

import os
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent


def _resolve_python() -> tuple[Path, bool]:
    """Return (interpreter_path, is_degraded).

    Venv resolution order:
    1. ${CLAUDE_PLUGIN_DATA}/.venv  — persistent across plugin version bumps (CC v2.1.78+)
    2. PLUGIN_ROOT / ".venv"        — dev-mode fallback (no CLAUDE_PLUGIN_DATA set)
    3. sys.executable               — degraded, no venv found anywhere

    Note: on first install, CLAUDE_PLUGIN_DATA exists but .venv has not been created
    yet; the venv_py.exists() check handles this by falling through to the next tier.
    """
    suffix = "Scripts/python.exe" if sys.platform == "win32" else "bin/python"

    data_dir = os.environ.get("CLAUDE_PLUGIN_DATA")
    if data_dir:
        venv_py = Path(data_dir) / ".venv" / suffix
        if venv_py.exists():
            return venv_py, False

    venv_py = PLUGIN_ROOT / ".venv" / suffix
    if venv_py.exists():
        return venv_py, False

    return Path(sys.executable), True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(0)

    py, degraded = _resolve_python()
    module = sys.argv[1]
    if degraded and module == "pyhooks.session_start":
        _data_dir = os.environ.get("CLAUDE_PLUGIN_DATA")
        _searched = (
            f"{_data_dir}/.venv, " if _data_dir else ""
        ) + str(PLUGIN_ROOT / ".venv")
        print(
            f"[neuroloom] degraded: .venv not found at {_searched} — using system Python",
            file=sys.stderr,
        )

    args = [str(py), "-m", module] + sys.argv[2:]

    # Add the plugin root to PYTHONPATH so `python -m pyhooks.*` resolves
    # without needing pyhooks installed as a package in the venv.
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(PLUGIN_ROOT) + (os.pathsep + existing if existing else "")

    if sys.platform == "win32":
        import subprocess

        result = subprocess.run(args, env=env)
        sys.exit(result.returncode or 0)
    else:
        try:
            os.execve(str(py), args, env)
        except OSError:
            sys.exit(0)
