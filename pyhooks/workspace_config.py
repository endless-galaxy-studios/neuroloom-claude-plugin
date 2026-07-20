"""
Workspace ID configuration for neuroloom-claude-plugin (D169).

--- Why this matters ---

The Neuroloom MCP server issues OAuth JWTs that authenticate a *user*, not a
workspace (ADR-13) — workspace selection happens per-request, via an
``X-Workspace-Id`` header or server-side auto-resolution for single-workspace
callers. When a developer works on multiple projects that each need their
own workspace, per-project routing requires a literal, non-templated header
written into that project's own MCP connection.

--- The mechanism (D169; supersedes the ``${user_config.workspace_id}``
    substitution design shipped in commit dc7226e) ---

Claude Code's ``${user_config.X}`` template substitution deliberately reads
only user-level and managed settings, never project-level ``pluginConfigs``
— a security boundary, not an oversight, so a cloned/untrusted repository
can't smuggle values into a plugin's declared configuration. Writing an
auto-fetched workspace_id into project-level ``pluginConfigs`` and expecting
substitution to pick it up therefore never worked; the header stayed an
unresolved template string on every request.

This module instead writes a **literal** ``X-Workspace-Id`` header directly
into a project-scope ``.mcp.json`` entry — but only under these conditions:

1. **Override-first, human-provenance only.** The sole trigger for a literal
   header is a validated UUID read from this project's own
   ``.claude/settings.json`` (``pluginConfigs["neuroloom@endless-galaxy-studios"]
   .options.workspace_id``) — never an auto-fetched default. An auto-fetched
   default (the workspace the caller's API key resolves to via the
   Token-authenticated bootstrap call) is cached locally for other
   bookkeeping but is never written to any file.

2. **Residue migration.** Released versions of this plugin (through the
   version shipping this fix) wrote the auto-fetched default into the same
   settings key a human override lives at. On first read after upgrading,
   if the value found there exactly matches this session's fingerprint-
   matched cached default, it's recognized as the plugin's own past write
   (not a human's choice), deleted from ``.claude/settings.json``, and
   treated as "no override" for the rest of the call.

3. **Headerless-by-default (ADR-13 conformance).** When no genuine override
   is configured, the project-scope ``.mcp.json`` entry this module ensures
   exists is left headerless — no literal value is ever guessed or pinned.
   The live MCP connection's own server-side auto-resolution then applies
   ADR-13's rule: silent, correct auto-resolve for a single-workspace
   caller; a structured error (not a silent guess) for a multi-workspace
   caller with nothing configured.

4. **Ownership-scoped, self-healing writes.** This module only ever creates,
   modifies, or blanks a ``.mcp.json`` ``neuroloom`` entry it can prove it
   created. Ownership is tracked per-project in ``.neuroloom.db`` (not as an
   in-entry marker — see "Ownership marker design" below) and stamped only
   at the moment this module creates a brand-new entry, never added
   retroactively to an existing entry that merely happens to match the
   expected ``url``/``type``. An entry matching shape but lacking this
   module's ownership record is left completely untouched, headers
   included, and the writer reports ``WriteResult.SKIPPED_UNMANAGED`` so
   callers never mistake "left alone" for "succeeded". This is what
   protects a user's own hand-maintained or hand-worked-around ``neuroloom``
   entry from ever being adopted, modified, or blanked by this module.

--- Ownership marker design: db-tracked, not in-entry ---

D169's plan preferred an in-entry ``"managed_by": "neuroloom-plugin"`` key
alongside ``type``/``url``/``headers``, contingent on confirming Claude
Code's ``.mcp.json`` schema tolerates an unrecognized key inside an
``mcpServers`` entry. That tolerance could not be confirmed during this
implementation (no Context7/live-host verification channel was available).
Per the plan's own documented fallback, ownership is instead tracked as a
boolean flag in this project's ``.neuroloom.db`` config table
(``_MCP_ENTRY_OWNED_DB_KEY``), set only at entry-creation time. See ADR-14
for the full risk-asymmetry rationale: an unverified in-entry key risks a
silent, fleet-wide connection outage if the host schema turns out to reject
it (this module would report ``SUCCESS`` — the JSON write itself succeeds —
while Claude Code silently fails to parse the file downstream, with no
feedback channel back to this module). The db-tracked fallback's worst case
is local and recoverable: a wiped ``.neuroloom.db`` loses the ownership
record, so this module stops self-healing its own entry and (under Branch C)
may emit a spurious "connection missing" warning — visible and degraded,
never a silent host-level rejection.

--- What this module does ---

1. Calls GET /api/v1/workspaces/mine/insight to resolve a default
   workspace_id for the authenticated API key (the "bootstrap" call — see
   ``_fetch_workspace_id_from_api``). This is gated on an API key being
   present; nothing else in this module is.

2. Caches that default in the .neuroloom.db config table, alongside a
   SHA-256 fingerprint of the API key it was resolved for (D167 Phase 6 /
   F14). Also used as the residue-migration comparison value (item 2 above).

3. Reads a manual per-project override from .claude/settings.json (item 1
   above) and applies the resolution order documented on
   ``ensure_workspace_configured_detailed``.

4. Writes (or ensures) a project-scope ``.mcp.json`` ``neuroloom`` entry via
   ``_write_literal_mcp_json_entry`` — merge-preserving, atomic,
   symlink-safe, idempotent, and ownership-scoped.

--- When this is called ---

session_start.py calls ensure_workspace_configured()/
ensure_workspace_configured_detailed() after successfully starting a
session. The bootstrap fetch is skipped whenever a cached default is
present and its fingerprint still matches the current API key, so most
sessions incur zero network cost; the override read and the .mcp.json write
path never need a network call at all.
"""

import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import urllib.error
import urllib.request
import uuid
from enum import Enum
from pathlib import Path
from typing import Any, NamedTuple

logger = logging.getLogger(__name__)

_WORKSPACE_ID_KEY = "workspace_id"

# F14 (D167 Phase 6): SHA-256 of the API key the cached workspace_id was
# resolved for. Mirrors the MCP server's own token-hash-caching pattern —
# the raw key is never stored or logged, only its digest. Compared against a
# freshly computed fingerprint on every session start so a rotated API key
# (which may now belong to a different workspace) invalidates the cache
# instead of the plugin trusting a stale workspace_id forever. Also used
# (D169 / C1) as the comparison value that distinguishes a released
# version's own residue from a genuine human override.
_WORKSPACE_ID_FINGERPRINT_KEY = "workspace_id_key_fingerprint"

# D169 (C4 fallback — see module docstring "Ownership marker design"):
# per-project flag in .neuroloom.db recording that this module itself
# created the current project's .mcp.json "neuroloom" entry. Set only at
# entry-creation time inside _write_literal_mcp_json_entry — never added
# retroactively to an entry this module did not create.
_MCP_ENTRY_OWNED_DB_KEY = "mcp_json_neuroloom_entry_owned"
_MCP_ENTRY_OWNED_DB_VALUE = "1"

# F15: sentinel returned by ensure_workspace_configured when the API key
# belongs to a caller with 2+ workspace memberships and no workspace_id can
# be auto-resolved (the new `workspace_not_specified` 400). Distinct from
# None, which continues to mean "not resolved — stay silent, retry later."
# Not a valid workspace_id shape (workspace_ids are UUIDs), so it can never
# collide with a real resolved value.
#
# D169: confirmed unreachable via this module's Token-authenticated
# bootstrap call — GET /workspaces/mine/insight resolves the workspace from
# a direct, non-nullable API-key-to-workspace join
# (api/neuroloom_api/auth/dependencies.py:347-399), structurally incapable
# of ever being ambiguous regardless of how many workspaces the underlying
# user account belongs to. Only the separate Bearer/OAuth-authenticated path
# used by the live MCP connection can raise `workspace_not_specified`. This
# wiring is retained as defensive dead code only — in case the API's auth
# dispatch for this specific bootstrap call changes in the future — and is
# not given any new live trigger by this deliverable.
WORKSPACE_ID_AMBIGUOUS = "__ambiguous__"

_INSIGHT_PATH = "/api/v1/workspaces/mine/insight"

_FETCH_TIMEOUT = 5.0

_PLUGIN_ID = "neuroloom@endless-galaxy-studios"

# The MCP server entry this module reads/writes in the project's .mcp.json.
_MCP_SERVER_NAME = "neuroloom"
_MCP_SERVER_TYPE = "http"
_MCP_SERVER_URL = "https://mcp.neuroloom.dev/mcp"
_WORKSPACE_HEADER = "X-Workspace-Id"

# Prefix for the atomic-write temp file (C5). Random-suffixed by
# tempfile.mkstemp — never a fixed, predictable name — so an attacker
# cannot pre-plant a symlink at this path and have a write silently follow
# it. Also used to identify and sweep orphaned temp files left behind by a
# crashed prior write attempt.
_MCP_JSON_TMP_PREFIX = ".mcp.json.neuroloom-tmp-"

# Prefix for the atomic-write temp file used when rewriting
# .claude/settings.json (review fix following C5 — see
# _delete_manual_workspace_override_key). Kept in its own namespace, distinct
# from _MCP_JSON_TMP_PREFIX, so the two writers' orphan sweeps never
# cross-match each other's leftovers.
_SETTINGS_JSON_TMP_PREFIX = ".settings.json.neuroloom-tmp-"


class WorkspaceFetchStatus(Enum):
    """Outcome of a call to ``_fetch_workspace_id_from_api``."""

    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    UNREACHABLE = "unreachable"


class WorkspaceFetchResult(NamedTuple):
    """
    Widened result of a workspace-insight fetch (F15).

    ``status`` distinguishes three outcomes that used to all collapse to
    ``None``: a resolved workspace_id, a structurally ambiguous caller
    (2+ memberships, nothing to auto-pick), and "unreachable" — a bucket
    that covers genuine network failures AND any error-body shape this
    parser doesn't recognize (including the pre-D167-Phase-1 flat
    ``{"detail": ...}`` shape, which may still be served during a
    deploy-skew window). Unreachable is deliberately non-actionable —
    the caller stays silent and retries next session, same as before.
    """

    status: WorkspaceFetchStatus
    workspace_id: str | None = None


class WriteResult(Enum):
    """
    Outcome of ``_write_literal_mcp_json_entry`` (D169).

    SUCCESS: the target state (headered or headerless) is now on disk —
        either this call wrote it, or it already matched (idempotent skip).
    SKIPPED_CONFLICT: some structural expectation wasn't met — the
        top-level ``mcpServers`` value, the ``neuroloom`` entry itself, or
        its ``headers`` value isn't the dict shape expected, or an existing
        ``neuroloom`` entry's ``url``/``type`` doesn't match what this
        module would have created. The file is left completely untouched.
    SKIPPED_UNMANAGED: a ``neuroloom`` entry exists with matching
        ``url``/``type`` but this module has no record of having created
        it (C4). The file is left completely untouched, headers included —
        this is what protects a user's own hand-maintained entry.
    FAILED: an I/O or top-level parse error occurred. The file is left
        untouched (or, if a write was in flight, the original file is
        unaffected — the atomic rename never happens on failure).
    """

    SUCCESS = "success"
    SKIPPED_CONFLICT = "skipped_conflict"
    SKIPPED_UNMANAGED = "skipped_unmanaged"
    FAILED = "failed"


class WorkspaceConfigResult(NamedTuple):
    """
    Full outcome of ``ensure_workspace_configured_detailed`` (D169).

    ``workspace_id`` preserves the pre-D169 return domain (an actual
    workspace_id, the ``WORKSPACE_ID_AMBIGUOUS`` sentinel, or ``None``) so
    existing simple callers (e.g. ``skills/init/SKILL.md``'s inline script,
    via the ``ensure_workspace_configured`` wrapper below) are unaffected.

    The remaining fields exist so ``session_start.py`` can emit its
    visibility-log lines without re-deriving state this module already
    computed:

    - ``override_applied``: True only when a validated, non-residue
      override drove a ``WriteResult.SUCCESS`` literal-header write this
      call. Callers should emit the "override applied" log only when True.
    - ``migrated_residue_value``: the UUID string deleted from
      ``.claude/settings.json`` this call, because it matched the
      fingerprint-matched cached default (C1 residue migration). ``None``
      when no migration occurred this call.
    - ``baseline_write_result``: the ``WriteResult`` of the headerless
      baseline-ensure step (Branch C's "always ensure an owned, headerless
      entry exists" write), populated whenever that step ran — i.e.
      whenever no override applied. Callers should emit the C6
      connection-missing warning when this is present and not
      ``WriteResult.SUCCESS``.
    - ``override_write_result``: the ``WriteResult`` of the literal-header
      write attempted for a genuine (non-residue) override, populated
      whenever that step ran — i.e. whenever an override was present.
      Mutually exclusive with ``baseline_write_result`` (only one of the
      two branches runs per call). Review fix following C6: a non-
      ``SUCCESS`` result here previously only reached a debug-level
      ``logger.warning`` call (invisible — stderr is suppressed and no
      logging handler is configured for this hook). Callers should emit a
      distinct, user-visible warning when this is present and not
      ``WriteResult.SUCCESS``, worded to distinguish
      ``WriteResult.SKIPPED_UNMANAGED`` (an existing unmanaged
      ``.mcp.json`` entry is blocking the write) from ``WriteResult.FAILED``
      / ``WriteResult.SKIPPED_CONFLICT`` (the write itself failed or the
      file's shape didn't match).
    """

    workspace_id: str | None
    override_applied: bool = False
    migrated_residue_value: str | None = None
    baseline_write_result: WriteResult | None = None
    override_write_result: WriteResult | None = None


def _fingerprint_api_key(api_key: str) -> str:
    """SHA-256 hex digest of the API key — never store or log the raw key."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _load_config_value(db_path: Path, key: str) -> str | None:
    """
    Read a single value from the .neuroloom.db config table.

    Returns None if the file does not exist, the key has no row, or
    reading fails for any reason.
    """
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute(
            "SELECT value FROM config WHERE key = ? LIMIT 1",
            (key,),
        )
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        logger.debug("Failed to read %s from .neuroloom.db", key, exc_info=True)
        return None


def _save_config_value(db_path: Path, key: str, value: str) -> None:
    """
    Persist a single value into the config table of .neuroloom.db.

    Uses INSERT OR REPLACE so this is safe to call on every session start
    when the value changes (e.g. workspace re-assignment, key rotation).
    """
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
            (key, value),
        )
        conn.commit()
        conn.close()
    except Exception:
        logger.debug("Failed to save %s to .neuroloom.db", key, exc_info=True)


def load_workspace_id_from_db(db_path: Path) -> str | None:
    """Read the stored workspace_id from .neuroloom.db config table."""
    return _load_config_value(db_path, _WORKSPACE_ID_KEY)


def _save_workspace_id_to_db(db_path: Path, workspace_id: str) -> None:
    """Persist workspace_id into the config table of .neuroloom.db."""
    _save_config_value(db_path, _WORKSPACE_ID_KEY, workspace_id)


def _is_mcp_entry_owned(db_path: Path) -> bool:
    """
    True if this module has a record of having created the current
    project's .mcp.json "neuroloom" entry (D169 / C4 ownership check).
    """
    return _load_config_value(db_path, _MCP_ENTRY_OWNED_DB_KEY) == _MCP_ENTRY_OWNED_DB_VALUE


def _mark_mcp_entry_owned(db_path: Path) -> None:
    """
    Record that this module just created the current project's .mcp.json
    "neuroloom" entry. Called only at entry-creation time — never
    retroactively on an entry this module did not itself create.
    """
    _save_config_value(db_path, _MCP_ENTRY_OWNED_DB_KEY, _MCP_ENTRY_OWNED_DB_VALUE)


def _parse_ambiguous_error_body(raw_body: bytes) -> bool:
    """
    Return True if *raw_body* is a D167-Phase-1 workspace-resolution error
    body with ``code == "workspace_not_specified"``.

    Defensive by construction: any shape this doesn't recognize — including
    the pre-Phase-1 flat ``{"detail": ...}`` body, an empty body, or
    non-JSON content — returns False rather than raising. False here always
    means "treat as unreachable, not ambiguous"; it never raises up to the
    caller.
    """
    try:
        parsed = json.loads(raw_body.decode("utf-8"))
    except Exception:
        return False
    if not isinstance(parsed, dict):
        return False
    error = parsed.get("error")
    if not isinstance(error, dict):
        return False
    return error.get("code") == "workspace_not_specified"


def _fetch_workspace_id_from_api(api_base: str, api_key: str) -> WorkspaceFetchResult:
    """
    Call GET /api/v1/workspaces/mine/insight and classify the outcome.

    The insight endpoint resolves the workspace from the API key itself —
    the caller does not need to supply the workspace_id. This is the
    "bootstrap" call: from zero information (just an API key), discover
    which workspace this key defaults to.

    Never raises. Any failure mode this function does not specifically
    recognize as AMBIGUOUS classifies as UNREACHABLE.
    """
    url = f"{api_base}{_INSIGHT_PATH}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Token {api_key}",
            "User-Agent": "neuroloom-plugin/0.1.0",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            if resp.status != 200:
                return WorkspaceFetchResult(WorkspaceFetchStatus.UNREACHABLE)
            body = json.loads(resp.read().decode("utf-8"))
            workspace_id: str | None = body.get("workspace_id")
            if workspace_id:
                return WorkspaceFetchResult(WorkspaceFetchStatus.RESOLVED, workspace_id)
            return WorkspaceFetchResult(WorkspaceFetchStatus.UNREACHABLE)
    except urllib.error.HTTPError as exc:
        logger.debug("workspace insight fetch returned HTTP %s", exc.code)
        try:
            raw_body = exc.read()
        except Exception:
            raw_body = b""
        if exc.code == 400 and _parse_ambiguous_error_body(raw_body):
            return WorkspaceFetchResult(WorkspaceFetchStatus.AMBIGUOUS)
        return WorkspaceFetchResult(WorkspaceFetchStatus.UNREACHABLE)
    except Exception:
        logger.debug("workspace insight fetch failed", exc_info=True)
        return WorkspaceFetchResult(WorkspaceFetchStatus.UNREACHABLE)


def _read_manual_workspace_override(project_root: str) -> str | None:
    """
    Read a human-provided per-project workspace override from this
    project's .claude/settings.json, if present and well-formed.

    Reads pluginConfigs["neuroloom@endless-galaxy-studios"].options
    .workspace_id, strips surrounding whitespace, and validates the result
    as a UUID. Returns None on any missing, malformed, or non-string value
    — including a missing or unparseable settings.json, or any of the
    intermediate dict levels not being a dict. Never raises.
    """
    settings_path = Path(project_root) / ".claude" / "settings.json"
    if not settings_path.exists():
        return None
    try:
        with settings_path.open("r", encoding="utf-8") as fh:
            settings = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None

    if not isinstance(settings, dict):
        return None
    plugin_configs = settings.get("pluginConfigs")
    if not isinstance(plugin_configs, dict):
        return None
    plugin_entry = plugin_configs.get(_PLUGIN_ID)
    if not isinstance(plugin_entry, dict):
        return None
    options = plugin_entry.get("options")
    if not isinstance(options, dict):
        return None

    raw_value = options.get(_WORKSPACE_ID_KEY)
    if not isinstance(raw_value, str):
        return None
    candidate = raw_value.strip()
    if not candidate:
        return None
    try:
        uuid.UUID(candidate)
    except (ValueError, AttributeError, TypeError):
        return None
    return candidate


def _delete_manual_workspace_override_key(project_root: str) -> bool:
    """
    Delete pluginConfigs["neuroloom@endless-galaxy-studios"].options
    .workspace_id from .claude/settings.json (D169 / C1 residue migration).

    Merge-preserving: only this one key is removed; every other key in the
    file (including sibling plugin options and unrelated pluginConfigs
    entries) is left exactly as found. Used when the value found there is
    recognized as this plugin's own prior auto-write (a released-version
    artifact), not a human's deliberate override.

    Returns True on success (the key existed and was removed), False on any
    failure or if there was nothing to delete. Never raises.

    Atomic and symlink-safe (review fix following C5): a leftover temp file
    from a previous crashed write attempt is swept at the start of this
    call, and the actual write goes through ``_atomic_write_json`` — same
    ``tempfile.mkstemp`` + ``os.replace`` + permission-mode-preservation
    pattern already used for ``.mcp.json`` via
    ``_write_literal_mcp_json_entry``, in its own
    ``_SETTINGS_JSON_TMP_PREFIX`` namespace so the two writers' orphan
    sweeps never cross-match.
    """
    settings_path = Path(project_root) / ".claude" / "settings.json"
    _sweep_orphan_temp_files(str(Path(project_root) / ".claude"), _SETTINGS_JSON_TMP_PREFIX)
    try:
        if not settings_path.exists():
            return False
        with settings_path.open("r", encoding="utf-8") as fh:
            settings = json.load(fh)
        if not isinstance(settings, dict):
            return False
        plugin_configs = settings.get("pluginConfigs")
        if not isinstance(plugin_configs, dict):
            return False
        plugin_entry = plugin_configs.get(_PLUGIN_ID)
        if not isinstance(plugin_entry, dict):
            return False
        options = plugin_entry.get("options")
        if not isinstance(options, dict) or _WORKSPACE_ID_KEY not in options:
            return False

        del options[_WORKSPACE_ID_KEY]

        if not _atomic_write_json(
            settings_path,
            settings,
            tmp_dir=str(settings_path.parent),
            tmp_prefix=_SETTINGS_JSON_TMP_PREFIX,
        ):
            return False
        return True
    except Exception:
        logger.debug("Failed to delete workspace_id override residue", exc_info=True)
        return False


def _sweep_orphan_temp_files(directory: str, prefix: str) -> None:
    """
    Best-effort cleanup of leftover atomic-write temp files matching
    *prefix* in *directory* (no recursion).

    Shared by every atomic-write path in this module (D169 / C5, extended
    to ``.claude/settings.json`` by a later review fix). Only unlinks
    regular, non-symlink files matching the prefix. Never raises — any
    failure here is swallowed, since this is purely a hygiene step and must
    never block the actual write that follows it.

    Fully closes the fixed-name symlink-plant attack this prefix scheme
    defends against (see ``_MCP_JSON_TMP_PREFIX``): an attacker cannot
    pre-plant a symlink at a *predictable* temp-file name, because
    ``tempfile.mkstemp`` always generates a random suffix and this sweep
    only ever unlinks names it already finds on disk with that prefix — it
    never creates or follows a symlink itself.

    Does NOT fully close a same-process-family concurrent-session race: if
    a second session's write is genuinely in flight (its ``mkstemp`` file
    exists but ``os.replace`` hasn't run yet) when a first session's sweep
    runs, the first session can unlink the second session's in-progress
    temp file out from under it. That window is narrowed by this sweep
    running immediately before each write rather than as an independent
    cron-style pass, but it is not eliminated. The failure is self-healing
    (the victim session simply gets ``WriteResult.FAILED`` and the next
    session's write starts clean) and never corrupts the on-disk file
    itself, since ``os.replace`` only ever targets the victim's own
    already-open file descriptor.
    """
    try:
        for candidate in Path(directory).glob(f"{prefix}*"):
            try:
                if candidate.is_symlink():
                    continue
                if candidate.is_file():
                    candidate.unlink()
            except OSError:
                continue
    except OSError:
        pass


def _sweep_orphan_mcp_json_temp_files(project_root: str) -> None:
    """
    Best-effort cleanup of leftover ``.mcp.json.neuroloom-tmp-*`` files from
    a previous crashed write attempt (D169 / C5), in *project_root* itself.

    Thin wrapper over ``_sweep_orphan_temp_files``. Kept as a separate,
    stably-named entry point since it is exercised directly by tests.

    Fully closes the fixed-name symlink-plant attack (an attacker cannot
    pre-plant a symlink at a predictable temp-file name, since
    ``tempfile.mkstemp`` always randomizes the suffix). Does NOT fully
    close a same-process-family concurrent-session race — a second
    session's genuinely in-flight temp file can still be swept by a first
    session's sweep before the second session's ``os.replace`` runs. That
    race is narrowed, not eliminated: see ``_sweep_orphan_temp_files`` for
    the full explanation and the self-healing worst case (a spurious
    ``WriteResult.FAILED`` for the victim session).
    """
    _sweep_orphan_temp_files(project_root, _MCP_JSON_TMP_PREFIX)


def _atomic_write_json(
    target_path: Path,
    data: Any,
    *,
    tmp_dir: str,
    tmp_prefix: str,
) -> bool:
    """
    Atomically write *data* as JSON to *target_path* (D169 / C5, extended by
    a later review fix to cover ``.claude/settings.json`` as well as
    ``.mcp.json`` — both are hand-maintained, potentially repo-committed
    files where a crash-mid-write truncate is unacceptable).

    Does NOT sweep orphaned temp files itself — callers are expected to
    call ``_sweep_orphan_temp_files(tmp_dir, tmp_prefix)`` once at the start
    of their own function (before any read/parse work), matching the
    existing ``_write_literal_mcp_json_entry`` pattern, rather than have
    this helper re-sweep on every call.

    Writes to a ``tempfile.mkstemp``-created temp file (random name,
    exclusive, non-symlink-following by construction) in *tmp_dir* — which
    must be the same directory as *target_path* for ``os.replace`` to be
    atomic. *target_path*'s existing permission mode is preserved onto the
    temp file before the replace, if the file exists and its mode can be
    read.

    Returns True on success. Returns False on any failure, in which case
    the original file (if any) is left completely untouched — the rename
    never happens, and any partially-written temp file is unlinked before
    returning. Never raises.
    """
    original_mode: int | None = None
    if target_path.exists():
        try:
            original_mode = os.stat(target_path).st_mode
        except OSError:
            original_mode = None

    try:
        fd, tmp_name = tempfile.mkstemp(dir=tmp_dir, prefix=tmp_prefix)
    except OSError:
        return False

    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        if original_mode is not None:
            try:
                os.chmod(tmp_path, original_mode)
            except OSError:
                pass
        os.replace(tmp_path, target_path)
        return True
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _write_literal_mcp_json_entry(
    project_root: str,
    db_path: Path,
    workspace_id: str | None,
    *,
    create_if_missing: bool,
) -> WriteResult:
    """
    Ensure the project-scope .mcp.json "neuroloom" entry carries exactly the
    requested X-Workspace-Id state (D169).

    ``workspace_id=None`` means "ensure headerless" (R2 — a project may
    validly have no literal header at all, deferring to ADR-13's server-side
    auto-resolution). A UUID string means "ensure the header equals this
    value".

    Deviation from the plan's literal signature: this function additionally
    takes ``db_path``, needed to read/write the ownership record (see module
    docstring, "Ownership marker design" — the plan's preferred in-entry
    ``managed_by`` marker could not be verified as schema-tolerant, so
    ownership is tracked in .neuroloom.db instead).

    Full state machine:
    - Malformed top-level JSON, or the file can't be read at all -> FAILED,
      file untouched.
    - ``mcpServers`` present but not a dict -> SKIPPED_CONFLICT.
    - No "neuroloom" entry and ``create_if_missing=False`` -> SUCCESS
      (nothing to do — the true-zero-write guarantee for a project this
      module has never touched).
    - No "neuroloom" entry and ``create_if_missing=True`` -> a new entry is
      created with the expected ``type``/``url`` and an empty ``headers``
      dict; ownership is stamped in .neuroloom.db at this point only.
    - Existing "neuroloom" entry that is not a dict -> SKIPPED_CONFLICT.
    - Existing entry whose ``url``/``type`` don't match what this module
      would have created -> SKIPPED_CONFLICT (also blocks a spoofed,
      repo-committed entry from ever being legitimized by this writer).
    - Existing entry with matching ``url``/``type`` but no ownership record
      in .neuroloom.db -> SKIPPED_UNMANAGED, file completely untouched,
      headers included (C4 — protects a user's own hand-maintained entry).
    - Existing owned entry whose ``headers`` value is present but not a
      dict -> SKIPPED_CONFLICT.
    - Otherwise: merge-preserving header update — only ``headers
      ["X-Workspace-Id"]`` is ever touched, never any other key in the entry
      or any other entry under ``mcpServers``.

    Idempotent: if the on-disk state already matches the target, returns
    SUCCESS without writing.

    Atomic and symlink-safe (C5): writes go to a
    ``tempfile.mkstemp``-created temp file (random name, exclusive,
    non-symlink-following by construction) in *project_root*, with the
    original file's permission mode preserved, then ``os.replace`` onto
    ``.mcp.json``. A leftover temp file from a previous crashed run is swept
    at the start of this call. On any exception before the rename
    completes, the temp file is unlinked and FAILED is returned with the
    original file untouched.

    Never raises.
    """
    mcp_path = Path(project_root) / ".mcp.json"

    _sweep_orphan_mcp_json_temp_files(project_root)

    try:
        if mcp_path.exists():
            try:
                text = mcp_path.read_text(encoding="utf-8")
            except OSError:
                return WriteResult.FAILED
            if text.strip() == "":
                config: dict[str, Any] = {"mcpServers": {}}
            else:
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    return WriteResult.FAILED
                if not isinstance(parsed, dict):
                    return WriteResult.FAILED
                config = parsed
        else:
            config = {"mcpServers": {}}

        mcp_servers = config.get("mcpServers")
        if mcp_servers is None:
            mcp_servers = {}
            config["mcpServers"] = mcp_servers
        elif not isinstance(mcp_servers, dict):
            return WriteResult.SKIPPED_CONFLICT

        entry = mcp_servers.get(_MCP_SERVER_NAME)
        is_new_entry = False

        if entry is None:
            if not create_if_missing:
                return WriteResult.SUCCESS
            entry = {
                "type": _MCP_SERVER_TYPE,
                "url": _MCP_SERVER_URL,
                "headers": {},
            }
            mcp_servers[_MCP_SERVER_NAME] = entry
            is_new_entry = True
        else:
            if not isinstance(entry, dict):
                return WriteResult.SKIPPED_CONFLICT
            if entry.get("type") != _MCP_SERVER_TYPE or entry.get("url") != _MCP_SERVER_URL:
                return WriteResult.SKIPPED_CONFLICT
            if not _is_mcp_entry_owned(db_path):
                return WriteResult.SKIPPED_UNMANAGED
            headers_val = entry.get("headers")
            if headers_val is not None and not isinstance(headers_val, dict):
                return WriteResult.SKIPPED_CONFLICT

        headers: dict[str, Any] = entry.setdefault("headers", {})
        current_header = headers.get(_WORKSPACE_HEADER)

        if not is_new_entry:
            if workspace_id is None and current_header is None:
                return WriteResult.SUCCESS
            if workspace_id is not None and current_header == workspace_id:
                return WriteResult.SUCCESS

        if workspace_id is None:
            headers.pop(_WORKSPACE_HEADER, None)
        else:
            headers[_WORKSPACE_HEADER] = workspace_id

        if not _atomic_write_json(
            mcp_path, config, tmp_dir=project_root, tmp_prefix=_MCP_JSON_TMP_PREFIX
        ):
            return WriteResult.FAILED

        if is_new_entry:
            _mark_mcp_entry_owned(db_path)

        return WriteResult.SUCCESS

    except Exception:
        logger.debug("Failed to write .mcp.json workspace entry", exc_info=True)
        return WriteResult.FAILED


def ensure_workspace_configured_detailed(
    project_root: str,
    db_path: Path,
    api_base: str,
    api_key: str,
) -> WorkspaceConfigResult:
    """
    Ensure the workspace routing state is configured for this project, and
    return the full outcome (D169).

    Resolution order:
    1. If ``api_key`` is present: run the cache-fingerprint-fast-path ->
       ``_fetch_workspace_id_from_api`` flow. The result (``cached_default``)
       is written *only* to the .neuroloom.db F14 cache — never to
       ``.claude/settings.json``, never as a literal header directly from
       this step (that collision — writing an auto-fetched default into the
       same key a human override lives at — is R1's fixed regression).
       This is the *only* part of this function gated on ``api_key``: the
       override read, the residue migration, and both literal-writer calls
       below all run unconditionally (C3) — none of them need an API key.
    2. Read a manual per-project override from ``.claude/settings.json``
       (``_read_manual_workspace_override``).
    3. Residue check (C1): if a value was read and ``cached_default`` is
       not None and the two are equal, this is the plugin's own prior
       auto-write, not a human override — delete it from
       ``.claude/settings.json`` and treat this call as "no override". If a
       value was read but there is no ``cached_default`` to compare against
       (never fetched, or the API key has since rotated), the comparison is
       inconclusive and the value is trusted as a genuine override.
    4. If a genuine (non-residue) override remains: write the literal
       header (``create_if_missing=True``). Only on ``WriteResult.SUCCESS``
       is the override considered "applied" and returned as the resolved
       workspace_id. On any other result, nothing is reported as
       succeeding — the header wasn't actually written, so this returns
       ``workspace_id=None`` (never the override value) even though a
       ``cached_default`` may exist.
    5. Otherwise (no override, or residue just migrated away): Branch C
       always ensures an owned, headerless baseline .mcp.json entry exists
       (``create_if_missing=True``). ``cached_default`` (which may be
       ``None``) is returned as ``workspace_id`` for callers that want the
       resolved bootstrap default for other bookkeeping — it is never
       written to any file at this point.

    Always non-fatal — never raises.
    """
    cached_default: str | None = None

    if api_key:
        fingerprint = _fingerprint_api_key(api_key)
        cached_workspace_id = load_workspace_id_from_db(db_path)
        cached_fingerprint = _load_config_value(db_path, _WORKSPACE_ID_FINGERPRINT_KEY)

        if cached_workspace_id and cached_fingerprint == fingerprint:
            cached_default = cached_workspace_id
        else:
            result = _fetch_workspace_id_from_api(api_base, api_key)
            if result.status is WorkspaceFetchStatus.RESOLVED and result.workspace_id:
                cached_default = result.workspace_id
                _save_workspace_id_to_db(db_path, cached_default)
                _save_config_value(db_path, _WORKSPACE_ID_FINGERPRINT_KEY, fingerprint)
            elif result.status is WorkspaceFetchStatus.AMBIGUOUS:
                return WorkspaceConfigResult(WORKSPACE_ID_AMBIGUOUS)
            # else UNREACHABLE: cached_default stays None, matching the
            # prior "stay silent, retry next session" behavior.

    override = _read_manual_workspace_override(project_root)

    migrated_residue_value: str | None = None
    if override is not None and cached_default is not None and override == cached_default:
        if _delete_manual_workspace_override_key(project_root):
            migrated_residue_value = override
        override = None

    if override is not None:
        write_result = _write_literal_mcp_json_entry(
            project_root, db_path, override, create_if_missing=True
        )
        if write_result is WriteResult.SUCCESS:
            return WorkspaceConfigResult(
                override,
                override_applied=True,
                migrated_residue_value=migrated_residue_value,
                override_write_result=write_result,
            )
        logger.warning(
            "Failed to write X-Workspace-Id override into .mcp.json (result=%s)",
            write_result,
        )
        return WorkspaceConfigResult(
            None,
            migrated_residue_value=migrated_residue_value,
            override_write_result=write_result,
        )

    baseline_result = _write_literal_mcp_json_entry(
        project_root, db_path, None, create_if_missing=True
    )
    return WorkspaceConfigResult(
        cached_default,
        migrated_residue_value=migrated_residue_value,
        baseline_write_result=baseline_result,
    )


def ensure_workspace_configured(
    project_root: str,
    db_path: Path,
    api_base: str,
    api_key: str,
) -> str | None:
    """
    Backward-compatible entry point returning just the resolved
    workspace_id (an actual UUID, ``WORKSPACE_ID_AMBIGUOUS``, or ``None``).

    Used by callers (e.g. ``skills/init/SKILL.md``'s inline script) that
    only need the resolved value, not the full ``WorkspaceConfigResult``
    (visibility-log fields consumed by ``session_start.py`` — see
    ``ensure_workspace_configured_detailed``).

    Always non-fatal — never raises.
    """
    return ensure_workspace_configured_detailed(project_root, db_path, api_base, api_key).workspace_id
