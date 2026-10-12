"""Root's privileged jobs, read by the board service and recorded on their tickets (SYRD-113).

Root's helper records each privileged run in its rollout journal, and a run
asked for a ticket carries its job: the action, its values, the ticket, the
requesting role and the release (slice A). This module is the board service's
reader of those records. It runs in the board service -- `boardsvc`, its own
account and its own database role -- and never in the notify listener, whose
account every role shares (the listener could be impersonated by any role
process; the service cannot).

A record is believed only as root wrote it. Every directory from the journal's
root down and every file read is opened without following a link, and must be
owned by root, writable by nobody but its owner, and in root's group or this
board's own tenant group. The record must name this project and an attempt
matching its directory, and a ticket of this board's prefix. A finished record
is read only within its bounds, and only when its logs hash to what its result
says and its result hashes to what root's index chained at finish.

What passes is handed to `ticket_board.record_privileged_job`, which records it
once by its attempt id. A finished job is recorded once, writes the ticket's
event and wakes the owner (and on a failure or timeout the controller), and
never changes again. Reading the same record after a restart or an outage
changes nothing, so the reconciler simply reads everything not yet finished,
on start and on a short interval.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import threading
from pathlib import Path
from typing import Any, Iterable

from . import rollout_journal

ATTEMPT_PATTERN = re.compile(r"[0-9]{4,}-[0-9]{8}T[0-9]{6}Z")
TICKET_PATTERN = re.compile(r"[A-Z][A-Z0-9]{0,15}-[1-9][0-9]{0,8}")
STATES = ("running", "succeeded", "failed", "timed_out", "rolled_back")
RESULT_LIMIT_BYTES = 1024 * 1024
INDEX_LIMIT_BYTES = 64 * 1024 * 1024
#: A finished log may exceed its stream's limit by its truncation marker and
#: the redacted tail written at close.
LOG_LIMIT_BYTES = rollout_journal.STREAM_LIMIT_BYTES + 64 * 1024
FINAL_LINES = 12
FINAL_LINE_CHARS = 400
INTERVAL_SECONDS = 30.0
INTERVAL_ENV = "TICKET_BOARD_PRIVILEGED_JOB_INTERVAL"


class Refused(Exception):
    """A record this board will not believe, and why."""


def _checked(fd: int, what: str, *, owner_uid: int, gids: Iterable[int], directory: bool) -> os.stat_result:
    status = os.fstat(fd)
    if directory and not stat.S_ISDIR(status.st_mode):
        raise Refused(f"{what} is not a directory")
    if not directory and not stat.S_ISREG(status.st_mode):
        raise Refused(f"{what} is not a regular file")
    if status.st_uid != owner_uid:
        raise Refused(f"{what} is owned by uid {status.st_uid}, not root's")
    if status.st_mode & 0o022:
        raise Refused(f"{what} is writable by its group or by anybody (mode {stat.S_IMODE(status.st_mode):o})")
    if status.st_gid not in set(gids):
        raise Refused(f"{what} belongs to group {status.st_gid}, neither root's nor this tenant's")
    return status


def _open_root(root: Path, *, owner_uid: int, gids: Iterable[int]) -> int:
    """The journal root, reached from / one component at a time, following no link.

    Its ancestors must be root's (or, under test, the stand-in owner's) and not
    writable by others unless sticky; the root itself is checked as a record is.
    """
    if not root.is_absolute():
        raise Refused(f"the journal root {root} is not an absolute path")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        parts = root.parts[1:]
        for position, part in enumerate(parts):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
            if position == len(parts) - 1:
                _checked(fd, f"the journal root {root}", owner_uid=owner_uid, gids=gids, directory=True)
                continue
            status = os.fstat(fd)
            if status.st_uid not in (0, owner_uid) or (status.st_mode & 0o022 and not status.st_mode & stat.S_ISVTX):
                raise Refused(f"{Path('/', *parts[:position + 1])} may be replaced by somebody other than root")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read(name: str, dir_fd: int, limit: int, what: str, *, owner_uid: int, gids: Iterable[int]) -> bytes:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    except OSError as exc:
        raise Refused(f"{what} cannot be opened without following a link ({exc.strerror})") from None
    try:
        status = _checked(fd, what, owner_uid=owner_uid, gids=gids, directory=False)
        if status.st_size > limit:
            raise Refused(f"{what} is {status.st_size} bytes, over its {limit}-byte bound")
        data = os.read(fd, limit + 1)
        if len(data) > limit:
            raise Refused(f"{what} grew past its {limit}-byte bound while it was read")
        return data
    finally:
        os.close(fd)


def _final_lines(text: str) -> list[str]:
    return [line[:FINAL_LINE_CHARS] for line in text.splitlines()[-FINAL_LINES:]]


def _read_attempt(name: str, project_fd: int, finishes: dict[str, dict], *, project: str, prefix: str,
                  root: Path, owner_uid: int, gids: Iterable[int]) -> dict | None:
    try:
        attempt_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=project_fd)
    except OSError as exc:
        raise Refused(f"its directory cannot be opened without following a link ({exc.strerror})") from None
    try:
        _checked(attempt_fd, "its directory", owner_uid=owner_uid, gids=gids, directory=True)
        raw = _read(rollout_journal.RESULT_NAME, attempt_fd, RESULT_LIMIT_BYTES, "its result",
                    owner_uid=owner_uid, gids=gids)
        try:
            result = json.loads(raw)
        except ValueError:
            raise Refused("its result is not JSON") from None
        job = result.get("job") if isinstance(result, dict) else None
        if not isinstance(job, dict) or not job.get("ticket"):
            return None  # a run asked for no ticket is not a job to report
        if result.get("schema") != rollout_journal.SCHEMA:
            raise Refused(f"its result has schema {result.get('schema')!r}")
        if result.get("project") != project or result.get("attempt") != name:
            raise Refused(f"its result names {result.get('project')!r} attempt {result.get('attempt')!r}")
        ticket = str(job.get("ticket"))
        if not TICKET_PATTERN.fullmatch(ticket) or ticket.rsplit("-", 1)[0] != prefix:
            raise Refused(f"its job names {ticket!r}, not a ticket of this board")
        state = result.get("status")
        if state not in STATES:
            raise Refused(f"its result has status {state!r}")
        record = {
            "job_id": name, "ticket": ticket, "action": str(job.get("action") or ""),
            "action_id": str(job.get("action_id") or ""),
            "arguments": job.get("arguments") if isinstance(job.get("arguments"), dict) else {},
            "requester_role": str(job.get("requester_role") or ""),
            "release_commit": str(job.get("release_commit") or ""),
            "state": state, "exit_status": result.get("exit_status"),
            "started_at": str(result.get("started_at") or ""), "finished_at": str(result.get("finished_at") or ""),
            "record_dir": str(root / project / name),
        }
        if state == "running":
            return record
        logs = {}
        for stream in ("stdout", "stderr"):
            data = _read(f"{stream}.log", attempt_fd, LOG_LIMIT_BYTES, f"its {stream}.log",
                         owner_uid=owner_uid, gids=gids)
            if hashlib.sha256(data).hexdigest() != (result.get("artifacts") or {}).get(f"{stream}.log"):
                raise Refused(f"its {stream}.log does not hash to what its result recorded")
            logs[stream] = data.decode("utf-8", "replace")
        finish = finishes.get(name)
        if finish is None or finish.get("result_sha256") != hashlib.sha256(raw).hexdigest():
            raise Refused("its result does not hash to what root's index recorded at finish")
        return {
            **record,
            "artifacts": {f"{stream}.log": result["artifacts"][f"{stream}.log"] for stream in logs},
            "result_sha256": finish["result_sha256"],
            "capture": result.get("capture") if isinstance(result.get("capture"), dict) else {},
            "final_lines": _final_lines(logs["stdout"]) + _final_lines(logs["stderr"]),
        }
    finally:
        os.close(attempt_fd)


def scan(project: str, prefix: str, *, root: Path | str | None = None, owner_uid: int = 0,
         gids: Iterable[int] | None = None, skip: Iterable[str] = ()) -> tuple[list[dict], list[str]]:
    """This project's job records that may be believed, and why any others may not.

    `owner_uid` and `gids` are root and root's or this process's group in
    production; a test stands in an owner it can create files as.
    """
    root = Path(root) if root is not None else rollout_journal.journal_root()
    allowed = {0, os.getgid()} if gids is None else set(gids)
    skipped = set(skip)
    if not os.path.lexists(root):
        return [], []  # a host that has run nothing privileged has no journal yet
    root_fd = _open_root(root, owner_uid=owner_uid, gids=allowed)
    try:
        try:
            project_fd = os.open(project, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=root_fd)
        except FileNotFoundError:
            return [], []
        try:
            _checked(project_fd, f"{project}'s journal", owner_uid=owner_uid, gids=allowed, directory=True)
            names = sorted(name for name in os.listdir(project_fd)
                           if ATTEMPT_PATTERN.fullmatch(name) and name not in skipped)
            if not names:
                return [], []
            finishes: dict[str, dict] = {}
            index = _read(rollout_journal.INDEX_NAME, project_fd, INDEX_LIMIT_BYTES, f"{project}'s index",
                          owner_uid=owner_uid, gids=allowed)
            for line in index.decode("utf-8", "replace").splitlines():
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get("event") == "finish":
                    finishes[str(entry.get("attempt"))] = entry
            jobs, refusals = [], []
            for name in names:
                try:
                    job = _read_attempt(name, project_fd, finishes, project=project, prefix=prefix, root=root,
                                        owner_uid=owner_uid, gids=allowed)
                except Refused as exc:
                    refusals.append(f"{project} attempt {name}: {exc}")
                    continue
                if job is not None:
                    jobs.append(job)
            return jobs, refusals
        finally:
            os.close(project_fd)
    finally:
        os.close(root_fd)


def _rows(result: Any) -> list[Any]:
    return [row[0] if not isinstance(row, dict) else next(iter(row.values())) for row in result.fetchall()]


def reconcile(app: Any, *, root: Path | str | None = None, owner_uid: int = 0,
              gids: Iterable[int] | None = None) -> dict[str, list[str]]:
    """Record every believable job this board does not already have finished."""
    with app._pg_connect() as conn:
        finished = set(_rows(conn.execute("SELECT * FROM ticket_board.finished_privileged_jobs()")))
    jobs, refusals = scan(str(app.project), str(app.ticket_prefix), root=root, owner_uid=owner_uid,
                          gids=gids, skip=finished)
    outcome: dict[str, list[str]] = {"recorded": [], "refused": list(refusals)}
    for job in jobs:
        try:
            with app._pg_connect() as conn:
                with conn.transaction():
                    conn.execute("SELECT set_config('ticket_board.privileged_job_reconciler', 'on', true)")
                    said = _rows(conn.execute("SELECT ticket_board.record_privileged_job(%s::jsonb)",
                                              (json.dumps(job),)))
            outcome["recorded"].append(f"{job['job_id']}: {said[0] if said else '?'}")
        except Exception as exc:  # noqa: BLE001 - one job's refusal does not stop the rest
            outcome["refused"].append(f"{app.project} attempt {job['job_id']}: {exc}")
    return outcome


def start_reconciler(app: Any, *, interval: float | None = None, stop: threading.Event | None = None,
                     log=lambda line: print(line, file=sys.stderr, flush=True),
                     **options: Any) -> threading.Thread | None:
    """Reconcile on start and then every `interval` seconds, in the board service.

    `options` reach `reconcile`; the service passes none, so it believes only root.
    """
    if getattr(app, "store_backend", "postgres") != "postgres":
        return None
    seconds = interval if interval is not None else float(os.environ.get(INTERVAL_ENV, "") or INTERVAL_SECONDS)
    done = stop or threading.Event()
    reported: set[str] = set()

    def loop() -> None:
        while True:
            try:
                outcome = reconcile(app, **options)
            except Exception as exc:  # noqa: BLE001 - the next pass tries again
                outcome = {"recorded": [], "refused": [f"the journal could not be read: {exc}"]}
            for line in outcome["recorded"]:
                if not line.endswith(": unchanged"):
                    log(f"[ticket-board] privileged job {line}")
            for line in outcome["refused"]:
                if line not in reported:
                    reported.add(line)
                    log(f"[ticket-board] privileged job refused: {line}")
            if done.wait(seconds):
                return

    thread = threading.Thread(target=loop, name="ticket-board-privileged-jobs", daemon=True)
    thread.start()
    return thread
