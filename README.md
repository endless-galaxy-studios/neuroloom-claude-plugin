# Neuroloom Claude Code Plugin

Claude Code forgets everything between sessions. This plugin gives it persistent memory — every tool use is captured as an observation, and each new session starts with context from your past work.

Using Cursor or Windsurf? See the [MCP Integration guide](https://neuroloom.dev/docs/mcp-integration) for the `.mcp.json` setup path.

> **Note:** Always launch `claude` from the project root directory. The plugin writes session state to `.neuroloom/` relative to the directory where you run `claude`. If you launch from a subdirectory (e.g., `cd src && claude`), session state will be written to the wrong location.

---

## Prerequisites

- **`curl`** — standard on macOS and Linux
- **`jq`** — required for payload construction during tool capture. Install with:
  - macOS: `brew install jq`
  - Linux (Debian/Ubuntu): `apt install jq`
  - Other: https://jqlang.org/download/
- **`openssl`** — used for portable sha256 hashing, standard on macOS and Linux
- **Claude Code** with plugin support

---

## Quick Install

1. Add the Endless Galaxy Studios marketplace:
   ```
   /plugin marketplace add endless-galaxy-studios/claude-plugins
   ```

2. Install Neuroloom:
   ```
   /plugin install neuroloom@endless-galaxy-studios
   ```

3. Configure your API key by running `/plugins configure neuroloom`. Get your key at https://app.neuroloom.dev/settings/api-keys.

4. Add `.neuroloom/` to your project's `.gitignore` — it contains session state:
   ```
   .neuroloom/
   ```

5. Restart Claude Code, then verify:
   ```
   /neuroloom:status
   ```

---

## What You Get

- Your past decisions and patterns are available at every session start
- Tool use from previous sessions surfaces as searchable context — nothing is lost between sessions
- Claude queries Neuroloom automatically via MCP tools (`memory_search`, `memory_store`, etc.)
- `/neuroloom:status` — check session health, buffer depth, and MCP connection

---

## How It Works

When a session starts, the plugin creates a Neuroloom session, injects relevant memories from past work, and flushes any buffered observations. During the session, every tool use is captured and sent to the Neuroloom API in the background — if the API is unreachable, observations buffer locally to `.neuroloom/events.jsonl` in your project root and flush on the next session start. When the session ends, the plugin closes the session and triggers server-side memory extraction.

---

## Skills Reference

| Command | Description |
|---------|-------------|
| `/neuroloom:status` | Check session health, buffer depth, and connection |

---

## Configuration

Run `/plugins configure neuroloom` and enter your API key when prompted. Get your key at [app.neuroloom.dev/settings/api-keys](https://app.neuroloom.dev/settings/api-keys).

Run `/neuroloom:status` to verify your configuration is active.

---

## Troubleshooting

**No observations appearing in the Neuroloom dashboard**

Run `/neuroloom:status` to check that your API key is detected. If the config source shows `unknown`, re-run `/plugins configure neuroloom` to set your key, then restart Claude Code.

**Context not injecting at session start**

Run `/neuroloom:status` to check if a session is active. If not, the SessionStart hook may not have fired — try restarting Claude Code.

**`jq: command not found`**

Install jq before using the plugin:

- macOS: `brew install jq`
- Linux (Debian/Ubuntu): `sudo apt install jq`

Without jq, observation capture is disabled.

**MCP connection fails**

Run `/neuroloom:status` to verify your API key is configured. If the config source shows `unknown`, re-run `/plugins configure neuroloom` to set your key.

---

## Session Tracing

Session tracing is an opt-in diagnostic tool for debugging plugin behavior — not needed for normal use. When enabled, every exit path in the plugin scripts writes a structured JSON entry to a trace file.

### Enabling tracing

Set `NEUROLOOM_TRACE=true` before launching Claude Code:

```bash
NEUROLOOM_TRACE=true claude
```

Or add to your shell profile for persistent tracing:

```bash
export NEUROLOOM_TRACE=true
```

### Where trace files are written

- Session traces: `~/.neuroloom/traces/<session_id>.jsonl`
- Pre-session exits (before a session ID is available): `~/.neuroloom/traces/pre-session-YYYY-MM-DD.jsonl`

### Auto-cleanup

Trace files older than 14 days are deleted automatically at the start of each traced session. Date-based pre-session filenames (`pre-session-YYYY-MM-DD.jsonl`) ensure they age out correctly.

### Known gaps

- **Dependency gap:** If `jq` is not installed, tracing is entirely unavailable — `jq` is required by both the plugin and `trace.sh`.
- **Execution-order gap:** The ERR trap fires before any sourcing; bash errors at that point are not traced.

Source-order gaps have been resolved. With `trace.sh` sourced immediately after `config.sh`, all decision exits in both scripts are traceable. The only untraceable paths are the two gaps listed above.

### Performance

When `NEUROLOOM_TRACE` is unset, overhead is zero — the trace library guard returns immediately. When enabled, tracing adds ~1–2ms per traced exit.

---

## Update

To update to the latest version:

```
/plugin marketplace update endless-galaxy-studios
```

Buffered events in `.neuroloom/events.jsonl` and session state are not affected by plugin updates.

---

## License

MIT
