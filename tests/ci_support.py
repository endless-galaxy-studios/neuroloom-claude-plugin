"""
Shared test support for the D178d CI-mode work (standard library only).

This module is imported by the base plugin's test suite (``from tests import
ci_support``) and loaded *by file path* by the SDLC plugin's suite, which has
no ``tests`` package.  It therefore imports nothing from other test files, and
touches ``pyhooks`` only lazily inside functions, after the caller has put the
intended plugin checkout on ``sys.path``.

Contents (each has a self-test in ``tests/test_ci_support.py``):

* ``BlackHole``            -- a local listener that counts connections and never answers.
* ``make_ignored_git_repo``-- a throwaway git repository whose ``.gitignore`` covers the state db.
* ``make_bait_repo``       -- a repository set up so an unsafe hook visibly changes it,
                              with a trigger-to-assertion table.
* ``tree_hash`` / ``presence_snapshot`` -- "did the checkout change?" detectors.
* ``forbid_side_effects``  -- patches thread/subprocess/network entry points to raise a
                              ``BaseException`` sentinel that ``except Exception`` cannot swallow.
* ``SlimRecorder``         -- loopback API stand-in; bodies retained, headers never.
* ``write_egress_shim`` / ``write_codeweaver_stub`` / ``interpreter_params`` -- hermeticity helpers.
* ``run_hook``             -- subprocess hook runner with a minimal, explicit environment.
* ``registered_hook_modules`` -- hook modules listed in ``hooks/hooks.json``.
* ``credential_env_names`` -- environment names that look like credentials.

Plain-language picture: the bait repository is a mousetrap, the control run is
the pencil you test it with, and the black hole is a mailbox with a counter on
the flap.  A clean trap only means "no mouse" if the pencil sets it off.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.server
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Root of the plugin checkout this module lives in (``<root>/tests/ci_support.py``).
PLUGIN_ROOT = Path(__file__).resolve().parent.parent

# DUMMY credential for tests only; it is not a real key for anything.  It is
# shaped like a real one (40 lowercase hex characters, high entropy, nothing
# sequential) so the sweep's partial-match logic is exercised on realistic data
# instead of a pattern that is trivially easy to spot.
SENTINEL_KEY = "e3807da0816f1b2484e68c8d42e76ded1a95d810"

_CREDENTIAL_NAME_PARTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "BEARER")

_GIT_ISOLATION_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
}


# ---------------------------------------------------------------------------
# Environment hygiene
# ---------------------------------------------------------------------------


def credential_env_names(environ: Mapping[str, str]) -> list[str]:
    """Return the names in *environ* that look like credentials.

    A name matches when it contains KEY, TOKEN, SECRET, PASSWORD, CREDENTIAL or
    BEARER (case-insensitive).  Used by the autouse hygiene fixtures so a
    developer's real secrets can never reach a hook under test.
    """
    return sorted(
        name
        for name in environ
        if any(part in name.upper() for part in _CREDENTIAL_NAME_PARTS)
    )


# ---------------------------------------------------------------------------
# Black-hole listener
# ---------------------------------------------------------------------------

_MARKER_TOKEN = b"NLMARKER"


class BlackHole:
    """A loopback listener that accepts connections, counts them, and closes them.

    Like a mailbox with a counter on the flap: pointing a hook's API base at it
    proves the hook made zero connections when the count stays at zero.

    Accepts lag behind connects, so a count read straight after the hook exits
    can miss connections still in the accept queue.  ``connection_count()``
    therefore first sends a *marker* connection and waits until the accept loop
    has seen it; the loop is sequential, so everything queued before the marker
    has been counted by then.  The marker itself is not counted.
    """

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(128)
        self._sock.settimeout(0.05)
        self.port: int = int(self._sock.getsockname()[1])
        self._count = 0
        self._lock = threading.Lock()
        self._marker_seen = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="black-hole", daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                conn.settimeout(0.2)
                try:
                    first = conn.recv(len(_MARKER_TOKEN))
                except (socket.timeout, OSError):
                    first = b""
                if first == _MARKER_TOKEN:
                    self._marker_seen.set()
                else:
                    with self._lock:
                        self._count += 1
            finally:
                with contextlib.suppress(OSError):
                    conn.close()

    def connection_count(self, timeout: float = 5.0) -> int:
        """Return the number of (non-marker) connections accepted so far.

        Sends a marker connection and reads the count only after the accept
        loop has processed it.
        """
        self._marker_seen.clear()
        with socket.create_connection(("127.0.0.1", self.port), timeout=timeout) as marker:
            marker.sendall(_MARKER_TOKEN)
            if not self._marker_seen.wait(timeout):
                raise TimeoutError("black hole did not see its marker connection")
        with self._lock:
            return self._count

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        with contextlib.suppress(OSError):
            self._sock.close()


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def git_available() -> bool:
    return shutil.which("git") is not None


def _git_env() -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/"),
        "LC_ALL": "C",
    }
    env.update(_GIT_ISOLATION_ENV)
    return env


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git in *root* with the user's global and system configuration disabled.

    Disabling them matters: a developer's ``core.excludesFile`` could otherwise
    mask a missing ``.gitignore`` entry and make a bad fixture look good.
    """
    return subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        capture_output=True,
        text=True,
        env=_git_env(),
        check=False,
    )


def make_ignored_git_repo(root: Path) -> Path:
    """Create a git repository at *root* whose ``.gitignore`` ignores ``.neuroloom.db*``.

    ``.gitignore`` and a ``CLAUDE.md`` (without the memory-first marker) are
    committed, so after a run ``git status --porcelain`` is empty exactly when
    the hook edited no tracked file and created no un-ignored file.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / ".gitignore").write_text(".neuroloom.db*\n", encoding="utf-8")
    (root / "CLAUDE.md").write_text("# Fixture project\n\nPlain notes.\n", encoding="utf-8")
    for step in (("init", "-q"), ("add", ".gitignore", "CLAUDE.md"), ("commit", "-q", "-m", "fixture")):
        done = git(root, *step)
        if done.returncode != 0:
            raise RuntimeError(f"git {step[0]} failed while building the fixture repository")
    return root


# ---------------------------------------------------------------------------
# Tree hash and presence snapshot
# ---------------------------------------------------------------------------


def _iter_files(root: Path, skip_dirs: tuple[str, ...]) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in skip_dirs)
        for name in sorted(filenames):
            yield Path(dirpath) / name


def tree_hash(root: Path, *, skip_dirs: tuple[str, ...] = (".git",)) -> str:
    """Hash every file under *root* except the state database and its sidecars.

    ``.neuroloom.db*`` is excluded explicitly (the hook is allowed to write it).
    The hash cannot see a file *appearing* that is itself excluded; pair it with
    ``presence_snapshot`` for that.
    """
    digest = hashlib.sha256()
    for path in _iter_files(root, skip_dirs):
        if path.name.startswith(".neuroloom.db"):
            continue
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(b"\x00")
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
        digest.update(b"\x01")
    return digest.hexdigest()


def presence_snapshot(
    root: Path,
    *,
    exclude: tuple[str, ...] = (),
    skip_dirs: tuple[str, ...] = (".git",),
) -> list[str]:
    """Return the sorted relative names of all files under *root*.

    *exclude* is caller-supplied: nothing is hidden by default, so a stray
    ``.neuroloom.db`` created by a hook that should have stayed dormant shows
    up here even though ``tree_hash`` ignores it.  Entries in *exclude* are
    matched against the RELATIVE path (POSIX separators), exactly or as a
    prefix when they end with ``*``; ``.neuroloom.db*`` therefore hides the
    root-level database and sidecars only, never ``sub/.neuroloom.db``.
    """

    def _excluded(rel: str) -> bool:
        for pattern in exclude:
            if pattern.endswith("*") and rel.startswith(pattern[:-1]):
                return True
            if rel == pattern:
                return True
        return False

    names = (p.relative_to(root).as_posix() for p in _iter_files(root, skip_dirs))
    return sorted(n for n in names if not _excluded(n))


# ---------------------------------------------------------------------------
# Bait repository and trigger table
# ---------------------------------------------------------------------------

_BAIT_GITIGNORE = "node_modules/\n"
_BAIT_CLAUDEMD = "# Bait project\n\nNo memory-first marker here.\n"
_BAIT_MCP_JSON = (
    '{\n  "mcpServers": {\n    "unrelated": {"type": "http", "url": "https://example.invalid/mcp"}\n  }\n}\n'
)
_CLAUDEMD_MARKER = "<!-- neuroloom:memory-first -->"
_STALE_SESSION_ID = "sess-1700000000-aabbccdd"

# Keys of the ``config`` table that arm the workspace-reconcile step.
_ARM_WORKSPACE_ID = "11111111-2222-3333-4444-555555555555"


@dataclass
class Trigger:
    """One thing an unsafe session_start would do to the bait.

    ``fired(bait)`` returns True when the effect is visible *now*, compared with
    the seeded state.  A CI-mode run asserts every trigger is False; the non-CI
    control run asserts every listed trigger is True (proving the bait works).
    """

    name: str
    description: str
    fired: Callable[[Bait], bool]


@dataclass
class Bait:
    """A repository seeded so an unsafe session_start visibly changes it."""

    root: Path
    db_path: Path
    api_key: str
    armed_mcp_json: bool
    seeded_gitignore: bytes
    seeded_claudemd: bytes
    seeded_mcp_json: bytes | None
    seeded_buffer_rows: int
    # Primary keys of the seeded ``event_buffer`` rows (AUTOINCREMENT, so never reused).
    seeded_buffer_ids: list[int] = field(default_factory=list)
    triggers: list[Trigger] = field(default_factory=list)

    def fired(self) -> dict[str, bool]:
        """Map trigger name -> whether its effect is visible now."""
        return {t.name: t.fired(self) for t in self.triggers}

    def buffer_rows(self) -> int:
        conn = sqlite3.connect(str(self.db_path))
        try:
            return int(conn.execute("SELECT COUNT(*) FROM event_buffer").fetchone()[0])
        finally:
            conn.close()


def _trigger_gitignore(bait: Bait) -> bool:
    path = bait.root / ".gitignore"
    return path.read_bytes() != bait.seeded_gitignore


def _trigger_claudemd(bait: Bait) -> bool:
    path = bait.root / "CLAUDE.md"
    return path.read_bytes() != bait.seeded_claudemd or _CLAUDEMD_MARKER in path.read_text(
        encoding="utf-8"
    )


def _trigger_buffer(bait: Bait) -> bool:
    """True when ANY seeded buffer row is gone.

    Counting rows would be blind to a flush followed by new rows (a CI capture
    legitimately adds local rows), so the seeded ids themselves are checked.
    """
    conn = sqlite3.connect(str(bait.db_path))
    try:
        present = {int(r[0]) for r in conn.execute("SELECT id FROM event_buffer")}
    finally:
        conn.close()
    return any(i not in present for i in bait.seeded_buffer_ids)


def _trigger_mcp_json(bait: Bait) -> bool:
    path = bait.root / ".mcp.json"
    if bait.seeded_mcp_json is None:
        return path.exists()
    return path.read_bytes() != bait.seeded_mcp_json


def make_bait_repo(
    root: Path,
    *,
    arm_mcp_json: bool = False,
    api_key: str = SENTINEL_KEY,
) -> Bait:
    """Build the bait repository at *root* and return it with its trigger table.

    Seeded state:

    * ``.gitignore`` without any ``.neuroloom.db`` entry;
    * ``CLAUDE.md`` without the memory-first marker;
    * a ``config`` row holding *api_key* (so every key-gated step is live);
    * typed, shape-valid ``event_buffer`` rows (two observations, one document);
    * a stale ``sessions`` row keyed by the resolved root;
    * a ``.mcp.json`` with unrelated content, so a byte-identical assertion
      later is meaningful when ``arm_mcp_json`` is False.

    With ``arm_mcp_json=True`` the workspace-reconcile step is armed instead:
    the cached workspace id and key fingerprint make the resolvability gate
    return RESOLVED without a network call, a desired workspace is recorded,
    and ``.mcp.json`` is left absent so reconcile would create it.

    Trigger table (name: what it looks like afterwards):

    =================  ==================================================
    gitignore_written  ``.gitignore`` bytes differ from the seeded bytes
    claudemd_appended  ``CLAUDE.md`` differs / now carries the marker
    buffer_flushed     any seeded ``event_buffer`` row id is gone
    mcp_json_written   (armed only) ``.mcp.json`` now exists
    =================  ==================================================
    """
    # Lazy import: the caller has already put the intended checkout first on sys.path.
    import pyhooks.db as db_mod  # noqa: PLC0415

    root.mkdir(parents=True, exist_ok=True)
    (root / ".gitignore").write_text(_BAIT_GITIGNORE, encoding="utf-8")
    (root / "CLAUDE.md").write_text(_BAIT_CLAUDEMD, encoding="utf-8")
    seeded_mcp: bytes | None = None
    if not arm_mcp_json:
        (root / ".mcp.json").write_text(_BAIT_MCP_JSON, encoding="utf-8")
        seeded_mcp = (root / ".mcp.json").read_bytes()

    db_path = root / ".neuroloom.db"
    conn = db_mod.open_db(db_path)
    if conn is None:
        raise RuntimeError("could not create the bait database")
    try:
        conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('api_key', ?)", (api_key,))
        if arm_mcp_json:
            fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
            for key, value in (
                ("workspace_id", _ARM_WORKSPACE_ID),
                ("workspace_id_key_fingerprint", fingerprint),
                ("desired_workspace_id", _ARM_WORKSPACE_ID),
                ("desired_workspace_set_state", "value"),
            ):
                conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
        observation = {
            "observation_id": "obs-bait",
            "session_id": _STALE_SESSION_ID,
            "observed_at": "2026-07-10T00:00:00+00:00",
            "category": "Write",
            "content": "{}",
            "agent_id": None,
            "agent_type": None,
        }
        for n in range(2):
            row = dict(observation, observation_id=f"obs-bait-{n}")
            conn.execute(
                "INSERT INTO event_buffer (payload, created_at, payload_type) VALUES (?, ?, ?)",
                (json.dumps(row), 1_700_000_000.0 + n, "observation"),
            )
        document = {
            "title": "bait.md",
            "content": "# bait",
            "source_type": "sdlc_deliverable",
            "source_path": "docs/bait.md",
            "tags": ["sdlc:doc:spec"],
        }
        conn.execute(
            "INSERT INTO event_buffer (payload, created_at, payload_type) VALUES (?, ?, ?)",
            (json.dumps(document), 1_700_000_002.0, "document"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO sessions (session_key, session_id, started_at, last_submit_ms) "
            "VALUES (?, ?, datetime('now'), 0)",
            (str(root.resolve()), _STALE_SESSION_ID),
        )
        conn.commit()
        seeded_ids = [int(r[0]) for r in conn.execute("SELECT id FROM event_buffer ORDER BY id")]
    finally:
        conn.close()

    bait = Bait(
        root=root,
        db_path=db_path,
        api_key=api_key,
        armed_mcp_json=arm_mcp_json,
        seeded_gitignore=(root / ".gitignore").read_bytes(),
        seeded_claudemd=(root / "CLAUDE.md").read_bytes(),
        seeded_mcp_json=seeded_mcp,
        seeded_buffer_rows=len(seeded_ids),
        seeded_buffer_ids=seeded_ids,
    )
    bait.triggers = [
        Trigger("gitignore_written", "a state-db entry is added to .gitignore", _trigger_gitignore),
        Trigger("claudemd_appended", "the memory-first block is appended to CLAUDE.md", _trigger_claudemd),
        Trigger("buffer_flushed", "buffered events are POSTed and deleted", _trigger_buffer),
    ]
    if arm_mcp_json:
        bait.triggers.append(
            Trigger("mcp_json_written", ".mcp.json is created by workspace reconcile", _trigger_mcp_json)
        )
    return bait


class FakeApi:
    """In-process stand-in for ``pyhooks.http.post_json`` used by bait runs.

    Records ``(method-less) path`` names in ``paths`` -- never headers, bodies
    or the environment, so a failing assertion cannot print a credential.
    """

    def __init__(self) -> None:
        self.paths: list[str] = []

    def __call__(
        self, url: str, headers: dict[str, str], payload: bytes, timeout: float
    ) -> tuple[int, bytes] | None:
        path = urllib.parse.urlsplit(url).path
        self.paths.append(path)
        if path.endswith("/sessions/start"):
            return 200, b'{"session_id":"sess-ok"}'
        if path.endswith("/documents/ingest/batch"):
            docs = json.loads(payload.decode("utf-8")).get("documents", [])
            results = [{"index": i, "title": d.get("title"), "status": "created"} for i, d in enumerate(docs)]
            return 200, json.dumps({"results": results}).encode("utf-8")
        return 200, b'{"ok":true}'


def join_hook_threads(timeout: float = 5.0) -> None:
    """Wait for non-daemon threads started by a hook under test."""
    for thread in threading.enumerate():
        if thread is threading.current_thread() or thread.daemon:
            continue
        thread.join(timeout=timeout)


# ---------------------------------------------------------------------------
# Slim recorder: records (method, path, carried-the-key) and bodies, never headers
# ---------------------------------------------------------------------------


class SlimRecorder:
    """Loopback API stand-in.  Bodies retained; headers never.

    ``requests`` holds ``(method, path-with-query, carried_key)`` triples, where
    ``carried_key`` is a boolean computed inside the handler, so a failing
    assertion cannot print a credential.  ``bodies`` is parallel to
    ``requests`` and holds the raw request bodies (the key travels in a header
    only, so a body containing it is itself a finding).

    ``reflect_401=True`` answers everything with 401 and a JSON body that echoes
    the request headers (the key included); it exists to prove hooks never copy
    a response body into an output.  ``status_overrides`` maps a bare path to a
    status code for that endpoint.
    """

    def __init__(
        self,
        *,
        key: str = SENTINEL_KEY,
        reflect_401: bool = False,
        status_overrides: dict[str, int] | None = None,
        sdlc_latest: str = "2.0.0",
        sdlc_seeded: str | None = "1.0.0",
    ) -> None:
        self.requests: list[tuple[str, str, bool]] = []
        self.bodies: list[bytes] = []
        recorder = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length", 0) or 0)
                body = self.rfile.read(length) if length else b""
                carried = self.headers.get("Authorization") == f"Token {key}"
                recorder.requests.append((self.command, self.path, carried))
                recorder.bodies.append(body)
                bare = self.path.split("?", 1)[0]
                if reflect_401:
                    status = 401
                    payload: Any = {"detail": "invalid token", "echo": dict(self.headers)}
                else:
                    status = (status_overrides or {}).get(bare, 200)
                    payload = _canned(bare, self.path, body, sdlc_latest, sdlc_seeded)
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:  # noqa: N802
                self._handle()

            def do_GET(self) -> None:  # noqa: N802
                self._handle()

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def paths(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p, _ in self.requests]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _canned(bare: str, full: str, body: bytes, sdlc_latest: str, sdlc_seeded: str | None) -> Any:
    if bare == "/api/v1/sessions/start":
        return {"session_id": "sess-ok"}
    if bare == "/api/v1/documents/ingest/batch":
        try:
            docs = json.loads(body.decode("utf-8")).get("documents", [])
        except Exception:
            docs = []
        return {"results": [{"index": i, "title": d.get("title"), "status": "created"} for i, d in enumerate(docs)]}
    if bare == "/api/v1/context":
        return {"injection_text": "context text", "cache_hint_ttl_seconds": 300}
    if bare == "/api/v1/sdlc/cc-sdlc-version":
        return {"version": sdlc_latest}
    if bare == "/api/v1/memories/":
        tags = ["sdlc:sentinel"] + ([f"sdlc:seed-version:{sdlc_seeded}"] if sdlc_seeded else [])
        return [{"tags": tags}]
    return {"ok": True}


# ---------------------------------------------------------------------------
# Forbid fixture (sentinel exception)
# ---------------------------------------------------------------------------


class ForbiddenCallError(BaseException):
    """Raised by a forbidden call.

    Derives from ``BaseException`` on purpose: the hooks contain dozens of
    ``except Exception`` blocks (and ``post_json`` swallows everything), so an
    ordinary exception would be silently absorbed and the test would pass for
    the wrong reason.  Like a fire alarm that staff cannot silence with the
    normal mute button.
    """


class ForbidRecorder:
    """Names of the forbidden functions that were called."""

    def __init__(self) -> None:
        self.calls: list[str] = []


def _is_loopback_url(target: Any) -> bool:
    url = target.full_url if hasattr(target, "full_url") else str(target)
    host = urllib.parse.urlsplit(url).hostname or ""
    return host in {"127.0.0.1", "localhost", "::1"}


@contextlib.contextmanager
def forbid_side_effects() -> Iterator[ForbidRecorder]:
    """Patch ``urlopen`` (non-loopback), ``subprocess.run``, ``subprocess.Popen``
    and ``threading.Thread.start`` so any call appends its name to
    ``recorder.calls`` and raises ``ForbiddenCallError``.

    Install it only around the hook's ``main()``, after test servers have
    started.  Loopback ``urlopen`` passes through to the original.
    """
    recorder = ForbidRecorder()
    original_urlopen = urllib.request.urlopen
    original_run = subprocess.run
    original_popen = subprocess.Popen
    original_start = threading.Thread.start

    def _urlopen(target: Any, *args: Any, **kwargs: Any) -> Any:
        if _is_loopback_url(target):
            return original_urlopen(target, *args, **kwargs)
        recorder.calls.append("urllib.request.urlopen")
        raise ForbiddenCallError("urllib.request.urlopen")

    def _run(*args: Any, **kwargs: Any) -> Any:
        recorder.calls.append("subprocess.run")
        raise ForbiddenCallError("subprocess.run")

    def _popen(*args: Any, **kwargs: Any) -> Any:
        recorder.calls.append("subprocess.Popen")
        raise ForbiddenCallError("subprocess.Popen")

    def _start(self: threading.Thread) -> None:
        recorder.calls.append("threading.Thread.start")
        raise ForbiddenCallError("threading.Thread.start")

    urllib.request.urlopen = _urlopen  # type: ignore[assignment]
    subprocess.run = _run
    subprocess.Popen = _popen  # type: ignore[assignment,misc]
    threading.Thread.start = _start  # type: ignore[method-assign]
    try:
        yield recorder
    finally:
        urllib.request.urlopen = original_urlopen
        subprocess.run = original_run
        subprocess.Popen = original_popen  # type: ignore[misc]
        threading.Thread.start = original_start  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Hook list from hooks/hooks.json
# ---------------------------------------------------------------------------

_HOOK_CMD_RE = re.compile(r"run_hook\.py\s+(\S+)")


def registered_hook_modules(plugin_root: Path | None = None) -> list[str]:
    """Return the hook modules registered in ``hooks/hooks.json`` (unique, in order).

    Root-pinning and CI-safety tests parametrize over this list so a newly
    registered hook cannot be silently missed.
    """
    root = plugin_root or PLUGIN_ROOT
    data = json.loads((root / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    modules: list[str] = []
    for groups in data.get("hooks", {}).values():
        for group in groups:
            for hook in group.get("hooks", []):
                match = _HOOK_CMD_RE.search(str(hook.get("command", "")))
                if match and match.group(1) not in modules:
                    modules.append(match.group(1))
    return modules


# ---------------------------------------------------------------------------
# Hermeticity helpers: egress shim, codeweaver stub, interpreter parameters
# ---------------------------------------------------------------------------

EGRESS_LOG_ENV = "NEUROLOOM_TEST_EGRESS_LOG"
EGRESS_HIDE_CODEWEAVER_ENV = "NEUROLOOM_TEST_EGRESS_HIDE_CODEWEAVER"

# Python 3.10 compatible.  Imported automatically by the interpreter (it is a
# ``sitecustomize`` module) when its directory is on PYTHONPATH.
_EGRESS_SHIM_SOURCE = '''\
import os, socket, urllib.parse, urllib.request

_LOG = os.environ.get("NEUROLOOM_TEST_EGRESS_LOG")


def _record(kind, host):
    if _LOG:
        try:
            with open(_LOG, "a", encoding="utf-8") as fh:
                fh.write(kind + " " + str(host) + "\\n")
        except OSError:
            pass


def _loop(host):
    host = str(host)
    return host in ("localhost", "::1") or host.startswith("127.")


_orig_connect = socket.socket.connect
_orig_connect_ex = socket.socket.connect_ex
_orig_getaddrinfo = socket.getaddrinfo
_orig_urlopen = urllib.request.urlopen


def _connect(self, address):
    if isinstance(address, tuple) and address and not _loop(address[0]):
        _record("connect", address[0])
        raise OSError("non-loopback connect blocked by the egress shim")
    return _orig_connect(self, address)


def _connect_ex(self, address):
    if isinstance(address, tuple) and address and not _loop(address[0]):
        _record("connect", address[0])
        return 111
    return _orig_connect_ex(self, address)


def _getaddrinfo(host, *args, **kwargs):
    if host is not None and not _loop(host):
        _record("resolve", host)
        raise socket.gaierror("non-loopback name resolution blocked by the egress shim")
    return _orig_getaddrinfo(host, *args, **kwargs)


def _urlopen(target, *args, **kwargs):
    url = target.full_url if hasattr(target, "full_url") else str(target)
    host = urllib.parse.urlsplit(url).hostname or ""
    if not _loop(host):
        _record("urlopen", host)
        raise OSError("non-loopback urlopen blocked by the egress shim")
    return _orig_urlopen(target, *args, **kwargs)


socket.socket.connect = _connect
socket.socket.connect_ex = _connect_ex
socket.getaddrinfo = _getaddrinfo
urllib.request.urlopen = _urlopen

if os.environ.get("NEUROLOOM_TEST_EGRESS_HIDE_CODEWEAVER") == "1":
    # Hide the codeweaver DISTRIBUTION from importlib.metadata so the upgrade
    # check (which the offline flag does not skip) cannot run: the sweep then
    # sees only egress that is NOT the known PyPI check.
    import importlib.metadata as _md

    _orig_version = _md.version

    def _version(name):
        if str(name).lower().replace("_", "-") == "neuroloom-codeweaver":
            raise _md.PackageNotFoundError(name)
        return _orig_version(name)

    _md.version = _version
'''

_CODEWEAVER_STUB_SOURCE = '''\
"""Test stub for ``codeweaver``: exposes only what pyhooks/code_graph_sync.py imports."""


def parse_files(paths, workspace_root):
    return {
        "stub": True,
        "workspace_root": str(workspace_root),
        "files": [str(p) for p in paths],
    }
'''


def write_egress_shim(directory: Path) -> tuple[Path, Path]:
    """Write ``sitecustomize.py`` into *directory*; return ``(directory, log_path)``.

    Put *directory* on PYTHONPATH and set ``EGRESS_LOG_ENV`` to the returned log
    path.  Any non-loopback ``urlopen``, name resolution or ``socket.connect`` is
    recorded (host name only) and refused.  The log lives outside every tree the
    key-hygiene sweep scans.  It only takes effect for an interpreter that
    honours PYTHONPATH (the normal-mode launcher; not the ``-I`` CI launcher).
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "sitecustomize.py").write_text(_EGRESS_SHIM_SOURCE, encoding="utf-8")
    return directory, directory.parent / (directory.name + ".log")


def egress_attempts(log_path: Path) -> list[str]:
    """Lines recorded by the egress shim (``kind host``); empty when none."""
    if not log_path.exists():
        return []
    return [ln for ln in log_path.read_text(encoding="utf-8").splitlines() if ln]


def write_codeweaver_stub(directory: Path) -> Path:
    """Write a stub ``codeweaver`` package into *directory* and return it.

    On PYTHONPATH it shadows any real installation, so code-graph sync is live
    on every interpreter instead of silently degrading where codeweaver is
    absent.  Honoured only by interpreters that read PYTHONPATH (not ``-I``).
    """
    package = directory / "codeweaver"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text(_CODEWEAVER_STUB_SOURCE, encoding="utf-8")
    return directory


def interpreter_params() -> list[Any]:
    """pytest params for the suite interpreter plus python3.10.

    python3.10 comes from ``NEUROLOOM_TEST_PY310`` or ``shutil.which``.  When it
    is missing the param is still emitted, marked skip, so the gap shows up as a
    SKIP in the report rather than as a quietly shorter matrix.
    """
    import pytest  # noqa: PLC0415 (test-time dependency, kept out of module import)

    override = os.environ.get("NEUROLOOM_TEST_PY310", "")
    found = override or shutil.which("python3.10") or ""
    missing = not found or not Path(found).exists()
    return [
        pytest.param(sys.executable, id="suite-python"),
        pytest.param(
            found or "python3.10",
            id="py310",
            marks=pytest.mark.skipif(
                missing, reason="python3.10 not found (set NEUROLOOM_TEST_PY310 or put it on PATH)"
            ),
        ),
    ]


# ---------------------------------------------------------------------------
# Subprocess hook runner
# ---------------------------------------------------------------------------


class ImportOriginError(AssertionError):
    """The child interpreter would import ``pyhooks`` from the wrong checkout."""


# Runs a hook module the way the launcher would, with the control-run protections:
# no codeweaver bootstrap, and any non-loopback urlopen raises.
_SHIM_SOURCE = """\
import runpy, sys, urllib.parse, urllib.request
_orig = urllib.request.urlopen
def _guard(target, *a, **k):
    url = target.full_url if hasattr(target, "full_url") else str(target)
    if (urllib.parse.urlsplit(url).hostname or "") not in ("127.0.0.1", "localhost", "::1"):
        raise RuntimeError("non-loopback urlopen blocked by control shim")
    return _orig(target, *a, **k)
urllib.request.urlopen = _guard
module = sys.argv[1]
sys.argv = sys.argv[1:]
if module == "pyhooks.session_start":
    # run_module(run_name="__main__") would execute the source in a fresh
    # namespace and ignore a rebind made on the imported module, so import it,
    # rebind, and call main() the way its own __main__ block does.
    import pyhooks.session_start as _ss
    _ss._codeweaver_bootstrap_and_upgrade = lambda *a, **k: None
    try:
        _ss.main()
    except Exception:
        pass
else:
    runpy.run_module(module, run_name="__main__", alter_sys=True)
"""

_ORIGIN_PROBE = "import pyhooks, sys; sys.stdout.write(pyhooks.__file__)"


@dataclass
class HookRun:
    stdout: str
    stderr: str
    returncode: int
    env_names: list[str]


def _minimal_env(
    interpreter: str,
    plugin_root: Path,
    scratch: Path,
    black_hole_url: str,
    extra: Mapping[str, str] | None,
) -> dict[str, str]:
    """Explicit minimal environment -- never a copy of ``os.environ``."""
    home = scratch / "home"
    data = scratch / "plugin-data"
    home.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.pathsep.join([str(Path(interpreter).parent), "/usr/local/bin", "/usr/bin", "/bin"]),
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONPATH": str(plugin_root),
        "PYTHONDONTWRITEBYTECODE": "1",
        # A regressed CI skip must not be able to install anything or write
        # <worktree>/.venv: plugin data is a scratch dir and pip points at the
        # black hole, failing fast.
        "CLAUDE_PLUGIN_DATA": str(data),
        "PIP_INDEX_URL": black_hole_url,
        "PIP_RETRIES": "0",
        "PIP_TIMEOUT": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "NEUROLOOM_API_BASE": black_hole_url,
        **_GIT_ISOLATION_ENV,
    }
    if extra:
        env.update(extra)
    return env


def assert_import_origin(
    interpreter: str, expected_root: Path, env: Mapping[str, str], cwd: Path
) -> str:
    """Run a child ``import pyhooks`` with the identical interpreter, env and cwd.

    Returns the file path; raises ``ImportOriginError`` unless it is under
    *expected_root*.  Guards against the venv's editable install, which maps
    ``pyhooks`` to the main checkout rather than a worktree.
    """
    done = subprocess.run(
        [interpreter, "-c", _ORIGIN_PROBE],
        capture_output=True,
        text=True,
        env=dict(env),
        cwd=str(cwd),
        timeout=30,
        check=False,
    )
    origin = done.stdout.strip()
    if done.returncode != 0 or not origin:
        raise ImportOriginError("pyhooks could not be imported by the child interpreter")
    resolved = Path(origin).resolve()
    if expected_root.resolve() not in resolved.parents:
        raise ImportOriginError(f"pyhooks would be imported from outside {expected_root}: {resolved}")
    return str(resolved)


def run_hook(
    module: str,
    *,
    stdin: str = "",
    cwd: Path,
    scratch: Path,
    black_hole_url: str,
    env_extra: Mapping[str, str] | None = None,
    interpreter: str | None = None,
    plugin_root: Path | None = None,
    expected_root: Path | None = None,
    control: bool = False,
    timeout: float = 10.0,
    pythonpath_extra: tuple[Path, ...] = (),
) -> HookRun:
    """Run a hook module as a subprocess with an explicit minimal environment.

    * *interpreter* defaults to the suite's interpreter (``sys.executable``).
    * *plugin_root* is put on ``PYTHONPATH`` (default: this checkout) and the
      import origin is asserted against *expected_root* (default: *plugin_root*)
      before the hook is launched.
    * Every run, CI mode included, gets a scratch ``CLAUDE_PLUGIN_DATA`` and a
      ``PIP_INDEX_URL`` at the black hole.
    * ``control=True`` is for non-CI control runs: it sets
      ``NEUROLOOM_CODEWEAVER_OFFLINE=1`` and runs the hook through a shim that
      no-ops the codeweaver bootstrap and blocks non-loopback ``urlopen``.
      Otherwise the real launcher ``run_hook.py`` is used.
    * *pythonpath_extra* directories (egress shim, codeweaver stub) are appended
      to PYTHONPATH; the normal-mode launcher honours it, the ``-I`` CI one does not.
    """
    py = interpreter or sys.executable
    root = plugin_root or PLUGIN_ROOT
    extra = dict(env_extra or {})
    if control:
        extra.setdefault("NEUROLOOM_CODEWEAVER_OFFLINE", "1")
    env = _minimal_env(py, root, scratch, black_hole_url, extra)
    if pythonpath_extra:
        # Appended AFTER the plugin root(s), so a decoy can never shadow pyhooks.
        env["PYTHONPATH"] = os.pathsep.join([env["PYTHONPATH"], *map(str, pythonpath_extra)])

    assert_import_origin(py, expected_root or root, env, cwd)

    if control:
        argv = [py, "-c", _SHIM_SOURCE, module]
    else:
        argv = [py, str(root / "run_hook.py"), module]
    done = subprocess.run(
        argv,
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd),
        timeout=timeout,
        check=False,
    )
    return HookRun(done.stdout, done.stderr, done.returncode, sorted(env))
