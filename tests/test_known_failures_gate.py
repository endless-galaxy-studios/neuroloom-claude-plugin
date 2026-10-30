"""Self-tests for the known-failures gate (tests/known_failures_gate.py)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests import ci_support
from tests import known_failures_gate as gate

_BASELINE = "# comment\n\ntests/a.py::test_one\ntests/b.py::test_two\n"
_GOOD = (
    "FAILED tests/a.py::test_one - AssertionError\n"
    "ERROR tests/b.py::test_two - boom\n"
    "2 failed, 10 passed, 1 skipped in 3.21s\n"
)


def test_baseline_ignores_comments_and_blanks() -> None:
    assert gate.baseline_ids(_BASELINE) == {"tests/a.py::test_one", "tests/b.py::test_two"}


def test_listed_failures_with_a_summary_line_pass() -> None:
    assert gate.check(_GOOD, gate.baseline_ids(_BASELINE)) == (True, [])


def test_all_green_run_passes() -> None:
    assert gate.check("12 passed in 1.00s\n", gate.baseline_ids(_BASELINE))[0] is True


def test_a_new_failure_is_reported_by_id() -> None:
    out = _GOOD + "FAILED tests/c.py::test_new - x\n"
    ok, problems = gate.check(out, gate.baseline_ids(_BASELINE))
    assert not ok and problems == ["tests/c.py::test_new"]


@pytest.mark.parametrize(
    "output",
    [
        "",
        "\n",
        "Traceback (most recent call last):\nImportError: boom\n",
        "ERROR: file or directory not found: tests/\n",
        "1 error in 0.40s\n",
        "no tests ran in 0.01s\n",
    ],
    ids=["empty", "blank", "crash", "usage-error", "collection-error-only", "no-tests"],
)
def test_empty_or_crashed_output_is_rejected(output: str) -> None:
    ok, problems = gate.check(output, gate.baseline_ids(_BASELINE))
    assert not ok, "a run without a summary line must not pass the gate"
    assert any("summary" in p for p in problems)


def test_command_line_rejects_empty_stdin_and_accepts_a_good_run(tmp_path: Path) -> None:
    baseline = tmp_path / "known_failures.txt"
    baseline.write_text(_BASELINE, encoding="utf-8")
    cmd = [sys.executable, "-m", "tests.known_failures_gate", str(baseline)]
    empty = subprocess.run(cmd, input="", capture_output=True, text=True, cwd=ci_support.PLUGIN_ROOT, check=False)
    assert empty.returncode == 1, "gate: empty input must exit 1"
    good = subprocess.run(cmd, input=_GOOD, capture_output=True, text=True, cwd=ci_support.PLUGIN_ROOT, check=False)
    assert good.returncode == 0, f"gate: a good run must exit 0, printed {good.stdout!r}"


def test_the_checked_in_baseline_has_only_real_test_ids() -> None:
    ids = gate.baseline_ids((ci_support.PLUGIN_ROOT / "known_failures.txt").read_text(encoding="utf-8"))
    assert ids and all(i.startswith("tests/") and "::" in i for i in ids)
