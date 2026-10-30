"""
D178d Phase 3 follow-up: base session_start in CI mode when the project root
cannot be determined (deleted working directory, no CLAUDE_PROJECT_DIR).

Decision: ``config.load()`` already reports that case as "no key", so the CI
path prints the dormant line and opens nothing.  If the root only fails AFTER a
key was read (a race), the state cannot be recorded, so the unopenable line is
printed.  Either way: exit 0, no traceback.  Lines are copied from spec 5.6.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests import ci_support

DORMANT_LINE = "[Neuroloom CI mode] No project key was seeded, so Neuroloom is off for this job."
UNOPENABLE_LINE = (
    "[Neuroloom CI mode] The project state could not be opened, so observations are not recorded."
)


def test_deleted_cwd_without_project_dir_is_dormant(
    tmp_path: Path,
    black_hole: ci_support.BlackHole,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import pyhooks.session_start as ss

    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.setenv("NEUROLOOM_MODE", "ci")
    monkeypatch.setenv("NEUROLOOM_API_BASE", black_hole.url)

    ss.main()
    captured = capsys.readouterr()
    assert captured.out == DORMANT_LINE + "\n", "deleted cwd: stdout must be exactly the dormant line"
    assert captured.err == "", "deleted cwd: nothing may reach stderr"
    assert black_hole.connection_count() == 0, "deleted cwd: the black hole saw a connection"


def test_root_failing_after_key_read_is_unopenable(
    tmp_path: Path,
    black_hole: ci_support.BlackHole,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import pyhooks.containment as containment
    import pyhooks.session_start as ss
    from pyhooks.config import save_project_api_key

    root = tmp_path / "proj"
    root.mkdir()
    save_project_api_key(root, ci_support.SENTINEL_KEY)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    monkeypatch.setenv("NEUROLOOM_MODE", "ci")
    monkeypatch.setenv("NEUROLOOM_API_BASE", black_hole.url)
    real = containment.project_root
    calls = {"n": 0}

    def flaky() -> Path:
        calls["n"] += 1
        if calls["n"] > 1:  # load() succeeds; the CI path's own call fails
            raise OSError("cwd vanished")
        return real()

    monkeypatch.setattr(containment, "project_root", flaky)

    ss.main()
    captured = capsys.readouterr()
    assert captured.out == UNOPENABLE_LINE + "\n", "root failure after key read: stdout must be the unopenable line"
    assert "Traceback" not in captured.err, "root failure after key read: a traceback reached stderr"


def test_cd_into_subdirectory_still_records_the_session_under_the_root(
    tmp_path: Path,
    black_hole: ci_support.BlackHole,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Root pinning: CLAUDE_PROJECT_DIR wins over the process cwd after a ``cd``."""
    import sqlite3

    import pyhooks.session_start as ss
    from pyhooks.config import save_project_api_key

    root = tmp_path / "proj"
    sub = root / "sub"
    sub.mkdir(parents=True)
    save_project_api_key(root, ci_support.SENTINEL_KEY)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    monkeypatch.setenv("NEUROLOOM_MODE", "ci")
    monkeypatch.setenv("NEUROLOOM_API_BASE", black_hole.url)
    monkeypatch.chdir(sub)

    ss.main()

    out = capsys.readouterr().out
    assert out.startswith("[Neuroloom CI mode] Ready."), "cd root pinning: the ready line was not printed"
    assert not (sub / ".neuroloom.db").exists(), "cd root pinning: a database appeared in the subdirectory"
    conn = sqlite3.connect(str(root / ".neuroloom.db"))
    try:
        keys = [str(r[0]) for r in conn.execute("SELECT session_key FROM sessions")]
    finally:
        conn.close()
    assert keys == [str(root.resolve())], f"cd root pinning: session keyed {keys}, expected the root"
