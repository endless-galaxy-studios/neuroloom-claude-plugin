#!/usr/bin/env bash
# lib/trace.sh — Optional JSONL session tracing for neuroloom-claude-plugin
#
# Source this file after config.sh. It provides nl_trace_write() for recording
# structured trace entries to ~/.neuroloom/traces/.
#
# Opt-in: NEUROLOOM_TRACE=true must be set in the environment. When the variable
# is absent or set to any other value, this file is a complete no-op — every line
# beyond the guard returns immediately, and nl_trace_write() is never defined.
#
# Usage:
#   nl_trace_write <script> <decision> [session_id] [tool_name] [elapsed_ms] [detail]
#
# Pass literal string "null" for optional args when the value is unavailable.
# All tracing failures are silent — this library must never break the caller.

if [ "${NEUROLOOM_TRACE:-}" != "true" ]; then
  nl_trace_write() { :; }
  return 0
fi

# ---------------------------------------------------------------------------
# Timestamp — captured once at source time; reused for all entries this run
# ---------------------------------------------------------------------------
_nl_trace_ts="$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo "1970-01-01T00:00:00Z")"

# ---------------------------------------------------------------------------
# Trace directory — global, not project-scoped
# ---------------------------------------------------------------------------
_nl_trace_dir="${HOME}/.neuroloom/traces"
(umask 077; mkdir -p "$_nl_trace_dir")

# ---------------------------------------------------------------------------
# Auto-cleanup: remove trace files older than 14 days
# ---------------------------------------------------------------------------
find "$_nl_trace_dir" -name "*.jsonl" -mtime +14 -delete 2>/dev/null || true

# ---------------------------------------------------------------------------
# _nl_trace_append <file> <data>
#
# Appends a single line to a JSONL file. Uses flock(1) for atomic append on
# Linux where the tool is available in the base system. Falls back to a direct
# append on macOS where flock is absent from /usr/bin.
# ---------------------------------------------------------------------------
_nl_trace_append() {
  local file="$1" data="$2"
  if command -v flock &>/dev/null; then
    ( flock -n 9 || true; printf '%s\n' "$data" >> "$file" ) 9>>"$file"
  else
    printf '%s\n' "$data" >> "$file"
  fi
}

# ---------------------------------------------------------------------------
# nl_trace_write <script> <decision> [session_id] [tool_name] [elapsed_ms] [detail]
#
# Writes one JSON object to the appropriate trace JSONL file.
#
# Arguments:
#   script      — name of the calling script (e.g. "inject-context.sh")
#   decision    — short label for the exit path (e.g. "session_started", "no_api_key")
#   session_id  — active session ID, or literal "null" if not yet available
#   tool_name   — tool name from the hook event, or literal "null"
#   elapsed_ms  — integer milliseconds, or literal "null"
#   detail      — freeform string for additional context, or literal "null"
#
# Output fields:
#   ts, script, decision, session_id, tool_name, elapsed_ms, detail
#
# This function never writes to stdout or stderr unless NEUROLOOM_DEBUG=1.
# It must never call exit — failures are always silently swallowed.
# ---------------------------------------------------------------------------
nl_trace_write() {
  local script="$1"
  local decision="$2"
  local session_id="${3:-null}"
  local tool_name="${4:-null}"
  local elapsed_ms="${5:-null}"
  local detail="${6:-null}"

  ( set +e
    local trace_file
    if [ "$session_id" != "null" ]; then
      trace_file="${_nl_trace_dir}/${session_id}.jsonl"
    else
      trace_file="${_nl_trace_dir}/pre-session-$(date -u +%Y-%m-%d 2>/dev/null || echo "unknown").jsonl"
    fi

    local json
    json="$(jq -n \
      --arg ts       "$_nl_trace_ts" \
      --arg script   "$script" \
      --arg decision "$decision" \
      --arg sid      "$session_id" \
      --arg tname    "$tool_name" \
      --arg ems      "$elapsed_ms" \
      --arg det      "$detail" \
      '{
        ts:         $ts,
        script:     $script,
        decision:   $decision,
        session_id: (if $sid   == "null" then null else $sid   end),
        tool_name:  (if $tname == "null" then null else $tname end),
        elapsed_ms: (if $ems   == "null" then null else ($ems | tonumber) end),
        detail:     (if $det   == "null" then null else $det   end)
      }'
    )" || return 0

    _nl_trace_append "$trace_file" "$json"

    if [ "${NEUROLOOM_DEBUG:-0}" = "1" ]; then
      echo "[neuroloom:trace] $script/$decision -> $trace_file" >&2
    fi
  ) 2>/dev/null || true
}
