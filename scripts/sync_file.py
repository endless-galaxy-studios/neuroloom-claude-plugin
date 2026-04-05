#!/usr/bin/env python3
"""sync_file.py — Parse changed source files and POST structural metadata to the Neuroloom API.

Invoked by code-graph-sync.sh in a background subshell after a Write/Edit tool fires.
Uses stdlib-only HTTP (urllib.request) so the plugin has zero Python dependencies
beyond the optional neuroloom-mcp[codegraph] extra.

Exit codes:
  0  — success or any non-fatal error (parse failure, network error, missing deps, etc.)
  42 — HTTP 429 Too Many Requests (signals the bash caller to increase backoff)
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# API key — read from environment, never from CLI args (keeps key off ps output)
# ---------------------------------------------------------------------------
api_key = os.environ.get("NEUROLOOM_API_KEY", "")
if not api_key:
    print(
        "[neuroloom] code-graph sync skipped: NEUROLOOM_API_KEY not set",
        file=sys.stderr,
    )
    sys.exit(0)

debug = os.environ.get("NEUROLOOM_DEBUG") == "1"

# ---------------------------------------------------------------------------
# Optional dependency guard — two distinct failure modes
# ---------------------------------------------------------------------------
try:
    import neuroloom_mcp  # noqa: F401
except ImportError:
    print(
        "[neuroloom] code-graph sync disabled: install neuroloom-mcp[codegraph] to enable",
        file=sys.stderr,
    )
    sys.exit(0)

try:
    from neuroloom_mcp.services.code_graph_parser import parse_files
except ImportError:
    print(
        "[neuroloom] code-graph sync disabled: neuroloom-mcp is installed but missing "
        "the codegraph extra. Run: pip install 'neuroloom-mcp[codegraph]'",
        file=sys.stderr,
    )
    sys.exit(0)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Parse source files and POST structural metadata to Neuroloom.",
    )
    p.add_argument(
        "abs_paths",
        nargs="+",
        metavar="ABS_PATH",
        help="Absolute path(s) of the changed file(s).",
    )
    p.add_argument(
        "--workspace-root",
        required=True,
        metavar="WORKSPACE_ROOT",
        help="Absolute path to the workspace root (repository root).",
    )
    p.add_argument(
        "--api-base",
        required=True,
        metavar="API_BASE",
        help="Base URL of the Neuroloom API (e.g. https://api.neuroloom.dev).",
    )
    return p


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # ---------------------------------------------------------------------------
    # Parse files → SyncPayload dict
    # ---------------------------------------------------------------------------
    paths = [Path(p).resolve() for p in args.abs_paths]
    workspace_root = Path(args.workspace_root).resolve()

    sync_data: dict[str, Any] = parse_files(paths, workspace_root)

    # ---------------------------------------------------------------------------
    # POST to API
    # ---------------------------------------------------------------------------
    url = f"{args.api_base.rstrip('/')}/api/v1/code-graph/sync"
    payload = json.dumps(sync_data).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Token {api_key}",
        },
        method="POST",
    )

    if debug:
        masked = f"Token ...{api_key[-4:]}" if len(api_key) >= 4 else "Token ...????"
        print(f"[neuroloom:sync] url={url} auth={masked}", file=sys.stderr)

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            if debug:
                body: str = resp.read(200).decode("utf-8", errors="replace")
                print(
                    f"[neuroloom:sync] status={resp.status} body={body[:200]}",
                    file=sys.stderr,
                )
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            sys.exit(42)
        # Any other HTTP error — swallow and exit clean
        sys.exit(0)
    except Exception:
        # Network errors, timeouts, parse errors — never crash the hook
        sys.exit(0)


if __name__ == "__main__":
    main()
