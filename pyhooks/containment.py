"""
Project-root path containment for neuroloom hook modules.

Hooks that forward a file path, search target, or tool event to the Neuroloom
API (``preload_context``, ``cache_prefetch``, ``capture``) must first prove the
target lies inside the project root.  A path outside it -- ``~/.ssh/config``,
``/etc/hosts``, a sibling repository -- would otherwise be sent to this
project's shared workspace and pull back (or upload) unrelated content.

Which directory is "the project root", and which is "where Claude is"
----------------------------------------------------------------------
Two different directories matter, and they diverge after a ``cd``:

- **Project root** -- ``CLAUDE_PROJECT_DIR`` ("the project root where the
  session started", exported to hook processes and stable across ``cd``).
  When it is unset the root falls back to the hook process's working
  directory.  That fallback is weaker, and it weakens EVERY hook that uses
  this module (``preload_context`` and ``capture`` as much as
  ``cache_prefetch``): Claude Code spawns hooks in its *current* directory,
  which follows ``cd``, so after a ``cd`` out of the project the fallback root
  *is* the new directory and a containment check against it passes trivially.
  Claude Code sets ``CLAUDE_PROJECT_DIR`` for hooks, so the fallback matters
  mainly for tests and older versions; hooks trace ``project_root_fallback_cwd``
  when they take it.
- **Session cwd** -- the ``cwd`` field of the hook's stdin JSON, i.e. the
  directory Claude is working in right now.  Relative targets are resolved
  against it, and a Glob/Grep/bfs/ugrep call that names no path searches it, so
  it must itself be inside the root.  A ``cwd`` that cannot be resolved to an
  existing directory (a symlink loop, a NUL byte, a missing path) makes
  ``session_cwd`` return None; every check treats that as outside and the hook
  skips.

The root must also be the directory the API key came from
----------------------------------------------------------
``.neuroloom.db`` (the API key, session rows, cache keys) is still read from the
hook process's cwd, which follows ``cd`` -- where it lives is a separate
decision this module does not make.  But a root and a key that disagree are
dangerous: after a ``cd`` into a sibling repo that has its own ``.neuroloom.db``,
a file read in the ORIGINAL project would be sent with the SIBLING's key to the
sibling's workspace.  Every sending hook therefore skips unless
``credential_matches_root`` says the directory holding ``.neuroloom.db`` IS the
project root.  (This also skips a ``cd`` into a nested project that has its own
key; that is deliberate -- fail closed.)

How paths are judged
--------------------
Think of it as a building's front desk: a visitor's destination is checked
against the floors they are cleared for *after* the desk has worked out where
the stairwell actually leads -- a door labelled "floor 3" that opens onto the
parking garage (a symlink) or a corridor that doubles back out of the building
(``..``) is judged by where it ends up, not by what it says.

Targets a tool takes literally (a Read ``file_path``, a Glob/Grep ``path``) are
resolved and compared (``within_root``).

Targets that a shell or glob engine will *expand* (a Glob ``pattern``, the
words of a Bash ``bfs``/``ugrep``) are harder: the value the program finally
sees depends on an expansion that has not run yet.  Three techniques cover it:

- *Fail closed* on constructs whose value is unknowable here -- ``$VAR``,
  command substitution, a ``~`` that cannot be expanded, a ``.*`` that Bash
  before 5.2 can expand to ``..``, a ``..`` that follows a wildcard.  A Bash
  word is scanned with its quoting tracked (``bash_search_outside_root``):
  inside single quotes nothing expands, inside double quotes only ``$`` and
  backtick do, so a quoted regex such as ``'[a-z]+'``, ``'end$'`` or
  ``'\\d{1,2}'`` is judged only as a path.  An unquoted word keeps the full rule
  (``$ ` { [``), so an unquoted ``{a,/etc}`` is still skipped.
- *Enumerate* a Glob tool pattern's braces (``{..,src}/x`` -> ``../x``,
  ``src/x``; at most 32 alternatives, malformed -> outside) and check every
  alternative; a ``[...]`` class is a single-character wildcard.
- *Expand wildcards against the filesystem.*  A wildcard can match an
  in-project symlink that points elsewhere, so ``src/e*/x``, ``l?nk`` or
  ``lin*/secret.txt`` are not safe merely because their literal text is
  relative.  A wildcard part with further parts after it (and, for an unquoted
  Bash word, a wildcard that is the last part) is globbed against the real
  filesystem and every match is checked with ``within_root``; more than 64
  matches at any level is treated as outside.  Two dialects differ: in Bash
  ``*`` skips dotfiles and a trailing ``**`` is just ``*`` (globstar is off by
  default); the Glob tool's engine may match dotfiles, so there ``*`` includes
  them.  Bash is handled conservatively where shells differ: zsh -- the
  ``$SHELL`` on this machine -- recurses on ``**/``, so an unquoted ``**``
  followed by more path parts is failed closed, not modelled.
- *Do not model quoting inside a wildcard word.*  The shell matches a quoted or
  escaped glob character literally (``'a[b]'*`` matches the file ``a[b]x``), but
  the filesystem scan cannot tell which characters were quoted and would read
  ``[b]`` as a class.  A Bash word that has a live wildcard AND a quoted or
  escaped ``[ ] * ?`` is failed closed.  A quoted word with no live wildcard
  (``'[a-z]+'``, ``-name '*.{ts,tsx}'``) is a regex or a program-side pattern,
  never expanded, and is unaffected.
- *Delete what Bash deletes before it looks.*  A backslash-newline is a line
  continuation: Bash removes the pair, so a dot, a backslash-newline and another
  dot are ``..``.  The scanner does the same, outside single quotes (inside
  them the backslash is literal).  An unquoted word starting with ``=`` is
  failed closed because zsh expands ``=ls`` to ``/bin/ls``.

Remaining gaps -- stated, not hidden:

- In the GLOB TOOL, ``**`` is never expanded.  Expanding it against a cap would
  skip every repo-root ``**/*.ts`` (this repo has 663 directories), and walking
  it unbounded is not safe in a hook.  So ``**`` ends the expandable prefix: an
  in-tree symlink reached through ``**`` is not detected.  This is accepted for
  the Glob tool only, on the review's assessment that a recursive glob engine
  such as ripgrep does not follow symlinks (not verified here).  It does NOT
  apply to Bash words: a trailing ``**`` is expanded as ``*`` and ``**``
  followed by more parts is failed closed.
- ``bfs -L .`` and ``ugrep -R`` follow symlinks inside the tree they search.
  Only the *search term* (and, for ``preload_context``, the memories it
  retrieves) crosses the boundary -- never file content -- but a term typed
  against an outside directory reached that way is still sent.
- Case: the comparison is case-sensitive.  On macOS's default
  (case-insensitive) filesystem ``resolve()`` keeps the case it was given, so a
  case-mismatched spelling of an inside path is judged outside (over-skips,
  never leaks).

Uses only stdlib -- zero pip dependencies.
"""

import fnmatch
import itertools
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NamedTuple

_PROJECT_DIR_ENV = "CLAUDE_PROJECT_DIR"

# Upper bounds that keep a hook fast and its answer decidable.
_GLOB_MATCH_CAP = 64  # filesystem matches checked for one wildcard prefix
_BRACE_ALTERNATIVE_CAP = 32  # alternatives a Glob pattern's braces may expand to


class _Dialect(NamedTuple):
    """How a value is going to be expanded, which decides what must be checked."""

    # Characters that make a path part a glob.
    wildcards: str
    # Bash has globstar off by default, so a trailing `**` is just `*` and is
    # expanded here like any other wildcard.  (A `**` followed by more parts is
    # rejected before it gets here: zsh recurses on `**/`.)  The Glob tool's
    # engine recurses on `**`; that is not expanded (see the module docs).
    double_star_is_star: bool
    # Whether `*` can match a dotfile.  Bash: no.  The Glob tool's engine: it may.
    hidden: bool


# Bash words only ever reach the filesystem check with `*?` (an unquoted `[` is
# rejected first).  A Glob tool pattern also treats `[...]` as a one-character
# wildcard.
_BASH = _Dialect(wildcards="*?", double_star_is_star=True, hidden=False)
_GLOB_TOOL = _Dialect(wildcards="*?[", double_star_is_star=False, hidden=True)

# Shell operator characters.  Unquoted, a run of them separates words.
_OPERATOR_CHARS = ";&|<>()"

# Redirection targets that name no file: /dev/null, and the fd duplicates 2>&1 / 1>&2.
_ALLOWED_REDIRECT_FILE = "/dev/null"
_ALLOWED_REDIRECT_FDS = frozenset({"1", "2"})

# Tools whose event target is a file path, and the input keys that carry it.
_FILE_TARGET_KEYS = ("file_path", "notebook_path")
_FILE_TOOLS = frozenset({"Read", "Write", "Edit", "NotebookEdit"})


# ---------------------------------------------------------------------------
# Root, session cwd, and the credential directory
# ---------------------------------------------------------------------------


def _configured_project_dir() -> str | None:
    """``CLAUDE_PROJECT_DIR`` if it is set to an absolute path, else None.

    A relative value is ignored: Claude Code exports an absolute path, and
    anything else is not trustworthy.
    """
    configured = os.environ.get(_PROJECT_DIR_ENV, "")
    return configured if configured and os.path.isabs(configured) else None


def project_root() -> Path:
    """Return the resolved project root: ``CLAUDE_PROJECT_DIR``, else the cwd."""
    return Path(_configured_project_dir() or os.getcwd()).resolve()


def project_root_is_fallback() -> bool:
    """True when ``project_root`` fell back to the process cwd (see module docs)."""
    return _configured_project_dir() is None


def session_cwd(event: Mapping[str, object]) -> Path | None:
    """Return the resolved directory Claude is working in: the event's ``cwd``.

    Falls back to the hook process's working directory when the event carries
    no ``cwd`` string.  Returns **None** when that directory cannot be proven to
    be an existing directory: a symlink loop, a NUL byte, a path that does not
    exist or is not a directory, or a process cwd that has been deleted.  A real
    working directory is none of those, so an event that claims one is malformed
    and nothing it says can be trusted.  None means "cannot judge", and every
    check that takes a cwd (``tool_targets_outside_root``,
    ``bash_search_outside_root``) treats it as outside, so the hook skips.

    The ``is_dir()`` check is what refuses a symlink loop, on every Python
    version: ``resolve()`` raises on a loop only on older Pythons, and on newer
    ones (observed on 3.14) returns the loop path unchanged -- which would pass as
    "inside" when the loop sits under the root -- but ``is_dir()`` is False for it.
    """
    raw = event.get("cwd")
    try:
        resolved = Path(raw if isinstance(raw, str) and raw else os.getcwd()).resolve()
        return resolved if resolved.is_dir() else None
    except (OSError, RuntimeError, ValueError):
        return None


def credential_matches_root(state_db_path: Path, root: Path) -> bool:
    """True only if the directory holding ``.neuroloom.db`` IS the project *root*.

    The API key is read from that directory.  A key from anywhere else would
    send this project's paths and content to a different project's workspace
    (see the module docs).  Fails closed on an unresolvable path.
    """
    try:
        return state_db_path.parent.resolve() == root
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Literal paths
# ---------------------------------------------------------------------------


def within_root(raw_path: str, root: Path, base: str | Path | None = None) -> bool:
    """Return True only if *raw_path*, fully resolved, lies inside *root*.

    Resolution follows symlinks and collapses ``..`` before comparing, so
    ``root/../other`` and a symlink under *root* that points elsewhere are both
    reported as outside.  Comparison uses ``Path.is_relative_to()`` rather than
    ``str.startswith()`` so ``/workspace-evil/x`` cannot pass as inside
    ``/workspace``.

    Args:
        raw_path: The path as the tool supplied it, treated literally.  ``~``
                  is expanded; a path that still begins with ``~`` afterwards
                  (``~nosuchuser/x``, ``~+``, ``~-``) names a place that cannot
                  be determined here and is reported as outside.
        root:     The project root.  Must already be resolved (see
                  ``project_root``) -- on macOS ``/var`` and ``/private/var``
                  are the same directory spelled two ways, and an unresolved
                  root would reject every path under it.
        base:     Directory a *relative* ``raw_path`` is interpreted against --
                  pass the session cwd.  Defaults to the process working
                  directory, which follows ``cd`` and is therefore only a
                  fallback.

    Fails closed: any path that cannot be resolved (embedded NUL byte, symlink
    loop) is reported as outside rather than raising.

    Case: the comparison is case-sensitive.  On macOS's default
    (case-insensitive) filesystem ``resolve()`` keeps the case it was given, so
    ``/Users/x/Proj/a.py`` is *not* recognised as inside ``/Users/x/proj``; a
    case-mismatched spelling is skipped (over-skips, never leaks).
    """
    try:
        expanded = os.path.expanduser(raw_path)
        if expanded.startswith("~"):
            return False
        candidate = Path(expanded)
        if not candidate.is_absolute() and base is not None:
            candidate = Path(os.path.expanduser(str(base))) / candidate
        return candidate.resolve().is_relative_to(root)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Wildcard paths (values a glob engine or shell will expand)
# ---------------------------------------------------------------------------


def _path_inside(
    raw: str,
    root: Path,
    base: str | Path | None,
    *,
    dialect: _Dialect,
    expand_last: bool,
) -> bool:
    """Path rules for a value that a glob engine or shell will expand.

    The caller has already rejected expansion constructs that cannot be modelled
    (``$``, backtick, braces, and for Bash an unquoted ``[``).  What is left:

    - a part that can start with a dot (``.*``, ``.?``, or a bracket class,
      which may contain ``.``) and also contains a wildcard may expand to ``..``
      in Bash before 5.2 -- outside;
    - a ``..`` after a wildcard climbs out of whatever the wildcard matched
      (possibly a symlink), not out of ours -- outside;
    - the literal text must resolve inside the root;
    - the wildcard parts that can match an in-project symlink are expanded
      against the filesystem and every match is checked.
    """
    parts = raw.split("/")
    wildcard_seen = False
    for part in parts:
        if part == ".." and wildcard_seen:
            return False
        if any(char in part for char in dialect.wildcards):
            if part[:1] in (".", "["):
                return False
            wildcard_seen = True
    if not within_root(raw, root, base):
        return False
    return _expansion_inside(parts, root, base, dialect=dialect, expand_last=expand_last)


def _glob_matches(prefix: str, base: str | Path | None, *, dialect: _Dialect) -> list[str] | None:
    """Filesystem matches of the wildcard path *prefix*, or None if over the cap.

    A small scan of our own instead of ``glob.iglob``: ``iglob`` never matches a
    dotfile (right for Bash, wrong for the Glob tool's engine), and its
    ``include_hidden`` option needs Python 3.11 while the hooks run on 3.10.  One
    code path serves both dialects.  Matches are paths relative to *base* (or
    absolute).  Wildcard parts match only entries that exist; a literal part is
    joined as written without an existence check, which is harmless because
    ``_expansion_inside`` only ever passes a prefix that ENDS at a wildcard part.
    A trailing slash (directories only, to a shell) is ignored, so files are
    returned too -- a superset, which can only over-check.  More than
    ``_GLOB_MATCH_CAP`` at any level returns None -- the work, and the answer,
    are bounded.

    *dialect* says which characters are wildcards and whether dotfiles match; a
    Bash caller is never judged by the Glob tool's table (where ``[`` is live).
    """
    absolute = prefix.startswith("/")
    current = ["/" if absolute else ""]
    for part in (piece for piece in prefix.split("/") if piece):
        following: list[str] = []
        for entry in current:
            if not any(char in part for char in dialect.wildcards):
                following.append(os.path.join(entry, part))
                continue
            directory = entry if absolute else os.path.join(str(base) if base else ".", entry)
            try:
                names = os.listdir(directory)
            except OSError:
                continue  # not a directory, or unreadable: nothing to match
            for name in sorted(fnmatch.filter(names, part)):
                if dialect.hidden or part.startswith(".") or not name.startswith("."):
                    following.append(os.path.join(entry, name))
        if len(following) > _GLOB_MATCH_CAP:
            return None
        current = following
    return current


def _expansion_inside(
    parts: list[str],
    root: Path,
    base: str | Path | None,
    *,
    dialect: _Dialect,
    expand_last: bool,
) -> bool:
    """Glob the wildcard prefix of *parts* and check every match with within_root.

    Only the prefix up to the LAST wildcard part that can reach a symlink is
    globbed -- a part with further parts after it, or (``expand_last``) the final
    part -- so a pattern such as ``src/*/f*.py`` globs the directories at
    ``src/*``, not every file.  For the Glob tool ``**`` is never expanded and
    ends the prefix (see the module docs); in Bash a trailing ``**`` is just
    ``*``.  Zero matches means nothing exists to leak, so it passes.
    """
    prefix_parts = parts
    if "**" in parts and not dialect.double_star_is_star:
        prefix_parts = parts[: parts.index("**")]
    last = len(parts) - 1
    candidates = [
        index
        for index, part in enumerate(prefix_parts)
        if any(char in part for char in dialect.wildcards) and (index < last or expand_last)
    ]
    if not candidates:
        return True

    cut = max(candidates)
    prefix = os.path.expanduser("/".join(parts[: cut + 1]))
    remainder = "/".join(parts[cut + 1 :])
    matches = _glob_matches(prefix, base, dialect=dialect)
    if matches is None:
        return False  # too many to check: cannot be proven inside
    return all(
        within_root(os.path.join(match, remainder) if remainder else match, root, base)
        for match in matches
    )


def _expand_braces(pattern: str) -> list[str] | None:
    """Enumerate Bash-style ``{a,b}`` braces; None if malformed or too many.

    Returns the pattern itself when it has no braces.  A group with no comma
    (``{a}``) is treated as its one alternative -- the stricter reading, since
    Bash would leave it literal and a literal can never be worse than an
    alternative.  An unbalanced ``{`` or a stray ``}`` is malformed, and more
    than ``_BRACE_ALTERNATIVE_CAP`` results is too many; both are unprovable.
    """
    pending = [pattern]
    done: list[str] = []
    steps = 0
    while pending:
        steps += 1
        if steps > 4 * _BRACE_ALTERNATIVE_CAP:
            return None  # deeply nested groups: bound the work, not just the output
        text = pending.pop()
        start = text.find("{")
        if start == -1:
            if "}" in text:
                return None
            done.append(text)
        else:
            depth = 0
            commas: list[int] = []
            end = -1
            for index in range(start, len(text)):
                char = text[index]
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        end = index
                        break
                elif char == "," and depth == 1:
                    commas.append(index)
                    if len(pending) + len(done) + len(commas) >= _BRACE_ALTERNATIVE_CAP:
                        return None  # too many: stop before building any of them
            if end == -1:
                return None
            edges = [start, *commas, end]
            for left, right in itertools.pairwise(edges):
                pending.append(text[:start] + text[left + 1 : right] + text[end + 1 :])
        if len(pending) + len(done) > _BRACE_ALTERNATIVE_CAP:
            return None
    return done


def glob_within_root(pattern: str, root: Path, base: str | Path | None = None) -> bool:
    """Return True only if every path a Glob tool *pattern* can match is inside *root*.

    Braces are enumerated and each alternative checked (``{..,src}/x`` yields
    ``../x``); ``[...]`` is a single-character wildcard.  Each alternative is
    then judged by the wildcard path rules, including filesystem expansion of
    wildcard directories (``lin*/secret.txt`` when ``link`` is a symlink out).
    Anything that cannot be modelled -- ``$``, backtick, malformed or oversized
    braces -- is outside.
    """
    alternatives = _expand_braces(pattern)
    if alternatives is None:
        return False
    return all(
        "$" not in alt
        and "`" not in alt
        and _path_inside(alt, root, base, dialect=_GLOB_TOOL, expand_last=False)
        for alt in alternatives
    )


# ---------------------------------------------------------------------------
# Bash bfs / ugrep commands
# ---------------------------------------------------------------------------


class _Word(NamedTuple):
    """One shell word with the quoting information the checks need.

    ``value`` is what the program receives (quotes removed).  ``unquoted`` and
    ``double`` are the characters that came from unquoted and from
    double-quoted text; text from single quotes, or escaped with a backslash,
    appears in neither -- nothing in it expands.
    """

    value: str
    unquoted: str
    double: str


def _scan_command(command: str) -> list[_Word | str] | None:
    """Split a command into words (quoting tracked) and operator runs.

    Operators (``;&|<>()``) separate words only when unquoted, so ``'a|b'`` is
    one literal word.  A backslash outside single quotes makes the next
    character literal.  Returns None for an unterminated quote.
    """
    items: list[_Word | str] = []
    value: list[str] = []
    unquoted: list[str] = []
    double: list[str] = []
    in_word = False

    def flush() -> None:
        nonlocal in_word
        if in_word:
            items.append(_Word("".join(value), "".join(unquoted), "".join(double)))
        value.clear()
        unquoted.clear()
        double.clear()
        in_word = False

    index, length = 0, len(command)
    while index < length:
        char = command[index]
        if char in " \t\r\n":
            flush()
            index += 1
        elif char in _OPERATOR_CHARS:
            flush()
            end = index
            while end < length and command[end] in _OPERATOR_CHARS:
                end += 1
            items.append(command[index:end])
            index = end
        elif char == "\\" and command[index + 1 : index + 2] == "\n":
            index += 2  # line continuation: Bash deletes both characters and the word goes on
        elif char == "\\":
            value.append(command[index + 1] if index + 1 < length else char)
            in_word = True
            index += 2
        elif char == "'":
            end = command.find("'", index + 1)
            if end == -1:
                return None
            value.append(command[index + 1 : end])
            in_word = True
            index = end + 1
        elif char == '"':
            index += 1
            closed = False
            while index < length:
                inner = command[index]
                if inner == '"':
                    closed = True
                    index += 1
                    break
                if inner == "\\" and command[index + 1 : index + 2] == "\n":
                    index += 2  # line continuation inside double quotes: also deleted
                elif inner == "\\" and index + 1 < length and command[index + 1] in '$`"\\':
                    value.append(command[index + 1])  # escaped: a literal, not an expansion
                    index += 2
                else:
                    value.append(inner)
                    double.append(inner)
                    index += 1
            if not closed:
                return None
            in_word = True
        else:
            value.append(char)
            unquoted.append(char)
            in_word = True
            index += 1
    flush()
    return items


def _flag_values(token: str) -> list[str]:
    """Candidate path values carried inside a flag token.

    ``--include=/etc/x`` -> the part after ``=``.  ``-f/etc`` -> everything
    after the two-character flag.  A cluster such as ``-rf/etc`` hides the value
    further in, so the tail from the first ``/`` is also checked.
    """
    if "=" in token:
        return [token.split("=", 1)[1]]
    values = [token[2:]]
    if "/" in token:
        values.append(token[token.index("/") :])
    return values


def _word_outside(word: _Word, root: Path, cwd: Path) -> bool:
    """Judge one command word; True if it may point outside *root*."""
    # Quoting decides what can still expand: anything unquoted, `$`/backtick in
    # double quotes; single-quoted and backslash-escaped text expands to nothing.
    if any(char in word.unquoted for char in "$`{["):
        return True
    if any(char in word.double for char in "$`"):
        return True
    if word.value.startswith("=") and word.unquoted.startswith("="):
        return True  # zsh: `=ls` expands to `/bin/ls`
    value = word.value
    if not value:
        return False
    if value.startswith("-"):
        return any(v and not within_root(v, root, cwd) for v in _flag_values(value))
    if any(char in word.unquoted for char in _BASH.wildcards):
        # A live wildcard beside a quoted or escaped glob character (`'a[b]'*`,
        # or the same with backslashes): the shell matches the quoted part
        # literally, but the scan below cannot tell which characters were quoted
        # and would treat `[b]` as a class.  Not modelled -- failed closed.
        if any(value.count(char) > word.unquoted.count(char) for char in "[]*?"):
            return True
        # zsh (this machine's $SHELL) recurses on `**/`, Bash does not.  An
        # unquoted `**` with more path parts after it is failed closed; a
        # trailing `**` (`src/**`) keeps the `*` treatment.
        if any(len(part) >= 2 and not part.strip("*") for part in value.split("/")[:-1]):
            return True
        return not _path_inside(value, root, cwd, dialect=_BASH, expand_last=True)
    return not within_root(value, root, cwd)


def _redirect_target_allowed(operator: str, word: _Word) -> bool:
    """True for a redirection target that names no file: /dev/null, 2>&1, 1>&2."""
    if word.value == _ALLOWED_REDIRECT_FILE:
        return True
    return operator.endswith("&") and word.value in _ALLOWED_REDIRECT_FDS


def bash_search_outside_root(command: str, root: Path, cwd: Path | None) -> bool:
    """Return True if a Bash ``bfs``/``ugrep`` command targets outside *root*.

    Search commands carry their targets inside the command string, so every word
    is checked -- flag values (``-f/etc``, ``--include=/etc/x``) and redirection
    targets (``>/etc/x``) included.  Quoting is tracked per word (see the
    module docs), wildcards in unquoted words are expanded against the
    filesystem, and redirections to ``/dev/null`` or an fd (``2>/dev/null``,
    ``2>&1``) are allowed.  The session *cwd* must itself be inside *root*,
    because a search that names no path (``bfs -name x``, ``ugrep -r foo``) runs
    there.

    This errs towards skipping: a word that is really a regex but spelled like
    an outside path or an unquoted expansion costs one nudge, whereas a missed
    outside path would leak it.  An unparseable command is outside, and so is an
    unusable *cwd* (None, see ``session_cwd``).
    """
    if cwd is None or not within_root(str(cwd), root):
        return True
    items = _scan_command(command)
    if items is None:
        return True

    program_seen = False
    redirect_operator: str | None = None
    for item in items:
        if isinstance(item, str):
            redirect_operator = item if ("<" in item or ">" in item) else None
            continue
        if not program_seen:  # the first word is the program name
            program_seen = True
            continue
        operator, redirect_operator = redirect_operator, None
        if operator is not None and _redirect_target_allowed(operator, item):
            continue
        if _word_outside(item, root, cwd):
            return True
    return False


# ---------------------------------------------------------------------------
# Tool events
# ---------------------------------------------------------------------------


def tool_targets_outside_root(
    tool_name: str,
    tool_input: Mapping[str, Any],
    root: Path,
    cwd: Path | None,
) -> bool:
    """Return True if this tool call's target is outside *root* (or unprovable).

    An unusable *cwd* (None, see ``session_cwd``) is outside for EVERY tool, path-
    targeted or not: the event is malformed, so none of it is trusted.

    Covers Read, Write, Edit, NotebookEdit (file path), and Glob, Grep
    (``path``, plus a Glob ``pattern``).  Any other tool is not path-targeted
    and returns False -- Bash is deliberately not handled here: output of an
    arbitrary command cannot be attributed to a path.  Bash ``bfs``/``ugrep``
    search commands have their own check, ``bash_search_outside_root``.

    Only the *location* is judged, never the content searched for: a Grep
    ``pattern`` is a regex over file contents, so one that merely looks like
    ``/etc/hosts`` is still a project search.  A Glob ``pattern`` IS a location
    (``/etc/**``, ``../other/*.py``) and is checked relative to its ``path``.

    A Glob/Grep with no ``path`` searches the session cwd, which must then
    itself be inside *root*.
    """
    if cwd is None:
        return True
    if tool_name in _FILE_TOOLS:
        targets = [
            value
            for key in _FILE_TARGET_KEYS
            if isinstance(value := tool_input.get(key), str) and value
        ]
        if not targets:
            return True  # a file tool with no target cannot be proven inside
        return any(not within_root(t, root, cwd) for t in targets)

    if tool_name in ("Glob", "Grep"):
        raw_path = tool_input.get("path")
        if isinstance(raw_path, str) and raw_path:
            if not within_root(raw_path, root, cwd):
                return True
            # An absolute `path` wins the join; a relative one hangs off the cwd.
            search_dir = cwd / Path(os.path.expanduser(raw_path))
        else:
            if not within_root(str(cwd), root):
                return True
            search_dir = cwd
        if tool_name == "Glob":
            pattern = tool_input.get("pattern")
            if isinstance(pattern, str) and pattern:
                return not glob_within_root(pattern, root, search_dir)
        return False

    return False
