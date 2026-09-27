"""
Tests for hooks/session_start.py.

Covers: no-API-key guard, stale session ending, corrupt session handling,
session ID format, event-buffer flush, .gitignore management, CLAUDE.md
injection, trace pruning, and session start failure.
"""

import io
import json
import re
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import pyhooks.db as _db_mod
import pyhooks.session_start as _ss_mod
import pyhooks.workspace_config as _wc_mod

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
        # D171 round 8, MAJOR-6 + round 9, M1: explicit env isolation (Step 3's
        # guard now reads CLAUDE_PLUGIN_OPTION_API_KEY directly to choose
        # between the dormant/no-key messages) plus chdir/bootstrap-patch,
        # matching the suite's main()-invoking convention defensively.
        db_path = tmp_path / ".neuroloom.db"
        output = io.StringIO()
        monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_API_KEY", raising=False)
        monkeypatch.chdir(tmp_path)

        with (
            patch("pyhooks.session_start._config.load") as mock_load,
            patch("pyhooks.session_start._db.open_db") as mock_open_db,
            patch("pyhooks.session_start.sys.stdout", output),
            patch("pyhooks.session_start._codeweaver_bootstrap_and_upgrade", return_value=None),
        ):
            import pyhooks.config as _config_mod

            mock_load.return_value = _config_mod.Config(
                api_key="",
                api_base="http://localhost:19999",
                state_db_path=db_path,
                api_key_source="none",
            )
            mock_open_db.side_effect = _real_open_db
            _ss_mod.main()

        text = output.getvalue()
        assert "No API key configured" in text
        assert "/neuroloom:init" in text
        assert "/plugins configure" not in text  # not the sole CTA (D171 round 7)

    def test_no_key_does_not_call_api(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # D171 round 8, MAJOR-6 + round 9, M1: same env isolation + chdir/
        # bootstrap-patch as test_no_key_message_printed above.
        db_path = tmp_path / ".neuroloom.db"
        post_calls: list[str] = []
        monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_API_KEY", raising=False)
        monkeypatch.chdir(tmp_path)

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
            patch("pyhooks.session_start._codeweaver_bootstrap_and_upgrade", return_value=None),
        ):
            import pyhooks.config as _config_mod

            mock_load.return_value = _config_mod.Config(
                api_key="",
                api_base="http://localhost:19999",
                state_db_path=db_path,
                api_key_source="none",
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


def _seed_buffer_row(
    conn: sqlite3.Connection,
    payload: dict[str, object],
    payload_type: str | None,
) -> int:
    """Insert one event_buffer row with an explicit payload_type and return its id."""
    cur = conn.execute(
        "INSERT INTO event_buffer (payload, created_at, payload_type) VALUES (?, ?, ?)",
        (json.dumps(payload), time.time(), payload_type),
    )
    conn.commit()
    row_id = cur.lastrowid
    assert row_id is not None
    return row_id


def _observation_payload(n: int = 0) -> dict[str, object]:
    return {
        "observation_id": f"obs-{n}",
        "session_id": f"sess-{n}",
        "observed_at": "2026-07-10T00:00:00+00:00",
        "category": "Write",
        "content": "{}",
        "agent_id": None,
        "agent_type": None,
    }


def _document_payload(title: str = "doc.md", source_type: str = "sdlc_deliverable") -> dict[str, object]:
    return {
        "title": title,
        "content": "# hello",
        "source_type": source_type,
        "source_path": "/docs/doc.md",
        "tags": ["sdlc:doc:spec"],
    }


class TestFlushEventBufferRouting:
    """``_flush_event_buffer`` partitions buffered rows and routes each group
    to its own endpoint (observations/batch vs. documents/ingest/batch)."""

    def test_mixed_payload_type_rows_route_to_correct_endpoints(self, tmp_path: Path) -> None:
        db_path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(db_path)
        assert conn is not None
        _seed_buffer_row(conn, _observation_payload(1), "observation")
        _seed_buffer_row(conn, _document_payload(), "document")
        conn.close()

        calls: list[str] = []

        def _fake_post(
            url: str, headers: dict[str, str], payload: bytes, timeout: float
        ) -> tuple[int, bytes] | None:
            calls.append(url)
            if "observations/batch" in url:
                return (200, b'{"ok":true}')
            if "documents/ingest/batch" in url:
                body = json.loads(payload.decode("utf-8"))
                results = [
                    {"index": i, "title": d["title"], "status": "created"}
                    for i, d in enumerate(body["documents"])
                ]
                return (200, json.dumps({"results": results}).encode("utf-8"))
            return (200, b"{}")

        with patch("pyhooks.session_start._http.post_json", side_effect=_fake_post):
            _ss_mod._flush_event_buffer(db_path, "http://localhost:19999", "test-key")

        assert any("observations/batch" in c for c in calls)
        assert any("documents/ingest/batch" in c for c in calls)

        conn2 = _db_mod.open_db(db_path)
        assert conn2 is not None
        try:
            remaining = conn2.execute("SELECT COUNT(*) FROM event_buffer").fetchone()[0]
            assert remaining == 0
        finally:
            conn2.close()

    def test_legacy_null_payload_type_rows_sniffed_and_routed(self, tmp_path: Path) -> None:
        db_path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(db_path)
        assert conn is not None
        _seed_buffer_row(conn, _observation_payload(2), None)
        _seed_buffer_row(conn, _document_payload(title="legacy.md"), None)
        conn.close()

        obs_calls: list[dict[str, object]] = []
        doc_calls: list[dict[str, object]] = []

        def _fake_post(
            url: str, headers: dict[str, str], payload: bytes, timeout: float
        ) -> tuple[int, bytes] | None:
            body = json.loads(payload.decode("utf-8"))
            if "observations/batch" in url:
                obs_calls.append(body)
                return (200, b'{"ok":true}')
            if "documents/ingest/batch" in url:
                doc_calls.append(body)
                results = [
                    {"index": i, "title": d["title"], "status": "created"}
                    for i, d in enumerate(body["documents"])
                ]
                return (200, json.dumps({"results": results}).encode("utf-8"))
            return (200, b"{}")

        with patch("pyhooks.session_start._http.post_json", side_effect=_fake_post):
            _ss_mod._flush_event_buffer(db_path, "http://localhost:19999", "test-key")

        assert len(obs_calls) == 1
        assert len(doc_calls) == 1

        conn2 = _db_mod.open_db(db_path)
        assert conn2 is not None
        try:
            remaining = conn2.execute("SELECT COUNT(*) FROM event_buffer").fetchone()[0]
            assert remaining == 0
        finally:
            conn2.close()

    def test_unclassifiable_row_isolated_does_not_block_others(self, tmp_path: Path) -> None:
        db_path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(db_path)
        assert conn is not None
        # Ambiguous: no observation keys, no document keys.
        _seed_buffer_row(conn, {"foo": "bar"}, None)
        good_obs_id = _seed_buffer_row(conn, _observation_payload(3), "observation")
        conn.close()

        def _fake_post(
            url: str, headers: dict[str, str], payload: bytes, timeout: float
        ) -> tuple[int, bytes] | None:
            if "observations/batch" in url:
                return (200, b'{"ok":true}')
            return (200, b"{}")

        with patch("pyhooks.session_start._http.post_json", side_effect=_fake_post):
            _ss_mod._flush_event_buffer(db_path, "http://localhost:19999", "test-key")

        conn2 = _db_mod.open_db(db_path)
        assert conn2 is not None
        try:
            remaining_ids = {
                row[0] for row in conn2.execute("SELECT id FROM event_buffer").fetchall()
            }
            assert good_obs_id not in remaining_ids, "delivered observation should be deleted"
            assert len(remaining_ids) == 1, "ambiguous row must be left in place, not dropped"
        finally:
            conn2.close()

    def test_invalid_document_row_isolated_siblings_still_deliver(self, tmp_path: Path) -> None:
        db_path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(db_path)
        assert conn is not None
        bad_doc = _document_payload()
        del bad_doc["content"]  # missing required field
        bad_id = _seed_buffer_row(conn, bad_doc, "document")
        good_id = _seed_buffer_row(conn, _document_payload(title="good.md"), "document")
        conn.close()

        def _fake_post(
            url: str, headers: dict[str, str], payload: bytes, timeout: float
        ) -> tuple[int, bytes] | None:
            body = json.loads(payload.decode("utf-8"))
            results = [
                {"index": i, "title": d["title"], "status": "created"}
                for i, d in enumerate(body["documents"])
            ]
            return (200, json.dumps({"results": results}).encode("utf-8"))

        with patch("pyhooks.session_start._http.post_json", side_effect=_fake_post):
            _ss_mod._flush_event_buffer(db_path, "http://localhost:19999", "test-key")

        conn2 = _db_mod.open_db(db_path)
        assert conn2 is not None
        try:
            remaining_ids = {
                row[0] for row in conn2.execute("SELECT id FROM event_buffer").fetchall()
            }
            assert bad_id in remaining_ids, "malformed document row must remain buffered"
            assert good_id not in remaining_ids, "valid sibling should still be delivered"
        finally:
            conn2.close()

    def test_invalid_source_type_isolated(self, tmp_path: Path) -> None:
        db_path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(db_path)
        assert conn is not None
        bad_id = _seed_buffer_row(
            conn, _document_payload(source_type="not-a-real-type"), "document"
        )
        conn.close()

        calls: list[str] = []

        def _fake_post(
            url: str, headers: dict[str, str], payload: bytes, timeout: float
        ) -> tuple[int, bytes] | None:
            calls.append(url)
            return (200, b'{"results":[]}')

        with patch("pyhooks.session_start._http.post_json", side_effect=_fake_post):
            _ss_mod._flush_event_buffer(db_path, "http://localhost:19999", "test-key")

        assert calls == [], "invalid source_type row must never reach the POST"

        conn2 = _db_mod.open_db(db_path)
        assert conn2 is not None
        try:
            remaining_ids = {
                row[0] for row in conn2.execute("SELECT id FROM event_buffer").fetchall()
            }
            assert bad_id in remaining_ids
        finally:
            conn2.close()

    def test_error_status_items_remain_buffered(self, tmp_path: Path) -> None:
        db_path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(db_path)
        assert conn is not None
        error_id = _seed_buffer_row(conn, _document_payload(title="fails.md"), "document")
        ok_id = _seed_buffer_row(conn, _document_payload(title="ok.md"), "document")
        conn.close()

        def _fake_post(
            url: str, headers: dict[str, str], payload: bytes, timeout: float
        ) -> tuple[int, bytes] | None:
            body = json.loads(payload.decode("utf-8"))
            results = []
            for i, d in enumerate(body["documents"]):
                status = "error" if d["title"] == "fails.md" else "created"
                results.append({"index": i, "title": d["title"], "status": status})
            return (200, json.dumps({"results": results}).encode("utf-8"))

        with patch("pyhooks.session_start._http.post_json", side_effect=_fake_post):
            _ss_mod._flush_event_buffer(db_path, "http://localhost:19999", "test-key")

        conn2 = _db_mod.open_db(db_path)
        assert conn2 is not None
        try:
            remaining_ids = {
                row[0] for row in conn2.execute("SELECT id FROM event_buffer").fetchall()
            }
            assert error_id in remaining_ids, "error-status document row must remain buffered"
            assert ok_id not in remaining_ids, "created-status document row must be deleted"
        finally:
            conn2.close()

    def test_all_observation_buffer_regression_unchanged(self, tmp_path: Path) -> None:
        """An all-observation buffer flushes identically to pre-payload_type behavior."""
        db_path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(db_path)
        assert conn is not None
        for i in range(3):
            _seed_buffer_row(conn, _observation_payload(i), "observation")
        conn.close()

        posted: list[dict[str, object]] = []

        def _fake_post(
            url: str, headers: dict[str, str], payload: bytes, timeout: float
        ) -> tuple[int, bytes] | None:
            posted.append(json.loads(payload.decode("utf-8")))
            return (200, b'{"ok":true}')

        with patch("pyhooks.session_start._http.post_json", side_effect=_fake_post):
            _ss_mod._flush_event_buffer(db_path, "http://localhost:19999", "test-key")

        assert len(posted) == 1
        assert len(posted[0]["observations"]) == 3

        conn2 = _db_mod.open_db(db_path)
        assert conn2 is not None
        try:
            remaining = conn2.execute("SELECT COUNT(*) FROM event_buffer").fetchone()[0]
            assert remaining == 0
        finally:
            conn2.close()

    def test_document_group_chunked_at_50(self, tmp_path: Path) -> None:
        """Document rows beyond 50 are POSTed as a second chunk."""
        db_path = tmp_path / ".neuroloom.db"
        conn = _db_mod.open_db(db_path)
        assert conn is not None
        for i in range(60):
            _seed_buffer_row(conn, _document_payload(title=f"doc-{i}.md"), "document")
        conn.close()

        chunk_sizes: list[int] = []

        def _fake_post(
            url: str, headers: dict[str, str], payload: bytes, timeout: float
        ) -> tuple[int, bytes] | None:
            body = json.loads(payload.decode("utf-8"))
            chunk_sizes.append(len(body["documents"]))
            results = [
                {"index": i, "title": d["title"], "status": "created"}
                for i, d in enumerate(body["documents"])
            ]
            return (200, json.dumps({"results": results}).encode("utf-8"))

        with patch("pyhooks.session_start._http.post_json", side_effect=_fake_post):
            _ss_mod._flush_event_buffer(db_path, "http://localhost:19999", "test-key")

        assert chunk_sizes == [50, 10]


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


class TestDormantCapture:
    """A project with no per-project key but a global seed goes dormant
    with disclosure, distinct from the no-key-anywhere message."""

    def test_dormant_message_printed_when_seed_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / ".neuroloom.db"
        output = io.StringIO()
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_API_KEY", "global-seed-value")
        monkeypatch.chdir(tmp_path)

        with (
            patch("pyhooks.session_start._config.load") as mock_load,
            patch("pyhooks.session_start._db.open_db") as mock_open_db,
            patch("pyhooks.session_start.sys.stdout", output),
            patch("pyhooks.session_start._codeweaver_bootstrap_and_upgrade", return_value=None),
        ):
            import pyhooks.config as _config_mod

            mock_load.return_value = _config_mod.Config(
                api_key="",
                api_base="http://localhost:19999",
                state_db_path=db_path,
                api_key_source="none",
            )
            mock_open_db.side_effect = _real_open_db
            _ss_mod.main()

        text = output.getvalue()
        assert "Memory capture is off for this project" in text
        assert "No API key configured" not in text

    def test_no_key_message_printed_when_no_seed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / ".neuroloom.db"
        output = io.StringIO()
        monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_API_KEY", raising=False)
        monkeypatch.chdir(tmp_path)

        with (
            patch("pyhooks.session_start._config.load") as mock_load,
            patch("pyhooks.session_start._db.open_db") as mock_open_db,
            patch("pyhooks.session_start.sys.stdout", output),
            patch("pyhooks.session_start._codeweaver_bootstrap_and_upgrade", return_value=None) as mock_bootstrap,
        ):
            import pyhooks.config as _config_mod

            mock_load.return_value = _config_mod.Config(
                api_key="",
                api_base="http://localhost:19999",
                state_db_path=db_path,
                api_key_source="none",
            )
            mock_open_db.side_effect = _real_open_db
            _ss_mod.main()

        text = output.getvalue()
        assert "No API key configured" in text
        mock_bootstrap.assert_not_called()  # D171 round 10: pins round-9 CRITICAL-1's revert
        assert "Memory capture is off for this project" not in text

    def test_dormant_and_no_key_messages_mutually_exclusive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / ".neuroloom.db"
        monkeypatch.chdir(tmp_path)
        import pyhooks.config as _config_mod

        for seed_present in (True, False):
            output = io.StringIO()
            if seed_present:
                monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_API_KEY", "global-seed-value")
            else:
                monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_API_KEY", raising=False)

            with (
                patch("pyhooks.session_start._config.load") as mock_load,
                patch("pyhooks.session_start._db.open_db") as mock_open_db,
                patch("pyhooks.session_start.sys.stdout", output),
                patch("pyhooks.session_start._codeweaver_bootstrap_and_upgrade", return_value=None),
            ):
                mock_load.return_value = _config_mod.Config(
                    api_key="",
                    api_base="http://localhost:19999",
                    state_db_path=db_path,
                    api_key_source="none",
                )
                mock_open_db.side_effect = _real_open_db
                _ss_mod.main()

            text = output.getvalue()
            dormant_shown = "Memory capture is off for this project" in text
            no_key_shown = "No API key configured" in text
            assert dormant_shown != no_key_shown, "exactly one of the two messages must print"

    def test_gitignore_ordering_survives_two_dormant_calls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """D171 round 8, MAJOR-7 -- an earlier revision's version copied
        test_no_key_message_printed's Config(..., debug=False) construction,
        the exact broken fixture pattern causing most of the pre-existing
        baseline failures, and never actually exercised the try/except wrap
        it claimed to verify."""
        db_path = tmp_path / ".neuroloom.db"
        monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_API_KEY", raising=False)
        monkeypatch.chdir(tmp_path)
        import pyhooks.config as _config_mod

        for _ in range(2):
            output = io.StringIO()
            with (
                patch("pyhooks.session_start._config.load") as mock_load,
                patch("pyhooks.session_start._db.open_db") as mock_open_db,
                patch("pyhooks.session_start.sys.stdout", output),
                patch("pyhooks.session_start._codeweaver_bootstrap_and_upgrade", return_value=None),
            ):
                mock_load.return_value = _config_mod.Config(
                    api_key="",
                    api_base="http://localhost:19999",
                    state_db_path=db_path,
                    api_key_source="none",
                )
                mock_open_db.side_effect = _real_open_db
                _ss_mod.main()
            assert "No API key configured" in output.getvalue()

        gi = tmp_path / ".gitignore"
        assert gi.read_text().count(".neuroloom.db") == 1

    def test_gitignore_failure_does_not_suppress_no_key_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """D171 round 8, MAJOR-7 -- item 4's try/except wrap around
        _ensure_gitignore must prevent a write failure there from
        suppressing the no-key/dormant disclosure that follows it."""
        db_path = tmp_path / ".neuroloom.db"
        output = io.StringIO()
        monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_API_KEY", raising=False)
        monkeypatch.chdir(tmp_path)

        with (
            patch("pyhooks.session_start._config.load") as mock_load,
            patch("pyhooks.session_start._db.open_db") as mock_open_db,
            patch("pyhooks.session_start._ensure_gitignore", side_effect=OSError("disk full")),
            patch("pyhooks.session_start.sys.stdout", output),
            patch("pyhooks.session_start._codeweaver_bootstrap_and_upgrade", return_value=None),
        ):
            import pyhooks.config as _config_mod

            mock_load.return_value = _config_mod.Config(
                api_key="",
                api_base="http://localhost:19999",
                state_db_path=db_path,
                api_key_source="none",
            )
            mock_open_db.side_effect = _real_open_db
            _ss_mod.main()

        assert "No API key configured" in output.getvalue()


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


# ---------------------------------------------------------------------------
# D172 Phase 5 — Step 8 emission wiring — session_start.py's own
# print/.format() calls, rewritten against the new gated_reconcile /
# ReconcileOutcome API (WorkspaceConfigResult is retired). Same six
# scenarios as the D169-era class this replaces, remapped onto the six
# decision/write_result shapes Step 8 actually branches on today:
#   WARN_ONLY                                -> unowned-conflict warning
#   STAMP_OWNERSHIP_ONLY                     -> stamp-ownership-only message (D176 #4)
#   STAMP_OWNERSHIP_WRITE_HEADER             -> unowned-conflict warning
#   CREATE_BASELINE/CREATE_WITH_HEADER, fail -> connection-missing warning
#   CREATE_BASELINE/CREATE_WITH_HEADER, ok   -> silent (baseline ensured)
#   WRITE_HEADER/CLEAR_HEADER, fail          -> update-write-failed warning
#   WRITE_HEADER/CLEAR_HEADER, ok            -> silent (already applied)
#   NO_OP                                    -> silent (already converged)
# test_workspace_config.py already covers the *fields* (decision,
# write_result, error) that drive these prints; this class covers the
# print/.format() wiring itself, which is otherwise exercised only by
# inspection. A future rename of a message constant's placeholder, or a
# `.value` access on the wrong type, would raise inside main()'s
# un-guarded try block (only `finally` closes the DB — nothing catches),
# aborting steps 9-12 including the tool-catalog print. That is exactly
# the class of silent session-start failure this deliverable exists to
# close, so the wiring itself needs direct coverage, not just the
# upstream fields.
# ---------------------------------------------------------------------------


def _run_session_start_with_reconcile_outcome(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    db_path: Path,
    gated_outcome: _wc_mod.GatedReconcileOutcome,
) -> str:
    """
    Run ``main()`` with ``gated_reconcile`` stubbed to return
    *gated_outcome* directly, isolating Step 8's print/.format() wiring
    from the resolution logic it's driven by (already covered exhaustively
    in test_workspace_config.py). Patching ``gated_reconcile`` (rather than
    letting Step 8 drive a real filesystem/network round-trip) is the D172
    equivalent of the retired helper's ``ensure_workspace_configured_
    detailed`` patch — same isolation shape, new target function.
    """
    monkeypatch.chdir(tmp_path)
    output = io.StringIO()

    with (
        patch("pyhooks.session_start._config.load") as mock_load,
        patch("pyhooks.session_start._db.open_db") as mock_open_db,
        patch(
            "pyhooks.session_start._http.post_json",
            return_value=(200, b'{"session_id":"sess-1-aabb"}'),
        ),
        patch("pyhooks.session_start.sys.stdout", output),
        patch("pyhooks.session_start._codeweaver_bootstrap_and_upgrade", return_value=None),
        patch(
            "pyhooks.session_start._workspace_config.gated_reconcile",
            return_value=gated_outcome,
        ),
    ):
        import pyhooks.config as _config_mod

        mock_load.return_value = _config_mod.Config(
            api_key="test-key-abc123",
            api_base="http://localhost:19999",
            state_db_path=db_path,
            api_key_source="project",  # D171 round 9, M1 -- default now that
            # Config carries api_key_source; a project with a resolved key
            # (as every test using this helper has) is always "project".
        )
        mock_open_db.side_effect = _real_open_db
        _ss_mod.main()

    return output.getvalue()


class TestWorkspaceStepEmissionWiring:
    """MIGRATE (D172 Phase 5) — rewritten against gated_reconcile/ReconcileOutcome."""

    def test_warn_only_prints_unowned_conflict_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / ".neuroloom.db"
        gated_outcome = _wc_mod.GatedReconcileOutcome(
            resolvability=_wc_mod.Resolvability.RESOLVED,
            reconcile=_wc_mod.ReconcileOutcome(_wc_mod.Decision.WARN_ONLY, None),
        )
        text = _run_session_start_with_reconcile_outcome(
            monkeypatch, tmp_path, db_path, gated_outcome
        )

        assert "isn't one this plugin manages" in text
        assert "Could not ensure a Neuroloom MCP connection" not in text
        assert "Could not update this project's .mcp.json" not in text

    def test_stamp_ownership_only_prints_distinct_no_conflict_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        D176 #4 (this plan): Decision.STAMP_OWNERSHIP_ONLY previously shared
        _UNOWNED_CONFLICT_WARNING with WARN_ONLY/STAMP_OWNERSHIP_WRITE_HEADER
        — wording that falsely implied a conflict exists. STAMP_OWNERSHIP_ONLY
        means the unowned entry's header already equals the desired
        workspace value (decide_reconciliation, workspace_config.py ~876-880)
        — routing is already correct, so the hook must print the distinct
        _STAMP_OWNERSHIP_ONLY_MESSAGE instead. decide_reconciliation's
        decision logic itself is unchanged by this fix — only the hook's
        print branch changed (verified by test_workspace_config.py, which
        this file does not duplicate).

        Round-2 review finding (D172 completion review, MINOR) originally
        added this class's STAMP_OWNERSHIP_ONLY case, asserting the old
        (wrong) shared-warning behavior; D176 #4 corrects it.
        """
        db_path = tmp_path / ".neuroloom.db"
        gated_outcome = _wc_mod.GatedReconcileOutcome(
            resolvability=_wc_mod.Resolvability.RESOLVED,
            reconcile=_wc_mod.ReconcileOutcome(
                _wc_mod.Decision.STAMP_OWNERSHIP_ONLY,
                None,
                error=(
                    "reconcile() reached an adoption decision "
                    "(STAMP_OWNERSHIP_ONLY) — refusing to execute without "
                    "explicit human consent; run /neuroloom:workspace to "
                    "review and adopt this entry"
                ),
            ),
        )
        text = _run_session_start_with_reconcile_outcome(
            monkeypatch, tmp_path, db_path, gated_outcome
        )

        assert "Workspace routing is already correct for this project" in text
        assert "no conflict to resolve" in text
        assert "isn't one this plugin manages" not in text
        assert "Could not ensure a Neuroloom MCP connection" not in text
        assert "Could not update this project's .mcp.json" not in text


class TestStampOwnershipOnlyDistinctFromSiblings:
    """
    D176 #4 acceptance criterion: WARN_ONLY, STAMP_OWNERSHIP_ONLY, and
    STAMP_OWNERSHIP_WRITE_HEADER must each produce non-empty output, and
    STAMP_OWNERSHIP_ONLY's output must be distinct from its two siblings'.

    Deviation from a literal "all three pairwise distinct" reading of the
    dispatch: per this plan's own Outcome/Guidance for #4 (lines 160, 192),
    WARN_ONLY and STAMP_OWNERSHIP_WRITE_HEADER are *designed* to keep
    sharing the exact same `_UNOWNED_CONFLICT_WARNING` text — only
    STAMP_OWNERSHIP_ONLY gets a new, distinct message. Asserting all three
    pairwise-distinct would fail against the correct implementation. This
    test instead pins the real invariant the plan's purpose clause (line
    194) describes: STAMP_OWNERSHIP_ONLY's message must never collapse back
    to empty or onto the shared warning, and the shared-warning pair must
    keep matching each other (so a future accidental split of that pair is
    also caught).
    """

    def test_stamp_ownership_only_distinct_others_share_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        outputs: dict[_wc_mod.Decision, str] = {}

        warn_only_outcome = _wc_mod.GatedReconcileOutcome(
            resolvability=_wc_mod.Resolvability.RESOLVED,
            reconcile=_wc_mod.ReconcileOutcome(_wc_mod.Decision.WARN_ONLY, None),
        )
        outputs[_wc_mod.Decision.WARN_ONLY] = _run_session_start_with_reconcile_outcome(
            monkeypatch, tmp_path, tmp_path / "warn_only.db", warn_only_outcome
        )

        stamp_only_outcome = _wc_mod.GatedReconcileOutcome(
            resolvability=_wc_mod.Resolvability.RESOLVED,
            reconcile=_wc_mod.ReconcileOutcome(
                _wc_mod.Decision.STAMP_OWNERSHIP_ONLY,
                None,
                error="reconcile() reached an adoption decision (STAMP_OWNERSHIP_ONLY)",
            ),
        )
        outputs[_wc_mod.Decision.STAMP_OWNERSHIP_ONLY] = (
            _run_session_start_with_reconcile_outcome(
                monkeypatch, tmp_path, tmp_path / "stamp_only.db", stamp_only_outcome
            )
        )

        stamp_write_header_outcome = _wc_mod.GatedReconcileOutcome(
            resolvability=_wc_mod.Resolvability.RESOLVED,
            reconcile=_wc_mod.ReconcileOutcome(
                _wc_mod.Decision.STAMP_OWNERSHIP_WRITE_HEADER,
                None,
                error=(
                    "reconcile() reached an adoption decision "
                    "(STAMP_OWNERSHIP_WRITE_HEADER)"
                ),
            ),
        )
        outputs[_wc_mod.Decision.STAMP_OWNERSHIP_WRITE_HEADER] = (
            _run_session_start_with_reconcile_outcome(
                monkeypatch,
                tmp_path,
                tmp_path / "stamp_write_header.db",
                stamp_write_header_outcome,
            )
        )

        for decision, text in outputs.items():
            assert text.strip() != "", f"{decision} produced empty output"

        warn_only_text = outputs[_wc_mod.Decision.WARN_ONLY]
        stamp_only_text = outputs[_wc_mod.Decision.STAMP_OWNERSHIP_ONLY]
        stamp_write_header_text = outputs[_wc_mod.Decision.STAMP_OWNERSHIP_WRITE_HEADER]

        # By design (plan lines 160, 192): WARN_ONLY and
        # STAMP_OWNERSHIP_WRITE_HEADER intentionally share the same
        # _UNOWNED_CONFLICT_WARNING text.
        assert warn_only_text == stamp_write_header_text

        # STAMP_OWNERSHIP_ONLY must be distinct from both — this is the
        # regression this fix and this test exist to guard.
        assert stamp_only_text != warn_only_text
        assert stamp_only_text != stamp_write_header_text

    def test_create_baseline_failure_prints_connection_missing_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / ".neuroloom.db"
        gated_outcome = _wc_mod.GatedReconcileOutcome(
            resolvability=_wc_mod.Resolvability.RESOLVED,
            reconcile=_wc_mod.ReconcileOutcome(
                _wc_mod.Decision.CREATE_BASELINE, _wc_mod.WriteResult.SKIPPED_CONFLICT
            ),
        )
        text = _run_session_start_with_reconcile_outcome(
            monkeypatch, tmp_path, db_path, gated_outcome
        )

        assert "Could not ensure a Neuroloom MCP connection" in text
        assert "(result: skipped_conflict)" in text
        assert "isn't one this plugin manages" not in text

    def test_create_with_header_success_prints_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / ".neuroloom.db"
        gated_outcome = _wc_mod.GatedReconcileOutcome(
            resolvability=_wc_mod.Resolvability.RESOLVED,
            reconcile=_wc_mod.ReconcileOutcome(
                _wc_mod.Decision.CREATE_WITH_HEADER, _wc_mod.WriteResult.SUCCESS
            ),
        )
        text = _run_session_start_with_reconcile_outcome(
            monkeypatch, tmp_path, db_path, gated_outcome
        )

        assert "Could not ensure a Neuroloom MCP connection" not in text
        assert "Could not update this project's .mcp.json" not in text
        assert "isn't one this plugin manages" not in text
        # The rest of the hook must still complete.
        assert "Neuroloom Tool Catalog" in text

    def test_write_header_failure_prints_update_write_failed_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / ".neuroloom.db"
        gated_outcome = _wc_mod.GatedReconcileOutcome(
            resolvability=_wc_mod.Resolvability.RESOLVED,
            reconcile=_wc_mod.ReconcileOutcome(
                _wc_mod.Decision.WRITE_HEADER, _wc_mod.WriteResult.FAILED
            ),
        )
        text = _run_session_start_with_reconcile_outcome(
            monkeypatch, tmp_path, db_path, gated_outcome
        )

        assert "Could not update this project's .mcp.json" in text
        assert "(result: failed)" in text
        assert "Could not ensure a Neuroloom MCP connection" not in text

    def test_clear_header_success_prints_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / ".neuroloom.db"
        gated_outcome = _wc_mod.GatedReconcileOutcome(
            resolvability=_wc_mod.Resolvability.RESOLVED,
            reconcile=_wc_mod.ReconcileOutcome(
                _wc_mod.Decision.CLEAR_HEADER, _wc_mod.WriteResult.SUCCESS
            ),
        )
        text = _run_session_start_with_reconcile_outcome(
            monkeypatch, tmp_path, db_path, gated_outcome
        )

        assert "Could not update this project's .mcp.json" not in text
        assert "Could not ensure a Neuroloom MCP connection" not in text
        assert "isn't one this plugin manages" not in text

    def test_no_op_prints_no_warnings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / ".neuroloom.db"
        gated_outcome = _wc_mod.GatedReconcileOutcome(
            resolvability=_wc_mod.Resolvability.RESOLVED,
            reconcile=_wc_mod.ReconcileOutcome(_wc_mod.Decision.NO_OP, None),
        )
        text = _run_session_start_with_reconcile_outcome(
            monkeypatch, tmp_path, db_path, gated_outcome
        )

        assert "Could not ensure a Neuroloom MCP connection" not in text
        assert "Could not update this project's .mcp.json" not in text
        assert "isn't one this plugin manages" not in text
        # The rest of the hook must still complete — tool catalog is the
        # last thing printed, proving no exception aborted mid-way.
        assert "Neuroloom Tool Catalog" in text


# ---------------------------------------------------------------------------
# D172 Phase 5 — M-12: automatic-axis resolvability-gate negative test.
# Parity with the sibling security tests in test_workspace_config.py
# (TestResolvabilityGateRefusesWrites) — this one proves the gate is
# actually wired into Step 8's real call, not merely that gated_reconcile
# itself refuses in isolation.
# ---------------------------------------------------------------------------


class TestAutomaticAxisResolvabilityGateNegative:
    def test_unreachable_key_never_writes_mcp_json_from_main(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A desired workspace is on file (set via a prior /neuroloom:workspace
        run), but the configured API key doesn't resolve right now (dead
        API base -- the same "present but unreachable" hazard an invalid
        key or a network blip produces). Step 8 must reach zero filesystem
        writes: gated_reconcile's resolvability gate refuses before
        reconcile() is ever called, exactly as
        TestResolvabilityGateRefusesWrites proves one layer down -- this
        test proves that refusal is actually reached from main() itself,
        not merely from a direct gated_reconcile() call in isolation.
        """
        import pyhooks.workspace_config as _wc_prod

        db_path = tmp_path / ".neuroloom.db"
        monkeypatch.chdir(tmp_path)
        output = io.StringIO()

        with (
            patch("pyhooks.session_start._config.load") as mock_load,
            patch("pyhooks.session_start._db.open_db") as mock_open_db,
            patch(
                "pyhooks.session_start._http.post_json",
                return_value=(200, b'{"session_id":"sess-1-aabb"}'),
            ),
            patch("pyhooks.session_start.sys.stdout", output),
            patch("pyhooks.session_start._codeweaver_bootstrap_and_upgrade", return_value=None),
        ):
            import pyhooks.config as _config_mod

            mock_load.return_value = _config_mod.Config(
                api_key="unreachable-key",
                # A dead port -- nothing listening, so the bootstrap fetch
                # inside check_resolvability_with_cache genuinely fails.
                api_base="http://127.0.0.1:1",
                state_db_path=db_path,
                api_key_source="project",
            )
            mock_open_db.side_effect = _real_open_db
            conn = _real_open_db(db_path)
            assert conn is not None
            _wc_prod.write_desired_state(
                db_path,
                _wc_prod.DesiredState(
                    _wc_prod.DesiredKind.SET_TO_VALUE,
                    "90000000-0000-0000-0000-000000000001",
                ),
            )
            conn.close()

            _ss_mod.main()

        text = output.getvalue()
        assert "Could not ensure a Neuroloom MCP connection" not in text
        assert "Could not update this project's .mcp.json" not in text
        assert "isn't one this plugin manages" not in text
        assert not (tmp_path / ".mcp.json").exists()
        # The rest of the hook must still complete.
        assert "Neuroloom Tool Catalog" in text
