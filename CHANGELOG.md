# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.9.0] - 2026-08-01

### Added

- New `/neuroloom:workspace` command — the sole human-driven entry point for setting, changing, clearing, or adopting a project's desired workspace. `/neuroloom:init` invokes it as well, rather than duplicating its logic. The adoption path (bringing a hand-edited `.mcp.json` entry the plugin doesn't already own under plugin management) is TOCTOU-guarded — the entry's state is re-read and re-compared immediately before the write, not trusted from an earlier read — and a decline is sticky per exact entry state: the offer is suppressed until the entry's content actually changes, via a stored content hash, not re-asked every session.

### Changed

- `.neuroloom.db`'s existing `config` key/value table now holds a **mutable desired-state** record (desired workspace, a three-valued "is desired state set" sentinel, a settings.json-migration-done marker, and the unowned-entry decline hash) instead of the prior confirm-once override. A single reconciliation routine diffs this desired state against `.mcp.json`'s observed state and converges every entry the plugin owns — including creating the baseline entry from scratch, header included, when a workspace is already desired at creation time. This replaces the ADR-15 confirm-once override model entirely; see ADR-18 (supersedes ADR-15 in full).
- Every write path — including the automatic session-start axis, not just human-driven commands — is now gated on a **resolved** per-project API key (a live-checked, working key), not merely a *present* one. A stored-but-invalid key correctly blocks writes rather than silently no-oping.
- `.claude/settings.json` is retired as a routing input. A one-time, resolvability-gated migration reads any legacy override value out of it and moves it into `.neuroloom.db`'s desired-state record; after that, `.claude/settings.json` is never read again by anything in this plugin.
- `.claude-plugin/plugin.json`'s version bumped to `0.9.0`.

### Removed

- The old `ensure_workspace_configured` / `ensure_workspace_configured_detailed` entry points, and the automatic residue-vs-override equality guessing they drove, are retired outright — not wrapped or deprecated. The now-orphaned `WORKSPACE_ID_AMBIGUOUS` sentinel (its only producer lived inside the deleted function) is removed along with every consumer and message that referenced it.

## [0.8.0] - 2026-07-30

### Changed

- The global, plugin-configured API key (`CLAUDE_PLUGIN_OPTION_API_KEY`, set via `/plugins configure neuroloom`) is no longer used to route any project's memory capture. It survives only as a **seed** — a one-time starting point `/neuroloom:init` can offer to copy into a project's own dedicated key, entirely in-process, never printed anywhere a transcript could capture it. Every hook process resolves its API key from `.neuroloom.db` (a genuine per-project key) — or not at all. See [ADR-17](../../docs/architecture/decisions/adr-17_plugin_api_key_scope_reversal.md) for the full rationale.
- `.claude-plugin/plugin.json`'s `userConfig.api_key` is now optional (`required: false`) — it was previously required, which pushed a project-scoped credential into global Keychain storage by construction.
- `/neuroloom:init`'s onboarding flow now correctly detects whether *this project* already has a dedicated API key, instead of short-circuiting past setup whenever any global key exists.

### Removed

- `NEUROLOOM_API_KEY` is dropped from `config.py`'s resolution chain entirely — it was a second, equally global-scoped credential tier this plugin's own credential-scope contract had left unexamined.
- `.claude-plugin/plugin.json`'s `workspace_id` userConfig field is removed outright (functionally inert).
- `scripts/seed_code_graph.py` no longer consults `CLAUDE_PLUGIN_OPTION_API_KEY` or `NEUROLOOM_API_KEY` — it now reads its per-project key exclusively from `_NL_KEY`, passed in via the invoking process's environment.

### Fixed

- A project with no dedicated API key now goes **dormant** (memory capture off, fail-closed, zero network I/O) with a clear, every-session message distinguishing that state from "no key configured anywhere" — instead of silently riding the account-wide global key and potentially misrouting captured observations to the wrong workspace.
- `skills/init/SKILL.md`'s Step 1 now actually checks per-project key presence (`api_key_source == "project"`), rather than the global seed's presence — the root cause of the reported cross-project misrouting incident.

### Breaking Changes

This is a documented breaking change requiring manual action on any project that previously relied on the global plugin key to route its memory capture: run `/neuroloom:init` once per project to restore capture with a dedicated key.
