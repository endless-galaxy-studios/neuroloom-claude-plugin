#!/usr/bin/env python3
"""
Seed script: full-project code graph sync.

Walks the workspace for supported file types, parses them with codeweaver,
and POSTs the structural metadata to the Neuroloom code-graph API.

Usage
-----
    python seed_code_graph.py [--workspace-root /path/to/project] [file1 file2 ...]

Positional arguments are optional explicit file paths. When omitted, the script
discovers all supported files under the workspace root automatically.

Environment variables
---------------------
CLAUDE_PLUGIN_OPTION_API_KEY
    Neuroloom API key (checked first).

NEUROLOOM_API_KEY
    Neuroloom API key fallback.

CLAUDE_PLUGIN_OPTION_API_BASE
    Neuroloom API base URL (checked first).

NEUROLOOM_API_BASE
    Neuroloom API base URL fallback. Defaults to https://api.neuroloom.dev.

Exit codes
----------
0   Success or graceful skip (codeweaver not installed, no supported files).
1   Hard failure (missing API key, network error, non-2xx HTTP response).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def _resolve_config() -> tuple[str, str]:
    """Return (api_key, api_base) from the environment.

    Priority for each:
    - api_key: CLAUDE_PLUGIN_OPTION_API_KEY → NEUROLOOM_API_KEY
    - api_base: CLAUDE_PLUGIN_OPTION_API_BASE → NEUROLOOM_API_BASE → default
    """
    api_key = (
        os.environ.get("CLAUDE_PLUGIN_OPTION_API_KEY", "").strip()
        or os.environ.get("NEUROLOOM_API_KEY", "").strip()
    )
    api_base = (
        os.environ.get("CLAUDE_PLUGIN_OPTION_API_BASE", "").strip()
        or os.environ.get("NEUROLOOM_API_BASE", "").strip()
        or "https://api.neuroloom.dev"
    )
    return api_key, api_base


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed the Neuroloom code graph for a full workspace."
    )
    parser.add_argument(
        "--workspace-root",
        default=None,
        help="Root directory of the workspace. Defaults to the current working directory.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Explicit file paths to parse. When omitted, all supported files under --workspace-root are discovered automatically.",
    )
    args = parser.parse_args()

    root = Path(args.workspace_root).resolve() if args.workspace_root else Path.cwd().resolve()

    # Graceful skip if codeweaver is not installed
    try:
        from codeweaver import discover_files, parse_files  # type: ignore[import-untyped,unused-ignore]
    except ImportError:
        print("code-graph: skipped (codeweaver not installed)")
        sys.exit(0)

    # Resolve the list of files to parse
    if args.paths:
        target_paths = [Path(p).resolve() for p in args.paths]
    else:
        target_paths = list(discover_files(root))

    if not target_paths:
        print("code-graph: skipped (no supported files found)")
        sys.exit(0)

    # Parse
    try:
        payload: dict = parse_files(target_paths, root)
    except Exception as exc:
        print(f"code-graph: failed (parse error: {exc})")
        sys.exit(1)

    # Validate config
    api_key, api_base = _resolve_config()
    if not api_key:
        print("code-graph: failed (missing API key)")
        sys.exit(1)

    # Count symbols for the status line
    symbols: list = payload.get("symbols", [])
    file_count = len({s.get("file") for s in symbols if s.get("file")}) if symbols else 0
    symbol_count = len(symbols)

    # POST to the code-graph sync endpoint
    url = f"{api_base.rstrip('/')}/api/v1/code-graph/sync"
    body = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Token {api_key}",
            "User-Agent": "neuroloom-seed",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            _ = resp.read()
    except urllib.error.HTTPError as exc:
        print(f"code-graph: failed (HTTP {exc.code})")
        sys.exit(1)
    except urllib.error.URLError as exc:
        print(f"code-graph: failed (network error: {exc.reason})")
        sys.exit(1)
    except Exception as exc:
        print(f"code-graph: failed ({exc})")
        sys.exit(1)

    print(f"code-graph: seeded ({file_count} files, {symbol_count} symbols)")
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"code-graph: failed ({exc})")
        sys.exit(1)
