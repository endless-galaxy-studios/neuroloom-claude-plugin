#!/usr/bin/env bats
# summarize.bats — Tests for scripts/summarize.sh (Stop hook)
#
# Mock strategy: PATH-prepend stubs in $STUB_DIR.
#
# Config model: env-var only. lib/config.sh reads CLAUDE_PLUGIN_OPTION_API_KEY.
# at source time. Tests must `cd` into a project temp directory BEFORE running
# the script so STATE_DIR resolves to that directory.

SCRIPTS_DIR="$(cd "$(dirname "$BATS_TEST_FILENAME")/../scripts" && pwd)"

# ---------------------------------------------------------------------------
# Setup / Teardown
# ---------------------------------------------------------------------------

setup() {
  export BATS_TEST_TMPDIR
  BATS_TEST_TMPDIR="$(mktemp -d)"

  # Override HOME so config.sh doesn't find real ~/.claude/settings.json
  export HOME="$BATS_TEST_TMPDIR/home"
  mkdir -p "$HOME"

  # Project root simulation: lib/config.sh uses $PWD at source time,
  # so the test must cd here before running the script.
  export PROJECT_DIR="$BATS_TEST_TMPDIR/project"
  mkdir -p "$PROJECT_DIR/.neuroloom"
  cd "$PROJECT_DIR"

  export STUB_DIR="$BATS_TEST_TMPDIR/stubs"
  mkdir -p "$STUB_DIR"

  # Default curl stub: succeeds silently
  cat > "$STUB_DIR/curl" << 'STUB'
#!/bin/sh
exit 0
STUB
  chmod +x "$STUB_DIR/curl"

  export PATH="$STUB_DIR:$PATH"
  export NEUROLOOM_API_BASE="http://test.invalid"
  export NEUROLOOM_DEBUG=0
  export CLAUDE_PLUGIN_OPTION_API_KEY="nl_testkey1234"
}

teardown() {
  rm -rf "$BATS_TEST_TMPDIR"
}

_write_session() {
  printf '{"session_id":"sess-1234567890-abcdef12"}\n' > "$PROJECT_DIR/.neuroloom/session.json"
  chmod 600 "$PROJECT_DIR/.neuroloom/session.json"
}

# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

@test "happy path: session.json exists -> POST to end endpoint called, session.json removed" {
  _write_session

  # curl stub: record endpoints called
  cat > "$STUB_DIR/curl" << STUB
#!/bin/sh
for arg in "\$@"; do
  printf '%s\n' "\$arg" >> "$PROJECT_DIR/.neuroloom/curl_endpoints.txt"
done
exit 0
STUB
  chmod +x "$STUB_DIR/curl"

  run bash "$SCRIPTS_DIR/summarize.sh"
  [ "$status" -eq 0 ]

  # The end endpoint must have been called
  [ -f "$PROJECT_DIR/.neuroloom/curl_endpoints.txt" ]
  grep -q "sess-1234567890-abcdef12/end" "$PROJECT_DIR/.neuroloom/curl_endpoints.txt"

  # session.json must be removed (unconditional cleanup via EXIT trap)
  [ ! -f "$PROJECT_DIR/.neuroloom/session.json" ]
}

@test "session absent: no session.json -> exits 0, curl not called" {
  # No session.json
  [ ! -f "$PROJECT_DIR/.neuroloom/session.json" ]

  cat > "$STUB_DIR/curl" << STUB
#!/bin/sh
touch "$PROJECT_DIR/.neuroloom/curl_called"
exit 0
STUB
  chmod +x "$STUB_DIR/curl"

  run bash "$SCRIPTS_DIR/summarize.sh"
  [ "$status" -eq 0 ]

  [ ! -f "$PROJECT_DIR/.neuroloom/curl_called" ]
}

@test "API failure: curl exits non-zero -> session.json still removed (unconditional cleanup)" {
  _write_session

  # curl stub: fail
  cat > "$STUB_DIR/curl" << 'STUB'
#!/bin/sh
exit 1
STUB
  chmod +x "$STUB_DIR/curl"

  run bash "$SCRIPTS_DIR/summarize.sh"
  [ "$status" -eq 0 ]

  # session.json must be removed regardless of curl result (EXIT trap is unconditional)
  [ ! -f "$PROJECT_DIR/.neuroloom/session.json" ]
}

@test "config absent: env var unset -> exits 0 silently, curl not called" {
  unset CLAUDE_PLUGIN_OPTION_API_KEY

  # Write session.json so we know the api key guard is the only exit point
  _write_session

  cat > "$STUB_DIR/curl" << STUB
#!/bin/sh
touch "$PROJECT_DIR/.neuroloom/curl_called"
exit 0
STUB
  chmod +x "$STUB_DIR/curl"

  run bash "$SCRIPTS_DIR/summarize.sh"
  [ "$status" -eq 0 ]

  # curl must not have been called
  [ ! -f "$PROJECT_DIR/.neuroloom/curl_called" ]

  # stdout must be empty
  [ -z "$output" ]
}

@test "invalid session format: malformed session_id in session.json -> exits 0, curl not called" {
  # Write a session.json with a session_id that doesn't match sess-<epoch>-<hex>
  printf '{"session_id":"not-a-valid-session"}\n' > "$PROJECT_DIR/.neuroloom/session.json"

  cat > "$STUB_DIR/curl" << STUB
#!/bin/sh
touch "$PROJECT_DIR/.neuroloom/curl_called"
exit 0
STUB
  chmod +x "$STUB_DIR/curl"

  run bash "$SCRIPTS_DIR/summarize.sh"
  [ "$status" -eq 0 ]

  [ ! -f "$PROJECT_DIR/.neuroloom/curl_called" ]

  # Known behavior: the EXIT trap is registered AFTER format validation.
  # An invalid session_id causes exit 0 at the [[ =~ ]] || exit 0 guard,
  # before the trap cleanup() is set. session.json is therefore NOT removed.
  [ -f "$PROJECT_DIR/.neuroloom/session.json" ]
}
