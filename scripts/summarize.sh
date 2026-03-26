#!/usr/bin/env bash
# summarize.sh — Stop hook
#
# Responsibilities:
#   1. Read session_id from .neuroloom/session.json in the project root, validate format
#   2. Schedule cleanup of session.json via EXIT trap (runs unconditionally)
#   3. POST /api/v1/sessions/{session_id}/end (fire and forget)
#   4. Exit 0 — cleanup runs via EXIT trap regardless of curl result

set -euo pipefail
trap 'exit 0' ERR

# ---------------------------------------------------------------------------
# Debug logging helper — never logs the API key value
# ---------------------------------------------------------------------------
debug() {
  if [ "${NEUROLOOM_DEBUG:-0}" = "1" ]; then
    echo "[neuroloom:summarize] $*" >&2
  fi
}

# ---------------------------------------------------------------------------
# Read API key and STATE_DIR from .neuroloom/config.json
# Must happen before SESSION_FILE is set so STATE_DIR is available
# shellcheck source=../lib/config.sh
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "${SCRIPT_DIR}/../lib/config.sh"

# ---------------------------------------------------------------------------
# Read and validate session_id — exit 0 silently if session.json missing
# ---------------------------------------------------------------------------
SESSION_FILE="$STATE_DIR/session.json"
[ -f "$SESSION_FILE" ] || exit 0

if command -v jq &>/dev/null; then
  session_id="$(jq -r '.session_id // empty' "$SESSION_FILE" 2>/dev/null || true)"
else
  session_id="$(grep -o '"session_id" *: *"[^"]*"' "$SESSION_FILE" | cut -d'"' -f4)"
fi

# Validate format: sess-<epoch>-<hex>
[[ "$session_id" =~ ^sess-[0-9]+-[a-f0-9]+$ ]] || exit 0
debug "Ending session: $session_id"

# ---------------------------------------------------------------------------
# Schedule unconditional cleanup of session.json via EXIT trap
# Runs regardless of whether the curl call succeeds or fails
# ---------------------------------------------------------------------------
cleanup() {
  rm -f "$SESSION_FILE"
  debug "session.json removed"
}
trap cleanup EXIT

[ -n "$api_key" ] || { debug "No API key found — skipping end call"; exit 0; }

_masked_key="nl_****${api_key: -4}"
debug "API key: $_masked_key"

API_BASE="${NEUROLOOM_API_BASE:-https://api.neuroloom.dev}"

# ---------------------------------------------------------------------------
# POST /api/v1/sessions/{session_id}/end (fire and forget)
# No body required. We do not care about the response.
# Token scheme — not Bearer. Neuroloom API uses "Token <key>" format.
# ---------------------------------------------------------------------------
curl -s --max-time 5 \
  -X POST \
  -H "Authorization: Token $api_key" \
  "${API_BASE}/api/v1/sessions/${session_id}/end" \
  >/dev/null 2>&1 || true

debug "summarize.sh complete — cleanup will run on EXIT"
exit 0
