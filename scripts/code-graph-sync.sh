#!/usr/bin/env bash
# code-graph-sync.sh — PostToolUse hook (Write|Edit only)
#
# Critical constraint: must exit in under 100ms.
# All heavy work is backgrounded; the main process exits immediately.
#
# Responsibilities:
#   1. Guard on jq, python3, API key, and NEUROLOOM_CODE_GRAPH_SYNC opt-out
#   2. Read tool event JSON from stdin
#   3. Extract and validate the changed file path
#   4. Filter to .ts, .tsx, .py extensions only
#   5. Reject files outside the current workspace
#   6. Debounce rapid writes using a per-workspace lock file with mtime-expiry
#   7. Batch accumulated file paths into a single sync_file.py invocation
#   8. Adapt the debounce window based on 429 rate limit responses
#   9. Exit 0 immediately

set -euo pipefail
trap 'exit 0' ERR

# jq is required for trace.sh — skip entirely if absent
command -v jq &>/dev/null || exit 0  # pre-bootstrap: jq required for trace.sh; tracing unavailable if jq absent

# ---------------------------------------------------------------------------
# Debug logging helper — never logs the API key value
# ---------------------------------------------------------------------------
debug() {
  if [ "${NEUROLOOM_DEBUG:-0}" = "1" ]; then
    echo "[neuroloom:code-graph-sync] $*" >&2
  fi
}

# ---------------------------------------------------------------------------
# Source config (provides: api_key, STATE_DIR) and trace helpers
# shellcheck source=../lib/config.sh
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "${SCRIPT_DIR}/../lib/config.sh"
# shellcheck source=../lib/trace.sh
. "${SCRIPT_DIR}/../lib/trace.sh"

# ---------------------------------------------------------------------------
# Opt-out check — NEUROLOOM_CODE_GRAPH_SYNC=false or =0 disables this hook
# ---------------------------------------------------------------------------
if [[ "${NEUROLOOM_CODE_GRAPH_SYNC:-}" == "false" || "${NEUROLOOM_CODE_GRAPH_SYNC:-}" == "0" ]]; then
  nl_trace_write "code-graph-sync" "disabled_by_config" "null" "null" "0" "NEUROLOOM_CODE_GRAPH_SYNC=${NEUROLOOM_CODE_GRAPH_SYNC}"
  exit 0
fi

# ---------------------------------------------------------------------------
# python3 guard — sync_file.py requires python3
# ---------------------------------------------------------------------------
command -v python3 &>/dev/null || {
  nl_trace_write "code-graph-sync" "no_python3" "null" "null" "0" "python3 not found in PATH"
  exit 0
}

# ---------------------------------------------------------------------------
# API key check — api_key comes from lib/config.sh
# ---------------------------------------------------------------------------
if [ -z "$api_key" ]; then
  nl_trace_write "code-graph-sync" "no_api_key" "null" "null" "0" "CLAUDE_PLUGIN_OPTION_API_KEY not set"
  exit 0
fi

# ---------------------------------------------------------------------------
# API base resolution — inline, not from lib/config.sh
# ---------------------------------------------------------------------------
API_BASE="${NEUROLOOM_API_BASE:-https://api.neuroloom.dev}"

# ---------------------------------------------------------------------------
# Debounce state directory
# ---------------------------------------------------------------------------
DEBOUNCE_DIR="$STATE_DIR/code_graph_debounce"
(umask 077; mkdir -p "$DEBOUNCE_DIR")

# ---------------------------------------------------------------------------
# Periodic cleanup — remove stale lock and pending files (older than 1 minute)
# ---------------------------------------------------------------------------
find "$DEBOUNCE_DIR" -mmin +1 -name "*.lock" -delete 2>/dev/null || true
find "$DEBOUNCE_DIR" -mmin +1 -name "*.pending" -delete 2>/dev/null || true

# ---------------------------------------------------------------------------
# Platform-portable millisecond timestamp (from capture.sh)
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Get file modification time in milliseconds (macOS + Linux portable)
# ---------------------------------------------------------------------------
_get_file_mtime_ms() {
  local file="$1"
  if stat -f %m "$file" 2>/dev/null | grep -qE '^[0-9]+$'; then
    # macOS (BSD stat) — %m returns mtime in seconds
    echo $(( $(stat -f %m "$file") * 1000 ))
  else
    # Linux (GNU stat) — %Y returns mtime in seconds
    echo $(( $(stat -c %Y "$file") * 1000 ))
  fi
}

# ---------------------------------------------------------------------------
# Read tool event JSON from stdin
# ---------------------------------------------------------------------------
stdin_data="$(cat)"
if [ -z "$stdin_data" ]; then
  exit 0
fi

# ---------------------------------------------------------------------------
# Extract file path — field name varies by SDK version
# ---------------------------------------------------------------------------
file_path="$(echo "$stdin_data" | jq -r '.tool_input.path // .tool_input.file_path // .input.path // .input.file_path // empty' 2>/dev/null || true)"
if [ -z "$file_path" ]; then
  exit 0
fi

debug "file_path: $file_path"

# ---------------------------------------------------------------------------
# Extension filter — only .ts, .tsx, .py files carry structural metadata
# ---------------------------------------------------------------------------
ext="${file_path##*.}"
case ".$ext" in
  .ts|.tsx|.py) ;;
  *)
    nl_trace_write "code-graph-sync" "extension_filtered" "null" "null" "0" "ext=.$ext path=$file_path"
    exit 0
    ;;
esac

# ---------------------------------------------------------------------------
# Path normalization — resolve symlinks, compute relative path, reject files
# outside workspace
# ---------------------------------------------------------------------------
# Resolve symlinks before workspace containment check
real_path="$(realpath "$file_path" 2>/dev/null \
  || python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$file_path" 2>/dev/null \
  || true)"
if [[ -z "$real_path" || "$real_path" != "$PWD"/* ]]; then
  nl_trace_write "code-graph-sync" "path_outside_workspace" "null" "null" "0" "path=$file_path resolved=${real_path:-unresolvable} pwd=$PWD"
  exit 0
fi
rel_path="${real_path#"$PWD"/}"

debug "rel_path: $rel_path"

# ---------------------------------------------------------------------------
# Per-workspace debounce lock (hash the workspace root, not individual file paths)
# ---------------------------------------------------------------------------
debounce_key=$(echo -n "$PWD" | openssl dgst -sha256 | awk '{print $NF}' | head -c 16)
debounce_file="$DEBOUNCE_DIR/${debounce_key}.lock"
buffer_file="$DEBOUNCE_DIR/${debounce_key}.pending"

# ---------------------------------------------------------------------------
# Always append the current file to the pending buffer
# ---------------------------------------------------------------------------
(umask 077; echo "$file_path" >> "$buffer_file")

# ---------------------------------------------------------------------------
# Debounce check — read adaptive backoff window and compare lock file mtime
# ---------------------------------------------------------------------------
# Read adaptive backoff window (defaults to 2000ms = 2 seconds)
backoff_ms=$(cat "$DEBOUNCE_DIR/backoff_ms" 2>/dev/null || echo 2000)
backoff_ms="${backoff_ms:-2000}"

if [[ -f "$debounce_file" ]]; then
  lock_mtime_ms=$(_get_file_mtime_ms "$debounce_file")
  now_ms=$(_get_epoch_ms)
  if (( now_ms - lock_mtime_ms < backoff_ms )); then
    nl_trace_write "code-graph-sync" "debounced" "null" "null" "0" "lock age=$((now_ms - lock_mtime_ms))ms window=${backoff_ms}ms"
    exit 0
  fi
fi

# ---------------------------------------------------------------------------
# Create/refresh the lock file — mtime records when this batch window started
# ---------------------------------------------------------------------------
(umask 077; touch "$debounce_file")

# ---------------------------------------------------------------------------
# Export API key as env var — NOT as a CLI arg (security: keeps key off ps output)
# ---------------------------------------------------------------------------
export NEUROLOOM_API_KEY="$api_key"

# ---------------------------------------------------------------------------
# Background subshell — wait the debounce window, then batch and POST all
# accumulated file paths. The main process exits 0 immediately after this block.
# ---------------------------------------------------------------------------
(
  trap - ERR
  set +e

  # Wait the debounce window to accumulate paths
  sleep $(( backoff_ms / 1000 ))

  # Atomically drain the buffer — mv is atomic on the same filesystem
  tmp_buffer="${buffer_file}.$BASHPID"
  mv "$buffer_file" "$tmp_buffer" 2>/dev/null || exit 0
  mapfile -t all_paths < "$tmp_buffer"
  rm -f "$tmp_buffer"

  if [[ ${#all_paths[@]} -eq 0 ]]; then
    exit 0
  fi

  # Deduplicate paths (same file may have been written multiple times)
  declare -A seen
  unique_paths=()
  for p in "${all_paths[@]}"; do
    [[ -z "$p" ]] && continue
    if [[ -z "${seen[$p]+x}" ]]; then
      seen[$p]=1
      unique_paths+=("$p")
    fi
  done

  if [[ ${#unique_paths[@]} -eq 0 ]]; then
    exit 0
  fi

  python3 "$SCRIPT_DIR/sync_file.py" "${unique_paths[@]}" \
    --workspace-root "$PWD" \
    --api-base "$API_BASE"
  sync_exit=$?

  # Adaptive backoff based on sync result
  if [[ $sync_exit -eq 42 ]]; then
    # Rate limited — double the backoff (cap 60000ms = 1 minute)
    new_backoff=$(( backoff_ms * 2 ))
    (( new_backoff > 60000 )) && new_backoff=60000
    (umask 077; echo "$new_backoff" > "$DEBOUNCE_DIR/backoff_ms")
  elif [[ $sync_exit -eq 0 && $backoff_ms -gt 2000 ]]; then
    # Success — halve the backoff (floor 2000ms = 2 seconds)
    new_backoff=$(( backoff_ms / 2 ))
    (( new_backoff < 2000 )) && new_backoff=2000
    (umask 077; echo "$new_backoff" > "$DEBOUNCE_DIR/backoff_ms")
  fi
) &

nl_trace_write "code-graph-sync" "dispatched" "null" "null" "0" "path=$rel_path window=${backoff_ms}ms"

debug "code-graph-sync.sh dispatched background worker — exiting"
exit 0
