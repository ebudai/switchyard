#!/usr/bin/env python3
"""SYRD-273: work can wait on a person, once, and is woken when their result is relayed.

MEFP-2 (in_progress/ops) needed Eric -- at work -- to run an already-reviewed
export script that evening. Nothing else could happen until then. The board
had no honest way to say so:

* `await-role MEFP-2 --role user` is refused: `user` has no pane to hand to;
* `request-dependency --role director` hands the Director a "new handoff",
  which the Director can do nothing with, and clears -- and Ops, left looking
  stalled, asks again: the same non-decision delivered over and over;
* there is no ticket to point an external blocker (SYRD-270) at, and
  inventing one would be false provenance.

`operator:<name>` is that wait: a blocker naming a person, set and released
by the Director. Being a blocker, it keeps the stage and owner, silences every
reminder and handoff, and refuses forward promotion; its release records what
the person reported and tells the owner once.

This replays MEFP-2 on real PostgreSQL through the real app and the real
notification listener (`--before` runs only the reproduction, on the board as
it shipped).
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

import psycopg  # noqa: E402

import ticket_board_write_api_test as t  # noqa: E402
from scripts.ticket_board.notify_listener import TicketBoardNotifyListener  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402
from workflow_document_eras import before_relaying  # noqa: E402
from schema_function_drift import schema_before  # noqa: E402

CHECKS = 0
COMMIT = "4f9fd175593d5e804cadad77549b649a34c7d611"
DIRECTOR = "pgu-director:0.0"
OPS = "pgu-ops:0.0"
TICKET = "PGU-2"
REASON = "Eric has to run the reviewed 4.5.1 export script after work; nothing else can happen until then."


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


class Board:
    """A declared board, like MEFP's, with the real listener beside it."""

    def __init__(self, cluster, db: str, schema: str) -> None:
        self.admin = t.conninfo(cluster.socket_dir, cluster.port, db)
        self.listener_url = t.conninfo(cluster.socket_dir, cluster.port, db, "ticket_board_listener")
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", db])
        t.psql(self.admin, schema)
        try:
            t.create_roles(self.admin)
        except AssertionError as exc:
            if "already exists" not in str(exc):
                raise
        t.psql(self.admin, t.RBAC_PATH.read_text())
        self.app = t.TicketBoardApp(
            cluster.root / f"frames-{db}", cluster.root / f"assets-{db}", project="cerulean", ticket_prefix="PGU",
            database_url=t.conninfo(cluster.socket_dir, cluster.port, db, t.SERVICE_ROLE),
        )
        # Commits are checked against a repository; this is about the wait.
        self.app._resolve_known_commit = lambda value: (COMMIT if COMMIT.startswith(value) else value, [])
        document = copy.deepcopy(before_relaying())
        document["project"] = "cerulean"
        document.setdefault("reassign", {})
        document.setdefault("remove_stages", [])
        with self.app._pg_connect() as conn:
            self.app._pg_set_caller_role(conn, "director")
            conn.execute("SELECT set_config('ticket_board.project','cerulean',false)")
            conn.execute("SELECT ticket_board.apply_declared_workflow(%s::jsonb)", (json.dumps(document),))
            conn.commit()
        t.seed_postgres_ticket(self.admin, TICKET, title="Export for 4.5.1", state="in_progress", assignee="ops")
        # MEFP-2 was already Ops's work; its own arrival notice is not the subject.
        t.psql(self.admin, "DELETE FROM ticket_board.ticket_notification_queue;")
        self.delivered: list[tuple[str, str]] = []

    def refused(self, call) -> str:
        try:
            call()
        except Exception as exc:  # noqa: BLE001 - the refusal's text is what is checked
            return str(exc)
        return ""

    def listen(self, *, everything: bool = False) -> int:
        """One pass of the real listener, into panes that take what they are sent.

        `everything` brings every queued row's time forward -- the rest of a
        handoff schedule, as if its hours had passed.
        """
        if everything:
            t.psql(self.admin, "UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() "
                               "WHERE dead_lettered_at IS NULL;")

        def pane(target: str, message: str) -> None:
            self.delivered.append((target, message))

        listener = TicketBoardNotifyListener(
            conninfo=self.listener_url, project="cerulean", sender=pane,
            activity_gate=lambda _target: False, target_exists=lambda _target: True,
            submission_witness=lambda target, _since: any(sent == target for sent, _m in self.delivered),
        )
        with psycopg.connect(self.listener_url, autocommit=True) as conn:
            listener.refresh_workflow(conn)
            return listener.process_due_notifications(conn)

    def to(self, target: str) -> list[str]:
        return [message for sent, message in self.delivered if sent == target and TICKET in message]


def schema_before_this_change() -> str:
    return schema_before(ROOT / "scripts/ticket_board/migrations/pgu962_syrd273_operator_waits.sql")


def run_before(cluster) -> None:
    """MEFP-2's loop, on the board as it shipped."""
    board = Board(cluster, "before", schema_before_this_change())
    user = board.refused(lambda: board.app.set_awaiting_role(TICKET, "user", caller_role="ops"))
    check("invalid awaiting_role: user" in user, f"reproduced: a person cannot be awaited: {user!r}")

    for round_ in (1, 2, 3):
        board.app.request_dependency(TICKET, awaiting_role="director", reason=REASON, caller_role="ops")
        board.listen()
        check(len(board.to(DIRECTOR)) == round_,
              f"reproduced: round {round_} hands the Director the same wait again: {len(board.to(DIRECTOR))}")
        board.app.clear_awaiting_role(TICKET, caller_role="director")
    operator = board.refused(lambda: board.app.update_ticket(
        TICKET, {"blocked_by": ["operator:eric"], "blocked_reason": REASON}, caller_role="director"))
    # Refused by the old board -- by its API or, once the API knows the form,
    # by its database: either way there was nothing to record it with.
    check(("invalid blocked_by ticket id: operator:" in operator or "invalid blocker ticket id: operator:" in operator),
          f"and there was nothing else to record it with: {operator!r}")


def every_reminder(board: Board) -> None:
    for role in ("ops", "director"):
        t.psql(board.listener_url, f"""
SELECT ticket_board.notify_idle_stall_nudges(
    jsonb_build_object('{role}', (clock_timestamp() - interval '3 hours')::text), clock_timestamp());
SELECT ticket_board.notify_idle_turn_end_nudges(
    jsonb_build_object('{role}', (clock_timestamp() - interval '3 hours')::text),
    clock_timestamp(), interval '0 seconds', '{{}}'::jsonb);
""")
    t.psql(board.admin, "SELECT ticket_board.notify_due_nudges(clock_timestamp() + interval '6 hours');")


def run_after(cluster) -> None:
    board = Board(cluster, "after", t.SCHEMA_PATH.read_text())

    # Ops asks once, and the Director is told once -- that part was right.
    board.app.request_dependency(TICKET, awaiting_role="director", reason=REASON, caller_role="ops")
    board.listen()
    check(len(board.to(DIRECTOR)) == 1, f"the Director hears about it once: {board.to(DIRECTOR)}")

    # The Director records what it actually waits on: Eric. Not a ticket.
    code = board.refused(lambda: board.app.update_ticket(
        TICKET, {"blocked_by": ["operator:eric"], "blocked_reason": REASON}, caller_role="ops"))
    check("ops cannot" in code or "cannot call set_blockers" in code or "role ops" in code,
          f"Ops cannot record it itself: {code!r}")
    waiting = board.app.update_ticket(
        TICKET, {"blocked_by": ["Operator:Eric"], "blocked_reason": REASON}, caller_role="director")
    check((waiting["state"], waiting["assignee"]) == ("in_progress", "ops"),
          f"MEFP-2 stays in_progress/ops: {waiting['state']}/{waiting['assignee']}")
    check(waiting["blocked_by"] == ["operator:eric"] and waiting["awaiting_role"] == "",
          f"waiting on operator:eric, and the Director handoff it replaces is closed: "
          f"{waiting['blocked_by']} awaiting={waiting['awaiting_role']!r}")

    # Ops is told once that its wait is recorded, and that is all.
    board.listen()
    recorded_notice = board.to(OPS)
    check(len(recorded_notice) == 1 and "changed by director: blockers" in recorded_notice[0],
          f"Ops is told once that the wait is recorded: {recorded_notice}")

    # From here nothing reaches anybody about it: not the rest of the handoff
    # schedule already queued, not a reminder, not an escalation.
    every_reminder(board)
    before = len(board.delivered)
    board.listen(everything=True)
    check(len(board.delivered) == before,
          f"no handoff, reminder or escalation is delivered while it waits: {board.delivered[before:]}")
    again = board.refused(lambda: board.app.request_dependency(
        TICKET, awaiting_role="director", reason=REASON, caller_role="ops"))
    check("unresolved blocker prevents an awaiting-role handoff: operator:eric" in again,
          f"Ops cannot reopen the Director handoff behind it: {again!r}")
    submit = board.refused(lambda: board.app.perform_workflow_action(
        TICKET, "submit_to_audit", {"commit_hash": COMMIT}, caller_role="ops"))
    check("unresolved blocker prevents forward promotion: operator:eric" in submit,
          f"and the work cannot go forward past it: {submit!r}")
    ticket_named = board.refused(lambda: board.app.update_ticket(
        TICKET, {"blocked_by": ["operator:PGU-3"], "blocked_reason": "x"}, caller_role="director"))
    check("invalid blocked_by ticket id" in ticket_named,
          f"an operator is a person, not a ticket id: {ticket_named!r}")

    # Eric reports; the Director relays it. That is the release.
    result = "Eric ran the 4.5.1 export at 19:40: exit 0, 42 files, sha256 manifest attached to MEFP-2."
    comments_before = len(board.app.get_ticket(TICKET)["comments"])
    unexplained = board.refused(lambda: board.app.release_external_blocker(
        TICKET, ref="operator:eric", reason=" ", caller_role="director"))
    check("releasing an external blocker requires a reason" in unexplained, f"the result is required: {unexplained!r}")
    ops_release = board.refused(lambda: board.app.release_external_blocker(
        TICKET, ref="operator:eric", reason=result, caller_role="ops"))
    check("cannot call set_blockers" in ops_release, f"only the Director relays it: {ops_release!r}")
    released = board.app.release_external_blocker(TICKET, ref="operator:eric", reason=result, caller_role="director")
    check(released["blocked_by"] == [] and (released["state"], released["assignee"]) == ("in_progress", "ops"),
          f"released, and nothing moved: {released['blocked_by']} {released['state']}/{released['assignee']}")
    recorded = released["comments"][comments_before:]
    check([c["text"] for c in recorded] == [f"External blocker operator:eric released: {result}"],
          f"Eric's result is on the ticket: {recorded}")

    # Ops is woken once, through the real listener, with the result to act on.
    board.listen()
    woken = board.to(OPS)[1:]
    check(len(woken) == 1 and "external blocker released" in woken[0],
          f"Ops is told exactly once that the wait is over: {woken}")
    board.listen(everything=True)
    check(len(board.to(OPS)) == 2 and len(board.to(DIRECTOR)) == 1,
          f"and nothing repeats: ops={len(board.to(OPS))} director={len(board.to(DIRECTOR))}")

    # Every gate is still there: Ops submits, and Audit reviews.
    submitted = board.app.perform_workflow_action(TICKET, "submit_to_audit", {"commit_hash": COMMIT}, caller_role="ops")
    check((submitted["state"], submitted["audit_signoff"]) == ("audit", False),
          f"Ops submits into Audit, unsigned: {submitted['state']} {submitted['audit_signoff']}")


MIGRATION = ROOT / "scripts/ticket_board/migrations/pgu962_syrd273_operator_waits.sql"


def run_upgrade(cluster) -> None:
    """A board provisioned before this change takes the migration, twice, and then records a person."""
    db = "upgraded"
    admin = t.conninfo(cluster.socket_dir, cluster.port, db)
    t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", db])
    t.psql(admin, schema_before_this_change())
    for _ in range(2):
        t.psql(admin, MIGRATION.read_text())
    t.psql(admin, t.RBAC_PATH.read_text())
    t.seed_postgres_ticket(admin, TICKET, title="Export for 4.5.1", state="in_progress", assignee="ops")
    service = t.conninfo(cluster.socket_dir, cluster.port, db, t.SERVICE_ROLE)
    t.psql(service, "SELECT set_config('ticket_board.caller_role', 'director', false); "
                    f"SELECT ticket_board.set_blockers('{TICKET}', ARRAY['Operator:Eric'], 'Waiting on Eric.');")
    stored = t.psql(admin, f"SELECT blocker_ticket_id || '|' || resolved::text FROM ticket_board.ticket_blockers "
                           f"WHERE ticket_id = '{TICKET}';").strip()
    check(stored == "operator:eric|false", f"an upgraded board records a person, unresolved: {stored!r}")
    try:
        t.psql(service, "SELECT set_config('ticket_board.caller_role', 'director', false); "
                        f"SELECT ticket_board.set_blockers('{TICKET}', ARRAY['operator:PGU-3'], 'x');")
    except AssertionError as exc:
        shaped = str(exc)
    else:
        shaped = ""
    check("invalid blocker ticket id: operator:pgu-3" in shaped,
          f"and its database refuses a ticket-shaped name: {shaped!r}")


def main() -> int:
    before_only = "--before" in sys.argv
    with temporary_cluster(prefix="syrd273-", shutdown="immediate") as cluster:
        run_before(cluster)
        if not before_only:
            run_after(cluster)
            run_upgrade(cluster)
    print(f"operator_wait_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
