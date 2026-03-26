#!/usr/bin/env bash
# lib/config.sh — Config resolution for neuroloom-claude-plugin
# Source this file; it sets api_key and STATE_DIR in the caller.
#
# Config source: CLAUDE_PLUGIN_OPTION_API_KEY env var (set by Claude Code plugin runtime).

# Capture state directory as absolute at source time so callers
# that cd elsewhere still find the right directory.
STATE_DIR="${PWD}/.neuroloom"

api_key="${CLAUDE_PLUGIN_OPTION_API_KEY:-}"
