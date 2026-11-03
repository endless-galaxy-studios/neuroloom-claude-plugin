#!/usr/bin/env python3
"""
Interpreter launcher for skill snippets (D178d).

Skills run ``python -c "..."`` snippets that import ``pyhooks``.  They used to
call ``${CLAUDE_PLUGIN_ROOT}/.venv/bin/python``, a path that exists only in the
development clone.  An installed copy keeps its virtualenv in the persistent
plugin-data directory (``${CLAUDE_PLUGIN_DATA}/.venv``) and has no ``.venv``
next to the code, and ``pyhooks`` itself is not installed into any venv: hooks
find it because ``run_hook.py`` puts the plugin root on ``PYTHONPATH``.

This launcher gives a skill exactly the hook launcher's view of the world:

* the interpreter comes from ``run_hook._resolve_python`` (CI mode ->
  ``sys.executable``; else ``${CLAUDE_PLUGIN_DATA}/.venv``; else
  ``<plugin>/.venv``; else ``sys.executable``), imported rather than copied so
  the two can never disagree;
* ``PYTHONPATH`` is the plugin root followed by whatever was already set;
* in CI mode the child runs under ``python -I`` with a bootstrap that puts only
  the plugin root on ``sys.path``, so a planted ``pyhooks/`` or ``json.py`` in
  an untrusted working directory is never imported (same rule as hooks).

Usage (``python3`` is enough; this file only needs the standard library)::

    CLAUDE_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" python3 .../scripts/skill_python.py -c "..."
    python3 .../scripts/skill_python.py path/to/script.py arg ...

Everything after the launcher's own name is forwarded to the interpreter
unchanged, and the interpreter's exit status becomes this process's exit status.

Finding the plugin-data directory.  Claude Code exports ``CLAUDE_PLUGIN_DATA``
to hooks but not to the Bash tool that runs skills, so the skill passes the
value Claude Code substitutes into plugin skill text, as an environment prefix.
An empty value, a relative path, or one that still contains a literal ``${``
(substitution did not happen) counts as absent.  As a last resort the launcher
derives the directory from its own location: an installed plugin lives at
``<plugins>/cache/<marketplace>/<plugin>/<version>`` and its data directory is
``<plugins>/data/<plugin>-<marketplace>``.  Like finding the garage by knowing
which house you are standing in, when nobody handed you the key.

Stdlib only; the import-and-exec step runs on Python 3.9.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path
from types import ModuleType

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_DATA_ENV = "CLAUDE_PLUGIN_DATA"

# Runs under ``python -I -c``: sys.argv is [-c, plugin_root, *forwarded].
_CI_BOOTSTRAP = (
    "import sys, runpy\n"
    "root = sys.argv[1]\n"
    "rest = sys.argv[2:]\n"
    "sys.path[:0] = [root]\n"
    "if rest[:1] == ['-c'] and len(rest) >= 2:\n"
    "    sys.argv = ['-c'] + rest[2:]\n"
    "    exec(compile(rest[1], '<string>', 'exec'), {'__name__': '__main__'})\n"
    "else:\n"
    "    sys.argv = rest\n"
    "    runpy.run_path(rest[0], run_name='__main__')\n"
)


def _clean_data_dir(value: str | None) -> str | None:
    """The value if it is a usable absolute path, else None (empty, relative, unsubstituted)."""
    if not value or "${" in value or not os.path.isabs(value):
        return None
    return value


def _derived_data_dir(plugin_root: Path) -> str | None:
    """Data directory implied by an installed-copy path, or None for any other layout."""
    parts = plugin_root.parts
    if len(parts) < 5 or parts[-4] != "cache" or parts[-5] != "plugins":
        return None
    marketplace, plugin = parts[-3], parts[-2]
    plugins_dir = Path(*parts[:-4])
    name = re.sub(r"[^A-Za-z0-9_-]", "-", f"{plugin}-{marketplace}")
    return str(plugins_dir / "data" / name)


def resolve_data_dir(
    environ: dict[str, str], plugin_root: Path = PLUGIN_ROOT
) -> str | None:
    """The plugin-data directory to use: the exported/substituted one, else the derived one."""
    return _clean_data_dir(environ.get(_DATA_ENV)) or _derived_data_dir(plugin_root)


def _load_run_hook(plugin_root: Path) -> ModuleType:
    """Import run_hook.py from the plugin root by path (no sys.path change, no package import)."""
    path = plugin_root / "run_hook.py"
    spec = importlib.util.spec_from_file_location("neuroloom_run_hook", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = (
        True  # do not litter the plugin directory with __pycache__
    )
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def build_command(
    forwarded: list[str], plugin_root: Path = PLUGIN_ROOT
) -> tuple[Path, list[str], dict[str, str], bool]:
    """Return (interpreter, argv, env, degraded) for the forwarded arguments.

    Mutates ``os.environ`` for the duration of the hook launcher's resolver call
    only: ``run_hook._resolve_python`` reads ``CLAUDE_PLUGIN_DATA`` and the CI
    switch from the process environment, so the cleaned/derived data directory is
    placed there first and the original value restored afterwards.
    """
    env = os.environ.copy()
    data_dir = resolve_data_dir(env, plugin_root)
    if data_dir is not None:
        env[_DATA_ENV] = data_dir
    else:
        env.pop(_DATA_ENV, None)

    run_hook = _load_run_hook(plugin_root)
    saved = os.environ.copy()
    try:
        os.environ.clear()
        os.environ.update(env)
        py, degraded = run_hook._resolve_python()
        ci = bool(run_hook._ci_mode())
    finally:
        os.environ.clear()
        os.environ.update(saved)

    root = str(plugin_root)
    if ci:
        args = [str(py), "-I", "-c", _CI_BOOTSTRAP, root, *forwarded]
        env["PYTHONPATH"] = (
            root  # inert under -I; the bootstrap's sys.path insert is the control
        )
    else:
        args = [str(py), *forwarded]
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = root + (os.pathsep + existing if existing else "")
    return Path(py), args, env, degraded


def main(argv: list[str]) -> int:
    forwarded = argv[1:]
    if not forwarded:
        print("usage: skill_python.py (-c CODE | SCRIPT) [ARG ...]", file=sys.stderr)
        return 2

    py, args, env, degraded = build_command(forwarded)
    if degraded:
        print(
            "neuroloom: no plugin virtualenv found (CLAUDE_PLUGIN_DATA/.venv or <plugin>/.venv);"
            " using system Python",
            file=sys.stderr,
        )

    if sys.platform == "win32":
        import subprocess

        return subprocess.run(args, env=env).returncode or 0
    try:
        os.execve(str(py), args, env)
    except OSError as exc:
        print(f"neuroloom: could not start {py}: {exc}", file=sys.stderr)
        return 1
    return 0  # unreachable: execve replaces the process


if __name__ == "__main__":
    sys.exit(main(sys.argv))
