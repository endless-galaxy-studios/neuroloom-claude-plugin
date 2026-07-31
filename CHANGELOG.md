# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
