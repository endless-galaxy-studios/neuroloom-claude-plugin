"""
Tests for hooks/preload_context.py.

Covers: inject mode, nudge mode query extraction, circuit breaker, cache
hit/miss/TTL, token budget, empty-response non-caching (D83 fix), and corrupt
session handling.
"""

import io
import json
import sqlite3
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import pyhooks.db as _db_mod
import pyhooks.preload_context as _pc_mod

# Save reference to real open_db before any patches shadow it.
_real_open_db = _db_mod.open_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_session(
    conn: sqlite3.Connection,
    workspace_key: str,
    session_id: str = "sess-1000000000-aabbccdd",
) -> None:
    conn.execute(
        """
        INSERT INTO sessions (session_key, session_id, started_at, last_submit_ms)
        VALUES (?, ?, datetime('now'), 0)
        """,
        (workspace_key, session_id),
    )
    conn.commit()


def _run_preload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    db_path: Path,
    stdin_data: str,
    api_key: str = "test-key",
    fetch_return: tuple[str, int] | None = ("Relevant context here", 300),
    raise_on_fetch: Exception | None = None,
) -> str:
    """
    Run ``preload_context.main()`` with controlled environment.

    Returns captured stdout.
    """
    monkeypatch.chdir(tmp_path)
    output = io.StringIO()

    def _fake_fetch(
        api_base: str,
        api_key_arg: str,
        file_path: str,
        timeout: float = 3.0,
        fmt: str = "inject",
        query: str | None = None,
    ) -> tuple[str, int] | None:
        if raise_on_fetch is not None:
            raise raise_on_fetch
        return fetch_return

    with (
        patch("pyhooks.preload_context._config_mod.load") as mock_load,
        patch("pyhooks.preload_context._db_mod.open_db") as mock_open_db,
        patch("pyhooks.preload_context._fetch_context", side_effect=_fake_fetch),
        patch("pyhooks.preload_context.sys.stdin", io.StringIO(stdin_data)),
        patch("pyhooks.preload_context.sys.stdout", output),
    ):
        import pyhooks.config as _config_mod

        mock_load.return_value = _config_mod.Config(
            api_key=api_key,
            api_base="http://localhost:19999",
            state_db_path=db_path,
            debug=False,
        )
        mock_open_db.side_effect = _real_open_db
        _pc_mod.main()

    return output.getvalue()


def _inject_event(file_path: str = "/project/api.py") -> str:
    return json.dumps({"tool_name": "Read", "tool_input": {"file_path": file_path}})


def _nudge_event(tool_name: str = "Grep", pattern: str = "def auth_handler") -> str:
    return json.dumps({"tool_name": tool_name, "tool_input": {"pattern": pattern}})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestInjectMode:
    """Inject mode: tool_name=Read → queries API and returns additionalContext."""

    def test_inject_returns_additional_context(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(db_path)
        assert conn is not None
        _seed_session(conn, str(tmp_path.resolve()))
        conn.close()

        stdout = _run_preload(
            monkeypatch,
            tmp_path,
            db_path,
            _inject_event("/project/api.py"),
            fetch_return=("Memory context for api.py", 300),
        )
        out = json.loads(stdout)
        assert "additionalContext" in out
        assert "Memory context for api.py" in out["additionalContext"]

    def test_inject_returns_empty_dict_on_empty_context(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(db_path)
        assert conn is not None
        _seed_session(conn, str(tmp_path.resolve()))
        conn.close()

        stdout = _run_preload(
            monkeypatch,
            tmp_path,
            db_path,
            _inject_event(),
            fetch_return=("", 300),
        )
        out = json.loads(stdout)
        assert out == {}


class TestNudgeMode:
    """Nudge mode: Grep/Glob patterns → extract query → additionalContext."""

    def test_nudge_grep_pattern_extracted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(db_path)
        assert conn is not None
        _seed_session(conn, str(tmp_path.resolve()))
        conn.close()

        stdout = _run_preload(
            monkeypatch,
            tmp_path,
            db_path,
            _nudge_event("Grep", "def auth_handler"),
            fetch_return=("Auth handler memory nudge", 300),
        )
        out = json.loads(stdout)
        assert "additionalContext" in out

    def test_nudge_unextractable_pattern_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pattern that yields no meaningful query returns ``{}`` without hitting the API."""
        db_path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(db_path)
        assert conn is not None
        _seed_session(conn, str(tmp_path.resolve()))
        conn.close()

        # Pattern of only regex metacharacters — nothing extractable
        stdout = _run_preload(
            monkeypatch,
            tmp_path,
            db_path,
            _nudge_event("Grep", ".*"),
            fetch_return=("should not be called", 300),
        )
        out = json.loads(stdout)
        assert out == {}

    def test_glob_pattern_extracted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db_path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(db_path)
        assert conn is not None
        _seed_session(conn, str(tmp_path.resolve()))
        conn.close()

        stdout = _run_preload(
            monkeypatch,
            tmp_path,
            db_path,
            _nudge_event("Glob", "**/auth_service.py"),
            fetch_return=("Auth service nudge", 300),
        )
        out = json.loads(stdout)
        assert "additionalContext" in out


class TestCircuitBreaker:
    """A tripped circuit breaker suppresses the API call and returns ``{}``."""

    def test_circuit_breaker_active_no_http_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(db_path)
        assert conn is not None
        # Trip the circuit breaker
        conn.execute(
            "INSERT OR REPLACE INTO circuit_breaker (id, tripped_at) VALUES (1, ?)",
            (time.time(),),
        )
        _seed_session(conn, str(tmp_path.resolve()))
        conn.commit()
        conn.close()

        fetch_calls: list[str] = []

        def _tracking_fetch(*args: Any, **kwargs: Any) -> None:
            fetch_calls.append("called")

        with (
            patch("pyhooks.preload_context._config_mod.load") as mock_load,
            patch("pyhooks.preload_context._db_mod.open_db") as mock_open_db,
            patch("pyhooks.preload_context._fetch_context", side_effect=_tracking_fetch),
            patch("pyhooks.preload_context.sys.stdin", io.StringIO(_inject_event())),
            patch("pyhooks.preload_context.sys.stdout", io.StringIO()) as out,
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
            _pc_mod.main()
            stdout_text = out.getvalue()

        assert fetch_calls == [], "API should not be called when circuit breaker is active"
        assert json.loads(stdout_text) == {}


class TestCacheHit:
    """A cache hit returns context without making an API call."""

    def test_cache_hit_no_api_call(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db_path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(db_path)
        assert conn is not None

        workspace_root = str(tmp_path.resolve())
        file_path = "/project/api.py"
        cache_key = _pc_mod._cache_key(file_path, workspace_root)

        # Pre-seed the cache
        conn.execute(
            "INSERT OR REPLACE INTO cache (cache_key, context, created_at) VALUES (?, ?, ?)",
            (cache_key, "Cached context text", time.time()),
        )
        _seed_session(conn, workspace_root)
        conn.commit()
        conn.close()

        fetch_calls: list[str] = []

        def _tracking_fetch(*args: Any, **kwargs: Any) -> tuple[str, int]:
            fetch_calls.append("called")
            return ("should not appear", 300)

        monkeypatch.chdir(tmp_path)
        output = io.StringIO()

        with (
            patch("pyhooks.preload_context._config_mod.load") as mock_load,
            patch("pyhooks.preload_context._db_mod.open_db") as mock_open_db,
            patch("pyhooks.preload_context._fetch_context", side_effect=_tracking_fetch),
            patch("pyhooks.preload_context.sys.stdin", io.StringIO(_inject_event(file_path))),
            patch("pyhooks.preload_context.sys.stdout", output),
        ):
            import pyhooks.config as _config_mod

            mock_load.return_value = _config_mod.Config(
                api_key="test-key",
                api_base="http://localhost:19999",
                state_db_path=db_path,
                debug=False,
            )
            mock_open_db.side_effect = _real_open_db
            _pc_mod.main()

        assert fetch_calls == [], "API should not be called on cache hit"
        out = json.loads(output.getvalue())
        assert "additionalContext" in out
        assert "Cached context text" in out["additionalContext"]


class TestCacheTtl:
    """An expired cache entry is treated as a miss and triggers an API call."""

    def test_expired_cache_treated_as_miss(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(db_path)
        assert conn is not None

        workspace_root = str(tmp_path.resolve())
        file_path = "/project/old.py"
        cache_key = _pc_mod._cache_key(file_path, workspace_root)

        # Seed a cache entry that is more than 3600 seconds old
        expired_ts = time.time() - 4000
        conn.execute(
            "INSERT OR REPLACE INTO cache (cache_key, context, created_at) VALUES (?, ?, ?)",
            (cache_key, "Old stale context", expired_ts),
        )
        _seed_session(conn, workspace_root)
        conn.commit()
        conn.close()

        fetch_calls: list[str] = []

        def _tracking_fetch(*args: Any, **kwargs: Any) -> tuple[str, int]:
            fetch_calls.append("called")
            return ("Fresh context", 300)

        monkeypatch.chdir(tmp_path)
        output = io.StringIO()

        with (
            patch("pyhooks.preload_context._config_mod.load") as mock_load,
            patch("pyhooks.preload_context._db_mod.open_db") as mock_open_db,
            patch("pyhooks.preload_context._fetch_context", side_effect=_tracking_fetch),
            patch("pyhooks.preload_context.sys.stdin", io.StringIO(_inject_event(file_path))),
            patch("pyhooks.preload_context.sys.stdout", output),
        ):
            import pyhooks.config as _config_mod

            mock_load.return_value = _config_mod.Config(
                api_key="test-key",
                api_base="http://localhost:19999",
                state_db_path=db_path,
                debug=False,
            )
            mock_open_db.side_effect = _real_open_db
            _pc_mod.main()

        assert len(fetch_calls) == 1, "Expired cache should trigger an API call"
        out = json.loads(output.getvalue())
        assert "Fresh context" in out.get("additionalContext", "")


class TestTokenBudget:
    """When the token budget is exhausted, injection is suppressed."""

    def test_budget_exhausted_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(db_path)
        assert conn is not None

        session_id = "sess-1000000000-aabbccdd"
        workspace_root = str(tmp_path.resolve())
        _seed_session(conn, workspace_root, session_id=session_id)

        # Exhaust the budget by setting total_chars to the full limit
        conn.execute(
            "INSERT OR REPLACE INTO token_budget (session_id, total_chars) VALUES (?, ?)",
            (session_id, _pc_mod.TOTAL_CHAR_BUDGET),
        )
        conn.commit()
        conn.close()

        stdout = _run_preload(
            monkeypatch,
            tmp_path,
            db_path,
            _inject_event(),
            fetch_return=("Some context text that would exceed budget", 300),
        )
        out = json.loads(stdout)
        assert out == {}, "Budget exhausted — expected empty response"


class TestEmptyResponseNotCached:
    """Empty API responses must not be written to the cache (D83 fix)."""

    def test_empty_response_not_cached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(db_path)
        assert conn is not None
        _seed_session(conn, str(tmp_path.resolve()))
        conn.close()

        _run_preload(
            monkeypatch,
            tmp_path,
            db_path,
            _inject_event("/project/empty.py"),
            fetch_return=("", 300),
        )

        conn2 = _db_mod.open_db(db_path)
        assert conn2 is not None
        try:
            count = conn2.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
            assert count == 0, "Empty response must not be written to the cache"
        finally:
            conn2.close()


class TestCorruptSession:
    """An invalid session_id traces ``corrupt_session`` but the hook still proceeds."""

    def test_corrupt_session_traces_and_continues(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(db_path)
        assert conn is not None
        workspace_key = str(tmp_path.resolve())

        # Insert a session with an invalid ID
        conn.execute(
            """
            INSERT INTO sessions (session_key, session_id, started_at, last_submit_ms)
            VALUES (?, ?, datetime('now'), 0)
            """,
            (workspace_key, "INVALID-SESSION-ID"),
        )
        conn.commit()
        conn.close()

        stdout = _run_preload(
            monkeypatch,
            tmp_path,
            db_path,
            _inject_event(),
            fetch_return=("Context after corrupt session", 300),
        )

        conn2 = _db_mod.open_db(db_path)
        assert conn2 is not None
        try:
            trace = conn2.execute(
                "SELECT decision FROM traces WHERE decision = 'corrupt_session'"
            ).fetchone()
            assert trace is not None, "Expected corrupt_session trace"
        finally:
            conn2.close()

        # The hook should still attempt to inject (using workspace_root as fallback session_id)
        out = json.loads(stdout)
        assert "additionalContext" in out


class TestExtractQuery:
    """Unit tests for the ``_extract_query`` helper used in nudge mode."""

    def test_grep_extracts_plain_tokens(self) -> None:
        result = _pc_mod._extract_query("Grep", "def authenticate")
        assert result is not None
        assert "authenticate" in result

    def test_grep_strips_metacharacters(self) -> None:
        result = _pc_mod._extract_query("Grep", r"function\s+render")
        assert result is not None
        assert len(result) >= 3

    def test_glob_extracts_stem_from_path(self) -> None:
        result = _pc_mod._extract_query("Glob", "**/auth_service.py")
        assert result == "auth_service"

    def test_glob_skips_wildcard_only_segments(self) -> None:
        result = _pc_mod._extract_query("Glob", "**/*.ts")
        assert result is None

    def test_returns_none_for_empty_pattern(self) -> None:
        assert _pc_mod._extract_query("Grep", "") is None

    def test_returns_none_for_unknown_tool(self) -> None:
        assert _pc_mod._extract_query("Write", "something") is None
