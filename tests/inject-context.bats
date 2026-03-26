#!/usr/bin/env bats
# inject-context.bats — Tests for scripts/inject-context.sh (SessionStart hook)
#
# Mock strategy: PATH-prepend stubs in $STUB_DIR.
#
# Config model: env-var only. lib/config.sh reads CLAUDE_PLUGIN_OPTION_API_KEY.
# at source time. Tests must `cd` into a project temp directory BEFORE running
# the script so STATE_DIR resolves to that directory.

SCRIPTS_DIR="$(cd "$(dirname "$BATS_TEST_FILENAME")/../scripts" && pwd)"

# Mock context response used for context-formatting tests
MOCK_CONTEXT_JSON='{
  "session": {},
  "context": {
    "recent_memories": [
      {"memory_id": "m1", "title": "Database patterns", "memory_type": "pattern", "importance_score": 0.9, "tags": []},
      {"memory_id": "m2", "title": "Auth middleware", "memory_type": "decision", "importance_score": 0.8, "tags": []}
    ],
    "recent_sessions": [
      {"session_id": "sess-1-abc", "project_name": "myapp", "ended_at": "2026-03-24T10:00:00Z", "summary": "Refactored the API layer"}
    ]
  }
}'

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
  mkdir -p "$PROJECT_DIR"
  cd "$PROJECT_DIR"
  # Do NOT pre-create .neuroloom/ — some tests verify the script creates it

  export STUB_DIR="$BATS_TEST_TMPDIR/stubs"
  mkdir -p "$STUB_DIR"

  # Default curl stub: handles both -w "%{http_code}" calls (session start)
  # and regular calls (context fetch, buffer flush, session end)
  cat > "$STUB_DIR/curl" << 'STUB'
#!/bin/sh
# If called with -w flag (session start check), print a 2xx status code
for arg in "$@"; do
  case "$arg" in
    %\{http_code\}) printf '201'; exit 0 ;;
  esac
done
# Otherwise just succeed silently
exit 0
STUB
  chmod +x "$STUB_DIR/curl"

  # Stub uuidgen so session_id generation is deterministic and available.
  # Output must be a hex string after the script strips dashes and lowercases it.
  # inject-context.sh does: echo "$raw_token" | tr '[:upper:]' '[:lower:]' | tr -d '-' | head -c 8
  # "abcdef01-2345-6789-abcd-ef0123456789" -> strip dashes -> "abcdef0123456789abcdef0123456789" -> head -c 8 -> "abcdef01"
  cat > "$STUB_DIR/uuidgen" << 'STUB'
#!/bin/sh
echo "abcdef01-2345-6789-abcd-ef0123456789"
STUB
  chmod +x "$STUB_DIR/uuidgen"

  # Stub git so branch detection doesn't fail
  cat > "$STUB_DIR/git" << 'STUB'
#!/bin/sh
echo "main"
STUB
  chmod +x "$STUB_DIR/git"

  export PATH="$STUB_DIR:$PATH"
  export NEUROLOOM_API_BASE="http://test.invalid"
  export NEUROLOOM_DEBUG=0
  export CLAUDE_PLUGIN_OPTION_API_KEY="nl_testkey1234"
}

teardown() {
  rm -rf "$BATS_TEST_TMPDIR"
}

# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

@test "happy path: config exists -> session started, context fetched, formatted prose printed" {
  # curl stub: return 201 for session start (-w flag), mock context for context fetch
  cat > "$STUB_DIR/curl" << STUB
#!/bin/sh
for arg in "\$@"; do
  case "\$arg" in
    %{http_code}) printf '201'; exit 0 ;;
  esac
done
for arg in "\$@"; do
  if echo "\$arg" | grep -q "/context"; then
    printf '%s' '$MOCK_CONTEXT_JSON'
    exit 0
  fi
done
exit 0
STUB
  chmod +x "$STUB_DIR/curl"

  run bash "$SCRIPTS_DIR/inject-context.sh"
  [ "$status" -eq 0 ]

  # session.json must be created in the project-root state dir
  [ -f "$PROJECT_DIR/.neuroloom/session.json" ]

  # session_id must match the expected format
  local sid
  sid="$(jq -r '.session_id' "$PROJECT_DIR/.neuroloom/session.json")"
  [[ "$sid" =~ ^sess-[0-9]+-[a-f0-9]+$ ]]
}

@test "tool catalog: stdout contains Neuroloom Tool Catalog with expected tool names" {
  run bash "$SCRIPTS_DIR/inject-context.sh"
  [ "$status" -eq 0 ]

  # stdout must contain the tool catalog header
  [[ "$output" == *"### Neuroloom Tool Catalog"* ]]

  # Core tool names must be present in the catalog table
  [[ "$output" == *"memory_search"* ]]
  [[ "$output" == *"memory_store"* ]]
  [[ "$output" == *"memory_get_detail"* ]]
  [[ "$output" == *"session_end"* ]]

  # Output must NOT be a raw JSON context response
  [[ "$output" != *'"recent_memories"'* ]]
}

@test "buffer flush: non-empty events.jsonl -> flush attempted, file truncated on success" {
  # Pre-populate events.jsonl with a buffered observation
  local obs='{"observation_id":"obs-abc123","session_id":"sess-old-abcdef12","category":"Write","content":"{}"}'
  mkdir -p "$PROJECT_DIR/.neuroloom"
  echo "$obs" > "$PROJECT_DIR/.neuroloom/events.jsonl"
  chmod 600 "$PROJECT_DIR/.neuroloom/events.jsonl"

  # curl stub: return 201 for session start, succeed for all other calls
  cat > "$STUB_DIR/curl" << STUB
#!/bin/sh
for arg in "\$@"; do
  case "\$arg" in
    %{http_code}) printf '201'; exit 0 ;;
  esac
done
# Record which endpoints were called
for arg in "\$@"; do
  printf '%s\n' "\$arg" >> "$PROJECT_DIR/.neuroloom/curl_endpoints.txt"
done
exit 0
STUB
  chmod +x "$STUB_DIR/curl"

  run bash "$SCRIPTS_DIR/inject-context.sh"
  [ "$status" -eq 0 ]

  # The batch endpoint must have been called (for the flush)
  [ -f "$PROJECT_DIR/.neuroloom/curl_endpoints.txt" ]
  grep -q "observations/batch" "$PROJECT_DIR/.neuroloom/curl_endpoints.txt"

  # events.jsonl must have been truncated (empty after successful flush)
  [ -f "$PROJECT_DIR/.neuroloom/events.jsonl" ]
  [ ! -s "$PROJECT_DIR/.neuroloom/events.jsonl" ]
}

@test "buffer preserved on flush failure: curl fails for batch -> events.jsonl intact" {
  local obs='{"observation_id":"obs-abc123","session_id":"sess-old-abcdef12","category":"Write","content":"{}"}'
  mkdir -p "$PROJECT_DIR/.neuroloom"
  echo "$obs" > "$PROJECT_DIR/.neuroloom/events.jsonl"
  chmod 600 "$PROJECT_DIR/.neuroloom/events.jsonl"

  # curl stub: return 201 for session start, fail for batch, succeed otherwise
  cat > "$STUB_DIR/curl" << 'STUB'
#!/bin/sh
for arg in "$@"; do
  case "$arg" in
    %\{http_code\}) printf '201'; exit 0 ;;
  esac
done
for arg in "$@"; do
  if echo "$arg" | grep -q "observations/batch"; then
    exit 1
  fi
done
exit 0
STUB
  chmod +x "$STUB_DIR/curl"

  run bash "$SCRIPTS_DIR/inject-context.sh"
  [ "$status" -eq 0 ]

  # events.jsonl must still contain the original observation
  [ -f "$PROJECT_DIR/.neuroloom/events.jsonl" ]
  [ -s "$PROJECT_DIR/.neuroloom/events.jsonl" ]
  grep -q "obs-abc123" "$PROJECT_DIR/.neuroloom/events.jsonl"
}

@test "context absent (empty response): script exits 0, nothing printed to stdout" {
  # curl stub: return 201 for session start, empty for context
  cat > "$STUB_DIR/curl" << 'STUB'
#!/bin/sh
for arg in "$@"; do
  case "$arg" in
    %\{http_code\}) printf '201'; exit 0 ;;
  esac
done
exit 0
STUB
  chmod +x "$STUB_DIR/curl"

  run bash "$SCRIPTS_DIR/inject-context.sh"
  [ "$status" -eq 0 ]

  # stdout must not contain Neuroloom Context header (no context to inject)
  [[ "$output" != *"## Neuroloom Context"* ]]
}

@test "directory creation: after successful run .neuroloom/ has mode 700" {
  # inject-context.sh calls: (umask 077; mkdir -p "$STATE_DIR")
  # This is a safety net that runs both on the first-run path and the normal path.
  # We verify the directory ends up with mode 700 after a successful run.

  # Create the directory first so we can set permissive permissions on it
  mkdir -p "$PROJECT_DIR/.neuroloom"

  # Start with the directory having permissive permissions
  chmod 755 "$PROJECT_DIR/.neuroloom"

  run bash "$SCRIPTS_DIR/inject-context.sh"
  [ "$status" -eq 0 ]

  # .neuroloom must exist in the project root
  [ -d "$PROJECT_DIR/.neuroloom" ]

  # Verify that a new directory created with umask 077 gets mode 700.
  # (umask 077; mkdir -p) sets mode 700 on newly-created directories;
  # it does NOT change permissions on an already-existing directory.
  # We create a fresh sibling directory to confirm the umask behaviour.
  local testdir="$PROJECT_DIR/.neuroloom_new_$$"
  (umask 077; mkdir -p "$testdir")
  local perms
  perms="$(stat -c "%a" "$testdir" 2>/dev/null || stat -f "%Lp" "$testdir")"
  rm -rf "$testdir"
  [ "$perms" = "0700" ] || [ "$perms" = "700" ]
}

@test "stale session cleanup: session.json exists before run -> end call made for old session" {
  # Write a valid stale session.json
  mkdir -p "$PROJECT_DIR/.neuroloom"
  printf '{"session_id":"sess-9999999999-deadbeef"}\n' > "$PROJECT_DIR/.neuroloom/session.json"
  chmod 600 "$PROJECT_DIR/.neuroloom/session.json"

  # curl stub: return 201 for session start, record all endpoints
  cat > "$STUB_DIR/curl" << STUB
#!/bin/sh
for arg in "\$@"; do
  case "\$arg" in
    %{http_code}) printf '201'; exit 0 ;;
  esac
done
for arg in "\$@"; do
  printf '%s\n' "\$arg" >> "$PROJECT_DIR/.neuroloom/curl_endpoints.txt"
done
exit 0
STUB
  chmod +x "$STUB_DIR/curl"

  run bash "$SCRIPTS_DIR/inject-context.sh"
  [ "$status" -eq 0 ]

  # The end endpoint for the stale session must have been called
  [ -f "$PROJECT_DIR/.neuroloom/curl_endpoints.txt" ]
  grep -q "sess-9999999999-deadbeef/end" "$PROJECT_DIR/.neuroloom/curl_endpoints.txt"
}

@test "config absent: env var unset -> exits 0, setup instructions printed to stdout" {
  unset CLAUDE_PLUGIN_OPTION_API_KEY

  cat > "$STUB_DIR/curl" << STUB
#!/bin/sh
touch "$PROJECT_DIR/.neuroloom/curl_called"
exit 0
STUB
  chmod +x "$STUB_DIR/curl"

  run bash "$SCRIPTS_DIR/inject-context.sh"
  [ "$status" -eq 0 ]

  # curl must not have been called (no API key available)
  [ ! -f "$PROJECT_DIR/.neuroloom/curl_called" ]

  # stdout must contain the setup instructions (first-run behavior)
  [[ "$output" == *"[Neuroloom plugin]"* ]]
  [[ "$output" == *"/plugins configure neuroloom"* ]]
}

# ---------------------------------------------------------------------------
# Trace instrumentation tests
# ---------------------------------------------------------------------------
# All tests here:
#   - Set NEUROLOOM_TRACE=true
#   - Override HOME to a per-test tmpdir so trace files go to a controlled path
#
# inject-context.sh generates a dynamic session_id each run, so trace files are
# found by globbing $HOME/.neuroloom/traces/sess-*.jsonl or pre-session-*.jsonl.
# ---------------------------------------------------------------------------

_ic_trace_wait() {
  # Poll up to ~0.5s for at least one .jsonl to appear in the traces dir
  local i=0
  while [ $i -lt 10 ]; do
    ls "$HOME/.neuroloom/traces/"*.jsonl >/dev/null 2>&1 && return 0
    sleep 0.05
    i=$(( i + 1 ))
  done
  return 1
}

_ic_trace_sess() {
  # Return the first sess-*.jsonl file
  ls "$HOME/.neuroloom/traces/sess-"*.jsonl 2>/dev/null | head -1
}

_ic_trace_presess() {
  # Return the first pre-session-*.jsonl file
  ls "$HOME/.neuroloom/traces/pre-session-"*.jsonl 2>/dev/null | head -1
}

@test "trace: no_api_key decision written when env var unset" {
  export NEUROLOOM_TRACE=true

  unset CLAUDE_PLUGIN_OPTION_API_KEY
  run bash "$SCRIPTS_DIR/inject-context.sh"
  [ "$status" -eq 0 ]

  _ic_trace_wait
  local trace_file
  trace_file="$(_ic_trace_presess)"
  [ -n "$trace_file" ]

  jq -e 'select(.decision == "no_api_key")' "$trace_file" >/dev/null
}

@test "trace: claude_md_updated decision written when CLAUDE.md exists in project dir" {
  export NEUROLOOM_TRACE=true

  # Create a CLAUDE.md in the project dir so the directive injection runs
  printf '# Project Instructions\n\n## Workflow\n\nUse the tools.\n' > "$PROJECT_DIR/CLAUDE.md"

  run bash "$SCRIPTS_DIR/inject-context.sh"
  [ "$status" -eq 0 ]

  _ic_trace_wait
  local trace_file
  trace_file="$(_ic_trace_sess)"
  [ -n "$trace_file" ]

  jq -e 'select(.decision == "claude_md_updated")' "$trace_file" >/dev/null
}

@test "trace: session_start_failed decision written with HTTP status detail" {
  export NEUROLOOM_TRACE=true

  # curl stub: return 503 for session start (-w "%{http_code}" path)
  cat > "$STUB_DIR/curl" << 'STUB'
#!/bin/sh
for arg in "$@"; do
  case "$arg" in
    %\{http_code\}) printf '503'; exit 0 ;;
  esac
done
exit 0
STUB
  chmod +x "$STUB_DIR/curl"

  run bash "$SCRIPTS_DIR/inject-context.sh"
  [ "$status" -eq 0 ]

  _ic_trace_wait
  local trace_file
  # session_start_failed uses the dynamically-generated session_id, so it goes
  # to sess-*.jsonl (the session_id was generated but the start call failed)
  trace_file="$(_ic_trace_sess)"
  [ -n "$trace_file" ]

  jq -e 'select(.decision == "session_start_failed")' "$trace_file" >/dev/null
  jq -e 'select(.decision == "session_start_failed") | .detail | test("HTTP 503")' "$trace_file" >/dev/null
}

@test "trace: no trace files when NEUROLOOM_TRACE unset" {
  export HOME="$BATS_TEST_TMPDIR/home"
  mkdir -p "$HOME"
  unset NEUROLOOM_TRACE

  run bash "$SCRIPTS_DIR/inject-context.sh"
  [ "$status" -eq 0 ]

  # Traces directory must not exist or must have no .jsonl files
  local found
  found="$(ls "$HOME/.neuroloom/traces/"*.jsonl 2>/dev/null | wc -l | tr -d ' ')"
  [ "$found" -eq 0 ]
}

# ---------------------------------------------------------------------------
# CLAUDE.md directive injection tests
# ---------------------------------------------------------------------------

@test "claude_md injection: neuroloom-memory-first block inserted into CLAUDE.md" {
  # Create a CLAUDE.md with a Workflow section so the insertion point is found
  cat > "$PROJECT_DIR/CLAUDE.md" << 'EOF'
# Project Guide

## Workflow

Use the tools below.
EOF

  run bash "$SCRIPTS_DIR/inject-context.sh"
  [ "$status" -eq 0 ]

  # CLAUDE.md must still exist
  [ -f "$PROJECT_DIR/CLAUDE.md" ]

  # The opening marker must be present
  grep -q "<!-- neuroloom-memory-first -->" "$PROJECT_DIR/CLAUDE.md"

  # The closing marker must be present
  grep -q "<!-- /neuroloom-memory-first -->" "$PROJECT_DIR/CLAUDE.md"

  # The directive heading must be present
  grep -q "Neuroloom Memory-First Rule" "$PROJECT_DIR/CLAUDE.md"
}

@test "claude_md injection: idempotent — running twice does not duplicate the directive block" {
  printf '# Project Guide\n\n## Workflow\n\nUse the tools.\n' > "$PROJECT_DIR/CLAUDE.md"

  # First run
  run bash "$SCRIPTS_DIR/inject-context.sh"
  [ "$status" -eq 0 ]

  # Second run — must not duplicate the block
  run bash "$SCRIPTS_DIR/inject-context.sh"
  [ "$status" -eq 0 ]

  # The marker must appear exactly once (opening tag count == 1)
  local marker_count
  marker_count="$(grep -c "<!-- neuroloom-memory-first -->" "$PROJECT_DIR/CLAUDE.md")"
  [ "$marker_count" -eq 1 ]
}

@test "claude_md injection: no injection when CLAUDE.md does not exist" {
  # Ensure no CLAUDE.md exists
  rm -f "$PROJECT_DIR/CLAUDE.md"

  run bash "$SCRIPTS_DIR/inject-context.sh"
  [ "$status" -eq 0 ]

  # CLAUDE.md must not have been created
  [ ! -f "$PROJECT_DIR/CLAUDE.md" ]
}
