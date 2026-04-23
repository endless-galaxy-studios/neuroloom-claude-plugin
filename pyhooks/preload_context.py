"""
hooks/preload_context.py — PreToolUse context injection hook.

Supports two modes, determined by the tool_name field in the stdin JSON:

    inject  — fired when tool_name == "Read": queries the composite
              POST /api/v1/context endpoint with format=inject and writes
              additionalContext into the Claude conversation window.

    nudge   — fired when tool_name in ("Glob", "Grep") or when tool_name ==
              "Bash" with a bfs or ugrep subcommand (native macOS/Linux builds
              route Glob/Grep through these tools since v2.1.117): extracts a
              meaningful query from the tool's pattern, queries the same
              endpoint with format=nudge, and injects a compact "you may want
              to memory_search for X" reminder.

Writes a Claude hook JSON object to stdout:

    {"additionalContext": "<text>"}   — when context is available
    {}                                — when context is empty, budget exhausted,
                                        or any failure

State is stored in the shared .neuroloom.db SQLite file opened by
``pyhooks.db.open_db``.  The circuit breaker, cache, token budget, and trace
tables are all part of that shared schema — no separate database file is
created by this module.

Uses only stdlib — zero pip dependencies.

Exit code is always 0.  A crash here must never block Claude from reading
a file.

Domain note: "additionalContext" is the Claude hook protocol field that
injects text into Claude's context window before the tool runs — like sticky
notes placed on a file before someone opens it, giving relevant background
without having to ask for it.
"""

import hashlib
import json
import os
import random
import re
import shlex
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from pyhooks import config as _config_mod
from pyhooks import db as _db_mod
from pyhooks import trace as _trace_mod

# ---------------------------------------------------------------------------
# Session ID validation
# ---------------------------------------------------------------------------

_SESSION_ID_RE = re.compile(r"^sess-[0-9]+-[a-f0-9]+$")


# ---------------------------------------------------------------------------
# Query extraction for nudge mode
# ---------------------------------------------------------------------------


def _extract_query(tool_name: str, pattern: str) -> str | None:
    """Extract a meaningful search term from a Glob or Grep pattern.

    bfs and ugrep patterns are normalised to Glob and Grep respectively at the
    call site (via the effective_tool mapping in main()) before this function is
    invoked — so this function always receives a standard tool name and a
    pre-extracted pattern string, never "bfs" or "ugrep" as the tool argument.

    For Glob patterns, we look for the most specific path segment that actually
    names something — skipping wildcards-only segments.  Think of it like
    finding the most descriptive word in a file path after stripping the
    wildcards.

    For Grep patterns, we strip regex metacharacters and keep the plain text
    tokens.  Think of it like pulling the readable words out of a regex
    expression.

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


def _extract_bash_pattern(sub_tool: str, command: str) -> str:
    """Extract the search pattern from a bfs or ugrep command string.

    bfs uses POSIX find-compatible flags: -name, -path, -iname, -ipath.
    The pattern is the argument immediately following the flag.

    ugrep uses the pattern as the first non-flag positional argument.
    This mirrors how Grep patterns work: the search term comes before
    the path argument.

    Returns an empty string if no pattern can be extracted — the caller
    will treat that as a no-op nudge.
    """
    if sub_tool == "bfs":
        # Match -name/-path/-iname/-ipath followed by a quoted or unquoted value.
        # The unquoted group uses [^\s|&;>]+ to avoid capturing shell pipe/redirect
        # characters (e.g. `bfs . -name file.ts|grep foo` must not capture `file.ts|grep`).
        m = re.search(
            r"-(?:i?name|i?path)\s+(?:\"([^\"]+)\"|\'([^\']+)\'|([^\s|&;>]+))",
            command,
        )
        if m:
            return m.group(1) or m.group(2) or m.group(3) or ""
        return ""

    elif sub_tool == "ugrep":
        # shlex.split honours quoted tokens, so `ugrep -r "async def" .` is
        # tokenised as ["ugrep", "-r", "async def", "."] rather than splitting
        # on the space inside the quoted string.
        try:
            tokens = shlex.split(command)
        except ValueError:
            # Malformed quoting — return empty rather than crashing.
            return ""
        pattern = ""
        skip_next = False
        capture_next = False
        for tok in tokens:
            if capture_next:
                # Previous token was -e; this token IS the pattern.
                pattern = tok
                break
            if skip_next:
                skip_next = False
                continue
            if tok == "-e":
                # -e PATTERN: explicit pattern flag — next token IS the pattern.
                capture_next = True
                continue
            # Flags that consume the next argument but are not the pattern.
            if tok in ("-f", "-m", "-A", "-B", "-C",
                       "--include", "--exclude",
                       "--include-dir", "--exclude-dir"):
                skip_next = True
                continue
            if tok.startswith("-e") and len(tok) > 2:
                pattern = tok[2:]
                break
            if tok.startswith("-"):
                continue
            if tok == "ugrep":
                continue
            # First non-flag, non-program-name token is the pattern.
            pattern = tok
            break
        return pattern

    return ""


# ---------------------------------------------------------------------------
# Cache key
# ---------------------------------------------------------------------------


def _cache_key(file_path: str, workspace_root: str) -> str:
    """Compute a stable cache key from the file path and workspace root.

    The null-byte separator prevents boundary-shift collisions: without it,
    ("/a", "/bc") and ("/ab", "/c") would produce the same concatenated string
    and collide to the same cache entry even though they refer to different
    files.
    """
    raw = file_path + "\x00" + workspace_root
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_key_nudge(query: str, workspace_root: str) -> str:
    """Compute a stable cache key for a nudge result keyed by query + workspace.

    The "\\x00nudge" suffix namespaces nudge entries away from inject entries,
    preventing a query string that happens to match a file path from colliding
    with a file-scoped inject cache entry.
    """
    raw = query + "\x00" + workspace_root + "\x00nudge"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Cache helpers (operate on the shared .neuroloom.db `cache` table)
# ---------------------------------------------------------------------------


def _cache_get(conn: sqlite3.Connection, key: str, ttl: float = 3600.0) -> str | None:
    """Return cached context if present and not expired, else None.

    TTL is 3600 seconds (1 hour) — long enough to cover an extended editing
    session on a set of files, short enough that memories captured mid-session
    will appear on the next cache miss.
    """
    try:
        cutoff = time.time() - ttl
        row = conn.execute(
            "SELECT context FROM cache WHERE cache_key = ? AND created_at >= ?",
            (key, cutoff),
        ).fetchone()
        return str(row[0]) if row is not None else None
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


def _cache_expire_opportunistic(conn: sqlite3.Connection, ttl: float = 3600.0) -> None:
    """Opportunistically remove expired rows ~2% of the time.

    Running a full DELETE on every invocation would slow down the common path.
    Instead, we roll the dice: 1-in-50 calls does the cleanup.  This keeps the
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
# Circuit breaker (DB-backed, single row id=1 in circuit_breaker table)
# ---------------------------------------------------------------------------


def _circuit_breaker_active(conn: sqlite3.Connection | None, cooldown: float = 30.0) -> bool:
    """Return True if the API was recently unavailable and we should skip the call.

    The circuit breaker is a lightweight DB-backed mechanism:
    - When an API call times out or fails, the current timestamp is written to
      the ``circuit_breaker`` table (id=1).
    - On the next invocation, if that row exists and is less than 30 seconds
      old, we skip the API call entirely.  This avoids hammering a degraded API
      with requests every time Claude reads a file (which could be dozens per
      minute).
    - On a successful API call, the row is deleted and the breaker resets.

    Think of it like a fuse box: when something overloads the circuit, the fuse
    blows and you can't use the outlet for 30 seconds.  After that, you try again.
    """
    if conn is None:
        return False
    try:
        row = conn.execute("SELECT tripped_at FROM circuit_breaker WHERE id = 1").fetchone()
        if row is None:
            return False
        return (time.time() - float(row[0])) < cooldown
    except Exception:
        return False


def _circuit_breaker_trip(conn: sqlite3.Connection | None) -> None:
    """Record API unavailability — trips the circuit breaker."""
    if conn is None:
        return
    try:
        conn.execute(
            "INSERT OR REPLACE INTO circuit_breaker (id, tripped_at) VALUES (1, ?)",
            (time.time(),),
        )
        conn.commit()
    except Exception:
        pass


def _circuit_breaker_reset(conn: sqlite3.Connection | None) -> None:
    """Delete the circuit breaker row on successful API response."""
    if conn is None:
        return
    try:
        conn.execute("DELETE FROM circuit_breaker WHERE id = 1")
        conn.commit()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Token budget
# ---------------------------------------------------------------------------

TOTAL_CHAR_BUDGET = 30_000
"""Maximum cumulative characters injected across all preload calls in a session.

Token budgeting works like a shared word count for a meeting: once you've used
your allotment of words, you stop injecting new information even if more context
is available.  This prevents the injection hook from monopolising Claude's
context window when working on many files in sequence.

30,000 characters ≈ ~7,500 tokens — a meaningful amount without dominating a
typical context window.
"""


def _budget_check_and_update(
    conn: sqlite3.Connection,
    session_id: str,
    injection_text: str,
) -> bool:
    """Return True if there is budget for this injection and update the counter.

    Checks BEFORE injecting.  If the text would push the session over the
    limit, returns False and leaves the budget unchanged.
    """
    try:
        row = conn.execute(
            "SELECT total_chars FROM token_budget WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        accumulated = int(row[0]) if row is not None else 0

        if accumulated + len(injection_text) > TOTAL_CHAR_BUDGET:
            return False

        conn.execute(
            "INSERT OR REPLACE INTO token_budget (session_id, total_chars) VALUES (?, ?)",
            (session_id, accumulated + len(injection_text)),
        )
        conn.commit()
        return True
    except Exception:
        # On any DB error, allow the injection rather than silently suppressing
        # context.
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
    critical path before Claude reads a file.  If the API takes longer than 3
    seconds, Claude's entire response is delayed — so we prefer injecting
    nothing over making the user wait.

    Mode determines the payload:
      inject — sends file_path + format=inject, reads injection_text from
               response
      nudge  — sends query + format=nudge, reads nudge_text from response

    The null payload field (query for inject mode, file_path for nudge mode)
    is intentionally omitted to avoid confusing the API endpoint.

    Raises urllib.error.URLError / TimeoutError on network-level failures so
    the caller can trip the circuit breaker.  Returns a 3-tuple sentinel
    ("__http_error__", status_code, body) for HTTP-level errors (4xx/5xx) so
    the caller can trace the status without tripping the breaker.
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
            "User-Agent": "neuroloom",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            text = data.get("injection_text", "") or data.get("nudge_text", "")
            ttl = int(data.get("cache_hint_ttl_seconds", 300))
            return (text, ttl)
    except urllib.error.HTTPError as exc:
        # 4xx/5xx — not a network failure, don't trip the circuit breaker.
        # Return error info as a 3-tuple so callers can trace the status code.
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        return ("__http_error__", exc.code, body)  # type: ignore[return-value]
    except (urllib.error.URLError, TimeoutError):
        # Connection refused, DNS failure, timeout — re-raise so caller
        # can trip the circuit breaker.
        raise


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point: read stdin JSON, fetch context, write hook JSON to stdout."""
    # ------------------------------------------------------------------
    # Load config (never raises)
    # ------------------------------------------------------------------
    cfg = _config_mod.load()

    # ------------------------------------------------------------------
    # Open the shared state DB (non-fatal if unavailable)
    # ------------------------------------------------------------------
    conn = _db_mod.open_db(cfg.state_db_path)

    try:
        # --------------------------------------------------------------
        # Read stdin JSON — the Claude hook runtime passes tool_name and
        # tool_input as a JSON object on stdin.
        # --------------------------------------------------------------
        try:
            raw = sys.stdin.read()
            hook_input: dict[str, object] = json.loads(raw) if raw.strip() else {}
        except Exception:
            hook_input = {}

        tool_name = str(hook_input.get("tool_name", ""))
        tool_input = hook_input.get("tool_input", {})
        if not isinstance(tool_input, dict):
            tool_input = {}

        # --------------------------------------------------------------
        # Bash sub-tool detection (native builds route Glob → bfs, Grep → ugrep)
        # --------------------------------------------------------------
        _cmd = ""
        _bash_sub_tool: str | None = None
        if tool_name == "Bash":
            _cmd = str(tool_input.get("command", ""))
            if re.match(r"\s*bfs\b", _cmd):
                _bash_sub_tool = "bfs"
            elif re.match(r"\s*ugrep\b", _cmd):
                _bash_sub_tool = "ugrep"

        if tool_name in ("Glob", "Grep"):
            mode = "nudge"
        elif tool_name == "Bash" and _bash_sub_tool is not None:
            mode = "nudge"
        elif tool_name == "Read":
            mode = "inject"
        else:
            # Unknown tool — nothing to do
            print(json.dumps({}))
            return

        # --------------------------------------------------------------
        # Extract mode-specific inputs from tool_input
        # --------------------------------------------------------------
        file_path = str(tool_input.get("file_path", "")) if mode == "inject" else ""
        if mode == "nudge":
            if tool_name == "Bash" and _bash_sub_tool is not None:
                query_pattern = _extract_bash_pattern(_bash_sub_tool, _cmd)
            else:
                query_pattern = str(tool_input.get("pattern", ""))
        else:
            query_pattern = ""

        # Workspace root is the process working directory (resolved)
        workspace_root = str(Path(os.getcwd()).resolve())

        # --------------------------------------------------------------
        # Guard: required inputs and API key must be present
        # --------------------------------------------------------------
        if (
            (mode == "inject" and not file_path)
            or (mode == "nudge" and not query_pattern)
            or not cfg.api_key
        ):
            print(json.dumps({}))
            return

        # --------------------------------------------------------------
        # Session ID — look up from sessions table by workspace key.
        # Falls back to workspace_root string if lookup fails or value
        # is invalid (corrupt session).
        # --------------------------------------------------------------
        session_id: str = workspace_root  # default / fallback
        if conn is not None:
            try:
                row = conn.execute(
                    "SELECT session_id FROM sessions WHERE session_key = ?",
                    (workspace_root,),
                ).fetchone()
                if row is not None:
                    candidate = str(row[0])
                    if _SESSION_ID_RE.match(candidate):
                        session_id = candidate
                    else:
                        _trace_mod.write(conn, "preload_context", "corrupt_session")
            except Exception:
                pass

        # --------------------------------------------------------------
        # Nudge mode: extract a meaningful query from the raw pattern
        # --------------------------------------------------------------
        extracted_query: str | None = None
        if mode == "nudge":
            effective_tool = (
                {"bfs": "Glob", "ugrep": "Grep"}[_bash_sub_tool]
                if tool_name == "Bash" and _bash_sub_tool
                else tool_name
            )
            extracted_query = _extract_query(effective_tool, query_pattern)
            if not extracted_query:
                _trace_mod.write(conn, "preload_context", "nudge_no_query")
                print(json.dumps({}))
                return

        # --------------------------------------------------------------
        # Circuit breaker check — skip API call if recently unavailable
        # --------------------------------------------------------------
        if _circuit_breaker_active(conn):
            _trace_mod.write(conn, "preload_context", "api_unavailable")
            print(json.dumps({}))
            return

        # --------------------------------------------------------------
        # Compute cache key
        # --------------------------------------------------------------
        if mode == "inject":
            cache_key = _cache_key(file_path, workspace_root)
        else:
            # extracted_query is non-None here — guarded by the early return above.
            # Use an explicit None check for type narrowing instead of assert, so
            # that running with python -O (optimised mode) cannot silently skip the guard.
            if extracted_query is None:
                print(json.dumps({}))
                return
            cache_key = _cache_key_nudge(extracted_query, workspace_root)

        # Opportunistic expiry — runs ~2% of the time to keep the DB lean
        if conn is not None:
            _cache_expire_opportunistic(conn)

        # --------------------------------------------------------------
        # Cache lookup
        # --------------------------------------------------------------
        injection_text: str | None = None
        if conn is not None:
            cached = _cache_get(conn, cache_key)
            if cached is not None:
                _trace_mod.write(conn, "preload_context", "cache_hit")
                injection_text = cached
            else:
                _trace_mod.write(conn, "preload_context", "cache_miss")

        # --------------------------------------------------------------
        # API call (on cache miss)
        # --------------------------------------------------------------
        if injection_text is None:
            try:
                result = _fetch_context(
                    cfg.api_base,
                    cfg.api_key,
                    file_path,
                    fmt=mode,
                    query=extracted_query if mode == "nudge" else None,
                )
            except (TimeoutError, urllib.error.URLError):
                # Network-level failure — trip the circuit breaker
                _circuit_breaker_trip(conn)
                _trace_mod.write(conn, "preload_context", "api_timeout")
                print(json.dumps({}))
                return
            except Exception as exc:
                # Any other unexpected failure — degrade silently
                _trace_mod.write(
                    conn,
                    "preload_context",
                    f"api_error_unexpected:{type(exc).__name__}",
                )
                print(json.dumps({}))
                return

            if isinstance(result, tuple) and len(result) == 3 and result[0] == "__http_error__":
                _trace_mod.write(conn, "preload_context", f"api_error_http:{result[1]}")
                print(json.dumps({}))
                return

            if result is None:
                _trace_mod.write(conn, "preload_context", "api_error_http")
                print(json.dumps({}))
                return

            # Successful API response — reset circuit breaker and populate cache
            _circuit_breaker_reset(conn)
            _trace_mod.write(conn, "preload_context", "api_ok")
            text, _ttl = result
            injection_text = text

            # Do not cache empty results — a cache hit on an empty response
            # would hide memories stored mid-session until the TTL expires
            # (bug fix, D83).
            if conn is not None and injection_text:
                _cache_set(conn, cache_key, injection_text)

        # --------------------------------------------------------------
        # Empty context — nothing to inject
        # --------------------------------------------------------------
        if not injection_text:
            if mode == "nudge":
                _trace_mod.write(conn, "preload_context", "nudge_empty")
            else:
                _trace_mod.write(conn, "preload_context", "empty")
            print(json.dumps({}))
            return

        # --------------------------------------------------------------
        # Token budget check — BEFORE constructing output
        # --------------------------------------------------------------
        if conn is not None and not _budget_check_and_update(conn, session_id, injection_text):
            _trace_mod.write(conn, "preload_context", "budget_exhausted")
            print(json.dumps({}))
            return

        # --------------------------------------------------------------
        # Inject context
        # "additionalContext" is the Claude hook protocol mechanism for
        # inserting text into the conversation window before the tool
        # runs.  Omit "permissionDecisionReason" entirely — that field
        # is for blocking hooks, not passive injection hooks.
        # --------------------------------------------------------------
        if mode == "nudge":
            _trace_mod.write(conn, "preload_context", "nudge_injected")
        else:
            _trace_mod.write(conn, "preload_context", "injected")
        print(json.dumps({"additionalContext": injection_text}))

    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Last-resort catch: never let an exception surface to the hook runtime.
        # Print empty output so Claude proceeds without context rather than
        # failing.
        print(json.dumps({}))
        sys.exit(0)
