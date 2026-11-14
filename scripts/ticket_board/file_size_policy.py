"""Measure file growth between two exact commits for the size review (SYRD-541).

Pure measurement: git blobs at exact commits, never a working tree; physical
lines; rename-aware file identity; an explicit table of what is in scope. It
decides bands, not approvals -- whether growth is allowed is the board's
question, answered from durable records this module never reads.

Bands:
  warn    -- at or above WARN_LINES: reported, never gated.
  review  -- above REVIEW_LINES: gated, but only on GROWTH (see `classify`).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

WARN_LINES = 1100
REVIEW_LINES = 1250

#: Out of scope, decided here and nowhere else. Prose is not code; vendored
#: trees are someone else's; binary blobs have no lines. Tests, SQL, JS and
#: extensionless scripts are IN scope -- the old scanner's extension list is
#: not carried forward.
EXCLUDED_PREFIXES = ("docs/", "external/", "third_party/")
EXCLUDED_SUFFIXES = (".md",)
#: An applied migration is history: it is never rewritten to meet a number, and
#: changing one is already refused, so it cannot grow. A NEW migration is a new
#: file and is measured like any other.
MIGRATION_PREFIX = "scripts/ticket_board/migrations/"
#: Mirrors: by construction they carry the newest copy of what other files
#: define, so they grow with every change to those. Growth is excused only where
#: it is demonstrably that copy: a whole added block (a run of added lines) whose
#: every line is a line the candidate's own migrations ADD -- never an unchanged
#: historical migration -- in the migrations' order, each migration line excusing
#: one copy, once. Anything not proved so is judged like any growth, against the
#: file's allowance and any Director ceiling. The full before/after is always
#: reported. See `_with_mirror_proof`.
MIRROR_PATHS = frozenset({"scripts/ticket_board/schema.sql"})
#: A migration's number, as ticket_board/migration_order.py reads it. That module
#: decides the order migrations run in; this file is installed alone as a hook,
#: so it cannot import it, and a test holds the two to the same order.
MIGRATION_NUMBER = re.compile(r"(?:pgu)?(0|[1-9][0-9]*)_[a-z0-9_]+\.sql", re.ASCII)


class ScanError(RuntimeError):
    """The measurement could not be completed; the caller must not read this as clean."""


@dataclass(frozen=True)
class FileGrowth:
    path: str
    previous_path: str | None
    before: int  # lines at the base (0 for a new file)
    after: int  # lines at the candidate
    status: str  # added, modified, renamed
    unmirrored: int = 0  # a mirror's added non-blank lines not proved to be a migration's copy
    mirrored: int = 0  # a mirror's added lines that are excused: proved blocks, and blank lines

    @property
    def growth(self) -> int:
        return self.after - self.before

    @property
    def band(self) -> str:
        if self.after > REVIEW_LINES:
            return "review"
        if self.after >= WARN_LINES:
            return "warn"
        return "ok"


@dataclass
class Measurement:
    base: str
    candidate: str
    files: list[FileGrowth] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)


def in_scope(path: str) -> bool:
    if path.startswith(EXCLUDED_PREFIXES) or path.endswith(EXCLUDED_SUFFIXES):
        return False
    return True


def physical_lines(data: bytes) -> int | None:
    """Physical lines of a text blob, or None for a binary one."""
    if b"\0" in data[:8192]:
        return None
    return len(data.decode("utf-8", errors="replace").splitlines())


def _git(repo: Path, *args: str) -> bytes:
    # A path is a checkout of one's own; the board passes the reader of its
    # trusted commit cache, which names the repository exactly (SYRD-571).
    argv, env = repo.git_command(args) if hasattr(repo, "git_command") else (["git", "-C", str(repo), *args], None)
    proc = subprocess.run(argv, capture_output=True, env=env)
    if proc.returncode != 0:
        raise ScanError(f"git {' '.join(args)}: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


def resolve(repo: Path, rev: str) -> str:
    return _git(repo, "rev-parse", "--verify", "--end-of-options", f"{rev}^{{commit}}").decode().strip()


def merge_base(repo: Path, main: str, candidate: str) -> str:
    return _git(repo, "merge-base", main, candidate).decode().strip()


def _lines_at(repo: Path, commit: str, path: str) -> int | None:
    return physical_lines(_git(repo, "cat-file", "blob", f"{commit}:{path}"))


def measure(repo: Path, base: str, candidate: str) -> Measurement:
    """Every in-scope text file the candidate adds, changes or renames, against `base`.

    Raises ScanError on any git failure: an incomplete scan is never a clean one.
    """
    base, candidate = resolve(repo, base), resolve(repo, candidate)
    raw = _git(repo, "diff", "--name-status", "-M", "-z", "--no-ext-diff", "--no-textconv", base, candidate)
    fields = [f.decode("utf-8", errors="surrogateescape") for f in raw.split(b"\0") if f]
    result = Measurement(base=base, candidate=candidate)
    i = 0
    while i < len(fields):
        status = fields[i]
        if status.startswith(("R", "C")):
            old, new = fields[i + 1], fields[i + 2]
            i += 3
        else:
            old = new = fields[i + 1]
            i += 2
        kind = status[0]
        if kind == "D":
            if in_scope(old):
                result.deleted.append(old)
            continue
        if not in_scope(new):
            continue
        after = _lines_at(repo, candidate, new)
        if after is None:
            continue  # binary
        if kind == "A":
            before, previous, label = 0, None, "added"
        elif kind == "R":
            before, previous, label = _lines_at(repo, base, old) or 0, old, "renamed"
        elif kind == "C":
            before, previous, label = 0, None, "added"  # a copy is a new file
        else:
            before, previous, label = _lines_at(repo, base, new) or 0, None, "modified"
        result.files.append(FileGrowth(path=new, previous_path=previous, before=before, after=after, status=label))
    result.files.sort(key=lambda f: f.path)
    if any(f.path in MIRROR_PATHS for f in result.files):
        migrations = sorted((f.path for f in result.files if f.path.startswith(MIGRATION_PREFIX)), key=migration_key)
        sources = [block for path in migrations for block in _added_blocks(repo, base, candidate, path)]
        result.files = [_with_mirror_proof(f, _added_blocks(repo, base, candidate, f.path), sources)
                        if f.path in MIRROR_PATHS else f for f in result.files]
    return result


def migration_key(path: str) -> tuple[int, int, bytes]:
    """The order migrations run in: by number, then (for the few historical shared numbers) by name. A name that is
    not a migration's, which migration_order would refuse, sorts last."""
    name = PurePosixPath(path).name
    match = MIGRATION_NUMBER.fullmatch(name)
    return (0, int(match.group(1)), name.encode()) if match else (1, 0, name.encode())


def _added_blocks(repo: Path, base: str, candidate: str, path: str) -> list[list[str]]:
    """Each run of lines `candidate` adds to `path`, trailing whitespace dropped; a new file is one run."""
    diff = _git(repo, "diff", "-U0", "--no-color", "--no-ext-diff", "--no-textconv", base, candidate, "--", path)
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in diff.decode("utf-8", errors="replace").splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            current.append(line[1:].rstrip())
            continue
        if current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def _neutral(line: str) -> bool:
    """A blank line or a comment-only line: annotation, not SQL."""
    stripped = line.strip()
    return not stripped or stripped.startswith("--")


def _with_mirror_proof(growth: FileGrowth, added: list[list[str]], sources: list[list[str]]) -> FileGrowth:
    """How much of a mirror's growth is proved to be its migrations' copy.

    `sources` are the lines the candidate's migrations add, in the order the
    migrations run. Their SQL lines -- comment-only and blank lines set aside --
    are one sequence, and each of its lines can prove one copy, once: an
    occurrence used by one proof is consumed, so one migration line cannot
    excuse many copies. An added block's SQL lines are proved when they occur
    among the unused lines in the same order. They need not be adjacent there:
    one hunk may fold several migrations, and a mirror does not carry every
    migration line where the migration has it -- not BEGIN and COMMIT, not the
    grants rbac.sql holds, and not an object it replaces in place instead of
    appending. Every line of a proved block is still a distinct line of the
    candidate's migrations, in their order: reordered or altered blocks prove
    nothing, and a block is proved whole or not at all (no partial credit). A
    proved block is excused whole, its annotations with it; an unproved block's
    every non-blank line counts, comments included.

    Blocks that occur as one contiguous run are matched first, each to its
    first unused run: a short in-place copy then takes its own run, not lines
    scattered through an object that a longer appended block needs. The rest
    are matched after, in file order, each line to its earliest unused
    occurrence, which finds an in-order match whenever one exists.
    """
    stream = [line for block in sources for line in block if not _neutral(line)]
    used = [False] * len(stream)
    cores = [[line for line in block if not _neutral(line)] for block in added]
    proved = [bool(core) and _consume_run(core, stream, used) for core in cores]
    proved = [done or (bool(core) and _consume_in_order(core, stream, used)) for done, core in zip(proved, cores)]
    excused = unproved = 0
    for block, done in zip(added, proved):
        if done:
            excused += len(block)
            continue
        blanks = sum(1 for line in block if not line.strip())
        excused += blanks
        unproved += len(block) - blanks
    return FileGrowth(growth.path, growth.previous_path, growth.before, growth.after, growth.status,
                      unmirrored=unproved, mirrored=excused)


def _consume_run(core: list[str], stream: list[str], used: list[bool]) -> bool:
    """The first unused contiguous occurrence of `core` in `stream`, consumed; False if there is none."""
    size = len(core)
    for at in range(len(stream) - size + 1):
        if stream[at] == core[0] and stream[at:at + size] == core and not any(used[at:at + size]):
            used[at:at + size] = [True] * size
            return True
    return False


def _consume_in_order(core: list[str], stream: list[str], used: list[bool]) -> bool:
    """`core` as an in-order selection of unused `stream` lines, consumed; False, consuming nothing, if there is none.
    Taking each line's earliest unused occurrence finds a selection whenever one exists."""
    picked: list[int] = []
    at = 0
    for line in core:
        while at < len(stream) and (used[at] or stream[at] != line):
            at += 1
        if at == len(stream):
            return False
        picked.append(at)
        at += 1
    for k in picked:
        used[k] = True
    return True


def classify(growth: FileGrowth, ceiling: int | None) -> str | None:
    """Why this change needs a review, or None.

    A file's allowance is the larger of its size at the base -- the candidate is
    never asked about growth it did not make, which is what keeps historical
    debt and merge-base movement from stopping unrelated work -- and its
    approved `ceiling`, if one is on record (an approved exception's reviewed
    maximum; headroom a Director granted up to it).
      crossing -- the candidate takes it over REVIEW_LINES from at or under it,
                  with no approval above REVIEW_LINES;
      growth   -- it is over REVIEW_LINES and the candidate grows it past its
                  allowance.
    Shrinking, holding steady, or staying within the allowance needs nothing.
    A mirror is judged the same way on its size less the lines proved to be
    its migrations' copy (`mirrored`): so proved copies never count, replaced
    lines never make an unchanged file a finding, and a Director's ceiling
    covers its unproved growth exactly as it covers any file's.
    """
    if growth.after <= REVIEW_LINES:
        return None
    if growth.path in MIRROR_PATHS:
        if growth.after - growth.mirrored <= max(growth.before, ceiling or 0):
            return None
        return "growth"
    allowance = max(growth.before, ceiling or 0)
    if growth.after <= allowance:
        return None
    if growth.before <= REVIEW_LINES and (ceiling is None or ceiling <= REVIEW_LINES):
        return "crossing"
    return "growth"


def inventory(repo: Path, commit: str, threshold: int = REVIEW_LINES) -> list[tuple[str, int]]:
    """Every in-scope text file over `threshold` at `commit`: the debt a baseline records."""
    commit = resolve(repo, commit)
    names = [n.decode("utf-8", errors="surrogateescape")
             for n in _git(repo, "ls-tree", "-r", "-z", "--name-only", commit).split(b"\0") if n]
    found = []
    for name in names:
        if not in_scope(name) or PurePosixPath(name).name == "":
            continue
        lines = _lines_at(repo, commit, name)
        if lines is not None and lines > threshold:
            found.append((name, lines))
    return sorted(found)
