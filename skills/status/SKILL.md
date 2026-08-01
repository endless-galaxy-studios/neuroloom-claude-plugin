---
name: neuroloom:status
description: Show Neuroloom memory engine status — what it knows, how many memories, edge counts, buffer depth, and connection health.
---

When the user runs this command, gather and present Neuroloom status. Execute steps in order — if step 1 fails, still run step 1a (it makes no network call of its own and is independent of step 1's outcome — see step 1a below), then stop early as step 1 indicates. Steps 2 onward only run when step 1 succeeded.

Note: These paths are relative to your project root. Run `/neuroloom:status` from the directory where you launched `claude`.

1. **Connection check (authoritative)**: Call the `workspace_insight` tool from the `neuroloom` MCP server.

   - If the MCP call succeeds → the API key is configured correctly. Record "Connected" and proceed with the data returned.
   - If the MCP call fails with an authentication error (401/403) → show: `API Key: Invalid or expired — run /plugins configure neuroloom` and stop (after step 1a below).
   - If the MCP call fails with a connection error → show: `Connection: Failed (MCP server unreachable)` and stop (after step 1a below).
   - If `workspace_insight` returns an `"error"` key → show the error message and stop (after step 1a below).

1a. **Workspace routing check (R2-6, network-free)**: Run this step **regardless of whether step 1 succeeded or failed** — it makes no MCP/API call of its own, so it is meaningful even when the connection check above just failed. This is what makes "runs independent of step 1's outcome" actually true rather than aspirational (see `compare_desired_vs_observed`'s own docstring in `pyhooks/workspace_config.py`).

   ```bash
   ${CLAUDE_PLUGIN_ROOT}/.venv/bin/python -c "
   from pathlib import Path
   import datetime
   import sqlite3
   from pyhooks.workspace_config import compare_desired_vs_observed, RoutingComparison, DesiredKind

   project_root = Path.cwd()
   db_path = project_root / '.neuroloom.db'
   mcp_path = project_root / '.mcp.json'

   result = compare_desired_vs_observed(str(project_root), db_path)

   if result.comparison == RoutingComparison.MALFORMED:
       print('routing:malformed')
   elif result.comparison == RoutingComparison.UNOWNED_CONFLICT:
       print('routing:unowned_conflict')
   elif result.comparison == RoutingComparison.OUT_OF_SYNC:
       print('routing:out_of_sync')
   else:
       # CONVERGED — compare_desired_vs_observed is file-only and cannot see
       # across a restart boundary (its own docstring says so explicitly).
       # Disambiguate 'not yet restarted' from genuinely 'in sync' using
       # .mcp.json's mtime against this session's started_at (sessions
       # table, already queried below in step 2) — if the file was written
       # after this session began, the live MCP connection (opened at
       # session start, before the write) can't reflect it yet.
       not_yet_restarted = False
       if mcp_path.exists() and db_path.exists():
           try:
               conn = sqlite3.connect(str(db_path))
               row = conn.execute('SELECT started_at FROM sessions ORDER BY started_at DESC LIMIT 1').fetchone()
               conn.close()
               if row and row[0]:
                   started = datetime.datetime.strptime(row[0], '%Y-%m-%d %H:%M:%S').replace(tzinfo=datetime.timezone.utc)
                   mtime = datetime.datetime.fromtimestamp(mcp_path.stat().st_mtime, tz=datetime.timezone.utc)
                   not_yet_restarted = mtime > started
           except Exception:
               not_yet_restarted = False
       if not_yet_restarted:
           print('routing:not_yet_restarted')
       else:
           # This check is network-free (compare_desired_vs_observed's own
           # docstring) and cannot look up a workspace name from a bare
           # uuid, so this prints the uuid itself -- presented copy must
           # not claim it is a resolved name.
           uuid_value = result.desired.value if result.desired.kind == DesiredKind.SET_TO_VALUE else ''
           print('routing:converged:' + (uuid_value or ''))
   "
   ```

   - `routing:converged:{uuid}` → show `Routing: in sync (workspace {uuid})` (omit the parenthetical if `{uuid}` is empty — i.e. desired state is "no override"; in that case show `Routing: in sync (auto-resolve)`). This is genuinely a uuid, not a resolved workspace name — this check is network-free and has no way to look one up.
   - `routing:not_yet_restarted` → show `Routing: changed, restart to apply`.
   - `routing:out_of_sync` or `routing:unowned_conflict` → show `Routing: out of sync — run /neuroloom:workspace`.
   - `routing:malformed` → show `Routing: couldn't read .mcp.json — run /neuroloom:workspace to check it`.

   This line is included in the structured summary below (Neuroloom Status block) even when step 1 failed — place it directly under the `Connection:` line.

2. **Session info**: Query `.neuroloom.db` using the Bash tool:
   ```bash
   sqlite3 .neuroloom.db "SELECT session_id, started_at FROM sessions LIMIT 1;" 2>/dev/null || echo "No active session"
   ```
   Show the session ID and when it started (use the `started_at` column).

3. **Buffer health**: Count buffered events using the Bash tool:
   ```bash
   sqlite3 .neuroloom.db "SELECT COUNT(*) FROM event_buffer;" 2>/dev/null || echo "0"
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
  Routing:     in sync (workspace a1b2c3d4-...)
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
- `pending_observations` is the observation buffer depth from `workspace_insight` (plus `event_buffer` row count from step 3 for local buffered events not yet flushed to the API).
- `last_memory_added_at` is an ISO timestamp or null — if null, show "no memories stored yet".
- Keep the layout scannable in 5 seconds. No JSON blobs in the default output.

If `.neuroloom.db` does not exist or the `sessions` table is empty, report "No active session — a new one starts automatically on your next Claude Code session."

5. **Memory base check (legacy seed detection)**: Skip this step if `workspace_insight` already returned meaningful data. Otherwise, fall back to a `memory_search` call:
   - `query`: `"project seed memories"`
   - `tags`: `["seed"]`

   Count results where `"seed"` appears in the `memory.tags` list. If zero: `Memory base: Empty — run /neuroloom:init to build your project's memory base.`
