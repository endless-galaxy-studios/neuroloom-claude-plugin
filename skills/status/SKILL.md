---
name: neuroloom:status
description: Show Neuroloom session health — active session, buffer depth, and connection status.
---

When the user runs this command, gather and present Neuroloom status. Execute steps in order — if step 1 fails, stop early as indicated.

Note: These paths are relative to your project root. Run `/neuroloom:status` from the directory where you launched `claude`.

1. **Connection check (authoritative)**: Call the `memory_search` tool from the `neuroloom` MCP server with a simple test query (e.g., "project overview") to verify the MCP connection is working.

   - If the MCP call succeeds → the API key is configured correctly. Record "Connected" and continue.
   - If the MCP call fails with an authentication error (401/403) → show: `API Key: Invalid or expired — run /plugins configure neuroloom` and stop.
   - If the MCP call fails with a connection error → show: `Connection: Failed (MCP server unreachable)` and stop.

2. **Session info**: Read `.neuroloom/session.json` using the Bash tool:
   ```bash
   cat .neuroloom/session.json 2>/dev/null || echo "No active session"
   ```
   Show the session ID and when it started (use the `started_at` field if present; otherwise derive from the epoch in the session ID).

3. **Buffer health**: Count buffered events using the Bash tool:
   ```bash
   wc -l < .neuroloom/events.jsonl 2>/dev/null || echo "0"
   ```
   Report the number of events waiting to be flushed.

4. **Plugin version**: Read the version from the plugin manifest:
   ```bash
   cat ${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json 2>/dev/null
   ```

Present results in a structured summary:
```
Neuroloom Status
  Connection: Connected (MCP server responding)
  Session:    sess-1234567890-abcdef12 (active since 2026-03-25 14:30)
  Buffer:     3 events pending flush
  Version:    0.1.0
```

If session.json does not exist, report "No active session — a new one starts automatically on your next Claude Code session."

5. **Memory base status**: Make a second `memory_search` call specifically for seed detection:
   - `query`: `"project seed memories"`
   - `tags`: `["seed"]`

   The response has the shape `{ count, results: [{ memory: { tags: [...], ... }, score, ... }] }`. Count the results where `"seed"` appears in the `memory.tags` list.

After presenting the summary above, append one additional line to the output:

- If the seed search returned zero results OR none of the results have a `"seed"` tag:
  ```
    Memory base: Empty — run /neuroloom:init to build your project's memory base.
  ```

- If one or more results have the `"seed"` tag:
  ```
    Memory base: [N] seed memories indexed
  ```

Where `[N]` is the count of results that have the `"seed"` tag.
