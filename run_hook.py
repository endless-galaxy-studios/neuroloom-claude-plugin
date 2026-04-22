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
VENV = PLUGIN_ROOT / ".venv"


def _resolve_python() -> tuple[Path, bool]:
    """Return (interpreter_path, is_degraded)."""
    venv_py = VENV / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if venv_py.exists():
        return venv_py, False
    return Path(sys.executable), True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(0)

    py, degraded = _resolve_python()
    module = sys.argv[1]
    if degraded and module == "pyhooks.session_start":
        print(
            f"[neuroloom] degraded: .venv not found at {VENV} — using system Python",
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
