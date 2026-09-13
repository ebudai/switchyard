"""A rollout's own record of itself, written where the tenant cannot rewrite it.

The SYRD-126 operator wrappers captured a privileged run's output so the User
did not have to copy and paste it, and then wrote it under the project
account's home and chowned it there. Every role in a project runs as that one
account (SYRD-69), so the evidence of a privileged run ended up owned by every
role that run was evidence about: any of them could edit or delete it
afterwards, including by accident.

So the record is root's. The journal lives under a root-owned directory, each
completed file is mode 0444, and a role reads it without sudo and cannot write
it at all. Root-ownership alone would make tampering require root; the chain
below makes it VISIBLE even then, which is what the word journal is doing in
the title (SYRD-128).

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

import hashlib
import json
import os
import re
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

#: Private key material spans lines, so it is handled as a state rather than a
#: pattern: everything between the markers is dropped, markers included.
PRIVATE_KEY_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
PRIVATE_KEY_END = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY-----")


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

    def __init__(self) -> None:
        self._pending = ""
        self._in_private_key = False

    def feed(self, text: str) -> str:
        self._pending += text
        out: list[str] = []
        while True:
            newline = self._pending.find("\n")
            if newline < 0:
                break
            line, self._pending = self._pending[: newline + 1], self._pending[newline + 1 :]
            out.append(self._line(line))
        return "".join(out)

    def flush(self) -> str:
        """Whatever never got its newline. Redacted the same way."""
        if not self._pending:
            return ""
        line, self._pending = self._pending, ""
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

    The chain is over the LINE as it was written, so an edited, removed or
    reordered entry breaks every entry after it. That is the point: root can
    still rewrite the whole file, but it cannot quietly remove one attempt from
    the middle of it.
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
        elif entry.get("event") == "finish":
            record["finished_at"] = entry.get("at", "")
            record["status"] = entry.get("status", "")
            record["exit_status"] = entry.get("exit_status")
    ordered = sorted(seen.values(), key=lambda item: item["attempt"])
    for record in ordered:
        directory = Path(record.get("directory") or "")
        result = directory / RESULT_NAME if directory else None
        record["result_present"] = bool(result and result.is_file())
    return ordered


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
        if record.get("directory"):
            lines.append(f"      {record['directory']}")
        if not record.get("result_present", True):
            lines.append("      result.json is missing from this attempt's directory")
    listed_problems = list(problems)
    if listed_problems:
        lines.append("  journal integrity:")
        lines.extend(f"    {problem}" for problem in listed_problems)
    return "\n".join(lines)


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
    ) -> None:
        self.project = project
        self.command = list(command)
        self.target_commit = target_commit
        self.operator = operator or os.environ.get("SUDO_USER", "") or ""
        self.base = project_journal_dir(project, root=root)
        self.index = self.base / INDEX_NAME
        self.attempt = ""
        self.directory = Path()
        self.started_at = ""
        self._stdout = None
        self._stderr = None
        self._redactors: dict[str, Redactor] = {}

    # -- lifecycle ----------------------------------------------------------

    def open(self) -> Path:
        self.base.mkdir(parents=True, exist_ok=True)
        os.chmod(self.base, DIRECTORY_MODE)
        self.started_at = now_text()
        self.attempt = self._next_attempt_id()
        self.directory = self.base / self.attempt
        # Not exist_ok: reusing a directory is how two runs come to share a log.
        self.directory.mkdir()
        os.chmod(self.directory, DIRECTORY_MODE)
        self._stdout = (self.directory / "stdout.log").open("w", encoding="utf-8")
        self._stderr = (self.directory / "stderr.log").open("w", encoding="utf-8")
        self._redactors = {"stdout": Redactor(), "stderr": Redactor()}
        self._append_index(
            "start",
            {
                "command": self.command,
                "directory": str(self.directory),
                "target_commit": self.target_commit,
                "operator": self.operator,
            },
        )
        self._write_result(status="running", exit_status=None, finished_at="")
        return self.directory

    def write(self, stream: str, text: str) -> None:
        handle = self._stdout if stream == "stdout" else self._stderr
        if handle is None:
            return
        handle.write(self._redactors[stream].feed(text))
        handle.flush()

    def close(self, *, status: str, exit_status: int | None, detail: str = "") -> dict:
        for stream, handle in (("stdout", self._stdout), ("stderr", self._stderr)):
            if handle is None:
                continue
            handle.write(self._redactors[stream].flush())
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
                os.chmod(path, COMPLETED_MODE)
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
            "operator": self.operator,
            "started_at": self.started_at,
            "finished_at": finished_at,
            "status": status,
            "exit_status": exit_status,
            "detail": detail,
            "artifacts": artifacts,
        }
        path = self.directory / RESULT_NAME
        # Rewritten in place while the run is live, because the record has to
        # exist from the first moment: a run killed before it can finish is one
        # this file says is still running, which is the truth.
        if path.exists():
            os.chmod(path, 0o644)
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(path, 0o644 if status == "running" else COMPLETED_MODE)
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
        previous = ""
        for entry in read_index(self.project, root=self.base.parent):
            previous = sha256_text(
                json.dumps(entry, sort_keys=True, separators=(",", ":"))
            )
        line = IndexEntry(
            attempt=self.attempt,
            event=event,
            at=now_text(),
            previous=previous,
            digest="",
            detail=detail,
        ).to_json()
        with self.index.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        os.chmod(self.index, INDEX_MODE)


def now_text() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
