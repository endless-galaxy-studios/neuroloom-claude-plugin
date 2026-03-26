#!/usr/bin/env bash
# capture.sh — PostToolUse hook
#
# Critical constraint: must exit in under 100ms.
# All heavy work is backgrounded; the main process exits immediately.
#
# Responsibilities:
#   1. Read session_id from .neuroloom/session.json in the project root, validate format
#   2. Read tool event JSON from stdin
#   3. Filter out Neuroloom MCP tools (prevents feedback loop)
#   4. Rate-throttle at 100ms
#   5. Build observation payload
#   6. Background: try API submit, fall back to events.jsonl buffer
#   7. Update last_submit timestamp
#   8. Exit 0 immediately

set -euo pipefail
trap 'exit 0' ERR

# jq is required for payload construction — skip entirely if absent
command -v jq &>/dev/null || exit 0  # pre-bootstrap: jq required for trace.sh; tracing unavailable if jq absent

# ---------------------------------------------------------------------------
# Debug logging helper — never logs the API key value
# ---------------------------------------------------------------------------
debug() {
  if [ "${NEUROLOOM_DEBUG:-0}" = "1" ]; then
    echo "[neuroloom:capture] $*" >&2
  fi
}

# ---------------------------------------------------------------------------
# Read API key and STATE_DIR from .neuroloom/config.json
# Must happen before SESSION_FILE is set so STATE_DIR is available
# shellcheck source=../lib/config.sh
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "${SCRIPT_DIR}/../lib/config.sh"
# shellcheck source=../lib/trace.sh
. "${SCRIPT_DIR}/../lib/trace.sh"

# ---------------------------------------------------------------------------
# Read and validate session_id
# ---------------------------------------------------------------------------
SESSION_FILE="$STATE_DIR/session.json"
if [ ! -f "$SESSION_FILE" ]; then
  nl_trace_write "capture" "no_session_file" "null" "null" "null" "null"
  exit 0
fi

session_id="$(jq -r '.session_id // empty' "$SESSION_FILE" 2>/dev/null || true)"

# Validate format: sess-<epoch>-<hex>
if [[ ! "$session_id" =~ ^sess-[0-9]+-[a-f0-9]+$ ]]; then
  nl_trace_write "capture" "invalid_session_id" "null" "null" "null" "null"
  exit 0
fi
debug "session_id: $session_id"

# ---------------------------------------------------------------------------
# Read tool event JSON from stdin
# ---------------------------------------------------------------------------
stdin_data="$(cat)"
if [ -z "$stdin_data" ]; then
  nl_trace_write "capture" "empty_stdin" "$session_id" "null" "null" "null"
  exit 0
fi

# ---------------------------------------------------------------------------
# Extract tool name (supports both field names Claude Code may send)
# ---------------------------------------------------------------------------
tool_name="$(echo "$stdin_data" | jq -r '.tool_name // .name // "unknown"' 2>/dev/null || echo "unknown")"
debug "tool_name: $tool_name"

# ---------------------------------------------------------------------------
# MCP tool filter: skip Neuroloom's own tools to prevent feedback loop
# ---------------------------------------------------------------------------
if [[ "$tool_name" =~ ^mcp__neuroloom__ ]]; then
  debug "Skipping Neuroloom MCP tool: $tool_name"
  nl_trace_write "capture" "mcp_filtered" "$session_id" "$tool_name" "null" "null"
  exit 0
fi

# ---------------------------------------------------------------------------
# Rate throttle: skip if < 100ms since last submit
# ---------------------------------------------------------------------------
LAST_SUBMIT_FILE="$STATE_DIR/last_submit"

# Platform-portable millisecond timestamp
_get_epoch_ms() {
  if date +%s%3N 2>/dev/null | grep -qE '^[0-9]+$'; then
    # Linux — date supports %3N nanosecond truncation
    date +%s%3N
  else
    # macOS — date does not support %3N; use python3 or fall back to seconds*1000
    python3 -c 'import time; print(int(time.time()*1000))' 2>/dev/null \
      || echo $(( $(date +%s) * 1000 ))
  fi
}

now_ms="$(_get_epoch_ms)"

if [ -f "$LAST_SUBMIT_FILE" ]; then
  last_ms="$(cat "$LAST_SUBMIT_FILE" 2>/dev/null || echo 0)"
  elapsed=$(( now_ms - last_ms ))
  if [ "$elapsed" -lt 100 ]; then
    debug "Rate throttle: ${elapsed}ms elapsed (< 100ms) — skipping"
    nl_trace_write "capture" "rate_throttled" "$session_id" "$tool_name" "$elapsed" "$(printf '%sms elapsed (< 100ms threshold)' "$elapsed")"
    exit 0
  fi
fi

if [ -z "$api_key" ]; then
  nl_trace_write "capture" "no_api_key" "$session_id" "$tool_name" "null" "null"
  exit 0
fi

_masked_key="nl_****${api_key: -4}"
debug "API key: $_masked_key"

# ---------------------------------------------------------------------------
# Build observation payload
# Named function — keeps the logic unit-testable and readable
# ---------------------------------------------------------------------------
EVENTS_FILE="$STATE_DIR/events.jsonl"
API_BASE="${NEUROLOOM_API_BASE:-https://api.neuroloom.dev}"

build_payload() {
  local sid="$1"
  local tname="$2"
  local raw_stdin="$3"

  # Portable UTC timestamp (BSD-compatible — no %:z, no nanoseconds in format)
  local observed_at
  observed_at="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

  # Build observation_id: hash of session+tool+timestamp, prefixed with obs-
  # Detect macOS: date +%N returns literal "%N" on BSD date
  local ns_suffix
  if date +%N 2>/dev/null | grep -qE '^[0-9]+$'; then
    # Linux — nanoseconds available
    ns_suffix="$(date +%N)"
  else
    # macOS — no nanoseconds; use $RANDOM for entropy
    ns_suffix="${RANDOM}${RANDOM}"
  fi

  local hash_input="${sid}_${tname}_${observed_at}_${ns_suffix}"
  # openssl dgst is portable (present on macOS + Linux); sha256sum is absent on macOS
  local obs_hash
  obs_hash="$(echo -n "$hash_input" \
    | openssl dgst -sha256 \
    | awk '{print $NF}' \
    | head -c 16)"
  local observation_id="obs-${obs_hash}"

  # Serialize the full stdin JSON as a string value for the content field
  local content_str
  content_str="$(echo "$raw_stdin" | jq -c '.' 2>/dev/null || echo "$raw_stdin")"

  # Construct the single observation object
  local observation
  observation="$(jq -n \
    --arg oid "$observation_id" \
    --arg sid "$sid" \
    --arg oat "$observed_at" \
    --arg tname "$tname" \
    --arg content "$content_str" \
    '{
      observation_id: $oid,
      session_id: $sid,
      observed_at: $oat,
      category: $tname,
      content: $content
    }')"

  # Wrap in the batch envelope
  jq -n --argjson obs "$observation" '{observations: [$obs]}'
}

batch_payload="$(build_payload "$session_id" "$tool_name" "$stdin_data")"
debug "Payload built for observation"
nl_trace_write "capture" "matched" "$session_id" "$tool_name" "null" "null"

# ---------------------------------------------------------------------------
# Background: attempt API submit, buffer to events.jsonl on failure
# The main process exits 0 immediately after this block — the subshell runs async
# ---------------------------------------------------------------------------

# Extract the single observation from the batch payload for buffering
single_observation="$(echo "$batch_payload" | jq -c '.observations[0]' 2>/dev/null || true)"

(
  # Disable ERR trap inside subshell — we need curl failure to fall through
  # to the buffer-write branch, not exit the subshell via the inherited trap.
  trap - ERR
  set +e

  # Token scheme — not Bearer. Neuroloom API uses "Token <key>" format.
  curl -s --fail-with-body --max-time 1 \
    -X POST \
    -H "Authorization: Token $api_key" \
    -H "Content-Type: application/json" \
    -d "$batch_payload" \
    "${API_BASE}/api/v1/observations/batch" >/dev/null 2>&1
  curl_exit=$?

  if [ $curl_exit -eq 0 ]; then
    nl_trace_write "capture" "api_completed" "$session_id" "$tool_name" "null" "null"
  else
    nl_trace_write "capture" "api_errored" "$session_id" "$tool_name" "null" "curl exit $curl_exit"
    # API unavailable — buffer the observation to events.jsonl
    if [ -n "$single_observation" ]; then
      # Enforce size bound BEFORE appending (prevents unbounded growth)
      if [ -f "$EVENTS_FILE" ]; then
        line_count="$(wc -l < "$EVENTS_FILE" | tr -d ' ')"
        if [ "$line_count" -gt 10000 ]; then
          tmp_file="$(umask 077; mktemp)"
          tail -n 8000 "$EVENTS_FILE" > "$tmp_file"
          mv "$tmp_file" "$EVENTS_FILE"
        fi
      fi
      (umask 077; echo "$single_observation" >> "$EVENTS_FILE")
    fi
  fi
) &

# ---------------------------------------------------------------------------
# Update last_submit timestamp (milliseconds) and exit immediately
# Deliberate: written in main process, not subshell, so the throttle applies
# even when the background curl fails. This trades completeness for latency —
# a fast-failing API won't cause a burst of buffered retries.
# ---------------------------------------------------------------------------
(umask 077; echo "$now_ms" > "$LAST_SUBMIT_FILE")

debug "capture.sh dispatched background worker — exiting"
exit 0
