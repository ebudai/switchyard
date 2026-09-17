#!/usr/bin/env python3
"""SYRD-37: a bench of interchangeable workers, rehearsed against a real board.

Everything here is a property the database is supposed to hold, so it is asked
of the database. A fake connection would assert the shape of a query; what this
milestone needs proved is what happens when eight workers, declared from one
pool, all reach for the same board at once:

* every worker is its own role with its own pane address, and the board itself
  refuses to let two of them share one -- which is the whole of "no
  cross-delivery";
* a worker holds one ticket at a time, and a second routed to it is queued
  rather than delivered on top of the first;
* the review lane they all feed has a head and an order, and the notifications
  behind that head are DEFERRED, never dropped -- which is the whole of "review
  load cannot go silent or ambiguous";
* clearing the declaration puts the ambiguity back, so the property is shown to
  come from the tenant's own document and not from something incidental;
* a retired worker cannot be registered again, and work addressed to a worker
  with no live runtime waits instead of disappearing.

The real Hermes turns and the live tenant upgrade are deliberately NOT here:
those are the Director's UAT, and a test that claimed them would be claiming a
model's behaviour from a fixture.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from scripts import team_launcher, worker_pool  # noqa: E402
from scripts.ticket_board.notify_listener import TicketBoardNotifyListener  # noqa: E402
from scripts.ticket_board.peer_identity import read_process  # noqa: E402
from scripts.ticket_board.workflow_config import validate  # noqa: E402

from temporary_cluster import temporary_cluster  # noqa: E402
from worker_pool_lifecycle_test import PROJECT, POOL, base_document  # noqa: E402

SCHEMA_PATH = ROOT / "scripts" / "ticket_board" / "schema.sql"
RBAC_PATH = ROOT / "scripts" / "ticket_board" / "rbac.sql"

CHECKS = 0


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def psql(conninfo: str, sql: str) -> str:
    proc = subprocess.run(
        ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-tA", conninfo],
        input=sql, text=True, capture_output=True, check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr or proc.stdout)
    return proc.stdout.strip()


def psql_fails(conninfo: str, sql: str) -> str:
    proc = subprocess.run(
        ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-tA", conninfo],
        input=sql, text=True, capture_output=True, check=False,
    )
    assert proc.returncode != 0, f"expected a refusal, got: {proc.stdout}"
    return proc.stderr


def ticket_source(ticket_id: str, state: str, assignee: str) -> str:
    payload = {
        "id": ticket_id, "title": ticket_id, "body": "", "state": state,
        "assignee": assignee, "comments": [],
        "created": "2026-09-16T00:00:00+00:00", "updated": "2026-09-16T00:00:00+00:00",
    }
    return json.dumps(payload, sort_keys=True).replace("'", "''")


class Bench:
    """One disposable board, and the few things a rehearsal does to it."""

    def __init__(self, admin: str, service: str, listener: str) -> None:
        self.admin = admin
        self.service = service
        self.listener = listener
        self.sent: list[tuple[str, str]] = []
        self.helpers: list[subprocess.Popen] = []

    # -- setup ---------------------------------------------------------
    def apply_document(self, document: dict) -> None:
        # A fresh board carries the built-in stage set. A document that omits a
        # stage has to say so explicitly -- the board refuses to drop one by
        # silence -- so the stages this tenant does not have are named here,
        # which is exactly what an operator migrating a board would write.
        present = set(psql(
            self.admin, "SELECT string_agg(name, ',') FROM ticket_board.workflow_stages;"
        ).split(",")) - {""}
        keeping = {stage["name"] for stage in document["stages"]}
        document = json.loads(json.dumps(document))
        document["remove_stages"] = sorted(present - keeping)
        payload = json.dumps(validate(document, project=PROJECT))
        psql(
            self.service,
            f"SELECT set_config('ticket_board.caller_role', 'director', false);\n"
            f"SELECT set_config('ticket_board.project', '{PROJECT}', false);\n"
            "SELECT ticket_board.apply_declared_workflow($$" + payload + "$$::jsonb);",
        )

    def live_pid(self) -> tuple[int, int]:
        """A real process this test owns, so a runtime assignment reads as live."""
        helper = subprocess.Popen(["sleep", "300"])
        self.helpers.append(helper)
        for _ in range(200):
            process = read_process(helper.pid)
            if process is not None:
                return helper.pid, process.start_time
            time.sleep(0.01)
        raise AssertionError("the helper process never appeared in /proc")

    def register(self, role: str, *, runtime: str, target: str, pid: int, start_time: int) -> str:
        return psql(
            self.admin,
            "SELECT actual_target FROM ticket_board.register_role_runtime("
            f"'{role}', '{runtime}', '{target}', '/srv/worktrees/{role}', "
            f"'/srv/state/{role}', {pid}, {start_time}, {os.getuid()}, 1);",
        )

    def register_refused(self, role: str, *, runtime: str, target: str, pid: int, start_time: int) -> str:
        return psql_fails(
            self.admin,
            "SELECT actual_target FROM ticket_board.register_role_runtime("
            f"'{role}', '{runtime}', '{target}', '/srv/worktrees/{role}', "
            f"'/srv/state/{role}', {pid}, {start_time}, {os.getuid()}, 1);",
        )

    def close(self) -> None:
        for helper in self.helpers:
            helper.terminate()
        for helper in self.helpers:
            helper.wait()

    # -- tickets -------------------------------------------------------
    def add_ticket(
        self, ticket_id: str, *, state: str, assignee: str,
        caller: str = "director", action: str = "route",
    ) -> None:
        psql(self.admin, f"""
SELECT set_config('ticket_board.caller_role', 'director', false);
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, implementation, created_text, updated_text, source_json
) VALUES (
    '{ticket_id}', '{ticket_id}', '', 'analysis', 'director', '',
    '2026-09-16T00:00:00+00:00', '2026-09-16T00:00:00+00:00',
    '{ticket_source(ticket_id, state, assignee)}'::jsonb
);
""")
        self.move(ticket_id, state=state, assignee=assignee, caller=caller, action=action)

    def drop_ticket(self, ticket_id: str) -> None:
        psql(self.admin, f"DELETE FROM ticket_board.tickets WHERE id = '{ticket_id}';")

    def move(
        self,
        ticket_id: str,
        *,
        state: str,
        assignee: str,
        caller: str = "director",
        action: str = "route",
        entered_seconds_ago: int = 0,
    ) -> None:
        """Move a ticket AS somebody, because a declared board asks who.

        The caller is named on every move rather than defaulted to a role that
        can do anything: a rehearsal where the director takes every transition
        would never exercise the worker's own submit, which is the transition
        the pool has to be an actor of.
        """
        psql(self.admin, f"""
SELECT set_config('ticket_board.caller_role', '{caller}', false);
SELECT set_config('ticket_board.workflow_action', '{action}', false);
UPDATE ticket_board.tickets SET state = '{state}', assignee = '{assignee}' WHERE id = '{ticket_id}';
SELECT set_config('ticket_board.workflow_action', '', false);
UPDATE ticket_board.ticket_notification_state
SET entered_current_state_at = clock_timestamp() - interval '{entered_seconds_ago} seconds'
WHERE ticket_id = '{ticket_id}';
""")

    def state_of(self, ticket_id: str) -> tuple[str, str]:
        raw = psql(self.admin, f"SELECT state || ' ' || assignee FROM ticket_board.tickets WHERE id = '{ticket_id}';")
        state, assignee = raw.split()
        return state, assignee

    def queued(self) -> list[dict[str, object]]:
        raw = psql(self.listener, """
SELECT coalesce(jsonb_agg(jsonb_build_object(
    'id', id, 'ticket_id', ticket_id, 'kind', kind, 'target_role', target_role
) ORDER BY id), '[]'::jsonb)::text
FROM ticket_board.ticket_notification_queue;
""")
        return json.loads(raw)

    def clear_queue(self) -> None:
        psql(self.admin, "DELETE FROM ticket_board.ticket_notification_queue;")
        psql(self.admin, "DELETE FROM ticket_board.notification_trace;")

    def trace_events(self) -> list[tuple[str, str, str]]:
        raw = psql(self.listener, """
SELECT coalesce(jsonb_agg(jsonb_build_object(
    'ticket_id', ticket_id, 'event', event, 'busy_reason', coalesce(busy_reason, '')
) ORDER BY id), '[]'::jsonb)::text
FROM ticket_board.notification_trace;
""")
        return [(row["ticket_id"], row["event"], row["busy_reason"]) for row in json.loads(raw)]

    def deliver(self, *, rounds: int) -> int:
        """Work the queue with every pane idle, one delivery per listener life."""
        worked = 0
        for _round in range(rounds):
            listener = TicketBoardNotifyListener(
                conninfo=self.listener,
                project=PROJECT,
                sender=lambda target, message: self.sent.append((target, message)),
                activity_gate=lambda _target: False,
                poll_seconds=0,
                target_exists=lambda _target: True,
            )
            worked += listener.listen_once(max_notifications=1)
        return worked


def main() -> int:
    pool = team_launcher.parse_worker_pool(POOL)
    document, changes = worker_pool.expand_pool(
        base_document(), pool, project=PROJECT, worktree_base=Path("/srv/worktrees")
    )
    members = worker_pool.live_members(document, pool)

    with temporary_cluster(prefix="worker-pool-rehearsal.") as cluster:
        dbname = "syrd_worker_pool_rehearsal"
        admin = f"host={cluster.socket_dir} port={cluster.port} dbname={dbname} user=postgres"
        service = f"host={cluster.socket_dir} port={cluster.port} dbname={dbname} user=ticket_board_service"
        listener = f"host={cluster.socket_dir} port={cluster.port} dbname={dbname} user=ticket_board_listener"
        subprocess.run(
            ["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", dbname],
            check=True, capture_output=True, text=True,
        )
        psql(admin, SCHEMA_PATH.read_text(encoding="utf-8"))
        psql(admin, "\n".join(
            f'CREATE ROLE "{name}" LOGIN;'
            for name in ("director", "user", "main", "review", *members)
        ))
        psql(admin, RBAC_PATH.read_text(encoding="utf-8"))
        bench = Bench(admin, service, listener)
        try:
            run_rehearsal(bench, pool, document, members)
        finally:
            bench.close()

    print(f"worker_pool_rehearsal_postgres_test: {CHECKS} checks ok")
    return 0


def run_rehearsal(bench: Bench, pool, document: dict, members: list[str]) -> None:
    # ---------------------------------------------------------------- 1
    # The pool becomes eight roles the board knows, each with its own address.
    bench.apply_document(document)
    known = psql(bench.admin, "SELECT string_agg(name, ',' ORDER BY name) FROM ticket_board.workflow_roles;")
    for member in members:
        check(member in known.split(","), f"{member} is a role the board knows: {known}")
    owners = psql(
        bench.admin,
        "SELECT array_to_string(owner_roles, ',') FROM ticket_board.workflow_stages WHERE name='in_progress';",
    ).split(",")
    check(all(member in owners for member in members), f"and an owner of the implementation stage: {owners}")
    targets = {
        member: psql(
            bench.admin,
            f"SELECT definition->>'target' FROM ticket_board.workflow_roles WHERE name='{member}';",
        )
        for member in members
    }
    check(len(set(targets.values())) == len(members), f"every worker has its own address: {targets}")
    for member, target in targets.items():
        check(target == f"{PROJECT}-{member}:0.0", f"{member} answers at {target}")

    # ---------------------------------------------------------------- 2
    # And the board is what enforces that, not this code: two workers cannot
    # share a pane address, so a notification cannot land on the wrong one.
    identities = {member: bench.live_pid() for member in members}
    for member in members:
        pid, start = identities[member]
        check(
            bench.register(member, runtime=pool.runtime, target=targets[member], pid=pid, start_time=start)
            == targets[member],
            f"{member} registers its own runtime",
        )
    # The persistent roles keep their own panes: a pool is added beside them,
    # never in place of them, and the rest of the rehearsal needs the reviewer
    # reachable to be about review at all.
    for role in document["roles"]:
        if not role.get("target") or role["name"] in targets:
            continue
        pid, start = bench.live_pid()
        bench.register(
            role["name"], runtime=role["runtime"], target=role["target"], pid=pid, start_time=start
        )
    spare_pid, spare_start = bench.live_pid()
    refusal = bench.register_refused(
        "impl-2", runtime=pool.runtime, target=targets["impl-1"], pid=spare_pid, start_time=spare_start
    )
    check(
        "not active workflow configuration" in refusal or "actual_target" in refusal,
        f"one worker may not take another's address: {refusal}",
    )

    # ---------------------------------------------------------------- 3
    # Eight distinct tickets, routed to eight distinct workers. Every message
    # goes to the worker it names and to nobody else.
    for index, member in enumerate(members, start=1):
        bench.add_ticket(f"STL-{100 + index}", state="in_progress", assignee=member)
    pending = bench.queued()
    check(len(pending) == len(members), f"one notification per worker: {pending}")
    check(
        {row["target_role"] for row in pending} == set(members),
        f"addressed to each of them: {pending}",
    )
    bench.sent.clear()
    delivered = bench.deliver(rounds=len(members))
    check(delivered == len(members), f"all eight are delivered: {delivered}")
    for index, member in enumerate(members, start=1):
        ticket = f"STL-{100 + index}"
        addressed = [target for target, message in bench.sent if ticket in message]
        check(addressed == [targets[member]], f"{ticket} went only to {member}: {addressed}")
        # And an ephemeral worker is handed a cleared session first, at its own
        # address: this is what makes a bench member interchangeable rather
        # than an implementer carrying the last ticket's context (SYRD-135).
        for_member = [message for target, message in bench.sent if target == targets[member]]
        check(for_member[0] == "/clear", f"{member} was cleared before its ticket: {for_member}")
        check(len(for_member) == 2 and ticket in for_member[1], f"{member} got one ticket: {for_member}")
    check(bench.queued() == [], "and the queue is empty afterwards")

    # ---------------------------------------------------------------- 4
    # A ninth ticket routed to a worker that already holds one is QUEUED, not
    # delivered on top of it. This is the pool's concurrency: one ticket per
    # worker, eight across the bench, and the ninth waits for a free one.
    bench.clear_queue()
    bench.add_ticket("STL-150", state="in_progress", assignee="impl-1")
    state, assignee = bench.state_of("STL-150")
    queue = document["queue"]
    check(
        (state, assignee) == (queue["stage"], queue["assignee"]),
        f"the board diverts it to the tenant's own holding destination: {state}/{assignee}",
    )
    marker = psql(
        bench.admin,
        "SELECT queued_for_assignee || ' ' || queued_behind_ticket"
        " FROM ticket_board.tickets WHERE id='STL-150';",
    )
    check(marker == "impl-1 STL-101", f"and records who it is waiting for and behind what: {marker!r}")
    check(
        psql(
            bench.admin,
            "SELECT ticket_board.ticket_serial_focus_reservation_is_current("
            "id, queued_for_assignee, queued_behind_ticket)"
            " FROM ticket_board.tickets WHERE id='STL-150';",
        ) == "t",
        "the reservation reads as current, so reminders will not ask for the one move the board refuses",
    )
    check(
        not [row for row in bench.queued() if row["ticket_id"] == "STL-150" and row["target_role"] == "impl-1"],
        f"and it is not announced to the worker that cannot take it: {bench.queued()}",
    )

    # ---------------------------------------------------------------- 5
    # THE REVIEW LANE. All eight submit. One reviewer owns the stage, and the
    # pool's expansion declared it serial, so the lane has a head: exactly one
    # notification is delivered and the other seven are deferred -- requeued,
    # traced, and still there.
    psql(bench.admin, "DELETE FROM ticket_board.tickets WHERE id = 'STL-150';")
    bench.clear_queue()
    bench.sent.clear()
    for index, member in enumerate(members, start=1):
        # Deliberately against the numbering: the HIGHEST-numbered ticket
        # arrived first. The lane has to take them in arrival order, so a case
        # where arrival and numbering agree would not show which one decides.
        bench.move(f"STL-{100 + index}", state="inspection", assignee="review",
                   caller=member, action="submit", entered_seconds_ago=index * 60)
    pending = bench.queued()
    check(len(pending) == len(members), f"eight review notifications are queued: {pending}")
    check({row["target_role"] for row in pending} == {"review"}, f"all for one reviewer: {pending}")

    delivered = bench.deliver(rounds=len(members) * 2)
    check(delivered == 1, f"exactly one reaches the reviewer: {delivered}, sent {bench.sent}")
    head = f"STL-{100 + len(members)}"
    check(
        any(head in message for _target, message in bench.sent),
        f"and it is the oldest waiting ticket, {head}: {bench.sent}",
    )
    remaining = {row["ticket_id"] for row in bench.queued()}
    check(
        len(remaining) == len(members) - 1 and head not in remaining,
        f"the other seven are still queued, not dropped: {sorted(remaining)}",
    )
    deferred = {ticket for ticket, event, reason in bench.trace_events() if event == "finish_current_defer"}
    check(
        deferred == remaining,
        f"and each says why it waited, naming the ticket ahead of it: {sorted(deferred)}",
    )
    check(
        not [ticket for ticket, event, _reason in bench.trace_events() if event == "drop"],
        "nothing was dropped",
    )

    # ---------------------------------------------------------------- 6
    # THE MUTATION THAT PROVES IT. Clear the declaration the expansion made and
    # the same eight tickets are all delivered at once, with nothing saying
    # which is current. That is the behaviour this ticket exists to remove, and
    # showing it here is what stops the case above passing for another reason.
    loosened = json.loads(json.dumps(document))
    next(role for role in loosened["roles"] if role["name"] == "review")["serial"] = False
    bench.apply_document(loosened)
    for index in range(1, len(members) + 1):
        bench.drop_ticket(f"STL-{100 + index}")
    bench.clear_queue()
    bench.sent.clear()
    for index, member in enumerate(members, start=1):
        bench.add_ticket(f"STL-{200 + index}", state="in_progress", assignee=member)
    bench.clear_queue()
    for index, member in enumerate(members, start=1):
        bench.move(f"STL-{200 + index}", state="inspection", assignee="review",
                   caller=member, action="submit", entered_seconds_ago=index * 60)
    delivered = bench.deliver(rounds=len(members) * 2)
    check(
        delivered == len(members),
        f"without the declaration every one of them is delivered at once: {delivered}",
    )
    check(
        not [t for t, event, _r in bench.trace_events() if event == "finish_current_defer"],
        "and nothing is deferred, because nothing says which is current",
    )

    # ---------------------------------------------------------------- 7
    # Retirement, and what it costs. A retired worker's identity survives; the
    # board refuses to register it again, and work addressed to a worker with no
    # live runtime WAITS rather than vanishing.
    bench.apply_document(document)
    for index in range(1, len(members) + 1):
        bench.drop_ticket(f"STL-{200 + index}")
    retired_document, _changes = worker_pool.retire_worker(document, pool, "impl-3")
    bench.apply_document(retired_document)
    check(
        psql(bench.admin, "SELECT count(*) FROM ticket_board.workflow_roles WHERE name='impl-3';") == "1",
        "the identity is still in the board, so its history still resolves",
    )
    check(
        psql(bench.admin,
             "SELECT (definition->>'active')::boolean FROM ticket_board.workflow_roles WHERE name='impl-3';") == "f",
        "and it is inactive",
    )
    owners = psql(
        bench.admin,
        "SELECT array_to_string(owner_roles, ',') FROM ticket_board.workflow_stages WHERE name='in_progress';",
    ).split(",")
    check("impl-3" not in owners, f"nothing is routed to it again: {owners}")
    pid, start = bench.live_pid()
    refusal = bench.register_refused(
        "impl-3", runtime=pool.runtime, target=f"{PROJECT}-impl-3:0.0", pid=pid, start_time=start
    )
    check("not active workflow configuration" in refusal, f"and it cannot come back under that name: {refusal}")

    # A live worker with no runtime assignment: the notification is requeued
    # with a reason, not acked away. Recovery is restarting the worker.
    bench.clear_queue()
    bench.sent.clear()
    psql(bench.admin, "DELETE FROM ticket_board.role_runtime_assignments WHERE role = 'impl-4';")
    bench.add_ticket("STL-300", state="in_progress", assignee="impl-4")
    delivered = bench.deliver(rounds=2)
    check(delivered == 0, f"nothing is delivered to a worker with no live runtime: {bench.sent}")
    check(
        [row["ticket_id"] for row in bench.queued()] == ["STL-300"],
        f"and the work is still queued for it: {bench.queued()}",
    )


if __name__ == "__main__":
    raise SystemExit(main())
