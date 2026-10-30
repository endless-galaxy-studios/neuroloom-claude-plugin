# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.9.2] - 2026-10-29

### Fixed

- Four hooks now apply one containment rule, `pyhooks/containment.py`. Three of them forwarded whatever they were pointed at, so reading `~/.ssh/config`, `/etc/hosts` or a file in another repository sent that path to the project's shared workspace and pulled back unrelated context (`preload_context`), a `cd` into another directory sent that directory (`cache_prefetch`), and the PostToolUse event for such a Read -- including the file's full content -- was uploaded as an observation (`capture`). The fourth, `code_graph_sync`, already contained its own uploads and now uses the shared rule. What is contained, exactly:
  - `preload_context` (Read; Glob and Grep; Bash `bfs`/`ugrep`): a target outside the project produces no API call and no cache entry.
  - `cache_prefetch`: a `cd` target outside the project is not sent.
  - `capture`: PostToolUse events for Read, Write, Edit, NotebookEdit, Glob and Grep whose target is outside the project are dropped entirely -- not sent and not buffered for replay.
- **The project root and the API key must agree.** The key, the session row and the cache keys are still read from the hook process's working directory, which Claude Code moves on `cd`; the project root is `CLAUDE_PROJECT_DIR`, which does not move. After a `cd` into a sibling repository that has its own `.neuroloom.db`, a file read in the original project would have been sent with the sibling's key to the sibling's workspace. `preload_context`, `capture` and `cache_prefetch` now skip unless the directory holding `.neuroloom.db` is the project root. This also skips a `cd` into a nested project that has its own key. Where `.neuroloom.db` lives is unchanged.
- **Not contained.**
  - Bash is the main remaining channel. `capture` still records Bash events as before, because a command's output can't be attributed to a path; output from outside the project (say `cat /etc/hosts`) is still uploaded.
  - An Agent event's `tool_response` can summarise content from outside the project.
  - Every observation carries `transcript_path` and `cwd`, which include the home path.
  - WebFetch and WebSearch are not path-targeted and are unchanged.
  - The search text itself (a Grep regex, a Glob or `ugrep` term) is still sent as the query whenever the location searched is inside the project.
  - Events that an earlier version already buffered after a failed upload are still replayed as stored.
- "The project" is `CLAUDE_PROJECT_DIR`, which Claude Code sets to the directory the session started in and which does not move on `cd`. Relative targets are resolved against the `cwd` in the hook's own event, and a Glob, Grep, `bfs` or `ugrep` call that names no path (so searches where Claude currently is) requires that `cwd` to be inside the project. Without `CLAUDE_PROJECT_DIR` the root falls back to the hook process's working directory, which also moves on `cd`: for `preload_context` and `capture` as much as `cache_prefetch`, a `cd` out of the project can then no longer be told apart from staying put. Claude Code sets the variable for hooks, so this mainly affects tests and older versions; each hook records a `project_root_fallback_cwd` trace when it takes the fallback.
- Paths are resolved before they are compared -- symlinks are followed and `..` is collapsed -- so `project/../other` and a symlink inside the project that points out are outside, and `project-evil` is not mistaken for `project`. Wildcards are expanded against the real filesystem for the same reason: `src/e*/x`, `l?nk` and `lin*/secret.txt` are outside when the wildcard matches an in-project symlink that points out (more than 64 matches at any level is treated as outside). Bash and the Glob tool are treated differently where they differ: in Bash `*` skips dotfiles and a trailing `**` is just `*` (globstar is off by default), while in the Glob tool `*` includes dotfiles. Bash is handled conservatively where shells disagree: zsh recurses on `**/`, so an unquoted `**` followed by more path parts is rejected rather than modelled. A Grep `pattern` is a content regex, not a location, so only its `path` is checked.
- Constructs whose value cannot be known without running the shell are treated as outside rather than guessed at: `$VAR` and command substitution, a `~` that cannot be expanded (`~name`, `~+`, `~-`), `.*`/`.?` (which Bash before 5.2 can expand to `..`), a bracket class that can match a dot, and `..` after a wildcard. A backslash-newline is deleted before the word is judged, as Bash does (so `.`, backslash-newline, `.` is `..`), and an unquoted word starting with `=` is rejected because zsh expands `=ls` to `/bin/ls`. Quoting is tracked for Bash `bfs`/`ugrep`: nothing expands inside single quotes and only `$` and backtick inside double quotes, so a quoted regex such as `'[a-z]+'`, `'end$'` or `'\d{1,2}'` and `-name '*.{ts,tsx}'` are allowed, while the same text unquoted (`{a,/etc}`) is still skipped. A word that has a live wildcard next to a quoted or escaped glob character (`'a[b]'*`, `"a[b]"*`) is also rejected: the shell matches the quoted part literally, but the filesystem check cannot tell which characters were quoted and would read `[b]` as a character class. A Glob pattern's braces are enumerated and every alternative is checked (`{..,src}/x` yields `../x`; more than 32 alternatives, or malformed braces, is outside), and `[...]` is a single-character wildcard. Flag values (`-f/etc`, `--include=/etc/x`), redirection targets (`>/etc/x`) and unparseable commands are checked or rejected the same way; redirections to `/dev/null` and to a file descriptor (`2>/dev/null`, `2>&1`) are allowed.
- Remaining gaps: in the Glob tool `**` is never expanded (doing so against a cap would skip every repository-root `**/*.ts`), so an in-tree symlink reached through `**` is not detected; this is accepted for the Glob tool only, on the assessment that its engine does not follow symlinks (not verified), and does not apply to Bash words, where a trailing `**` is expanded as `*` and `**` followed by more path parts is rejected (zsh recurses on it). And `bfs -L` and `ugrep -R` follow symlinks inside the tree they search, though only the search term, never file content, is sent.
- Cost of failing closed: a nudge is skipped for a legitimate search that uses an expansion character unquoted, a double-quoted `$`, a Glob part that starts with a bracket class or a dot-wildcard (`.env*`), or more matches or alternatives than the caps above. On macOS's default case-insensitive filesystem a path spelled with the wrong case (`/Users/x/Proj` for `/Users/x/proj`) is also skipped; it never leaks.
- A hook event whose `cwd` cannot be resolved to an existing directory -- a symlink loop, a NUL byte, a path that does not exist or is a file -- is treated as malformed and skipped by `preload_context`, `capture` and `cache_prefetch`, rather than raising or being judged on its target alone. `capture` drops every event for it, path-targeted or not, since none of a malformed event is trusted. (On newer Pythons, observed on 3.14, a plain `resolve()` no longer raises on a symlink loop but returns the loop path, which would have passed as inside the project; the check that the directory exists and is a directory refuses it.)
- `preload_context` no longer sends the absolute project path as the `session_id` when no session row exists; it sends none unless the stored value is a well-formed session id.

### Changed

- New `pyhooks/containment.py` holds the containment rule, including `project_root()`, so `preload_context`, `cache_prefetch`, `capture` and `code_graph_sync` share one definition. `code_graph_sync` keeps the hook's working directory as its root (it is also the base codeweaver computes stored symbol paths from); its check now additionally expands `~` and rejects an unexpandable `~name`, which changes nothing for the absolute paths Write and Edit supply.
- `.claude-plugin/plugin.json`'s version bumped to `0.9.2`.

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
