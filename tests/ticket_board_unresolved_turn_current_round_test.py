#!/usr/bin/env python3
"""SYRD-514: an unresolved-turn prompt from an earlier round escalated after a return.

Live on MEFP-104/106 and SYRD-486. An owner's turn ended unresolved just before
it submitted; the ticket went through review and was returned to the same owner
in the same stage. When the old prompt's grace ran out, the Director was told
the returned owner "ended a turn without resolving" the ticket -- while that
owner had ended no turn since the return and was working.

Two things let it through. The escalation joined every historical repair prompt
to the ticket's CURRENT row, so nothing tied a prompt to the round it was made
in. And the round could not be told from the board: the notification trigger
stamped a transition's stage entry with the row's own updated_at, which the
workflow actions never touch, so a ticket that had just been returned looked as
if it had entered the stage at its last edit, often hours earlier.

The trigger now records the real transition time, and a separate
current_assignment_at, which a same-stage reassignment moves and the stage
entry rightly does not, bounds the escalation: only a prompt made in the
current assignment of the current state escalates. Delivery applies the same
bound to an escalation that was already queued before the return.

Not covered here, and not changed: a repair prompt to the OWNER that was queued
before a submission and is still undelivered after a return to the same owner
carries no prompt time, so delivery keeps it. It can no longer escalate.

Both copies are exercised: schema.sql alone (a fresh board) and schema.sql plus
every migration (production, where the newest migration's function wins).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

import ticket_board_write_api_test as fixture  # noqa: E402
from scripts.ticket_board.notification_eligibility import NotificationEligibility  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    assert condition, message
    CHECKS += 1


def exercise(cluster, dbname: str, *, migrated: bool) -> None:
    label = "migrated" if migrated else "fresh schema"
    admin = fixture.conninfo(cluster.socket_dir, cluster.port, dbname)
    fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", dbname])
    fixture.psql(admin, fixture.SCHEMA_PATH.read_text())
    fixture.create_roles(admin)
    fixture.psql(admin, fixture.RBAC_PATH.read_text())
    if migrated:
        result = subprocess.run(["bash", str(ROOT / "scripts/ticket-board-migrate")], capture_output=True, text=True,
                                env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": admin}, timeout=240)
        check(result.returncode == 0, f"every migration applies: {result.stderr[-1500:]}")
        fixture.psql(admin, fixture.RBAC_PATH.read_text())
    app = fixture.TicketBoardApp(cluster.root / f"frames-{dbname}", cluster.root / f"assets-{dbname}",
                                 project="pgu", ticket_prefix="PGU",
                                 database_url=fixture.conninfo(cluster.socket_dir, cluster.port, dbname, fixture.SERVICE_ROLE))
    server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
    fixture.TEST_WRITE_TOKEN = server.write_token
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    listener_url = fixture.conninfo(cluster.socket_dir, cluster.port, dbname, "ticket_board_listener")

    def act(ticket: str, operation: str, actor: str, **payload):
        return fixture.post_json(base, f"/api/tickets/{ticket}/actions/{operation}", payload, caller=actor)

    def sql(statement: str) -> str:
        return fixture.psql(admin, statement).strip()

    def as_listener(statement: str) -> str:
        raw = fixture.psql(admin, f"SET ROLE ticket_board_listener;\n{statement}\nRESET ROLE;")
        return "\n".join(line for line in raw.splitlines() if line.strip() not in {"SET", "RESET"}).strip()

    def turn_ended(owner: str, turn: str) -> int:
        return int(as_listener(f"SELECT ticket_board.notify_unresolved_turn_end('{json.dumps({owner: turn})}'::jsonb, "
                               "clock_timestamp(), interval '10 minutes');"))

    def escalated(ticket: str, offset: str) -> list[str]:
        """The Director escalations one listener pass `offset` from now adds, by identity."""
        before = int(sql(f"SELECT count(*) FROM ticket_board.notification_trace WHERE ticket_id='{ticket}' "
                         "AND target_role='director' AND kind='unresolved_turn' AND event='enqueue';"))
        as_listener("SELECT ticket_board.notify_unresolved_turn_end('{}'::jsonb, "
                    f"clock_timestamp() + interval '{offset}', interval '10 minutes');")
        keys = sql("SELECT coalesce(string_agg(detail ->> 'dedupe_key', ' ' ORDER BY id), '') FROM ("
                   f"SELECT * FROM ticket_board.notification_trace WHERE ticket_id='{ticket}' AND target_role='director' "
                   f"AND kind='unresolved_turn' AND event='enqueue' ORDER BY id OFFSET {before}) x;")
        return keys.split() if keys else []

    def seconds_since(ticket: str, column: str) -> float:
        return float(sql(f"SELECT extract(epoch FROM clock_timestamp() - {column}) "
                         f"FROM ticket_board.ticket_notification_state WHERE ticket_id='{ticket}';"))

    def raw(ticket: str, column: str) -> str:
        return sql(f"SELECT {column}::text FROM ticket_board.ticket_notification_state WHERE ticket_id='{ticket}';")

    def seed(ticket: str, state: str, assignee: str, *, edited_hours_ago: int = 0) -> None:
        fixture.seed_postgres_ticket(admin, ticket, title=ticket, state=state, assignee=assignee,
                                     needs_inspection=True, commit_exempt=True)
        if edited_hours_ago:
            # The production shape: the last edit was hours ago, and workflow actions do not touch updated_at.
            sql(f"SET ticket_board.caller_role = 'director'; UPDATE ticket_board.tickets SET updated_at = "
                f"clock_timestamp() - interval '{edited_hours_ago} hours' WHERE id='{ticket}';")

    def round_trip(ticket: str, owner: str) -> None:
        act(ticket, "submit_to_inspection", owner)
        act(ticket, "inspector_sign_off", "inspector")
        act(ticket, "audit_sign_off", "audit", text="Audited.")
        act(ticket, "director_kick_back", "director", reason="Fix the UAT finding.")

    checker = NotificationEligibility(logger=logging.getLogger("syrd514"), ledger=None, workflow=lambda: None,
                                      decode_text=lambda v: v.decode("utf-8") if isinstance(v, (bytes, bytearray)) else str(v))

    def queued_grace(ticket: str) -> list[dict]:
        return json.loads(sql("SELECT coalesce(json_agg(json_build_object('id', id, 'kind', kind, 'target', target_role, "
                              "'payload', payload::text) ORDER BY id), '[]')::text FROM ticket_board.ticket_notification_queue "
                              f"WHERE ticket_id='{ticket}' AND kind IN ('unresolved_turn', 'unresolved_turn_repair');"))

    def deliverable(ticket: str, row: dict) -> bool:
        with psycopg.connect(listener_url, autocommit=True, row_factory=dict_row) as conn:
            return checker._notification_is_current(conn, ticket, row["target"], row["payload"])

    try:
        cfg = json.loads((ROOT / "examples/workflows/inspection.json").read_text())
        cfg["project"] = "pgu"
        for role in cfg["roles"]:
            if role.get("target"):
                role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
        app.apply_workflow(cfg, expected_revision=0, dry_run=False, caller_role="director")

        # 1. THE REPORTED SEQUENCE. A turn ends unresolved just before the
        #    owner submits; review; a return to the same owner and stage; the old
        #    prompt's grace runs out while the owner works on the return.
        T = "PGU-104"
        seed(T, "in_progress", "main", edited_hours_ago=3)
        check(turn_ended("main", "turn-1") == 1, f"{label}: the owner is prompted")
        round_trip(T, "main")
        check(app.get_ticket(T)["state"] == "in_progress", f"{label}: the return lands in Implementation")
        check(seconds_since(T, "entered_current_state_at") < 120,
              f"{label}: stage entry is the return, not the last edit 3 h ago: {raw(T, 'entered_current_state_at')}")
        check(escalated(T, "11 minutes") == [],
              f"{label}: a prompt from before the submission does not escalate after the return")

        # 2. A GENUINE NEW UNRESOLVED TURN IN THE RETURNED ROUND still escalates:
        #    not inside its grace, once after it, not again.
        check(turn_ended("main", "turn-2") == 1, f"{label}: the new turn is prompted")
        check(escalated(T, "2 minutes") == [], f"{label}: nothing inside the new turn's grace")
        check(escalated(T, "11 minutes") == ["unresolved-turn:PGU-104:in_progress:main:turn-2"],
              f"{label}: the new turn escalates once after its grace, under its own identity")
        check(escalated(T, "25 minutes") == [], f"{label}: and not on a later pass")

        # 3. SAME-STATE REASSIGNMENT A -> B -> A. The identity names the state
        #    and assignee, so on the return to A an A-round prompt matches the
        #    current key again; only the assignment time tells it apart.
        R = "PGU-108"
        seed(R, "in_progress", "ops")
        turn_ended("ops", "turn-r1")
        entered = raw(R, "entered_current_state_at")
        act(R, "reassign", "director", assignee="app", reason="Cover for a moment.")
        act(R, "reassign", "director", assignee="ops", reason="Back to the original owner.")
        check(raw(R, "entered_current_state_at") == entered, f"{label}: a reassignment is not a stage entry")
        check(seconds_since(R, "current_assignment_at") < 120, f"{label}: but it starts a new assignment")
        check(escalated(R, "11 minutes") == [], f"{label}: the A-round prompt does not escalate after A -> B -> A")
        turn_ended("ops", "turn-r2")
        check(escalated(R, "2 minutes") == [], f"{label}: nothing inside the returned owner's new grace")
        check(escalated(R, "11 minutes") == ["unresolved-turn:PGU-108:in_progress:ops:turn-r2"],
              f"{label}: the returned owner's own unresolved turn escalates after its grace")

        # 3b. A BACKFILLED ROW. The migration starts current_assignment_at at
        #     the old stage entry and rewrites no history, so on a ticket in
        #     flight at the upgrade the time bound can be too old. The prompt's
        #     identity still has to name the current assignee.
        act(R, "defer", "director")
        B = "PGU-113"
        seed(B, "in_progress", "ops")
        check(turn_ended("ops", "turn-b1") == 1, f"{label}: the first assignee is prompted")
        act(B, "reassign", "director", assignee="app", reason="Hand it over.")
        check(sql(f"SELECT state || '/' || assignee FROM ticket_board.tickets WHERE id = '{B}';") == "in_progress/app",
              f"{label}: the ticket is still in progress, now with app")
        sql(f"UPDATE ticket_board.ticket_notification_state SET current_assignment_at = clock_timestamp() - interval '1 day' "
            f"WHERE ticket_id = '{B}';")
        check(escalated(B, "11 minutes") == [], f"{label}: another assignee's prompt does not escalate on a backfilled row")
        act(B, "force_move", "director", state="backlog", assignee="unassigned")

        # 4. A PLAIN STALL is unchanged.
        C = "PGU-106"
        seed(C, "in_progress", "app")
        turn_ended("app", "turn-9")
        check(escalated(C, "2 minutes") == [], f"{label}: a stall does not escalate inside its grace")
        check(escalated(C, "11 minutes") == ["unresolved-turn:PGU-106:in_progress:app:turn-9"],
              f"{label}: and does after it")

        # 5. ACTIVATION RESET and a SUPPRESSED transition take the real time too;
        #    suppression still sends nothing.
        AR = "PGU-109"
        seed(AR, "backlog", "unassigned", edited_hours_ago=3)
        act(AR, "route", "director", state="analysis", assignee="director")
        check(seconds_since(AR, "entered_current_state_at") < 120, f"{label}: activation reset records the move")
        check(seconds_since(AR, "current_assignment_at") < 120, f"{label}: and starts the assignment")
        UP = "PGU-114"
        seed(UP, "analysis", "director", edited_hours_ago=3)
        sql(f"SET ticket_board.caller_role = 'director'; UPDATE ticket_board.tickets SET parked = true WHERE id = '{UP}';")
        sql(f"UPDATE ticket_board.ticket_notification_state SET entered_current_state_at = clock_timestamp() - interval '3 hours', "
            f"current_assignment_at = clock_timestamp() - interval '3 hours' WHERE ticket_id = '{UP}';")
        sql(f"SET ticket_board.caller_role = 'director'; UPDATE ticket_board.tickets SET parked = false WHERE id = '{UP}';")
        check(seconds_since(UP, "entered_current_state_at") < 120, f"{label}: unparking in place is an activation")
        check(seconds_since(UP, "current_assignment_at") < 120, f"{label}: and starts the assignment")
        SN = "PGU-110"
        seed(SN, "analysis", "director", edited_hours_ago=3)
        notices = f"SELECT count(*) FROM ticket_board.notification_trace WHERE ticket_id='{SN}' AND kind='transition' AND event='enqueue';"
        before = sql(notices)
        act(SN, "force_move", "director", state="backlog", assignee="unassigned", suppress_notification=True)
        check(sql(notices) == before, f"{label}: a suppressed move still enqueues no transition notice")
        check(seconds_since(SN, "entered_current_state_at") < 120, f"{label}: but its entry time is the move")

        # 6. NOT A TRANSITION: a comment moves neither time. AN IMPORT keeps its
        #    seeded time for both.
        both = "entered_current_state_at::text || ' ' || current_assignment_at::text"
        before = raw(C, both)
        act(C, "add_comment", "director", text="A note, not a transition.")
        check(raw(C, both) == before, f"{label}: a same-state comment leaves entry and assignment alone")
        INS = "PGU-111"
        seed(INS, "audit", "audit")
        check(sql("SELECT (ns.entered_current_state_at = t.row_updated_at AND ns.current_assignment_at = t.row_updated_at)::text "
                  "FROM ticket_board.tickets t JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id "
                  f"WHERE t.id = '{INS}';") == "true", f"{label}: a seeded ticket keeps its seeded time for both")

        # 7. ALREADY QUEUED. An escalation enqueued in the round before the
        #    submission, still undelivered after the return, is stale on
        #    delivery; one from the current round is not.
        for done in (T, C):
            act(done, "defer", "director")
        Q = "PGU-112"
        seed(Q, "in_progress", "main")
        turn_ended("main", "turn-q1")
        check(escalated(Q, "11 minutes") == ["unresolved-turn:PGU-112:in_progress:main:turn-q1"],
              f"{label}: the first round's escalation is queued")
        round_trip(Q, "main")
        stale = [row for row in queued_grace(Q) if row["kind"] == "unresolved_turn"]
        check(len(stale) == 1 and not deliverable(Q, stale[0]),
              f"{label}: the previous round's queued escalation is not delivered after the return: {stale}")
        # What the listener does with a row it judges stale.
        as_listener(f"SELECT ticket_board.ack_notification({stale[0]['id']});")
        turn_ended("main", "turn-q2")
        check(escalated(Q, "11 minutes") == ["unresolved-turn:PGU-112:in_progress:main:turn-q2"],
              f"{label}: the current round's turn escalates once the stale row is gone")
        fresh = [row for row in queued_grace(Q) if row["kind"] == "unresolved_turn"]
        check(len(fresh) == 1 and deliverable(Q, fresh[0]), f"{label}: and that escalation is delivered: {fresh}")

        # 8. ARRIVAL ORDER in a serialised review stage is the order of the
        #    transitions, not of the last edits.
        X, Y = "PGU-204", "PGU-205"
        seed(X, "in_progress", "ops", edited_hours_ago=3)
        seed(Y, "in_progress", "app", edited_hours_ago=1)
        act(Y, "submit_to_inspection", "app")
        act(X, "submit_to_inspection", "ops")
        blocker = "SELECT ticket_board.finish_current_stage_blocker('{}', 'inspector', 'inspection');"
        waits = {ticket: as_listener(blocker.format(ticket)) for ticket in (X, Y)}
        inspection = sql("SELECT string_agg(id || '/' || assignee, ' ' ORDER BY id) FROM ticket_board.tickets WHERE state = 'inspection';")
        check(waits[X] == Y, f"{label}: the later arrival waits behind the earlier one: {waits}; in inspection: {inspection}")
        check(waits[Y] == "", f"{label}: and the earlier arrival waits behind nothing: {waits}; in inspection: {inspection}")
    finally:
        server.shutdown()
        server.server_close()


def exercise_upgrade(cluster) -> None:
    """The migration on a board that already has tickets: backfill, no rewrite."""
    from schema_function_drift import schema_before

    migration = ROOT / "scripts/ticket_board/migrations/pgu966_syrd514_unresolved_turn_current_round.sql"
    dbname = "current_round_upgrade"
    admin = fixture.conninfo(cluster.socket_dir, cluster.port, dbname)
    fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", dbname])
    before = schema_before(migration)
    check("current_assignment_at" not in before, "the before-schema is really from before this change")
    fixture.psql(admin, before)
    fixture.create_roles(admin)
    fixture.psql(admin, fixture.RBAC_PATH.read_text())
    fixture.seed_postgres_ticket(admin, "PGU-301", title="In flight at the upgrade", state="in_progress", assignee="main",
                                 commit_exempt=True)
    stamp = "SELECT entered_current_state_at::text FROM ticket_board.ticket_notification_state WHERE ticket_id = 'PGU-301';"
    entered = fixture.psql(admin, stamp).strip()
    fixture.psql(admin, migration.read_text())
    check(fixture.psql(admin, stamp).strip() == entered, "the upgrade rewrites no existing stage entry")
    check(fixture.psql(admin, "SELECT (current_assignment_at = entered_current_state_at)::text FROM "
                              "ticket_board.ticket_notification_state WHERE ticket_id = 'PGU-301';").strip() == "true",
          "an existing row's assignment starts at its stage entry")
    check(fixture.psql(admin, "SELECT is_nullable FROM information_schema.columns WHERE table_schema = 'ticket_board' "
                              "AND table_name = 'ticket_notification_state' AND column_name = 'current_assignment_at';").strip() == "NO",
          "and the column is required from then on")
    fixture.psql(admin, migration.read_text())
    check(fixture.psql(admin, stamp).strip() == entered, "a replayed migration changes nothing either")


def main() -> int:
    # Roles are cluster-wide, so each board gets its own cluster.
    for dbname, migrated in (("current_round_fresh", False), ("current_round_migrated", True)):
        with temporary_cluster(prefix="syrd514-round.", shutdown="immediate") as cluster:
            exercise(cluster, dbname, migrated=migrated)
    with temporary_cluster(prefix="syrd514-round.", shutdown="immediate") as cluster:
        exercise_upgrade(cluster)
    print(f"ticket_board_unresolved_turn_current_round_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
