#!/usr/bin/env python3
"""SYRD-795: on a pull board, work for an implementer who claims nothing queues in place, and its freed slot hands it over.

MEFP, 2026-11-15: MEFP-1213, routed to ops while ops held MEFP-1209, was diverted into the ready stage
(pull_ready_source_first) pinned to ops behind MEFP-1209 -- the pull policy's queue (SYRD-539). Only ops could claim
it from there, and ops is not a claimant: MEFP's claim names luna-1..luna-9, and ops is an implementer the Director
dispatches. When MEFP-1209 was blocked and released ops's slot (SYRD-772) nothing consumed MEFP-1213; the Director
withdrew it to Analysis and routed it again by hand.

Now the pull diversion pins work to claimants only. Ops's ticket lands in Implementation, assigned, and waits in its
serial queue as on a Director-dispatched board (SYRD-568); the slot freed by the blocker makes it ops's active ticket,
told once, with no queue marker left behind. A claimant's ticket is still pinned in the ready stage, as before.

A production-built board (schema.sql, roles, rbac.sql, every migration) with MEFP's shape of workflow -- the shipped
example under a pull policy whose claimants are main and app, with ops dispatched by the Director -- the real HTTP
server and the real notify listener with an injected sender (SYRD-772's rig).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import user_review_serial_release_test as urs  # noqa: E402 - its production-built board, listener and helpers

CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")


def mefp_shaped() -> dict:
    """The shipped example under a pull policy: main and app claim from Ready; ops is dispatched, as MEFP's ops is."""
    from pull_scheduling_policy_test import pull_document

    cfg = pull_document()
    cfg["project"] = "pgu"
    for role in cfg["roles"]:
        if role.get("target"):
            role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
        if role["name"] == "ops":
            role.pop("ephemeral", None)  # not a claimant (pull_claimant_pool): the Director routes its work
    claim = next(t for t in cfg["transitions"] if t["action"] == "claim")
    claim["actors"] = [actor for actor in claim["actors"] if actor != "ops"]
    return cfg


def activated(batch: list[tuple[str, str]], role: str, ticket: str) -> list[str]:
    return [m for m in urs.about(batch, role, ticket) if "now your active ticket" in m]


def queue_marks(board, ticket: str) -> list[str]:
    return board.sql(f"SELECT queued_for_assignee || '|' || queued_behind_ticket FROM ticket_board.tickets "
                     f"WHERE id = '{ticket}';").split("|")


def test_dispatched_work_is_handed_over_by_the_freed_slot() -> None:
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import ticket_board_write_api_test as fixture
    from temporary_cluster import temporary_cluster

    with temporary_cluster(prefix="syrd795-", shutdown="immediate") as cluster:
        admin = urs.build_board(fixture, cluster, ROOT, "board")
        board = urs.Board(fixture, cluster, admin, "board", "a")
        try:
            board.app.apply_workflow(mefp_shaped(), expected_revision=0, dry_run=False, caller_role="director")
            A, B, X, C, D, E = "PGU-1209", "PGU-1213", "PGU-1214", "PGU-801", "PGU-802", "PGU-803"
            for ticket in (A, B, X, C, D, E):
                fixture.seed_postgres_ticket(admin, ticket, title=ticket, state="analysis", assignee="director",
                                             commit_exempt=True)
            board.one_pass()

            # ops holds A; the Director routes B to ops too.
            board.act(A, "route", "director", state="in_progress", assignee="ops")
            board.one_pass()
            routed = board.act(B, "route", "director", state="in_progress", assignee="ops")
            check(board.where(B) == ["in_progress", "ops"] and board.reservation("ops") == A
                  and queue_marks(board, B) == ["", ""],
                  f"B waits in ops's own queue, in Implementation and assigned -- not pinned in Ready, where ops "
                  f"cannot claim: {routed} {board.where(B)} {queue_marks(board, B)}")
            check(activated(board.one_pass(), "ops", B) == [], "and is not handed over while A holds the slot")

            # A becomes blocked by X: ops's slot is free, and B is handed over, once.
            board.act(A, "set_blockers", "director", blocked_by=[X], blocked_reason="needs PGU-1214 first")
            first = board.one_pass()
            again = board.one_pass(board.new_listener()) + board.one_pass()
            queue = board.ticket_json(B).get("serial_queue") or {}
            check(board.reservation("ops") == B and len(activated(first, "ops", B)) == 1 and activated(again, "ops", B) == []
                  and queue.get("active") is True and queue_marks(board, B) == ["", ""],
                  f"the freed slot makes B ops's active ticket, told once, with no stale queue marker: "
                  f"{board.reservation('ops')} {activated(first, 'ops', B)} {activated(again, 'ops', B)} {queue}")

            # The Director reassigns app's ticket to ops, which holds B: it stays in Implementation, in ops's queue.
            board.act(E, "route", "director", state="in_progress", assignee="app")
            board.one_pass()
            moved = board.act(E, "reassign", "director", assignee="ops", reason="ops owns this area")
            check(board.where(E) == ["in_progress", "ops"] and queue_marks(board, E) == ["", ""]
                  and board.reservation("ops") == B,
                  f"reassigned to a busy ops, it waits in ops's own queue, not pinned in Ready: {moved} {board.where(E)}")

            # A claimant's work is still pinned in Ready behind what it holds (SYRD-539, unchanged).
            board.act(C, "route", "director", state="in_progress", assignee="main")
            board.one_pass()
            board.act(D, "route", "director", state="in_progress", assignee="main")
            check(board.where(D) == ["ready", "unassigned"] and queue_marks(board, D) == ["main", C],
                  f"a claimant's ticket is pinned in Ready to it, behind its active one, as before: "
                  f"{board.where(D)} {queue_marks(board, D)}")
        finally:
            board.close()


def main() -> int:
    test_dispatched_work_is_handed_over_by_the_freed_slot()
    print(f"pull_dispatched_queue_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
