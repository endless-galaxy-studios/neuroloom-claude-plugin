---
name: neuroloom:status
description: Show Neuroloom session health — active session, buffer depth, and connection status.
---

When the user runs this command, gather and present Neuroloom status from four sources.

Note: These paths are relative to your project root. Run `/neuroloom:status` from the directory where you launched `claude`.

1. **Session info**: Read `.neuroloom/session.json` using the Bash tool:
   ```bash
   cat .neuroloom/session.json 2>/dev/null || echo "No active session"
   ```
   Show the session ID and when it started (use the `started_at` field if present; otherwise derive from the epoch in the session ID).

2. **Buffer health**: Count buffered events using the Bash tool:
   ```bash
   wc -l < .neuroloom/events.jsonl 2>/dev/null || echo "0"
   ```
   Report the number of events waiting to be flushed.

3. **Connection status**: Call the `memory_search` tool from the `neuroloom` MCP server with a simple test query (e.g., "test") to verify the MCP connection is working.

4. **Plugin version**: Read the version from the plugin manifest:
   ```bash
   cat ${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json 2>/dev/null
   ```

5. **API key check**: Test whether the API key is configured:
   ```bash
   [ -n "${CLAUDE_PLUGIN_OPTION_API_KEY:-}" ] && echo "configured" || echo "missing"
   ```
   If the result is "missing", show: `API Key: Not configured — run /plugins configure neuroloom` instead of proceeding to the session check.

Present results in a structured summary:
```
Neuroloom Status
  API Key:    Configured
  Session:    sess-1234567890-abcdef12 (active since 2026-03-25 14:30)
  Buffer:     3 events pending flush
  Connection: Connected (MCP server responding)
  Version:    0.1.0
```

If session.json does not exist, report "No active session — a new one starts automatically on your next Claude Code session."
