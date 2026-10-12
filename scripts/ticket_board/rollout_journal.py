"""A rollout's own record of itself, written where the tenant cannot rewrite it.

The SYRD-126 operator wrappers captured a privileged run's output so the User
did not have to copy and paste it, and then wrote it under the project
account's home and chowned it there. Every role in a project runs as that one
account (SYRD-69), so the evidence of a privileged run ended up owned by every
role that run was evidence about: any of them could edit or delete it
afterwards, including by accident.

So the record is root's. The journal lives under a root-owned directory, each
completed file is mode 0444, and a role reads it without sudo and cannot write
it at all.

WHAT THAT IS AND IS NOT WORTH, stated plainly because the first version of this
file overstated it. Root ownership is the boundary: it stops the project account
-- which is every role -- from editing or deleting the evidence of a run it was
the subject of. The chain in the index is an INTEGRITY check on top of that, not
an authenticity proof: it catches a truncated or partial write, an entry removed
or altered by something that did not recompute the hashes after it, and a file
that has been reordered. It does NOT survive a malicious root. The whole journal
is root's to rewrite, hashes included, so a root that recomputes the chain leaves
a journal that verifies. Proving this record against root would take a witness
root cannot reach -- an append-only store elsewhere, a signing key held off the
host -- and there is none here (SYRD-128).

Three shapes, and each exists for a failure that actually happens:

* ONE DIRECTORY PER ATTEMPT, never an append. A rerun after a failure is a
  different run with a different outcome, and two runs sharing a log is how you
  end up reading the first one's success as the second one's.
* AN EVENT AT START AND AN EVENT AT FINISH, both chained into one index. A run
  that is killed outright cannot write its own ending, so an unfinished attempt
  is one the index shows started and never completed rather than one that
  quietly looks like it never happened.
* REDACTION AS THE BYTES ARE WRITTEN, never a pass afterwards. A secret written
  to disk and cleaned up later was still on disk, and on a shared account that
  window is the whole problem.
"""

from __future__ import annotations

import grp
import hashlib
import json
import os
import pwd
import re
import shutil
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

#: Root's own tree. Beside the publication staging root for the same reason it
#: is there: /var/lib is where a host keeps state that outlives a run and that
#: no tenant owns.
DEFAULT_JOURNAL_ROOT = "/var/lib/switchyard/rollout"
JOURNAL_ROOT_ENV = "SWITCHYARD_ROLLOUT_JOURNAL_ROOT"
INDEX_NAME = "index.jsonl"
RESULT_NAME = "result.json"
SCHEMA = "switchyard.rollout-journal.v1"

#: Written by root, read by everyone, rewritten by nobody.
DIRECTORY_MODE = 0o755
COMPLETED_MODE = 0o444
INDEX_MODE = 0o644

#: SYRD-113: a tenant's journal is that tenant's to read, and nobody else's.
#: Root owns it and its group is the one the tenant's board service runs with,
#: taken from root's own unit for that board (`tenant_group`); another tenant's
#: account can read none of it. A project whose unit names no group keeps the
#: modes above, and `repair_journal` says so.
TENANT_DIRECTORY_MODE = 0o750
TENANT_LIVE_MODE = 0o640
TENANT_COMPLETED_MODE = 0o440
TENANT_INDEX_MODE = 0o640
SYSTEM_UNIT_DIR = Path("/etc/systemd/system")
UNIT_LIMIT_BYTES = 256 * 1024

#: SYRD-113: how long an attempt's directory is kept. The newest
#: `RETENTION_KEEP` are kept whatever their age; an older one is removed once it
#: finished more than `RETENTION_DAYS` ago. Its index entries stay, and a chained
#: `pruned` entry says it was removed, so the chain still verifies.
RETENTION_KEEP = 200
RETENTION_DAYS = 180
ATTEMPT_NAME = re.compile(r"[0-9]{4,}-[0-9]{8}T[0-9]{6}Z")
_RESOLVE = object()

#: What must never reach the disk. Applied line by line as output is captured,
#: because a redaction pass that runs afterwards is a pass that runs after the
#: secret was already written.
REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # key=value and key: value, for anything whose name says it is a secret.
    (
        re.compile(
            r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSPHRASE|API[_-]?KEY|PRIVATE[_-]?KEY)[A-Z0-9_]*)"
            r"(\s*[=:]\s*)(\S+)"
        ),
        r"\1\2<redacted>",
    ),
    # Command-line forms: --write-token VALUE, --password=VALUE.
    (
        re.compile(r"(?i)(--[a-z0-9-]*(?:token|secret|password|passphrase|key)[a-z0-9-]*)([= ]+)(\S+)"),
        r"\1\2<redacted>",
    ),
    # Authorization headers and bearer tokens.
    (re.compile(r"(?i)(authorization\s*:\s*)(\S+\s*\S*)"), r"\1<redacted>"),
    (re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._~+/=-]{8,})"), r"\1<redacted>"),
    # A URL that carries its own credentials.
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^/\s@]+)@"), r"\1\2:<redacted>@"),
)

#: SYRD-113: what a privileged job's capture records at most. Output past a
#: stream's limit is counted and replaced by one marker; a line past its limit
#: is cut, and the end of what is kept goes too (`CUT_LINE_MARGIN`).
STREAM_LIMIT_BYTES = 4 * 1024 * 1024
LINE_LIMIT_CHARS = 64 * 1024
CUT_LINE_MARGIN = 256

#: Private key material spans lines, so it is handled as a state rather than a
#: pattern: everything between the markers is dropped, markers included.
PRIVATE_KEY_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
PRIVATE_KEY_END = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY-----")


@dataclass(frozen=True)
class Operator:
    """Which human asked for a privileged run, and which mechanism said so.

    Only the two mechanisms that actually elevated this process are consulted.
    `USER`, `LOGNAME` and `os.getlogin()` are not: they describe whoever
    happened to set them, and a record that names a human on that basis is
    worse than one that names nobody.

    Polkit first. `PKEXEC_UID` is what pkexec sets for the invoking user, and
    when a run was elevated by pkexec that is the mechanism that authorized it
    -- `SUDO_USER` may still be lying around from an outer shell. Both are
    resolved through the account database, so a value that names no account
    records as unknown rather than as itself (SYRD-132).
    """

    name: str = ""
    uid: int | None = None
    source: str = ""

    @property
    def known(self) -> bool:
        return bool(self.name)


def resolve_operator(environ: dict[str, str] | None = None) -> Operator:
    """The invoking human, or nobody. Never a guess."""
    env = os.environ if environ is None else environ
    raw_uid = str(env.get("PKEXEC_UID", "")).strip()
    if raw_uid:
        if not raw_uid.isdigit():
            return Operator(source="pkexec:unresolved")
        try:
            record = pwd.getpwuid(int(raw_uid))
        except (KeyError, OverflowError, ValueError):
            return Operator(source="pkexec:unresolved")
        return Operator(name=record.pw_name, uid=record.pw_uid, source="pkexec")
    sudo_user = str(env.get("SUDO_USER", "")).strip()
    if sudo_user:
        try:
            record = pwd.getpwnam(sudo_user)
        except KeyError:
            return Operator(source="sudo:unresolved")
        return Operator(name=record.pw_name, uid=record.pw_uid, source="sudo")
    return Operator()


def journal_root() -> Path:
    configured = os.environ.get(JOURNAL_ROOT_ENV, "").strip()
    return Path(configured) if configured else Path(DEFAULT_JOURNAL_ROOT)


def project_journal_dir(project: str, *, root: Path | str | None = None) -> Path:
    base = Path(root) if root is not None else journal_root()
    return base / project


def index_path(project: str, *, root: Path | str | None = None) -> Path:
    return project_journal_dir(project, root=root) / INDEX_NAME


class Redactor:
    """One stream's worth of redaction state.

    Line-oriented, and deliberately not clever: a partial line is held until its
    newline arrives so a pattern cannot be split across two writes and slip
    through.
    """

    def __init__(self, max_line: int | None = None) -> None:
        self._pending = ""
        self._in_private_key = False
        #: SYRD-113: the most of one line held while its newline is awaited.
        #: Unbounded, a line that never ends is memory that never stops
        #: growing. What is past it is counted and not recorded.
        self.max_line = max_line
        self._dropped = 0

    def feed(self, text: str) -> str:
        out: list[str] = []
        while text:
            newline = text.find("\n")
            part, text = (text, "") if newline < 0 else (text[: newline + 1], text[newline + 1 :])
            self._take(part)
            if part.endswith("\n"):
                out.append(self._emit())
        return "".join(out)

    def flush(self) -> str:
        """Whatever never got its newline. Redacted the same way."""
        if not self._pending and not self._dropped:
            return ""
        return self._emit()

    def _take(self, part: str) -> None:
        if self.max_line is None:
            self._pending += part
            return
        ends = part.endswith("\n")
        body = part[:-1] if ends else part
        room = max(self.max_line - len(self._pending), 0)
        self._pending += body[:room]
        self._dropped += len(body) - len(body[:room])
        if ends:
            self._pending += "\n"

    def _emit(self) -> str:
        line, self._pending = self._pending, ""
        dropped, self._dropped = self._dropped, 0
        if dropped:
            # A secret may straddle the cut, and the part of it before the cut
            # can be too short for any pattern to know it for one. So the end
            # of a cut line is not recorded either.
            kept = line.rstrip("\n")
            margin = min(len(kept), CUT_LINE_MARGIN)
            kept, dropped = kept[: len(kept) - margin], dropped + margin
            line = f"{kept} [switchyard: {dropped} more characters of this line were not recorded]\n"
        return self._line(line)

    def _line(self, line: str) -> str:
        if self._in_private_key:
            if PRIVATE_KEY_END.search(line):
                self._in_private_key = False
            return ""
        if PRIVATE_KEY_BEGIN.search(line):
            self._in_private_key = not bool(PRIVATE_KEY_END.search(line))
            return "<redacted private key>\n"
        for pattern, replacement in REDACTIONS:
            line = pattern.sub(replacement, line)
        return line


def redact(text: str) -> str:
    """One-shot redaction, for text that is not a stream."""
    redactor = Redactor()
    return redactor.feed(text) + redactor.flush()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IndexEntry:
    """One line of the journal index."""

    attempt: str
    event: str
    at: str
    previous: str
    digest: str
    detail: dict = field(default_factory=dict)

    def to_json(self) -> str:
        body = {
            "schema": SCHEMA,
            "attempt": self.attempt,
            "event": self.event,
            "at": self.at,
            "previous": self.previous,
            **self.detail,
        }
        return json.dumps(body, sort_keys=True, separators=(",", ":"))


def read_index(project: str, *, root: Path | str | None = None) -> list[dict]:
    path = index_path(project, root=root)
    if not path.is_file():
        return []
    entries: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except ValueError:
            entries.append({"malformed": line})
    return entries


def verify_index(project: str, *, root: Path | str | None = None) -> list[str]:
    """What is wrong with this journal, or nothing.

    The chain is over the LINE as it was written, so an entry that is edited,
    removed or reordered WITHOUT the hashes after it being recomputed breaks
    every one of them. That is the class this catches: accidental edits, a
    truncated or interrupted write, a line deleted by hand.

    It is not a proof against root. Everything here -- the entries and the
    hashes linking them -- is in one root-owned file, so a root that rewrites
    the file and recomputes the chain produces a journal that verifies clean.
    What root ownership buys is that the project account, which is every role,
    cannot do any of it.
    """
    problems: list[str] = []
    previous = ""
    for position, entry in enumerate(read_index(project, root=root), start=1):
        if "malformed" in entry:
            problems.append(f"entry {position} is not readable as JSON")
            previous = ""
            continue
        recorded_previous = str(entry.get("previous") or "")
        if recorded_previous != previous:
            problems.append(
                f"entry {position} ({entry.get('attempt', '?')} {entry.get('event', '?')}) "
                f"follows {recorded_previous[:12] or 'nothing'}, but the entry before it hashes "
                f"to {previous[:12] or 'nothing'}"
            )
        line = json.dumps(
            {key: value for key, value in entry.items() if key != "line_digest"},
            sort_keys=True,
            separators=(",", ":"),
        )
        previous = sha256_text(line)
    return problems


def attempts(project: str, *, root: Path | str | None = None) -> list[dict]:
    """Every attempt this journal knows about, oldest first.

    Built from the index rather than from the directory listing, so an attempt
    whose directory was removed still appears -- as one that started and whose
    result is missing.
    """
    seen: dict[str, dict] = {}
    for entry in read_index(project, root=root):
        attempt = str(entry.get("attempt") or "")
        if not attempt:
            continue
        record = seen.setdefault(
            attempt,
            {"attempt": attempt, "started_at": "", "finished_at": "", "status": "interrupted",
             "exit_status": None, "command": entry.get("command", []), "directory": ""},
        )
        if entry.get("event") == "start":
            record["started_at"] = entry.get("at", "")
            record["command"] = entry.get("command", record["command"])
            record["directory"] = entry.get("directory", "")
            record["target_commit"] = entry.get("target_commit", "")
            if isinstance(entry.get("job"), dict):
                record["job"] = entry["job"]
        elif entry.get("event") == "finish":
            record["finished_at"] = entry.get("at", "")
            record["status"] = entry.get("status", "")
            record["exit_status"] = entry.get("exit_status")
        elif entry.get("event") == "pruned":
            record["pruned_at"] = entry.get("at", "")
    ordered = sorted(seen.values(), key=lambda item: item["attempt"])
    for record in ordered:
        directory = attempt_directory(record)
        record["result_present"] = bool(directory and (directory / RESULT_NAME).is_file())
    return ordered


def attempt_directory(record: dict) -> Path | None:
    """The directory an attempt recorded, or None when it recorded none to read.

    The writer records the absolute path it created. A record whose directory is
    missing, empty, null, not a string or relative names no fixed place: an
    empty one becomes Path(""), which is wherever the reader is standing, and a
    relative one is read against it. Such a record reaches a reader when the
    index loses or garbles an attempt's start entry -- its finish entry alone
    still makes an attempt -- or when the entry is edited. Reading its "result"
    would present unrelated files as the attempt's, so nothing is read for it
    (SYRD-441).
    """
    recorded = record.get("directory")
    if not isinstance(recorded, str) or not recorded:
        return None
    directory = Path(recorded)
    return directory if directory.is_absolute() else None


def unusable_directory_reason(record: dict) -> str:
    """Why `attempt_directory` gave nothing for this record, in words."""
    recorded = record.get("directory")
    if recorded is None or recorded == "":
        return "recorded no directory"
    return f"recorded the directory {recorded!r}, which is not an absolute path"


def format_attempts(project: str, records: Iterable[dict], problems: Iterable[str]) -> str:
    lines = [f"switchyard: {project} rollout journal"]
    listed = list(records)
    if not listed:
        lines.append("  no attempts recorded")
    for record in listed:
        status = record.get("status") or "interrupted"
        exit_status = record.get("exit_status")
        outcome = status if exit_status is None else f"{status} (exit {exit_status})"
        lines.append(
            f"  {record['attempt']}  {record.get('started_at', '') or '?':<24} {outcome}"
        )
        if record.get("pruned_at"):
            lines.append(f"      removed by retention at {record['pruned_at']}")
            continue
        if attempt_directory(record) is None:
            lines.append(f"      {unusable_directory_reason(record)}; nothing is read for it")
            continue
        lines.append(f"      {record['directory']}")
        job = record.get("job")
        if isinstance(job, dict):
            lines.append(
                f"      {job.get('action', '?')} for {job.get('ticket') or 'no ticket'}, "
                f"asked by {job.get('requester_role') or 'an unrecorded role'}"
            )
        if not record.get("result_present", True):
            lines.append("      result.json is missing from this attempt's directory")
    listed_problems = list(problems)
    if listed_problems:
        lines.append("  journal integrity:")
        lines.extend(f"    {problem}" for problem in listed_problems)
    return "\n".join(lines)


def tenant_group(project: str, *, unit_dir: Path | str | None = None, owner_uid: int = 0) -> int | None:
    """The group `project`'s board service runs with, from root's own unit for it; or None.

    The unit is read without following a link and believed only if it is
    root's and writable by nobody else: its `Group=` is what makes the board
    service able to read this journal and nobody else's (SYRD-113). A project
    named with a leading underscore (`_host`) is no tenant.
    """
    if not project or project.startswith("_"):
        return None
    directory = Path(unit_dir) if unit_dir is not None else SYSTEM_UNIT_DIR
    try:
        fd = os.open(directory / f"{project}-ticket-board.service", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        status = os.fstat(fd)
        if (not stat.S_ISREG(status.st_mode) or status.st_uid != owner_uid or status.st_mode & 0o022
                or status.st_size > UNIT_LIMIT_BYTES):
            return None
        text = os.read(fd, UNIT_LIMIT_BYTES).decode("utf-8", "replace")
    finally:
        os.close(fd)
    section, name = "", ""
    for line in (raw.strip() for raw in text.splitlines()):
        if line.startswith("[") and line.endswith("]"):
            section = line
        elif section == "[Service]" and line.startswith("Group="):
            name = line.partition("=")[2].strip()
    if not name:
        return None
    try:
        return grp.getgrnam(name).gr_gid
    except KeyError:
        return None


def _append_event(index: Path, attempt: str, event: str, detail: dict, group: int | None) -> None:
    """Chain one event onto the index, as `Attempt` does."""
    previous = ""
    if index.is_file():
        for line in index.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                entry = {"malformed": line}
            previous = sha256_text(json.dumps(entry, sort_keys=True, separators=(",", ":")))
    line = IndexEntry(attempt=attempt, event=event, at=now_text(), previous=previous, digest="", detail=detail).to_json()
    with index.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    if group is not None:
        os.chown(index, -1, group)
    os.chmod(index, INDEX_MODE if group is None else TENANT_INDEX_MODE)


def prune(project: str, *, root: Path | str | None = None, now: float | None = None,
          keep: int = RETENTION_KEEP, days: float = RETENTION_DAYS) -> list[str]:
    """Remove finished attempts past retention, saying so in the index. What was removed."""
    base = project_journal_dir(project, root=root)
    if base.is_symlink() or not base.is_dir():
        return []
    names = sorted(entry.name for entry in base.iterdir() if ATTEMPT_NAME.fullmatch(entry.name))
    cutoff = (time.time() if now is None else now) - days * 86400
    group = None if base.stat().st_gid == 0 else base.stat().st_gid
    removed: list[str] = []
    for name in names[: max(len(names) - keep, 0)]:
        directory = base / name
        if directory.is_symlink() or not directory.is_dir():
            continue
        result = directory / RESULT_NAME
        try:
            record = json.loads(result.read_text(encoding="utf-8")) if result.is_file() and not result.is_symlink() else {}
        except (OSError, ValueError):
            record = {}
        if record.get("status") == "running":
            continue
        finished = (result if result.is_file() and not result.is_symlink() else directory).lstat().st_mtime
        if finished > cutoff:
            continue
        _append_event(base / INDEX_NAME, name, "pruned", {"retention_days": days, "kept_newest": keep}, group)
        shutil.rmtree(directory)
        removed.append(name)
    return removed


def repair_journal(project: str, *, root: Path | str | None = None, group: object = _RESOLVE,
                   owner_uid: int = 0, now: float | None = None) -> list[str]:
    """Bring a tenant's journal to its tenant-group modes, and apply retention. What it did, and what it could not.

    Run by root on every upgrade, at provisioning and by `repair-boundary`.
    Nothing is followed: a link anywhere is reported and left alone, and so is
    anything that is not root's.
    """
    gid = tenant_group(project) if group is _RESOLVE else group
    said: list[str] = []
    base = project_journal_dir(project, root=root)
    if gid is None:
        if not project.startswith("_"):
            said.append(f"{project}'s board unit names no group, so its rollout journal stays readable by every account")
        return said + [f"pruned {name}" for name in prune(project, root=root, now=now)]
    if base.is_symlink():
        return [f"{base} is a link; left alone"]
    if not base.exists():
        base.mkdir(parents=True)
        os.chown(base, -1, gid)
        os.chmod(base, TENANT_DIRECTORY_MODE)
        said.append(f"created {base}")

    def fix(fd: int, path: Path, mode: int) -> None:
        status = os.fstat(fd)
        if status.st_uid != owner_uid:
            said.append(f"{path} is owned by uid {status.st_uid}, not root; left alone")
            return
        if status.st_gid != gid or stat.S_IMODE(status.st_mode) != mode:
            os.fchown(fd, -1, gid)
            os.fchmod(fd, mode)
            said.append(f"{path}: {stat.S_IMODE(status.st_mode):o} group {status.st_gid} -> {mode:o} group {gid}")

    def opened(path: Path, name: str, dir_fd: int, directory: bool) -> int | None:
        try:
            return os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | (os.O_DIRECTORY if directory else 0),
                           dir_fd=dir_fd)
        except OSError:
            said.append(f"{path} cannot be opened without following a link; left alone")
            return None

    base_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        fix(base_fd, base, TENANT_DIRECTORY_MODE)
        for name in sorted(os.listdir(base_fd)):
            path = base / name
            if name == INDEX_NAME:
                fd = opened(path, name, base_fd, False)
                if fd is not None:
                    try:
                        fix(fd, path, TENANT_INDEX_MODE)
                    finally:
                        os.close(fd)
                continue
            if not ATTEMPT_NAME.fullmatch(name):
                continue
            fd = opened(path, name, base_fd, True)
            if fd is None:
                continue
            try:
                fix(fd, path, TENANT_DIRECTORY_MODE)
                try:
                    with open(os.open(RESULT_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd),
                              encoding="utf-8") as handle:
                        running = json.load(handle).get("status") == "running"
                except (OSError, ValueError, AttributeError):
                    running = False
                for child in sorted(os.listdir(fd)):
                    child_fd = opened(path / child, child, fd, False)
                    if child_fd is None:
                        continue
                    try:
                        if stat.S_ISREG(os.fstat(child_fd).st_mode):
                            fix(child_fd, path / child, TENANT_LIVE_MODE if running else TENANT_COMPLETED_MODE)
                    finally:
                        os.close(child_fd)
            finally:
                os.close(fd)
    finally:
        os.close(base_fd)
    return said + [f"pruned {name}" for name in prune(project, root=root, now=now)]


class Attempt:
    """One privileged run, recorded as it happens.

    Root writes; everybody reads. The directory and its files are created by
    this process, so nothing a role account could have pre-created is written
    into -- a pre-existing attempt directory is an error rather than something
    to reuse.
    """

    def __init__(
        self,
        project: str,
        command: list[str],
        *,
        root: Path | str | None = None,
        target_commit: str = "",
        operator: str = "",
        group: object = _RESOLVE,
    ) -> None:
        self.project = project
        self.command = list(command)
        self.target_commit = target_commit
        # A caller may name the operator explicitly; otherwise it comes from
        # the mechanism that elevated this process, and from nothing else.
        self.operator = Operator(name=operator, source="explicit") if operator else resolve_operator()
        self.base = project_journal_dir(project, root=root)
        self.index = self.base / INDEX_NAME
        self.attempt = ""
        self.directory = Path()
        self.started_at = ""
        self._stdout = None
        self._stderr = None
        self._redactors: dict[str, Redactor] = {}
        self.job: dict | None = None
        self._capture: dict[str, dict] | None = None
        #: The tenant's group, resolved when the attempt opens (`tenant_group`), or None.
        self.group = group
        #: What closing this attempt repaired or pruned in the journal (`repair_journal`).
        self.repairs: list[str] = []

    def _mode(self, legacy: int, tenant: int) -> int:
        return legacy if self.group is None else tenant

    def _own(self, path: Path) -> None:
        if self.group is not None:
            os.chown(path, -1, self.group)

    def describe_job(self, job: dict) -> None:
        """Record this run as a privileged job (SYRD-113), before it is opened.

        `job` says what was asked for and on whose behalf: the action, its
        validated values, the originating ticket and the requesting role. A job
        also captures bounded output: each line timestamped, each stream cut
        at `STREAM_LIMIT_BYTES` and each line at `LINE_LIMIT_CHARS`, with
        what was dropped counted in the result and marked in the log.
        """
        self.job = dict(job)
        self._capture = {
            stream: {"limit": STREAM_LIMIT_BYTES, "recorded_bytes": 0, "dropped_bytes": 0,
                     "dropped_lines": 0, "truncated": False}
            for stream in ("stdout", "stderr")
        }

    # -- lifecycle ----------------------------------------------------------

    def open(self) -> Path:
        if self.group is _RESOLVE:
            # Only root writes a real journal, and only root can give a file to
            # a tenant's group; any other writer keeps the legacy modes.
            self.group = tenant_group(self.project) if os.geteuid() == 0 else None
        self.base.mkdir(parents=True, exist_ok=True)
        self._own(self.base)
        os.chmod(self.base, self._mode(DIRECTORY_MODE, TENANT_DIRECTORY_MODE))
        self.started_at = now_text()
        self.attempt = self._next_attempt_id()
        self.directory = self.base / self.attempt
        # Not exist_ok: reusing a directory is how two runs come to share a log.
        self.directory.mkdir()
        self._own(self.directory)
        os.chmod(self.directory, self._mode(DIRECTORY_MODE, TENANT_DIRECTORY_MODE))
        self._stdout = (self.directory / "stdout.log").open("w", encoding="utf-8")
        self._stderr = (self.directory / "stderr.log").open("w", encoding="utf-8")
        if self.group is not None:
            for name in ("stdout.log", "stderr.log"):
                self._own(self.directory / name)
                os.chmod(self.directory / name, TENANT_LIVE_MODE)
        line_limit = LINE_LIMIT_CHARS if self._capture is not None else None
        self._redactors = {"stdout": Redactor(line_limit), "stderr": Redactor(line_limit)}
        self._append_index(
            "start",
            {
                "command": self.command,
                "directory": str(self.directory),
                "target_commit": self.target_commit,
                "operator": self.operator.name,
                "operator_source": self.operator.source,
                **({"job": self.job} if self.job is not None else {}),
            },
        )
        self._write_result(status="running", exit_status=None, finished_at="")
        return self.directory

    def write(self, stream: str, text: str) -> None:
        handle = self._stdout if stream == "stdout" else self._stderr
        if handle is None:
            return
        handle.write(self._bounded(stream, self._redactors[stream].feed(text)))
        handle.flush()

    def _bounded(self, stream: str, redacted: str) -> str:
        """A job's redacted lines, timestamped and cut at the stream's limit. Anything else, as is."""
        if self._capture is None or not redacted:
            return redacted
        counts = self._capture[stream]
        stamp = now_text()
        kept: list[str] = []
        for line in redacted.splitlines(keepends=True):
            line = f"{stamp} {line}"
            size = len(line.encode("utf-8"))
            if counts["truncated"] or counts["recorded_bytes"] + size > counts["limit"]:
                counts["truncated"] = True
                counts["dropped_bytes"] += size
                counts["dropped_lines"] += 1
                continue
            counts["recorded_bytes"] += size
            kept.append(line)
        return "".join(kept)

    def close(self, *, status: str, exit_status: int | None, detail: str = "") -> dict:
        for stream, handle in (("stdout", self._stdout), ("stderr", self._stderr)):
            if handle is None:
                continue
            handle.write(self._bounded(stream, self._redactors[stream].flush()))
            if self._capture is not None and self._capture[stream]["truncated"]:
                counts = self._capture[stream]
                handle.write(
                    f"{now_text()} [switchyard: output truncated. {counts['dropped_bytes']} bytes in "
                    f"{counts['dropped_lines']} lines after the first {counts['recorded_bytes']} bytes "
                    f"were not recorded; the limit is {counts['limit']} bytes]\n"
                )
            handle.flush()
            handle.close()
        self._stdout = self._stderr = None
        finished_at = now_text()
        record = self._write_result(
            status=status, exit_status=exit_status, finished_at=finished_at, detail=detail
        )
        for name in ("stdout.log", "stderr.log", RESULT_NAME):
            path = self.directory / name
            if path.is_file():
                os.chmod(path, self._mode(COMPLETED_MODE, TENANT_COMPLETED_MODE))
        self._append_index(
            "finish",
            {
                "status": status,
                "exit_status": exit_status,
                "detail": detail,
                "artifacts": record["artifacts"],
                "result_sha256": sha256_file(self.directory / RESULT_NAME),
            },
        )
        # Every root write to a tenant's journal leaves the whole journal in its
        # tenant-group modes and within retention: provisioning's recorded
        # packet, `upgrade-tenant`, `repair-boundary` and every other privileged
        # action all close an attempt. A board unit installed during this very
        # run -- provisioning's -- names its group now if it did not at open.
        try:
            if self.group is None and os.geteuid() == 0:
                self.group = tenant_group(self.project)
            self.repairs = repair_journal(self.project, root=self.base.parent, group=self.group,
                                          owner_uid=os.geteuid())
        except OSError as exc:
            self.repairs = [f"the journal could not be repaired: {exc}"]  # never fails the run it follows
        return record

    # -- the files ----------------------------------------------------------

    def _write_result(
        self, *, status: str, exit_status: int | None, finished_at: str, detail: str = ""
    ) -> dict:
        artifacts = {
            name: sha256_file(self.directory / name)
            for name in ("stdout.log", "stderr.log")
            if (self.directory / name).is_file()
        }
        record = {
            "schema": SCHEMA,
            "project": self.project,
            "attempt": self.attempt,
            "command": self.command,
            "target_commit": self.target_commit,
            "operator": self.operator.name,
            "operator_uid": self.operator.uid,
            # How that name was established, or why there is none: a reader can
            # tell "nobody was recorded" from "polkit named a uid this host does
            # not know" (SYRD-132).
            "operator_source": self.operator.source,
            "started_at": self.started_at,
            "finished_at": finished_at,
            "status": status,
            "exit_status": exit_status,
            "detail": detail,
            "artifacts": artifacts,
        }
        if self.job is not None:
            record["job"] = self.job
            record["capture"] = {
                stream: {**counts, "bytes": (self.directory / f"{stream}.log").stat().st_size
                         if (self.directory / f"{stream}.log").is_file() else 0}
                for stream, counts in (self._capture or {}).items()
            }
        path = self.directory / RESULT_NAME
        # Rewritten in place while the run is live, because the record has to
        # exist from the first moment: a run killed before it can finish is one
        # this file says is still running, which is the truth.
        live = self._mode(0o644, TENANT_LIVE_MODE)
        if path.exists():
            os.chmod(path, live)
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self._own(path)
        os.chmod(path, live if status == "running" else self._mode(COMPLETED_MODE, TENANT_COMPLETED_MODE))
        return record

    def _next_attempt_id(self) -> str:
        highest = 0
        for entry in read_index(self.project, root=self.base.parent):
            attempt = str(entry.get("attempt") or "")
            number = attempt.split("-", 1)[0]
            if number.isdigit():
                highest = max(highest, int(number))
        for existing in self.base.glob("[0-9]*-*"):
            number = existing.name.split("-", 1)[0]
            if number.isdigit():
                highest = max(highest, int(number))
        stamp = self.started_at.replace("-", "").replace(":", "")
        return f"{highest + 1:04d}-{stamp}"

    def _append_index(self, event: str, detail: dict) -> None:
        _append_event(self.index, self.attempt, event, detail, None if self.group is _RESOLVE else self.group)


def now_text() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
