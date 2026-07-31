"""
Tests for hooks/config.py.

Verifies that ``load()`` reads configuration correctly (D171 / ADR-17: the
per-project ``.neuroloom.db`` row is the ONLY source of ``api_key`` --
``CLAUDE_PLUGIN_OPTION_API_KEY`` and ``NEUROLOOM_API_KEY`` are both dropped
from live routing), applies safe defaults when nothing is configured,
constructs ``state_db_path`` from the current working directory, and never
exposes the API key value in repr/str. Also covers ``load_project_api_key()``
and ``load_global_seed_key()`` (the seed-only reader), plus the three-surface
credential-isolation regression scan (D171 round 6 F-B, round 7 file-axis
fix, round 8 NameError fix + scripts/skills extension, round 9 CRITICAL-2
docstring self-fail correction).
"""

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

        Note: ``Config`` is a plain frozen dataclass -- ``repr()`` includes all
        fields including ``api_key``.  Masking would require a custom ``__repr__``.
        This test verifies the field is populated correctly; if masking is added
        in the future this test should be updated accordingly.
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
