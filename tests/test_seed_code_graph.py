"""
Tests for scripts/seed_code_graph.py's ``_resolve_config()``.

D171 round 7, CRITICAL-3: this script previously reproduced the exact
credential-scope bug this deliverable exists to eliminate -- it fell back to
CLAUDE_PLUGIN_OPTION_API_KEY / NEUROLOOM_API_KEY, a global, project-agnostic
credential, for project-scoped code-graph POSTs. It now reads the per-project
key exclusively from _NL_KEY, an environment variable the invoking skill
passes in via env= (never printed).

``scripts/`` has no ``__init__.py`` (verified against the live tree) -- it
resolves as a PEP 420 implicit namespace package, importable the same way
``pyhooks`` (a regular package) already is from every existing test module,
since pytest's rootdir insertion already puts the repo root on sys.path
(D171 round 7, round 8 MINOR-l -- import line specified explicitly since
this is the first test module targeting scripts/).
"""

import pytest

import scripts.seed_code_graph as _seed_mod


class TestResolveConfigApiKey:
    """Mirrors test_config.py's pinning-test shape for _resolve_config()."""

    def test_resolves_from_nl_key_when_others_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With _NL_KEY set and neither CLAUDE_PLUGIN_OPTION_API_KEY nor
        NEUROLOOM_API_KEY set, _resolve_config() returns that value as
        api_key."""
        monkeypatch.setenv("_NL_KEY", "project-scoped-key")
        monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_API_KEY", raising=False)
        monkeypatch.delenv("NEUROLOOM_API_KEY", raising=False)

        api_key, _api_base = _seed_mod._resolve_config()

        assert api_key == "project-scoped-key"

    def test_ignores_global_tiers_when_nl_key_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """D171 round 7 CRITICAL-3 pin: with _NL_KEY unset but
        CLAUDE_PLUGIN_OPTION_API_KEY and/or NEUROLOOM_API_KEY set,
        _resolve_config() returns api_key == "" -- this script must never
        fall back to a globally-scoped credential tier for project-scoped
        traffic."""
        monkeypatch.delenv("_NL_KEY", raising=False)
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_API_KEY", "global-seed-value")
        monkeypatch.setenv("NEUROLOOM_API_KEY", "another-global-value")

        api_key, _api_base = _seed_mod._resolve_config()

        assert api_key == ""

    def test_ignores_global_tiers_when_nl_key_unset_only_claude_plugin_option(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("_NL_KEY", raising=False)
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_API_KEY", "global-seed-value")
        monkeypatch.delenv("NEUROLOOM_API_KEY", raising=False)

        api_key, _api_base = _seed_mod._resolve_config()

        assert api_key == ""


class TestResolveConfigApiBase:
    """api_base resolution is unaffected by this deliverable -- one smoke
    assertion suffices."""

    def test_api_base_defaults_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_API_BASE", raising=False)
        monkeypatch.delenv("NEUROLOOM_API_BASE", raising=False)

        _api_key, api_base = _seed_mod._resolve_config()

        assert api_base == "https://api.neuroloom.dev"
