#!/usr/bin/env bats
# trace.bats — Unit tests for lib/trace.sh
#
# Strategy: source trace.sh inside bash -c subshells so each test gets a clean
# environment. HOME is overridden to a tmpdir so trace files never touch the
# developer's actual ~/.neuroloom/traces/.
#
# All assertions on file contents use jq so the tests are format-stable.

LIB_DIR="$(cd "$(dirname "$BATS_TEST_FILENAME")/../lib" && pwd)"

# ---------------------------------------------------------------------------
# Setup / Teardown
# ---------------------------------------------------------------------------

setup() {
  export BATS_TEST_TMPDIR
  BATS_TEST_TMPDIR="$(mktemp -d)"
  export HOME="$BATS_TEST_TMPDIR/home"
  mkdir -p "$HOME"
}

teardown() {
  rm -rf "$BATS_TEST_TMPDIR"
}

# ---------------------------------------------------------------------------
# Guard tests
# ---------------------------------------------------------------------------

@test "guard: NEUROLOOM_TRACE unset -> nl_trace_write is a no-op, writes no trace files" {
  run bash -c "
    export HOME='$HOME'
    unset NEUROLOOM_TRACE
    . '$LIB_DIR/trace.sh'
    # nl_trace_write must be callable without error (no-op stub)
    nl_trace_write 'test' 'decision' 'null' 'null' 'null' 'null'
    # No trace files must have been created
    found=\"\$(ls \"\$HOME/.neuroloom/traces/\"*.jsonl 2>/dev/null | wc -l | tr -d ' ')\"
    [ \"\$found\" -eq 0 ]
  "
  [ "$status" -eq 0 ]
}

@test "guard: NEUROLOOM_TRACE=false -> nl_trace_write is a no-op, writes no trace files" {
  run bash -c "
    export HOME='$HOME'
    export NEUROLOOM_TRACE=false
    . '$LIB_DIR/trace.sh'
    nl_trace_write 'test' 'decision' 'null' 'null' 'null' 'null'
    found=\"\$(ls \"\$HOME/.neuroloom/traces/\"*.jsonl 2>/dev/null | wc -l | tr -d ' ')\"
    [ \"\$found\" -eq 0 ]
  "
  [ "$status" -eq 0 ]
}

@test "guard: NEUROLOOM_TRACE=1 (not 'true') -> nl_trace_write is a no-op, writes no trace files" {
  run bash -c "
    export HOME='$HOME'
    export NEUROLOOM_TRACE=1
    . '$LIB_DIR/trace.sh'
    nl_trace_write 'test' 'decision' 'null' 'null' 'null' 'null'
    found=\"\$(ls \"\$HOME/.neuroloom/traces/\"*.jsonl 2>/dev/null | wc -l | tr -d ' ')\"
    [ \"\$found\" -eq 0 ]
  "
  [ "$status" -eq 0 ]
}

# ---------------------------------------------------------------------------
# Trace directory creation
# ---------------------------------------------------------------------------

@test "trace directory created when NEUROLOOM_TRACE=true" {
  run bash -c '
    export HOME="'"$HOME"'"
    export NEUROLOOM_TRACE=true
    . "'"$LIB_DIR"'/trace.sh"
    [ -d "$HOME/.neuroloom/traces" ]
  '
  [ "$status" -eq 0 ]
}

@test "trace directory has mode 700" {
  run bash -c '
    export HOME="'"$HOME"'"
    export NEUROLOOM_TRACE=true
    . "'"$LIB_DIR"'/trace.sh"
    perms="$(stat -c "%a" "$HOME/.neuroloom/traces" 2>/dev/null || stat -f "%Lp" "$HOME/.neuroloom/traces")"
    [ "$perms" = "0700" ] || [ "$perms" = "700" ]
  '
  [ "$status" -eq 0 ]
}

# ---------------------------------------------------------------------------
# Write to session file
# ---------------------------------------------------------------------------

@test "write to session file: creates sess-*.jsonl and contains decision" {
  local trace_dir="$HOME/.neuroloom/traces"

  run bash -c '
    export HOME="'"$HOME"'"
    export NEUROLOOM_TRACE=true
    . "'"$LIB_DIR"'/trace.sh"
    nl_trace_write "capture" "matched" "sess-1234567890-abcdef12" "Bash" "null" "null"
  '
  [ "$status" -eq 0 ]

  local trace_file="$trace_dir/sess-1234567890-abcdef12.jsonl"
  [ -f "$trace_file" ]

  jq -e 'select(.decision == "matched")' "$trace_file" >/dev/null
}

@test "write to session file: file contains exactly one JSON object after one write" {
  local trace_dir="$HOME/.neuroloom/traces"

  bash -c '
    export HOME="'"$HOME"'"
    export NEUROLOOM_TRACE=true
    . "'"$LIB_DIR"'/trace.sh"
    nl_trace_write "capture" "matched" "sess-1234567890-abcdef12" "Write" "null" "null"
  '

  local trace_file="$trace_dir/sess-1234567890-abcdef12.jsonl"
  [ -f "$trace_file" ]

  # jq -s reads all objects in the file; length gives the object count
  # (pretty-printed JSONL has multiple lines per object — count objects not lines)
  local obj_count
  obj_count="$(jq -s 'length' "$trace_file")"
  [ "$obj_count" -eq 1 ]
}

# ---------------------------------------------------------------------------
# Write to pre-session file
# ---------------------------------------------------------------------------

@test "write to pre-session file when session_id=null" {
  local trace_dir="$HOME/.neuroloom/traces"

  run bash -c '
    export HOME="'"$HOME"'"
    export NEUROLOOM_TRACE=true
    . "'"$LIB_DIR"'/trace.sh"
    nl_trace_write "capture" "no_session_file" "null" "null" "null" "null"
  '
  [ "$status" -eq 0 ]

  # A pre-session-*.jsonl file must exist
  local found
  found="$(ls "$trace_dir"/pre-session-*.jsonl 2>/dev/null | head -1)"
  [ -n "$found" ]

  jq -e 'select(.decision == "no_session_file")' "$found" >/dev/null
}

# ---------------------------------------------------------------------------
# JSON null fields
# ---------------------------------------------------------------------------

@test "null fields are JSON null, not string 'null'" {
  local trace_dir="$HOME/.neuroloom/traces"
  local trace_file="$trace_dir/pre-session-check.jsonl"

  bash -c '
    export HOME="'"$HOME"'"
    export NEUROLOOM_TRACE=true
    . "'"$LIB_DIR"'/trace.sh"
    nl_trace_write "capture" "no_session_file" "null" "null" "null" "null"
  '

  local found
  found="$(ls "$trace_dir"/pre-session-*.jsonl 2>/dev/null | head -1)"
  [ -n "$found" ]

  # session_id must be JSON null, not the string "null"
  jq -e '.session_id == null' "$found" >/dev/null

  # tool_name must be JSON null
  jq -e '.tool_name == null' "$found" >/dev/null

  # elapsed_ms must be JSON null
  jq -e '.elapsed_ms == null' "$found" >/dev/null

  # detail must be JSON null
  jq -e '.detail == null' "$found" >/dev/null
}

@test "session_id field is populated, not null, when session_id provided" {
  local trace_dir="$HOME/.neuroloom/traces"

  bash -c '
    export HOME="'"$HOME"'"
    export NEUROLOOM_TRACE=true
    . "'"$LIB_DIR"'/trace.sh"
    nl_trace_write "capture" "matched" "sess-1234567890-abcdef12" "null" "null" "null"
  '

  local trace_file="$trace_dir/sess-1234567890-abcdef12.jsonl"
  [ -f "$trace_file" ]

  jq -e '.session_id == "sess-1234567890-abcdef12"' "$trace_file" >/dev/null
}

# ---------------------------------------------------------------------------
# elapsed_ms type
# ---------------------------------------------------------------------------

@test "elapsed_ms is integer when provided as numeric string" {
  local trace_dir="$HOME/.neuroloom/traces"

  bash -c '
    export HOME="'"$HOME"'"
    export NEUROLOOM_TRACE=true
    . "'"$LIB_DIR"'/trace.sh"
    nl_trace_write "capture" "rate_throttled" "sess-1234567890-abcdef12" "Write" "45" "45ms elapsed"
  '

  local trace_file="$trace_dir/sess-1234567890-abcdef12.jsonl"
  [ -f "$trace_file" ]

  # Must be number 45, not string "45"
  jq -e '.elapsed_ms == 45' "$trace_file" >/dev/null
  jq -e '.elapsed_ms | type == "number"' "$trace_file" >/dev/null
}

# ---------------------------------------------------------------------------
# Required fields present
# ---------------------------------------------------------------------------

@test "trace entry contains all required top-level fields" {
  local trace_dir="$HOME/.neuroloom/traces"

  bash -c '
    export HOME="'"$HOME"'"
    export NEUROLOOM_TRACE=true
    . "'"$LIB_DIR"'/trace.sh"
    nl_trace_write "inject-context" "completed" "sess-1234567890-abcdef12" "null" "null" "null"
  '

  local trace_file="$trace_dir/sess-1234567890-abcdef12.jsonl"
  [ -f "$trace_file" ]

  jq -e '.ts'       "$trace_file" >/dev/null
  jq -e '.script'   "$trace_file" >/dev/null
  jq -e '.decision' "$trace_file" >/dev/null
  # session_id, tool_name, elapsed_ms, detail all present (even if null)
  jq -e 'has("session_id")'  "$trace_file" >/dev/null
  jq -e 'has("tool_name")'   "$trace_file" >/dev/null
  jq -e 'has("elapsed_ms")'  "$trace_file" >/dev/null
  jq -e 'has("detail")'      "$trace_file" >/dev/null
}

@test "script field matches argument passed to nl_trace_write" {
  local trace_dir="$HOME/.neuroloom/traces"

  bash -c '
    export HOME="'"$HOME"'"
    export NEUROLOOM_TRACE=true
    . "'"$LIB_DIR"'/trace.sh"
    nl_trace_write "inject-context" "completed" "sess-1234567890-abcdef12" "null" "null" "null"
  '

  local trace_file="$trace_dir/sess-1234567890-abcdef12.jsonl"
  jq -e '.script == "inject-context"' "$trace_file" >/dev/null
}

# ---------------------------------------------------------------------------
# Auto-cleanup
# ---------------------------------------------------------------------------

@test "auto-cleanup removes files older than 14 days" {
  local trace_dir="$HOME/.neuroloom/traces"
  mkdir -p "$trace_dir"

  local old_file="$trace_dir/pre-session-old.jsonl"
  echo '{"ts":"old","script":"test","decision":"old"}' > "$old_file"

  # Set mtime to 15 days ago — macOS and Linux portable
  touch -t "$(date -v-15d +%Y%m%d%H%M 2>/dev/null || date -d '15 days ago' +%Y%m%d%H%M)" "$old_file"

  run bash -c '
    export HOME="'"$HOME"'"
    export NEUROLOOM_TRACE=true
    . "'"$LIB_DIR"'/trace.sh"
  '
  [ "$status" -eq 0 ]

  # The 15-day-old file must have been removed
  [ ! -f "$old_file" ]
}

@test "auto-cleanup preserves recent files" {
  local trace_dir="$HOME/.neuroloom/traces"
  mkdir -p "$trace_dir"

  local recent_file="$trace_dir/sess-1234567890-recent.jsonl"
  echo '{"ts":"recent","script":"test","decision":"kept"}' > "$recent_file"
  # mtime is "now" (default for newly written file) — well within 14 days

  run bash -c '
    export HOME="'"$HOME"'"
    export NEUROLOOM_TRACE=true
    . "'"$LIB_DIR"'/trace.sh"
  '
  [ "$status" -eq 0 ]

  # The recent file must still be present
  [ -f "$recent_file" ]
}

# ---------------------------------------------------------------------------
# Concurrent write safety
# ---------------------------------------------------------------------------

@test "concurrent writes: 10 entries written in tight loop are all valid JSON" {
  local trace_dir="$HOME/.neuroloom/traces"
  local trace_file="$trace_dir/sess-9999999999-deadbeef.jsonl"
  local out_file="$BATS_TEST_TMPDIR/concurrent_out.txt"

  bash -c '
    export HOME="'"$HOME"'"
    export NEUROLOOM_TRACE=true
    . "'"$LIB_DIR"'/trace.sh"
    for i in $(seq 1 10); do
      nl_trace_write "capture" "concurrent_test" "sess-9999999999-deadbeef" "Write" "$i" "entry $i"
    done
  '

  [ -f "$trace_file" ]

  # Count JSON objects — file is pretty-printed so we use jq -s rather than wc -l
  local obj_count
  obj_count="$(jq -s 'length' "$trace_file")"
  [ "$obj_count" -eq 10 ]

  # Every object must have a valid decision field
  local valid_count
  valid_count="$(jq -s '[.[] | select(.decision)] | length' "$trace_file")"
  [ "$valid_count" -eq 10 ]
}

# ---------------------------------------------------------------------------
# detail field populated when provided
# ---------------------------------------------------------------------------

@test "detail field is populated when a non-null value is passed" {
  local trace_dir="$HOME/.neuroloom/traces"

  bash -c '
    export HOME="'"$HOME"'"
    export NEUROLOOM_TRACE=true
    . "'"$LIB_DIR"'/trace.sh"
    nl_trace_write "inject-context" "session_start_failed" "sess-1234567890-abcdef12" "null" "null" "HTTP 503"
  '

  local trace_file="$trace_dir/sess-1234567890-abcdef12.jsonl"
  [ -f "$trace_file" ]

  jq -e '.detail == "HTTP 503"' "$trace_file" >/dev/null
}
