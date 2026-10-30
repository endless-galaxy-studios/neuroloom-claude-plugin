#!/usr/bin/env python3
"""
Seed a per-project Neuroloom API key into ``<project root>/.neuroloom.db``.

A factory (CI) step pipes the key in on stdin::

    printf %s "$KEY" | python3 scripts/seed_project_key.py --project-root <dir>

Plain-language picture: this is a night-safe deposit slot.  The envelope (the
key) goes in through stdin, never through the command line, where every other
process on the machine could read it in the process list.  The receipt (stdout)
names the slot, never the envelope's contents.

Contract:

* ``--project-root`` is required, given once, and must be a non-empty absolute
  path to an existing, writable directory; there is no default, no relative
  path and no environment fallback.  A key given as an argument is refused
  (exit 1) before stdin is read.
* The key must be printable ASCII after stripping surrounding whitespace.
* Every failure exits 1 with a fixed message that names the reason and never
  contains the key or any argument value.  Success prints one line on stdout.
* Before writing, git must report ``.neuroloom.db``, ``.neuroloom.db-wal``,
  ``.neuroloom.db-shm`` and ``.neuroloom.db-journal`` as ignored (SQLite may
  write any of them).  Only rules that travel with the repository count:
  ``core.excludesFile`` is pinned to ``/dev/null`` (``.git/info/exclude`` still
  counts).  The check is skipped, with a stderr note, only when git is absent
  or the root has no ``.git`` at or above it.  Any other git result fails
  closed.  git is looked up on the absolute ``PATH`` entries only.
* A ``.neuroloom.db*`` path that is a symlink, a non-regular file or has more
  than one hard link is refused: the seed must not write through a link.  The
  lstat check runs before and after the git calls.  Just before the save every
  existing file is opened with ``O_NOFOLLOW`` and checked through the
  descriptor, missing sidecars are pre-created with ``O_EXCL|O_NOFOLLOW`` and
  the main db is created with ``O_EXCL``.  This shrinks the check-to-open race;
  it does not close it for the main db, because the plugin's ``open_db``
  reopens that file by path without ``O_NOFOLLOW`` (accepted residual: the
  attacker needs write access to the project root).  The main db's
  (st_dev, st_ino) is compared again after the save.
* Files are held to mode 0600 (a chmod is skipped when the mode already is
  0600), and the stored key is read back and compared for equality, because
  the save helper swallows its own errors.

Standard library plus the plugin's own ``pyhooks`` package; no network call.
"""

from __future__ import annotations

import argparse
import errno
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

# Run-by-path support: put the plugin root (this file's grandparent) on sys.path.
_PLUGIN_ROOT = str(Path(__file__).resolve().parent.parent)
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

from pyhooks.config import load_project_api_key, save_project_api_key  # noqa: E402

_DB_NAME = ".neuroloom.db"
_DB_FILES = (
    _DB_NAME,
    _DB_NAME + "-wal",
    _DB_NAME + "-shm",
    _DB_NAME + "-journal",
)
_MAX_KEY_BYTES = 4096
_GIT_TIMEOUT_S = 30.0  # module constant so a test can shorten it in-process
_KEY_RE = re.compile(r"[\x21-\x7e]+")  # printable ASCII: no NUL, DEL, BOM, space


class _Fail(Exception):
    """A refusal with a fixed, key-free message."""


def _die(message: str) -> NoReturn:
    sys.stderr.write(f"seed_project_key: {message}\n")
    sys.exit(1)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:  # noqa: ARG002
        # argparse's own text would echo the offending argument (maybe a key).
        _die(
            "invalid arguments; usage: seed_project_key.py --project-root DIR "
            "(the key is read from stdin, never from an argument)"
        )


_HELP_TEXT = """\
usage: seed_project_key.py --project-root DIR

Seed a per-project Neuroloom API key into DIR/.neuroloom.db.
The key is read from stdin, never from an argument:

  printf %s "$KEY" | python3 seed_project_key.py --project-root DIR

--project-root DIR  required; an absolute path to an existing, writable directory.

Exit codes: 0 on success, 1 on any failure (one fixed line on stderr, never
the key).  -h and --help print this text and exit 0.
"""


def _parse(argv: list[str]) -> str:
    parser = _Parser(add_help=False, allow_abbrev=False)
    parser.add_argument("--project-root", required=True, action="append")
    args = parser.parse_args(argv)
    roots: list[str] = list(args.project_root)
    if len(roots) != 1:
        parser.error("repeated")
    return str(roots[0])


def _read_key() -> str:
    stream = sys.stdin
    if stream is None:
        raise _Fail("stdin is closed; pipe the key on stdin")
    try:
        raw = stream.buffer.read(_MAX_KEY_BYTES + 1)
    except (OSError, ValueError, AttributeError):
        raise _Fail("could not read the key from stdin") from None
    if len(raw) > _MAX_KEY_BYTES:
        raise _Fail("the key on stdin is too long")
    try:
        key = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise _Fail("the key on stdin is not valid UTF-8") from None
    if not key:
        raise _Fail("no key on stdin")
    if any(ch.isspace() for ch in key):
        raise _Fail("the key on stdin contains whitespace")
    if _KEY_RE.fullmatch(key) is None:
        raise _Fail("the key on stdin must be printable ASCII characters only")
    return key


def _find_git_ancestor(root: str) -> str | None:
    """Directory at or above *root* that holds ``.git`` (file or directory)."""
    current = root
    while True:
        if os.path.lexists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def _absolute_path() -> str:
    """``PATH`` reduced to absolute entries: '' and '.' would mean the current directory."""
    entries = os.environ.get("PATH", os.defpath).split(os.pathsep)
    return os.pathsep.join(e for e in entries if e and os.path.isabs(e))


def _git_env(path: str) -> dict[str, str]:
    """Minimal child environment: nothing like GIT_DIR / GIT_INDEX_FILE gets through."""
    env = {"PATH": path, "LC_ALL": "C"}
    home = os.environ.get("HOME")
    if home is not None:
        env["HOME"] = home
    return env


def _check_ignored(root: str) -> None:
    path = _absolute_path()
    git = shutil.which("git", path=path) if path else None
    if git is not None and not os.path.isabs(git):
        git = None
    if git is None:
        sys.stderr.write("seed_project_key: git not found; skipping the ignore check\n")
        return
    repo = _find_git_ancestor(root)
    if repo is None:
        sys.stderr.write(
            "seed_project_key: no .git at or above the project root; "
            "skipping the ignore check\n"
        )
        return
    env = _git_env(path)
    unignored: list[str] = []
    for name in _DB_FILES:
        try:
            done = subprocess.run(
                [
                    git,
                    "-c",
                    f"safe.directory={repo}",
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "core.excludesFile=/dev/null",
                    "-C",
                    root,
                    "check-ignore",
                    "-q",
                    "--",
                    name,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                timeout=_GIT_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise _Fail(
                "git could not be run or timed out in the ignore check"
            ) from None
        if done.returncode == 1:
            unignored.append(name)
        elif done.returncode == 128:
            raise _Fail(
                "the git ignore check failed (git exited 128: a broken repository "
                "or git configuration, for example HOME unset with a system "
                "gitconfig that needs it); refusing to continue"
            )
        elif done.returncode != 0:
            raise _Fail(
                "the git ignore check failed for a reason other than 'not "
                f"ignored' (git exited {done.returncode}); refusing to continue"
            )
    if unignored:
        raise _Fail(
            f"git does not ignore {', '.join(unignored)}; add an entry to the "
            f"repository's .gitignore that ignores {_DB_NAME}* (the database and "
            f"its -wal, -shm and -journal files) and make sure {_DB_NAME} is "
            "not tracked (rules from core.excludesFile / the global gitignore are "
            "not counted: they do not travel with the repository)"
        )


def _lstat_regular(path: str, name: str) -> os.stat_result | None:
    """lstat *path*; None if absent; refuse links and anything but a lone regular file."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        raise _Fail(f"could not inspect {name}") from None
    if stat.S_ISLNK(info.st_mode):
        raise _Fail(f"{name} is a symlink; refusing to write through it")
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise _Fail(f"{name} is not a regular file with a single link; refusing")
    return info


def _check_files(root: str) -> None:
    for name in _DB_FILES:
        _lstat_regular(os.path.join(root, name), name)


def _narrow(root: str, *, after_write: bool = False) -> None:
    """Hold every existing database file to 0600 (SQLite only sets it on creation)."""
    nofollow = os.chmod in os.supports_follow_symlinks
    for name in _DB_FILES:
        path = os.path.join(root, name)
        try:
            info = _lstat_regular(path, name)
        except _Fail:
            if not after_write:
                raise
            raise _Fail(
                f"{name} is a symlink or not a single regular file after the "
                "write; the key may have been written through it"
            ) from None
        if info is None or stat.S_IMODE(info.st_mode) == 0o600:
            continue
        try:
            if nofollow:
                os.chmod(path, 0o600, follow_symlinks=False)
            else:
                os.chmod(path, 0o600)
        except FileNotFoundError:
            continue  # a sidecar SQLite removed between the check and the chmod
        except (OSError, NotImplementedError):
            raise _Fail(f"could not restrict {name} to mode 0600") from None


def _vet_fd(fd: int, name: str) -> os.stat_result:
    """Check an open descriptor: a lone regular file, held to 0600."""
    try:
        info = os.fstat(fd)
    except OSError:
        raise _Fail(f"could not inspect {name}") from None
    if not stat.S_ISREG(info.st_mode):
        raise _Fail(f"{name} is not a regular file; refusing")
    if info.st_nlink != 1:
        raise _Fail(f"{name} does not have a single link; refusing")
    if stat.S_IMODE(info.st_mode) != 0o600:
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            raise _Fail(f"could not restrict {name} to mode 0600") from None
    return info


def _open_existing(path: str, name: str, base: int) -> int | None:
    """Open with O_NOFOLLOW (never blocking on a FIFO); None if absent."""
    try:
        return os.open(path, os.O_RDONLY | os.O_NONBLOCK | base)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, getattr(errno, "EMLINK", errno.ELOOP)):
            raise _Fail(f"{name} is a symlink; refusing to write through it") from None
        raise _Fail(f"could not open {name} safely") from None


def _vet_existing(path: str, name: str, base: int) -> tuple[int, int] | None:
    fd = _open_existing(path, name, base)
    if fd is None:
        return None
    try:
        info = _vet_fd(fd, name)
    finally:
        os.close(fd)
    return (info.st_dev, info.st_ino)


def _prepare_files(root: str) -> tuple[int, int] | None:
    """Vet every existing file through an O_NOFOLLOW descriptor (sidecars and the main
    db first, so a refusal creates nothing), then pre-create the missing sidecars with
    O_EXCL|O_NOFOLLOW, then create the main db with O_EXCL.  Returns the main db's
    (st_dev, st_ino) for the post-save comparison (None where O_NOFOLLOW is missing).
    The window between this function and open_db's reopen of the main db by path
    remains (an accepted residual: the attacker needs write access to the project root)."""
    if not (hasattr(os, "O_NOFOLLOW") and hasattr(os, "fchmod")):
        _narrow(root)  # platform without O_NOFOLLOW: the lstat checks are all we have
        return None
    base = os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    main_ident: tuple[int, int] | None = None
    for name in (*_DB_FILES[1:], _DB_NAME):
        found = _vet_existing(os.path.join(root, name), name, base)
        if name == _DB_NAME:
            main_ident = found
    for name in _DB_FILES[1:]:
        path = os.path.join(root, name)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | base, 0o600)
        except FileExistsError:
            # appeared since the vet above (or a link: O_EXCL never follows one)
            if _vet_existing(path, name, base) is None:
                raise _Fail(f"{name} changed while it was being prepared") from None
        except OSError:
            raise _Fail(f"could not create {name} safely") from None
        else:
            os.close(fd)
    if main_ident is None:
        path = os.path.join(root, _DB_NAME)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | base, 0o600)
        except FileExistsError:
            main_ident = _vet_existing(path, _DB_NAME, base)
            if main_ident is None:
                raise _Fail(f"{_DB_NAME} changed while it was being prepared") from None
        except OSError:
            raise _Fail(f"could not create {_DB_NAME} safely") from None
        else:
            try:
                info = _vet_fd(fd, _DB_NAME)
            finally:
                os.close(fd)
            main_ident = (info.st_dev, info.st_ino)
    return main_ident


def _check_main_identity(root: str, ident: tuple[int, int] | None) -> None:
    if ident is None:
        return
    try:
        info = os.lstat(os.path.join(root, _DB_NAME))
        now: tuple[int, int] | None = (info.st_dev, info.st_ino)
    except OSError:
        now = None
    if now != ident:
        raise _Fail(
            f"{_DB_NAME} was replaced during the write; the key may have been "
            "written through it"
        )


def _run(argv: list[str]) -> str:
    root_arg = _parse(argv)
    if not root_arg or not os.path.isabs(root_arg) or not os.path.isdir(root_arg):
        raise _Fail(
            "--project-root is not an existing directory (an absolute path is required)"
        )
    root = os.path.realpath(root_arg)
    if not os.access(root, os.W_OK | os.X_OK):
        raise _Fail("project root is not writable")
    key = _read_key()
    _check_files(root)
    _check_ignored(root)
    _check_files(root)  # again, last, to shrink the window the git calls opened

    os.umask(0o077)  # backstop for files SQLite creates; existing ones are fixed below
    main_ident = _prepare_files(root)
    save_project_api_key(Path(root), key)
    _check_main_identity(root, main_ident)
    _narrow(root, after_write=True)  # sidecars that appeared during the write
    if load_project_api_key(Path(root)) != key:
        raise _Fail("the key could not be stored; read-back did not match")
    return os.path.join(root, _DB_NAME)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    # Only a lone -h/--help asks for help; any other use of them is a bad argument.
    if args in (["-h"], ["--help"]):
        sys.stdout.write(_HELP_TEXT)
        return 0
    try:
        db_path = _run(args)
    except _Fail as exc:
        _die(str(exc))
    sys.stdout.write(f"seeded project key into {db_path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
