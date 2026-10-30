"""
D178d Phase 3, Step A: base session_start in CI mode (FR-D1, FR-D6, FR-A1, FR-G1 neighbours).

Written before the implementation and run red against the unmodified hook.
Conventions (plan, "Conventions"):

* The three stdout lines are copied LITERALLY from spec section 5.6.  They are
  never imported from the module under test, so a wording change is a visible
  contract change.
* New names are imported inside test bodies (a missing name is a red test, not
  a collection error).
* ``main()`` runs under ``ci_support.forbid_side_effects()``; the sentinel
  derives from ``BaseException`` so the hook's ``except Exception`` blocks
  cannot swallow it.  A forbidden call is turned into this test's own failure
  message, with only the bare name list printed.
* No assertion reads a mock repr, header or environment.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from pathlib import Path

import pytest

from tests import ci_support

# Spec section 5.6, verbatim.
READY_LINE = "[Neuroloom CI mode] Ready. Memory lookups are on; observations are not uploaded."
DORMANT_LINE = "[Neuroloom CI mode] No project key was seeded, so Neuroloom is off for this job."
UNOPENABLE_LINE = (
    "[Neuroloom CI mode] The project state could not be opened, so observations are not recorded."
)

_SESSION_ID_RE = re.compile(r"^sess-[0-9]+-[a-f0-9]+$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ci_env(monkeypatch: pytest.MonkeyPatch, root: Path, api_base: str, mode: str = "ci") -> None:
    """Point the process at *root* the way Claude Code + the factory would."""
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    monkeypatch.setenv("NEUROLOOM_MODE", mode)
    monkeypatch.setenv("NEUROLOOM_API_BASE", api_base)
    monkeypatch.chdir(root)


def _run_main_forbidden() -> list[str]:
    """Run session_start ``main()`` with the forbid patches active.

    Returns the recorded names of forbidden calls (empty when none).  A
    forbidden call aborts ``main()`` via the sentinel; the caller asserts on the
    returned list with its own message.
    """
    import pyhooks.session_start as ss

    with ci_support.forbid_side_effects() as recorder:
        try:
            ss.main()
        except ci_support.ForbiddenCallError:
            pass
    return list(recorder.calls)


def _seed_key(root: Path, key: str = ci_support.SENTINEL_KEY) -> None:
    from pyhooks.config import save_project_api_key

    root.mkdir(parents=True, exist_ok=True)
    save_project_api_key(root, key)
    assert (root / ".neuroloom.db").is_file(), "fixture error: seeding the key did not create the database"


def _sessions(db_path: Path) -> list[tuple[str, str]]:
    conn = sqlite3.connect(str(db_path))
    try:
        return [(str(r[0]), str(r[1])) for r in conn.execute("SELECT session_key, session_id FROM sessions")]
    finally:
        conn.close()


def _count(db_path: Path, sql: str) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return int(conn.execute(sql).fetchone()[0])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Keyed CI path
# ---------------------------------------------------------------------------


class TestKeyedCiPath:
    def test_ready_line_session_row_and_untouched_buffer(
        self,
        bait_repo: ci_support.Bait,
        black_hole: ci_support.BlackHole,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _ci_env(monkeypatch, bait_repo.root, black_hole.url)
        before_tree = ci_support.tree_hash(bait_repo.root)

        calls = _run_main_forbidden()
        out = capsys.readouterr().out

        assert calls == [], f"CI keyed: session_start made forbidden calls {calls}"
        assert out == READY_LINE + "\n", "CI keyed: stdout must be exactly the ready line plus one newline"

        rows = _sessions(bait_repo.db_path)
        root_key = str(bait_repo.root.resolve())
        assert len(rows) == 1, "CI keyed: exactly one sessions row expected (old row replaced)"
        assert rows[0][0] == root_key, "CI keyed: sessions row must be keyed by str(project_root())"
        assert _SESSION_ID_RE.match(rows[0][1]), "CI keyed: minted id must match ^sess-[0-9]+-[a-f0-9]+$"
        assert rows[0][1] != ci_support._STALE_SESSION_ID, "CI keyed: stale session row was not replaced"

        assert bait_repo.buffer_rows() == bait_repo.seeded_buffer_rows, (
            "CI keyed: event_buffer rows must be left untouched (no flush)"
        )
        assert _count(bait_repo.db_path, "SELECT COUNT(*) FROM event_buffer WHERE payload_type='document'") == 1, (
            "CI keyed: the buffered document row must survive"
        )
        assert _count(bait_repo.db_path, "SELECT COUNT(*) FROM traces") >= 1, (
            "CI keyed: a trace row is expected"
        )
        fired = [name for name, hit in bait_repo.fired().items() if hit]
        assert fired == [], f"CI keyed: bait triggers fired {fired}"
        assert ci_support.tree_hash(bait_repo.root) == before_tree, "CI keyed: tracked files changed"
        assert black_hole.connection_count() == 0, (
            "CI keyed: empty request allowlist violated (a connection reached the API base)"
        )

    def test_ready_line_in_subprocess_through_launcher(
        self,
        bait_repo: ci_support.Bait,
        black_hole: ci_support.BlackHole,
        tmp_path: Path,
    ) -> None:
        before_tree = ci_support.tree_hash(bait_repo.root)
        try:
            run = ci_support.run_hook(
                "pyhooks.session_start",
                cwd=bait_repo.root,
                scratch=tmp_path / "scratch",
                black_hole_url=black_hole.url,
                env_extra={"CLAUDE_PROJECT_DIR": str(bait_repo.root), "NEUROLOOM_MODE": "ci"},
            )
        except subprocess.TimeoutExpired:
            pytest.fail("CI subprocess: session_start did not exit within the 10 s backstop")

        assert run.returncode == 0, "CI subprocess: exit code must be 0"
        assert run.stdout == READY_LINE + "\n", "CI subprocess: stdout must equal the section 5.6 ready line"
        assert "degraded" not in run.stderr, "CI subprocess: launcher printed a degraded-venv line in CI mode"
        assert black_hole.connection_count() == 0, "CI subprocess: connections reached the API base"
        assert ci_support.tree_hash(bait_repo.root) == before_tree, "CI subprocess: files changed"
        fired = [name for name, hit in bait_repo.fired().items() if hit]
        assert fired == [], f"CI subprocess: bait triggers fired {fired}"

    def test_process_lifetime_forbid_patches_stay_silent(
        self,
        bait_repo: ci_support.Bait,
        black_hole: ci_support.BlackHole,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _ci_env(monkeypatch, bait_repo.root, black_hole.url)
        calls = _run_main_forbidden()
        assert calls == [], f"process lifetime: session_start CI started a thread, subprocess or remote call {calls}"


# ---------------------------------------------------------------------------
# Ignored-repository fixture
# ---------------------------------------------------------------------------


class TestIgnoredRepo:
    def test_tracked_files_byte_identical_and_git_status_clean(
        self,
        ignored_git_repo: Path,
        black_hole: ci_support.BlackHole,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = ignored_git_repo
        _seed_key(root)
        gitignore_before = (root / ".gitignore").read_bytes()
        claude_before = (root / "CLAUDE.md").read_bytes()
        assert not (root / ".mcp.json").exists(), "fixture error: .mcp.json should start absent"

        # Hold a connection open so SQLite keeps the -wal and -shm sidecars in
        # place after the hook closes its own; otherwise they vanish on the last
        # close and "ignored sidecars" would be untested.
        holder = sqlite3.connect(str(root / ".neuroloom.db"))
        try:
            holder.execute("SELECT COUNT(*) FROM config").fetchone()
            _ci_env(monkeypatch, root, black_hole.url)
            calls = _run_main_forbidden()
            out = capsys.readouterr().out

            assert calls == [], f"ignored repo: forbidden calls {calls}"
            assert out == READY_LINE + "\n", "ignored repo: ready line expected"
            assert (root / ".gitignore").read_bytes() == gitignore_before, "ignored repo: .gitignore changed"
            assert (root / "CLAUDE.md").read_bytes() == claude_before, "ignored repo: CLAUDE.md changed"
            assert not (root / ".mcp.json").exists(), "ignored repo: .mcp.json was created"

            names = {p.name for p in root.iterdir()}
            assert ".neuroloom.db" in names, "ignored repo: state database missing"
            assert {".neuroloom.db-wal", ".neuroloom.db-shm"} <= names, (
                "fixture error: WAL sidecars are not present, so the ignore rule is untested"
            )
            status = ci_support.git(root, "status", "--porcelain")
            assert status.returncode == 0, "ignored repo: git status failed"
            assert status.stdout == "", "ignored repo: git status --porcelain is not empty"
        finally:
            holder.close()


# ---------------------------------------------------------------------------
# Dormant and unopenable
# ---------------------------------------------------------------------------


class TestDormantAndUnopenable:
    def _run_dormant(
        self,
        root: Path,
        hole: ci_support.BlackHole,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        label: str,
    ) -> None:
        before = ci_support.presence_snapshot(root)
        _ci_env(monkeypatch, root, hole.url)
        calls = _run_main_forbidden()
        out = capsys.readouterr().out
        assert calls == [], f"{label}: forbidden calls {calls}"
        assert out == DORMANT_LINE + "\n", f"{label}: stdout must be exactly the dormant line"
        assert ci_support.presence_snapshot(root) == before, f"{label}: a file was created or removed"

    def test_no_key_no_database(
        self,
        tmp_path: Path,
        black_hole: ci_support.BlackHole,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        self._run_dormant(root, black_hole, monkeypatch, capsys, "dormant, no database")

    def test_directory_at_database_path(
        self,
        tmp_path: Path,
        black_hole: ci_support.BlackHole,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = tmp_path / "proj"
        (root / ".neuroloom.db").mkdir(parents=True)
        (root / ".neuroloom.db" / "keep").write_text("x", encoding="utf-8")
        self._run_dormant(root, black_hole, monkeypatch, capsys, "dormant, directory at db path")

    def test_corrupt_database_file(
        self,
        tmp_path: Path,
        black_hole: ci_support.BlackHole,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        garbage = b"this is not a sqlite database" * 20
        (root / ".neuroloom.db").write_bytes(garbage)
        self._run_dormant(root, black_hole, monkeypatch, capsys, "dormant, corrupt db")
        assert (root / ".neuroloom.db").read_bytes() == garbage, "dormant, corrupt db: the file was modified"

    def test_key_present_but_open_db_fails(
        self,
        tmp_path: Path,
        black_hole: ci_support.BlackHole,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import pyhooks.db as db_mod

        root = tmp_path / "proj"
        _seed_key(root)
        _ci_env(monkeypatch, root, black_hole.url)
        monkeypatch.setattr(db_mod, "open_db", lambda *args, **kwargs: None)

        calls = _run_main_forbidden()
        out = capsys.readouterr().out
        assert calls == [], f"unopenable: forbidden calls {calls}"
        assert out == UNOPENABLE_LINE + "\n", "unopenable: stdout must be exactly the unopenable line"

    def test_failure_after_open_db_succeeds(
        self,
        tmp_path: Path,
        black_hole: ci_support.BlackHole,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The database opens, then the session write is refused by a trigger.

        A trigger is used instead of patching a helper so the test pins behaviour,
        not the name of an internal function.  (A trace-insert failure cannot be
        injected through ``trace.write``, which swallows its own errors; the CI
        path inserts the trace row directly, covered by the next test.)
        """
        root = tmp_path / "proj"
        _seed_key(root)
        conn = sqlite3.connect(str(root / ".neuroloom.db"))
        try:
            conn.execute(
                "CREATE TRIGGER nl_block_insert BEFORE INSERT ON sessions "
                "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
            )
            conn.execute(
                "CREATE TRIGGER nl_block_update BEFORE UPDATE ON sessions "
                "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
            )
            conn.commit()
        finally:
            conn.close()
        _ci_env(monkeypatch, root, black_hole.url)

        calls = _run_main_forbidden()  # a raised exception here also fails the test (exit must be 0)
        captured = capsys.readouterr()
        assert calls == [], f"post-open failure: forbidden calls {calls}"
        assert captured.out == UNOPENABLE_LINE + "\n", (
            "post-open failure: stdout must be exactly the unopenable line"
        )
        assert "Traceback" not in captured.err, "post-open failure: a traceback reached stderr"
        assert _sessions(root / ".neuroloom.db") == [], "post-open failure: a sessions row exists despite the block"

    def test_trace_insert_failure_rolls_back_session(
        self,
        tmp_path: Path,
        black_hole: ci_support.BlackHole,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The session upsert succeeds but the trace insert is refused.

        Both rows commit together, so the uncommitted upsert must be rolled back
        and the unopenable line printed.
        """
        root = tmp_path / "proj"
        _seed_key(root)
        conn = sqlite3.connect(str(root / ".neuroloom.db"))
        try:
            conn.execute(
                "CREATE TRIGGER nl_block_trace BEFORE INSERT ON traces "
                "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
            )
            conn.commit()
        finally:
            conn.close()
        _ci_env(monkeypatch, root, black_hole.url)

        calls = _run_main_forbidden()
        captured = capsys.readouterr()
        assert calls == [], f"trace failure: forbidden calls {calls}"
        assert captured.out == UNOPENABLE_LINE + "\n", "trace failure: stdout must be exactly the unopenable line"
        assert "Traceback" not in captured.err, "trace failure: a traceback reached stderr"
        assert _sessions(root / ".neuroloom.db") == [], "trace failure: the session upsert was not rolled back"


# ---------------------------------------------------------------------------
# Unrecognised mode (spec 6.2)
# ---------------------------------------------------------------------------


ACCEPTED_HINT = "Set NEUROLOOM_MODE=ci, or unset it for normal mode."


class TestUnrecognisedMode:
    @pytest.mark.parametrize("value", ["prod", "ci2", "cii", "off", "false", "cii "])
    def test_one_stderr_line_names_the_value(
        self,
        value: str,
        tmp_path: Path,
        black_hole: ci_support.BlackHole,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        _ci_env(monkeypatch, root, black_hole.url, mode=value)
        _run_main_forbidden()
        err = capsys.readouterr().err
        assert err.count("\n") == 1 and err.endswith("\n"), (
            f"unrecognised mode {value!r}: stderr must be exactly one line"
        )
        assert json.dumps(value.strip()) in err, f"unrecognised mode {value!r}: the line must name the quoted value"
        assert err.rstrip("\n").endswith(ACCEPTED_HINT), "the line must tell the operator the accepted values"

    @pytest.mark.parametrize(
        ("value", "length"),
        [
            ("prod\nsecond line", 16),
            ("x" * 200, 200),
            ("x" * 17, 17),
            ("has space", 9),
        ],
    )
    def test_non_conforming_value_is_redacted(
        self,
        value: str,
        length: int,
        tmp_path: Path,
        black_hole: ci_support.BlackHole,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        assert len(value) == length
        root = tmp_path / "proj"
        root.mkdir()
        _ci_env(monkeypatch, root, black_hole.url, mode=value)
        _run_main_forbidden()
        err = capsys.readouterr().err
        assert err.count("\n") == 1 and err.endswith("\n"), "redacted value: stderr must stay one line"
        assert f"<redacted, {length} chars>" in err, "redacted value: the line must give only the length"
        assert "not recognised" in err, "redacted value: the line must still name the situation"
        assert err.rstrip("\n").endswith(ACCEPTED_HINT), "redacted value: the line must tell the accepted values"
        assert value[:8] not in err, "redacted value: the value text reached stderr"

    def test_secret_looking_value_is_absent_from_stderr(
        self,
        tmp_path: Path,
        black_hole: ci_support.BlackHole,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        secret = "nl_live_" + "A1b2C3d4" * 4  # long, secret-shaped; never a real key
        root = tmp_path / "proj"
        root.mkdir()
        _ci_env(monkeypatch, root, black_hole.url, mode=secret)
        _run_main_forbidden()
        err = capsys.readouterr().err
        assert err.count("\n") == 1, "secret-looking value: stderr must stay one line"
        assert secret not in err and "A1b2C3d4" not in err, "secret-looking value: the value reached stderr"
        assert f"<redacted, {len(secret)} chars>" in err, "secret-looking value: expected the redacted form"

    def test_sixteen_char_conforming_value_is_named(
        self,
        tmp_path: Path,
        black_hole: ci_support.BlackHole,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        value = "a_b-C9" * 2 + "wxyz"  # 16 chars, all in the allowed set
        assert len(value) == 16
        root = tmp_path / "proj"
        root.mkdir()
        _ci_env(monkeypatch, root, black_hole.url, mode=value)
        _run_main_forbidden()
        err = capsys.readouterr().err
        assert json.dumps(value) in err, "16-char conforming value: the value must be named"

    @pytest.mark.parametrize("value", ["ci", "CI", " ci "])
    def test_recognised_values_print_no_message(
        self,
        value: str,
        tmp_path: Path,
        black_hole: ci_support.BlackHole,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = tmp_path / "proj"
        root.mkdir()
        _ci_env(monkeypatch, root, black_hole.url, mode=value)
        _run_main_forbidden()
        assert capsys.readouterr().err == "", f"recognised mode {value!r}: nothing may be printed to stderr"
