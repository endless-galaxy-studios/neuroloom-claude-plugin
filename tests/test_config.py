"""
Tests for hooks/config.py.

Verifies that ``load()`` reads configuration correctly (D171 / ADR-17: the
per-project ``.neuroloom.db`` row is the ONLY source of ``api_key`` --
``CLAUDE_PLUGIN_OPTION_API_KEY`` and ``NEUROLOOM_API_KEY`` are both dropped
from live routing), applies safe defaults when nothing is configured,
constructs ``state_db_path`` from the project root (D178d), and never
exposes the API key value in repr/str. Also covers ``load_project_api_key()``
and ``load_global_seed_key()`` (the seed-only reader), plus the three-surface
credential-isolation regression scan (D171 round 6 F-B, round 7 file-axis
fix, round 8 NameError fix + scripts/skills extension, round 9 CRITICAL-2
docstring self-fail correction).
"""

import re
import sqlite3
from pathlib import Path

import pytest

import pyhooks  # noqa: F401 -- needed for pyhooks.__file__ below (D171 round 8, MAJOR-12: an
                 # earlier revision used pyhooks.__file__ without ever importing pyhooks itself
                 # -- test_config.py imports only pyhooks.config as _config_mod, so the bare
                 # name was never bound and this test raised NameError as written)
import pyhooks.config as _config_mod


def _seed_neuroloom_db(db_path: Path, api_key: str | None = None) -> None:
    """Create a minimal .neuroloom.db with a config table, optionally seeded
    with an api_key row -- mirrors the schema pyhooks.db.open_db() creates."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    if api_key is not None:
        conn.execute(
            "INSERT INTO config (key, value) VALUES ('api_key', ?)", (api_key,)
        )
    conn.commit()
    conn.close()


class TestConfigLoad:
    """Tests for the ``load()`` factory function."""

    def test_load_returns_config_with_all_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``load()`` populates ``api_key`` from a genuine per-project
        .neuroloom.db row (D171 round 7 -- rewritten from the old
        CLAUDE_PLUGIN_OPTION_API_KEY-sourced version, which collided with
        the new contract: that env var is no longer consulted by load())."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("NEUROLOOM_API_BASE", "https://custom.api.example.com")
        _seed_neuroloom_db(tmp_path / ".neuroloom.db", api_key="my-secret-key")

        cfg = _config_mod.load()

        assert cfg.api_key == "my-secret-key"
        assert cfg.api_base == "https://custom.api.example.com"
        assert isinstance(cfg.state_db_path, Path)
        assert cfg.api_key_source == "project"

    def test_load_returns_defaults_when_env_vars_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``load()`` never raises and returns safe defaults for missing vars."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_API_KEY", raising=False)
        monkeypatch.delenv("NEUROLOOM_API_KEY", raising=False)
        monkeypatch.delenv("NEUROLOOM_API_BASE", raising=False)

        cfg = _config_mod.load()

        assert cfg.api_key == ""
        assert cfg.api_base == "https://api.neuroloom.dev"
        assert isinstance(cfg.state_db_path, Path)
        assert cfg.api_key_source == "none"

    def test_load_resolves_api_key_only_from_project_db_pin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """D171 F-A pinning test 1 (required): with CLAUDE_PLUGIN_OPTION_API_KEY
        set to a non-empty value and no .neuroloom.db present, load() resolves
        api_key == "" and api_key_source == "none" -- the global userConfig
        key is never live-routed, only ever offered as a seed by
        /neuroloom:init via load_global_seed_key()."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_API_KEY", "global-seed-value")
        assert not (tmp_path / ".neuroloom.db").exists()

        cfg = _config_mod.load()

        assert cfg.api_key == ""
        assert cfg.api_key_source == "none"

    def test_load_does_not_fall_back_to_neuroloom_api_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """D171 F-A pinning test 1b (required): NEUROLOOM_API_KEY is dropped
        entirely from the resolution chain -- with it set and no
        .neuroloom.db present, load() resolves api_key == "" and
        api_key_source == "none". This deletes the old fallback-tier
        contract (previously test_api_key_falls_back_to_neuroloom_api_key /
        test_claude_plugin_option_takes_precedence)."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_API_KEY", raising=False)
        monkeypatch.setenv("NEUROLOOM_API_KEY", "fallback-key")
        assert not (tmp_path / ".neuroloom.db").exists()

        cfg = _config_mod.load()

        assert cfg.api_key == ""
        assert cfg.api_key_source == "none"

    def test_state_db_path_uses_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``state_db_path`` is constructed from the process working directory."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_API_KEY", raising=False)
        monkeypatch.delenv("NEUROLOOM_API_KEY", raising=False)

        cfg = _config_mod.load()

        assert cfg.state_db_path == tmp_path / ".neuroloom.db"

    def test_api_key_stored_on_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The API key is accessible via the ``api_key`` field (used by callers to
        build ``Authorization`` headers), sourced from .neuroloom.db (D171
        round 7 -- rewritten the same way as
        test_load_returns_config_with_all_fields).

        Note: ``api_key`` is declared ``field(repr=False)`` (D178d), so ``repr()``
        never includes it; this test only verifies the field is populated.
        """
        monkeypatch.chdir(tmp_path)
        sentinel = "super-secret-token-xyz"
        _seed_neuroloom_db(tmp_path / ".neuroloom.db", api_key=sentinel)

        cfg = _config_mod.load()

        assert cfg.api_key == sentinel

    def test_api_key_source_only_ever_project_or_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """D171 F-A: api_key_source has exactly two possible values -- "project"
        when a per-project key is stored, "none" otherwise (including the
        no-api_key-row edge case: a .neuroloom.db file exists but has no
        api_key row at all)."""
        monkeypatch.chdir(tmp_path)

        # No .neuroloom.db at all.
        cfg_absent = _config_mod.load()
        assert cfg_absent.api_key_source in {"project", "none"}
        assert cfg_absent.api_key_source == "none"

        # .neuroloom.db exists but has no api_key row.
        _seed_neuroloom_db(tmp_path / ".neuroloom.db", api_key=None)
        cfg_no_row = _config_mod.load()
        assert cfg_no_row.api_key_source == "none"

        # Same file, now with a genuine api_key row inserted.
        conn = sqlite3.connect(str(tmp_path / ".neuroloom.db"))
        conn.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES ('api_key', ?)", ("k",)
        )
        conn.commit()
        conn.close()
        cfg_with_row = _config_mod.load()
        assert cfg_with_row.api_key_source == "project"

    def test_load_never_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``load()`` must not raise even if env vars contain unusual values.

        Null bytes cannot be set in OS environment variables (the OS rejects them
        at the system-call level), so we test with non-null garbage values instead.
        """
        monkeypatch.setenv("NEUROLOOM_API_BASE", "")
        # Should not raise
        cfg = _config_mod.load()
        assert cfg is not None


class TestLoadProjectApiKey:
    """D171 round 9, M5: load_project_api_key() -- the public, tested helper
    replacing hand-rolled SQL previously duplicated across SKILL.md's Step 5
    and Phase 4."""

    def test_returns_stored_value(self, tmp_path: Path) -> None:
        _seed_neuroloom_db(tmp_path / ".neuroloom.db", api_key="a-project-key")
        assert _config_mod.load_project_api_key(tmp_path) == "a-project-key"

    def test_returns_empty_string_when_file_does_not_exist(self, tmp_path: Path) -> None:
        assert not (tmp_path / ".neuroloom.db").exists()
        assert _config_mod.load_project_api_key(tmp_path) == ""

    def test_returns_empty_string_when_no_api_key_row(self, tmp_path: Path) -> None:
        _seed_neuroloom_db(tmp_path / ".neuroloom.db", api_key=None)
        assert _config_mod.load_project_api_key(tmp_path) == ""

    def test_returns_empty_string_on_read_failure_never_raises(self, tmp_path: Path) -> None:
        """A corrupt/non-SQLite file at .neuroloom.db must not raise --
        load_project_api_key() degrades to "" like _load_from_state_db()
        does."""
        (tmp_path / ".neuroloom.db").write_bytes(b"not a sqlite file")
        assert _config_mod.load_project_api_key(tmp_path) == ""


class TestLoadGlobalSeedKey:
    """D171: load_global_seed_key() is the sole sanctioned reader of
    CLAUDE_PLUGIN_OPTION_API_KEY -- exists only for /neuroloom:init (and its
    transitive callers) to offer the global value as a one-time seed. Never
    consulted by load()."""

    def test_returns_stripped_env_value_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_API_KEY", "  seed-value  ")
        assert _config_mod.load_global_seed_key() == "seed-value"

    def test_returns_empty_string_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_API_KEY", raising=False)
        assert _config_mod.load_global_seed_key() == ""

    def test_does_not_consult_neuroloom_db(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even with a per-project .neuroloom.db present and populated,
        load_global_seed_key() reads only the env var -- it never touches
        the state DB at all."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_API_KEY", "seed-value")
        _seed_neuroloom_db(tmp_path / ".neuroloom.db", api_key="project-key")

        assert _config_mod.load_global_seed_key() == "seed-value"


# ---------------------------------------------------------------------------
# Three-surface credential-isolation regression scan
# (D171 round 6 F-B; round 7 file-axis fix; round 8 NameError fix + MAJOR-4
# scripts/skills extension; round 9 CRITICAL-2 docstring self-fail fix)
# ---------------------------------------------------------------------------

_PYHOOKS_ISOLATION_SCAN_EXCEPTIONS = {"config.py", "__init__.py"}
# config.py is exempt: it defines load_global_seed_key() (the sanctioned
# reader) and legitimately contains both the function's own definition
# and the one CLAUDE_PLUGIN_OPTION_API_KEY read inside it. __init__.py
# is exempt as an empty package marker.


def test_seed_key_isolation_from_hook_path() -> None:
    """D171 regression guard (round 6 F-B; round 7 file-axis fix)."""
    pkg_dir = Path(pyhooks.__file__).parent
    scan_paths = sorted(
        p for p in pkg_dir.glob("*.py") if p.name not in _PYHOOKS_ISOLATION_SCAN_EXCEPTIONS
    )
    assert scan_paths, "isolation scan found zero files -- pyhooks package layout changed unexpectedly"
    for path in scan_paths:
        name = path.name
        source = path.read_text(encoding="utf-8")
        assert "load_global_seed_key" not in source, f"{name} must never reference load_global_seed_key"
        occurrences = source.count("CLAUDE_PLUGIN_OPTION_API_KEY")
        if name == "session_start.py":
            assert occurrences == 1, (
                f"session_start.py must contain EXACTLY ONE occurrence "
                f"of CLAUDE_PLUGIN_OPTION_API_KEY (the allowlisted "
                f"dormant-message presence check) — found {occurrences}."
            )
        else:
            assert occurrences == 0, f"{name} must never reference CLAUDE_PLUGIN_OPTION_API_KEY directly"


def test_seed_key_isolation_from_scripts() -> None:
    """D171 round 8, MAJOR-4 -- extends the isolation guard to scripts/,
    which the ADR's own Forbids clause already claimed to cover but no
    test enforced. Zero tolerance, no exception: after item 13's fix,
    scripts/seed_code_graph.py must reference neither the sanctioned
    reader nor the raw env-var literal at all."""
    repo_root = Path(pyhooks.__file__).parent.parent
    scripts_dir = repo_root / "scripts"
    scan_paths = sorted(scripts_dir.glob("*.py"))
    assert scan_paths, "isolation scan found zero files under scripts/"
    for path in scan_paths:
        source = path.read_text(encoding="utf-8")
        assert "load_global_seed_key" not in source, f"{path.name} must never reference load_global_seed_key"
        assert "CLAUDE_PLUGIN_OPTION_API_KEY" not in source, f"{path.name} must never reference CLAUDE_PLUGIN_OPTION_API_KEY"


def test_seed_key_isolation_from_skills() -> None:
    """D171 round 8, MAJOR-4 -- extends the isolation guard to every
    skills/**/SKILL.md file. load_global_seed_key() IS allowed here
    (Steps 2/3 of skills/init/SKILL.md are its sanctioned setup-flow
    callers) -- only the raw env-var literal is forbidden, and only
    achievable at all once Phase 2's Step 1 and Step 5 rewrites remove
    the two occurrences that exist in the live file today."""
    repo_root = Path(pyhooks.__file__).parent.parent
    skill_files = sorted((repo_root / "skills").glob("**/SKILL.md"))
    assert skill_files, "isolation scan found zero SKILL.md files"
    for path in skill_files:
        source = path.read_text(encoding="utf-8")
        assert "CLAUDE_PLUGIN_OPTION_API_KEY" not in source, (
            f"{path.relative_to(repo_root)} must never reference "
            f"CLAUDE_PLUGIN_OPTION_API_KEY directly -- use "
            f"load_global_seed_key() instead"
        )


# ---------------------------------------------------------------------------
# D172 Phase 5 — NFR-Arch-2 fitness function: single-reconciliation-
# entry-point predicate (R2-2 / MINOR 2, corrected mechanism)
# ---------------------------------------------------------------------------
#
# NFR-Arch-2 requires that all `.mcp.json` reads/writes/reconciliation logic
# live in `pyhooks/workspace_config.py`'s public functions -- `SKILL.md`
# files and `scripts/*.py` must never hand-roll JSON manipulation of
# `.mcp.json`/`.claude/settings.json` themselves. A naive whole-file
# substring grep for the literal filenames (the technique D171's existing
# `_PYHOOKS_ISOLATION_SCAN_EXCEPTIONS`-style scans above use for a *single*
# env-var literal) would false-positive here: this deliverable's own
# mandated user-facing copy legitimately mentions ".mcp.json" dozens of
# times in prose (error messages, SKILL.md instructions) with no file
# operation anywhere nearby.
#
# Corrected technique (R2-2), staying in the same grep idiom as the
# existing scans above -- explicitly NOT AST, NOT dataflow analysis:
# **same-file, one-logical-line co-occurrence**. A violation is a single
# line (or a short call chain joined by unbalanced parentheses -- see
# `_join_logical_lines` below) that contains BOTH a literal `.mcp.json` or
# `settings.json` filename token AND a file-operation call (`open(`,
# `.read_text(`, `.write_text(`, `json.load(`, `json.dump(` for Python;
# `jq`/`sed`/`cat`/a redirection operator for shell) on that *same* line.
# This does not trace whether an `open(...)` call's argument variable
# actually originated from a `Path(...) / ".mcp.json"` built on some
# earlier line -- that is a strictly narrower, cheaper check than dataflow
# tracing, and it is exactly how every real violation in this codebase (and
# every violation this deliverable's own mandated code needs to avoid) is
# actually written. See `test_multi_statement_evasion_is_an_accepted_
# residual` below for the one shape this deliberately does not catch.

_MCP_ARCH_PATH_TOKEN_RE = re.compile(r"\.mcp\.json|settings\.json")
_MCP_ARCH_FILE_OP_RE = re.compile(
    r"\bopen\(|\.read_text\(|\.write_text\(|json\.load\(|json\.dump\("
    r"|\bjq\b|\bsed\b|\bcat\b"
    # Shell redirection: `>`/`>>` only counts when preceded by whitespace,
    # so it matches real redirects (`cat foo > bar`) but not an angle
    # bracket immediately after a word -- e.g. the `<uuid>` placeholder
    # convention used throughout this deliverable's own verbatim message
    # copy (`header=<uuid>`), which would otherwise false-positive this
    # scan against its own mandated prose (the exact failure mode R2-2
    # exists to avoid).
    r"|(?<=\s)>>?(?!=)"
)

# Files this scan intentionally never runs against. `workspace_config.py`
# is NFR-Arch-2's single sanctioned reconciliation module -- it is SUPPOSED
# to construct `.mcp.json`/`settings.json` paths and call file operations on
# them; scanning it would be scanning the thing NFR-Arch-2 exists to
# protect, not a violation of it.
_MCP_ARCH_SCAN_EXCLUDED_PYHOOKS_FILES = {"workspace_config.py", "__init__.py"}

# Named allowlist of pre-existing, out-of-scope exceptions (D171-era or
# earlier), following the `_PYHOOKS_ISOLATION_SCAN_EXCEPTIONS` precedent
# above -- they are inline SQL against `.neuroloom.db`'s tables, a
# related-but-distinct NFR-Arch-2 concern ("no ad hoc JSON-editing code"
# also implicitly covers "no ad hoc SQL"), not this scan's
# `.mcp.json`/`settings.json` axis.
#
# Review finding (D172 completion review, Finding #4): this allowlist used
# to `continue`-skip the ENTIRE file for any name listed here, on the
# claim that the scan's predicate "never matches either of these two
# anyway." That claim went stale the moment a *different*, unrelated line
# was added to one of these files (skills/status/SKILL.md's step 1a, which
# reads `.mcp.json` directly) -- a whole-file skip silently stopped
# scanning that new line too, which is exactly the blind spot a named
# allowlist exists to avoid. The allowlist is now scoped to the specific
# pre-existing exception lines themselves (matched by substring against a
# found violation, not by filename), so everything else in both files --
# including any future line -- is still scanned. Named (not filename-keyed
# only) so a future reviewer does not read a clean run of THIS scan as
# proof no ad hoc DB/JSON manipulation remains in SKILL.md anywhere -- only
# that the `.mcp.json`/`settings.json` axis this deliverable touches is
# clean.
_SKILL_MD_SCAN_LINE_EXCEPTIONS: dict[str, tuple[str, ...]] = {
    "skills/init/SKILL.md": (
        # hand-rolled api_key INSERT OR REPLACE against .neuroloom.db's
        # config table (Step 3's paste-path and use-default python -c
        # payloads) -- pre-D172, out of scope for this JSON-file-only scan.
        # Repayment trigger: if NFR-Arch-2 is ever extended to cover
        # .neuroloom.db SQL construction, replace with a sanctioned
        # config-write helper in pyhooks/config.py or workspace_config.py.
        "INSERT OR REPLACE INTO config",
    ),
    "skills/status/SKILL.md": (
        # inline session/buffer-count queries via the sqlite3 CLI directly
        # against .neuroloom.db (steps 2 and 3) -- pre-D172, out of scope
        # for this JSON-file-only scan. Repayment trigger: same as above.
        'SELECT session_id, started_at FROM sessions',
        'SELECT COUNT(*) FROM event_buffer',
    ),
}


def _is_allowlisted_line(rel_path: str, violation_line: str) -> bool:
    """
    True when *violation_line* matches one of *rel_path*'s named,
    line-scoped exceptions above. Substring match against the exception
    text, not a whole-file skip -- every other line in the file is still
    scanned regardless of whether this file has any exceptions at all.
    """
    return any(
        pattern in violation_line for pattern in _SKILL_MD_SCAN_LINE_EXCEPTIONS.get(rel_path, ())
    )


def _join_logical_lines(source: str) -> list[str]:
    """
    Join consecutive physical lines into "logical lines" while parenthesis
    depth is unbalanced -- a cheap stand-in for "at most one logical
    statement, before any semicolon or line-continuation ends" that stays
    in the grep idiom (no AST). A call chain like::

        data = json.load(
            open(Path(root) / ".mcp.json")
        )

    becomes one joined line so the co-occurrence check still fires even
    when the call is wrapped across lines by a formatter. This is a
    heuristic, not a parser -- it does not understand strings/comments
    containing unbalanced parens -- but every real violation shape in this
    codebase is plain code, not paren-containing string literals.
    """
    lines = source.splitlines()
    logical: list[str] = []
    buffer = ""
    depth = 0
    for line in lines:
        buffer = f"{buffer} {line}" if buffer else line
        depth += line.count("(") - line.count(")")
        if depth <= 0:
            logical.append(buffer)
            buffer = ""
            depth = 0
    if buffer:
        logical.append(buffer)
    return logical


def _scan_source_for_mcp_arch_violations(source: str) -> list[str]:
    """
    Return the logical lines (if any) that co-occur a `.mcp.json`/
    `settings.json` filename token with a file-operation call. Used
    identically for Python source and for the joined text of a single
    SKILL.md fenced code block.
    """
    violations = []
    for logical_line in _join_logical_lines(source):
        if _MCP_ARCH_PATH_TOKEN_RE.search(logical_line) and _MCP_ARCH_FILE_OP_RE.search(
            logical_line
        ):
            violations.append(logical_line.strip())
    return violations


_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)


def _extract_fenced_code_blocks(markdown_source: str) -> list[str]:
    """
    Every fenced code block's contents, regardless of declared language
    (MINOR 3, infosec) -- a bash block using `jq`/`sed`/`cat`/redirection
    against `.mcp.json`/`.claude/settings.json` directly is exactly as
    forbidden as a Python one, since the boundary NFR-Arch-2 protects is
    "which file gets touched," not "which language touches it." Prose
    outside fences (before, after, or between them) is never scanned,
    regardless of what filenames it mentions.
    """
    return _FENCE_RE.findall(markdown_source)


def _scan_pyhooks_and_scripts_for_mcp_arch_violations(repo_root: Path) -> dict[str, list[str]]:
    violations: dict[str, list[str]] = {}
    pkg_dir = repo_root / "pyhooks"
    scripts_dir = repo_root / "scripts"
    candidates = [
        p for p in sorted(pkg_dir.glob("*.py")) if p.name not in _MCP_ARCH_SCAN_EXCLUDED_PYHOOKS_FILES
    ] + sorted(scripts_dir.glob("*.py"))
    for path in candidates:
        found = _scan_source_for_mcp_arch_violations(path.read_text(encoding="utf-8"))
        if found:
            violations[str(path.relative_to(repo_root))] = found
    return violations


def _scan_skill_md_for_mcp_arch_violations(repo_root: Path) -> dict[str, list[str]]:
    violations: dict[str, list[str]] = {}
    for path in sorted((repo_root / "skills").glob("**/SKILL.md")):
        rel = str(path.relative_to(repo_root))
        source = path.read_text(encoding="utf-8")
        found = []
        for block in _extract_fenced_code_blocks(source):
            found.extend(_scan_source_for_mcp_arch_violations(block))
        found = [line for line in found if not _is_allowlisted_line(rel, line)]
        if found:
            violations[rel] = found
    return violations


def test_fitness_function_clean_against_current_repo() -> None:
    """
    NFR-Arch-2's real gate: zero `.mcp.json`/`settings.json` construction +
    file-operation co-occurrences anywhere in `pyhooks/*.py` (outside
    `workspace_config.py`), `scripts/*.py`, or any `skills/**/SKILL.md`
    fenced code block. Correctly does NOT flag this deliverable's own
    mandated message copy or SKILL.md prose (see the mutation-proof tests
    below for the positive-catch side of that claim).
    """
    repo_root = Path(pyhooks.__file__).parent.parent

    py_violations = _scan_pyhooks_and_scripts_for_mcp_arch_violations(repo_root)
    assert not py_violations, f"NFR-Arch-2 violations found: {py_violations}"

    skill_violations = _scan_skill_md_for_mcp_arch_violations(repo_root)
    assert not skill_violations, f"NFR-Arch-2 violations found: {skill_violations}"


def test_skill_md_allowlist_is_line_scoped_not_file_scoped(tmp_path: Path) -> None:
    """
    D172 completion review, Finding #4: the two named SKILL.md exceptions
    (skills/init/SKILL.md's INSERT OR REPLACE, skills/status/SKILL.md's
    inline session/buffer-count sqlite3 queries) must not disable scanning
    of the rest of either file. Proven with a synthetic fixture (same
    tmp_path-copy technique as the mutation-style tests below) that
    reproduces one named exception line verbatim inside
    skills/status/SKILL.md, alongside a genuine, unrelated
    `.mcp.json`-reading violation the scan has never seen before -- if the
    allowlist were still file-scoped, both would be silently skipped; with
    the line-scoped allowlist, only the named exception is suppressed and
    the new violation is still caught.
    """
    skills_dir = tmp_path / "skills" / "status"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text(
        "---\nname: status\n---\n\n"
        "```bash\n"
        # The named, allowlisted pre-existing exception line, reproduced
        # verbatim -- must NOT be flagged.
        'sqlite3 .neuroloom.db "SELECT session_id, started_at FROM sessions LIMIT 1;"\n'
        # A brand-new, unrelated violation on the .mcp.json axis this scan
        # actually exists to catch -- MUST be flagged.
        "cat .mcp.json > /tmp/leak.json\n"
        "```\n",
        encoding="utf-8",
    )

    found = _scan_skill_md_for_mcp_arch_violations(tmp_path)

    assert "skills/status/SKILL.md" in found, (
        "the new .mcp.json violation was not caught -- the allowlist is "
        "silently skipping the whole file again"
    )
    joined = "".join(found["skills/status/SKILL.md"])
    assert "cat .mcp.json" in joined
    assert "SELECT session_id" not in joined, (
        "the named pre-existing exception line was incorrectly flagged"
    )


# ---------------------------------------------------------------------------
# Mutation-style proof (MINOR 3, infosec): three distinct reintroduction
# shapes, each run against an isolated synthetic fixture (never the real
# repo tree -- mutating real source files inside an automated test is a
# needless operational risk if the test crashes mid-run; running the exact
# same scan function against a throwaway `tmp_path` copy proves the same
# thing about the scan's *logic* without that risk) -- confirmed caught,
# alongside the one accepted-residual shape confirmed NOT caught.
# ---------------------------------------------------------------------------


def test_mutation_shape_a_path_joined_violation_is_caught(tmp_path: Path) -> None:
    """(a) A `Path(...)`-joined violation outside `workspace_config.py`."""
    victim = tmp_path / "pyhooks"
    victim.mkdir()
    (victim / "some_hook.py").write_text(
        'from pathlib import Path\n'
        'def f(root):\n'
        '    return open(Path(root) / ".mcp.json").read()\n',
        encoding="utf-8",
    )
    found = _scan_pyhooks_and_scripts_for_mcp_arch_violations(tmp_path)
    assert found, "Path(...)-joined violation was not caught"


def test_mutation_shape_b_os_path_join_fstring_violation_is_caught(tmp_path: Path) -> None:
    """(b) An `os.path.join`/f-string/string-concatenation-built violation
    (proves the scan isn't Path-syntax-specific)."""
    victim = tmp_path / "scripts"
    victim.mkdir()
    (victim / "some_script.py").write_text(
        'import os, json\n'
        'def f(root):\n'
        '    return json.load(open(os.path.join(root, "settings.json")))\n',
        encoding="utf-8",
    )
    found = _scan_pyhooks_and_scripts_for_mcp_arch_violations(tmp_path)
    assert found, "os.path.join-built violation was not caught"

    victim2 = tmp_path / "pyhooks"
    victim2.mkdir(exist_ok=True)
    (victim2 / "another_hook.py").write_text(
        'def g(root):\n'
        # `def g` is a SEPARATE-line variant of the multi-statement
        # evasion shape (D172 completion review, MINOR 2 -- an inline
        # comment here used to claim a same-line concatenation catch that
        # never actually happens: the token line below has no file-op
        # call, and the file-op line has no literal filename token, so
        # neither line trips the co-occurrence predicate on its own).
        # Kept alongside `def h` below deliberately, as a second instance
        # of the accepted-residual shape `test_multi_statement_evasion_is_
        # an_accepted_residual` documents further down -- proven NOT
        # caught by the assertion below, not claimed as a catch.
        '    path = root + "/.mcp.json"\n'
        '    return open(path + "").read()\n'
        '\n'
        'def h(root):\n'
        '    return open(f"{root}/.mcp.json").read()\n',
        encoding="utf-8",
    )
    found2 = _scan_pyhooks_and_scripts_for_mcp_arch_violations(tmp_path)
    assert any("f\"{root}/.mcp.json\"" in "".join(v) for v in found2.values()), (
        "f-string-built violation was not caught"
    )
    assert not any('path + ""' in "".join(v) for v in found2.values()), (
        "def g's separate-line evasion was unexpectedly caught -- either the "
        "scan changed, or this fixture/comment needs updating"
    )


def test_mutation_shape_c_skill_md_bash_fence_violation_is_caught(tmp_path: Path) -> None:
    """(c) A non-Python violation in a SKILL.md fenced code block -- a bash
    snippet using jq/sed/cat/redirection against .mcp.json or
    .claude/settings.json directly."""
    skills_dir = tmp_path / "skills" / "rogue"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text(
        "---\nname: rogue\n---\n\n"
        "Some prose mentioning .mcp.json with no code nearby.\n\n"
        "```bash\n"
        "jq '.mcpServers.neuroloom.headers[\"X-Workspace-Id\"] = \"x\"' .mcp.json > tmp.json\n"
        "```\n",
        encoding="utf-8",
    )
    found = _scan_skill_md_for_mcp_arch_violations(tmp_path)
    assert found, "bash-fence jq/.mcp.json violation was not caught"


def test_multi_statement_evasion_is_an_accepted_residual(tmp_path: Path) -> None:
    """
    A violation deliberately split across multiple statements to evade the
    same-line co-occurrence check is NOT caught by this scan -- an accepted
    gap of the chosen regex-idiom approach (closing it would require
    AST/dataflow analysis, out of scope for this deliverable's grep-idiom
    tooling). This is a PASS condition for this test, not a bug in it: the
    assertion below documents the residual exists and stays uncaught,
    rather than silently discovering it years later when someone assumes
    the scan is airtight.
    """
    victim = tmp_path / "pyhooks"
    victim.mkdir()
    (victim / "sneaky_hook.py").write_text(
        'from pathlib import Path\n'
        'def f(root):\n'
        '    p = Path(root) / ".mcp.json"\n'  # construction, its own statement
        '    return p.open().read()\n',  # file-op, DIFFERENT statement/line, no
        # literal filename token on this line at all
        encoding="utf-8",
    )
    found = _scan_pyhooks_and_scripts_for_mcp_arch_violations(tmp_path)
    assert not found, (
        "the multi-statement evasion shape was unexpectedly caught -- "
        "either the scan changed, or this test's fixture needs updating"
    )


# ---------------------------------------------------------------------------
# D178d Phase 2: mode switch, Config additions, load() reporting, save_project_api_key
#
# New names (parse_mode, ModeParse, save_project_api_key) are imported INSIDE the
# test bodies so a missing name is a red test, not a collection error.  Every
# assertion message is distinct so `-rf` lines are unambiguous.
# ---------------------------------------------------------------------------

# Spec §6.2 "Shared mode-parse table" (d178d_base_plugin_ci_mode_spec.md), copied
# verbatim: the eleven NEUROLOOM_MODE inputs.  (raw, ci, message) where "message"
# is whether the one unrecognised-mode stderr line is printed -- i.e. NOT recognised.
_MODE_TABLE_SPEC_6_2: list[tuple[str | None, bool, bool]] = [
    (None, False, False),        # unset
    ("", False, False),          # ""
    ("   ", False, False),       # "   "
    ("ci", True, False),         # ci
    ("CI", True, False),         # CI
    (" ci ", True, False),       # " ci "
    ("prod", True, True),        # prod
    ("ci2", True, True),         # ci2
    ("cii", True, True),         # cii
    ("off", True, True),         # off
    ("false", True, True),       # false
]


class TestParseMode:
    def test_the_table_has_the_eleven_inputs_of_spec_6_2(self) -> None:
        assert len(_MODE_TABLE_SPEC_6_2) == 11, "mode-table: spec §6.2 lists eleven inputs"

    @pytest.mark.parametrize(("raw", "ci", "message"), _MODE_TABLE_SPEC_6_2, ids=repr)
    def test_parse_mode_matches_spec_6_2(self, raw: str | None, ci: bool, message: bool) -> None:
        from pyhooks.config import parse_mode  # new name: imported inside the body

        result = parse_mode(raw)
        assert result.ci is ci, f"parse-mode/ci: wrong CI flag for NEUROLOOM_MODE={raw!r}"
        assert result.recognised is (not message), (
            f"parse-mode/recognised: wrong message decision for NEUROLOOM_MODE={raw!r}"
        )

    def test_config_built_without_ci_mode_defaults_to_false(self, tmp_path: Path) -> None:
        cfg = _config_mod.Config(api_key="k", api_base="http://x", state_db_path=tmp_path / ".neuroloom.db")
        assert getattr(cfg, "ci_mode", "absent") is False, "config-default: Config.ci_mode must default to False"

    def test_load_sets_ci_mode_from_the_environment_on_every_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("NEUROLOOM_MODE", "ci")
        assert getattr(_config_mod.load(), "ci_mode", None) is True, "load-ci-mode: NEUROLOOM_MODE=ci must set ci_mode"
        monkeypatch.setenv("NEUROLOOM_MODE", "prod")
        assert getattr(_config_mod.load(), "ci_mode", None) is True, (
            "load-ci-mode: an unrecognised non-empty mode also means CI"
        )
        monkeypatch.setenv("NEUROLOOM_MODE", "  ")
        assert getattr(_config_mod.load(), "ci_mode", None) is False, (
            "load-ci-mode: a blank mode is normal and the flag is re-read on every call"
        )
        monkeypatch.delenv("NEUROLOOM_MODE")
        assert getattr(_config_mod.load(), "ci_mode", None) is False, "load-ci-mode: unset is normal"

    def test_repr_and_str_of_config_do_not_contain_the_key(self, tmp_path: Path) -> None:
        cfg = _config_mod.Config(
            api_key="repr-leak-sentinel-key", api_base="http://x", state_db_path=tmp_path / ".neuroloom.db"
        )
        assert "repr-leak-sentinel-key" not in repr(cfg), "repr-hides-key: repr(Config) leaked the API key"
        assert "repr-leak-sentinel-key" not in str(cfg), "repr-hides-key: str(Config) leaked the API key"


def _snapshot(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


class TestLoadReporting:
    """A project with no usable key is dormant: source none, empty key, nothing created, nothing logged."""

    @pytest.fixture()
    def at_root(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        root = tmp_path.resolve()
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
        monkeypatch.chdir(root)
        return root

    def test_missing_file_reports_none_and_creates_nothing(
        self, at_root: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        before = _snapshot(at_root)
        with caplog.at_level("DEBUG"):
            cfg = _config_mod.load()
        assert (cfg.api_key_source, cfg.api_key) == ("none", ""), "load-missing: a missing database must report none"
        assert _snapshot(at_root) == before, "load-missing: load() must not create any file"
        assert [r for r in caplog.records if r.levelno >= 30] == [], "load-missing: no WARNING record expected"

    def test_directory_at_the_database_path_reports_none_quietly(
        self, at_root: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        (at_root / ".neuroloom.db").mkdir()
        before = _snapshot(at_root)
        with caplog.at_level("DEBUG"):
            cfg = _config_mod.load()
        assert (cfg.api_key_source, cfg.api_key) == ("none", ""), "load-directory: a directory must report none"
        assert _snapshot(at_root) == before, "load-directory: load() must not create any file"
        assert (at_root / ".neuroloom.db").is_dir(), "load-directory: the directory must be left alone"
        assert [r for r in caplog.records if r.levelno >= 30] == [], "load-directory: no WARNING record expected"

    def test_corrupt_file_reports_none_quietly_and_leaves_it_untouched(
        self, at_root: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        db = at_root / ".neuroloom.db"
        db.write_bytes(b"this is not a sqlite database at all")
        before_bytes, before = db.read_bytes(), _snapshot(at_root)
        with caplog.at_level("DEBUG"):
            cfg = _config_mod.load()
        assert (cfg.api_key_source, cfg.api_key) == ("none", ""), "load-corrupt: a corrupt file must report none"
        assert db.read_bytes() == before_bytes, "load-corrupt: the corrupt file must not be modified"
        assert _snapshot(at_root) == before, "load-corrupt: no sidecar or other file may appear"
        assert [r for r in caplog.records if r.levelno >= 30] == [], "load-corrupt: no WARNING record expected"

    def test_corrupt_file_prints_nothing_on_stderr_in_a_child_process(self, at_root: Path) -> None:
        """No logging handler is configured in a hook, so a warning would reach stderr via the last-resort handler."""
        import subprocess
        import sys

        (at_root / ".neuroloom.db").write_bytes(b"this is not a sqlite database at all")
        plugin_root = Path(__file__).resolve().parent.parent
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(at_root),
            "PYTHONPATH": str(plugin_root),
            "PYTHONDONTWRITEBYTECODE": "1",
            "CLAUDE_PROJECT_DIR": str(at_root),
        }
        from tests import ci_support

        # The editable install maps pyhooks to the MAIN checkout; prove this child imports the worktree's.
        ci_support.assert_import_origin(sys.executable, plugin_root, env, at_root)
        done = subprocess.run(
            [sys.executable, "-c", "import pyhooks.config as c; c.load()"],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(at_root),
            timeout=30,
            check=False,
        )
        assert done.returncode == 0, "load-stderr: the child process must exit cleanly"
        assert done.stderr == "", "load-stderr: load() printed to stderr for a corrupt database"

    def test_deleted_current_directory_gives_a_dormant_config_not_an_exception(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gone = tmp_path / "gone"
        gone.mkdir()
        monkeypatch.chdir(gone)
        gone.rmdir()
        cfg = _config_mod.load()
        assert (cfg.api_key_source, cfg.api_key) == ("none", ""), "load-deleted-cwd: must return a dormant config"
        import pyhooks.db as _db

        assert _db.open_db(cfg.state_db_path) is None, (
            "load-deleted-cwd: the dormant state path must be one that can never be opened"
        )
        assert not cfg.state_db_path.exists(), "load-deleted-cwd: the dormant state path must not exist afterwards"

    def test_readable_key_reports_project(self, at_root: Path) -> None:
        _seed_neuroloom_db(at_root / ".neuroloom.db", api_key="readable-key")
        cfg = _config_mod.load()
        assert (cfg.api_key_source, cfg.api_key) == ("project", "readable-key"), (
            "load-readable: a readable key must report project"
        )

    def test_readable_key_is_found_at_the_root_after_cd_into_a_subdirectory(
        self, at_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_neuroloom_db(at_root / ".neuroloom.db", api_key="root-held-key")
        sub = at_root / "sub"
        sub.mkdir()
        monkeypatch.chdir(sub)
        cfg = _config_mod.load()
        assert cfg.api_key == "root-held-key", "load-root-pin: the key must come from the root database after cd sub"
        assert cfg.state_db_path == at_root / ".neuroloom.db", (
            "load-root-pin: state_db_path must be the root database, not the cwd's"
        )


class TestSaveProjectApiKey:
    def test_key_written_to_an_empty_directory_is_read_back(self, tmp_path: Path) -> None:
        from pyhooks.config import save_project_api_key  # new name: imported inside the body

        target = tmp_path / "empty"
        target.mkdir()
        save_project_api_key(target, "first-key")
        assert _config_mod.load_project_api_key(target) == "first-key", (
            "save-key: a key written into an empty directory must be read back"
        )

    def test_a_second_write_replaces_the_key(self, tmp_path: Path) -> None:
        from pyhooks.config import save_project_api_key  # new name: imported inside the body

        target = tmp_path / "empty"
        target.mkdir()
        save_project_api_key(target, "first-key")
        save_project_api_key(target, "second-key")
        assert _config_mod.load_project_api_key(target) == "second-key", (
            "save-key-replace: a second write must replace the first key"
        )
        conn = sqlite3.connect(target / ".neuroloom.db")
        try:
            rows = conn.execute("SELECT COUNT(*) FROM config WHERE key = 'api_key'").fetchone()
        finally:
            conn.close()
        assert rows == (1,), "save-key-replace: exactly one api_key row must remain"

    def test_a_missing_target_directory_is_swallowed_and_nothing_is_created(self, tmp_path: Path) -> None:
        from pyhooks.config import save_project_api_key  # new name: imported inside the body

        missing = tmp_path / "does-not-exist"
        result = save_project_api_key(missing, "lost-key")
        assert result is None, "save-key-missing-dir: the call must return None, not report success or failure"
        assert not missing.exists(), "save-key-missing-dir: the missing directory must not be created"
        assert list(tmp_path.iterdir()) == [], "save-key-missing-dir: nothing at all may appear beside it"

    def test_a_failing_write_is_swallowed_and_the_connection_is_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """open_db hands back a database with no config table: the INSERT raises inside the function."""
        from pyhooks.config import save_project_api_key  # new name: imported inside the body

        opened: list[sqlite3.Connection] = []

        def _bare_open_db(_path: Path) -> sqlite3.Connection:
            conn = sqlite3.connect(tmp_path / "bare.db")
            opened.append(conn)
            return conn

        monkeypatch.setattr(_config_mod, "open_db", _bare_open_db)
        assert save_project_api_key(tmp_path, "k") is None, (
            "save-key-insert-fails: the call must return None when the INSERT raises"
        )
        assert len(opened) == 1, "save-key-insert-fails: open_db must have been used exactly once"
        with pytest.raises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")  # a closed connection refuses all use
