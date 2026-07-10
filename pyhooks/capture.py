"""
PostToolUse observation capture hook for neuroloom.

Invoked by Claude Code after every tool use.  Reads the tool event from stdin,
applies a series of guard checks (no API key, no session, MCP self-calls, rate
throttle), and — when all checks pass — ships the observation to the Neuroloom
API in a background thread.  If the POST fails the payload is buffered in the
local SQLite ``event_buffer`` table for later replay.

Design constraints
------------------
- Must exit in under 100 ms.  Network I/O is confined to a daemon-less
  background thread that is joined with a 90 ms timeout.
- Never crashes.  Every code path that could raise is wrapped in try/except.
- Never shares a SQLite connection across threads.  The background thread opens
  its own connection and closes it in a ``finally`` block.
- All decisions are recorded via ``pyhooks.trace.write`` for post-hoc debugging.
"""

import json
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pyhooks.trace as trace
from pyhooks.config import Config, load
from pyhooks.db import open_db
from pyhooks.http import post_json

_SCRIPT = "pyhooks.capture"

# Workspace key is the canonical absolute path of the current working directory.
_SESSION_ID_RE = re.compile(r"^sess-[0-9]+-[a-f0-9]+$")
_MCP_TOOL_RE = re.compile(r"^mcp__neuroloom__")

# Minimum gap between consecutive submits from the same session (milliseconds).
_RATE_LIMIT_MS = 100

# event_buffer hard cap: trim to 8 000 rows once this is exceeded.
_BUFFER_MAX = 10_000
_BUFFER_TRIM_TARGET = 8_000

# HTTP timeout for the background POST (seconds).  The background thread is
# daemon=False and outlives the 90 ms join — this timeout bounds the network
# call itself, not the hook latency.
_HTTP_TIMEOUT = 5.0

# Per-field truncation cap applied to large content-bearing fields before
# serialization.  Must exceed the server's extraction read-depth (up to 8 000
# chars, see _clean_observation_content(..., max_chars=8000) in the API) so no
# legitimate content is lost before extraction runs.
_TRUNCATE_CAP = 10_000

# Content-bearing keys inside tool_input truncated before serialization.
# Verified against real captured PostToolUse events for Write and Edit in
# api/scripts/fixtures/d87_eval_fixtures.json (observation_content field —
# genuine stdin payloads captured during real sessions): Write's tool_input
# has exactly {file_path, content}; Edit's has exactly
# {file_path, old_string, new_string, replace_all}. file_path / notebook_path
# / cell_id / cell_type / edit_mode are deliberately excluded — routing/
# identity fields, not content.
#
# new_source (NotebookEdit) is included even though no NotebookEdit example
# was found in either fixture file to confirm it: it is NotebookEdit's
# documented content-carrying field and can be just as large as an Edit's
# new_string. Truncated defensively — the isinstance guard below makes a wrong
# key name a silent no-op, never a crash, if this guess turns out incorrect.
_TOOL_INPUT_TRUNCATE_KEYS = ("content", "new_string", "old_string", "new_source")

# Content-bearing keys inside tool_response truncated before serialization.
# Verified against the same real captured events: Write's tool_response has
# exactly {type, filePath, content, structuredPatch, originalFile}; Edit's has
# exactly {filePath, oldString, newString, originalFile, structuredPatch,
# userModified, replaceAll}. filePath / structuredPatch / userModified /
# replaceAll / type are deliberately excluded — routing fields or non-string
# structured data, not content. newSource (NotebookEdit) is an unverified,
# defensive addition for the same reason as new_source above.
_TOOL_RESPONSE_TRUNCATE_KEYS = ("content", "oldString", "newString", "originalFile", "newSource")


def _truncate_str(value: str) -> str:
    """Truncate *value* to _TRUNCATE_CAP chars, appending a size marker if cut."""
    if len(value) <= _TRUNCATE_CAP:
        return value
    return value[:_TRUNCATE_CAP] + f"…[truncated: original {len(value)} chars]"


def _truncate_payload_fields(data: dict[str, object]) -> None:
    """
    Truncate oversized content fields of a parsed PostToolUse event in place.

    Only targets known content-bearing keys inside tool_input/tool_response —
    never touches routing/identity fields (file_path, tool_name, session_id,
    etc). isinstance guards at every hop make an absent or unexpected shape a
    silent no-op, matching the hook's never-crash design constraint. Must run
    on the parsed dict before json.dumps — truncating the serialized JSON
    string would corrupt structure.
    """
    tool_input = data.get("tool_input")
    if isinstance(tool_input, dict):
        for key in _TOOL_INPUT_TRUNCATE_KEYS:
            value = tool_input.get(key)
            if isinstance(value, str):
                tool_input[key] = _truncate_str(value)

    tool_response = data.get("tool_response")
    if isinstance(tool_response, dict):
        for key in _TOOL_RESPONSE_TRUNCATE_KEYS:
            value = tool_response.get(key)
            if isinstance(value, str):
                tool_response[key] = _truncate_str(value)


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------


def _submit(
    db_path: Path,
    api_base: str,
    api_key: str,
    batch_payload: dict[str, object],
    single_obs_json: str,
) -> None:
    """
    POST the batch payload to the API.  On failure, buffer the single
    observation in ``event_buffer``.

    This function runs in a background thread and must open its own DB
    connection — never touches the main thread's connection.
    """
    conn: sqlite3.Connection | None = None
    try:
        conn = open_db(db_path)

        url = f"{api_base}/api/v1/observations/batch"
        headers = {"Authorization": f"Token {api_key}"}
        body = json.dumps(batch_payload).encode("utf-8")

        result = post_json(url, headers, body, timeout=_HTTP_TIMEOUT)

        if result is not None and 200 <= result[0] < 300:
            if conn is not None:
                trace.write(conn, _SCRIPT, "api_completed")
            return

        # Network failure (result is None) or non-2xx response: buffer the
        # observation so a future session can replay it.
        status_detail = f"status={result[0]}" if result is not None else "network_error"
        if conn is not None:
            trace.write(conn, _SCRIPT, "api_errored", detail=status_detail)

        if conn is not None:
            conn.execute(
                "INSERT INTO event_buffer (payload, created_at, payload_type) VALUES (?, ?, ?)",
                (single_obs_json, time.time(), "observation"),
            )
            conn.commit()

            # Enforce the row cap: trim to _BUFFER_TRIM_TARGET if over _BUFFER_MAX.
            row = conn.execute("SELECT COUNT(*) FROM event_buffer").fetchone()
            if row is not None and int(row[0]) > _BUFFER_MAX:
                conn.execute(
                    "DELETE FROM event_buffer WHERE id NOT IN ("
                    "SELECT id FROM event_buffer ORDER BY id DESC LIMIT "
                    + str(_BUFFER_TRIM_TARGET)
                    + ")"
                )
                conn.commit()

    except Exception:
        # Background thread must never propagate — the join() in main already
        # limits the impact of slowness; a crash here would be silently lost
        # anyway since daemon=False threads don't print to the hook's stderr.
        if conn is not None:
            try:
                trace.write(conn, _SCRIPT, "buffer_error")
            except Exception:
                pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Capture a PostToolUse event and forward it to the Neuroloom API."""
    # ------------------------------------------------------------------ 1
    config: Config = load()

    # ------------------------------------------------------------------ 2
    # No API key → nothing to submit; skip even opening the DB.
    if not config.api_key:
        sys.exit(0)

    # ------------------------------------------------------------------ 3
    conn = open_db(config.state_db_path)
    if conn is None:
        sys.exit(0)

    session_id: str | None = None

    try:
        workspace_key = str(Path(os.getcwd()).resolve())

        # -------------------------------------------------------------- 4
        row = conn.execute(
            "SELECT session_id, last_submit_ms FROM sessions WHERE session_key = ?",
            (workspace_key,),
        ).fetchone()

        if row is None:
            trace.write(conn, _SCRIPT, "no_session")
            sys.exit(0)

        session_id = str(row["session_id"])
        last_submit_ms: int = int(row["last_submit_ms"])

        # -------------------------------------------------------------- 5
        if not _SESSION_ID_RE.match(session_id):
            # Use a fixed sentinel instead of writing the raw session_id to the
            # trace table to avoid persisting potentially attacker-controlled data.
            trace.write(conn, _SCRIPT, "corrupt_session", detail="<invalid>")
            conn.execute("DELETE FROM sessions WHERE session_key = ?", (workspace_key,))
            conn.commit()
            sys.exit(0)

        # -------------------------------------------------------------- 6
        raw_stdin = sys.stdin.read()
        if not raw_stdin.strip():
            trace.write(conn, _SCRIPT, "empty_input", session_id=session_id)
            sys.exit(0)

        try:
            data: dict[str, object] = json.loads(raw_stdin)
        except json.JSONDecodeError:
            trace.write(conn, _SCRIPT, "malformed_input", session_id=session_id)
            sys.exit(0)

        # Extract agent context (CC v2.1.69+). Both fields are absent on main-thread events.
        # isinstance guard satisfies mypy --strict and handles non-string values defensively.
        # [:100] truncation matches the API's String(100) column to prevent 422s.
        _raw_aid = data.get("agent_id")
        agent_id: str | None = str(_raw_aid)[:100] if isinstance(_raw_aid, str) else None
        _raw_atype = data.get("agent_type")
        agent_type: str | None = str(_raw_atype)[:100] if isinstance(_raw_atype, str) else None

        # -------------------------------------------------------------- 7
        tool_name: str = str(data.get("tool_name") or data.get("name") or "unknown")

        # -------------------------------------------------------------- 8
        if _MCP_TOOL_RE.match(tool_name):
            trace.write(
                conn,
                _SCRIPT,
                "mcp_filtered",
                session_id=session_id,
                tool_name=tool_name,
            )
            sys.exit(0)

        # -------------------------------------------------------------- 9
        now_ms = int(time.time() * 1000)
        if now_ms - last_submit_ms < _RATE_LIMIT_MS:
            trace.write(
                conn,
                _SCRIPT,
                "rate_throttled",
                session_id=session_id,
                tool_name=tool_name,
            )
            sys.exit(0)

        # -------------------------------------------------------------- 10
        observation_id = str(uuid.uuid4())
        observed_at = datetime.now(timezone.utc).isoformat()
        _truncate_payload_fields(data)
        content_str = json.dumps(data, separators=(",", ":"))

        single_obs: dict[str, object] = {
            "observation_id": observation_id,
            "session_id": session_id,
            "observed_at": observed_at,
            "category": tool_name,
            "content": content_str,
            "agent_id": agent_id,
            "agent_type": agent_type,
        }
        batch_payload: dict[str, object] = {"observations": [single_obs]}

        # -------------------------------------------------------------- 11
        # Update last_submit_ms BEFORE spawning the thread to prevent a race
        # where two concurrent hook processes both pass the rate check.
        conn.execute(
            "UPDATE sessions SET last_submit_ms = ? WHERE session_key = ?",
            (now_ms, workspace_key),
        )
        conn.commit()

        # -------------------------------------------------------------- 12
        trace.write(
            conn,
            _SCRIPT,
            "matched",
            session_id=session_id,
            tool_name=tool_name,
        )

        # -------------------------------------------------------------- 13
        # The background thread must open its own DB connection.  We pass
        # db_path so it can call open_db independently.
        single_obs_json = json.dumps(single_obs, separators=(",", ":"))

        t = threading.Thread(
            target=_submit,
            args=(
                config.state_db_path,
                config.api_base,
                config.api_key,
                batch_payload,
                single_obs_json,
            ),
            daemon=False,
        )
        t.start()
        t.join(timeout=0.090)

    finally:
        # -------------------------------------------------------------- 14
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # Crash safety: hooks must never surface tracebacks to the Claude Code
        # process.  Silently exit with 0 so the tool use completes normally.
        sys.exit(0)
