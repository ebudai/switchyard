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
#: it is demonstrably that copy: a whole added block (a run of added lines) that
#: appears, line for line and in order, among the lines the candidate's own
#: migrations ADD -- never an unchanged historical migration -- and each such
#: occurrence excuses one block, once. Anything not proved so is judged like any
#: growth, against the file's allowance and any Director ceiling. The full
#: before/after is always reported.
MIRROR_PATHS = frozenset({"scripts/ticket_board/schema.sql"})


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
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if proc.returncode != 0:
        raise ScanError(f"git {' '.join(args)}: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


def resolve(repo: Path, rev: str) -> str:
    return _git(repo, "rev-parse", "--verify", f"{rev}^{{commit}}").decode().strip()


def merge_base(repo: Path, main: str, candidate: str) -> str:
    return _git(repo, "merge-base", main, candidate).decode().strip()


def _lines_at(repo: Path, commit: str, path: str) -> int | None:
    return physical_lines(_git(repo, "cat-file", "blob", f"{commit}:{path}"))


def measure(repo: Path, base: str, candidate: str) -> Measurement:
    """Every in-scope text file the candidate adds, changes or renames, against `base`.

    Raises ScanError on any git failure: an incomplete scan is never a clean one.
    """
    base, candidate = resolve(repo, base), resolve(repo, candidate)
    raw = _git(repo, "diff", "--name-status", "-M", "-z", base, candidate)
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
        sources = [block for f in result.files if f.path.startswith(MIGRATION_PREFIX)
                   for block in _added_blocks(repo, base, candidate, f.path)]
        result.files = [_with_mirror_proof(f, _added_blocks(repo, base, candidate, f.path), sources)
                        if f.path in MIRROR_PATHS else f for f in result.files]
    return result


def _added_blocks(repo: Path, base: str, candidate: str, path: str) -> list[list[str]]:
    """Each run of lines `candidate` adds to `path`, trailing whitespace dropped; a new file is one run."""
    diff = _git(repo, "diff", "-U0", "--no-color", "--no-ext-diff", base, candidate, "--", path)
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

    A block is proved when its SQL lines -- comment-only and blank lines set
    aside -- occur contiguously and in order among one source block's SQL lines,
    at positions no earlier proof used: occurrences are consumed, so one
    migration line cannot excuse many copies, and reordered or partial blocks
    prove nothing. A proved block is excused whole, its annotations with it; an
    unproved block's every non-blank line counts, comments included.
    """
    filtered = [[line for line in block if not _neutral(line)] for block in sources]
    used = [[False] * len(block) for block in filtered]
    excused = unproved = 0
    for block in added:
        core = [line for line in block if not _neutral(line)]
        if core and _consume(core, filtered, used):
            excused += len(block)
            continue
        blanks = sum(1 for line in block if not line.strip())
        excused += blanks
        unproved += len(block) - blanks
    return FileGrowth(growth.path, growth.previous_path, growth.before, growth.after, growth.status,
                      unmirrored=unproved, mirrored=excused)


def _consume(core: list[str], sources: list[list[str]], used: list[list[bool]]) -> bool:
    size = len(core)
    for index, source in enumerate(sources):
        for at in range(len(source) - size + 1):
            if source[at:at + size] == core and not any(used[index][at:at + size]):
                for k in range(at, at + size):
                    used[index][k] = True
                return True
    return False


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
