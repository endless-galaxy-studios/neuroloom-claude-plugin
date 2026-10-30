"""
Shared pytest fixtures for the neuroloom-hooks test suite.

All fixtures are function-scoped by default so each test starts with a clean
state.  The `db_path` fixture always uses `tmp_path` (a real file) rather than
`:memory:` so that WAL mode, file permissions, and connection-close behaviour
are exercised under realistic conditions.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

import pyhooks.config as _config_mod
import pyhooks.db as _db_mod
from tests import ci_support


@pytest.fixture(autouse=True)
def _no_ambient_project_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove CLAUDE_PROJECT_DIR so the host environment cannot leak into a test.

    Claude Code exports it to hook processes, so it can be set when this suite
    is run from inside a session.  It defines the containment root
    (``pyhooks.containment.project_root``); a test that wants one sets it
    explicitly, after this fixture has cleared it.
    """
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)


@pytest.fixture(autouse=True)
def _no_ambient_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove NEUROLOOM_MODE so an ambient CI-mode switch cannot leak into a test.

    A test that wants CI mode sets it explicitly, after this fixture has run.
    """
    monkeypatch.delenv("NEUROLOOM_MODE", raising=False)


# test_workspace_config.py has an opt-in live smoke test whose collection-time
# skipif checks NEUROLOOM_SLEEVED_API_KEY.  Drop the key at conftest import (before
# any test module is collected) so that test skips instead of making a live network
# call from a CI run.  NEUROLOOM_RUN_LIVE_SMOKE=1 is the explicit opt-in.
if os.environ.get("NEUROLOOM_RUN_LIVE_SMOKE") != "1":
    os.environ.pop("NEUROLOOM_SLEEVED_API_KEY", None)


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip credential-shaped environment variables (KEY, TOKEN, SECRET, PASSWORD,
    CREDENTIAL, BEARER in the name) so a developer's real secrets never reach a
    hook under test.  A test that needs one sets it explicitly afterwards.
    """
    for name in ci_support.credential_env_names(os.environ):
        if name == "NEUROLOOM_SLEEVED_API_KEY" and os.environ.get("NEUROLOOM_RUN_LIVE_SMOKE") == "1":
            continue
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def black_hole() -> Iterator[ci_support.BlackHole]:
    """A loopback listener that counts connections and never answers."""
    hole = ci_support.BlackHole()
    try:
        yield hole
    finally:
        hole.close()


@pytest.fixture()
def ignored_git_repo(tmp_path: Path) -> Path:
    """A git repository whose .gitignore covers ``.neuroloom.db*``."""
    if not ci_support.git_available():
        pytest.skip("git is not available")
    return ci_support.make_ignored_git_repo(tmp_path / "ignored-repo")


@pytest.fixture()
def bait_repo(tmp_path: Path) -> ci_support.Bait:
    """A repository an unsafe session_start would visibly change (see ci_support)."""
    return ci_support.make_bait_repo(tmp_path / "bait")


@pytest.fixture()
def forbid() -> Iterator[ci_support.ForbidRecorder]:
    """Forbid thread/subprocess/non-loopback network use for the whole test.

    Prefer ``ci_support.forbid_side_effects()`` around ``main()`` only, so test
    servers can start first; this fixture is for tests that need it throughout.
    """
    with ci_support.forbid_side_effects() as recorder:
        yield recorder


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """Return a path inside ``tmp_path`` where the test database will live.

    The file does not exist yet — ``open_db`` creates it on first call.
    """
    return tmp_path / ".neuroloom.db"


@pytest.fixture()
def db_conn(db_path: Path):  # type: ignore[no-untyped-def]
    """Open the test database, yield the connection, close it in teardown.

    Uses ``hooks.db.open_db`` so WAL mode and the full schema are applied
    exactly as they would be in production.
    """
    conn = _db_mod.open_db(db_path)
    assert conn is not None, "open_db returned None for a writable tmp_path"
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def mock_config(db_path: Path) -> _config_mod.Config:
    """Return a ``Config`` wired to the test database and a localhost API base.

    The ``api_key`` value is deliberately chosen so that repr/str tests can
    verify it does not leak: ``"test-key-abc123"`` does not match any default
    value returned by ``config.load()``.
    """
    return _config_mod.Config(
        api_key="test-key-abc123",
        api_base="http://localhost:19999",
        state_db_path=db_path,
    )
