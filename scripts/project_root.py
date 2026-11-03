#!/usr/bin/env python3
"""
Project-root resolver for skills and scripts (D178d OQ-4).

Hooks learn the project root from ``CLAUDE_PROJECT_DIR``, which Claude Code
exports to hook processes only -- it is NOT set in the Bash tool environment
that skills run in.  A skill that used the bare working directory therefore
wrote ``.neuroloom.db`` state into a subdirectory after a ``cd``, where the
hooks (reading the root's database) never looked.

``resolve_project_root()`` finds the same directory from the outside, trying in
order:

0. The optional *hint*: Claude Code substitutes ``${CLAUDE_PROJECT_DIR}`` into
   plugin skill text with the true project root (verified empirically; it
   stays the root after a ``cd``), so a skill passes it as the first argument.
   Used when it is an absolute path to an existing directory.  An empty hint,
   or one still containing a literal ``${`` (substitution did not happen), is
   ignored.
1. ``CLAUDE_PROJECT_DIR`` when it is set to an existing absolute directory
   (the same rule hooks use -- ``pyhooks.containment``).
2. The nearest ancestor of the working directory (itself included) that holds
   a ``.neuroloom.db`` file.
3. The nearest ancestor holding a ``.git`` entry (directory or file, so
   worktrees and submodules count).  A fresh project has no database yet,
   which is why this step exists.
4. The working directory.

Like asking "which building am I in?" by looking for the front desk (the
database), then the building's foundation stone (``.git``), and only then
settling for "wherever I am standing".

The marker search never answers ``$HOME`` or ``/``: a stray ``.neuroloom.db``
or ``~/.git`` there would otherwise capture every project beneath it.  Reaching
either counts as "not found" and the search stops.

A skill resolves the root ONCE, in the shell, and hands it to the Python
interpreter as ``CLAUDE_PROJECT_DIR`` -- the variable the hooks already trust --
so ``pyhooks.config.load()``, ``cfg.state_db_path`` and the snippet's own
``require_project_root()`` agree by construction::

    CLAUDE_PROJECT_DIR="$(python3 .../scripts/project_root.py "${CLAUDE_PROJECT_DIR}")" python -c "..."

Returns ``None`` (CLI: non-zero exit, nothing on stdout) when no root can be
determined, i.e. the working directory has been deleted.  Never raises.
Stdlib only, Python 3.10+ semantics but importable on 3.9.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_DB_NAME = ".neuroloom.db"
_PROJECT_DIR_ENV = "CLAUDE_PROJECT_DIR"


def _configured_project_dir() -> Path | None:
    """``CLAUDE_PROJECT_DIR`` if absolute and an existing directory, else None.

    Mirrors ``pyhooks.containment._configured_project_dir`` (absolute only);
    the directory-exists check is added because a skill cannot work from a
    stale path.  Duplicated rather than imported so this file works under any
    interpreter, not only the plugin venv.
    """
    configured = os.environ.get(_PROJECT_DIR_ENV, "")
    if not configured or not os.path.isabs(configured):
        return None
    try:
        path = Path(configured).resolve()
        return path if path.is_dir() else None
    except (OSError, RuntimeError, ValueError):
        return None


def _hint_dir(hint: str | None) -> Path | None:
    """The hint as a resolved directory, or None if empty, unsubstituted, relative or missing."""
    if not hint or "${" in hint or not os.path.isabs(hint):
        return None
    try:
        path = Path(hint).resolve()
        return path if path.is_dir() else None
    except (OSError, RuntimeError, ValueError):
        return None


def _home() -> Path | None:
    try:
        return Path.home().resolve()
    except (OSError, RuntimeError, ValueError, KeyError):
        return None


def _first_ancestor_with(start: Path, name: str, *, want_file: bool) -> Path | None:
    """Nearest directory from ``start`` upward holding ``name``; stops at $HOME and ``/``."""
    home = _home()
    for directory in (start, *start.parents):
        if directory == home or directory.parent == directory:
            return None
        try:
            candidate = directory / name
            if candidate.is_file() if want_file else candidate.exists():
                return directory
        except (OSError, ValueError):
            continue
    return None


def _resolve(hint: str | None) -> tuple[Path | None, str]:
    """Return (root, step) where step is hint|env|db|git|cwd, or (None, "none")."""
    try:
        hinted = _hint_dir(hint)
        if hinted is not None:
            return hinted, "hint"
        configured = _configured_project_dir()
        if configured is not None:
            return configured, "env"
        cwd = Path(os.getcwd()).resolve()
        if not cwd.is_dir():
            return None, "none"
        found = _first_ancestor_with(cwd, _DB_NAME, want_file=True)
        if found is not None:
            return found, "db"
        found = _first_ancestor_with(cwd, ".git", want_file=False)
        if found is not None:
            return found, "git"
        return cwd, "cwd"
    except (OSError, RuntimeError, ValueError):
        return None, "none"


def resolve_project_root(hint: str | None = None) -> Path | None:
    """Return the resolved project root, or None if it cannot be determined."""
    return _resolve(hint)[0]


def require_project_root() -> Path:
    """For skill snippets: the root the shell wrapper exported, or exit with a message.

    Reads ``CLAUDE_PROJECT_DIR`` only (never re-resolves), so it cannot disagree
    with ``pyhooks.config.load()``.  An empty or invalid value means the
    wrapper's resolver failed; stop instead of silently using the cwd.  Prints
    the root on stderr so a wrong pick is visible.
    """
    root = _configured_project_dir()
    if root is None:
        sys.exit("neuroloom: could not determine the project root; aborting")
    print(f"neuroloom: project root is {root}", file=sys.stderr)
    return root


def main() -> int:
    hint = sys.argv[1] if len(sys.argv) > 1 else None
    root, step = _resolve(hint)
    if root is None:
        print("neuroloom: could not determine the project root", file=sys.stderr)
        return 1
    if hint and "${" not in hint and step != "hint":
        print(
            f"neuroloom: ignoring hint '{hint}' (not an existing absolute directory)",
            file=sys.stderr,
        )
    print(f"neuroloom: project root chosen by {step}", file=sys.stderr)
    print(root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
