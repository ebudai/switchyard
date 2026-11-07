#!/usr/bin/env python3
"""SYRD-744: the Publisher returns a ticket to an author who is already busy, as itself.

On a pull-scheduled board an author is free once Audit approves (`release_after: audit`), so by the time the
Publisher decides a candidate cannot be fast-forwarded, its author has usually claimed other work. The return to
`in_progress` is then diverted, by serial focus, into the ready stage pinned to that author -- and the trigger that
announces the diversion named its actor with `current_app_actor()`, which on a declared board refuses a session with no
caller role. The Publisher's write has none: it acts through `ticket_board.workflow_actor`. So every such return was
refused -- `the board refused to record returned: invalid configured caller role: <NULL>` -- and escalated instead,
and a Director's `publication_retry` failed the same way (MEFP-947, twice, nothing pushed).

Here, on a production-built pull board with the Publisher composed in, and the real Publisher and git, both ways in:

* a first attempt returns a candidate public main has moved past, while its author holds other work;
* an attempt escalated to the Director (a refused credential), retried by the Director once main has moved on: the
  retry returns it, to the same busy author.

Each time: recorded as returned, never escalated over a refused caller role; diverted to the ready stage pinned to the
author, behind the work it holds; every comment and the queue notice attributed to `publisher`, none to nobody;
public main exactly as somebody else left it.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import publisher_board_test as pb  # noqa: E402
from pull_scheduling_policy_test import pull_document  # noqa: E402

CHECKS = 0
PERMISSION = "git@github.com: Permission denied (publickey).\nfatal: Could not read from remote repository."


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def pull_board_document() -> dict:
    """The shipped example under a pull policy, as MEFP runs, with the Publisher composed in."""
    cfg = pull_document()
    cfg["project"] = "pgu"
    for role in cfg["roles"]:
        if role.get("target"):
            role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
    return pb.composed(cfg)


def busy(bench: pb.Bench, implementer: str = "app") -> str:
    """Other work its author holds: a ticket the Director starts on it directly, left in implementation."""
    bench.number += 1
    t = f"PGU-{bench.number}"
    pb.fixture.seed_postgres_ticket(bench.board.admin, t, title=f"Fixture {t}", state="analysis", assignee="director")
    check(pb.short(bench.board.act(t, "route", "director", state="in_progress", assignee=implementer)) == "ok",
          f"{t} started on {implementer}")
    held = pb.row(bench.board, t)
    check(held["state"] == "in_progress" and held["assignee"] == implementer, f"{implementer} holds {t}: {held}")
    bench.made.append(t)  # put down with the case's other tickets before the next case
    return t


def candidate(bench: pb.Bench, implementer: str = "app") -> tuple[str, str]:
    """A ticket through Audit to the Publisher, touching src/app.py -- made without settling the author's other work."""
    settle, bench.settle = bench.settle, lambda: None
    try:
        return bench.ticket(implementer, files={"src/app.py": f"print('mine {bench.number}')\n"})
    finally:
        bench.settle = settle


def moved_on(bench: pb.Bench) -> str:
    """Somebody else changes the same file on public main: the candidate can no longer be fast-forwarded."""
    world = bench.world
    theirs = world.commit([world.main()], {"src/app.py": f"print('theirs {bench.number}')\n"}, "someone else")
    world.land_on_main(theirs)
    return theirs


def queue_row(bench: pb.Bench, t: str) -> dict:
    return json.loads(bench.board.sql(
        "SELECT json_build_object('state', state, 'assignee', assignee, 'queued_for', queued_for_assignee, "
        "'behind', queued_behind_ticket)::text FROM ticket_board.tickets "
        f"WHERE id = '{t}';"))


def returned_to_busy_author(bench: pb.Bench, t: str, holding: str, theirs: str, result: dict, label: str) -> None:
    board = bench.board
    nobody = json.loads(board.sql("SELECT coalesce(json_agg(who || ': ' || left(text, 80)), '[]')::text FROM "
                                  f"ticket_board.ticket_comments WHERE ticket_id = '{t}' AND coalesce(who, '') = '';"))
    said = pb.comments(board, t)
    check(result["outcome"] == "returned" and result.get("recorded") and "caller role" not in json.dumps(result),
          f"{label}: recorded as returned, never escalated over a refused caller role: {result}")
    where = queue_row(bench, t)
    check(where["state"] == "ready" and where["queued_for"] == "app" and where["behind"] == holding,
          f"{label}: diverted by serial focus to the ready stage, pinned to its busy author behind {holding}: {where}")
    check(any(s.startswith("Publisher returned this ticket (") for s in said)
          and any(f"{t} is queued for app: serial focus is held by {holding}" in s for s in said) and nobody == [],
          f"{label}: the return and the queue notice are both the Publisher's, and nothing is anybody's else: "
          f"{said} {nobody}")
    check(pb.attempts(board, t)[-1]["state"] == "returned" and bench.world.main() == theirs,
          f"{label}: the attempt ended returned, and public main is exactly what somebody else put there")


def first_attempt(bench: pb.Bench) -> None:
    t, _candidate = candidate(bench)
    holding = busy(bench)
    theirs = moved_on(bench)
    returned_to_busy_author(bench, t, holding, theirs, bench.publish(t), "a first attempt")


def retried(bench: pb.Bench) -> None:
    bench.settle()  # the last case's tickets put down: the author starts this one free
    t, _candidate = candidate(bench)
    holding = busy(bench)
    runner = pb.Runner(lambda argv: pb.failed(argv, PERMISSION) if "push" in argv else None)
    first = bench.publish(t, runner)
    check(first["outcome"] == "escalated" and pb.row(bench.board, t)["state"] == "publication_failed",
          f"a refused credential is the Director's: {first}")
    theirs = moved_on(bench)
    check(pb.short(bench.board.act(t, "publication_retry", "director")) == "ok"
          and pb.row(bench.board, t)["state"] == "publication", f"the Director retries {t}")
    returned_to_busy_author(bench, t, holding, theirs, bench.publish(t), "the Director's retry")


def scenario() -> None:
    import tempfile

    with tempfile.TemporaryDirectory(prefix="syrd744-") as tmp, pb.temporary_cluster(prefix="syrd744-pg-") as cluster:
        bench = pb.Bench(cluster, "pull", Path(tmp), copy.deepcopy(pull_board_document()))
        try:
            first_attempt(bench)
            retried(bench)
        finally:
            bench.close()


def test_an_upgraded_board_runs_the_same_trigger() -> None:
    """A fresh board (schema.sql) and an upgraded one (pgu1031, the newest migration defining it) announce alike."""
    from schema_function_drift import assert_no_drift, owning_migration

    try:
        assert_no_drift("announce_serial_focus_queue")
        drift = ""
    except AssertionError as exc:
        drift = str(exc)
    check(drift == "" and owning_migration("announce_serial_focus_queue").name == "pgu1031_syrd744_publisher_busy_author.sql",
          f"schema.sql's announce_serial_focus_queue is pgu1031's, character for character: {drift}")


def main() -> int:
    test_an_upgraded_board_runs_the_same_trigger()
    scenario()
    print(f"publisher_busy_author_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
