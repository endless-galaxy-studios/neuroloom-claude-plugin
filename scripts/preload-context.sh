#!/usr/bin/env bash
# preload-context.sh — PreToolUse hook: inject or nudge Neuroloom context
#
# Fires before Read, Glob, and Grep tool calls.
#
#   Read  → inject mode: fetch file-scoped memories and inject as additionalContext
#   Glob  → nudge mode: extract query from glob pattern, return a search nudge
#   Grep  → nudge mode: extract query from grep pattern, return a search nudge
#
# Degrades silently on all failures:
#   - No API key → exit 0 (no output)
#   - Unrecognised tool → exit 0 (not our job)
#   - API timeout / error → exit 0 (Python helper writes {} to stdout)
#   - Circuit breaker active → exit 0 (Python helper writes {} to stdout)
#   - Token budget exhausted → exit 0 (Python helper writes {} to stdout)

set -euo pipefail
trap 'exit 0' ERR

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# shellcheck source=../lib/config.sh disable=SC1091
. "${SCRIPT_DIR}/../lib/config.sh"
# shellcheck source=../lib/trace.sh
. "${SCRIPT_DIR}/../lib/trace.sh"

# ---------------------------------------------------------------------------
# Guard: API key must be configured
# ---------------------------------------------------------------------------
if [ -z "$api_key" ]; then
  exit 0
fi

# ---------------------------------------------------------------------------
# Read stdin — PreToolUse hooks receive a JSON object with tool_name + tool_input
# ---------------------------------------------------------------------------
if ! stdin_json="$(cat)"; then
  exit 0
fi

# ---------------------------------------------------------------------------
# Guard: jq must be available to parse JSON
# ---------------------------------------------------------------------------
if ! command -v jq &>/dev/null; then
  exit 0
fi

# ---------------------------------------------------------------------------
# Extract tool_name and select mode
# ---------------------------------------------------------------------------
tool_name="$(echo "$stdin_json" | jq -r '.tool_name // empty' 2>/dev/null || true)"

if [ "$tool_name" = "Read" ]; then
  mode="inject"
elif [ "$tool_name" = "Glob" ] || [ "$tool_name" = "Grep" ]; then
  mode="nudge"
else
  exit 0
fi

export NEUROLOOM_HOOK_MODE="$mode"
export NEUROLOOM_HOOK_TOOL="$tool_name"

# ---------------------------------------------------------------------------
# Mode-specific input extraction
# ---------------------------------------------------------------------------
if [ "$mode" = "inject" ]; then
  file_path="$(echo "$stdin_json" | jq -r '.tool_input.file_path // empty' 2>/dev/null || true)"
  export NEUROLOOM_FILE_PATH="$file_path"
elif [ "$mode" = "nudge" ]; then
  NEUROLOOM_QUERY_PATTERN="$(echo "$stdin_json" | jq -r '.tool_input.pattern // empty' 2>/dev/null || true)"
  export NEUROLOOM_QUERY_PATTERN
fi

# ---------------------------------------------------------------------------
# Mode-aware early-exit guards
# ---------------------------------------------------------------------------
if [ "$mode" = "inject" ] && [ -z "$NEUROLOOM_FILE_PATH" ]; then exit 0; fi
if [ "$mode" = "nudge" ] && [ -z "$NEUROLOOM_QUERY_PATTERN" ]; then exit 0; fi

nl_trace_write "preload-context" "delegating" "null" "null" "null" "$mode"

# ---------------------------------------------------------------------------
# Delegate to Python helper
# Environment variables used instead of CLI args to keep secrets off ps output
# ---------------------------------------------------------------------------
export NEUROLOOM_API_KEY="$api_key"
export NEUROLOOM_API_BASE="${NEUROLOOM_API_BASE:-https://api.neuroloom.dev}"
export NEUROLOOM_WORKSPACE_ROOT="$PWD"

python3 "$SCRIPT_DIR/preload_context.py" --state-dir "$STATE_DIR" --mode "$mode"
