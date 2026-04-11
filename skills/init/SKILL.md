---
name: neuroloom:init
description: Bootstrap workspace memory by crawling the codebase and storing structured memories for all key files and modules.
---

You are executing the Neuroloom workspace initialization protocol. Your job is to crawl this codebase, understand its structure, and store a structured set of seed memories that will make future `memory_search` calls useful from the first query. Execute the three phases below in order. Do not skip phases or reorder them.

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

Every `memory_store` call requires a `memory_type`. Use exactly one of these values:

| Content being stored | `memory_type` value |
|---|---|
| Project overview, module structure, service architecture | `architecture` |
| Coding patterns, framework conventions, design patterns | `pattern` |
| Naming conventions, style guides, code standards | `convention` |
| Config files, environment variables, build setup | `configuration` |
| External service integrations, API clients, third-party SDKs | `integration` |
| Dependency management, package.json, pyproject.toml specifics | `dependency` |
| Explicit design decisions from README, ADRs, or code comments | `decision` |
| Auth, secrets management, permission models, security patterns | `security` |
| Performance-critical code, optimization notes, caching layers | `performance` |
| Documentation files, API docs, README sections | `documentation` |
| Concepts, domain knowledge, or explanatory context | `learning` |
| Anything that does not fit the above categories | `general` |

Do not invent other values. The full set of valid `memory_type` values is: `decision`, `pattern`, `bug_fix`, `architecture`, `integration`, `configuration`, `debugging`, `refactoring`, `documentation`, `learning`, `convention`, `dependency`, `performance`, `security`, `general`, `wiki`, `sdlc_knowledge`. The table above covers the types most commonly needed during init. Do not use `document`, `file`, `code`, or any other string not in the valid set.

### `memory_store` Parameter Reference

Call `memory_store` with these parameters for every memory:

- `title` (required, string) — A specific, descriptive title. Bad: "Auth module". Good: "JWT authentication with refresh token rotation — auth module".
- `memory_type` (required, string) — One value from the mapping table above.
- `content` (required, string) — The primary narrative. See content quality guidance below.
- `summary` (optional, string) — One sentence. The clearest possible description of what this memory covers. This is prepended to content in search results, so make it count.
- `concepts` (optional, list of strings) — 3–6 labels drawn from: framework names, language names, architectural patterns (e.g., "async", "event-sourcing", "CQRS"), domain terms (e.g., "workspace", "embedding", "tenant-isolation"), module names. These drive relationship discovery between memories.
- `tags` (optional, list of strings) — ALWAYS include `"seed"` on every memory stored during this init run. Add these operational tags as appropriate: `module-summary` (for module-level memories), `project-overview` (for the single project overview memory), `entry-point` (for main/index files), `schema` (for model/schema files), `configuration` (for config files), `integration` (for third-party integration files).
- `files` (optional, list of strings) — Always include on key-file memories and module summaries. Paths relative to project root, no leading `./`. Example: `"api/routers/memories.py"`, not `"./api/routers/memories.py"`.
- `importance` (optional, float 0.0–1.0) — Use `0.9` for the project overview, `0.8` for module summaries. Omit for key-file memories (the default of `0.7` applies).
- `confidence` (optional, float 0.0–1.0) — Omit; use the default of `0.8`.
- `sync_embedding` (optional, bool) — Leave at default. Do not set this parameter.

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

If a `memory_store` call fails for any reason:
- Print one line: `Warning: failed to store memory for [title] — [error message]`
- Skip that memory and continue with the next one
- Track the failure count for the final summary

Do not abort the entire run on a single failure.

### Progress Reporting

After every 5 memories stored, print:
```
Stored [N]/[target] memories...
```

Where `[target]` is your planned total count (established before you begin storing).

---

## Phase 4 — Code Graph Seeding

After the last `memory_store` call in Phase 3, run the code graph seed step.

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
