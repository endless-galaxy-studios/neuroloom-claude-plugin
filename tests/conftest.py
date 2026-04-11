"""
Shared pytest fixtures for the neuroloom-hooks test suite.

All fixtures are function-scoped by default so each test starts with a clean
state.  The `db_path` fixture always uses `tmp_path` (a real file) rather than
`:memory:` so that WAL mode, file permissions, and connection-close behaviour
are exercised under realistic conditions.
"""

from pathlib import Path

import pytest

import pyhooks.config as _config_mod
import pyhooks.db as _db_mod


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
        debug=False,
    )
