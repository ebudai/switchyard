#!/usr/bin/env python3
"""SYRD-163: a turn ending is not a stall.

SYRD-162 entered audit after the Inspector signed off. Less than a minute later,
while Audit was reviewing it, the Director was told "audit was reminded about
SYRD-162 (in audit) and still hasn't advanced it -- may be stuck".

Nothing was stuck. A review takes several turns, and between two of them the
pane is idle by every measure the turn-end reminder generator had: the hook says
idle and the live gate agrees, because at that instant nothing is running. So
the first turn boundary minted a reminder and the second escalated it, inside a
minute, against a role that had never stopped working.

The generator beside it, `notify_idle_stall_nudges`, has taken two inputs since
SYRD-58 that this one did not: the last observed WORK per role, and a grace
period. That asymmetry is the defect. These cases run against a real cluster,
because the decision is made in SQL by the selector that mints the reminder, and
a fake connection would assert the shape of a query rather than what the
database does with it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "scripts" / "ticket_board" / "schema.sql"
RBAC_PATH = ROOT / "scripts" / "ticket_board" / "rbac.sql"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from temporary_cluster import temporary_cluster  # noqa: E402

CHECKS = 0
GRACE = "45 seconds"


def check(condition: bool, message: str) -> None:
    global CHECKS
    assert condition, message
    CHECKS += 1


def psql(conninfo: str, sql: str) -> str:
    proc = subprocess.run(
        ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-tA", conninfo],
        input=sql, text=True, capture_output=True, check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr or proc.stdout)
    return proc.stdout.strip()


def ticket_source(ticket_id: str, title: str, state: str, assignee: str) -> str:
    payload = {
        "id": ticket_id, "title": title, "body": "", "state": state,
        "assignee": assignee, "comments": [],
        "created": "2026-09-15T00:00:00+00:00", "updated": "2026-09-15T00:00:00+00:00",
    }
    return json.dumps(payload, sort_keys=True).replace("'", "''")


def create_pane_roles(conninfo: str) -> None:
    psql(conninfo, "\n".join(
        f"CREATE ROLE {name} LOGIN;"
        for name in ('director', '"user"', 'ops', 'app', 'audit', 'inspector', 'perf', 'research', 'main')
    ))


class Board:
    def __init__(self, admin: str, listener: str) -> None:
        self.admin = admin
        self.listener = listener

    def add_ticket(self, ticket_id: str, title: str, state: str, assignee: str,
                   implementer: str = "main") -> None:
        # Which lane the ticket passed through on its way to review. It matters:
        # that implementer still holds the serial-focus reservation afterwards,
        # so two review tickets staged through one lane would queue the second
        # and this suite would be measuring SYRD-109 instead (SYRD-163).
        implementer = assignee if state == "in_progress" else implementer
        if state == "in_progress":
            extra_moves = ""
        elif state == "audit":
            # The sign-off is set BETWEEN the two moves, because entering
            # inspection clears it: that is the gate doing its job, and a
            # fixture that set it once up front would be moving a ticket the
            # board would refuse to move.
            extra_moves = (
                f"UPDATE ticket_board.tickets SET state = 'inspection', assignee = 'inspector' WHERE id = '{ticket_id}';\n"
                f"UPDATE ticket_board.tickets SET inspector_signoff = true WHERE id = '{ticket_id}';\n"
                f"UPDATE ticket_board.tickets SET state = 'audit', assignee = '{assignee}' WHERE id = '{ticket_id}';"
            )
        else:
            extra_moves = (
                f"UPDATE ticket_board.tickets SET state = '{state}', assignee = '{assignee}' WHERE id = '{ticket_id}';"
            )
        psql(self.admin, f"""
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, implementation, created_text, updated_text, source_json
) VALUES (
    '{ticket_id}', '{title}', '', 'backlog', '{assignee}', '',
    '2026-09-15T00:00:00+00:00', '2026-09-15T00:00:00+00:00',
    '{ticket_source(ticket_id, title, state, assignee)}'::jsonb
);
UPDATE ticket_board.tickets SET needs_inspection = true, inspector_signoff = true
WHERE id = '{ticket_id}';
-- Through the states the board allows, because the trigger that refuses a
-- jump is the same one that stamps entered_current_state_at, and a ticket
-- placed by a raw UPDATE would carry a stage it never entered.
UPDATE ticket_board.tickets SET state = 'analysis', assignee = 'director' WHERE id = '{ticket_id}';
UPDATE ticket_board.tickets SET state = 'in_progress', assignee = '{implementer}' WHERE id = '{ticket_id}';
{extra_moves}
DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{ticket_id}';
""")

    def entered_stage(self, ticket_id: str, ago: str) -> None:
        """How long this ticket has been in the stage it is in."""
        psql(self.admin, f"""
UPDATE ticket_board.ticket_notification_state
SET entered_current_state_at = clock_timestamp() - interval '{ago}',
    last_activity_at = clock_timestamp() - interval '{ago}',
    last_nudged_at = NULL
WHERE ticket_id = '{ticket_id}';
""")

    def reminded(self, ticket_id: str, ago: str | None) -> None:
        """A delivered idle reminder, and when it was delivered."""
        stamp = "NULL" if ago is None else f"clock_timestamp() - interval '{ago}'"
        count = 0 if ago is None else 1
        psql(self.admin, f"""
UPDATE ticket_board.ticket_notification_state
SET idle_reminder_count = {count}, last_idle_reminder_at = {stamp}
WHERE ticket_id = '{ticket_id}';
""")

    def clear_queue_rows(self) -> None:
        psql(self.admin, "DELETE FROM ticket_board.ticket_notification_queue;")

    def turn_end(self, *, idle: dict[str, str], working: dict[str, str] | None = None,
                 grace: str = GRACE, pinned: dict[str, str] | None = None) -> int:
        """One turn-end wave: who is idle, since when, and who was seen working.

        `pinned` passes an absolute idle timestamp instead of one relative to
        now, so two waves can present the SAME idle clock -- which is what the
        reminder's dedupe key is built from.
        """
        idle_pairs = ", ".join(
            f"'{role}', '{ago}'::text" if pinned and role in pinned
            else f"'{role}', (clock_timestamp() - interval '{ago}')::text"
            for role, ago in idle.items()
        )
        work_pairs = ", ".join(
            f"'{role}', (clock_timestamp() - interval '{ago}')::text"
            for role, ago in (working or {}).items()
        )
        work = f"jsonb_build_object({work_pairs})" if work_pairs else "'{}'::jsonb"
        raw = psql(self.listener, f"""
SELECT ticket_board.notify_idle_turn_end_nudges(
    jsonb_build_object({idle_pairs}),
    clock_timestamp(),
    interval '{grace}',
    {work}
);
""")
        return int(raw.splitlines()[-1])

    def queued(self, ticket_id: str) -> list[dict[str, object]]:
        raw = psql(self.listener, f"""
SELECT coalesce(jsonb_agg(jsonb_build_object(
    'kind', kind, 'target_role', target_role, 'message', message
) ORDER BY id), '[]'::jsonb)::text
FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{ticket_id}';
""")
        return json.loads(raw)


def main() -> int:
    with temporary_cluster(prefix="ticket-board-active-role-reminder.") as cluster:
        dbname = "syrd_active_role_reminder_test"
        admin = f"host={cluster.socket_dir} port={cluster.port} dbname={dbname} user=postgres"
        listener = f"host={cluster.socket_dir} port={cluster.port} dbname={dbname} user=ticket_board_listener"
        subprocess.run(
            ["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", dbname],
            check=True, capture_output=True, text=True,
        )
        psql(admin, SCHEMA_PATH.read_text(encoding="utf-8"))
        create_pane_roles(admin)
        psql(admin, RBAC_PATH.read_text(encoding="utf-8"))
        board = Board(admin, listener)

        # THE LIVE CASE. SYRD-162's shape: audit has just been handed the ticket
        # and is working it. The pane reports idle between two turns of the one
        # review, and work was observed a moment ago.
        board.add_ticket("SYRD-162", "Audit is reviewing this", "audit", "audit", implementer="app")
        board.entered_stage("SYRD-162", "40 seconds")
        board.clear_queue_rows()
        minted = board.turn_end(idle={"audit": "5 seconds"}, working={"audit": "5 seconds"})
        check(minted == 0, f"a role that was working five seconds ago is not stalled: {minted}")
        check(board.queued("SYRD-162") == [], f"nothing queued: {board.queued('SYRD-162')}")

        # The same instant WITHOUT the work observation still waits, because the
        # ticket has only just arrived and the idle clock has only just started.
        board.clear_queue_rows()
        minted = board.turn_end(idle={"audit": "5 seconds"})
        check(minted == 0, f"five seconds of quiet is not a stall either: {minted}")

        # ACCEPTANCE 3: the wake is not lost. The role goes quiet, the ticket is
        # still actionable, and the next wave delivers exactly one reminder.
        # The stage is aged too: a ticket that arrived forty seconds ago is not
        # stalled however long its owner was quiet BEFORE it arrived, and that
        # is the clock the first two cases were testing.
        board.entered_stage("SYRD-162", "10 minutes")
        board.clear_queue_rows()
        minted = board.turn_end(idle={"audit": "10 minutes"})
        rows = board.queued("SYRD-162")
        check(minted == 1, f"a genuinely quiet audit is reminded: {minted}")
        check([row["kind"] for row in rows] == ["idle_reminder"], f"one reminder: {rows}")
        check(rows[0]["target_role"] == "audit", f"addressed to the owner: {rows}")

        # And not twice for the same stall. The reminder's dedupe key is built
        # from the idle clock, so this presents the SAME clock the first wave
        # saw rather than a fresh one ten minutes back from a later instant.
        same_clock = psql(admin, "SELECT (clock_timestamp() - interval '10 minutes')::text;")
        board.turn_end(idle={"audit": same_clock}, pinned={"audit": same_clock})
        rows = board.queued("SYRD-162")
        check(len(rows) == 1, f"one row for one stall, not one per wave: {rows}")

        # THE LIVE SHAPE, and the one the grace alone does not answer: the hook
        # has said idle for ten minutes and the ticket has sat in the stage for
        # ten minutes, but the role was observed WORKING five seconds ago. That
        # is a pane whose hook is stale against a review that is still running,
        # which is what SYRD-162 was. Without the observed-work term this reads
        # as a ten-minute stall.
        board.clear_queue_rows()
        minted = board.turn_end(idle={"audit": "10 minutes"}, working={"audit": "5 seconds"})
        check(minted == 0, f"observed work outranks a stale idle hook: {minted}")
        check(board.queued("SYRD-162") == [], "and nothing is queued for it")

        # DELIVERY IS WHAT STAMPS THE CLOCK. Not the test: the reminder above is
        # acknowledged through the board's own ack path, and that is what has to
        # record when the owner was told.
        board.clear_queue_rows()
        board.reminded("SYRD-162", None)
        minted = board.turn_end(idle={"audit": "10 minutes"})
        check(minted == 1, f"a reminder is minted for the quiet owner: {minted}")
        reminder_id = psql(listener, """
SELECT id FROM ticket_board.ticket_notification_queue
WHERE ticket_id = 'SYRD-162' AND kind = 'idle_reminder' ORDER BY id DESC LIMIT 1;
""")
        psql(listener, f"SELECT ticket_board.ack_notification({reminder_id}::bigint);")
        stamped = psql(admin, """
SELECT last_idle_reminder_at IS NOT NULL, idle_reminder_count
FROM ticket_board.ticket_notification_state WHERE ticket_id = 'SYRD-162';
""")
        check(stamped.split("|") == ["t", "1"],
              f"delivering the reminder records that it was delivered, and when: {stamped}")

        # ESCALATION. The counter alone used to be enough, so a reminder nobody
        # had delivered -- or one delivered seconds ago -- still escalated.
        board.clear_queue_rows()
        board.reminded("SYRD-162", "5 seconds")
        minted = board.turn_end(idle={"audit": "10 minutes"})
        check(minted == 0,
              f"a reminder five seconds old has not been ignored yet: {board.queued('SYRD-162')}")

        # ACCEPTANCE 4: a genuinely idle stall still reaches the Director.
        board.clear_queue_rows()
        board.reminded("SYRD-162", "10 minutes")
        minted = board.turn_end(idle={"audit": "10 minutes"})
        rows = board.queued("SYRD-162")
        check(minted == 1, f"a real stall still escalates: {minted}")
        check([row["kind"] for row in rows] == ["escalation"], f"escalated: {rows}")
        check(rows[0]["target_role"] == "director", f"to the director: {rows}")
        check("may be stuck" in str(rows[0]["message"]), f"in the words the ticket quotes: {rows}")

        # ...and it is still suppressed while that same role is working, which
        # is what the Director was protected from: the escalation is the
        # expensive one, because it interrupts somebody else.
        board.clear_queue_rows()
        minted = board.turn_end(idle={"audit": "10 minutes"}, working={"audit": "5 seconds"})
        check(minted == 0, f"working now, however long the stall looked: {minted}")

        # THE SAME RULE FOR AN IMPLEMENTATION ROLE, not just review. The
        # acceptance asks for consistency, and the selector is role-generic --
        # this is the case that says so.
        # Ops rather than app: the audit ticket above passed through app's lane
        # on its way to review, so app still holds that serial-focus
        # reservation and a second app ticket would be queued behind it. That
        # is SYRD-109's suppression, and measuring it here would say nothing
        # about this one.
        board.add_ticket("SYRD-900", "Ops is working this", "in_progress", "ops")
        board.entered_stage("SYRD-900", "40 seconds")
        board.clear_queue_rows()
        minted = board.turn_end(idle={"ops": "5 seconds"}, working={"ops": "5 seconds"})
        check(minted == 0, f"an implementer mid-turn is not stalled either: {minted}")
        board.clear_queue_rows()
        board.entered_stage("SYRD-900", "10 minutes")
        minted = board.turn_end(idle={"ops": "10 minutes"})
        check(minted == 1, f"and a quiet one is still reminded: {minted}")

        # THE GRACE IS A PARAMETER, NOT A CONSTANT IN THE SELECTOR. With no
        # grace the old behaviour returns exactly, which is what makes the
        # suppression attributable to the grace rather than to anything else
        # this change touched.
        board.add_ticket("SYRD-901", "Just arrived", "audit", "audit", implementer="perf")
        board.entered_stage("SYRD-901", "5 seconds")
        board.clear_queue_rows()
        check(board.turn_end(idle={"audit": "5 seconds"}, grace="0 seconds") == 1,
              "with a zero grace the wave fires as it always did")
        board.clear_queue_rows()
        check(board.turn_end(idle={"audit": "5 seconds"}) == 0,
              "and with the real grace it does not")

    print(f"ticket_board_active_role_reminder_postgres_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
