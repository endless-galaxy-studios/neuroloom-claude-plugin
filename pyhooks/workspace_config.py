"""
Workspace ID configuration for neuroloom-claude-plugin.

--- Why this matters ---

The Neuroloom MCP server issues OAuth JWTs that authenticate a *user*, not a
workspace (ADR-13) — workspace selection happens per-request, via an
``X-Workspace-Id`` header or server-side auto-resolution for single-workspace
callers. When a developer works on multiple projects that each need their
own workspace, per-project routing requires a literal, non-templated header
written into that project's own MCP connection.

--- The mechanism (D169, superseded by D172) ---

Claude Code's ``${user_config.X}`` template substitution deliberately reads
only user-level and managed settings, never project-level ``pluginConfigs``
— a security boundary, not an oversight, so a cloned/untrusted repository
can't smuggle values into a plugin's declared configuration. Writing an
auto-fetched workspace_id into project-level ``pluginConfigs`` and expecting
substitution to pick it up therefore never worked; the header stayed an
unresolved template string on every request (commit ``dc7226e``).

This module instead writes a **literal** ``X-Workspace-Id`` header directly
into a project-scope ``.mcp.json`` entry.

--- D172: mutable desired state + reconciliation ---

D169's mechanism read a human override from ``.claude/settings.json`` once
per session and conditionally wrote it, with a one-shot residue-vs-override
comparison standing in for a real lifecycle. A round-6 review found the
model itself unsound: a workspace override is mutable desired state — a
human can set it, change it, and clear it over a project's lifetime — not a
value confirmed once and then forgotten. D172 replaces that model with an
explicit **desired state vs. observed state** split, borrowed from control
theory (the same shape a thermostat uses — see ``decide_reconciliation``'s
docstring for the plain-language version):

- **Desired state** lives in ``.neuroloom.db``, written only through a
  human-driven command flow (``/neuroloom:workspace``, also invoked by
  ``/neuroloom:init``). It is genuinely three-valued: unset (no decision
  has ever been made), set-to-a-value, or explicitly-cleared (a real,
  confirmed "auto-resolve, no override" decision — distinct from "never
  decided").
- **Observed state** is read directly from ``.mcp.json`` — absent, an
  owned/unowned entry with no header, or an owned/unowned entry with a
  header value.
- **Ownership** (unchanged from D169 — see "Ownership marker design"
  below) distinguishes an entry this module created from one a human
  hand-authored or hand-edited. Automatic paths never touch an entry they
  don't own (FR-7); the human-driven command's adoption flow (Phase 3) is
  the only place ownership can be stamped retroactively, under explicit
  per-value consent.
- **Reconciliation** (``reconcile`` / ``decide_reconciliation`` below) is
  the pure function + imperative-shell pair that compares desired against
  observed and drives observed toward desired for owned entries only,
  idempotently, every session.

``.claude/settings.json`` drops out of the live reconciliation loop
entirely under D172 — it survives only as a one-time migration input
(``migrate_settings_json_override``, the *sole* function in this module,
and the whole plugin, that reads that file for routing purposes). D169's
one-shot residue-vs-override equality-guessing machinery — the private
helpers that gated a single per-project "have we already run the
comparison" check, the settings.json reader that fed it, and the old
two entry points that owned this whole flow, along with the sentinel
value one of those entry points used to signal an unresolvable multi-
membership caller — is retired outright, not wrapped (D172 Phase 2). D172's
explicit desired-state model never needs to *guess* whether a value is
human intent or the plugin's own stale write, because desired state is
always the product of an explicit human answer, a migration, or an
adoption, never an inference from file contents. See ADR-18 (supersedes
ADR-15 in full) for the full lifecycle transition table this module
implements.

--- Ownership marker design: db-tracked, not in-entry (D169, unchanged) ---

D169's plan preferred an in-entry ``"managed_by": "neuroloom-plugin"`` key
alongside ``type``/``url``/``headers``, contingent on confirming Claude
Code's ``.mcp.json`` schema tolerates an unrecognized key inside an
``mcpServers`` entry. That tolerance could not be confirmed during that
implementation (no Context7/live-host verification channel was available).
Per the plan's own documented fallback, ownership is instead tracked as a
boolean flag in this project's ``.neuroloom.db`` config table
(``_MCP_ENTRY_OWNED_DB_KEY``), set only at entry-creation time (or, under
D172, at explicit-consent adoption time). See ADR-14 for the full
risk-asymmetry rationale: an unverified in-entry key risks a silent,
fleet-wide connection outage if the host schema turns out to reject it
(this module would report ``SUCCESS`` — the JSON write itself succeeds —
while Claude Code silently fails to parse the file downstream, with no
feedback channel back to this module). The db-tracked fallback's worst case
is local and recoverable: a wiped ``.neuroloom.db`` loses the ownership
record, so this module stops self-healing its own entry and (under
CREATE_BASELINE/CREATE_WITH_HEADER) may emit a spurious "connection
missing" warning — visible and degraded, never a silent host-level
rejection.

--- Resolvability, not presence (D171, carried forward by D172) ---

D171 established that "an API key is present" is the wrong gate for safe
reconciliation — the right gate is "the key resolves to a workspace right
now" (a successful, current API round-trip), because a present-but-invalid
key or a temporarily unreachable API reaches the same hazard an absent key
does. D172 makes this gate a single shared function
(``check_resolvability`` / ``check_resolvability_with_cache`` /
``resolvability_from_fetch_status``) every write path funnels through,
rather than a convention re-implemented per call site. Both
``WorkspaceFetchStatus.UNREACHABLE`` and ``WorkspaceFetchStatus.AMBIGUOUS``
collapse to ``Resolvability.UNREACHABLE`` at this gate — "not resolved, no
write" — before ever reaching the pure decision core, which only ever sees
a two-valued ``Resolvability``. This collapse is retained defensively even
though the underlying Token-authenticated bootstrap call
(``_fetch_workspace_id_from_api``) is structurally incapable of returning
``AMBIGUOUS`` (see that function's docstring) — if that call's auth dispatch
ever changes, this is the one place that keeps a write from silently going
through on a value the code couldn't actually confirm.
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
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Any, NamedTuple

logger = logging.getLogger(__name__)

_WORKSPACE_ID_KEY = "workspace_id"

# F14 (D167 Phase 6): SHA-256 of the API key the cached workspace_id was
# resolved for. Mirrors the MCP server's own token-hash-caching pattern —
# the raw key is never stored or logged, only its digest. Compared against a
# freshly computed fingerprint on every session start so a rotated API key
# (which may now belong to a different workspace) invalidates the cache
# instead of the plugin trusting a stale workspace_id forever.
_WORKSPACE_ID_FINGERPRINT_KEY = "workspace_id_key_fingerprint"

# D169 (C4 fallback — see module docstring "Ownership marker design"):
# per-project flag in .neuroloom.db recording that this module itself
# created (D169) or was given explicit consent to adopt (D172, Phase 3) the
# current project's .mcp.json "neuroloom" entry. Set at entry-creation time
# inside _write_literal_mcp_json_entry, or at adoption time by Phase 3's
# adopt flow — never added retroactively by any automatic path (FR-7).
_MCP_ENTRY_OWNED_DB_KEY = "mcp_json_neuroloom_entry_owned"
_MCP_ENTRY_OWNED_DB_VALUE = "1"

# D172 — mutable desired-state keys (§4 of the spec: "Data Model Changes").
# Genuinely three-valued (see DesiredKind): a missing/"unset" row means "no
# decision has ever been made", distinct from "cleared" (a real, confirmed
# "auto-resolve, no override" decision) — the structural fix for the D169
# round-6 modeling error, where a one-shot "confirm" action had no way to
# represent "explicitly nothing," only "not yet."
_DESIRED_WORKSPACE_VALUE_KEY = "desired_workspace_id"
_DESIRED_WORKSPACE_SET_STATE_KEY = "desired_workspace_set_state"
_DESIRED_SET_STATE_VALUE = "value"
_DESIRED_SET_STATE_CLEARED = "cleared"
_DESIRED_SET_STATE_UNSET = "unset"

# D172 (FR-8) — records that the one-time settings.json disclose-confirm-
# migrate step has run for this project, whether it resulted in a migrated
# value, "nothing was there to migrate", or (Phase 4's concern) an explicit
# decline. Distinct in purpose from D169's now-retired one-time
# residue-check marker: that marker gated a guess ("is this value ours or a
# human's"); this one gates a disclosure ("have we already told the human
# about this file's legacy value"). See the module docstring's D172 section
# for why the guess itself is retired, not carried forward.
_SETTINGS_MIGRATION_DONE_KEY = "settings_json_migration_done"
_SETTINGS_MIGRATION_DONE_VALUE = "1"

_INSIGHT_PATH = "/api/v1/workspaces/mine/insight"
_WORKSPACES_LIST_PATH = "/api/v1/workspaces/"

_FETCH_TIMEOUT = 5.0

# D172 pagination constraint (Phase 2): request the endpoint's max page
# size and scan up to this many pages before giving up — 5 * 200 = 1,000
# workspaces, a bounded cost with an honest "scan truncated" outcome when
# the cap is hit while pages are still full (see verify_target_workspace_exists).
_WORKSPACES_PAGE_SIZE = 200
_WORKSPACES_MAX_PAGES = 5

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
# _delete_settings_json_legacy_key). Kept in its own namespace, distinct
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
        (or been given consent to adopt) it. The file is left completely
        untouched, headers included — this is what protects a user's own
        hand-maintained ``neuroloom`` entry.
    FAILED: an I/O or top-level parse error occurred. The file is left
        untouched (or, if a write was in flight, the original file is
        unaffected — the atomic rename never happens on failure).
    """

    SUCCESS = "success"
    SKIPPED_CONFLICT = "skipped_conflict"
    SKIPPED_UNMANAGED = "skipped_unmanaged"
    FAILED = "failed"


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
    """Read the stored auto-fetched-default workspace_id from .neuroloom.db.

    This is the F14 bootstrap-fetch cache (the API key's default
    workspace), never the D172 desired-state value — see
    ``read_desired_state`` for that. NFR-Arch-1 requires the two never be
    conflated: this cache is never promoted to desired state by any
    automatic path.
    """
    return _load_config_value(db_path, _WORKSPACE_ID_KEY)


def _save_workspace_id_to_db(db_path: Path, workspace_id: str) -> None:
    """Persist the auto-fetched-default workspace_id (F14 cache)."""
    _save_config_value(db_path, _WORKSPACE_ID_KEY, workspace_id)


def _is_mcp_entry_owned(db_path: Path) -> bool:
    """
    True if this module has a record of having created (or been given
    explicit consent to adopt) the current project's .mcp.json "neuroloom"
    entry (D169 / C4 ownership check).
    """
    return _load_config_value(db_path, _MCP_ENTRY_OWNED_DB_KEY) == _MCP_ENTRY_OWNED_DB_VALUE


def _mark_mcp_entry_owned(db_path: Path) -> None:
    """
    Record that this module now owns the current project's .mcp.json
    "neuroloom" entry. Called at entry-creation time, or (Phase 3) at
    explicit-consent adoption time — never retroactively by any automatic
    path.
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

    Confirmed (D169, re-verified at D172 plan time by reading
    ``auth/dependencies.py``'s ``get_current_workspace``): a Token-
    authenticated project API key resolves to exactly one workspace by a
    direct, non-nullable join — this call is structurally incapable of
    returning AMBIGUOUS regardless of how many workspaces the underlying
    user account belongs to. The AMBIGUOUS branch below is retained as
    defensive dead code only, in case the API's auth dispatch for this
    specific bootstrap call changes in the future — D172 gives it no new
    live trigger, but does add a defensive test (below,
    ``TestForcedAmbiguousResolvabilityRefusesWrite``) proving the write
    gate still refuses to write even if this branch somehow fires.

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


def fetch_workspace_id_from_api(api_base: str, api_key: str) -> WorkspaceFetchResult:
    """
    Public entry point for callers outside this module (D171 / SKILL.md's
    Phase 0 Step 2) that need the bootstrap-insight lookup without
    reaching across a `_`-prefixed boundary. Thin, behavior-preserving
    delegation — see `_fetch_workspace_id_from_api` for the actual
    implementation. Never called from this module's own internals; kept
    as a separate symbol so the existing private function's call sites
    (and the tests that patch it) are completely unaffected.
    """
    return _fetch_workspace_id_from_api(api_base, api_key)


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
    bypass_unowned_gate: bool = False,
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

    ``bypass_unowned_gate`` (D172 Phase 3, default False): when True, skips
    the "existing entry but no ownership record -> SKIPPED_UNMANAGED" check
    below and proceeds with the merge-preserving write anyway. The url/type
    structural check that precedes it (SKIPPED_CONFLICT) is untouched by
    this flag — that gate still blocks a spoofed/mismatched entry from ever
    being written to, adoption or not. This flag exists for exactly one
    caller: ``adopt_unowned_entry``'s explicit-consent write, after a human
    has confirmed they want this entry adopted (D172 Phase 3) — it must
    never be set True by any automatic path, since that would silently
    defeat the FR-7 protection this whole gate exists for. Note this
    function still does NOT stamp ownership for an existing (non-new)
    entry even when the bypass fires and the write succeeds — the caller
    is responsible for stamping ownership itself, after confirming success
    (see ``adopt_unowned_entry``'s docstring for why that ordering matters).

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
      headers included (C4 — protects a user's own hand-maintained entry;
      D172's explicit-consent adoption flow, Phase 3, is the only thing
      that can change this outcome for a given entry).
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
            if not _is_mcp_entry_owned(db_path) and not bypass_unowned_gate:
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


# ===========================================================================
# D172 — mutable desired state + pure reconciliation-decision core
# ===========================================================================


class DesiredKind(Enum):
    UNSET = auto()
    SET_TO_VALUE = auto()
    EXPLICITLY_CLEARED = auto()


class ObservedKind(Enum):
    ABSENT = auto()
    HEADERLESS = auto()
    HEADERED_WITH_VALUE = auto()


class Ownership(Enum):
    OWNED = auto()
    UNOWNED = auto()


class Resolvability(Enum):
    """
    Two-valued resolvability, as consumed by the pure decision core.

    Distinct from ``WorkspaceFetchStatus`` (three-valued: RESOLVED /
    AMBIGUOUS / UNREACHABLE) — ``resolvability_from_fetch_status`` is the
    single collapse point between the two, so AMBIGUOUS is never a value
    this enum, or anything downstream of it, has to reason about.
    """

    RESOLVED = auto()
    UNREACHABLE = auto()


class Decision(Enum):
    """The eight-value reconciliation decision vocabulary (D172 R2-3/M-20)."""

    NO_OP = auto()
    CREATE_BASELINE = auto()
    CREATE_WITH_HEADER = auto()
    WRITE_HEADER = auto()
    CLEAR_HEADER = auto()
    STAMP_OWNERSHIP_ONLY = auto()
    STAMP_OWNERSHIP_WRITE_HEADER = auto()
    WARN_ONLY = auto()


@dataclass(frozen=True)
class DesiredState:
    kind: DesiredKind
    value: str | None = None

    def __post_init__(self) -> None:
        if self.kind is DesiredKind.SET_TO_VALUE and self.value is None:
            raise ValueError("SET_TO_VALUE requires a value")
        if self.kind is not DesiredKind.SET_TO_VALUE and self.value is not None:
            raise ValueError(f"{self.kind} must not carry a value")


@dataclass(frozen=True)
class ObservedState:
    kind: ObservedKind
    value: str | None = None

    def __post_init__(self) -> None:
        if self.kind is ObservedKind.HEADERED_WITH_VALUE and self.value is None:
            raise ValueError("HEADERED_WITH_VALUE requires a value")
        if self.kind is not ObservedKind.HEADERED_WITH_VALUE and self.value is not None:
            raise ValueError(f"{self.kind} must not carry a value")


def decide_reconciliation(
    desired: DesiredState,
    observed: ObservedState,
    ownership: Ownership,
    resolvability: Resolvability,
) -> Decision:
    """
    Pure decision core (D172, Phase 1 red suite / Phase 2 implementation).

    A thermostat compares its dial setting (desired) against its sensor
    reading (observed) and decides one thing: heat, cool, or do nothing.
    This function is that comparison for workspace routing — given what a
    human wants (``desired``), what's actually on disk (``observed``),
    whether this plugin is allowed to touch that disk state
    (``ownership``), and whether the plugin can currently prove the API
    key still resolves to a real workspace (``resolvability``), it answers
    exactly one of eight things: do nothing, create an entry (with or
    without a header already applied), write or clear a header on an
    entry this plugin already owns, stamp ownership on an unowned entry
    (with or without also changing its header), or warn a human because no
    safe automatic answer exists.

    Pure and idempotent by construction: no filesystem, no network, no
    database, no memory of previous calls — the same four inputs always
    produce the same answer (Phase 1's ``TestDecisionCoreIdempotency``).
    This is what lets the full 4-axis grid be exhaustively unit-tested with
    no mocking (see ``tests/test_workspace_config.py``'s D172 Phase 1
    section) instead of requiring a real filesystem/database harness for
    each of the 17 reachable combinations — that imperative-shell coverage
    (``reconcile``, below) is what actually executes a decision against
    real state.

    Design call this function makes explicit (an explicit SDET judgment
    call from the Phase 1 red suite, confirmed as written during Phase 2
    review — see the test file's own module docstring for the original
    framing): distinguishing WARN_ONLY from the two STAMP_OWNERSHIP_*
    decisions for an unowned entry cannot come from the ownership axis
    alone (both cases are ``Ownership.UNOWNED``) — it comes from whether
    the *observed* entry carries a concrete header value to make an
    adoption decision about. An unowned, headerless entry has no existing
    value to sensibly "adopt as-is" against, so a desired SET answers
    WARN_ONLY — flag it for a human, never guess. An unowned entry that
    already carries a header answers STAMP_OWNERSHIP_ONLY (the header
    already equals the desired value — "adopt as-is") or
    STAMP_OWNERSHIP_WRITE_HEADER (it differs — "adopt and change"). This
    function only ever answers "what write would converge, in principle"
    — it is a caller-side policy choice (FR-7, enforced by ``reconcile``
    below) whether a given caller is ever allowed to *execute* the two
    STAMP_OWNERSHIP_* decisions without a human's explicit consent.

    Collapse point: ``resolvability`` is checked first and unconditionally
    — under UNREACHABLE, every combination of the other three axes answers
    NO_OP, with no exception. This is a safety property, not a tidiness
    one: if the plugin cannot currently prove the caller's key resolves to
    a real, reachable workspace, writing *anything* to ``.mcp.json`` risks
    pointing a project at a workspace nobody confirmed still exists — the
    same way a GPS that's lost satellite lock should report "position
    unknown" rather than keep dead-reckoning as if nothing had changed.
    """
    if resolvability is Resolvability.UNREACHABLE:
        return Decision.NO_OP

    if observed.kind is ObservedKind.ABSENT:
        # Ownership is moot here — there is nothing yet to own or not own
        # (TestOwnershipMootWhenAbsent pins this: the answer must not
        # depend on the ownership axis at all when observed=ABSENT).
        if desired.kind is DesiredKind.SET_TO_VALUE:
            return Decision.CREATE_WITH_HEADER
        return Decision.CREATE_BASELINE

    if ownership is Ownership.OWNED:
        if observed.kind is ObservedKind.HEADERLESS:
            if desired.kind is DesiredKind.SET_TO_VALUE:
                return Decision.WRITE_HEADER
            return Decision.NO_OP
        # HEADERED_WITH_VALUE, owned.
        if desired.kind is DesiredKind.SET_TO_VALUE:
            if observed.value == desired.value:
                return Decision.NO_OP
            return Decision.WRITE_HEADER
        return Decision.CLEAR_HEADER

    # ownership is UNOWNED.
    if observed.kind is ObservedKind.HEADERLESS:
        if desired.kind is DesiredKind.SET_TO_VALUE:
            return Decision.WARN_ONLY
        return Decision.NO_OP
    # HEADERED_WITH_VALUE, unowned.
    if desired.kind is DesiredKind.SET_TO_VALUE:
        if observed.value == desired.value:
            return Decision.STAMP_OWNERSHIP_ONLY
        return Decision.STAMP_OWNERSHIP_WRITE_HEADER
    return Decision.NO_OP


# ===========================================================================
# D172 — resolvability gate (shared by every write path)
# ===========================================================================


def resolvability_from_fetch_status(status: WorkspaceFetchStatus) -> Resolvability:
    """
    The single collapse point between the three-valued
    ``WorkspaceFetchStatus`` and the two-valued ``Resolvability`` the pure
    decision core consumes.

    Both ``UNREACHABLE`` and ``AMBIGUOUS`` collapse to
    ``Resolvability.UNREACHABLE`` — "not resolved, no write" — because from
    a write-safety point of view they are the same hazard: the plugin
    cannot currently prove the caller's key resolves to a real, reachable
    workspace. Every resolvability check in this module
    (``check_resolvability``, ``check_resolvability_with_cache``) routes
    through this one function, so there is exactly one place in the
    codebase that decides "does this classify as safe to write" — not one
    per call site.
    """
    if status is WorkspaceFetchStatus.RESOLVED:
        return Resolvability.RESOLVED
    return Resolvability.UNREACHABLE


def check_resolvability(api_base: str, api_key: str) -> tuple[Resolvability, str | None]:
    """
    The shared resolvability gate (FR-2 / NFR-Sec-1) for human-driven write
    paths: set/change/clear/adopt/migrate. Always performs a fresh,
    uncached bootstrap round-trip — a human explicitly asking to change
    routing right now deserves a live answer, not a cached one from a
    possibly-stale session.

    Returns ``(Resolvability.RESOLVED, workspace_id)`` only when the
    round-trip succeeds and resolves a workspace right now. Returns
    ``(Resolvability.UNREACHABLE, None)`` for every other outcome —
    absent key, invalid key, temporarily unreachable API, or the
    defensively-retained AMBIGUOUS case — via
    ``resolvability_from_fetch_status``.

    No write is reachable when this returns anything other than
    ``Resolvability.RESOLVED`` — every write-executing function in this
    module (``reconcile``, ``migrate_settings_json_override``,
    ``gated_reconcile``) is structured so that a non-RESOLVED result short-
    circuits before any filesystem or database write is attempted.
    """
    if not api_key:
        return Resolvability.UNREACHABLE, None
    result = _fetch_workspace_id_from_api(api_base, api_key)
    resolvability = resolvability_from_fetch_status(result.status)
    if resolvability is Resolvability.RESOLVED:
        return Resolvability.RESOLVED, result.workspace_id
    return Resolvability.UNREACHABLE, None


def check_resolvability_with_cache(
    db_path: Path, api_base: str, api_key: str
) -> tuple[Resolvability, str | None]:
    """
    Resolvability-gate variant for the automatic session-start axis
    (Phase 3), which accepts the F14 fingerprint-cache fast path as a
    satisfaction of "resolved" — a network round trip on every single
    session start would be wasteful, and the cache already protects
    against a rotated key silently reusing a stale answer (a mismatched
    fingerprint always forces a fresh fetch below).

    Every other write path (the human-driven command's own
    set/change/clear/adopt/migrate flows) must use ``check_resolvability``
    instead — see that function's docstring for why a live answer matters
    more there. Both functions route through the same
    ``resolvability_from_fetch_status`` collapse point, so the actual
    UNREACHABLE/AMBIGUOUS classification logic is centralized to one place
    even though there are two entry points for "how fresh does the check
    need to be."
    """
    if not api_key:
        return Resolvability.UNREACHABLE, None
    fingerprint = _fingerprint_api_key(api_key)
    cached_workspace_id = load_workspace_id_from_db(db_path)
    cached_fingerprint = _load_config_value(db_path, _WORKSPACE_ID_FINGERPRINT_KEY)
    if cached_workspace_id and cached_fingerprint == fingerprint:
        return Resolvability.RESOLVED, cached_workspace_id

    result = _fetch_workspace_id_from_api(api_base, api_key)
    resolvability = resolvability_from_fetch_status(result.status)
    if resolvability is Resolvability.RESOLVED and result.workspace_id:
        _save_workspace_id_to_db(db_path, result.workspace_id)
        _save_config_value(db_path, _WORKSPACE_ID_FINGERPRINT_KEY, fingerprint)
        return Resolvability.RESOLVED, result.workspace_id
    return Resolvability.UNREACHABLE, None


# ===========================================================================
# D172 — pagination existence/name check (GET /workspaces/)
# ===========================================================================


class WorkspaceExistenceStatus(Enum):
    """
    Three precisely distinguished outcomes of
    ``verify_target_workspace_exists`` (Phase 2 pagination constraint).

    FOUND: the target id was seen on some page within the scan cap.
    NOT_FOUND: every page up to the cap was fetched successfully and none
        contained the target — a real, deterministic answer.
    SCAN_TRUNCATED: the cap (5 full pages of 200) was hit while every page
        was still full, with no signal a 6th page would be empty. This is
        a *weaker* claim than NOT_FOUND — like searching only the first
        1,000 entries of a phone book and reaching the cap still finding
        full pages: the target may exist further in, "not found in the
        part checked" is not the same claim as "confirmed absent."
    UNREACHABLE: a page request itself failed or timed out mid-scan — a
        genuine network failure, not a deterministic cap-exhaustion. Routes
        to the existing unreachable-during-verify UX state (S10), never to
        SCAN_TRUNCATED.
    """

    FOUND = auto()
    NOT_FOUND = auto()
    SCAN_TRUNCATED = auto()
    UNREACHABLE = auto()


class WorkspaceExistenceResult(NamedTuple):
    status: WorkspaceExistenceStatus
    name: str | None = None


def _fetch_workspaces_page(
    api_base: str, api_key: str, offset: int, limit: int
) -> list[dict[str, Any]] | None:
    """
    Fetch one page of ``GET /workspaces/`` (a bare JSON array of workspace
    objects — see ``api/neuroloom_api/routers/workspaces.py``'s
    ``list_workspaces``, verified against live code this phase — not a
    paginated envelope with an ``items``/``total`` wrapper).

    Returns the page's records on success, or ``None`` on any failure
    (network error, non-200, malformed body) — the caller treats ``None``
    as UNREACHABLE, never as an empty page (which would be misread as
    NOT_FOUND).
    """
    url = f"{api_base}{_WORKSPACES_LIST_PATH}?active_only=true&limit={limit}&offset={offset}"
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
                return None
            body = json.loads(resp.read().decode("utf-8"))
    except Exception:
        logger.debug("workspaces list page fetch failed", exc_info=True)
        return None

    if not isinstance(body, list):
        return None
    return [item for item in body if isinstance(item, dict)]


def verify_target_workspace_exists(
    api_base: str, api_key: str, target_workspace_id: str
) -> WorkspaceExistenceResult:
    """
    Paginate ``GET /workspaces/`` (max page size 200, ``active_only=true``)
    up to 5 pages (1,000 workspaces), looking for *target_workspace_id*.
    See ``WorkspaceExistenceStatus`` for the three precisely distinguished
    outcomes this returns.

    Non-target records are held only long enough to check each page for
    the target id — never logged, cached, or persisted past this call.
    Only the target's resolved name (or its absence) survives past this
    function's return.

    Local pagination-correctness proof (real multi-page ``HTTPServer``,
    this repo's convention) is Phase 5's job, not this phase's — this
    phase's own tests cover single-page FOUND/NOT_FOUND, the
    SCAN_TRUNCATED cap-hit shape via a stub page fetcher, and one benign
    live smoke test against the deployed API (page 1, no seeding).
    """
    offset = 0
    for _page_index in range(_WORKSPACES_MAX_PAGES):
        records = _fetch_workspaces_page(api_base, api_key, offset, _WORKSPACES_PAGE_SIZE)
        if records is None:
            return WorkspaceExistenceResult(WorkspaceExistenceStatus.UNREACHABLE)

        for record in records:
            if record.get("id") == target_workspace_id:
                name = record.get("name")
                return WorkspaceExistenceResult(
                    WorkspaceExistenceStatus.FOUND,
                    name if isinstance(name, str) else None,
                )

        if len(records) < _WORKSPACES_PAGE_SIZE:
            return WorkspaceExistenceResult(WorkspaceExistenceStatus.NOT_FOUND)

        offset += _WORKSPACES_PAGE_SIZE

    return WorkspaceExistenceResult(WorkspaceExistenceStatus.SCAN_TRUNCATED)


# ===========================================================================
# D172 — desired-state read/write
# ===========================================================================


def read_desired_state(db_path: Path) -> DesiredState:
    """
    Read the current desired workspace state from ``.neuroloom.db``.

    Genuinely three-valued: a missing/unrecognized set-state row means
    ``DesiredKind.UNSET`` ("no decision has ever been made for this
    project" — §6 state S3), a set-state row of ``"cleared"`` means
    ``DesiredKind.EXPLICITLY_CLEARED`` (a real, confirmed "auto-resolve, no
    override" decision), and a set-state row of ``"value"`` with a present
    value means ``DesiredKind.SET_TO_VALUE``. A ``"value"`` row with no
    actual value stored (a corrupt/partial write) fails closed to UNSET
    rather than raising or fabricating a value — the next reconciliation
    pass then treats this project as never-decided, which is the safe
    direction to fail in (no write is more dangerous than a missing one).
    """
    set_state = _load_config_value(db_path, _DESIRED_WORKSPACE_SET_STATE_KEY)
    if set_state == _DESIRED_SET_STATE_VALUE:
        value = _load_config_value(db_path, _DESIRED_WORKSPACE_VALUE_KEY)
        if value:
            return DesiredState(DesiredKind.SET_TO_VALUE, value)
        return DesiredState(DesiredKind.UNSET)
    if set_state == _DESIRED_SET_STATE_CLEARED:
        return DesiredState(DesiredKind.EXPLICITLY_CLEARED)
    return DesiredState(DesiredKind.UNSET)


def write_desired_state(db_path: Path, desired: DesiredState) -> None:
    """
    Persist a desired workspace state into ``.neuroloom.db``.

    Low-level, unvalidated I/O only — this function does not check
    resolvability (that is ``check_resolvability``'s job, called by the
    orchestration layer above this one, Phase 3/4, before ever reaching
    here) and does not reconcile ``.mcp.json`` (that is ``reconcile``'s
    job). It only ever changes what "desired" means; driving "observed"
    toward it is always a separate, explicit step.
    """
    if desired.kind is DesiredKind.SET_TO_VALUE:
        assert desired.value is not None  # enforced by DesiredState.__post_init__
        _save_config_value(db_path, _DESIRED_WORKSPACE_VALUE_KEY, desired.value)
        _save_config_value(db_path, _DESIRED_WORKSPACE_SET_STATE_KEY, _DESIRED_SET_STATE_VALUE)
    elif desired.kind is DesiredKind.EXPLICITLY_CLEARED:
        _save_config_value(db_path, _DESIRED_WORKSPACE_SET_STATE_KEY, _DESIRED_SET_STATE_CLEARED)
    else:
        _save_config_value(db_path, _DESIRED_WORKSPACE_SET_STATE_KEY, _DESIRED_SET_STATE_UNSET)


def _read_ownership(db_path: Path) -> Ownership:
    return Ownership.OWNED if _is_mcp_entry_owned(db_path) else Ownership.UNOWNED


# ===========================================================================
# D172 — observed-state read (.mcp.json)
# ===========================================================================


class McpJsonConflictError(Exception):
    """
    Raised by ``read_observed_state`` when ``.mcp.json`` is malformed, or
    its "neuroloom" entry doesn't match this module's expected shape
    (NFR-Sec-5 fail-closed): a corrupt or unparseable ``.mcp.json``, or a
    structurally conflicting "neuroloom" entry (wrong ``url``/``type``, a
    non-dict ``headers`` value), is never silently treated as "no entry,
    safe to create." Callers must catch this and surface a visible error
    rather than proceed as if the entry were absent.
    """


def read_observed_state(project_root: str) -> ObservedState:
    """
    Read the current observed workspace-routing state directly from this
    project's ``.mcp.json`` — never from any cache, never from
    ``.neuroloom.db``. This is the "sensor reading" half of the
    desired-vs-observed comparison (see ``decide_reconciliation``'s
    docstring); it is read-only and side-effect-free.

    Raises ``McpJsonConflictError`` (never returns a bogus "absent" or
    "headerless" answer) for anything this module cannot safely interpret:
    unparseable JSON, a non-dict ``mcpServers``/entry/``headers`` value, or
    an existing "neuroloom" entry whose ``url``/``type`` don't match what
    this module would have created. Fail-closed per NFR-Sec-5 — a caller
    that mishandled this exception by treating it as ABSENT would risk
    "safely" creating a fresh entry over one a human is relying on but this
    module can't parse, which is exactly backwards.
    """
    mcp_path = Path(project_root) / ".mcp.json"
    if not mcp_path.exists():
        return ObservedState(ObservedKind.ABSENT)

    try:
        text = mcp_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise McpJsonConflictError(f"could not read .mcp.json: {exc}") from exc

    if text.strip() == "":
        return ObservedState(ObservedKind.ABSENT)

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise McpJsonConflictError(f".mcp.json is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise McpJsonConflictError(".mcp.json top level is not a JSON object")

    mcp_servers = parsed.get("mcpServers")
    if mcp_servers is None:
        return ObservedState(ObservedKind.ABSENT)
    if not isinstance(mcp_servers, dict):
        raise McpJsonConflictError("mcpServers is not a JSON object")

    entry = mcp_servers.get(_MCP_SERVER_NAME)
    if entry is None:
        return ObservedState(ObservedKind.ABSENT)
    if not isinstance(entry, dict):
        raise McpJsonConflictError("'neuroloom' entry is not a JSON object")
    if entry.get("type") != _MCP_SERVER_TYPE or entry.get("url") != _MCP_SERVER_URL:
        raise McpJsonConflictError("'neuroloom' entry url/type does not match")

    headers_val = entry.get("headers")
    if headers_val is not None and not isinstance(headers_val, dict):
        raise McpJsonConflictError("'neuroloom' entry headers is not a JSON object")

    header_value = (headers_val or {}).get(_WORKSPACE_HEADER)
    if header_value is None:
        return ObservedState(ObservedKind.HEADERLESS)
    if not isinstance(header_value, str):
        raise McpJsonConflictError("'neuroloom' entry header value is not a string")
    return ObservedState(ObservedKind.HEADERED_WITH_VALUE, header_value)


def confirmed_state_from_env() -> ObservedState:
    """
    Reconstruct the ``ObservedState`` a human confirmed in the Phase 5
    (S8) adopt flow of ``skills/workspace/SKILL.md``, from the
    ``_NL_CONFIRMED_KIND``/``_NL_CONFIRMED_VALUE`` environment variables
    rather than raw string interpolation into a ``python -c`` payload
    (D172 R2 MAJOR fix). The prior template-substitution approach
    interpolated an unvalidated ``.mcp.json`` header value directly into a
    Python string literal — a crafted value containing a single quote
    could break out of the literal and inject arbitrary code. Threading
    the value through the environment (same convention as
    ``_NL_TARGET_WORKSPACE_ID`` and ``skills/init/SKILL.md``'s
    ``_NL_KEY``) avoids that entirely, since the value never becomes part
    of the source text being executed.

    ``_NL_CONFIRMED_VALUE`` is only meaningful when
    ``_NL_CONFIRMED_KIND`` is ``HEADERED_WITH_VALUE``; for any other kind
    it is ignored (even if set) and the resulting state carries no value.
    When the kind *is* ``HEADERED_WITH_VALUE``, a missing
    ``_NL_CONFIRMED_VALUE`` is treated as an empty string, not ``None`` —
    an empty header value is a valid, distinct ``ObservedState`` (see
    ``ObservedState.__post_init__``), and coercing it to ``None`` would
    raise ``ValueError`` when this function tries to reconstruct it.
    """
    kind = ObservedKind[os.environ["_NL_CONFIRMED_KIND"]]
    value = os.environ.get("_NL_CONFIRMED_VALUE", "") if kind == ObservedKind.HEADERED_WITH_VALUE else None
    return ObservedState(kind, value)


# ===========================================================================
# D172 — reconciliation routine (FR-5)
# ===========================================================================


class ReconcileOutcome(NamedTuple):
    """
    Outcome of ``reconcile``. ``write_result`` is populated only when the
    decision required a filesystem write (``None`` for NO_OP/WARN_ONLY,
    which never touch disk). ``error`` is populated only when
    ``.mcp.json`` couldn't be read at all (``McpJsonConflictError``) or a
    caller reached this function with a decision it is not allowed to
    execute (see ``reconcile``'s docstring, FR-7).
    """

    decision: Decision
    write_result: WriteResult | None
    error: str | None = None


def reconcile(project_root: str, db_path: Path, desired: DesiredState) -> ReconcileOutcome:
    """
    FR-5: compare *desired* (already known — this function performs no
    resolvability check and takes no ``Resolvability`` parameter at all,
    so it cannot accidentally be invoked with a stale or UNREACHABLE
    resolvability value; that is structurally impossible here, not merely
    disciplined) against ``.mcp.json``'s current observed state, and drive
    the latter to match, for entries this module owns. Idempotent: running
    this twice in a row against an already-converged project performs zero
    writes both times (the second call reads NO_OP from
    ``decide_reconciliation`` because the first call's write already
    updated the observed state read on the second call).

    Callers must have already confirmed resolvability (via
    ``check_resolvability`` / ``check_resolvability_with_cache`` — see
    ``gated_reconcile`` for the wired-together version) before calling
    this — this function assumes RESOLVED and only ever asks
    ``decide_reconciliation`` with ``Resolvability.RESOLVED``.

    FR-7 (automatic paths never touch unowned entries): the two
    STAMP_OWNERSHIP_* decisions require explicit human consent (Phase 3's
    adoption flow) and are never executed by this function. Reaching one
    here is an ordinary, expected outcome — not a caller bug — whenever a
    workspace is desired for a project (via a prior /neuroloom:workspace
    run) and its ".mcp.json" "neuroloom" entry is unowned but already
    carries a header (e.g. hand-edited to point at a different workspace,
    or a pre-existing entry a human wrote before this plugin ever touched
    the project). Desired state (``read_desired_state``) is entirely
    independent of the adoption-consent flow, so this combination is
    reachable on every session start for such a project, not just once.
    This function refuses to execute the write and returns a no-write
    outcome with ``error`` populated — adoption requires explicit human
    consent, so the refusal is surfaced to the caller (which is expected
    to make it visible to the human, e.g. session_start.py's Step 8), not
    executed silently.
    """
    try:
        observed = read_observed_state(project_root)
    except McpJsonConflictError as exc:
        return ReconcileOutcome(Decision.NO_OP, None, error=str(exc))

    ownership = _read_ownership(db_path)
    decision = decide_reconciliation(desired, observed, ownership, Resolvability.RESOLVED)

    if decision is Decision.NO_OP or decision is Decision.WARN_ONLY:
        return ReconcileOutcome(decision, None)

    if decision is Decision.CREATE_BASELINE:
        result = _write_literal_mcp_json_entry(
            project_root, db_path, None, create_if_missing=True
        )
        return ReconcileOutcome(decision, result)

    if decision is Decision.CREATE_WITH_HEADER:
        assert desired.value is not None
        result = _write_literal_mcp_json_entry(
            project_root, db_path, desired.value, create_if_missing=True
        )
        return ReconcileOutcome(decision, result)

    if decision is Decision.WRITE_HEADER:
        assert desired.value is not None
        result = _write_literal_mcp_json_entry(
            project_root, db_path, desired.value, create_if_missing=False
        )
        return ReconcileOutcome(decision, result)

    if decision is Decision.CLEAR_HEADER:
        result = _write_literal_mcp_json_entry(
            project_root, db_path, None, create_if_missing=False
        )
        return ReconcileOutcome(decision, result)

    # STAMP_OWNERSHIP_ONLY / STAMP_OWNERSHIP_WRITE_HEADER — see docstring.
    # This is an ordinary, expected outcome for a project with an unowned,
    # headered ".mcp.json" entry and a desired workspace set — not a
    # caller bug. Adoption requires explicit human consent, so this
    # function refuses to write and surfaces that refusal via `error`
    # for the caller to make visible to the human (e.g.
    # session_start.py's Step 8 prints a warning for this outcome),
    # rather than executing it silently.
    return ReconcileOutcome(
        decision,
        None,
        error=(
            f"reconcile() reached an adoption decision ({decision.name}) "
            "— refusing to execute without explicit human consent; run "
            "/neuroloom:workspace to review and adopt this entry"
        ),
    )


class GatedReconcileOutcome(NamedTuple):
    resolvability: Resolvability
    reconcile: ReconcileOutcome | None


def gated_reconcile(
    project_root: str, db_path: Path, api_base: str, api_key: str
) -> GatedReconcileOutcome:
    """
    The automatic-axis entry point: resolvability gate + reconcile, wired
    together. No write is reachable through this function when the
    resolvability gate returns anything other than ``Resolvability
    .RESOLVED`` — ``reconcile`` itself is not even called in that case,
    the same "gate never dials through" shape Phase 1's
    ``TestUnreachableGateNeverInvokesCore`` proves one layer down for the
    pure decision core.

    Uses ``check_resolvability_with_cache`` (the F14 fast-path-tolerant
    gate) — this is the intended entry point for Phase 3's automatic
    session-start axis, where a network round trip on every session start
    would be wasteful. The human-driven command flow (Phase 3/4) should
    call ``check_resolvability`` and ``reconcile`` directly instead, since
    a human explicitly asking to change routing deserves a live answer.
    """
    resolvability, _ = check_resolvability_with_cache(db_path, api_base, api_key)
    if resolvability is not Resolvability.RESOLVED:
        return GatedReconcileOutcome(resolvability, None)
    desired = read_desired_state(db_path)
    outcome = reconcile(project_root, db_path, desired)
    return GatedReconcileOutcome(resolvability, outcome)


# ===========================================================================
# D172 — adoption flow (explicit-consent, TOCTOU-guarded) + decline-hash
# ===========================================================================

# Audit 1 (OQ-2, CD-confirmed hash-scoped sticky): records a fingerprint of
# an unowned .mcp.json "neuroloom" entry's state at the moment a human
# declined to adopt it. Reused shape from the existing F14 fingerprint-cache
# pattern above (_WORKSPACE_ID_FINGERPRINT_KEY) — pair a stored decision
# with a fingerprint of the state it was made against, so a later change
# invalidates it automatically instead of requiring an explicit "did
# anything change" check written by hand at every call site.
_UNOWNED_ENTRY_DECLINE_HASH_KEY = "unowned_entry_decline_hash"


class AdoptionChoice(Enum):
    """
    The three-way consent choice presented by ``/neuroloom:workspace``'s
    adoption prompt (Phase 4) for an unowned ``.mcp.json`` "neuroloom"
    entry.
    """

    ADOPT_AS_IS = auto()  # stamp ownership only; keep the entry's current header exactly
    ADOPT_AND_CHANGE = auto()  # stamp ownership AND write desired_value as the header
    LEAVE_UNOWNED = auto()  # decline; persists a decline-hash (see above)


class AdoptionOutcome(Enum):
    ADOPTED = auto()
    DECLINED = auto()
    STALE_CONFIRMATION = auto()
    WRITE_FAILED = auto()
    NOT_APPLICABLE = auto()


class AdoptionResult(NamedTuple):
    outcome: AdoptionOutcome
    write_result: WriteResult | None = None


def _compute_entry_fingerprint(observed: ObservedState) -> str:
    """
    Deterministic fingerprint of an unowned entry's current observed state
    (kind + header value), for the decline-hash mechanism.

    Same idea as a sticky note left on a shared whiteboard: "I looked at
    this exact entry and chose to leave it alone." If someone later erases
    and rewrites the whiteboard (the entry's state changes — a different
    header value, or the entry disappears and comes back headerless), the
    sticky note no longer describes what's actually there, so it stops
    applying and the adoption offer is free to resurface.
    """
    payload = f"{observed.kind.name}:{observed.value or ''}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def record_unowned_decline(db_path: Path, observed: ObservedState) -> None:
    """
    Persist the "leave unowned" decision's decline-hash for *observed*'s
    exact current state (Audit 1 / §7's LEAVE_UNOWNED branch).
    """
    _save_config_value(
        db_path, _UNOWNED_ENTRY_DECLINE_HASH_KEY, _compute_entry_fingerprint(observed)
    )


def should_resurface_adoption_offer(db_path: Path, observed: ObservedState) -> bool:
    """
    True when the adoption offer should (re)surface for *observed*'s
    current state: either nothing was ever declined for this project, or
    what's declined no longer matches what's actually on disk right now.

    False only when a decline was recorded AND the entry's state today
    fingerprints identically to the state it was declined against — the
    "S7-suppressed" case (Phase 4's copy layer) — meaning the offer stays
    hidden until the entry's state genuinely changes.
    """
    stored = _load_config_value(db_path, _UNOWNED_ENTRY_DECLINE_HASH_KEY)
    if stored is None:
        return True
    return stored != _compute_entry_fingerprint(observed)


def adopt_unowned_entry(
    project_root: str,
    db_path: Path,
    choice: AdoptionChoice,
    confirmed_observed: ObservedState,
    desired_value: str | None = None,
) -> AdoptionResult:
    """
    Execute a human's three-way adoption choice for an unowned
    ``.mcp.json`` "neuroloom" entry. The only intended caller is
    ``/neuroloom:workspace``'s adoption prompt (Phase 4) — never an
    automatic path (FR-7).

    TOCTOU re-check, concretely why it matters: the human is shown the
    entry's current state, thinks it over for a few seconds (or longer —
    nothing enforces how quickly they answer), then confirms a choice.
    In that gap, something else could have changed ``.mcp.json`` — a
    second Claude Code session running in the same project, a script, a
    `git pull` that merges in a teammate's edit. Picture confirming "yes,
    adopt the entry pointing at workspace A" while, unnoticed in the
    background, it now points at workspace B — executing the human's
    choice against that now-stale picture would silently adopt the wrong
    thing. This is exactly the TOCTOU (time-of-check to time-of-use) bug
    shape. To close it, this function re-reads ``.mcp.json``'s state right
    here, immediately before the write below, with no other awaited I/O
    between this read and that write, and refuses to proceed
    (``STALE_CONFIRMATION``) unless it still matches *confirmed_observed*
    — the exact state the caller captured and showed the human at prompt
    time.

    Ownership is stamped only *after* the ``.mcp.json`` write itself
    reports ``SUCCESS`` — never before, and never as part of the write
    call for an existing entry (see ``_write_literal_mcp_json_entry``'s
    ``bypass_unowned_gate`` docstring). This ordering means a crash or
    failure between "the bytes are on disk" and "the db says we own it"
    can only ever under-report ownership (a project that's actually
    converged but whose db hasn't caught up yet — self-heals next run),
    never over-report it (a project whose db claims ownership of an entry
    this module never actually wrote).

    The structural url/type gate inside ``_write_literal_mcp_json_entry``
    (``SKIPPED_CONFLICT``) is unmodified and still applies here — adoption
    only ever bypasses the *ownership* check, never the shape check that
    protects against a spoofed or otherwise-mismatched entry.
    """
    if choice is AdoptionChoice.LEAVE_UNOWNED:
        record_unowned_decline(db_path, confirmed_observed)
        return AdoptionResult(AdoptionOutcome.DECLINED)

    # TOCTOU re-check — see docstring. No other awaited I/O happens between
    # this read and the write below.
    try:
        current_observed = read_observed_state(project_root)
    except McpJsonConflictError:
        return AdoptionResult(AdoptionOutcome.STALE_CONFIRMATION)

    if current_observed != confirmed_observed:
        return AdoptionResult(AdoptionOutcome.STALE_CONFIRMATION)

    if current_observed.kind is ObservedKind.ABSENT:
        # Narrow degenerate case: the entry itself vanished between
        # confirmation and execution. Nothing left to adopt — distinct
        # from a successful adoption, but not an error either.
        return AdoptionResult(AdoptionOutcome.NOT_APPLICABLE)

    target_value = (
        current_observed.value if choice is AdoptionChoice.ADOPT_AS_IS else desired_value
    )

    write_result = _write_literal_mcp_json_entry(
        project_root,
        db_path,
        target_value,
        create_if_missing=False,
        bypass_unowned_gate=True,
    )

    if write_result is not WriteResult.SUCCESS:
        return AdoptionResult(AdoptionOutcome.WRITE_FAILED, write_result)

    _mark_mcp_entry_owned(db_path)
    return AdoptionResult(AdoptionOutcome.ADOPTED, write_result)


# ===========================================================================
# D172 — settings.json migration (FR-8, the sole settings.json reader)
# ===========================================================================


def _read_settings_json_legacy_value(project_root: str) -> str | None:
    """
    Read the legacy per-project workspace override from this project's
    ``.claude/settings.json``, if present and well-formed.

    Reads ``pluginConfigs["neuroloom@endless-galaxy-studios"].options
    .workspace_id``, strips surrounding whitespace, and validates the
    result as a UUID. Returns ``None`` on any missing, malformed, or
    non-string value — including a missing or unparseable
    ``settings.json``, or any of the intermediate dict levels not being a
    dict. Never raises.

    This is a fresh D172 function, not a rewrap of D169's retired
    settings.json reader (that function, and its conditional
    residue-equality-guessing caller, are deleted outright — see the
    module docstring's D172 section). Together with
    ``migrate_settings_json_override`` below, this is one of exactly two
    functions in the whole plugin that read ``.claude/settings.json``, and
    both live in this module (FR-8 / NFR-Arch-1).
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


def _delete_settings_json_legacy_key(project_root: str) -> bool:
    """
    Delete ``pluginConfigs["neuroloom@endless-galaxy-studios"].options
    .workspace_id`` from ``.claude/settings.json`` (D172 / FR-8 migration).

    Merge-preserving: only this one key is removed; every other key in the
    file (including sibling plugin options and unrelated pluginConfigs
    entries) is left exactly as found. Called only after the value has
    been persisted into ``.neuroloom.db``'s desired state and has passed
    the same resolvability check any other desired-state write requires
    (see ``migrate_settings_json_override`` — the db write happens first,
    the settings.json delete second, so a crash between them leaves a
    harmless leftover, never a source of drift: this module never reads
    settings.json again for a project once the migration-done marker is
    set).

    Returns True on success (the key existed and was removed), False on
    any failure or if there was nothing to delete. Never raises. Atomic
    and symlink-safe — same ``tempfile.mkstemp`` + ``os.replace`` +
    permission-mode-preservation pattern used for ``.mcp.json``, in its
    own ``_SETTINGS_JSON_TMP_PREFIX`` namespace so the two writers' orphan
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
        logger.debug("Failed to delete workspace_id legacy key", exc_info=True)
        return False


def is_settings_json_migration_done(db_path: Path) -> bool:
    """
    True if the one-time settings.json disclose-confirm-migrate step has
    already run for this project (whether it migrated a value, found
    nothing to migrate, or — Phase 4's concern — the human explicitly
    declined). Prevents re-disclosing the same already-handled
    settings.json state on every future command invocation.
    """
    return (
        _load_config_value(db_path, _SETTINGS_MIGRATION_DONE_KEY)
        == _SETTINGS_MIGRATION_DONE_VALUE
    )


def _mark_settings_json_migration_done(db_path: Path) -> None:
    _save_config_value(db_path, _SETTINGS_MIGRATION_DONE_KEY, _SETTINGS_MIGRATION_DONE_VALUE)


def decline_settings_json_migration(db_path: Path) -> None:
    """
    Records an explicit human decline of the settings.json migration offer
    (§6 state S9, choice "no") — a small, cheap completeness addition for
    Phase 4's orchestration to call; this phase does not itself present
    that prompt.
    """
    _mark_settings_json_migration_done(db_path)


class MigrationOutcome(Enum):
    ALREADY_DONE = auto()
    NOTHING_TO_MIGRATE = auto()
    MIGRATED = auto()
    VERIFY_FAILED = auto()


class MigrationResult(NamedTuple):
    outcome: MigrationOutcome
    workspace_id: str | None = None


def migrate_settings_json_override(
    project_root: str, db_path: Path, api_base: str, api_key: str
) -> MigrationResult:
    """
    FR-8: the sole function in this module — and the whole plugin — that
    reads ``.claude/settings.json`` for routing purposes (together with
    the private ``_read_settings_json_legacy_value`` helper it calls,
    which lives in this same module). One-time, resolvability-gated: a
    legacy value is never silently promoted to desired state without
    first passing the same live verification (``check_resolvability``)
    every other desired-state write requires.

    Idempotent at the "already done" level: once
    ``is_settings_json_migration_done`` is True for this project, this
    function is a no-op that reports ``ALREADY_DONE`` without touching
    ``.claude/settings.json`` again.

    On ``MIGRATED``: the value is written into ``.neuroloom.db``'s desired
    state, the migration-done marker is set, and the key is deleted from
    ``.claude/settings.json`` — in that order (db write, then delete), so
    a crash in between leaves a harmless leftover value in
    ``.claude/settings.json`` that is never read again (the migration-done
    marker is already set), never a source of drift.

    On ``VERIFY_FAILED``: the legacy value was found but did not resolve
    (unreachable API, or — Phase 4's concern once the paginated existence
    check is layered on top via ``verify_target_workspace_exists`` — the
    workspace itself no longer exists). ``.claude/settings.json`` is left
    completely untouched and the migration-done marker is NOT set, so a
    later command run can retry.
    """
    if is_settings_json_migration_done(db_path):
        return MigrationResult(MigrationOutcome.ALREADY_DONE)

    legacy_value = _read_settings_json_legacy_value(project_root)
    if legacy_value is None:
        _mark_settings_json_migration_done(db_path)
        return MigrationResult(MigrationOutcome.NOTHING_TO_MIGRATE)

    resolvability, _ = check_resolvability(api_base, api_key)
    if resolvability is not Resolvability.RESOLVED:
        return MigrationResult(MigrationOutcome.VERIFY_FAILED)

    write_desired_state(db_path, DesiredState(DesiredKind.SET_TO_VALUE, legacy_value))
    _mark_settings_json_migration_done(db_path)
    _delete_settings_json_legacy_key(project_root)
    return MigrationResult(MigrationOutcome.MIGRATED, legacy_value)


# ===========================================================================
# D172 — network-free desired-vs-observed comparison (R2-6)
# ===========================================================================


class RoutingComparison(Enum):
    """
    Outcome of ``compare_desired_vs_observed``. Deliberately does not
    distinguish "not yet restarted" from "out of sync" — this function has
    no way to see across a process-restart boundary (it only ever reads
    the current state of two local files), so it only ever answers
    CONVERGED or one of the disagreement shapes below. Phase 4's
    ``/neuroloom:status`` orchestration, which does know whether a restart
    has happened since the last write, is responsible for choosing which
    of the two user-facing strings ("changed, restart to apply" vs. "out
    of sync") to show for a non-CONVERGED result — this function
    structurally cannot make that claim itself (NFR-Sec-4: no message
    claims something it cannot verify).
    """

    CONVERGED = auto()
    OUT_OF_SYNC = auto()
    UNOWNED_CONFLICT = auto()
    MALFORMED = auto()


class RoutingComparisonResult(NamedTuple):
    comparison: RoutingComparison
    desired: DesiredState
    observed: ObservedState


def compare_desired_vs_observed(project_root: str, db_path: Path) -> RoutingComparisonResult:
    """
    R2-6: network-free comparison of desired state (``.neuroloom.db``)
    against observed state (``.mcp.json``), for ``/neuroloom:status``'s
    post-restart check (Phase 4). Reads only local files on this
    machine — no bootstrap fetch, no ``urllib`` call of any kind, by
    construction (this function contains no ``urllib`` import usage
    whatsoever) — so this step can run independent of status's own
    live-connection-check step without itself making a second live call.

    A project whose current entry is unowned and non-absent is reported as
    ``UNOWNED_CONFLICT`` regardless of whether its header happens to equal
    the desired value — an unowned entry is never "converged" in the sense
    this deliverable cares about, because this plugin has no ability to
    keep it that way (FR-7: automatic paths never touch it, so any
    apparent agreement today is coincidental, not maintained).
    """
    desired = read_desired_state(db_path)
    try:
        observed = read_observed_state(project_root)
    except McpJsonConflictError:
        return RoutingComparisonResult(
            RoutingComparison.MALFORMED, desired, ObservedState(ObservedKind.ABSENT)
        )

    ownership = _read_ownership(db_path)
    if observed.kind is not ObservedKind.ABSENT and ownership is Ownership.UNOWNED:
        return RoutingComparisonResult(RoutingComparison.UNOWNED_CONFLICT, desired, observed)

    converged = (
        desired.kind in (DesiredKind.UNSET, DesiredKind.EXPLICITLY_CLEARED)
        and observed.kind in (ObservedKind.ABSENT, ObservedKind.HEADERLESS)
    ) or (
        desired.kind is DesiredKind.SET_TO_VALUE
        and observed.kind is ObservedKind.HEADERED_WITH_VALUE
        and observed.value == desired.value
    )
    if converged:
        return RoutingComparisonResult(RoutingComparison.CONVERGED, desired, observed)
    return RoutingComparisonResult(RoutingComparison.OUT_OF_SYNC, desired, observed)


# ===========================================================================
# D172 — FR-1 inventory (single pinned-shape call)
# ===========================================================================


class WorkspaceInventory(NamedTuple):
    """
    FR-1: the full routing-state inventory for the current project,
    returned as one pinned shape from ``inventory_workspace_state`` — every
    caller (the command flow, ``/neuroloom:init``, and this module's own
    tests) reads this same tuple rather than re-deriving any field
    independently, which is what keeps "single reconciliation entry point"
    (NFR-Arch-2) true in practice.

    ``observed`` is ``None`` exactly when ``observed_error`` is populated
    (a malformed ``.mcp.json`` — see ``read_observed_state``); the two are
    mutually exclusive and exactly one is always non-``None``.
    """

    desired: DesiredState
    ownership: Ownership
    observed: ObservedState | None
    observed_error: str | None
    settings_json_legacy_value: str | None
    settings_json_migration_done: bool
    resolvability: Resolvability
    resolved_workspace_id: str | None


def inventory_workspace_state(
    project_root: str, db_path: Path, api_base: str, api_key: str
) -> WorkspaceInventory:
    """
    FR-1: a single call returning desired state, ownership, ``.mcp.json``
    observed state, ``.claude/settings.json`` legacy-override state, and a
    *fresh* resolvability attempt result, together. Performs exactly one
    live resolvability round-trip (via ``check_resolvability`` — always
    fresh, matching the human-driven command flow this feeds) per call;
    every other field is a local read.
    """
    desired = read_desired_state(db_path)
    ownership = _read_ownership(db_path)

    observed: ObservedState | None
    observed_error: str | None
    try:
        observed = read_observed_state(project_root)
        observed_error = None
    except McpJsonConflictError as exc:
        observed = None
        observed_error = str(exc)

    legacy_value = _read_settings_json_legacy_value(project_root)
    migration_done = is_settings_json_migration_done(db_path)

    resolvability, resolved_workspace_id = check_resolvability(api_base, api_key)

    return WorkspaceInventory(
        desired=desired,
        ownership=ownership,
        observed=observed,
        observed_error=observed_error,
        settings_json_legacy_value=legacy_value,
        settings_json_migration_done=migration_done,
        resolvability=resolvability,
        resolved_workspace_id=resolved_workspace_id,
    )
