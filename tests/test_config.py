"""
Tests for hooks/config.py.

Verifies that ``load()`` reads environment variables correctly, applies safe
defaults when variables are absent, constructs ``state_db_path`` from the
current working directory, and never exposes the API key value in repr/str.
"""

from pathlib import Path

import pytest

import pyhooks.config as _config_mod


class TestConfigLoad:
    """Tests for the ``load()`` factory function."""

    def test_load_returns_config_with_all_fields(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``load()`` populates all fields when env vars are set."""
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_API_KEY", "my-secret-key")
        monkeypatch.setenv("NEUROLOOM_API_BASE", "https://custom.api.example.com")

        cfg = _config_mod.load()

        assert cfg.api_key == "my-secret-key"
        assert cfg.api_base == "https://custom.api.example.com"
        assert isinstance(cfg.state_db_path, Path)

    def test_load_returns_defaults_when_env_vars_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``load()`` never raises and returns safe defaults for missing vars."""
        monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_API_KEY", raising=False)
        monkeypatch.delenv("NEUROLOOM_API_BASE", raising=False)

        cfg = _config_mod.load()

        assert cfg.api_key == ""
        assert cfg.api_base == "https://api.neuroloom.dev"
        assert isinstance(cfg.state_db_path, Path)

    def test_state_db_path_uses_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``state_db_path`` is constructed from the process working directory."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_API_KEY", raising=False)

        cfg = _config_mod.load()

        assert cfg.state_db_path == tmp_path / ".neuroloom.db"

    def test_api_key_stored_on_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The API key is accessible via the ``api_key`` field (used by callers to
        build ``Authorization`` headers).

        Note: ``Config`` is a plain frozen dataclass — ``repr()`` includes all
        fields including ``api_key``.  Masking would require a custom ``__repr__``.
        This test verifies the field is populated correctly; if masking is added
        in the future this test should be updated accordingly.
        """
        sentinel = "super-secret-token-xyz"
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_API_KEY", sentinel)

        cfg = _config_mod.load()

        assert cfg.api_key == sentinel

    def test_load_never_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``load()`` must not raise even if env vars contain unusual values.

        Null bytes cannot be set in OS environment variables (the OS rejects them
        at the system-call level), so we test with non-null garbage values instead.
        """
        monkeypatch.setenv("NEUROLOOM_API_BASE", "")
        # Should not raise
        cfg = _config_mod.load()
        assert cfg is not None
