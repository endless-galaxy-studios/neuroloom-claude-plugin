---
name: neuroloom:workspace
description: Inventory and change this project's Neuroloom workspace routing — set, change, clear, or adopt the .mcp.json connection this plugin manages.
---

<!--
D172 Phase 4 design notes (not printed to the user — orchestration
context for whoever maintains this file next):

- M-16: "repair" is NOT a distinct menu action. FR-4's original action set
  (set/change/clear/adopt/repair) is narrowed to set/change/clear/adopt.
  Repair's intent is satisfied two ways: (a) re-running this command at
  any time re-offers the main menu, which itself re-reconciles nothing
  extra beyond what set/change/clear/adopt already do; (b) every
  automatic session-start pass (session_start.py Step 8, Phase 3) already
  performs an unconditional reconcile-and-report for owned entries. A
  human who wants to "just fix it" either re-runs this command (which
  re-inventories and shows the current, possibly-already-converged state
  at S4) or simply starts a new session.

- Main-menu numbering: the literal template in the spec (§6) lists five
  fixed slots — 1 Set, 2 Change, 3 Clear, 4 Adopt, 5 Leave-as-is — with a
  footnote that Set/Change are "never both simultaneously" shown. The
  spec's own manual-test scenario 7 pins Clear to literal "menu option 3"
  regardless of whether Set or Change is showing, and the S7-suppressed
  copy is the ONE place the spec explicitly calls for closing a gap
  ("item 4 is omitted, remaining items renumbered with no gap"). Reading
  those two facts together: this file uses FIXED numbering (1=Set-or-
  Change [only one ever prints, at slot 1], 2=Clear, 3=Adopt [only when
  offered], 4=Leave-as-is) as the DEFAULT, moving Clear to slot 2 so
  Set/Change never leaves a numbered gap before it in the common case —
  this differs from a literal reading of the spec's 5-slot template but
  satisfies the explicit "never both simultaneously" instruction without
  requiring an unstated general gap-closing rule. The ONE place gaps are
  explicitly closed per spec is the S7-suppressed variant (adopt item
  dropped, Leave-as-is renumbered) — implemented below exactly as written.
  See this phase's handoff notes for the full reasoning; this is a
  judgment call on genuinely ambiguous spec prose, not a literal-text
  requirement.

- NFR-Arch-2: this file calls only public functions in
  pyhooks/workspace_config.py (and pyhooks/config.py for key resolution).
  No inline SQL and no direct .mcp.json/.claude/settings.json parsing
  appear anywhere below, except the one pre-existing, out-of-scope
  api_key `INSERT OR REPLACE` pattern reused verbatim from
  skills/init/SKILL.md's Step 3 (a named exemption, not new surface).

- Workspace *names* are resolved, where verbatim copy calls for one, via
  `verify_target_workspace_exists` (the existing GET /workspaces/ listing
  endpoint) — this deliverable adds no new API endpoint (FR-10/§5's
  "Verified constraint").
-->

You are executing the Neuroloom workspace-routing command. This project's `.mcp.json` "neuroloom" connection routes every MCP tool call to a specific workspace; this command inventories that routing, lets the human set/change/clear it, and — where an entry exists that this plugin didn't create — offers explicit-consent adoption. Every state-changing step ends with the restart-honesty disclosure (below) before returning to the main menu or exiting.

Run all Bash commands from the project root (`Path.cwd()` in the snippets below is the directory the user launched `claude` from).

---

## Phase 0 — Establish a Resolvable Key (FR-2, FR-3)

**Step 1: Check whether this project has its own dedicated key.**

```bash
${CLAUDE_PLUGIN_ROOT}/.venv/bin/python -c "
import pyhooks.config as _config
cfg = _config.load()
print('KEY_SET' if cfg.api_key_source == 'project' else 'KEY_MISSING')
"
```

- `KEY_MISSING` → go to **Step 2 (keyless-first flow, FR-3)**.
- `KEY_SET` → go to **Step 3 (resolvability check on the stored key)**.

### Step 2 — Keyless-first flow (reuses D171's existing copy verbatim, unchanged)

Determine whether a global seed key is on file and how it resolves — identical check to `skills/init/SKILL.md`'s Phase 0 Step 2:

```bash
${CLAUDE_PLUGIN_ROOT}/.venv/bin/python -c "
import pyhooks.config as _config
from pyhooks.workspace_config import fetch_workspace_id_from_api, WorkspaceFetchStatus

seed_key = _config.load_global_seed_key()
if not seed_key:
    print('NO_SEED')
else:
    result = fetch_workspace_id_from_api('https://api.neuroloom.dev', seed_key)
    if result.status == WorkspaceFetchStatus.RESOLVED:
        print(f'RESOLVED:{result.workspace_id}')
    elif result.status == WorkspaceFetchStatus.AMBIGUOUS:
        print('AMBIGUOUS')
    else:
        print('UNREACHABLE')
"
```

**If the output is `RESOLVED:{workspace_id}`**, present (verbatim, unchanged from D171):

```
No per-project key configured for this project yet.

Your Claude Code plugin has a default (global) API key on file, which
resolves to workspace: {workspace_id}
(look up the workspace name at https://app.neuroloom.dev)

- Say "use default" (or similar) if that's the workspace this
  project's memories should go to — I'll copy that key in as this
  project's own dedicated key.
- If this project should go to a DIFFERENT workspace, get a key scoped
  to that workspace at https://app.neuroloom.dev/api-keys and
  paste it here.
- Say "skip" (or similar) to leave this project without a dedicated
  key for now — memory capture will stay OFF for this project until
  you run /neuroloom:init again and choose a key.
```

**If the output is `NO_SEED`, `AMBIGUOUS`, or `UNREACHABLE`**, present (verbatim, unchanged from D171):

```
No per-project key configured for this project yet, and no default
(global) key is on file either.

- Get a key at https://app.neuroloom.dev/api-keys and paste it
  here, or
- Say "skip" (or similar) to leave this project without a dedicated key
  for now — memory capture will stay OFF for this project until you run
  /neuroloom:init again and choose a key.
```

**Interpreting the response:**

- `"use default"` (or similar, RESOLVED path only) → store the seed key exactly as `skills/init/SKILL.md`'s Step 3 "Use-default" variant does (reuse that snippet verbatim — `pyhooks.config.load_global_seed_key()` + `pyhooks.db.open_db()` + `INSERT OR REPLACE INTO config`, the pre-existing, out-of-scope exemption to the no-inline-SQL rule). On `ok` → go to Step 1 again (re-check key presence; the key is now present). On `failed`/`failed:no_seed` → print the matching init-Step-3 message and return to this step's prompt.
- A pasted key → store it via `skills/init/SKILL.md`'s Step 3 "Paste-path" variant (same `_NL_KEY` env-var pattern — never echo the key). On `ok` → go to Step 1 again. On `failed` → print the matching message and return to this prompt.
- `"skip"` (or similar) → print (verbatim, unchanged from D171): *"Skipping — memory capture will stay off for this project until you run /neuroloom:init again. This doesn't remove any workspace connection you already have; setting up a new one, or changing an existing one, will wait until you have a dedicated key. Continuing to check that now."* Then **exit the command** — zero workspace-routing writes occur, per FR-3.

### Step 3 — Resolvability check on the stored key (FR-2)

```bash
${CLAUDE_PLUGIN_ROOT}/.venv/bin/python -c "
from pathlib import Path
import pyhooks.config as _config
from pyhooks.workspace_config import check_resolvability, Resolvability

cfg = _config.load()
resolvability, workspace_id = check_resolvability(cfg.api_base, cfg.api_key)
if resolvability == Resolvability.RESOLVED:
    print(f'resolved:{workspace_id}')
else:
    print('unreachable')
"
```

- `resolved:{workspace_id}` → go to **Phase 1**.
- `unreachable` → go to **Phase 0b (R2-7 — stored key not resolving)**. This is distinct from Step 2's keyless flow: a key IS present here, it just isn't working right now.

---

## Phase 0b — R2-7: stored key not resolving

Present (verbatim, R2-7):

```
No changes have been made yet. This project has a stored API key, but it isn't resolving to a workspace right now — either the key itself doesn't work, or the Neuroloom API is temporarily unreachable (retrying only helps with the second case). [Replace the key / Retry once / Cancel]
```

- **Cancel** → exit the command, zero writes.
- **Retry once** → re-run Phase 0 Step 3's resolvability check exactly once.
  - Resolves now → go to Phase 1.
  - Fails again → re-show this prompt with only **[Replace the key / Cancel]** — retry is exhausted, not offered a second time.
- **Replace the key** → present (verbatim, R2-7):

  ```
  This project's stored key doesn't work. Get a new key at https://app.neuroloom.dev/api-keys and paste it here, or say "skip" (or similar) to leave the old (non-working) key in place for now — workspace routing will stay unavailable until you provide a working one.
  ```

  If `pyhooks.config.load_global_seed_key()` resolves to a non-empty value, additionally offer **"use default"** as an alternative to pasting — framed explicitly as *replacing* the current non-working project key, not first-time setup (do not reuse Phase 0 Step 2's first-time-setup framing here).

  - A pasted key, or "use default" → store it (same `INSERT OR REPLACE`/use-default patterns as Phase 0 Step 2) and re-run the resolvability check once.
    - Resolves → go to Phase 1.
    - Still fails → re-show the reduced **[Replace the key / Cancel]** prompt (retry is exhausted at this point too — do not loop the Replace sub-flow more than once per command invocation).
  - **"skip"** (or similar) → **exit the command entirely** (not a drop-through to the main menu), restating the warning as the final line: *"Skipping. This project's stored key still doesn't work — workspace routing will stay unavailable until you run /neuroloom:workspace again with a working key."*

---

## Phase 1 — Inventory & Settings Migration (FR-1, FR-8)

**Step 1: Full inventory (single call, FR-1).**

```bash
${CLAUDE_PLUGIN_ROOT}/.venv/bin/python -c "
from pathlib import Path
import pyhooks.config as _config
from pyhooks.workspace_config import (
    inventory_workspace_state, should_resurface_adoption_offer, compare_desired_vs_observed,
    RoutingComparison, Ownership, ObservedKind,
)

cfg = _config.load()
project_root = Path.cwd()
db_path = project_root / '.neuroloom.db'
inv = inventory_workspace_state(str(project_root), db_path, cfg.api_base, cfg.api_key)

print('desired_kind:' + inv.desired.kind.name)
print('desired_value:' + (inv.desired.value or ''))
print('ownership:' + inv.ownership.name)
if inv.observed is None:
    print('observed_error:' + (inv.observed_error or 'unknown'))
else:
    print('observed_kind:' + inv.observed.kind.name)
    print('observed_value:' + (inv.observed.value or ''))
    # Phase 2 Step 2's S7/S7-suppressed branch needs this flag — the
    # pinned WorkspaceInventory shape doesn't carry it, so it is computed
    # here, once, off the same `inv.observed` the rest of Phase 2 uses.
    if inv.ownership is Ownership.UNOWNED and inv.observed.kind is not ObservedKind.ABSENT:
        print('resurface_adoption_offer:' + str(should_resurface_adoption_offer(db_path, inv.observed)))
    if inv.ownership is Ownership.OWNED and inv.observed.kind is not ObservedKind.ABSENT:
        # Phase 2 Step 3's convergence check — call the existing,
        # network-free comparison function directly instead of
        # restating its logic in prose. (Re-reads project_root/db_path
        # internally; cheap, local-file-only, and keeps this the one
        # place the drift/converged distinction is computed.)
        comparison = compare_desired_vs_observed(str(project_root), db_path)
        print('converged:' + str(comparison.comparison is RoutingComparison.CONVERGED))
print('legacy_value:' + (inv.settings_json_legacy_value or ''))
print('migration_done:' + str(inv.settings_json_migration_done))
print('resolvability:' + inv.resolvability.name)
"
```

If `observed_error` is present, `.mcp.json` is malformed or structurally conflicting (`McpJsonConflictError`, NFR-Sec-5 fail-closed). Present: *"This project's .mcp.json couldn't be read: {observed_error}. No changes will be made until this is fixed — check the file for syntax errors or a conflicting 'neuroloom' entry, or restore it from a backup, then run /neuroloom:workspace again."* Exit the command, zero writes. (This branch is not one of §6's named states — it is the orchestration-level fail-closed guard NFR-Sec-5 requires whenever `read_observed_state` cannot answer safely.)

Otherwise, hold the inventory result and proceed to Step 2.

**Step 2: Settings.json migration disclosure (S9), only if `legacy_value` is non-empty AND `migration_done` is `False`.**

Present (verbatim, S9 — substitute `<uuid>` with `legacy_value`):

```
This project has an older-style workspace setting in .claude/settings.json: <uuid>. Newer plugin versions store this differently. Migrate it now? [yes / no]
```

- **"yes"** → run migration (note: `migrate_settings_json_override` itself only re-checks the *key's* general resolvability — `check_resolvability` — not whether `legacy_value` specifically still exists; per that function's own docstring, layering the specific-workspace existence check on top for the MIGRATE-FAILED reason string is this phase's job, done below when the outcome is `VERIFY_FAILED`):

  ```bash
  ${CLAUDE_PLUGIN_ROOT}/.venv/bin/python -c "
  from pathlib import Path
  import pyhooks.config as _config
  from pyhooks.workspace_config import migrate_settings_json_override, MigrationOutcome, verify_target_workspace_exists, WorkspaceExistenceStatus

  cfg = _config.load()
  project_root = Path.cwd()
  db_path = project_root / '.neuroloom.db'
  legacy_value = '{legacy_value}'  # the exact value already read and shown in Phase 1 Step 1/2's S9 prompt
  result = migrate_settings_json_override(str(project_root), db_path, cfg.api_base, cfg.api_key)
  print('outcome:' + result.outcome.name)
  print('workspace_id:' + (result.workspace_id or ''))
  if result.outcome == MigrationOutcome.MIGRATED and result.workspace_id:
      existence = verify_target_workspace_exists(cfg.api_base, cfg.api_key, result.workspace_id)
      print('existence:' + existence.status.name)
      print('name:' + (existence.name or ''))
  elif result.outcome == MigrationOutcome.VERIFY_FAILED:
      # Layer the specific-workspace existence check on top (see note above)
      # only when the key itself DOES resolve right now — if the key
      # itself doesn't resolve, checking the specific workspace's
      # existence would need a different, unauthenticated call this
      # module doesn't have; report 'API unreachable' in that case rather
      # than attempt a call that would itself just fail the same way.
      from pyhooks.workspace_config import check_resolvability, Resolvability
      resolvability, _ = check_resolvability(cfg.api_base, cfg.api_key)
      if resolvability == Resolvability.RESOLVED:
          existence = verify_target_workspace_exists(cfg.api_base, cfg.api_key, legacy_value)
          print('existence:' + existence.status.name)
      else:
          print('existence:UNREACHABLE')
  "
  ```

  - `MIGRATED` → present (verbatim, MIGRATE-SUCCESS — `<workspace name>` from the `existence` lookup above, falling back to the bare uuid if the lookup didn't return `FOUND`): *"Migrated. Desired workspace is now <workspace name> (<uuid>), stored the current way. Removed the old setting from .claude/settings.json."* Then proceed directly into the restart-honesty disclosure (below) — this is a completed set/change, no further confirmation needed. Re-run Phase 1 Step 1's inventory (the desired state has changed) and continue to Phase 2.
  - `VERIFY_FAILED` → present (verbatim, MIGRATE-FAILED — reason is "the workspace no longer exists" when the `existence` value above is `NOT_FOUND` or `SCAN_TRUNCATED`, otherwise "couldn't reach the Neuroloom API" when it's `UNREACHABLE`): *"Found <uuid> in .claude/settings.json, but couldn't verify it — [the workspace no longer exists | couldn't reach the Neuroloom API]. Leaving .claude/settings.json untouched. Want to set a workspace directly instead? [set / clear / not now]"*
    - `set` → go to **Phase 3** directly.
    - `clear` → go to **Phase 4** directly.
    - `not now` → continue to Phase 2 with the legacy value still un-migrated (it will be re-disclosed next run, since the migration-done marker was not set on `VERIFY_FAILED`).
  - `ALREADY_DONE` / `NOTHING_TO_MIGRATE` → should not occur here (Step 2's own gate already checked `migration_done`/`legacy_value`), but if seen, continue silently to Phase 2.

- **"no"** → call:

  ```bash
  ${CLAUDE_PLUGIN_ROOT}/.venv/bin/python -c "
  from pathlib import Path
  from pyhooks.workspace_config import decline_settings_json_migration
  decline_settings_json_migration(Path.cwd() / '.neuroloom.db')
  "
  ```

  Present (verbatim, S9 "no"): *"Left as-is. This project's workspace routing will use its current default for now — run /neuroloom:workspace again anytime to migrate or set it directly."* Continue to Phase 2 using the inventory already read in Step 1 (the legacy value is now marked handled and will not be re-disclosed).

If `legacy_value` is empty, or `migration_done` is already `True`, skip Step 2 entirely and go straight to Phase 2.

---

## Phase 2 — Present State & Main Menu (S3, S4, S6, S7, S7-suppressed)

Using the (possibly refreshed) inventory, determine which named state applies. Check in this exact order — the convergence check (step 3) must run *before* branching on `desired_kind == UNSET` (step 4), because an owned entry can carry a stray header even when desired state has never been explicitly set (e.g. a pre-D172 entry, or a partial prior write) — that combination is drift needing repair, not "never decided," and routing it to S3 without repairing first would silently leave a headered entry behind an "auto-resolve" message that isn't true yet:

1. `observed_kind == ABSENT` → **S6**. Present (verbatim): *"This project has no Neuroloom MCP connection configured yet."*
2. `observed_kind != ABSENT` and `ownership == UNOWNED` → an unowned entry exists. Use the `resurface_adoption_offer` value Phase 1 Step 1's inventory snippet already printed for this case (`should_resurface_adoption_offer(db_path, observed)`, called there against the exact `ObservedState` just read — not re-derived here).
   - `True` → **S7**. Present (verbatim, substituting `[none | <uuid>]` from `observed_value`): *"This project's .mcp.json has a "neuroloom" connection this plugin didn't create — it will not be modified automatically. Current header: [none | <uuid>]. Choose "Adopt the existing entry" below if you want this plugin to manage it."*
   - `False` → **S7-suppressed**. Present (verbatim): *"This project's .mcp.json has a "neuroloom" connection this plugin didn't create — it will not be modified automatically. Current header: [none | <uuid>]. You previously chose to leave this unowned, and nothing about it has changed since. (Say "reconsider adopting" (or similar) any time to see the adoption options again — you'll be shown its current state before anything changes.)"*
3. `observed_kind != ABSENT` and `ownership == OWNED` → use the `converged` value Phase 1 Step 1's inventory snippet already printed for this case (a direct call to `compare_desired_vs_observed(project_root, db_path)`, the existing network-free comparison function — not re-derived in prose here, and it additionally handles the ABSENT case correctly, which does not arise in this branch since step 1 already routed ABSENT to S6).
   - **Not matching (drift)** → **S5, command-path**, regardless of `desired_kind` (including `UNSET` — an owned entry can carry a stray header with no desired value ever recorded, and that still needs repairing before any "never decided" message is accurate). Call `reconcile(project_root, db_path, desired)` immediately (this is the auto-repair — the same call `session_start.py`'s Step 8 makes on the automatic axis, just invoked here because a human is running the command mid-drift). On success, present (verbatim, informational — only when `desired_kind == SET_TO_VALUE`; for `UNSET`/`EXPLICITLY_CLEARED` drift, state plainly instead: *"This project's .mcp.json had a leftover workspace header with no matching decision on file — cleared it."*): *"This project's .mcp.json entry didn't match its desired workspace — fixed it to match. (Desired: <workspace name>.)"* Then re-evaluate step 3 (it is now converged) and continue to the matching branch below.
   - **Matching, and `desired_kind == UNSET`** → **S3**. Present (verbatim): *"No workspace decision has been made for this project yet. Right now it will auto-resolve your workspace when you connect (this works silently if you belong to exactly one Neuroloom workspace, and asks you to pick one if you belong to more than one)."*
   - **Matching, and `desired_kind != UNSET`** → **S4**. Resolve the desired workspace's name (if `desired_kind == SET_TO_VALUE`) via `verify_target_workspace_exists`, falling back to the bare uuid if the lookup doesn't return `FOUND`. Present (verbatim): *"This project's workspace routing is already up to date. Desired: [none — auto-resolve | <workspace name> (<uuid>)]. Nothing to change."*

**Main menu** (numbering per this file's own header-comment design note — Set/Change never both shown, Clear fixed at slot 2, Adopt at slot 3 only when offered, Leave-as-is renumbered to slot 3 or 4 to close the resulting gap):

```
Current workspace routing for this project:
  Desired:        [not yet decided | auto-resolve (no override) | <workspace name> (<uuid>)]
  .mcp.json entry: [none | headerless, owned | header=<uuid>, owned | header=<uuid>, NOT owned by this plugin]
  Legacy setting:  [none found | <uuid> — not yet migrated]

What would you like to do?
  1. Set a workspace for this project             [shown when no workspace is currently desired, or the override was explicitly cleared]
  1. Change the workspace this project uses        [shown instead of the above when a workspace is already desired]
  2. Clear the override (let this project auto-resolve its workspace)
  3. Adopt the existing .mcp.json entry so this plugin can manage it   [shown only when an unowned entry exists AND the adoption offer is not currently suppressed — S7, not S7-suppressed]
  4. Leave everything as-is
```

Render only the applicable lines from the template above, sequentially numbered starting at 1 with no gaps (i.e., if item 3 does not apply this run, "Leave everything as-is" prints as item 3, not item 4).

Exception, per C-6/MINOR k: in **S7-suppressed**, item 4's slot is never rendered at all (there is no "Adopt" line to omit-with-a-gap, since S7-suppressed's own message already told the human how to resurface it) — "Leave everything as-is" renders at whatever the next sequential number is, same gapless rule as above. Additionally, in S7-suppressed, listen for **"reconsider adopting" (or similar)** as an out-of-band input at any point this menu is shown: on hearing it, re-read the project's current `.mcp.json` state fresh (re-run Phase 1 Step 1's inventory — never reuse the state already held in memory) and go to **Phase 5 (S8)** using that freshly-read state.

**Choice handling:**

- **Set / Change** → go to **Phase 3**.
- **Clear** → go to **Phase 4**.
- **Adopt** → go to **Phase 5**.
- **Leave everything as-is** → exit the command, zero writes.

---

## Phase 3 — Set / Change Flow

Prompt: *"Enter the workspace ID (uuid) this project should route to."*

On receiving a candidate `target_workspace_id`, verify it exists and resolve its name. Pass `target_workspace_id` via the `_NL_TARGET_WORKSPACE_ID` environment variable when invoking this snippet (same convention as `skills/init/SKILL.md`'s `_NL_KEY` pattern) rather than interpolating raw human input into the `python -c` payload string:

```bash
${CLAUDE_PLUGIN_ROOT}/.venv/bin/python -c "
import os
import pyhooks.config as _config
from pyhooks.workspace_config import verify_target_workspace_exists, WorkspaceExistenceStatus

cfg = _config.load()
result = verify_target_workspace_exists(cfg.api_base, cfg.api_key, os.environ['_NL_TARGET_WORKSPACE_ID'])
print('status:' + result.status.name)
print('name:' + (result.name or ''))
"
```

- `FOUND` → go to the **write step** below.
- `NOT_FOUND` → present: *"That workspace ID wasn't found (checked all pages up to 1,000 workspaces). Double-check the ID, or try a different one. [Try again / Cancel]"* — Try again re-prompts for a target id; Cancel returns to the main menu with zero writes.
- `SCAN_TRUNCATED` → present (verbatim, MINOR d): *"Couldn't confirm that workspace — checked up to 1,000 workspaces (5 pages) without finding it. This is different from confirming it doesn't exist; it may exist further in the list. [Cancel — no changes made]"* — Cancel-only, no Retry (truncation is deterministic — see this state's own docstring in `verify_target_workspace_exists`). Returns to the main menu with zero writes.
- `UNREACHABLE` → present (verbatim, S10): *"Couldn't reach the Neuroloom API to verify that workspace. No changes were made. [Retry / cancel]"* — Retry re-runs this exact verify call; cancel returns to the main menu with zero writes. (S11's copy — *"Got an unexpected response while checking that workspace. No changes were made. [Retry / cancel]"* — has no live trigger through this call, since `verify_target_workspace_exists` only ever returns FOUND/NOT_FOUND/SCAN_TRUNCATED/UNREACHABLE; it is documented here for completeness per NFR-DX-1, matching the spec's own note that S10/S11 are treated identically and S11 is defensive.)

**Write step** (on `FOUND`), again passing `target_workspace_id` via `_NL_TARGET_WORKSPACE_ID` rather than string interpolation:

```bash
${CLAUDE_PLUGIN_ROOT}/.venv/bin/python -c "
import os
from pathlib import Path
from pyhooks.workspace_config import write_desired_state, reconcile, DesiredState, DesiredKind, WriteResult

project_root = Path.cwd()
db_path = project_root / '.neuroloom.db'
target_workspace_id = os.environ['_NL_TARGET_WORKSPACE_ID']
write_desired_state(db_path, DesiredState(DesiredKind.SET_TO_VALUE, target_workspace_id))
outcome = reconcile(str(project_root), db_path, DesiredState(DesiredKind.SET_TO_VALUE, target_workspace_id))
print('decision:' + outcome.decision.name)
print('write_result:' + (outcome.write_result.name if outcome.write_result else 'none'))
print('error:' + (outcome.error or ''))
"
```

- `write_result` is `SUCCESS` (or the decision was `NO_OP` because it was already converged) → present: *"Workspace set to <workspace name> (<uuid>)."* → show the **restart-honesty disclosure** (below) → return to the main menu (re-run Phase 1 Step 1 first, so the menu reflects the new state).
- Anything else (`FAILED`, `SKIPPED_CONFLICT`, `SKIPPED_UNMANAGED`, or `outcome.error` populated) → present: *"Recorded your choice, but couldn't update .mcp.json yet ({write_result or error}) — it will be retried at your next session start. Or run /neuroloom:workspace again now to retry immediately, or check the file for a conflicting entry."* (`write_desired_state` above already persisted this choice to `.neuroloom.db` before `reconcile` ran — the desired state is saved even when the `.mcp.json` write itself fails, and every subsequent session start's automatic reconciliation pass will retry it.) Return to the main menu without the restart-honesty disclosure (the `.mcp.json` write itself did not land, even though the choice was recorded).

---

## Phase 4 — Clear Flow

Confirm: *"Clear this project's workspace override? It will auto-resolve its workspace going forward. [yes / cancel]"*

- **cancel** → return to the main menu, zero writes.
- **yes** →

  ```bash
  ${CLAUDE_PLUGIN_ROOT}/.venv/bin/python -c "
  from pathlib import Path
  from pyhooks.workspace_config import write_desired_state, reconcile, DesiredState, DesiredKind

  project_root = Path.cwd()
  db_path = project_root / '.neuroloom.db'
  write_desired_state(db_path, DesiredState(DesiredKind.EXPLICITLY_CLEARED))
  outcome = reconcile(str(project_root), db_path, DesiredState(DesiredKind.EXPLICITLY_CLEARED))
  print('decision:' + outcome.decision.name)
  print('write_result:' + (outcome.write_result.name if outcome.write_result else 'none'))
  print('error:' + (outcome.error or ''))
  "
  ```

  - `write_result` is `SUCCESS` or decision was `NO_OP` → present: *"Cleared. This project will auto-resolve its workspace going forward."* → show the **restart-honesty disclosure** → return to the main menu (re-run inventory first).
  - Otherwise → present the same "recorded your choice, but couldn't update .mcp.json yet" failure message pattern as Phase 3's write step (the clear was likewise already persisted to `.neuroloom.db` by `write_desired_state` above before `reconcile` ran), return to the main menu, no disclosure.

---

## Phase 5 — Adopt Flow (S8)

Re-read the current observed state fresh immediately before presenting this prompt (never reuse a value read earlier in this run, to keep the TOCTOU window as tight as possible). **Capture this snippet's `kind:`/`value:` output** — this is the exact state that gets shown to and confirmed by the human below; it must be threaded forward, verbatim, into whichever choice snippet the human picks. Pass it via the `_NL_CONFIRMED_KIND`/`_NL_CONFIRMED_VALUE` environment variables when invoking whichever choice snippet the human picks (same convention as `_NL_TARGET_WORKSPACE_ID`), rather than interpolating the raw, unvalidated header value into the `python -c` payload string — unlike `{legacy_value}` in Phase 1 Step 2 (which is UUID-validated before substitution), this value comes straight from `.mcp.json` and could otherwise be a code-injection vector. Do **not** re-derive it with a fresh `read_observed_state` call inside the choice snippets below — that would compare a just-read state against another just-read state taken moments later, not against what the human actually saw and approved.

```bash
${CLAUDE_PLUGIN_ROOT}/.venv/bin/python -c "
from pathlib import Path
import pyhooks.config as _config
from pyhooks.workspace_config import read_observed_state, verify_target_workspace_exists, ObservedKind

cfg = _config.load()
project_root = Path.cwd()
observed = read_observed_state(str(project_root))
print('kind:' + observed.kind.name)
print('value:' + (observed.value or ''))
if observed.kind == ObservedKind.HEADERED_WITH_VALUE:
    existence = verify_target_workspace_exists(cfg.api_base, cfg.api_key, observed.value)
    print('existence:' + existence.status.name)
    print('name:' + (existence.name or ''))
"
```

Present (verbatim template, S8):

```
This project's .mcp.json has a "neuroloom" connection this plugin didn't create.

Current routing:  header = <uuid | none>
Resolves to:       <workspace name, if resolvable | "couldn't verify — [reason]">

What would you like to do?
  1. Adopt as-is — this plugin will manage this entry going forward, keeping it exactly as it is now
  2. Adopt and change — this plugin will manage this entry and update it to a different workspace
  3. Leave unowned — no changes; this plugin will keep leaving this entry alone
```

Substitute `<uuid | none>` from `value`; substitute the "Resolves to" line from the `existence` lookup (`FOUND` → the name; `NOT_FOUND`/`SCAN_TRUNCATED`/`UNREACHABLE` → `"couldn't verify — [not found | scan truncated, checked 1,000 workspaces | API unreachable]"`) when `kind == HEADERED_WITH_VALUE`. When `kind == HEADERLESS`, show `Resolves to: (no header set — this project will auto-resolve if adopted as-is)`. No option is a default; the human must choose one explicitly (NFR-Sec-2).

**Choice 1 — Adopt as-is:**

```bash
${CLAUDE_PLUGIN_ROOT}/.venv/bin/python -c "
from pathlib import Path
from pyhooks.workspace_config import adopt_unowned_entry, confirmed_state_from_env, AdoptionChoice, AdoptionOutcome

project_root = Path.cwd()
db_path = project_root / '.neuroloom.db'
# confirmed_state_from_env() reconstructs, from _NL_CONFIRMED_KIND/
# _NL_CONFIRMED_VALUE, the exact kind/value printed by the display
# snippet above and shown to the human — NOT a fresh read. This is what
# the human actually confirmed, so adopt_unowned_entry's own internal
# re-read (immediately before its write) is compared against the real
# TOCTOU baseline instead of another point-in-time snapshot.
confirmed = confirmed_state_from_env()
result = adopt_unowned_entry(str(project_root), db_path, AdoptionChoice.ADOPT_AS_IS, confirmed)
print('outcome:' + result.outcome.name)
"
```

(Note: `adopt_unowned_entry` re-reads `.mcp.json` fresh, internally, immediately before its write, and refuses [`STALE_CONFIRMATION`] unless that fresh read still matches the `confirmed` argument above. Passing the state captured from the display snippet — rather than re-reading here — is what makes that comparison meaningful: it is checking "has anything changed since the human saw this," not "do two reads taken microseconds apart in this snippet agree with each other.")

- `ADOPTED` → present: *"Adopted. This plugin now manages this entry."* → show the **restart-honesty disclosure** only if the header was also written this call (i.e. if the adoption changed the on-disk header value — for adopt-as-is it typically did not, so the disclosure is optional here; state plainly whether anything on disk changed: *"No header value changed — only ownership was recorded."* when nothing changed) → return to main menu (re-run inventory).
- `STALE_CONFIRMATION` → present: *"This entry changed since it was shown to you a moment ago — nothing was adopted. Showing its current state now."* → re-run this Phase 5 from the top (fresh read).
- `WRITE_FAILED` → present: *"Couldn't write to .mcp.json — no changes were made. Try again."* → return to main menu.
- `NOT_APPLICABLE` → present: *"That entry no longer exists — nothing to adopt."* → return to main menu (re-run inventory; this project is now likely S6).

**Choice 2 — Adopt and change:** prompt for a target workspace id and verify it exists using the identical FOUND/NOT_FOUND/SCAN_TRUNCATED/UNREACHABLE handling as Phase 3's write step (same verbatim S10/scan-truncated copy, same `_NL_TARGET_WORKSPACE_ID` env-var passing convention). On `FOUND`, call `adopt_unowned_entry` with `AdoptionChoice.ADOPT_AND_CHANGE`, `desired_value=target_workspace_id`, and the same `confirmed_state_from_env()`-reconstructed `confirmed` argument as Choice 1 (the state captured from the display snippet at the top of this phase, threaded via `_NL_CONFIRMED_KIND`/`_NL_CONFIRMED_VALUE` — not a fresh read):

```bash
${CLAUDE_PLUGIN_ROOT}/.venv/bin/python -c "
import os
from pathlib import Path
from pyhooks.workspace_config import adopt_unowned_entry, confirmed_state_from_env, AdoptionChoice, AdoptionOutcome

project_root = Path.cwd()
db_path = project_root / '.neuroloom.db'
confirmed = confirmed_state_from_env()
target_workspace_id = os.environ['_NL_TARGET_WORKSPACE_ID']
result = adopt_unowned_entry(
    str(project_root), db_path, AdoptionChoice.ADOPT_AND_CHANGE, confirmed, desired_value=target_workspace_id
)
print('outcome:' + result.outcome.name)
"
```

Interpret `outcome` identically to Choice 1 above, except `ADOPTED` here always changed the header, so always show the restart-honesty disclosure.

**Choice 3 — Leave unowned:**

```bash
${CLAUDE_PLUGIN_ROOT}/.venv/bin/python -c "
from pathlib import Path
from pyhooks.workspace_config import adopt_unowned_entry, confirmed_state_from_env, AdoptionChoice

project_root = Path.cwd()
db_path = project_root / '.neuroloom.db'
# Same threaded state as Choice 1/2, via confirmed_state_from_env() — the
# state shown to and declined by the human, not a fresh read (same
# TOCTOU rationale).
confirmed = confirmed_state_from_env()
result = adopt_unowned_entry(str(project_root), db_path, AdoptionChoice.LEAVE_UNOWNED, confirmed)
print('outcome:' + result.outcome.name)
"
```

`DECLINED` is the only reachable outcome for this choice. Present: *"Left unowned. This won't be offered again unless this entry's state changes — say "reconsider adopting" anytime to see it again."* Return to the main menu (re-run inventory — the state now presents as S7-suppressed).

---

## Restart-Honesty Disclosure (NFR-DX-4, verbatim — shown after every SET, CHANGE, CLEAR, or ADOPT-with-header-change)

```
Saved. This takes effect on your next Claude Code session for this project — like changing a Wi-Fi password: your phone stays on the old network until it reconnects. This session keeps using its current routing until you restart. Run /neuroloom:status after restarting to confirm the new routing is live.
```
