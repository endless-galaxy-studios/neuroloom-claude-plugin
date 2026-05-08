---
name: neuroloom:init
description: Bootstrap workspace memory by crawling the codebase and storing structured memories for all key files and modules.
---

You are executing the Neuroloom workspace initialization protocol. Your job is to crawl this codebase, understand its structure, and store a structured set of seed memories that will make future `memory_search` calls useful from the first query. Execute all phases below in order. Do not skip phases or reorder them.

---

## Phase 0 — API Key Setup

Before anything else, ensure a Neuroloom API key is configured for this project.

**Step 1: Check if a key already exists.**

Run this Bash command to check whether the plugin already has an API key configured:

```bash
test -n "${CLAUDE_PLUGIN_OPTION_API_KEY:-}" && echo "KEY_SET" || echo "KEY_MISSING"
```

If the output is `KEY_SET`, skip to Phase 1 — the key is already configured via the plugin system.

**Step 2: Prompt for the key.**

If the key is missing, ask the user:

```
No Neuroloom API key found for this project.

1. Go to https://app.neuroloom.dev/settings/api-key to get your key
2. Paste it here when ready

(If you've already configured it via `/plugins configure neuroloom`, just say "skip")
```

Wait for the user's response.

- If the user says "skip" or similar → proceed to Phase 1 without storing anything.
- If the user pastes a key → continue to Step 3.

**Step 3: Store the key in `.neuroloom.db`.**

Run this Bash command to store the key (replace `{KEY}` with the user's input — do NOT echo the key to stdout):

```bash
python3 -c "
import sqlite3, os
db = sqlite3.connect('.neuroloom.db')
db.execute('CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
db.execute('INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)', ('api_key', os.environ['_NL_KEY']))
db.commit()
db.close()
print('ok')
" 2>&1
```

Pass the key via an environment variable `_NL_KEY` to avoid it appearing in the command string or shell history.

**Step 4: Verify the key works.**

Run this Bash command to test the key against the API:

```bash
curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Token {KEY}" https://api.neuroloom.dev/api/v1/workspaces/
```

- `200` → Print `API key verified.` and proceed to Phase 1.
- `401` → Print `API key rejected — check that you copied the full key. Try again or say "skip" to continue without it.` Return to Step 2.
- Any other status or timeout → Print `Could not reach the Neuroloom API. The key has been saved — it will be verified on next session start.` Proceed to Phase 1.

Also remind the user to configure the key in the plugin system for future sessions:

```
Tip: Run `/plugins configure neuroloom` and paste your key there too — that persists it across all projects.
```

**Step 5: Configure workspace routing.**

After the API key is verified (or was already present), run the workspace auto-configuration:

```bash
${CLAUDE_PLUGIN_ROOT}/.venv/bin/python -c "
from pathlib import Path
from pyhooks.workspace_config import ensure_workspace_configured
import os

project_root = os.getcwd()
db_path = Path(project_root) / '.neuroloom.db'
api_key = os.environ.get('CLAUDE_PLUGIN_OPTION_API_KEY', '')

if not api_key:
    import sqlite3
    try:
        conn = sqlite3.connect(str(db_path))
        row = conn.execute('SELECT value FROM config WHERE key = ?', ('api_key',)).fetchone()
        api_key = row[0] if row else ''
        conn.close()
    except Exception:
        api_key = ''

ws = ensure_workspace_configured(
    project_root=project_root,
    db_path=db_path,
    api_base='https://api.neuroloom.dev',
    api_key=api_key,
)
print(f'workspace:{ws}' if ws else 'workspace:none')
"
```

- If the output contains `workspace:` followed by a UUID → Print `Workspace configured: [UUID]. All MCP requests will route to this workspace.`
- If the output is `workspace:none` → Print `Could not determine workspace — it will be configured on next session start.` Continue to Phase 1.

This step writes the `X-Workspace-Id` header into your project's `.mcp.json`, enabling per-project workspace routing. If you have multiple workspaces and want this project to use a different one, edit the `X-Workspace-Id` value in `.mcp.json`.

---

## Phase 1 — Self-Orient (no user input required)

**Step 1: Map the directory tree.**

Use the Glob tool with these depth-limited patterns in sequence:
- `*` — top-level entries
- `*/*` — one level deep
- `*/*/*` — two levels deep

Exclude results that begin with any of these directory prefixes: `.git`, `node_modules`, `__pycache__`, `.venv`, `dist`, `build`, `.next`, `target`, `vendor`. If a path segment matches one of those names, discard the entire result.

**Step 2: Read root-level orientation files.**

Read each of the following files if they exist. Skip missing files silently — do not report errors for absent files:
- `README.md`
- `package.json`
- `pyproject.toml`
- `docker-compose.yml`
- `Makefile`
- `go.mod`
- `.env.example`

Read at most 5 of these files — stop once you have enough to answer the four questions in Step 3. Do not read every file in the repo at this stage.

**Step 3: Identify the following — hold these internally, do not store them yet:**

(a) Project purpose in one sentence.
(b) Primary language(s) and framework(s).
(c) Top-level modules or service directories, each with a one-sentence description.
(d) Any areas the README explicitly flags as critical, complex, or non-obvious.

**Step 4: Produce an internal summary** from the above. This summary is your basis for the user question in Phase 2. Do not store it to Neuroloom yet.

**Step 5: Fallback check.** If fewer than 2 orientation files were found AND no top-level modules or service directories could be identified, present this warning to the user before proceeding to Phase 2:
```
I couldn't find enough structure to map this project automatically. Please confirm this is the correct working directory, or point me to the main source directory.
```
Wait for the user's response before continuing.

---

## Phase 2 — One Smart Question

Present your findings to the user in this exact format, then wait for their response before proceeding:

```
I've scanned your codebase. Here's what I found:

**Project:** [one sentence from Step 3a]
**Stack:** [languages and frameworks from Step 3b]
**Modules:**
- [module name] — [one sentence description]
- [module name] — [one sentence description]
(list all top-level modules/services identified)

One question before I dive in:
Are there specific modules or files you'd like me to prioritize, or anything important that isn't obvious from the code — dead directories, work-in-progress areas, or critical subsystems I should weight higher?
(e.g., "the legacy/ dir is dead code", "the worker/ service is the most complex part", "skip the scripts/ folder")

If everything looks right, just say "go" and I'll use heuristic priorities.
```

**Interpreting the user's response:**

- If the user says "go", "looks good", "proceed", "yes", "ok", "continue", or any similar affirmation with no specific direction → apply heuristic priorities only (defined in Phase 3).
- If the user names specific modules, directories, or files → re-weight the file read budget in Phase 3 to allocate more reads to those areas. A called-out service may receive up to 60% of the total file budget.
- If the user identifies dead directories or areas to skip → exclude those paths from the crawl entirely.

Do not proceed to Phase 3 until you have received and interpreted the user's response.

---

## Phase 3 — Crawl and Store

### Idempotency Check

Before reading any files, call `memory_search` once with:
- `query`: `"project seed memories"`
- `tags`: `["seed"]`

If the search returns one or more results:
- Report: `I found [N] existing seed memories.`
- Ask: `Run again to add coverage for new files only (existing memories are kept), or skip? (add/skip)`
- If the user says "skip" or equivalent → stop here, do not proceed.
- If the user says "add" or equivalent → proceed, but do not re-store memories for files already covered in existing seed memories. The response has the shape `{ count, results: [{ memory: { source_files: [...], tags: [...], title, ... }, score, ... }] }`. Check the `source_files` field of each existing seed memory before deciding whether to store a new one for the same path.

If the search returns zero results → proceed immediately without asking.

### File Read Budget

Read at most 50 files total. For small repos where fewer than 30 high-signal files exist, read what is available without padding. Never read low-value files to fill the budget.

### Default File Priority Ranking

Rank files for reading in this order when user has not specified priorities. Within each tier, use the framework-aware tiebreakers below.

**Tier 1 — Entry points** (read first):
`main.py`, `app.py`, `index.ts`, `server.ts`, `main.go`, any file directly inside `cmd/`

**Tier 2 — Data models and schemas:**
`models.py`, `schema.ts`, `types.ts`, any `*.prisma` file, any file with `model` or `schema` in its name inside a top-level module directory

**Tier 3 — Route and handler definitions:**
Files inside `routes/`, `router.py`, files inside `controllers/`, `views.py`

**Tier 4 — Service and business logic:**
Files inside `services/`, files inside `lib/`

**Tier 5 — Configuration:**
`.env.example`, any `*.config.ts`, `pyproject.toml`, `docker-compose.yml`

**Tier 6 — Utilities and helpers:**
Files inside `utils/`, `helpers/`, `middleware/`

**Tier 7 — Tests** (read last, limit to 3-5 files total):
Only if test names clearly reveal important behaviors (e.g., `test_billing_proration.py`, not `test_utils.py`). Do not read test files otherwise.

**Framework-aware tiebreakers within tiers:**

- FastAPI project → within each tier, prefer SQLAlchemy model files and files inside `routers/` first
- Next.js project → within each tier, prefer files inside `app/` route directories and `lib/` first
- Go project → within each tier, prefer files inside `cmd/` and `internal/` first
- Django project → within each tier, prefer `models.py` and `views.py` within each app directory first

**Monorepo budget allocation:**

If the project has multiple top-level service directories, distribute reads proportionally across services. Allocate a minimum of 5 files per service. If the user called out a specific service in Phase 2, that service may receive up to 60% of the total 50-file budget.

### Memory Budget and Allocation

Store between 20 and 40 memories total, allocated as follows:

- **1 project overview memory** — one memory describing the entire project: its purpose, stack, top-level structure, and any critical non-obvious details.
- **5–10 module summary memories** — one memory per top-level module or service directory. Describe the module's role, its key files, its internal structure, and anything non-obvious about how it fits into the whole.
- **15–25 key file memories** — one memory per highest-signal file within each module.

If the repo has fewer high-signal files than the lower bounds, store what is warranted. Do not pad with low-quality content. Quality over quantity.

### Memory Type Mapping

Every memory item requires a `memory_type`. Use exactly one of these values:

| Content being stored | `memory_type` value |
|---|---|
| Project overview, module structure, service architecture | `architecture` |
| Coding patterns, framework conventions, design patterns | `pattern` |
| Naming conventions, style guides, code standards | `convention` |
| Config choices, environment variables, build setup | `decision` or `convention` |
| External service integrations, API clients, third-party SDKs | `architecture` |
| Dependency management decisions (package.json, pyproject.toml) | `decision` |
| Explicit design decisions from README, ADRs, or code comments | `decision` |
| Auth, secrets management, permission models, security patterns | `convention` or `pattern` |
| Performance-critical code, optimization notes, caching layers | `discovery` or `pattern` |
| Documentation files, API docs, README sections | `wiki` |
| Concepts, domain knowledge, or explanatory context | `discovery` |
| Something broke and was fixed; failure mode and resolution | `incident` |
| Debugging insights and non-obvious runtime behaviors | `discovery` |
| Refactoring decisions and rationale | `decision` |
| Anything that does not fit the above categories | `general` |

Do not invent other values. The full set of valid `memory_type` values is: `decision`, `pattern`, `convention`, `architecture`, `discovery`, `incident`, `general`, `wiki`, `sdlc_knowledge`. The table above covers the types most commonly needed during init. Do not use `document`, `file`, `code`, or any other string not in the valid set.

### `memory_store_batch` Parameter Reference

Collect all memories planned for the run into a list, then call `memory_store_batch` once
with the full list. Do not call `memory_store` in a loop.

`memory_store_batch` accepts a `memories` array. Each item supports the same fields as the
individual `memory_store` tool:

- `title` (required, string)
- `memory_type` (required, string)
- `content` (required, string)
- `summary` (optional, string)
- `concepts` (optional, list of strings)
- `tags` (optional, list of strings)
- `files` (optional, list of strings)
- `context_files` (optional, list of strings)
- `importance` (optional, float 0.0–1.0)
- `confidence` (optional, float 0.0–1.0)

**Batching strategy:** Build the full list of memory dicts in memory first. When the planned
memory count exceeds 100, split into batches of <=100 and call `memory_store_batch` once per batch.

**Progress reporting:** After each `memory_store_batch` call completes, print:

    Stored [created_count] memories (batch [B]/[total_batches]). [error_count] failed.

If `error_count > 0`, print one warning line per failed item using the `error` field from
its result entry.

**Partial failure handling:** `memory_store_batch` returns per-item results. A failed item in
one batch does not abort subsequent batches. Track total failures across all batches for the
Final Summary.

### Content Quality Guidance

Write content that is specific, mechanistic, and non-obvious. The purpose of these memories is to answer future questions like "how does authentication work?" or "where do I fix a field mapping bug?" — not to describe what a file contains in general terms.

**Bad — too vague to be useful:**
> "This module handles authentication."

**Good — specific, mechanistic, explains the non-obvious:**
> "The auth module implements JWT-based authentication with refresh token rotation. It uses FastAPI's dependency injection for route guards and stores hashed API keys in the `api_keys` table. The non-obvious part: token refresh is handled client-side in `lib/auth.ts`, not by the API — the API only validates and issues, never refreshes on behalf of the client."

---

**Bad:**
> "This file contains utility functions."

**Good:**
> "The `lib/transforms.ts` module normalizes API responses from snake_case to camelCase before they reach UI components. Every API response passes through `toCamelCase()` here — this is the single place to fix if a field name mapping is wrong. It also handles null coalescion for optional fields whose absence would otherwise crash component renders."

---

**Bad:**
> "The worker processes background jobs."

**Good:**
> "The ARQ worker handles three job types: embedding computation, relationship discovery, and memory expiry. Embedding jobs are queued by `memory_store` calls when `sync_embedding=false`. Relationship discovery runs as a cron job every 5 minutes and is the component most likely to cause write contention under load — check here first when the DB shows lock waits."

The `summary` field should be one sentence that is the clearest possible description of what the memory covers. Write it as if it will appear alone in a search result snippet.

### Importance Scoring

- Project overview memory: `importance=0.9`
- Module summary memories: `importance=0.8`
- Key file memories: omit the `importance` parameter (default `0.7` applies)

### Planning Announcement

Before storing the first memory, print:
```
Planning to store [N] memories across [M] modules...
```
Where `[N]` is your planned total count and `[M]` is the number of top-level modules/services. This establishes the denominator used in progress reporting. Do not revise `[N]` mid-run.

### Partial Failure Handling

`memory_store_batch` returns a `results` list with one entry per input memory. Each entry has:
- `success` (bool) — whether the memory was created
- `memory_id` (string, if success=true) — the created memory's ID
- `error` (string, if success=false) — the failure reason

For each failed item, print one line:

    Warning: failed to store memory for [title] — [error]

Do not abort the run on item failures. Track the failure count for the Final Summary.

### Progress Reporting

Progress is reported after each `memory_store_batch` call completes, not after individual stores. See the **Progress reporting** note under the `memory_store_batch` Parameter Reference above for the exact output format.

---

## Phase 4 — Code Graph Seeding

After the last `memory_store_batch` call in Phase 3, run the code graph seed step.

**Step 1: Announce.**

Print:
```
Seeding code graph...
```

**Step 2: Run the seed script.**

Use the Bash tool to invoke:
```
timeout 120 ${CLAUDE_PLUGIN_ROOT}/.venv/bin/python ${CLAUDE_PLUGIN_ROOT}/scripts/seed_code_graph.py --workspace-root "{cwd}"
```

Replace `{cwd}` with the actual working directory path. Use `${CLAUDE_PLUGIN_ROOT}` literally — do not use Glob to discover the script path.

Capture the single structured status line printed to stdout.

**Step 3: Interpret the result.**

- Exit code 0, status line begins with `code-graph: seeded` → success. Record the status line for the Final Summary.
- Exit code 0, status line begins with `code-graph: skipped` → graceful skip. Do not include a code graph line in the Final Summary and do not warn the user.
- Non-zero exit code, or status line begins with `code-graph: failed` → record the status line for the Final Summary and continue to Final Summary. Do not abort init.

---

### Final Summary

After Phase 4 completes, print exactly this format:

```
Init complete.
  Memories stored: [N]/[attempted] ([failures] failed)
    Project overview: 1
    Module summaries: [N]
    Key file memories: [N]
  Code graph: [seeded (N files, M symbols) | failed — [error]]
  Coverage: [bulleted list of top-level modules/services covered]
  Semantic search will be available shortly as embeddings are computed in the background.
  Try asking: "How does [primary subsystem] work?"
```

The `Code graph:` line is ONLY included when seeding was attempted and not skipped — that is, when codeweaver was installed and the script ran. If the status was `skipped`, omit that line entirely. When the status was `failed`, show the reason extracted from the status line after the dash (e.g., `failed — HTTP 500`).

Do NOT print the literal text `[primary subsystem]`. Replace it with the actual name of the most important or complex subsystem you identified during the crawl — make the example query actually useful for this specific codebase. The "Try asking" prompt is how users invoke memory search in Claude Code — by asking a natural language question.
