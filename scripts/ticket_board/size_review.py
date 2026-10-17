"""The board's half of the size review (SYRD-541).

The board measures -- on its own commit cache, with file_size_policy -- and
records; the database decides (ticket_board.enforce_size_review refuses an
Audit approval or a Director's close of a commit it has not measured, or with
unresolved findings). Measurement runs just before the transition, in its own
transaction, so a refused transition still leaves its finding on the board.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import commit_cache_reader
from . import file_size_policy as policy

OPERATIONS = frozenset({"approve_size_exception", "enable_size_review", "measure_size"})
_MAIN_REFS = ("refs/remotes/origin/main", "refs/heads/main")
_SHA = re.compile(r"[0-9a-f]{40}")
#: What a ticket may name for measurement: a commit id, never a ref, a path or an option.
_COMMIT_ID = re.compile(r"[0-9A-Fa-f]{7,40}")


def _reader(app: Any) -> commit_cache_reader.CacheReader:
    """The board's trusted commit cache, opened for reading by the board's own account.

    Only the configured caches, never a path from the ticket (SYRD-571). When
    none can be opened, the scan fails with each one's reason.
    """
    problems: list[str] = []
    for git_dir in getattr(app, "commit_git_dirs", ()) or ():
        try:
            return commit_cache_reader.cache_reader(Path(git_dir))
        except (PermissionError, ValueError) as exc:
            problems.append(str(exc))
    if not problems:
        raise policy.ScanError("the board has no trusted commit cache to measure in")
    raise policy.ScanError("; ".join(problems))


def _with_repair(app: Any, error: str) -> str:
    """A refusal of the board's own account names the supported repair: the grant, re-applied."""
    if "Permission denied" not in error and "cannot read its trusted commit cache" not in error:
        return error
    return (f"{error}. Repair: pkexec switchyard repair-boundary {getattr(app, 'project', '<project>')} --apply "
            "grants the board service read access to a commit store in the tenant's home, and names the "
            "grant a store another account owns needs")


def _present(reader: commit_cache_reader.CacheReader, ticket_id: str, raw: str, what: str) -> str:
    """The full id of a commit in the cache, or a failure saying how to bring it there."""
    if not _COMMIT_ID.fullmatch(raw):
        raise policy.ScanError(f"{raw!r} is not a commit id")
    try:
        return policy.resolve(reader, raw)
    except policy.ScanError as exc:
        if "Needed a single revision" not in str(exc):
            raise  # not a missing commit (an unreadable object, say): git's own reason
        again = (f"the next approval or close measures again, or the Director runs ticket-board-write measure-size "
                 f"{ticket_id} --commit {raw}" if ticket_id else "then run enable-size-review again")
        raise policy.ScanError(
            f"the {what} {raw} is not in the board's trusted commit cache {reader.git_dir}. Repair: publish it "
            f"(switchyard-publish-candidate brings the cache up to what it publishes); {again}") from None


def _main_commit(reader: commit_cache_reader.CacheReader) -> str:
    for ref in _MAIN_REFS:
        try:
            return policy.resolve(reader, ref)
        except policy.ScanError:
            continue
    raise policy.ScanError(f"the board's trusted commit cache {reader.git_dir} has no main to measure against")


def _first_parent(reader: commit_cache_reader.CacheReader, commit: str) -> str:
    try:
        return policy.resolve(reader, f"{commit}^1")
    except policy.ScanError:
        raise policy.ScanError(f"{commit[:12]} has no parent in the board's trusted commit cache "
                               f"{reader.git_dir} to measure against") from None


def _gated(cfg: dict, transition: dict) -> tuple[bool, bool]:
    """(gated, terminal): an approval, or a commit-carrying move into a terminal stage."""
    terminal = any(s["name"] == transition["to"] and s.get("terminal") for s in cfg["stages"])
    record = bool(transition.get("require_commit")) and terminal
    return transition.get("primitive") == "approve" or record, record


def _last_scan(conn: Any, ticket_id: str) -> dict | None:
    return conn.execute("SELECT candidate, base FROM ticket_board.size_scans WHERE ticket_id = %s",
                        (ticket_id,)).fetchone()


def scan_for_transition(app: Any, ticket_id: str, action: str, payload: dict[str, Any], *, caller_role: str) -> None:
    """Measure the commit a gated or commit-carrying transition is about, and record it.

    Nothing here allows or refuses anything: the database does, from what is
    recorded. A failure to measure is recorded as a failed scan, which the
    database treats as unresolved.
    """
    cfg = app.workflow_configuration()
    if not cfg:
        return
    ticket = app.get_ticket(ticket_id)
    target = payload.get("target")
    transition = next((t for t in cfg["transitions"]
                       if t["action"] == action and t["from"] == ticket["state"]
                       and (not target or t["to"] == target)), None)
    if transition is None:
        return
    gated, record = _gated(cfg, transition)
    raw = str(payload.get("commit_hash") or ticket.get("commit_hash") or "").strip()
    if not (gated or payload.get("commit_hash")) or not raw:
        return
    measure_commit(app, ticket_id, raw, close=record, caller_role=caller_role)


def measure_commit(app: Any, ticket_id: str, raw: str, *, close: bool, caller_role: str) -> None:
    """Measure `raw` for this ticket now, and record the scan.

    A candidate is measured against its merge-base with main. A commit the
    Director closes on, or rechecks before pushing, is measured against the
    base the ticket's candidate was measured on when it is that same commit,
    and otherwise against its first parent: main just before a cherry-pick or
    a merge. Always measured afresh -- the database accepts only a scan made
    for the action at hand.
    """
    candidate, base, report, findings, error = raw, None, [], [], None
    try:
        repo = _reader(app)
        candidate = _present(repo, ticket_id, raw, "candidate")
        if close:
            with app._pg_connect() as conn:
                previous = _last_scan(conn, ticket_id)
            same = previous and previous["candidate"] == candidate and previous["base"]
            base = _present(repo, ticket_id, previous["base"], "base") if same else _first_parent(repo, candidate)
        else:
            base = policy.merge_base(repo, _main_commit(repo), candidate)
        measured = policy.measure(repo, base, candidate)
        with app._pg_connect() as conn:
            named = [f.path for f in measured.files] + [f.previous_path for f in measured.files if f.previous_path]
            ceilings = conn.execute("SELECT ticket_board.size_ceilings(%s, %s) AS c",
                                    (ticket_id, named)).fetchone()["c"] or {}
        for growth in measured.files:
            # A renamed file keeps its allowance: its own path's, else the one it had.
            ceiling = ceilings.get(growth.path, ceilings.get(growth.previous_path or ""))
            reason = policy.classify(growth, ceiling)
            detail = f"{growth.before} -> {growth.after} lines"
            if growth.path in policy.MIRROR_PATHS:
                detail += f"; {growth.unmirrored} added lines appear in none of this candidate's migrations"
            if growth.band != "ok":
                report.append({"path": growth.path, "previous_path": growth.previous_path, "before": growth.before,
                               "after": growth.after, "growth": growth.growth, "band": growth.band,
                               "ceiling": ceiling, "finding": reason, "detail": detail})
            if reason:
                findings.append({"path": growth.path, "previous_path": growth.previous_path, "reason": reason,
                                 "before": growth.before, "after": growth.after, "ceiling": ceiling,
                                 "detail": detail})
    except policy.ScanError as exc:
        error = _with_repair(app, str(exc) or "size scan failed")
    if not _SHA.fullmatch(candidate):
        return  # nothing resolvable to record against; the database refuses an unmeasured gated commit
    with app._pg_connect() as conn:
        with conn.transaction():
            app._pg_set_caller_role(conn, caller_role)
            conn.execute("SELECT ticket_board.record_size_scan(%s, %s, %s, %s::jsonb, %s::jsonb, %s)",
                         (ticket_id, candidate, base, json.dumps(report), json.dumps(findings), error))


def perform(app: Any, operation: str, payload: dict[str, Any], *, caller_role: str,
            ticket_id: str | None = None) -> dict[str, Any]:
    apply = payload.get("apply", False)
    if type(apply) is not bool:
        raise ValueError("apply must be true or false")
    if operation == "approve_size_exception":
        if ticket_id is None or set(payload) - {"path", "rationale", "standing", "apply"}:
            raise ValueError("approve_size_exception takes a ticket, path, rationale, standing and apply")
        standing = payload.get("standing", False)
        if type(standing) is not bool:
            raise ValueError("standing must be true or false")
        sql = "SELECT ticket_board.approve_size_exception(%s, %s, %s, %s, %s) AS result"
        params: tuple[Any, ...] = (ticket_id, str(payload.get("path") or ""), str(payload.get("rationale") or ""),
                                   standing, apply)
    elif operation == "measure_size":
        # The Director's recheck before pushing an integration that changed the
        # tree or base: measure the commit now, move nothing.
        if ticket_id is None or set(payload) - {"commit"}:
            raise ValueError("measure_size takes a ticket and a commit")
        measure_commit(app, ticket_id, str(payload.get("commit") or ""), close=True, caller_role=caller_role)
        return dict(app.get_ticket(ticket_id).get("size_review") or {})
    elif operation == "enable_size_review":
        if ticket_id is not None or set(payload) - {"baseline", "carried", "apply"}:
            raise ValueError("enable_size_review takes baseline, carried and apply")
        carried = payload.get("carried") or []
        if not isinstance(carried, list):
            raise ValueError("carried must be a list of exceptions")
        try:
            repo = _reader(app)
            baseline = _present(repo, "", str(payload.get("baseline") or _main_commit(repo)), "baseline")
        except policy.ScanError as exc:
            raise ValueError(f"cannot take a baseline: {_with_repair(app, str(exc))}") from None
        inventory = [{"path": path, "lines": lines} for path, lines in policy.inventory(repo, baseline)]
        sql = "SELECT ticket_board.enable_size_review(%s, %s::jsonb, %s::jsonb, %s) AS result"
        params = (baseline, json.dumps(inventory), json.dumps(carried), apply)
    else:
        raise ValueError(f"unknown size review operation: {operation}")
    with app._pg_connect() as conn:
        with conn.transaction():
            app._pg_set_caller_role(conn, caller_role)
            row = conn.execute(sql, params).fetchone()
    return dict(row["result"])
