"""
The known-failures gate, as a small tested program (standard library only).

Usage, from the plugin root (see the header of ``known_failures.txt``)::

    python -m pytest tests/ -q --tb=no -rfE | python -m tests.known_failures_gate known_failures.txt

Reads pytest's ``-q --tb=no -rfE`` output on stdin and prints every failing or
erroring test id that is NOT listed in the baseline file.  Exit 0 only when the
output carries pytest's summary line AND the failing set is a subset of the
baseline.  The summary-line requirement is what stops an empty or crashed run
from passing vacuously: no output means no failures were *listed*, not that
none occurred.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from pathlib import Path

# With ``-q`` the last line reads "3 failed, 1242 passed, 1 skipped in 80.1s".
_SUMMARY = re.compile(r"^=*\s*\d+ .*\bpassed\b.* in [0-9.]+s", re.MULTILINE)
_FAILED = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.MULTILINE)


def baseline_ids(text: str) -> set[str]:
    """Test ids in a baseline file: every non-blank line that is not a ``#`` comment."""
    return {line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")}


def check(pytest_output: str, baseline: Iterable[str]) -> tuple[bool, list[str]]:
    """Return (ok, problems).  Problems are new failing ids or a missing-summary message."""
    problems: list[str] = []
    if not _SUMMARY.search(pytest_output):
        problems.append("no pytest summary line found: the run was empty or crashed")
    allowed = set(baseline)
    problems += sorted({tid for tid in _FAILED.findall(pytest_output) if tid not in allowed})
    return (not problems, problems)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        sys.stderr.write("usage: python -m tests.known_failures_gate BASELINE_FILE < pytest-output\n")
        return 2
    ok, problems = check(sys.stdin.read(), baseline_ids(Path(argv[0]).read_text(encoding="utf-8")))
    for problem in problems:
        sys.stdout.write(problem + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
