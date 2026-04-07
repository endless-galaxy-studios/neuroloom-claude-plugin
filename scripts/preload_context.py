#!/usr/bin/env python3
"""preload_context.py — PreToolUse helper: fetch and inject Neuroloom context.

Supports two modes:

    inject  — fired by Read: queries the composite POST /api/v1/context endpoint
              with format=inject and writes additionalContext into the Claude
              conversation window.

    nudge   — fired by Glob/Grep: extracts a meaningful query from the tool's
              pattern, queries the same endpoint with format=nudge, and injects
              a compact "you may want to memory_search for X" reminder.

Writes a Claude hook JSON object to stdout:

    {"additionalContext": "<text>"}   — when context is available
    {}                                — when context is empty, budget exhausted, or any failure

Uses only stdlib — zero pip dependencies.

Exit code is always 0. A crash here must never block Claude from reading a file.

Domain note: "additionalContext" is the Claude hook protocol field that injects text into
Claude's context window before the tool runs — like sticky notes placed on a file before
someone opens it, giving them relevant background without them having to ask for it.
"""

import argparse
import hashlib
import json
import os
import random
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Tracing
# ---------------------------------------------------------------------------

def _trace(state_dir: str, decision: str) -> None:
    """Write a structured trace entry to state_dir/trace.jsonl.

    Mirrors the JSONL schema written by nl_trace_write in trace.sh so that
    all hook events appear in a single queryable file. Never raises.
    """
    try:
        trace_file = os.path.join(state_dir, "trace.jsonl")
        entry = json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "script": "preload-context",
            "decision": decision,
        })
        with open(trace_file, "a") as f:
            f.write(entry + "\n")
    except Exception:
        pass  # Tracing must never crash the hook


# ---------------------------------------------------------------------------
# Query extraction for nudge mode
# ---------------------------------------------------------------------------

def _extract_query(tool_name: str, pattern: str) -> str | None:
    """Extract a meaningful search term from a Glob or Grep pattern.

    For Glob patterns, we look for the most specific path segment that actually
    names something — skipping wildcards-only segments. Think of it like finding
    the most descriptive word in a file path after stripping the wildcards.

    For Grep patterns, we strip regex metacharacters and keep the plain text
    tokens. Think of it like pulling the readable words out of a regex expression.

    Returns None if no term of at least 3 characters can be extracted.
    """
    if not pattern:
        return None

    if tool_name == "Glob":
        segments = pattern.split("/")
        # Walk segments right-to-left, find the last segment that is not
        # purely wildcards and does not start with a wildcard character.
        for segment in reversed(segments):
            if not segment:
                continue
            # Skip segments that are entirely wildcard characters
            if all(c in "*?" for c in segment):
                continue
            # Skip segments that start with a wildcard (e.g. "*.tsx")
            if segment.startswith("*"):
                continue
            # Strip file extension (everything after the last dot)
            if "." in segment:
                stem = segment.rsplit(".", 1)[0]
            else:
                stem = segment
            # Strip any remaining wildcard characters
            stem = stem.replace("*", "").replace("?", "")
            if len(stem) >= 3:
                return stem
        return None

    elif tool_name == "Grep":
        # Strip regex escape sequences (e.g. \s, \w, \d)
        cleaned = re.sub(r"\\[a-zA-Z]", "", pattern)
        # Strip regex metacharacters
        cleaned = re.sub(r"[.*+^$()[\]{}|?]", " ", cleaned)
        # Split on whitespace and punctuation, keep tokens >= 3 chars
        tokens = [t for t in re.split(r"[\s_\-]+", cleaned) if len(t) >= 3]
        if not tokens:
            return None
        query = " ".join(tokens)
        if len(query) < 3:
            return None
        return query

    return None


# ---------------------------------------------------------------------------
# Cache key
# ---------------------------------------------------------------------------

def _cache_key(file_path: str, workspace_root: str) -> str:
    """Compute a stable cache key from the file path and workspace root.

    The null-byte separator prevents boundary-shift collisions: without it,
    ("/a", "/bc") and ("/ab", "/c") would produce the same concatenated string
    and collide to the same cache entry even though they refer to different files.
    """
    raw = file_path + "\x00" + workspace_root
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_key_nudge(query: str, workspace_root: str) -> str:
    """Compute a stable cache key for a nudge result keyed by query + workspace.

    The "\x00nudge" suffix namespaces nudge entries away from inject entries,
    preventing a query string that happens to match a file path from colliding
    with a file-scoped inject cache entry.
    """
    raw = query + "\x00" + workspace_root + "\x00nudge"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# SQLite cache
# ---------------------------------------------------------------------------

def _open_cache(state_dir: str) -> sqlite3.Connection | None:
    """Open (or create) the SQLite cache database.

    Uses WAL mode for concurrent hook processes — think of WAL (Write-Ahead Log)
    as a scratch pad that multiple readers can consult simultaneously while one
    writer is still working. Without WAL, a writing process would lock the whole
    database and concurrent hooks would stall.

    Returns None if the DB cannot be opened within 1 second — better to skip
    the cache than to stall Claude waiting for a lock.
    """
    try:
        db_path = os.path.join(state_dir, "context_cache.db")
        conn = sqlite3.connect(db_path, timeout=1.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS cache "
            "(cache_key TEXT PRIMARY KEY, context TEXT, created_at REAL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS token_budget "
            "(session_id TEXT PRIMARY KEY, total_chars INTEGER)"
        )
        conn.commit()
        return conn
    except Exception:
        return None


def _cache_get(conn: sqlite3.Connection, key: str, ttl: float = 300.0) -> str | None:
    """Return cached context if present and not expired, else None.

    TTL is 300 seconds (5 minutes) — long enough to cover a focused editing
    session on one file, short enough that new memories captured mid-session
    will appear on the next cache miss.
    """
    try:
        cutoff = time.time() - ttl
        row = conn.execute(
            "SELECT context FROM cache WHERE cache_key = ? AND created_at >= ?",
            (key, cutoff),
        ).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _cache_set(conn: sqlite3.Connection, key: str, context: str) -> None:
    """Insert or replace a cache entry with the current timestamp."""
    try:
        conn.execute(
            "INSERT OR REPLACE INTO cache (cache_key, context, created_at) VALUES (?, ?, ?)",
            (key, context, time.time()),
        )
        conn.commit()
    except Exception:
        pass


def _cache_expire_opportunistic(conn: sqlite3.Connection, ttl: float = 300.0) -> None:
    """Opportunistically remove expired rows ~2% of the time.

    Running a full DELETE on every invocation would slow down the common path.
    Instead, we roll the dice: 1-in-50 calls does the cleanup. This keeps the
    database from growing unboundedly without adding latency to 98% of calls.
    """
    if random.random() < 0.02:
        try:
            cutoff = time.time() - ttl
            conn.execute("DELETE FROM cache WHERE created_at < ?", (cutoff,))
            conn.commit()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

def _circuit_breaker_active(state_dir: str, cooldown: float = 30.0) -> bool:
    """Return True if the API was recently unavailable and we should skip the call.

    The circuit breaker is a lightweight file-based mechanism:
    - When an API call times out or fails, preload_context.py writes the current
      timestamp to api_unavailable.
    - On the next invocation, if that file exists and is less than 30 seconds old,
      we skip the API call entirely. This avoids hammering a degraded API with
      requests every time Claude reads a file (which could be dozens per minute).
    - On a successful API call, the file is deleted and the breaker resets.

    Think of it like a fuse box: when something overloads the circuit, the fuse blows
    and you can't use the outlet for 30 seconds. After that, you try again.
    """
    cb_file = os.path.join(state_dir, "api_unavailable")
    if not os.path.exists(cb_file):
        return False
    try:
        with open(cb_file) as f:
            ts = float(f.read().strip())
        return (time.time() - ts) < cooldown
    except Exception:
        # Unreadable file — treat as no breaker
        return False


def _circuit_breaker_trip(state_dir: str) -> None:
    """Record API unavailability — trips the circuit breaker."""
    try:
        cb_file = os.path.join(state_dir, "api_unavailable")
        with open(cb_file, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def _circuit_breaker_reset(state_dir: str) -> None:
    """Delete the circuit breaker file on successful API response."""
    try:
        cb_file = os.path.join(state_dir, "api_unavailable")
        if os.path.exists(cb_file):
            os.remove(cb_file)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Token budget
# ---------------------------------------------------------------------------

TOTAL_CHAR_BUDGET = 30_000
"""Maximum cumulative characters injected across all preload calls in a session.

Token budgeting works like a shared word count for a meeting: once you've used
your allotment of words, you stop injecting new information even if more context
is available. This prevents the injection hook from monopolising Claude's context
window when working on many files in sequence.

30,000 characters ≈ ~7,500 tokens — a meaningful amount without dominating a typical
context window.
"""


def _budget_check_and_update(
    conn: sqlite3.Connection,
    session_id: str,
    injection_text: str,
) -> bool:
    """Return True if there is budget for this injection and update the counter.

    Checks BEFORE injecting. If the text would push the session over the limit,
    returns False and leaves the budget unchanged.
    """
    try:
        row = conn.execute(
            "SELECT total_chars FROM token_budget WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        accumulated = row[0] if row else 0

        if accumulated + len(injection_text) > TOTAL_CHAR_BUDGET:
            return False

        conn.execute(
            "INSERT OR REPLACE INTO token_budget (session_id, total_chars) VALUES (?, ?)",
            (session_id, accumulated + len(injection_text)),
        )
        conn.commit()
        return True
    except Exception:
        # On any DB error, allow the injection rather than silently suppressing context
        return True


# ---------------------------------------------------------------------------
# API call
# ---------------------------------------------------------------------------

def _fetch_context(
    api_base: str,
    api_key: str,
    file_path: str,
    timeout: float = 3.0,
    fmt: str = "inject",
    query: str | None = None,
) -> tuple[str, int] | None:
    """POST to /api/v1/context and return (text, ttl_seconds), or None on failure.

    The 3-second timeout is intentionally tight: PreToolUse hooks fire in the
    critical path before Claude reads a file. If the API takes longer than 3
    seconds, Claude's entire response is delayed — so we prefer injecting nothing
    over making the user wait.

    Mode determines the payload:
      inject — sends file_path + format=inject, reads injection_text from response
      nudge  — sends query + format=nudge, reads nudge_text from response

    The null payload field (query for inject mode, file_path for nudge mode) is
    intentionally omitted to avoid confusing the API endpoint.
    """
    url = api_base.rstrip("/") + "/api/v1/context"

    if fmt == "nudge":
        payload_dict: dict[str, str] = {"query": query or "", "format": "nudge"}
    else:
        payload_dict = {"file_path": file_path, "format": "inject"}

    payload = json.dumps(payload_dict).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Token {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            text = data.get("injection_text", "") or data.get("nudge_text", "")
            ttl = int(data.get("cache_hint_ttl_seconds", 300))
            return (text, ttl)
    except urllib.error.HTTPError:
        # 4xx/5xx — not a network failure, don't trip the circuit breaker
        return None
    except (urllib.error.URLError, TimeoutError):
        # Connection refused, DNS failure, timeout — re-raise so caller
        # can trip the circuit breaker.
        raise


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch Neuroloom context and write Claude hook JSON to stdout.",
    )
    parser.add_argument(
        "--state-dir",
        required=True,
        metavar="STATE_DIR",
        help="Path to the .neuroloom/ state directory.",
    )
    parser.add_argument(
        "--mode",
        choices=["inject", "nudge"],
        default="inject",
        help="Hook mode: inject (Read) or nudge (Glob/Grep).",
    )
    args = parser.parse_args()
    state_dir: str = args.state_dir
    mode: str = args.mode

    # Read configuration from environment (not CLI args — keeps secrets off ps output)
    file_path = os.environ.get("NEUROLOOM_FILE_PATH", "")
    api_key = os.environ.get("NEUROLOOM_API_KEY", "")
    api_base = os.environ.get("NEUROLOOM_API_BASE", "https://api.neuroloom.dev")
    workspace_root = os.environ.get("NEUROLOOM_WORKSPACE_ROOT", "")
    session_id = os.environ.get("SESSION_ID", "default")
    query_pattern = os.environ.get("NEUROLOOM_QUERY_PATTERN", "")
    tool_name_env = os.environ.get("NEUROLOOM_HOOK_TOOL", "")

    # Guard: mode-specific required inputs must be present
    if (mode == "inject" and not file_path) or (mode == "nudge" and not query_pattern) or not api_key:
        print(json.dumps({}))
        return

    # ---------------------------------------------------------------------------
    # Nudge mode: extract a meaningful query from the raw pattern
    # ---------------------------------------------------------------------------
    extracted_query: str | None = None
    if mode == "nudge":
        extracted_query = _extract_query(tool_name_env, query_pattern)
        if not extracted_query:
            _trace(state_dir, "nudge_no_query")
            print(json.dumps({}))
            return

    # ---------------------------------------------------------------------------
    # Circuit breaker check — skip API call if recently unavailable
    # ---------------------------------------------------------------------------
    if _circuit_breaker_active(state_dir):
        _trace(state_dir, "api_unavailable")
        print(json.dumps({}))
        return

    # ---------------------------------------------------------------------------
    # Open cache (non-fatal if unavailable)
    # ---------------------------------------------------------------------------
    conn = _open_cache(state_dir)

    if mode == "inject":
        cache_key = _cache_key(file_path, workspace_root)
    else:
        # extracted_query is non-None here — guarded by the early return above
        assert extracted_query is not None
        cache_key = _cache_key_nudge(extracted_query, workspace_root)

    # Opportunistic expiry — runs ~2% of the time to keep the DB lean
    if conn is not None:
        _cache_expire_opportunistic(conn)

    # ---------------------------------------------------------------------------
    # Cache lookup
    # ---------------------------------------------------------------------------
    injection_text: str | None = None
    if conn is not None:
        cached = _cache_get(conn, cache_key)
        if cached is not None:
            _trace(state_dir, "cache_hit")
            injection_text = cached
        else:
            _trace(state_dir, "cache_miss")

    # ---------------------------------------------------------------------------
    # API call (on cache miss)
    # ---------------------------------------------------------------------------
    if injection_text is None:
        try:
            result = _fetch_context(
                api_base,
                api_key,
                file_path,
                fmt=mode,
                query=extracted_query if mode == "nudge" else None,
            )
        except (TimeoutError, urllib.error.URLError):
            # Network-level failure — trip the circuit breaker
            _circuit_breaker_trip(state_dir)
            _trace(state_dir, "api_timeout")
            print(json.dumps({}))
            return
        except Exception:
            # Any other unexpected failure — degrade silently
            _trace(state_dir, "api_error_unexpected")
            print(json.dumps({}))
            return

        if result is None:
            # HTTP error (4xx/5xx) — don't trip the breaker, just return empty
            _trace(state_dir, "api_error_http")
            print(json.dumps({}))
            return

        # Successful API response — reset circuit breaker and populate cache
        _circuit_breaker_reset(state_dir)
        _trace(state_dir, "api_ok")
        text, ttl = result
        injection_text = text

        # Do not cache empty results — a cache hit on an empty response
        # would hide memories stored mid-session until the TTL expires
        # (bug fix, D83).
        if conn is not None and injection_text:
            _cache_set(conn, cache_key, injection_text)

    # ---------------------------------------------------------------------------
    # Empty context — nothing to inject
    # ---------------------------------------------------------------------------
    if not injection_text:
        if mode == "nudge":
            _trace(state_dir, "nudge_empty")
        else:
            _trace(state_dir, "empty")
        print(json.dumps({}))
        return

    # ---------------------------------------------------------------------------
    # Token budget check — BEFORE constructing output
    # ---------------------------------------------------------------------------
    if conn is not None and not _budget_check_and_update(conn, session_id, injection_text):
        _trace(state_dir, "budget_exhausted")
        print(json.dumps({}))
        return

    # ---------------------------------------------------------------------------
    # Inject context
    # The "additionalContext" field is the Claude hook protocol mechanism for
    # inserting text into the conversation window before the tool runs.
    # Omit "permissionDecisionReason" entirely — that field is for blocking hooks,
    # not passive injection hooks.
    # ---------------------------------------------------------------------------
    if mode == "nudge":
        _trace(state_dir, "nudge_injected")
    else:
        _trace(state_dir, "injected")
    print(json.dumps({"additionalContext": injection_text}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Last-resort catch: never let an exception surface to the hook runtime.
        # Print empty output so Claude proceeds without context rather than failing.
        print(json.dumps({}))
        sys.exit(0)
