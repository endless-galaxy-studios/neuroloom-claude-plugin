---
name: neuroloom:status
description: Show Neuroloom memory engine status — what it knows, how many memories, edge counts, buffer depth, and connection health.
---

When the user runs this command, gather and present Neuroloom status. Execute steps in order — if step 1 fails, stop early as indicated.

Note: These paths are relative to your project root. Run `/neuroloom:status` from the directory where you launched `claude`.

1. **Connection check (authoritative)**: Call the `workspace_insight` tool from the `neuroloom` MCP server.

   - If the MCP call succeeds → the API key is configured correctly. Record "Connected" and proceed with the data returned.
   - If the MCP call fails with an authentication error (401/403) → show: `API Key: Invalid or expired — run /plugins configure neuroloom` and stop.
   - If the MCP call fails with a connection error → show: `Connection: Failed (MCP server unreachable)` and stop.
   - If `workspace_insight` returns an `"error"` key → show the error message and stop.

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
  Connection:  Connected (MCP server responding)
  Session:     sess-1234567890-abcdef12 (active since 2026-03-25 14:30)
  Buffer:      3 events pending flush
  Version:     0.1.0

Memory Base
  Total:       472 memories  |  47 relationship edges
  architecture: 202  |  sdlc_knowledge: 389  |  general: 132
  pattern: 86       |  decision: 22          |  incident: 19
  discovery: 17     |  convention: 11        |  wiki: 3

  Relationships by type (top 7):
    similar_to: 28  |  references: 12  |  related_to: 7  |  ...

  Observation buffer:  2 pending  (not yet extracted into memories)
  Last memory added:   2026-04-19 14:22 UTC
  Last discovery run:  2026-04-19 08:00 UTC (nightly job)
```

**Formatting rules:**
- `memory_counts` is a dict keyed by memory type — include ALL 9 types even when their count is 0, so evaluators see the full taxonomy.
- `total_relationships` is the total edge count for the workspace.
- `relationship_counts` is a list of `{relationship_type, count}` objects sorted by count descending.
- `last_discovery_run_at` is an ISO timestamp or null — if null, show "not yet run (nightly job hasn't fired)".
- `pending_observations` is the observation buffer depth from `workspace_insight` (plus `events.jsonl` lines from step 3 for local buffered events not yet flushed to the API).
- `last_memory_added_at` is an ISO timestamp or null — if null, show "no memories stored yet".
- Keep the layout scannable in 5 seconds. No JSON blobs in the default output.

If session.json does not exist, report "No active session — a new one starts automatically on your next Claude Code session."

5. **Memory base check (legacy seed detection)**: Skip this step if `workspace_insight` already returned meaningful data. Otherwise, fall back to a `memory_search` call:
   - `query`: `"project seed memories"`
   - `tags`: `["seed"]`

   Count results where `"seed"` appears in the `memory.tags` list. If zero: `Memory base: Empty — run /neuroloom:init to build your project's memory base.`
