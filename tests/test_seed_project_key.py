"""
Tests for ``scripts/seed_project_key.py`` (D178d, Phase 4, FR-C2).

The script is a factory step: it reads an API key on stdin and stores it in
``<project root>/.neuroloom.db``.  Every test runs it as a *subprocess* (the
way a workflow step would) with an explicit minimal environment, so nothing in
this process can leak into the script and the script's real exit code, stdout
and stderr are what gets asserted.

Plain-language picture: the script is a night-safe deposit slot.  These tests
check that the envelope lands in the right slot (key lands, read-back equal),
that the slot is locked (mode 0600), that the bank refuses a slot that is
visible from the street (ignore check), and that the receipt never prints the
contents of the envelope (key absent from every output stream).

Conventions:

* The key is a distinctive sentinel; ``assert_no_leak`` looks for the whole key
  and for its prefix, middle and suffix, so a partial echo is caught too.
* Assertions compare precomputed booleans and carry a distinct message, so a
  failure never prints a captured stream (which could carry a key).
* ``run_seed`` fails with an explicit "seed script absent" message while
  ``scripts/seed_project_key.py`` does not exist, so a red run before the
  script is written is distinguishable from a real assertion failure.
"""

from __future__ import annotations

import importlib.util
import io
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator
from types import ModuleType
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests import ci_support

SCRIPT = ci_support.PLUGIN_ROOT / "scripts" / "seed_project_key.py"

# Distinctive sentinel keys: prefix / middle / suffix are each searched for.
KEY = "nlk_live_SEN7INEL9f3a1c7e_tail42zz"
KEY2 = "nlk_live_SEN7INEL2b8d4f60_other77yy"
OLD_KEY = "nlk_old_0LDSEN7INEL5c1e_stale11xx"
SPACED_KEY = "nlk_live_SEN7INEL9f3a1c7e tail42zz"
_LEAK_FRAGMENTS = (
    KEY,
    KEY2,
    OLD_KEY,
    SPACED_KEY,
    "nlk_live_",
    "nlk_old_",
    "SEN7INEL9f3a1c7e",
    "SEN7INEL2b8d4f60",
    "0LDSEN7INEL5c1e",
    "tail42zz",
    "other77yy",
    "stale11xx",
)

DB_NAMES = (
    ".neuroloom.db",
    ".neuroloom.db-wal",
    ".neuroloom.db-shm",
    ".neuroloom.db-journal",
)
# The two sidecars SQLite keeps alive while a connection is open (WAL mode).
LIVE_SIDECARS = DB_NAMES[1:3]
# Real keys are secrets.token_hex(20): 40 lowercase hex characters.
HEX_KEY = "3fa85f6457174562b3fc2c963f66afa6c0ffee12"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@dataclass
class SeedRun:
    returncode: int
    stdout: str
    stderr: str


def _require_script() -> None:
    if not SCRIPT.is_file():
        pytest.fail(
            "seed script absent: scripts/seed_project_key.py does not exist yet",
            pytrace=False,
        )


def _seed_env(
    scratch: Path,
    path_dirs: list[Path] | None,
    extra: dict[str, str] | None,
    *,
    with_system_path: bool = True,
) -> dict[str, str]:
    """Explicit minimal environment (never a copy of ``os.environ``).

    PATH: the stub dirs first; then (unless ``with_system_path`` is False) the
    interpreter's directory and the directory of the git this machine resolves
    (``shutil.which``), so a real git is found wherever it is installed.  With
    ``with_system_path`` False the PATH holds the stub dirs and an empty
    directory only: git is genuinely absent even when the interpreter and git
    share a directory.
    """
    home = scratch / "home"
    home.mkdir(parents=True, exist_ok=True)
    parts = [str(p) for p in (path_dirs or [])]
    if with_system_path:
        parts.append(str(Path(sys.executable).parent))
        real_git = shutil.which("git")
        if real_git:
            parts.append(os.path.dirname(real_git))
    else:
        empty = scratch / "empty-path-dir"
        empty.mkdir(exist_ok=True)
        parts.append(str(empty))
    env = {
        "PATH": os.pathsep.join(parts),
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    if extra:
        env.update(extra)
    return env


def run_seed(
    args: list[str],
    *,
    stdin: str | bytes,
    scratch: Path,
    cwd: Path | None = None,
    path_dirs: list[Path] | None = None,
    with_system_path: bool = True,
    env_extra: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> SeedRun:
    """Run the script as a subprocess; stdin is sent as bytes (str is UTF-8)."""
    _require_script()
    work = cwd or (scratch / "neutral-cwd")
    work.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        input=stdin.encode("utf-8") if isinstance(stdin, str) else stdin,
        capture_output=True,
        env=_seed_env(scratch, path_dirs, env_extra, with_system_path=with_system_path),
        cwd=str(work),
        timeout=timeout,
        check=False,
    )
    return SeedRun(
        done.returncode,
        done.stdout.decode("utf-8", "replace"),
        done.stderr.decode("utf-8", "replace"),
    )


def run_seed_shell_stdin(
    redirect: str, args: list[str], *, scratch: Path, target: Path | None = None
) -> SeedRun:
    """Run the script with fd 0 set by a shell redirect.

    ``redirect`` is ``"<&-"`` (close fd 0) or ``'0>"$TARGET"'`` (fd 0 is *target*
    opened write-only, so a read fails with an OSError).
    """
    _require_script()
    work = scratch / "neutral-cwd"
    work.mkdir(parents=True, exist_ok=True)
    extra = {"TARGET": str(target)} if target is not None else None
    done = subprocess.run(
        [
            "/bin/sh",
            "-c",
            f'exec "$0" "$@" {redirect}',
            sys.executable,
            str(SCRIPT),
            *args,
        ],
        capture_output=True,
        env=_seed_env(scratch, None, extra),
        cwd=str(work),
        timeout=30,
        check=False,
    )
    return SeedRun(
        done.returncode,
        done.stdout.decode("utf-8", "replace"),
        done.stderr.decode("utf-8", "replace"),
    )


def assert_no_leak(run: SeedRun, label: str) -> None:
    """The key (whole or in pieces) is absent from stdout and stderr."""
    out_clean = not any(f in run.stdout for f in _LEAK_FRAGMENTS)
    err_clean = not any(f in run.stderr for f in _LEAK_FRAGMENTS)
    assert out_clean, f"{label}: a key fragment appeared on stdout"
    assert err_clean, f"{label}: a key fragment appeared on stderr"


def assert_fixed_failure(run: SeedRun, label: str, reason: str | None = None) -> None:
    """Exit 1, a message on stderr, no traceback, no key; optionally one reason word."""
    assert run.returncode == 1, f"{label}: expected exit code 1, got {run.returncode}"
    assert run.stderr.strip() != "", f"{label}: a failure must print a reason on stderr"
    assert "Traceback" not in run.stderr, (
        f"{label}: a failure must not print a traceback"
    )
    assert_no_leak(run, label)
    if reason is not None:
        assert reason in run.stderr.lower(), (
            f"{label}: the message must carry the reason {reason!r}"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def stored_key(root: Path) -> str | None:
    """Read the api_key row with plain sqlite (independent of pyhooks)."""
    db = root / ".neuroloom.db"
    if not db.is_file():
        return None
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute("SELECT value FROM config WHERE key = 'api_key'").fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return None if row is None else str(row[0])


def api_key_rows(root: Path) -> int:
    conn = sqlite3.connect(str(root / ".neuroloom.db"))
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM config WHERE key = 'api_key'"
            ).fetchone()[0]
        )
    finally:
        conn.close()


def db_files(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir() if p.name.startswith(".neuroloom.db"))


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def no_wider_than_0600(path: Path) -> bool:
    return (mode_of(path) & 0o177) == 0


def make_stub_git(directory: Path, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stub = directory / "git"
    stub.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    stub.chmod(0o755)
    return directory


def make_repo(root: Path, gitignore: str) -> Path:
    """A committed git repository whose .gitignore holds exactly *gitignore*."""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".gitignore").write_text(gitignore, encoding="utf-8")
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    for step in (
        ("init", "-q"),
        ("add", ".gitignore", "README.md"),
        ("commit", "-q", "-m", "fixture"),
    ):
        done = ci_support.git(root, *step)
        if done.returncode != 0:
            raise RuntimeError(
                f"git {step[0]} failed while building the fixture repository"
            )
    return root


@pytest.fixture()
def needs_git() -> None:
    if not ci_support.git_available():
        pytest.skip("git is not available")


def seed(
    root: Path,
    scratch: Path,
    key: str = KEY,
    *,
    path_dirs: list[Path] | None = None,
    with_system_path: bool = True,
    env_extra: dict[str, str] | None = None,
) -> SeedRun:
    return run_seed(
        ["--project-root", str(root)],
        stdin=key + "\n",
        scratch=scratch,
        path_dirs=path_dirs,
        with_system_path=with_system_path,
        env_extra=env_extra,
    )


# ---------------------------------------------------------------------------
# Presence (separates "script absent" from real assertion failures)
# ---------------------------------------------------------------------------


def test_seed_script_exists() -> None:
    assert SCRIPT.is_file(), "scripts/seed_project_key.py must exist"


# ---------------------------------------------------------------------------
# Happy path, end to end
# ---------------------------------------------------------------------------


def test_end_to_end_happy_path(ignored_git_repo: Path, tmp_path: Path) -> None:
    before = ci_support.tree_hash(ignored_git_repo)
    run = seed(ignored_git_repo, tmp_path)
    lines = [ln for ln in run.stdout.splitlines() if ln.strip()]
    db_text = ".neuroloom.db"
    named_paths = {
        str(ignored_git_repo / db_text),
        str(ignored_git_repo.resolve() / db_text),
    }

    assert run.returncode == 0, "happy path must exit 0"
    assert len(lines) == 1, "success prints exactly one stdout line"
    assert any(p in lines[0] for p in named_paths), (
        "the stdout line must name the database path"
    )
    assert_no_leak(run, "happy path")
    assert stored_key(ignored_git_repo) == KEY, (
        "read-back must equal the key given on stdin"
    )
    status = ci_support.git(ignored_git_repo, "status", "--porcelain").stdout
    assert status == "", (
        "the seed must leave git status clean (nothing un-ignored, no tracked edits)"
    )
    assert ci_support.tree_hash(ignored_git_repo) == before, (
        "no file other than .neuroloom.db* may change"
    )


def test_key_lands_with_mode_0600_and_surrounding_whitespace_is_stripped(
    ignored_git_repo: Path, tmp_path: Path
) -> None:
    run = run_seed(
        ["--project-root", str(ignored_git_repo)],
        stdin=f"  {KEY} \r\n",
        scratch=tmp_path,
    )
    assert run.returncode == 0, "a key with surrounding whitespace must be accepted"
    assert stored_key(ignored_git_repo) == KEY, (
        "surrounding whitespace must be stripped before storing"
    )
    assert mode_of(ignored_git_repo / ".neuroloom.db") == 0o600, (
        "a new db file must be mode 0600"
    )
    assert_no_leak(run, "whitespace-stripped success")


def test_rerun_replaces_the_key_row(ignored_git_repo: Path, tmp_path: Path) -> None:
    first = seed(ignored_git_repo, tmp_path, KEY)
    second = seed(ignored_git_repo, tmp_path, KEY2)
    assert first.returncode == 0, "first seed must succeed"
    assert second.returncode == 0, "re-run must succeed"
    assert stored_key(ignored_git_repo) == KEY2, "a re-run must replace the stored key"
    assert api_key_rows(ignored_git_repo) == 1, (
        "a re-run must leave exactly one api_key row"
    )
    assert_no_leak(second, "re-run")


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


def test_rerun_over_0644_main_file_leaves_it_0600(
    ignored_git_repo: Path, tmp_path: Path
) -> None:
    assert seed(ignored_git_repo, tmp_path, OLD_KEY).returncode == 0, (
        "setup seed must succeed"
    )
    main = ignored_git_repo / ".neuroloom.db"
    main.chmod(0o644)
    assert mode_of(main) == 0o644, "precondition: main file widened to 0644"

    run = seed(ignored_git_repo, tmp_path, KEY)
    assert run.returncode == 0, "re-run over a 0644 file must succeed"
    assert mode_of(main) == 0o600, "the main file must be narrowed to 0600"
    assert stored_key(ignored_git_repo) == KEY, "the new key must land"
    leftovers = [
        n
        for n in db_files(ignored_git_repo)
        if not no_wider_than_0600(ignored_git_repo / n)
    ]
    assert leftovers == [], (
        "every .neuroloom.db* file left behind must be no wider than 0600"
    )


def test_rerun_over_live_0644_sidecars_narrows_all_of_them(
    ignored_git_repo: Path, tmp_path: Path
) -> None:
    """Hold a reader open so SQLite keeps -wal and -shm alive across the script run.

    Without the open connection SQLite deletes the sidecars when the script's
    connection closes, and "all files no wider than 0600" would hold vacuously.
    """
    import pyhooks.db as db_mod  # noqa: PLC0415

    main = ignored_git_repo / ".neuroloom.db"
    holder = db_mod.open_db(main)
    assert holder is not None, "precondition: the fixture database opens"
    try:
        holder.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES ('api_key', ?)",
            (OLD_KEY,),
        )
        holder.commit()
        holder.execute("SELECT COUNT(*) FROM config").fetchone()
        sidecars = [ignored_git_repo / n for n in LIVE_SIDECARS]
        sidecars_exist = all(p.exists() for p in sidecars)
        assert sidecars_exist, (
            "precondition: -wal and -shm exist while a connection is open"
        )
        for p in [main, *sidecars]:
            p.chmod(0o644)
        widened = all(mode_of(p) == 0o644 for p in [main, *sidecars])
        assert widened, "precondition: main, -wal and -shm are all 0644"

        run = seed(ignored_git_repo, tmp_path, KEY)
        assert run.returncode == 0, "re-run over live 0644 sidecars must succeed"
        present = [p for p in [main, *sidecars] if p.exists()]
        assert main in present, "the main file must still exist"
        too_wide = [p.name for p in present if not no_wider_than_0600(p)]
        assert too_wide == [], (
            "every .neuroloom.db* file present must be no wider than 0600"
        )
        assert mode_of(main) == 0o600, "the main file must be exactly 0600"
        assert_no_leak(run, "live sidecars re-run")
    finally:
        holder.close()
    assert stored_key(ignored_git_repo) == KEY, "the new key must land"


def test_rerun_over_stale_0644_sidecar_files_leaves_nothing_wide(
    ignored_git_repo: Path, tmp_path: Path
) -> None:
    main = ignored_git_repo / ".neuroloom.db"
    assert seed(ignored_git_repo, tmp_path, OLD_KEY).returncode == 0, (
        "setup seed must succeed"
    )
    for name in DB_NAMES[1:]:
        (ignored_git_repo / name).write_bytes(b"")
    for name in DB_NAMES:
        (ignored_git_repo / name).chmod(0o644)

    run = seed(ignored_git_repo, tmp_path, KEY)
    assert run.returncode == 0, "re-run over stale 0644 sidecar files must succeed"
    too_wide = [
        n
        for n in db_files(ignored_git_repo)
        if not no_wider_than_0600(ignored_git_repo / n)
    ]
    assert too_wide == [], "no .neuroloom.db* file may be left wider than 0600"
    assert mode_of(main) == 0o600, "the main file must be exactly 0600"
    assert stored_key(ignored_git_repo) == KEY, "the new key must land"


# ---------------------------------------------------------------------------
# Failed write (read-back catches it)
# ---------------------------------------------------------------------------


def _blocked_db(root: Path, *, old_key: str | None) -> None:
    """A database whose config table rejects every INSERT (the write is swallowed
    by ``save_project_api_key``, so only the read-back can notice)."""
    import pyhooks.db as db_mod  # noqa: PLC0415

    conn = db_mod.open_db(root / ".neuroloom.db")
    assert conn is not None, "precondition: the fixture database opens"
    try:
        if old_key is not None:
            conn.execute(
                "INSERT OR REPLACE INTO config (key, value) VALUES ('api_key', ?)",
                (old_key,),
            )
        conn.execute(
            "CREATE TRIGGER block_config_insert BEFORE INSERT ON config "
            "BEGIN SELECT RAISE(ABORT, 'blocked by test'); END"
        )
        conn.commit()
    finally:
        conn.close()


def test_failed_write_over_older_key_exits_1(
    ignored_git_repo: Path, tmp_path: Path
) -> None:
    _blocked_db(ignored_git_repo, old_key=OLD_KEY)
    assert stored_key(ignored_git_repo) == OLD_KEY, (
        "precondition: the older key is stored"
    )

    run = seed(ignored_git_repo, tmp_path, KEY)
    assert_fixed_failure(run, "write failure over an older key", "read-back")
    assert stored_key(ignored_git_repo) == OLD_KEY, (
        "the failed write must leave the older key untouched"
    )


def test_failed_write_with_no_previous_key_exits_1(
    ignored_git_repo: Path, tmp_path: Path
) -> None:
    _blocked_db(ignored_git_repo, old_key=None)
    run = seed(ignored_git_repo, tmp_path, KEY)
    assert_fixed_failure(run, "write failure with no previous key", "read-back")
    assert stored_key(ignored_git_repo) is None, (
        "no key row may exist after a failed write"
    )


# ---------------------------------------------------------------------------
# Ignore check
# ---------------------------------------------------------------------------


def _unignored_paths(run: SeedRun) -> list[str]:
    """The path list in the failure message: the text before the first ';'.

    The advice after the ';' always names all three files, so only the part
    before it says which paths git actually reported as not ignored.
    """
    head = run.stderr.split(";", 1)[0]
    return sorted(
        w.strip(" ,") for w in head.split() if w.strip(" ,").startswith(".neuroloom.db")
    )


def test_bare_db_entry_names_wal_and_shm_but_not_the_db_as_unignored(
    needs_git: None, tmp_path: Path
) -> None:
    repo = make_repo(tmp_path / "bare-repo", ".neuroloom.db\n")
    run = seed(repo, tmp_path)
    assert_fixed_failure(run, "bare .neuroloom.db entry", "does not ignore")
    assert _unignored_paths(run) == [
        ".neuroloom.db-journal",
        ".neuroloom.db-shm",
        ".neuroloom.db-wal",
    ], "the failing-path portion must name exactly -journal, -shm and -wal"
    assert ".neuroloom.db*" in run.stderr, "the .neuroloom.db* advice must remain"
    assert db_files(repo) == [], "a refused seed must create no .neuroloom.db* file"


def test_only_some_paths_unignored_names_exactly_those(
    needs_git: None, tmp_path: Path
) -> None:
    repo = make_repo(tmp_path / "partial-repo", ".neuroloom.db\n.neuroloom.db-wal\n")
    run = seed(repo, tmp_path)
    assert_fixed_failure(run, "partially ignored", "does not ignore")
    assert _unignored_paths(run) == [".neuroloom.db-journal", ".neuroloom.db-shm"], (
        "only the un-ignored -shm and -journal paths may be named before the ';'"
    )


def test_only_the_journal_unignored_names_only_the_journal(
    needs_git: None, tmp_path: Path
) -> None:
    repo = make_repo(
        tmp_path / "journal-repo",
        ".neuroloom.db\n.neuroloom.db-wal\n.neuroloom.db-shm\n",
    )
    run = seed(repo, tmp_path)
    assert_fixed_failure(run, "journal unignored", "does not ignore")
    assert _unignored_paths(run) == [".neuroloom.db-journal"], (
        "the journal is named when it is the failing path"
    )
    assert ".neuroloom.db*" in run.stderr, "the .neuroloom.db* advice must remain"
    assert db_files(repo) == [], "a refused seed must create no .neuroloom.db* file"


def test_ignore_rules_that_do_not_travel_with_the_repo_do_not_count(
    needs_git: None, tmp_path: Path
) -> None:
    """core.excludesFile, ~/.gitignore and ~/.config/git/ignore are per-machine."""
    repo = make_repo(tmp_path / "excludes-repo", "node_modules/\n")
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / ".gitignore").write_text(".neuroloom.db*\n", encoding="utf-8")
    (home / ".gitconfig").write_text(
        f"[core]\n\texcludesFile = {home / '.gitignore'}\n", encoding="utf-8"
    )
    run = seed(
        repo, tmp_path, env_extra={"GIT_CONFIG_GLOBAL": str(home / ".gitconfig")}
    )
    assert_fixed_failure(run, "machine-local ignore rules", "does not ignore")
    assert "not counted" in run.stderr, (
        "the message must say global rules are not counted"
    )
    assert db_files(repo) == [], "a refused seed must create no .neuroloom.db* file"


def test_xdg_global_ignore_file_alone_does_not_count(
    needs_git: None, tmp_path: Path
) -> None:
    """Only ~/.config/git/ignore (no .gitconfig, no core.excludesFile)."""
    repo = make_repo(tmp_path / "xdg-repo", "node_modules/\n")
    home = tmp_path / "home"
    (home / ".config" / "git").mkdir(parents=True)
    (home / ".config" / "git" / "ignore").write_text(
        ".neuroloom.db*\n", encoding="utf-8"
    )
    assert not (home / ".gitconfig").exists(), "precondition: no .gitconfig"
    run = seed(repo, tmp_path)
    assert_fixed_failure(run, "xdg-only ignore rules", "does not ignore")
    assert db_files(repo) == [], "a refused seed must create no .neuroloom.db* file"


def test_nothing_ignored_names_all_three_paths(needs_git: None, tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "none-repo", "node_modules/\n")
    run = seed(repo, tmp_path)
    assert_fixed_failure(run, "nothing ignored", "does not ignore")
    assert _unignored_paths(run) == sorted(DB_NAMES), (
        "every un-ignored path must be named when none is ignored"
    )


def test_no_gitignore_entry_at_all_is_refused_and_creates_nothing(
    needs_git: None, tmp_path: Path
) -> None:
    repo = make_repo(tmp_path / "plain-repo", "node_modules/\n")
    run = seed(repo, tmp_path)
    assert_fixed_failure(run, "no ignore entry")
    assert db_files(repo) == [], "a refused seed must create no .neuroloom.db* file"
    text = (repo / ".gitignore").read_text(encoding="utf-8")
    assert text == "node_modules/\n", "the script must never edit .gitignore"


def test_tracked_db_matching_an_ignore_rule_is_refused(
    needs_git: None, tmp_path: Path
) -> None:
    repo = make_repo(tmp_path / "tracked-repo", ".neuroloom.db*\n")
    (repo / ".neuroloom.db").write_bytes(b"")
    forced = ci_support.git(repo, "add", "-f", ".neuroloom.db")
    committed = ci_support.git(repo, "commit", "-q", "-m", "track the db by force")
    assert forced.returncode == 0 and committed.returncode == 0, (
        "precondition: the db file is tracked"
    )
    probe = ci_support.git(repo, "check-ignore", "-q", "--", ".neuroloom.db")
    assert probe.returncode == 1, (
        "precondition: git reports a tracked file as not ignored"
    )

    run = seed(repo, tmp_path)
    assert_fixed_failure(run, "tracked db file")
    assert (repo / ".neuroloom.db").read_bytes() == b"", (
        "a refused seed must not write into the tracked file"
    )
    assert ".neuroloom.db" in run.stderr, "the message must name the tracked db path"


def test_ignore_check_calls_git_once_per_path_with_safe_directory(
    needs_git: None, tmp_path: Path
) -> None:
    """Recording stub that delegates to the real git; --project-root is a SUBDIRECTORY.

    safe.directory must be the ancestor that holds .git, not --project-root.
    """
    real_git = shutil.which("git")
    assert real_git, "precondition: a real git is on PATH"
    log = tmp_path / "git-calls.log"
    repo = ci_support.make_ignored_git_repo(tmp_path / "sub-repo")
    sub = repo / "packages" / "app"
    sub.mkdir(parents=True)
    stub_dir = make_stub_git(
        tmp_path / "recording-bin",
        f'{{ echo CALL; for a in "$@"; do echo "$a"; done; }} >> "{log}"\nexec "{real_git}" "$@"',
    )

    run = seed(sub, tmp_path, path_dirs=[stub_dir])
    assert run.returncode == 0, (
        "a --project-root that is a subdirectory of the repository must work"
    )
    assert stored_key(sub) == KEY, "the key must land in the subdirectory's database"
    assert_no_leak(run, "subdirectory success")

    calls: list[list[str]] = []
    for line in log.read_text(encoding="utf-8").splitlines():
        if line == "CALL":
            calls.append([])
        else:
            calls[-1].append(line)
    check_calls = [c for c in calls if "check-ignore" in c]
    assert len(check_calls) == 4, "one git check-ignore call per path (four paths)"
    named = sorted(Path(c[-1]).name for c in check_calls)
    assert named == sorted(DB_NAMES), (
        "the four calls cover .neuroloom.db, -wal, -shm and -journal"
    )
    assert all("-q" in c for c in check_calls), "each call uses -q"
    assert all("--no-index" not in c for c in check_calls), (
        "no call may use --no-index (spec D-15)"
    )
    assert all(c[-2] == "--" for c in check_calls), (
        "each call separates the path with --"
    )
    repo_real = os.path.realpath(repo)
    safe = {f"safe.directory={repo_real}", f"safe.directory={repo}"}
    assert all(any(a in safe for a in c) for c in check_calls), (
        "safe.directory must be the ancestor directory that holds .git, not --project-root"
    )


@pytest.mark.parametrize(
    ("stub_exit", "word"),
    [(128, "broken repository"), (2, "other than"), (255, "other than")],
)
def test_git_failing_for_another_reason_exits_1(
    needs_git: None, tmp_path: Path, stub_exit: int, word: str
) -> None:
    repo = ci_support.make_ignored_git_repo(tmp_path / "failing-git-repo")
    stub_dir = make_stub_git(tmp_path / "failing-bin", f"exit {stub_exit}")
    run = seed(repo, tmp_path, path_dirs=[stub_dir])
    assert_fixed_failure(run, f"git exit {stub_exit} with a .git present", word)
    assert f"git exited {stub_exit}" in run.stderr, (
        "the message must carry git's return code (never its stderr)"
    )
    assert db_files(repo) == [], (
        "a fail-closed refusal must create no .neuroloom.db* file"
    )


def test_git_absent_skips_the_check_with_a_stderr_note_and_succeeds(
    needs_git: None, tmp_path: Path
) -> None:
    repo = ci_support.make_ignored_git_repo(tmp_path / "no-git-binary-repo")
    run = seed(repo, tmp_path, with_system_path=False)
    assert run.returncode == 0, (
        "with git absent the ignore check is skipped and the seed succeeds"
    )
    assert "skip" in run.stderr.lower(), "the skip must be announced on stderr"
    assert stored_key(repo) == KEY, "the key must land when the check is skipped"
    assert_no_leak(run, "git absent")


def test_no_dot_git_at_or_above_root_skips_with_a_note_and_succeeds(
    tmp_path: Path,
) -> None:
    root = tmp_path / "plain-project"
    root.mkdir()
    # A stub that always says 128 proves the script decides "no repository" by
    # looking for .git itself, not by interpreting git's exit code.
    stub_dir = make_stub_git(tmp_path / "stub128-bin", "exit 128")
    run = seed(root, tmp_path, path_dirs=[stub_dir])
    assert run.returncode == 0, (
        "with no .git at or above the root the check is skipped and the seed succeeds"
    )
    assert "skip" in run.stderr.lower(), "the skip must be announced on stderr"
    assert stored_key(root) == KEY, "the key must land when the check is skipped"
    assert_no_leak(run, "no repository")


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "stdin", "reason"),
    [
        ("empty stdin", "", "no key"),
        ("whitespace-only stdin", "  \r\n\t \n", "no key"),
        ("internal whitespace", SPACED_KEY + "\n", "whitespace"),
        ("internal newline", "nlk_live_SEN7INEL9f3a1c7e\ntail42zz\n", "whitespace"),
        ("NUL in key", "nlk_live_SEN7INEL9f3a1c7e\x00tail42zz\n", "printable ascii"),
        ("BOM prefix", "\ufeff" + KEY + "\n", "printable ascii"),
        ("DEL in key", "nlk_live_SEN7INEL9f3a1c7e\x7ftail42zz\n", "printable ascii"),
        (
            "zero-width space",
            "nlk_live_SEN7INEL9f3a1c7e\u200btail42zz\n",
            "printable ascii",
        ),
        (
            "non-ASCII letter",
            "nlk_live_SEN7INEL9f3a1c7e\u00e9tail42zz\n",
            "printable ascii",
        ),
    ],
)
def test_bad_key_on_stdin_exits_1(
    ignored_git_repo: Path, tmp_path: Path, label: str, stdin: str, reason: str
) -> None:
    run = run_seed(
        ["--project-root", str(ignored_git_repo)], stdin=stdin, scratch=tmp_path
    )
    assert_fixed_failure(run, label, reason)
    assert db_files(ignored_git_repo) == [], (
        f"{label}: a rejected key must create no .neuroloom.db* file"
    )


def test_missing_project_root_directory_exits_1(tmp_path: Path) -> None:
    run = run_seed(
        ["--project-root", str(tmp_path / "does-not-exist")],
        stdin=KEY,
        scratch=tmp_path,
    )
    assert_fixed_failure(
        run, "missing --project-root directory", "not an existing directory"
    )
    assert not (tmp_path / "does-not-exist").exists(), (
        "the script must not create the missing root"
    )


def test_project_root_that_is_a_file_exits_1(tmp_path: Path) -> None:
    not_a_dir = tmp_path / "a-file"
    not_a_dir.write_text("x", encoding="utf-8")
    run = run_seed(["--project-root", str(not_a_dir)], stdin=KEY, scratch=tmp_path)
    assert_fixed_failure(
        run, "non-directory --project-root", "not an existing directory"
    )


def test_project_root_option_without_a_value_exits_1(tmp_path: Path) -> None:
    run = run_seed(["--project-root"], stdin=KEY, scratch=tmp_path)
    assert_fixed_failure(run, "--project-root with no value", "invalid arguments")


def test_omitted_project_root_exits_1_even_with_project_dir_and_repo_cwd(
    ignored_git_repo: Path, tmp_path: Path
) -> None:
    run = run_seed(
        [],
        stdin=KEY,
        scratch=tmp_path,
        cwd=ignored_git_repo,
        env_extra={"CLAUDE_PROJECT_DIR": str(ignored_git_repo)},
    )
    assert_fixed_failure(run, "omitted --project-root", "invalid arguments")
    assert db_files(ignored_git_repo) == [], (
        "the script has no default root: nothing may be seeded"
    )


def test_abbreviated_project_root_flag_is_refused(
    ignored_git_repo: Path, tmp_path: Path
) -> None:
    run = run_seed(["--project-r", str(ignored_git_repo)], stdin=KEY, scratch=tmp_path)
    assert_fixed_failure(run, "abbreviated --project-r", "invalid arguments")
    assert db_files(ignored_git_repo) == [], "an abbreviated flag must not be accepted"


@pytest.mark.parametrize(
    "argv_style",
    ["after-flag", "before-flag", "alone", "key-as-option-value"],
)
def test_key_as_argument_exits_1_and_is_refused_before_reading_stdin(
    ignored_git_repo: Path, tmp_path: Path, argv_style: str
) -> None:
    root = str(ignored_git_repo)
    argv = {
        "after-flag": ["--project-root", root, KEY],
        "before-flag": [KEY, "--project-root", root],
        "alone": [KEY],
        "key-as-option-value": ["--project-root", root, f"--key={KEY}"],
    }[argv_style]
    _require_script()
    neutral = tmp_path / "neutral-cwd"
    neutral.mkdir()
    # stdin is a pipe that is never written or closed: a script that reads it
    # before refusing the argument would block until the timeout.
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT), *argv],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_seed_env(tmp_path, None, None),
        cwd=str(neutral),
    )
    blocked = False
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        blocked = True
        proc.kill()
        proc.wait()
    out = proc.stdout.read() if proc.stdout else ""
    err = proc.stderr.read() if proc.stderr else ""
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream:
            stream.close()
    assert not blocked, (
        f"{argv_style}: the script waited on stdin instead of refusing the argument first"
    )
    run = SeedRun(proc.returncode, out, err)
    assert_fixed_failure(run, f"key as argument ({argv_style})", "invalid arguments")
    assert run.returncode != 2, f"{argv_style}: must exit 1, not argparse's exit code 2"
    assert db_files(ignored_git_repo) == [], f"{argv_style}: nothing may be seeded"


# ---------------------------------------------------------------------------
# --project-root: a non-empty ABSOLUTE path to an existing directory (D-9)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "root_arg", ["", ".", "relroot", "./relroot", "../neutral-cwd"]
)
def test_relative_or_empty_project_root_is_refused_and_seeds_nothing(
    tmp_path: Path, root_arg: str
) -> None:
    cwd = tmp_path / "neutral-cwd"
    (cwd / "relroot").mkdir(parents=True)
    run = run_seed(["--project-root", root_arg], stdin=KEY, scratch=tmp_path, cwd=cwd)
    assert_fixed_failure(
        run, f"relative root {root_arg!r}", "not an existing directory"
    )
    assert db_files(cwd) == [], "nothing may be seeded in the working directory"
    assert db_files(cwd / "relroot") == [], "nothing may be seeded in a relative target"


def test_repeated_project_root_is_refused(
    ignored_git_repo: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other-root"
    other.mkdir()
    run = run_seed(
        ["--project-root", str(ignored_git_repo), "--project-root", str(other)],
        stdin=KEY,
        scratch=tmp_path,
    )
    assert_fixed_failure(run, "repeated --project-root", "invalid arguments")
    assert db_files(ignored_git_repo) == [] and db_files(other) == [], (
        "a repeated flag must seed nothing"
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unwritable_project_root_is_refused_before_writing(tmp_path: Path) -> None:
    root = tmp_path / "locked-root"
    root.mkdir()
    root.chmod(0o500)
    try:
        run = seed(root, tmp_path)
    finally:
        root.chmod(0o700)
    assert_fixed_failure(run, "unwritable root", "not writable")
    assert db_files(root) == [], "nothing may be created in an unwritable root"


# ---------------------------------------------------------------------------
# stdin edge cases
# ---------------------------------------------------------------------------


def test_closed_stdin_is_a_fixed_failure(
    ignored_git_repo: Path, tmp_path: Path
) -> None:
    run = run_seed_shell_stdin(
        "<&-", ["--project-root", str(ignored_git_repo)], scratch=tmp_path
    )
    assert_fixed_failure(run, "closed stdin", "stdin")
    assert db_files(ignored_git_repo) == [], "closed stdin must seed nothing"


def test_unreadable_stdin_is_a_fixed_failure(
    ignored_git_repo: Path, tmp_path: Path
) -> None:
    run = run_seed_shell_stdin(
        '0>"$TARGET"',
        ["--project-root", str(ignored_git_repo)],
        scratch=tmp_path,
        target=tmp_path
        / "write-only-stdin",  # fd 0 opened write-only: read(2) fails with EBADF
    )
    assert_fixed_failure(run, "stdin read error", "could not read")
    assert db_files(ignored_git_repo) == [], "a read error must seed nothing"


def test_4097_byte_stdin_containing_the_key_is_refused(
    ignored_git_repo: Path, tmp_path: Path
) -> None:
    payload = KEY + "A" * (4097 - len(KEY))
    assert len(payload.encode()) == 4097, "precondition: exactly one byte over the cap"
    run = run_seed(
        ["--project-root", str(ignored_git_repo)], stdin=payload, scratch=tmp_path
    )
    assert_fixed_failure(run, "4097-byte stdin", "too long")
    assert db_files(ignored_git_repo) == [], "an over-long key must seed nothing"


def test_4096_byte_key_is_accepted(ignored_git_repo: Path, tmp_path: Path) -> None:
    payload = KEY + "A" * (4096 - len(KEY))
    run = run_seed(
        ["--project-root", str(ignored_git_repo)], stdin=payload, scratch=tmp_path
    )
    assert run.returncode == 0, "a key of exactly the cap must be accepted"
    assert stored_key(ignored_git_repo) == payload, "the full key must be stored"


def test_invalid_utf8_on_stdin_is_refused(
    ignored_git_repo: Path, tmp_path: Path
) -> None:
    run = run_seed(
        ["--project-root", str(ignored_git_repo)],
        stdin=KEY.encode() + b"\xff\xfe\n",
        scratch=tmp_path,
    )
    assert_fixed_failure(run, "invalid UTF-8", "utf-8")
    assert db_files(ignored_git_repo) == [], "invalid UTF-8 must seed nothing"


# ---------------------------------------------------------------------------
# Symlinks and special files
# ---------------------------------------------------------------------------


def _outside(tmp_path: Path, name: str, mode: int) -> Path:
    target = tmp_path / "outside" / name
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(b"precious")
    target.chmod(mode)
    return target


@pytest.mark.parametrize("link_name", DB_NAMES)
def test_symlinked_database_file_is_refused_and_target_untouched(
    tmp_path: Path, link_name: str
) -> None:
    root = tmp_path / "plain-project"
    root.mkdir()
    target = _outside(tmp_path, "victim", 0o644)
    (root / link_name).symlink_to(target)
    run = seed(root, tmp_path)
    assert_fixed_failure(run, f"symlinked {link_name}", "symlink")
    assert target.read_bytes() == b"precious", "the link target must not be written"
    assert mode_of(target) == 0o644, "the link target's mode must not be changed"
    others = [n for n in db_files(root) if n != link_name]
    assert others == [], "no other .neuroloom.db* file may be created"


def test_dangling_symlinked_database_is_refused_and_target_not_created(
    tmp_path: Path,
) -> None:
    root = tmp_path / "plain-project"
    root.mkdir()
    target = tmp_path / "outside-dangling" / "never-created"
    (root / ".neuroloom.db").symlink_to(target)
    run = seed(root, tmp_path)
    assert_fixed_failure(run, "dangling symlinked db", "symlink")
    assert not target.parent.exists(), "the dangling link target must not be created"


def test_hard_linked_database_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "plain-project"
    root.mkdir()
    target = _outside(tmp_path, "victim", 0o644)
    os.link(target, root / ".neuroloom.db")
    run = seed(root, tmp_path)
    assert_fixed_failure(run, "hard-linked db", "single")
    assert target.read_bytes() == b"precious", "the shared inode must not be written"
    assert mode_of(target) == 0o644, "the shared inode's mode must not change"


def test_database_that_is_a_directory_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "plain-project"
    (root / ".neuroloom.db-wal").mkdir(parents=True)
    run = seed(root, tmp_path)
    assert_fixed_failure(run, "directory sidecar", "regular file")


# ---------------------------------------------------------------------------
# git hardening
# ---------------------------------------------------------------------------


def test_recording_stub_sees_hardening_flags_and_a_minimal_environment(
    needs_git: None, tmp_path: Path
) -> None:
    real_git = shutil.which("git")
    assert real_git, "precondition: a real git is on PATH"
    log = tmp_path / "git-calls.log"
    envlog = tmp_path / "git-env.log"
    repo = ci_support.make_ignored_git_repo(tmp_path / "hardened-repo")
    stub_dir = make_stub_git(
        tmp_path / "recording-bin",
        f'echo "LC_ALL=$LC_ALL" >> "{envlog}"\n'
        f'env | cut -d= -f1 >> "{envlog}"\n'
        f'for a in "$@"; do echo "$a"; done >> "{log}"\n'
        f'exec "{real_git}" "$@"',
    )
    run = seed(
        repo,
        tmp_path,
        path_dirs=[stub_dir],
        env_extra={
            "GIT_DIR": str(tmp_path / "elsewhere.git"),
            "GIT_WORK_TREE": str(tmp_path / "elsewhere"),
            "GIT_INDEX_FILE": str(tmp_path / "elsewhere-index"),
        },
    )
    # GIT_DIR points nowhere, so a script that forwarded it would make git fail.
    assert run.returncode == 0, (
        "the ignore check must not depend on inherited GIT_* names"
    )
    args = log.read_text(encoding="utf-8").splitlines()
    assert "core.fsmonitor=false" in args, "git must run with core.fsmonitor=false"
    assert "core.hooksPath=/dev/null" in args, (
        "git must run with core.hooksPath=/dev/null"
    )
    assert "core.excludesFile=/dev/null" in args, (
        "git must run with core.excludesFile=/dev/null"
    )
    names = envlog.read_text(encoding="utf-8").splitlines()
    assert "LC_ALL=C" in names, "LC_ALL=C must reach git"
    leaked = sorted(
        n
        for n in names
        if n.startswith("GIT_") or n in {"LANG", "PYTHONDONTWRITEBYTECODE"}
    )
    assert leaked == [], "only PATH, HOME and LC_ALL may be passed to git"


def test_fsmonitor_hook_in_repo_config_does_not_run(
    needs_git: None, tmp_path: Path
) -> None:
    """Behavioural guard.  On git 2.33 ``check-ignore`` does not refresh the index, so
    this passes even unhardened; the recording-stub test asserts the flags."""
    repo = ci_support.make_ignored_git_repo(tmp_path / "fsmonitor-repo")
    marker = tmp_path / "fsmonitor-ran"
    hook = tmp_path / "fsmonitor.sh"
    hook.write_text(f'#!/bin/sh\ntouch "{marker}"\nprintf "\\0"\n', encoding="utf-8")
    hook.chmod(0o755)
    cfg = ci_support.git(repo, "config", "core.fsmonitor", str(hook))
    assert cfg.returncode == 0, "precondition: the fsmonitor setting is written"
    # Arm it: a refresh under the setting writes the fsmonitor index extension,
    # so a later git command that refreshes the index runs the script again.
    armed = ci_support.git(repo, "update-index", "--fsmonitor", "--refresh")
    assert armed.returncode == 0, "precondition: the index is fsmonitor-enabled"
    assert marker.exists(), "precondition: git does run the script when unhardened"
    marker.unlink()
    run = seed(repo, tmp_path)
    assert run.returncode == 0, "the seed must still succeed"
    assert not marker.exists(), "the repository's fsmonitor script must not run"


def test_inherited_git_dir_cannot_bypass_the_ignore_check(
    needs_git: None, tmp_path: Path
) -> None:
    good = ci_support.make_ignored_git_repo(tmp_path / "good-repo")
    bad = make_repo(tmp_path / "bad-repo", "node_modules/\n")
    run = seed(
        bad,
        tmp_path,
        env_extra={"GIT_DIR": str(good / ".git"), "GIT_WORK_TREE": str(good)},
    )
    assert_fixed_failure(run, "GIT_DIR pointing at another repo", "does not ignore")
    assert db_files(bad) == [], "the un-ignored repo must stay unseeded"


def test_git_that_cannot_start_fails_closed(needs_git: None, tmp_path: Path) -> None:
    repo = ci_support.make_ignored_git_repo(tmp_path / "unstartable-repo")
    stub_dir = tmp_path / "unstartable-bin"
    stub_dir.mkdir()
    stub = stub_dir / "git"
    # Executable (so PATH lookup finds it) but its interpreter does not exist,
    # so exec fails with an OSError.
    stub.write_text("#!/nonexistent/interpreter\n", encoding="utf-8")
    stub.chmod(0o755)
    run = seed(repo, tmp_path, path_dirs=[stub_dir])
    assert_fixed_failure(run, "git cannot start", "could not be run")
    assert db_files(repo) == [], "a fail-closed refusal must create no file"


# ---------------------------------------------------------------------------
# In-process runs (monkeypatching needs the module loaded here)
# ---------------------------------------------------------------------------


@pytest.fixture()
def seed_mod() -> Iterator[ModuleType]:
    """Load the script as a module; restore the umask it sets."""
    _require_script()
    spec = importlib.util.spec_from_file_location("seed_project_key_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    old_umask = os.umask(0o022)
    os.umask(old_umask)
    try:
        yield module
    finally:
        os.umask(old_umask)


def _inproc(
    mod: ModuleType, monkeypatch: pytest.MonkeyPatch, root: Path, key: str = KEY
) -> int:
    monkeypatch.setattr(
        sys, "stdin", io.TextIOWrapper(io.BytesIO(key.encode() + b"\n"))
    )
    try:
        return int(mod.main(["--project-root", str(root)]))
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 1


def test_git_timeout_fails_closed(
    seed_mod: ModuleType,
    needs_git: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = ci_support.make_ignored_git_repo(tmp_path / "timeout-repo")
    stub_dir = make_stub_git(tmp_path / "sleep-bin", "exec /bin/sleep 20")
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(seed_mod, "_GIT_TIMEOUT_S", 0.5)
    code = _inproc(seed_mod, monkeypatch, repo)
    captured = capsys.readouterr()
    run = SeedRun(code, captured.out, captured.err)
    assert_fixed_failure(run, "git timeout", "could not be run")
    assert "timed out" in run.stderr, "the timeout message must say so"
    assert db_files(repo) == [], "a timed-out check must create no file"


def test_chmod_failure_is_a_fixed_failure(
    seed_mod: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "plain-project"
    root.mkdir()
    (root / ".neuroloom.db").write_bytes(b"")
    real_chmod = os.chmod

    def failing_chmod(path: object, *args: object, **kwargs: object) -> None:
        if ".neuroloom.db" in str(path):
            raise PermissionError(1, "denied by test")
        real_chmod(path, *args, **kwargs)  # type: ignore[arg-type]

    def failing_fchmod(fd: int, mode: int) -> None:
        raise PermissionError(1, "denied by test")

    monkeypatch.setattr(os, "chmod", failing_chmod)
    monkeypatch.setattr(os, "fchmod", failing_fchmod)
    code = _inproc(seed_mod, monkeypatch, root)
    captured = capsys.readouterr()
    run = SeedRun(code, captured.out, captured.err)
    assert_fixed_failure(run, "chmod failure", "0600")
    assert stored_key(root) is None, "no key may be stored when the mode cannot be held"


def test_every_existing_db_file_is_0600_when_the_save_is_called(
    seed_mod: ModuleType,
    ignored_git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """chmod happens BEFORE open_db: at the save call, no file is wider than 0600."""
    assert seed(ignored_git_repo, ignored_git_repo.parent, OLD_KEY).returncode == 0, (
        "setup seed must succeed"
    )
    for name in DB_NAMES[1:]:
        (ignored_git_repo / name).write_bytes(b"")
    for name in DB_NAMES:
        (ignored_git_repo / name).chmod(0o644)
    seen: list[tuple[str, int]] = []
    real_save = seed_mod.save_project_api_key

    def recorder(root: Path, key: str) -> None:
        for p in sorted(root.iterdir()):
            if p.name.startswith(".neuroloom.db"):
                seen.append((p.name, mode_of(p)))
        real_save(root, key)

    monkeypatch.setattr(seed_mod, "save_project_api_key", recorder)
    assert _inproc(seed_mod, monkeypatch, ignored_git_repo) == 0, (
        "the seed must succeed"
    )
    assert len(seen) == 4, "precondition: all four files existed at the save call"
    wide = [name for name, mode in seen if mode != 0o600]
    assert wide == [], "every existing .neuroloom.db* file must be 0600 before the save"


# ---------------------------------------------------------------------------
# Round 2: PATH hygiene, mode skip, check-to-open races, real-shaped key
# ---------------------------------------------------------------------------


def test_forty_char_lowercase_hex_key_is_accepted(
    ignored_git_repo: Path, tmp_path: Path
) -> None:
    assert len(HEX_KEY) == 40, "precondition: the fixture key has the real shape"
    run = seed(ignored_git_repo, tmp_path, HEX_KEY)
    assert run.returncode == 0, "a 40-character lowercase hex key must be accepted"
    assert stored_key(ignored_git_repo) == HEX_KEY, "the hex key must be stored"
    leaked = HEX_KEY in run.stdout or HEX_KEY in run.stderr
    assert not leaked, "the hex key must not appear in any output stream"


def _real_path_dirs() -> list[str]:
    dirs = [str(Path(sys.executable).parent)]
    real_git = shutil.which("git")
    assert real_git, "precondition: a real git is on PATH"
    dirs.append(os.path.dirname(real_git))
    return dirs


@pytest.mark.parametrize("lead", [".", "", "relative-bin"])
def test_relative_or_empty_path_entries_never_run_a_git_from_the_cwd(
    needs_git: None, ignored_git_repo: Path, tmp_path: Path, lead: str
) -> None:
    cwd = tmp_path / "neutral-cwd"
    marker = tmp_path / "cwd-git-ran"
    make_stub_git(cwd, f': > "{marker}"\nexit 0')
    make_stub_git(cwd / "relative-bin", f': > "{marker}"\nexit 0')
    path = os.pathsep.join([lead, *_real_path_dirs()])
    run = run_seed(
        ["--project-root", str(ignored_git_repo)],
        stdin=KEY,
        scratch=tmp_path,
        cwd=cwd,
        env_extra={"PATH": path},
    )
    assert not marker.exists(), "a git found through a relative PATH entry must not run"
    assert run.returncode == 0, "the real git on the absolute entries must be used"
    assert "skip" not in run.stderr.lower(), (
        "the real git must have run: the ignore check was not skipped"
    )
    assert stored_key(ignored_git_repo) == KEY, "the key must land"


def test_path_with_only_relative_entries_means_git_is_absent(
    ignored_git_repo: Path, tmp_path: Path
) -> None:
    cwd = tmp_path / "neutral-cwd"
    marker = tmp_path / "cwd-git-ran"
    make_stub_git(cwd, f': > "{marker}"\nexit 0')
    run = run_seed(
        ["--project-root", str(ignored_git_repo)],
        stdin=KEY,
        scratch=tmp_path,
        cwd=cwd,
        env_extra={"PATH": "."},
    )
    assert not marker.exists(), "a git in the working directory must not run"
    assert run.returncode == 0, "no usable git means the check is skipped"
    assert "skip" in run.stderr.lower(), "the skip must be announced on stderr"


def test_a_mode_already_0600_is_not_chmodded(
    seed_mod: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A foreign-owned file with a fine mode would EPERM on chmod; skip the call."""
    root = tmp_path / "plain-project"
    root.mkdir()
    for name in (".neuroloom.db", ".neuroloom.db-journal"):
        (root / name).write_bytes(b"")
        (root / name).chmod(0o600)
    real_chmod = os.chmod

    def failing_chmod(path: object, *args: object, **kwargs: object) -> None:
        if ".neuroloom.db" in str(path):
            raise PermissionError(1, "denied by test")
        real_chmod(path, *args, **kwargs)  # type: ignore[arg-type]

    def failing_fchmod(fd: int, mode: int) -> None:
        raise PermissionError(1, "denied by test")

    monkeypatch.setattr(os, "chmod", failing_chmod)
    monkeypatch.setattr(os, "fchmod", failing_fchmod)
    assert _inproc(seed_mod, monkeypatch, root) == 0, (
        "files already at 0600 must not be chmodded"
    )
    assert stored_key(root) == KEY, "the key must land"


def _plant(root: Path, name: str, victim: Path) -> None:
    link = root / name
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(victim)


def _victim(tmp_path: Path) -> tuple[Path, tuple[bytes, int, int]]:
    victim = _outside(tmp_path, "toctou-victim", 0o644)
    info = victim.stat()
    return victim, (victim.read_bytes(), mode_of(victim), info.st_size)


def _unchanged(victim: Path, before: tuple[bytes, int, int]) -> bool:
    return (victim.read_bytes(), mode_of(victim), victim.stat().st_size) == before


@pytest.mark.parametrize("planted", [".neuroloom.db", ".neuroloom.db-wal"])
def test_link_planted_after_the_checks_is_refused_and_the_target_untouched(
    seed_mod: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    planted: str,
) -> None:
    root = tmp_path / "plain-project"
    root.mkdir()
    victim, before = _victim(tmp_path)
    real_prepare = seed_mod._prepare_files

    def plant_then_prepare(path: str) -> None:
        _plant(root, planted, victim)
        real_prepare(path)

    monkeypatch.setattr(seed_mod, "_prepare_files", plant_then_prepare)
    code = _inproc(seed_mod, monkeypatch, root)
    captured = capsys.readouterr()
    run = SeedRun(code, captured.out, captured.err)
    assert_fixed_failure(run, f"{planted} planted after the checks", "symlink")
    assert _unchanged(victim, before), "the link target must be byte-for-byte untouched"
    assert db_files(root) == [planted], (
        "a refused seed must create no .neuroloom.db* file besides the planted one"
    )


def test_lstat_checks_run_again_after_git_and_before_the_file_prepare(
    seed_mod: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "plain-project"
    root.mkdir()
    victim, before = _victim(tmp_path)
    real_check = seed_mod._check_ignored
    prepared: list[bool] = []

    def plant_after_git(path: str) -> None:
        real_check(path)
        _plant(root, ".neuroloom.db-shm", victim)

    def spy_prepare(path: str) -> None:
        prepared.append(True)

    monkeypatch.setattr(seed_mod, "_check_ignored", plant_after_git)
    monkeypatch.setattr(seed_mod, "_prepare_files", spy_prepare)
    code = _inproc(seed_mod, monkeypatch, root)
    captured = capsys.readouterr()
    run = SeedRun(code, captured.out, captured.err)
    assert_fixed_failure(run, "link planted during the git calls", "symlink")
    assert prepared == [], "the last lstat check must refuse before any file is opened"
    assert _unchanged(victim, before), "the link target must be untouched"


def test_link_swapped_in_after_the_write_has_its_own_message(
    seed_mod: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "plain-project"
    root.mkdir()
    victim, _ = _victim(tmp_path)
    real_save = seed_mod.save_project_api_key

    def save_then_plant(path: Path, key: str) -> None:
        real_save(path, key)
        _plant(path, ".neuroloom.db-wal", victim)

    monkeypatch.setattr(seed_mod, "save_project_api_key", save_then_plant)
    code = _inproc(seed_mod, monkeypatch, root)
    captured = capsys.readouterr()
    run = SeedRun(code, captured.out, captured.err)
    assert_fixed_failure(run, "link after the write", "after the write")
    assert "may have been written through it" in run.stderr, (
        "the message must say the key may have gone through the link"
    )


# ---------------------------------------------------------------------------
# Round 3: swap harness (a name swapped inside the prepare window)
# ---------------------------------------------------------------------------

_WAL = ".neuroloom.db-wal"


def _arm_window(
    seed_mod: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    mutate: Callable[[Path], None],
) -> dict[str, int]:
    """Wrap ``_prepare_files``: the first lstat of -wal (after it returns) or the
    first open of -wal (before it runs) triggers ``mutate``, i.e. the swap lands
    after any lstat-style vetting and before the file is used."""
    state = {"swapped": 0}
    target = str(root / _WAL)
    real_prepare = seed_mod._prepare_files
    real_lstat = os.lstat
    real_open = os.open

    def swap_once() -> None:
        if not state["swapped"]:
            state["swapped"] = 1
            mutate(root / _WAL)

    def fake_lstat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        result = real_lstat(path, *args, **kwargs)  # type: ignore[arg-type]
        if str(path) == target:
            swap_once()
        return result

    def fake_open(path: object, *args: object, **kwargs: object) -> int:
        if str(path) == target:
            swap_once()
        return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

    def wrapped(path: str) -> object:
        monkeypatch.setattr(os, "lstat", fake_lstat)
        monkeypatch.setattr(os, "open", fake_open)
        try:
            return real_prepare(path)
        finally:
            monkeypatch.setattr(os, "lstat", real_lstat)
            monkeypatch.setattr(os, "open", real_open)

    monkeypatch.setattr(seed_mod, "_prepare_files", wrapped)
    return state


def _wal_root(tmp_path: Path) -> Path:
    root = tmp_path / "plain-project"
    root.mkdir()
    (root / _WAL).write_bytes(b"")
    (root / _WAL).chmod(0o644)
    return root


def _run_window(
    seed_mod: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    root: Path,
    mutate: Callable[[Path], None],
) -> tuple[SeedRun, dict[str, int]]:
    state = _arm_window(seed_mod, monkeypatch, root, mutate)
    code = _inproc(seed_mod, monkeypatch, root)
    captured = capsys.readouterr()
    return SeedRun(code, captured.out, captured.err), state


def test_symlink_swapped_in_after_the_lstat_cannot_be_chmodded_through(
    seed_mod: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A 0644 -wal becomes a link to a 0644 victim right after its lstat.

    ``os.supports_follow_symlinks`` loses chmod (Linux has no lchmod), so any
    path-based chmod would follow the link and narrow the victim.
    """
    root = _wal_root(tmp_path)
    victim, before = _victim(tmp_path)
    monkeypatch.setattr(
        os, "supports_follow_symlinks", os.supports_follow_symlinks - {os.chmod}
    )

    def to_symlink(path: Path) -> None:
        path.unlink()
        path.symlink_to(victim)

    run, state = _run_window(seed_mod, monkeypatch, capsys, root, to_symlink)
    assert state["swapped"] == 1, "precondition: the swap window was reached"
    assert (root / _WAL).is_symlink(), "precondition: -wal is now a symlink"
    assert_fixed_failure(run, "symlink swapped in the window", "symlink")
    assert _unchanged(victim, before), "victim bytes and mode must be untouched"


@pytest.mark.parametrize(
    ("kind", "reason", "absent"),
    [
        ("hardlink", "single link", "not a regular file"),
        ("fifo", "not a regular file", "single link"),
    ],
)
def test_hardlink_or_fifo_swapped_in_the_window_is_refused_by_fstat(
    seed_mod: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
    reason: str,
    absent: str,
) -> None:
    root = _wal_root(tmp_path)
    victim, before = _victim(tmp_path)

    def swap(path: Path) -> None:
        path.unlink()
        if kind == "hardlink":
            os.link(victim, path)
        else:
            os.mkfifo(path)

    run, state = _run_window(seed_mod, monkeypatch, capsys, root, swap)
    assert state["swapped"] == 1, "precondition: the swap window was reached"
    assert_fixed_failure(run, f"{kind} swapped in the window", reason)
    assert absent not in run.stderr.lower(), "the refusal must name the right check"
    assert _unchanged(victim, before), "victim bytes and mode must be untouched"
    assert stored_key(root) is None, "no key may be stored after the refusal"


def test_fstat_oserror_is_a_fixed_failure(
    seed_mod: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _wal_root(tmp_path)
    real_prepare = seed_mod._prepare_files
    real_fstat = os.fstat

    def failing_fstat(fd: int) -> os.stat_result:
        raise OSError(5, "io error from test")

    def wrapped(path: str) -> object:
        monkeypatch.setattr(os, "fstat", failing_fstat)
        try:
            return real_prepare(path)
        finally:
            monkeypatch.setattr(os, "fstat", real_fstat)

    monkeypatch.setattr(seed_mod, "_prepare_files", wrapped)
    code = _inproc(seed_mod, monkeypatch, root)
    captured = capsys.readouterr()
    run = SeedRun(code, captured.out, captured.err)
    assert_fixed_failure(run, "fstat failure", "could not inspect")


def test_main_db_replaced_during_the_write_is_refused(
    seed_mod: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The (st_dev, st_ino) of the vetted main db is compared after the save."""
    root = tmp_path / "plain-project"
    root.mkdir()
    real_save = seed_mod.save_project_api_key

    def save_then_replace(path: Path, key: str) -> None:
        real_save(path, key)
        other = tmp_path / "replacement.db"
        other.write_bytes(b"")
        other.chmod(0o600)
        os.replace(other, path / ".neuroloom.db")

    monkeypatch.setattr(seed_mod, "save_project_api_key", save_then_replace)
    code = _inproc(seed_mod, monkeypatch, root)
    captured = capsys.readouterr()
    run = SeedRun(code, captured.out, captured.err)
    assert_fixed_failure(run, "main db replaced", "replaced during the write")
    assert "may have been written through it" in run.stderr, (
        "the message must say the key may have gone elsewhere"
    )


def test_missing_sidecars_are_precreated_0600_before_the_save(
    seed_mod: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "plain-project"
    root.mkdir()
    seen: dict[str, tuple[bool, int, int]] = {}
    real_save = seed_mod.save_project_api_key

    def recorder(path: Path, key: str) -> None:
        for name in DB_NAMES[1:]:
            p = path / name
            if p.is_file() and not p.is_symlink():
                seen[name] = (True, mode_of(p), p.stat().st_size)
        real_save(path, key)

    monkeypatch.setattr(seed_mod, "save_project_api_key", recorder)
    assert _inproc(seed_mod, monkeypatch, root) == 0, "the seed must succeed"
    assert sorted(seen) == sorted(DB_NAMES[1:]), (
        "-wal, -shm and -journal must exist as regular files at the save call"
    )
    assert all(m == 0o600 for _, m, _ in seen.values()), "pre-created at mode 0600"
    assert stored_key(root) == KEY, "the key must land"


def test_successful_seed_leaves_only_the_main_db(tmp_path: Path) -> None:
    root = tmp_path / "plain-project"
    root.mkdir()
    run = seed(root, tmp_path)
    assert run.returncode == 0, "the seed must succeed with pre-created sidecars"
    assert db_files(root) == [".neuroloom.db"], (
        "SQLite must remove the empty pre-created sidecars on close"
    )
    assert mode_of(root / ".neuroloom.db") == 0o600, "the main db must be 0600"
    assert stored_key(root) == KEY, "the key must land"


@pytest.mark.parametrize("name", DB_NAMES[1:])
def test_a_preexisting_regular_sidecar_is_reused(tmp_path: Path, name: str) -> None:
    root = tmp_path / "plain-project"
    root.mkdir()
    (root / name).write_bytes(b"")
    (root / name).chmod(0o644)
    run = seed(root, tmp_path)
    assert run.returncode == 0, "a leftover regular sidecar must not fail the seed"
    too_wide = [n for n in db_files(root) if not no_wider_than_0600(root / n)]
    assert too_wide == [], "a reused sidecar must be narrowed to 0600"
    assert stored_key(root) == KEY, "the key must land"


# ---------------------------------------------------------------------------
# The key never appears in output, on any failure path
# ---------------------------------------------------------------------------


def test_key_absent_from_output_on_every_subprocess_failure_path(
    needs_git: None, ignored_git_repo: Path, tmp_path: Path
) -> None:
    """One sweep over the main failure paths reachable from a subprocess.

    The rest (including the two that need monkeypatching, chmod failure and git
    timeout) assert ``assert_fixed_failure`` -- which includes the leak check --
    individually in their own tests.
    Each case puts the sentinel key on stdin (or, for the 4097-byte and UTF-8
    cases, inside the payload) so an echo of it would be caught.
    """
    bare = make_repo(tmp_path / "sweep-bare", ".neuroloom.db\n")
    a_file = tmp_path / "sweep-file"
    a_file.write_text("x", encoding="utf-8")
    failing_git = make_stub_git(tmp_path / "sweep-bin", "exit 128")
    blocked = tmp_path / "sweep-blocked"
    blocked.mkdir()
    _blocked_db(blocked, old_key=OLD_KEY)
    linked = tmp_path / "sweep-linked"
    linked.mkdir()
    (linked / ".neuroloom.db").symlink_to(_outside(tmp_path, "sweep-victim", 0o600))
    root_args = ["--project-root", str(ignored_git_repo)]
    cases: list[tuple[str, SeedRun]] = [
        ("unignored sidecars", seed(bare, tmp_path)),
        (
            "missing root",
            run_seed(
                ["--project-root", str(tmp_path / "nope")], stdin=KEY, scratch=tmp_path
            ),
        ),
        (
            "file as root",
            run_seed(["--project-root", str(a_file)], stdin=KEY, scratch=tmp_path),
        ),
        ("empty root", run_seed(["--project-root", ""], stdin=KEY, scratch=tmp_path)),
        (
            "relative root",
            run_seed(["--project-root", "."], stdin=KEY, scratch=tmp_path),
        ),
        ("no root", run_seed([], stdin=KEY, scratch=tmp_path)),
        (
            "flag without value",
            run_seed(["--project-root"], stdin=KEY, scratch=tmp_path),
        ),
        ("positional key", run_seed([*root_args, KEY], stdin=KEY, scratch=tmp_path)),
        (
            "repeated root",
            run_seed([*root_args, *root_args], stdin=KEY, scratch=tmp_path),
        ),
        (
            "abbreviation",
            run_seed(
                ["--project-r", str(ignored_git_repo)], stdin=KEY, scratch=tmp_path
            ),
        ),
        ("spaced key", run_seed(root_args, stdin=SPACED_KEY, scratch=tmp_path)),
        ("NUL in key", run_seed(root_args, stdin=KEY + "\x00x", scratch=tmp_path)),
        ("empty stdin", run_seed(root_args, stdin="", scratch=tmp_path)),
        (
            "4097-byte stdin",
            run_seed(root_args, stdin=KEY + "A" * 4100, scratch=tmp_path),
        ),
        (
            "invalid UTF-8",
            run_seed(root_args, stdin=KEY.encode() + b"\xff", scratch=tmp_path),
        ),
        ("closed stdin", run_seed_shell_stdin("<&-", root_args, scratch=tmp_path)),
        ("git 128", seed(ignored_git_repo, tmp_path, path_dirs=[failing_git])),
        ("write failure (read-back)", seed(blocked, tmp_path)),
        ("symlinked db", seed(linked, tmp_path)),
    ]
    for label, run in cases:
        assert run.returncode == 1, f"sweep {label}: expected exit code 1"
        assert_no_leak(run, f"sweep {label}")
