"""
Tests for hooks/cache_prefetch.py.

Covers the no-API-key guard: without a per-project key, the prefetch hook
must exit before opening the state DB at all -- mirroring
test_capture.py::TestNoApiKey::test_exits_without_db_open's shape (D171
Phase 3, round 5 "unchanged"), but WITHOUT copying its Config(...,
debug=False) construction -- Config has no debug field; that pattern is a
broken fixture causing most of this repo's pre-existing baseline failures
(D171 plan, Migration & Compatibility Story / Baseline section).
"""

import io
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import pyhooks.cache_prefetch as _cache_prefetch_mod
import pyhooks.config as _config_mod


class TestNoApiKey:
    """Without an API key, cache_prefetch exits immediately without touching
    the DB."""

    def test_exits_without_db_open(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)

        open_db_calls: list[Any] = []

        with (
            patch("pyhooks.cache_prefetch.load") as mock_load,
            patch("pyhooks.cache_prefetch.open_db") as mock_open_db,
            patch("pyhooks.cache_prefetch.sys.stdin", io.StringIO("{}")),
        ):
            mock_load.return_value = _config_mod.Config(
                api_key="",
                api_base="http://localhost:19999",
                state_db_path=tmp_path / ".neuroloom.db",
            )
            mock_open_db.side_effect = lambda path: open_db_calls.append(path)

            try:
                _cache_prefetch_mod.main()
            except SystemExit:
                pass

        assert open_db_calls == [], "open_db should not be called when no API key is configured"
