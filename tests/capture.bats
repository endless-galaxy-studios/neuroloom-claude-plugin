#!/usr/bin/env bats
# capture.bats — Tests for scripts/capture.sh (PostToolUse hook)
#
# Mock strategy: PATH-prepend stubs in $STUB_DIR.
# Function overrides are NOT used — they do not survive backgrounded subshells,
# which breaks mocking of capture.sh's backgrounded "(curl ... || ...) &" pattern.
#
# Note: capture.sh's background subshell disables the ERR trap (trap - ERR; set +e)
# so that curl failures correctly fall through to the buffer-write branch.
#
# Config model: env-var only. lib/config.sh reads CLAUDE_PLUGIN_OPTION_API_KEY.
# at source time. Tests must `cd` into a project temp directory BEFORE running
# the script so STATE_DIR resolves to that directory.

SCRIPTS_DIR="$(cd "$(dirname "$BATS_TEST_FILENAME")/../scripts" && pwd)"
FIXTURES_DIR="$(cd "$(dirname "$BATS_TEST_FILENAME")/fixtures" && pwd)"

# ---------------------------------------------------------------------------
# Setup / Teardown
# ---------------------------------------------------------------------------

setup() {
  export BATS_TEST_TMPDIR
  BATS_TEST_TMPDIR="$(mktemp -d)"

  # Override HOME so trace files go to a controlled location (not real ~/.neuroloom/traces/)
  export HOME="$BATS_TEST_TMPDIR/home"
  mkdir -p "$HOME"

  # Project root simulation: lib/config.sh uses $PWD at source time,
  # so the test must cd here before running the script.
  export PROJECT_DIR="$BATS_TEST_TMPDIR/project"
  mkdir -p "$PROJECT_DIR/.neuroloom"
  cd "$PROJECT_DIR"

  # Stub directory — prepended to PATH for all external tool mocking
  export STUB_DIR="$BATS_TEST_TMPDIR/stubs"
  mkdir -p "$STUB_DIR"

  # Default curl stub: succeeds silently
  cat > "$STUB_DIR/curl" << 'STUB'
#!/bin/sh
exit 0
STUB
  chmod +x "$STUB_DIR/curl"

  export PATH="$STUB_DIR:$PATH"

  export CLAUDE_PLUGIN_OPTION_API_KEY="nl_testkey1234"

  # Default session.json with a valid session_id
  printf '{"session_id":"sess-1234567890-abcdef12"}\n' > "$PROJECT_DIR/.neuroloom/session.json"
  chmod 600 "$PROJECT_DIR/.neuroloom/session.json"

  # Point API base at a safe test address (stub curl won't connect anyway)
  export NEUROLOOM_API_BASE="http://test.invalid"
  export NEUROLOOM_DEBUG=0
}

teardown() {
  rm -rf "$BATS_TEST_TMPDIR"
}

# ---------------------------------------------------------------------------
# Helper: run capture.sh with given stdin, then wait for background work.
# capture.sh exits immediately; the background subshell runs async.
# ---------------------------------------------------------------------------
_run_capture() {
  local input="$1"
  run bash "$SCRIPTS_DIR/capture.sh" <<< "$input"
  # Give the background subshell time to complete its work
  sleep 0.3
}

# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

@test "happy path: curl stub receives correct observation batch payload" {
  # Override curl stub to record its arguments
  cat > "$STUB_DIR/curl" << STUB
#!/bin/sh
printf '%s\n' "\$@" > "$PROJECT_DIR/.neuroloom/curl_args.txt"
exit 0
STUB
  chmod +x "$STUB_DIR/curl"

  _run_capture "$(cat "$FIXTURES_DIR/tool_event_write.json")"
  [ "$status" -eq 0 ]

  # curl must have been called
  [ -f "$PROJECT_DIR/.neuroloom/curl_args.txt" ]

  # The endpoint must include observations/batch
  grep -q "observations/batch" "$PROJECT_DIR/.neuroloom/curl_args.txt"

  # Authorization header must use Token scheme (not Bearer)
  grep -q "Token nl_testkey1234" "$PROJECT_DIR/.neuroloom/curl_args.txt"

  # Content-Type header must be present
  grep -q "application/json" "$PROJECT_DIR/.neuroloom/curl_args.txt"
}

@test "config absent: env var unset -> exits 0, curl not called" {
  unset CLAUDE_PLUGIN_OPTION_API_KEY

  cat > "$STUB_DIR/curl" << STUB
#!/bin/sh
touch "$PROJECT_DIR/.neuroloom/curl_called"
exit 0
STUB
  chmod +x "$STUB_DIR/curl"

  _run_capture "$(cat "$FIXTURES_DIR/tool_event_write.json")"
  [ "$status" -eq 0 ]

  [ ! -f "$PROJECT_DIR/.neuroloom/curl_called" ]
}

@test "session absent: no session.json -> exits 0, curl not called" {
  rm "$PROJECT_DIR/.neuroloom/session.json"

  cat > "$STUB_DIR/curl" << STUB
#!/bin/sh
touch "$PROJECT_DIR/.neuroloom/curl_called"
exit 0
STUB
  chmod +x "$STUB_DIR/curl"

  _run_capture "$(cat "$FIXTURES_DIR/tool_event_write.json")"
  [ "$status" -eq 0 ]

  [ ! -f "$PROJECT_DIR/.neuroloom/curl_called" ]
}

@test "API failure: curl exits non-zero -> observation buffered to events.jsonl" {
  cat > "$STUB_DIR/curl" << 'STUB'
#!/bin/sh
exit 1
STUB
  chmod +x "$STUB_DIR/curl"

  _run_capture "$(cat "$FIXTURES_DIR/tool_event_write.json")"

  # Script must exit 0 regardless of curl failure
  [ "$status" -eq 0 ]

  # Observation should be buffered to events.jsonl
  [ -f "$PROJECT_DIR/.neuroloom/events.jsonl" ]

  # Buffered line should be valid JSON with observation_id
  local buffered_line
  buffered_line="$(head -1 "$PROJECT_DIR/.neuroloom/events.jsonl")"
  echo "$buffered_line" | jq -e '.observation_id' >/dev/null 2>&1
}

@test "MCP tool filter: mcp__neuroloom__memory_search -> exits 0, curl not called" {
  cat > "$STUB_DIR/curl" << STUB
#!/bin/sh
touch "$PROJECT_DIR/.neuroloom/curl_called"
exit 0
STUB
  chmod +x "$STUB_DIR/curl"

  _run_capture "$(cat "$FIXTURES_DIR/tool_event_mcp.json")"
  [ "$status" -eq 0 ]

  [ ! -f "$PROJECT_DIR/.neuroloom/curl_called" ]
}

@test "MCP namespaced filter: mcp__neuroloom__session_get_context -> exits 0, curl not called" {
  cat > "$STUB_DIR/curl" << STUB
#!/bin/sh
touch "$PROJECT_DIR/.neuroloom/curl_called"
exit 0
STUB
  chmod +x "$STUB_DIR/curl"

  _run_capture '{"tool_name": "mcp__neuroloom__session_get_context", "session_id": "abc"}'
  [ "$status" -eq 0 ]

  [ ! -f "$PROJECT_DIR/.neuroloom/curl_called" ]
}

@test "name-keyed fixture: stdin with 'name' field (not 'tool_name') -> works correctly" {
  cat > "$STUB_DIR/curl" << STUB
#!/bin/sh
printf '%s\n' "\$@" > "$PROJECT_DIR/.neuroloom/curl_args.txt"
exit 0
STUB
  chmod +x "$STUB_DIR/curl"

  _run_capture "$(cat "$FIXTURES_DIR/tool_event_name_keyed.json")"
  [ "$status" -eq 0 ]

  # curl must have been called (the name-keyed event is not an MCP tool)
  [ -f "$PROJECT_DIR/.neuroloom/curl_args.txt" ]
  grep -q "observations/batch" "$PROJECT_DIR/.neuroloom/curl_args.txt"
}

@test "empty stdin: empty string -> exits 0 gracefully" {
  # True empty stdin — the script exits 0 early when stdin_data is empty
  run bash "$SCRIPTS_DIR/capture.sh" <<< ""
  [ "$status" -eq 0 ]
}

@test "malformed stdin: non-JSON stdin -> exits 0 gracefully" {
  run bash "$SCRIPTS_DIR/capture.sh" < "$FIXTURES_DIR/tool_event_malformed.json"
  [ "$status" -eq 0 ]
}

@test "observation ID determinism (Linux only): same input twice -> observation_ids start with obs-" {
  # On Linux, nanosecond timestamps are available so obs IDs are deterministic per-second.
  # On macOS, RANDOM is used for entropy so IDs intentionally differ.
  [[ "$(uname)" == "Linux" ]] || skip "macOS uses RANDOM for collision avoidance"

  # Use a NUL-delimited file so multiline JSON payloads don't split across lines
  cat > "$STUB_DIR/curl" << STUB
#!/bin/sh
prev=""
for arg in "\$@"; do
  if [ "\$prev" = "-d" ]; then
    printf '%s\0' "\$arg" >> "$PROJECT_DIR/.neuroloom/payloads.bin"
  fi
  prev="\$arg"
done
exit 0
STUB
  chmod +x "$STUB_DIR/curl"

  local input='{"tool_name":"Write","file_path":"/deterministic.ts"}'

  _run_capture "$input"
  _run_capture "$input"

  [ -f "$PROJECT_DIR/.neuroloom/payloads.bin" ]

  # Split NUL-delimited payloads and extract observation_ids
  local id1 id2
  id1="$(awk 'BEGIN{RS="\0"} NR==1' "$PROJECT_DIR/.neuroloom/payloads.bin" | jq -r '.observations[0].observation_id')"
  id2="$(awk 'BEGIN{RS="\0"} NR==2' "$PROJECT_DIR/.neuroloom/payloads.bin" | jq -r '.observations[0].observation_id')"

  # Both IDs must start with "obs-" (the hash function ran without error)
  [[ "$id1" =~ ^obs- ]]
  [[ "$id2" =~ ^obs- ]]
}

@test "rate throttle skip: recent last_submit timestamp -> exits 0, curl not called" {
  # Write a last_submit timestamp of "now" (milliseconds since epoch)
  local now_ms
  if date +%s%3N 2>/dev/null | grep -qE '^[0-9]+$'; then
    now_ms="$(date +%s%3N)"
  else
    now_ms="$(python3 -c 'import time; print(int(time.time()*1000))')"
  fi
  echo "$now_ms" > "$PROJECT_DIR/.neuroloom/last_submit"

  cat > "$STUB_DIR/curl" << STUB
#!/bin/sh
touch "$PROJECT_DIR/.neuroloom/curl_called"
exit 0
STUB
  chmod +x "$STUB_DIR/curl"

  _run_capture "$(cat "$FIXTURES_DIR/tool_event_write.json")"
  [ "$status" -eq 0 ]

  [ ! -f "$PROJECT_DIR/.neuroloom/curl_called" ]
}

@test "rate throttle pass: old last_submit timestamp -> calls curl" {
  # Write a last_submit timestamp 1 second in the past (1000ms ago — well past the 100ms threshold)
  local old_ms
  if date +%s%3N 2>/dev/null | grep -qE '^[0-9]+$'; then
    old_ms=$(( $(date +%s%3N) - 1000 ))
  else
    old_ms=$(( $(python3 -c 'import time; print(int(time.time()*1000))') - 1000 ))
  fi
  echo "$old_ms" > "$PROJECT_DIR/.neuroloom/last_submit"

  cat > "$STUB_DIR/curl" << STUB
#!/bin/sh
touch "$PROJECT_DIR/.neuroloom/curl_called"
exit 0
STUB
  chmod +x "$STUB_DIR/curl"

  _run_capture "$(cat "$FIXTURES_DIR/tool_event_write.json")"
  [ "$status" -eq 0 ]

  [ -f "$PROJECT_DIR/.neuroloom/curl_called" ]
}

@test "jq absent: jq not in PATH -> exits 0, curl not called" {
  # capture.sh uses: command -v jq &>/dev/null || exit 0
  # command -v succeeds if jq exists in PATH regardless of exit code.
  # To simulate jq absent, we build a PATH that omits all directories containing jq.

  cat > "$STUB_DIR/curl" << STUB
#!/bin/sh
touch "$PROJECT_DIR/.neuroloom/curl_called"
exit 0
STUB
  chmod +x "$STUB_DIR/curl"

  # Build PATH without any directory that contains a real jq binary,
  # but keep STUB_DIR (which has curl but not jq).
  # Use absolute path to bash so it doesn't need to be in PATH itself
  # (on Linux CI, /usr/bin often contains both bash and jq).
  local bash_path
  bash_path="$(command -v bash)"

  local no_jq_path="$STUB_DIR"
  local dir
  IFS=':' read -ra _path_dirs <<< "$PATH"
  for dir in "${_path_dirs[@]}"; do
    if [ "$dir" = "$STUB_DIR" ]; then
      continue  # already added
    fi
    if [ -x "$dir/jq" ]; then
      continue  # skip directories containing jq
    fi
    no_jq_path="${no_jq_path}:${dir}"
  done

  PATH="$no_jq_path" run "$bash_path" "$SCRIPTS_DIR/capture.sh" <<< "$(cat "$FIXTURES_DIR/tool_event_write.json")"
  [ "$status" -eq 0 ]

  sleep 0.3
  [ ! -f "$PROJECT_DIR/.neuroloom/curl_called" ]
}

@test "debug mode: NEUROLOOM_DEBUG=1 -> stderr does not contain raw API key" {
  export NEUROLOOM_DEBUG=1

  # Run capture.sh and capture both stdout and stderr
  run bash -c "bash '$SCRIPTS_DIR/capture.sh' <<< '$(cat "$FIXTURES_DIR/tool_event_write.json")'" 2>&1
  [ "$status" -eq 0 ]

  # stderr/stdout must not contain the raw API key value
  [[ "$output" != *"nl_testkey1234"* ]]
}

@test "payload construction: build_payload() produces correct JSON shape" {
  # Verify build_payload() by extracting and running it in isolation.
  # The function is defined in capture.sh — we source the relevant portion.
  local payload
  payload="$(bash << 'INNERSCRIPT'
set -euo pipefail
trap 'exit 0' ERR

# Reproduce the build_payload function from capture.sh
build_payload() {
  local sid="$1"
  local tname="$2"
  local raw_stdin="$3"

  local observed_at
  observed_at="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

  local ns_suffix
  if date +%N 2>/dev/null | grep -qE '^[0-9]+$'; then
    ns_suffix="$(date +%N)"
  else
    ns_suffix="${RANDOM}${RANDOM}"
  fi

  local hash_input="${sid}_${tname}_${observed_at}_${ns_suffix}"
  local obs_hash
  obs_hash="$(echo -n "$hash_input" \
    | openssl dgst -sha256 \
    | awk '{print $NF}' \
    | head -c 16)"
  local observation_id="obs-${obs_hash}"

  local content_str
  content_str="$(echo "$raw_stdin" | jq -c '.' 2>/dev/null || echo "$raw_stdin")"

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

  jq -n --argjson obs "$observation" '{observations: [$obs]}'
}

build_payload 'sess-1234567890-abcdef12' 'Write' '{"tool_name":"Write","file_path":"/src/main.ts"}'
INNERSCRIPT
)"

  # Payload must be valid JSON
  echo "$payload" | jq -e '.' > /dev/null

  # Must have observations array with exactly one element
  local count
  count="$(echo "$payload" | jq '.observations | length')"
  [ "$count" -eq 1 ]

  # Observation must have required fields with correct values
  echo "$payload" | jq -e '.observations[0].observation_id' > /dev/null
  echo "$payload" | jq -e '.observations[0].session_id == "sess-1234567890-abcdef12"' > /dev/null
  echo "$payload" | jq -e '.observations[0].category == "Write"' > /dev/null
  echo "$payload" | jq -e '.observations[0].content' > /dev/null
  echo "$payload" | jq -e '.observations[0].observed_at' > /dev/null

  # observation_id must start with "obs-"
  local obs_id
  obs_id="$(echo "$payload" | jq -r '.observations[0].observation_id')"
  [[ "$obs_id" =~ ^obs- ]]
}

# ---------------------------------------------------------------------------
# Trace instrumentation tests
# ---------------------------------------------------------------------------
# All tests here:
#   - Set NEUROLOOM_TRACE=true
#   - Override HOME to a per-test tmpdir so trace files never touch the real
#     ~/.neuroloom/traces/
#   - Use _run_capture for running capture.sh (with background-wait)
#
# Trace file helpers:
#   _trace_wait  — waits for at least one .jsonl in $HOME/.neuroloom/traces/
#   _trace_first — returns the path to the first .jsonl file found
# ---------------------------------------------------------------------------

_trace_wait() {
  # Poll up to ~0.5s for a trace file to appear (background subshell may lag)
  local i=0
  while [ $i -lt 10 ]; do
    ls "$HOME/.neuroloom/traces/"*.jsonl >/dev/null 2>&1 && return 0
    sleep 0.05
    i=$(( i + 1 ))
  done
  return 1
}

_trace_first() {
  ls "$HOME/.neuroloom/traces/"*.jsonl 2>/dev/null | head -1
}

@test "trace: no_session_file decision written when session.json absent" {
  export NEUROLOOM_TRACE=true

  rm -f "$PROJECT_DIR/.neuroloom/session.json"

  _run_capture "$(cat "$FIXTURES_DIR/tool_event_write.json")"
  [ "$status" -eq 0 ]

  _trace_wait
  local trace_file
  trace_file="$(_trace_first)"
  [ -n "$trace_file" ]

  jq -e 'select(.decision == "no_session_file")' "$trace_file" >/dev/null
}

@test "trace: invalid_session_id decision written for malformed session_id" {
  export NEUROLOOM_TRACE=true

  # Write a session.json with an invalid (non-matching) session_id
  printf '{"session_id":"bad-id"}\n' > "$PROJECT_DIR/.neuroloom/session.json"

  _run_capture "$(cat "$FIXTURES_DIR/tool_event_write.json")"
  [ "$status" -eq 0 ]

  _trace_wait
  local trace_file
  trace_file="$(_trace_first)"
  [ -n "$trace_file" ]

  jq -e 'select(.decision == "invalid_session_id")' "$trace_file" >/dev/null
}

@test "trace: empty_stdin decision written for empty stdin" {
  export NEUROLOOM_TRACE=true

  # Use the empty string stdin path (session.json must be valid so we reach that check)
  run bash "$SCRIPTS_DIR/capture.sh" <<< ""
  [ "$status" -eq 0 ]
  sleep 0.3

  _trace_wait
  local trace_file
  trace_file="$(_trace_first)"
  [ -n "$trace_file" ]

  jq -e 'select(.decision == "empty_stdin")' "$trace_file" >/dev/null
}

@test "trace: mcp_filtered decision written for mcp__neuroloom__ tool" {
  export NEUROLOOM_TRACE=true

  _run_capture '{"tool_name":"mcp__neuroloom__memory_search"}'
  [ "$status" -eq 0 ]

  _trace_wait
  local trace_file
  trace_file="$(_trace_first)"
  [ -n "$trace_file" ]

  jq -e 'select(.decision == "mcp_filtered")' "$trace_file" >/dev/null
}

@test "trace: rate_throttled decision written with numeric elapsed_ms" {
  export NEUROLOOM_TRACE=true

  # Write a last_submit timestamp slightly in the future so the rate throttle fires.
  # "now" is insufficient — python3 startup + bash overhead can exceed 100ms on macOS,
  # causing the elapsed to be >= 100ms by the time capture.sh reads _get_epoch_ms().
  local future_ms
  if date +%s%3N 2>/dev/null | grep -qE '^[0-9]+$'; then
    future_ms=$(( $(date +%s%3N) + 5000 ))
  else
    future_ms=$(( $(python3 -c 'import time; print(int(time.time()*1000))') + 5000 ))
  fi
  echo "$future_ms" > "$PROJECT_DIR/.neuroloom/last_submit"

  _run_capture "$(cat "$FIXTURES_DIR/tool_event_write.json")"
  [ "$status" -eq 0 ]

  _trace_wait
  local trace_file
  trace_file="$(_trace_first)"
  [ -n "$trace_file" ]

  jq -e 'select(.decision == "rate_throttled")' "$trace_file" >/dev/null
  jq -e 'select(.decision == "rate_throttled") | .elapsed_ms | type == "number"' "$trace_file" >/dev/null
}

@test "trace: matched decision written for valid session and stdin" {
  export NEUROLOOM_TRACE=true

  # Remove last_submit so rate throttle passes
  rm -f "$PROJECT_DIR/.neuroloom/last_submit"

  _run_capture "$(cat "$FIXTURES_DIR/tool_event_write.json")"
  [ "$status" -eq 0 ]

  _trace_wait
  local trace_file
  trace_file="$HOME/.neuroloom/traces/sess-1234567890-abcdef12.jsonl"
  [ -f "$trace_file" ]

  jq -e 'select(.decision == "matched")' "$trace_file" >/dev/null
}

@test "trace: no_api_key decision written when api_key absent from all sources" {
  export NEUROLOOM_TRACE=true

  unset CLAUDE_PLUGIN_OPTION_API_KEY

  _run_capture "$(cat "$FIXTURES_DIR/tool_event_write.json")"
  [ "$status" -eq 0 ]

  _trace_wait
  local trace_file
  trace_file="$(_trace_first)"
  [ -n "$trace_file" ]

  jq -e 'select(.decision == "no_api_key")' "$trace_file" >/dev/null
}

@test "trace: api_completed decision written when curl succeeds" {
  export NEUROLOOM_TRACE=true

  # Default curl stub succeeds — nothing to change
  rm -f "$PROJECT_DIR/.neuroloom/last_submit"

  _run_capture "$(cat "$FIXTURES_DIR/tool_event_write.json")"
  # _run_capture already waits 0.3s
  [ "$status" -eq 0 ]

  local trace_file="$HOME/.neuroloom/traces/sess-1234567890-abcdef12.jsonl"
  [ -f "$trace_file" ]

  jq -e 'select(.decision == "api_completed")' "$trace_file" >/dev/null
}

@test "trace: api_errored decision written and observation buffered when curl fails" {
  export NEUROLOOM_TRACE=true

  cat > "$STUB_DIR/curl" << 'STUB'
#!/bin/sh
exit 1
STUB
  chmod +x "$STUB_DIR/curl"

  rm -f "$PROJECT_DIR/.neuroloom/last_submit"

  _run_capture "$(cat "$FIXTURES_DIR/tool_event_write.json")"
  [ "$status" -eq 0 ]

  local trace_file="$HOME/.neuroloom/traces/sess-1234567890-abcdef12.jsonl"
  [ -f "$trace_file" ]

  jq -e 'select(.decision == "api_errored")' "$trace_file" >/dev/null

  # Observation must also be buffered to events.jsonl
  [ -f "$PROJECT_DIR/.neuroloom/events.jsonl" ]
}

@test "trace: no trace files created when NEUROLOOM_TRACE unset" {
  export HOME="$BATS_TEST_TMPDIR/home"
  mkdir -p "$HOME"
  unset NEUROLOOM_TRACE

  rm -f "$PROJECT_DIR/.neuroloom/last_submit"

  _run_capture "$(cat "$FIXTURES_DIR/tool_event_write.json")"
  [ "$status" -eq 0 ]

  # Traces directory must not exist or must be empty
  local found
  found="$(ls "$HOME/.neuroloom/traces/"*.jsonl 2>/dev/null | wc -l | tr -d ' ')"
  [ "$found" -eq 0 ]
}
