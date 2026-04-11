#!/usr/bin/env bash
# ensure-venv.sh — Idempotently bootstrap a plugin-local .venv with tree-sitter deps.
#
# Creates $PLUGIN_ROOT/.venv and installs the native tree-sitter packages
# required by codeweaver on first use. Subsequent invocations return
# immediately thanks to the .neuroloom-ready marker file.
#
# Usage:
#   ensure-venv.sh [PLUGIN_ROOT]
#
#   If PLUGIN_ROOT is omitted, it defaults to the directory containing this
#   script's parent (i.e., the plugin root itself).
#
# Exit codes:
#   0  — .venv is ready
#   1  — bootstrap failed (python3 missing, pip failed, etc.)
#
# Environment:
#   NEUROLOOM_DEBUG=1  — log progress to stderr

set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve PLUGIN_ROOT
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $# -ge 1 && -n "$1" ]]; then
    PLUGIN_ROOT="$1"
else
    PLUGIN_ROOT="$(dirname "$SCRIPT_DIR")"
fi

VENV_DIR="${PLUGIN_ROOT}/.venv"
MARKER="${VENV_DIR}/.neuroloom-ready"
LOCK_FILE="${VENV_DIR}/.bootstrap.lock"

# ---------------------------------------------------------------------------
# Debug helper
# ---------------------------------------------------------------------------
_debug() {
    if [[ "${NEUROLOOM_DEBUG:-0}" == "1" ]]; then
        echo "[neuroloom:ensure-venv] $*" >&2
    fi
}

# ---------------------------------------------------------------------------
# Fast path: already bootstrapped
# ---------------------------------------------------------------------------
if [[ -f "$MARKER" ]]; then
    _debug "venv already ready at ${VENV_DIR}"
    exit 0
fi

# ---------------------------------------------------------------------------
# Verify python3 is available
# ---------------------------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
    echo "[neuroloom] ensure-venv: python3 not found on PATH — code graph sync disabled" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Lock: prevent concurrent bootstraps from racing.
# Uses a lock directory (mkdir is atomic on POSIX filesystems).
# ---------------------------------------------------------------------------
mkdir -p "$VENV_DIR"

acquire_lock() {
    local deadline=$(( $(date +%s) + 60 ))
    while ! mkdir "${LOCK_FILE}" 2>/dev/null; do
        if [[ $(date +%s) -ge $deadline ]]; then
            echo "[neuroloom] ensure-venv: timed out waiting for bootstrap lock" >&2
            exit 1
        fi
        # Another process is bootstrapping — wait and re-check the marker.
        sleep 1
        if [[ -f "$MARKER" ]]; then
            _debug "venv became ready while waiting for lock"
            exit 0
        fi
    done
}

release_lock() {
    rmdir "${LOCK_FILE}" 2>/dev/null || true
}

trap release_lock EXIT

acquire_lock

# Double-check after acquiring the lock — another process may have finished.
if [[ -f "$MARKER" ]]; then
    _debug "venv ready after acquiring lock"
    exit 0
fi

# ---------------------------------------------------------------------------
# Create the virtual environment
# ---------------------------------------------------------------------------
_debug "creating venv at ${VENV_DIR}"

if ! python3 -m venv "$VENV_DIR" 2>&1; then
    echo "[neuroloom] ensure-venv: failed to create venv at ${VENV_DIR}" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Install dependencies
# ---------------------------------------------------------------------------
PIP="${VENV_DIR}/bin/pip"

if [[ ! -x "$PIP" ]]; then
    echo "[neuroloom] ensure-venv: pip not found after venv creation" >&2
    exit 1
fi

_debug "installing neuroloom-codeweaver (includes tree-sitter deps)"

if ! "$PIP" install --quiet neuroloom-codeweaver 2>&1; then
    echo "[neuroloom] ensure-venv: pip install failed — code graph sync disabled" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Write the ready marker (only on full success)
# ---------------------------------------------------------------------------
touch "$MARKER"
_debug "bootstrap complete"
