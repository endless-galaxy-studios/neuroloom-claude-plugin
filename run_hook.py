"""
Platform-agnostic launcher for neuroloom hook modules.

Usage:
    python run_hook.py <module> [args...]

Locates the .venv Python interpreter at the plugin root and re-execs the
specified module with all remaining arguments forwarded. On POSIX the current
process is replaced via os.execv; on Windows a subprocess is spawned and its
exit code is forwarded.

If the venv does not exist the launcher prints a warning to stderr and exits 0
so that Claude Code hook failures never block the user's session.
"""

import os
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent
VENV = PLUGIN_ROOT / ".venv"


def find_python() -> Path:
    if sys.platform == "win32":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(0)

    venv_py = find_python()
    if not venv_py.exists():
        print(f"[neuroloom] venv not found at {venv_py}", file=sys.stderr)
        sys.exit(0)

    module = sys.argv[1]
    args = [str(venv_py), "-m", module] + sys.argv[2:]

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
            os.execve(str(venv_py), args, env)
        except OSError:
            sys.exit(0)
