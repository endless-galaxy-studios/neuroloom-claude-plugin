"""
Tests for hooks/session_start.py.

Covers: no-API-key guard, stale session ending, corrupt session handling,
session ID format, event-buffer flush, .gitignore management, CLAUDE.md
injection, trace pruning, and session start failure.
"""

import io
import re
import subprocess
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import pyhooks.db as _db_mod
import pyhooks.session_start as _ss_mod

# Save a reference to the real open_db before any patches can shadow it.
_real_open_db = _db_mod.open_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(db_path: Path, api_base: str = "http://localhost:19999") -> Any:
    """Return a Config pointed at the test DB and a mock API base."""
    import pyhooks.config as _config_mod

    return _config_mod.Config(
        api_key="test-key-abc123",
        api_base=api_base,
        state_db_path=db_path,
        debug=False,
    )


def _run_session_start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    db_path: Path,
    api_key: str = "test-key-abc123",
    start_response: tuple[int, bytes] | None = (200, b'{"session_id":"sess-1-aabb"}'),
    end_response: tuple[int, bytes] | None = (200, b"{}"),
    flush_response: tuple[int, bytes] | None = (200, b"{}"),
    pypi_response: tuple[int, bytes] | None = None,
) -> str:
    """
    Run ``main()`` with a fully controlled environment.

    Returns the captured stdout text.
    """
    monkeypatch.chdir(tmp_path)

    output = io.StringIO()

    def _fake_post(
        url: str,
        headers: dict[str, str],
        payload: bytes,
        timeout: float,
    ) -> tuple[int, bytes] | None:
        if "sessions/start" in url:
            return start_response
        if "/end" in url:
            return end_response
        if "observations/batch" in url:
            return flush_response
        if "pypi.org" in url:
            return pypi_response
        return None

    with (
        patch("pyhooks.session_start._config.load") as mock_load,
        patch("pyhooks.session_start._db.open_db") as mock_open_db,
        patch("pyhooks.session_start._http.post_json", side_effect=_fake_post),
        patch("pyhooks.session_start.sys.stdout", output),
        # Prevent the codeweaver bootstrap/upgrade thread from making real network calls
        patch("pyhooks.session_start._codeweaver_bootstrap_and_upgrade", return_value=None),
    ):
        import pyhooks.config as _config_mod

        mock_load.return_value = _config_mod.Config(
            api_key=api_key,
            api_base="http://localhost:19999",
            state_db_path=db_path,
            debug=False,
        )
        mock_open_db.side_effect = _real_open_db

        _ss_mod.main()

    return output.getvalue()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNoApiKey:
    """Without an API key the hook prints setup instructions and exits cleanly."""

    def test_no_key_message_printed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db_path = tmp_path / ".neuroloom.db"
        output = io.StringIO()

        with (
            patch("pyhooks.session_start._config.load") as mock_load,
            patch("pyhooks.session_start._db.open_db") as mock_open_db,
            patch("pyhooks.session_start.sys.stdout", output),
        ):
            import pyhooks.config as _config_mod

            mock_load.return_value = _config_mod.Config(
                api_key="",
                api_base="http://localhost:19999",
                state_db_path=db_path,
                debug=False,
            )
            mock_open_db.side_effect = _real_open_db
            _ss_mod.main()

        text = output.getvalue()
        assert "No API key configured" in text

    def test_no_key_does_not_call_api(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / ".neuroloom.db"
        post_calls: list[str] = []

        def _record_post(
            url: str, headers: dict[str, str], payload: bytes, timeout: float
        ) -> tuple[int, bytes]:
            post_calls.append(url)
            return (200, b"{}")

        with (
            patch("pyhooks.session_start._config.load") as mock_load,
            patch("pyhooks.session_start._db.open_db") as mock_open_db,
            patch("pyhooks.session_start._http.post_json", side_effect=_record_post),
            patch("pyhooks.session_start.sys.stdout", io.StringIO()),
        ):
            import pyhooks.config as _config_mod

            mock_load.return_value = _config_mod.Config(
                api_key="",
                api_base="http://localhost:19999",
                state_db_path=db_path,
                debug=False,
            )
            mock_open_db.side_effect = _real_open_db
            _ss_mod.main()

        assert post_calls == []


class TestStaleSession:
    """A pre-existing sessions row triggers a POST to the sessions/end endpoint."""

    def test_stale_session_ended(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db_path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(db_path)
        assert conn is not None

        workspace_key = str(tmp_path.resolve())
        old_sid = "sess-1000000000-aabbccdd"
        conn.execute(
            """
            INSERT INTO sessions (session_key, session_id, started_at, last_submit_ms)
            VALUES (?, ?, datetime('now'), 0)
            """,
            (workspace_key, old_sid),
        )
        conn.commit()
        conn.close()

        end_called: list[str] = []

        def _fake_post(
            url: str, headers: dict[str, str], payload: bytes, timeout: float
        ) -> tuple[int, bytes] | None:
            if "/end" in url:
                end_called.append(url)
                return (200, b"{}")
            return (200, b'{"session_id":"sess-2-ccdd"}')

        with (
            patch("pyhooks.session_start._config.load") as mock_load,
            patch("pyhooks.session_start._db.open_db") as mock_open_db,
            patch("pyhooks.session_start._http.post_json", side_effect=_fake_post),
            patch("pyhooks.session_start.sys.stdout", io.StringIO()),
            patch("pyhooks.session_start._codeweaver_bootstrap_and_upgrade", return_value=None),
        ):
            import pyhooks.config as _config_mod

            monkeypatch.chdir(tmp_path)
            mock_load.return_value = _config_mod.Config(
                api_key="test-key",
                api_base="http://localhost:19999",
                state_db_path=db_path,
                debug=False,
            )
            mock_open_db.side_effect = _real_open_db
            _ss_mod.main()

        assert any("/end" in url for url in end_called), "Expected a POST to sessions/<id>/end"


class TestCorruptStaleSession:
    """A stale session with an invalid session_id is deleted with a corrupt_session trace."""

    def test_corrupt_session_deleted_and_traced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(db_path)
        assert conn is not None

        workspace_key = str(tmp_path.resolve())
        conn.execute(
            """
            INSERT INTO sessions (session_key, session_id, started_at, last_submit_ms)
            VALUES (?, ?, datetime('now'), 0)
            """,
            (workspace_key, "INVALID-SESSION"),
        )
        conn.commit()
        conn.close()

        post_calls: list[str] = []

        def _fake_post(
            url: str, headers: dict[str, str], payload: bytes, timeout: float
        ) -> tuple[int, bytes] | None:
            post_calls.append(url)
            if "sessions/start" in url:
                return (200, b'{"session_id":"sess-3-eeff"}')
            return (200, b"{}")

        with (
            patch("pyhooks.session_start._config.load") as mock_load,
            patch("pyhooks.session_start._db.open_db") as mock_open_db,
            patch("pyhooks.session_start._http.post_json", side_effect=_fake_post),
            patch("pyhooks.session_start.sys.stdout", io.StringIO()),
            patch("pyhooks.session_start._codeweaver_bootstrap_and_upgrade", return_value=None),
        ):
            import pyhooks.config as _config_mod

            monkeypatch.chdir(tmp_path)
            mock_load.return_value = _config_mod.Config(
                api_key="test-key",
                api_base="http://localhost:19999",
                state_db_path=db_path,
                debug=False,
            )
            mock_open_db.side_effect = _real_open_db
            _ss_mod.main()

        # No /end call for corrupt sessions
        assert not any("/end" in url for url in post_calls), (
            "Should not POST to /end for corrupt session"
        )

        conn2 = _db_mod.open_db(db_path)
        assert conn2 is not None
        try:
            trace = conn2.execute(
                "SELECT decision FROM traces WHERE decision = 'corrupt_session'"
            ).fetchone()
            assert trace is not None, "Expected corrupt_session trace row"

            row = conn2.execute(
                "SELECT * FROM sessions WHERE session_key = ?",
                (workspace_key,),
            ).fetchone()
            # The corrupt row may have been replaced by a new valid session
            # OR deleted — either way the new session_id must match the pattern.
            if row is not None:
                new_sid = str(row["session_id"])
                assert re.match(r"^sess-[0-9]+-[a-f0-9]+$", new_sid), (
                    f"New session_id {new_sid!r} does not match expected pattern"
                )
        finally:
            conn2.close()


class TestSessionStartFailure:
    """When the API returns 500, no session row should be persisted."""

    def test_no_session_row_on_500(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db_path = tmp_path / ".neuroloom.db"

        with (
            patch("pyhooks.session_start._config.load") as mock_load,
            patch("pyhooks.session_start._db.open_db") as mock_open_db,
            patch(
                "pyhooks.session_start._http.post_json",
                return_value=(500, b'{"error":"internal"}'),
            ),
            patch("pyhooks.session_start.sys.stdout", io.StringIO()),
            patch("pyhooks.session_start._codeweaver_bootstrap_and_upgrade", return_value=None),
        ):
            import pyhooks.config as _config_mod

            monkeypatch.chdir(tmp_path)
            mock_load.return_value = _config_mod.Config(
                api_key="test-key",
                api_base="http://localhost:19999",
                state_db_path=db_path,
                debug=False,
            )
            mock_open_db.side_effect = _real_open_db
            _ss_mod.main()

        conn = _db_mod.open_db(db_path)
        assert conn is not None
        try:
            count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            assert count == 0, "Expected no session row after a 500 start failure"
        finally:
            conn.close()


class TestSessionIdFormat:
    """The generated session_id must match ``sess-<timestamp>-<hex>``."""

    def test_session_id_matches_pattern(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / ".neuroloom.db"

        with (
            patch("pyhooks.session_start._config.load") as mock_load,
            patch("pyhooks.session_start._db.open_db") as mock_open_db,
            patch(
                "pyhooks.session_start._http.post_json",
                return_value=(200, b'{"session_id":"sess-ok"}'),
            ),
            patch("pyhooks.session_start.sys.stdout", io.StringIO()),
            patch("pyhooks.session_start._codeweaver_bootstrap_and_upgrade", return_value=None),
        ):
            import pyhooks.config as _config_mod

            monkeypatch.chdir(tmp_path)
            mock_load.return_value = _config_mod.Config(
                api_key="test-key",
                api_base="http://localhost:19999",
                state_db_path=db_path,
                debug=False,
            )
            mock_open_db.side_effect = _real_open_db
            _ss_mod.main()

        conn = _db_mod.open_db(db_path)
        assert conn is not None
        try:
            row = conn.execute("SELECT session_id FROM sessions").fetchone()
            assert row is not None, "Expected a session row"
            sid = str(row["session_id"])
            assert re.match(r"^sess-[0-9]+-[a-f0-9]+$", sid), (
                f"session_id {sid!r} does not match expected pattern"
            )
        finally:
            conn.close()


class TestEventBufferFlush:
    """Pre-seeded event_buffer rows are flushed during session start."""

    def test_buffer_flushed_on_start(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db_path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(db_path)
        assert conn is not None

        now = time.time()
        conn.executemany(
            "INSERT INTO event_buffer (payload, created_at) VALUES (?, ?)",
            [(f'{{"n":{i}}}', now - i) for i in range(5)],
        )
        conn.commit()
        conn.close()

        flush_calls: list[str] = []

        def _fake_post(
            url: str, headers: dict[str, str], payload: bytes, timeout: float
        ) -> tuple[int, bytes] | None:
            if "observations/batch" in url:
                flush_calls.append(url)
                return (200, b'{"ok":true}')
            if "sessions/start" in url:
                return (200, b'{"session_id":"sess-ok"}')
            return (200, b"{}")

        with (
            patch("pyhooks.session_start._config.load") as mock_load,
            patch("pyhooks.session_start._db.open_db") as mock_open_db,
            patch("pyhooks.session_start._http.post_json", side_effect=_fake_post),
            patch("pyhooks.session_start.sys.stdout", io.StringIO()),
            patch("pyhooks.session_start._codeweaver_bootstrap_and_upgrade", return_value=None),
        ):
            import pyhooks.config as _config_mod

            monkeypatch.chdir(tmp_path)
            mock_load.return_value = _config_mod.Config(
                api_key="test-key",
                api_base="http://localhost:19999",
                state_db_path=db_path,
                debug=False,
            )
            mock_open_db.side_effect = _real_open_db
            _ss_mod.main()

        assert len(flush_calls) >= 1, "Expected a POST to observations/batch"

        conn2 = _db_mod.open_db(db_path)
        assert conn2 is not None
        try:
            remaining = conn2.execute("SELECT COUNT(*) FROM event_buffer").fetchone()[0]
            assert remaining == 0, "event_buffer should be empty after successful flush"
        finally:
            conn2.close()


class TestGitignoreManagement:
    """The ``.gitignore`` file is updated to include ``.neuroloom.db``."""

    def test_gitignore_created_when_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _ss_mod._ensure_gitignore(str(tmp_path))
        gi = tmp_path / ".gitignore"
        assert gi.exists()
        assert ".neuroloom.db" in gi.read_text()

    def test_gitignore_appended_when_present(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text("*.log\n")
        _ss_mod._ensure_gitignore(str(tmp_path))
        content = gi.read_text()
        assert ".neuroloom.db" in content
        assert "*.log" in content

    def test_gitignore_not_duplicated(self, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text(".neuroloom.db\n")
        _ss_mod._ensure_gitignore(str(tmp_path))
        _ss_mod._ensure_gitignore(str(tmp_path))
        count = gi.read_text().count(".neuroloom.db")
        assert count == 1, "Entry should appear exactly once"


class TestClaudeMdInjection:
    """The memory-first block is appended to ``CLAUDE.md`` exactly once."""

    def test_no_op_when_claudemd_absent(self, tmp_path: Path) -> None:
        """No CLAUDE.md means nothing is created."""
        _ss_mod._inject_claudemd(str(tmp_path))
        assert not (tmp_path / "CLAUDE.md").exists()

    def test_block_appended_when_marker_absent(self, tmp_path: Path) -> None:
        claudemd = tmp_path / "CLAUDE.md"
        claudemd.write_text("# My Project\n")
        _ss_mod._inject_claudemd(str(tmp_path))
        content = claudemd.read_text()
        assert _ss_mod._CLAUDEMD_MARKER in content

    def test_idempotent_double_call(self, tmp_path: Path) -> None:
        claudemd = tmp_path / "CLAUDE.md"
        claudemd.write_text("# My Project\n")
        _ss_mod._inject_claudemd(str(tmp_path))
        _ss_mod._inject_claudemd(str(tmp_path))
        count = claudemd.read_text().count(_ss_mod._CLAUDEMD_MARKER)
        assert count == 1, "Marker should appear exactly once"


class TestTracesPruning:
    """Old trace rows are pruned to ``_TRACES_KEEP`` during session start."""

    def test_traces_pruned_to_limit(self, tmp_path: Path) -> None:
        db_path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(db_path)
        assert conn is not None
        try:
            # Seed more rows than the keep limit
            over = _ss_mod._TRACES_KEEP + 500
            conn.executemany(
                "INSERT INTO traces (ts, script, decision) VALUES (datetime('now'), 'test', 'x')",
                [() for _ in range(over)],
            )
            conn.commit()
            _ss_mod._prune_traces(conn)
            count = conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
            assert count <= _ss_mod._TRACES_KEEP, (
                f"Expected at most {_ss_mod._TRACES_KEEP} traces, got {count}"
            )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Codeweaver bootstrap tests
# ---------------------------------------------------------------------------


def test_codeweaver_already_installed_skips_ensure(tmp_path: Path) -> None:
    """When find_spec returns a spec, ensure_installed returns True without subprocesses."""
    mock_spec = MagicMock()
    with (
        patch("pyhooks.session_start.importlib.util.find_spec", return_value=mock_spec),
        patch("pyhooks.session_start.subprocess.run") as mock_run,
    ):
        result = _ss_mod._codeweaver_ensure_installed(tmp_path)

    assert result is True
    mock_run.assert_not_called()


def test_codeweaver_not_installed_venv_path_succeeds(tmp_path: Path) -> None:
    """When find_spec returns None, venv creation and pip install succeed; flag stays False."""
    import pyhooks.session_start as _ss

    _ss._codeweaver_install_failed = False

    mock_venv_builder = MagicMock()

    with (
        patch("pyhooks.session_start.importlib.util.find_spec", return_value=None),
        patch("pyhooks.session_start.subprocess.run", return_value=MagicMock(returncode=0)),
        patch("pyhooks.session_start.Path.exists", return_value=False),
        patch("venv.EnvBuilder", return_value=mock_venv_builder),
    ):
        result = _ss._codeweaver_ensure_installed(tmp_path)

    assert result is True
    assert _ss._codeweaver_install_failed is False


def test_codeweaver_venv_fails_user_fallback_succeeds(tmp_path: Path) -> None:
    """When venv creation raises, the --user fallback succeeds; flag stays False."""
    import pyhooks.session_start as _ss

    _ss._codeweaver_install_failed = False

    import venv as _venv_mod

    # EnvBuilder.create raises (macOS ensurepip stripped) so the entire venv
    # try-block is caught; subprocess.run is never reached inside that block.
    # The --user fallback subprocess.run then succeeds.
    with (
        patch("pyhooks.session_start.importlib.util.find_spec", return_value=None),
        patch("pyhooks.session_start.subprocess.run", return_value=MagicMock(returncode=0)),
        patch("pyhooks.session_start.Path.exists", return_value=False),
        patch.object(_venv_mod.EnvBuilder, "create", side_effect=Exception("ensurepip stripped")),
    ):
        result = _ss._codeweaver_ensure_installed(tmp_path)

    assert result is True
    assert _ss._codeweaver_install_failed is False


def test_codeweaver_both_paths_fail_sets_flag(tmp_path: Path) -> None:
    """When both venv and --user paths fail, _codeweaver_install_failed is set True."""
    import pyhooks.session_start as _ss

    _ss._codeweaver_install_failed = False

    with (
        patch("pyhooks.session_start.importlib.util.find_spec", return_value=None),
        patch("pyhooks.session_start.subprocess.run", side_effect=subprocess.CalledProcessError(1, "pip")),
        patch("pyhooks.session_start.Path.exists", return_value=False),
    ):
        import venv as _venv_mod

        with patch.object(_venv_mod.EnvBuilder, "create", side_effect=Exception("ensurepip stripped")):
            result = _ss._codeweaver_ensure_installed(tmp_path)

    assert result is False
    assert _ss._codeweaver_install_failed is True

    # Reset module-level state after test
    _ss._codeweaver_install_failed = False


def test_upgrade_if_stale_guards_when_not_installed() -> None:
    """When metadata.version raises PackageNotFoundError, upgrade returns immediately without subprocess."""
    with (
        patch(
            "pyhooks.session_start.importlib.metadata.version",
            side_effect=_ss_mod.importlib.metadata.PackageNotFoundError("neuroloom-codeweaver"),
        ),
        patch("pyhooks.session_start.subprocess.run") as mock_run,
        patch("pyhooks.session_start.urllib.request.urlopen") as mock_urlopen,
    ):
        _ss_mod._codeweaver_upgrade_if_stale()

    mock_run.assert_not_called()
    mock_urlopen.assert_not_called()


def test_offline_env_var_skips_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When NEUROLOOM_CODEWEAVER_OFFLINE is set, ensure_installed skips all install attempts."""
    monkeypatch.setenv("NEUROLOOM_CODEWEAVER_OFFLINE", "1")

    with (
        patch("pyhooks.session_start.importlib.util.find_spec", return_value=None),
        patch("pyhooks.session_start.subprocess.run") as mock_run,
    ):
        import venv as _venv_mod

        with patch.object(_venv_mod.EnvBuilder, "create") as mock_create:
            result = _ss_mod._codeweaver_ensure_installed(tmp_path)

    # find_spec returned None so result is False, but no subprocess or venv called
    assert result is False
    mock_run.assert_not_called()
    mock_create.assert_not_called()


# ---------------------------------------------------------------------------
# Banner tests
# ---------------------------------------------------------------------------


def test_banner_fires_when_install_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When _codeweaver_install_failed is True, the degraded banner appears in stdout."""
    import pyhooks.session_start as _ss

    _ss._codeweaver_install_failed = True
    try:
        db_path = tmp_path / ".neuroloom.db"
        output = io.StringIO()
        monkeypatch.chdir(tmp_path)

        with (
            patch("pyhooks.session_start._config.load") as mock_load,
            patch("pyhooks.session_start._db.open_db") as mock_open_db,
            patch("pyhooks.session_start._http.post_json", return_value=(200, b'{"session_id":"sess-1-aabb"}')),
            patch("pyhooks.session_start.sys.stdout", output),
            patch("pyhooks.session_start._codeweaver_bootstrap_and_upgrade", return_value=None),
        ):
            import pyhooks.config as _config_mod

            mock_load.return_value = _config_mod.Config(
                api_key="test-key-abc123",
                api_base="http://localhost:19999",
                state_db_path=db_path,
            )
            mock_open_db.side_effect = _real_open_db
            _ss_mod.main()

        assert "neuroloom-codeweaver could not be installed" in output.getvalue()
    finally:
        _ss._codeweaver_install_failed = False


def test_no_banner_on_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When _codeweaver_install_failed is False (default), the banner must not appear."""
    import pyhooks.session_start as _ss

    # Ensure the flag is in its default state
    _ss._codeweaver_install_failed = False

    db_path = tmp_path / ".neuroloom.db"
    output = io.StringIO()
    monkeypatch.chdir(tmp_path)

    with (
        patch("pyhooks.session_start._config.load") as mock_load,
        patch("pyhooks.session_start._db.open_db") as mock_open_db,
        patch("pyhooks.session_start._http.post_json", return_value=(200, b'{"session_id":"sess-1-aabb"}')),
        patch("pyhooks.session_start.sys.stdout", output),
        patch("pyhooks.session_start._codeweaver_bootstrap_and_upgrade", return_value=None),
    ):
        import pyhooks.config as _config_mod

        mock_load.return_value = _config_mod.Config(
            api_key="test-key-abc123",
            api_base="http://localhost:19999",
            state_db_path=db_path,
        )
        mock_open_db.side_effect = _real_open_db
        _ss_mod.main()

    assert "neuroloom-codeweaver could not be installed" not in output.getvalue()
