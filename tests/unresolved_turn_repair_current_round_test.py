#!/usr/bin/env python3
"""SYRD-527: an owner's repair prompt from an earlier round is not delivered after a return.

SYRD-514 bounded the Director's unresolved-turn escalation to the current
assignment: the escalation carries the time of the prompt it follows, and
delivery drops it when the current assignment began after that. The owner's
own prompt -- "PGU-n is still yours and this turn ended without resolving it"
-- carries no time. Queued before a submission and still undelivered after a
return to the same owner, or before a reassignment away and back, it matches
the ticket's state and assignee again and is delivered once, asking an owner
who is working on the returned round to resolve a turn from the round before.

The prompt's time is not invented: its own queue row is stamped created_at by
the board when the prompt is generated, which happens once per turn identity
(the generator's own trace marker guards it), and a refresh of the row never
moves it. So a row of any age, whatever its payload carries, has a trustworthy
generation time, and delivery drops the prompt when the current assignment
began after it -- the bound SYRD-514 applies to the escalation.

Every observation is made on disposable boards built from schema.sql alone
and from schema.sql plus every migration, through the real generator, the real
workflow actions, and the listener's own eligibility check run as the
least-privileged listener role.
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
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

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

    def handovers_delivered() -> None:
        """Deliver (ack, as the listener does) every queued handover, so each owner has been told."""
        for queued in sql("SELECT coalesce(string_agg(id::text, ' '), '') FROM ticket_board.ticket_notification_queue "
                          "WHERE kind = 'transition' AND dead_lettered_at IS NULL;").split():
            as_listener(f"SELECT ticket_board.ack_notification({queued});")

    def turn_ended(owner: str, turn: str) -> int:
        handovers_delivered()
        return int(as_listener(f"SELECT ticket_board.notify_unresolved_turn_end('{json.dumps({owner: turn})}'::jsonb, "
                               "clock_timestamp(), interval '10 minutes');"))

    def seed(ticket: str, state: str, assignee: str) -> None:
        fixture.seed_postgres_ticket(admin, ticket, title=ticket, state=state, assignee=assignee,
                                     needs_inspection=True, commit_exempt=True)

    def round_trip(ticket: str, owner: str) -> None:
        act(ticket, "submit_to_inspection", owner)
        act(ticket, "inspector_sign_off", "inspector")
        act(ticket, "audit_sign_off", "audit", text="Audited.")
        act(ticket, "director_kick_back", "director", reason="Fix the UAT finding.")

    checker = NotificationEligibility(logger=logging.getLogger("syrd527"), ledger=None, workflow=lambda: None,
                                      decode_text=lambda v: v.decode("utf-8") if isinstance(v, (bytes, bytearray)) else str(v))

    def prompts(ticket: str) -> list[dict]:
        """The owner repair prompts still queued for a ticket, oldest first."""
        return json.loads(sql("SELECT coalesce(json_agg(json_build_object('id', id, 'target', target_role, "
                              "'payload', payload::text) ORDER BY id), '[]')::text FROM ticket_board.ticket_notification_queue "
                              f"WHERE ticket_id='{ticket}' AND kind = 'unresolved_turn_repair' AND dead_lettered_at IS NULL;"))

    def turn_of(row: dict) -> str:
        return json.loads(row["payload"])["turn_id"]

    def deliverable(ticket: str, row: dict) -> bool:
        """The listener's own verdict, as the listener role."""
        handovers_delivered()
        with psycopg.connect(listener_url, autocommit=True, row_factory=dict_row) as conn:
            return checker._notification_is_current(conn, ticket, row["target"], row["payload"])

    def dropped_as_the_listener_does(row: dict) -> None:
        as_listener(f"SELECT ticket_board.ack_notification({row['id']});")

    try:
        cfg = json.loads((ROOT / "examples/workflows/inspection.json").read_text())
        cfg["project"] = "pgu"
        for role in cfg["roles"]:
            if role.get("target"):
                role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
        app.apply_workflow(cfg, expected_revision=0, dry_run=False, caller_role="director")

        # 1. THE REPORTED SEQUENCE. The owner's turn ends unresolved just before
        #    it submits; the prompt is still queued through review and a return
        #    to the same owner in the same stage.
        T = "PGU-301"
        seed(T, "in_progress", "main")
        check(turn_ended("main", "turn-1") == 1, f"{label}: the owner is prompted")
        round_trip(T, "main")
        check(app.get_ticket(T)["state"] == "in_progress" and app.get_ticket(T)["assignee"] == "main",
              f"{label}: the return lands with the same owner in the same stage")
        old = prompts(T)
        check([turn_of(row) for row in old] == ["turn-1"], f"{label}: the earlier round's prompt is still queued: {old}")
        check(not deliverable(T, old[0]),
              f"{label}: the earlier round's prompt is not delivered to the owner working on the return")
        dropped_as_the_listener_does(old[0])
        check(prompts(T) == [] and sql(f"SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id='{T}' "
                                       "AND dead_lettered_at IS NOT NULL;") == "0",
              f"{label}: dropped as stale drains the queue and dead-letters nothing")
        # Dedupe holds: the dropped prompt's turn is not prompted a second time.
        check(turn_ended("main", "turn-1") == 0, f"{label}: the old turn is not prompted again after the drop")

        # 2. THE CURRENT ROUND is untouched: the returned owner's own unresolved
        #    turn is prompted and delivered, and still is after its grace.
        check(turn_ended("main", "turn-2") == 1, f"{label}: the returned round's turn is prompted")
        fresh = prompts(T)
        check([turn_of(row) for row in fresh] == ["turn-2"] and deliverable(T, fresh[0]),
              f"{label}: the returned round's prompt is delivered: {fresh}")
        as_listener("SELECT ticket_board.notify_unresolved_turn_end('{}'::jsonb, "
                    "clock_timestamp() + interval '11 minutes', interval '10 minutes');")
        check(deliverable(T, fresh[0]), f"{label}: and still is once its grace has passed")

        # 3. SAME-STATE REASSIGNMENT A -> B -> A. State and assignee match again
        #    on the return to A; only the assignment time tells the rounds apart.
        R = "PGU-302"
        seed(R, "in_progress", "ops")
        check(turn_ended("ops", "turn-r1") == 1, f"{label}: ops is prompted")
        act(R, "reassign", "director", assignee="app", reason="Cover for a moment.")
        act(R, "reassign", "director", assignee="ops", reason="Back to the original owner.")
        stale = prompts(R)
        check([turn_of(row) for row in stale] == ["turn-r1"] and not deliverable(R, stale[0]),
              f"{label}: the A-round prompt is not delivered after A -> B -> A: {stale}")
        # Both queued at once: each prompt is judged by its own row, so the new
        # round's prompt is not held back by the old one beside it.
        check(turn_ended("ops", "turn-r2") == 1, f"{label}: the returned owner's new turn is prompted")
        both = prompts(R)
        check([turn_of(row) for row in both] == ["turn-r1", "turn-r2"], f"{label}: both prompts are queued: {both}")
        check(not deliverable(R, both[0]) and deliverable(R, both[1]),
              f"{label}: the old one is stale and the new one is delivered, side by side")
        dropped_as_the_listener_does(both[0])
        current = prompts(R)
        check([turn_of(row) for row in current] == ["turn-r2"] and deliverable(R, current[0]),
              f"{label}: and still delivered once the old one is dropped: {current}")

        # 4. A PLAIN STALL, never submitted or reassigned, is delivered as before.
        C = "PGU-303"
        seed(C, "in_progress", "app")
        turn_ended("app", "turn-c1")
        stall = prompts(C)
        check(len(stall) == 1 and deliverable(C, stall[0]), f"{label}: a stalled owner is still prompted: {stall}")

        # Finished scenarios step aside, as in SYRD-514's suite, so each owner
        # below holds one active ticket.
        for done in (T, R, C):
            act(done, "defer", "director")

        # 5. A HISTORICAL QUEUED PAYLOAD. Rows queued by an older release carry an
        #    older payload; the bound reads the row's own created_at, so a row of
        #    that shape is judged the same way -- stale after a return, current
        #    in its own round. Nothing about its time is invented.
        H = "PGU-304"
        seed(H, "in_progress", "main")
        turn_ended("main", "turn-h1")
        older_shape = {"kind": "unresolved_turn_repair", "id": H, "state": "in_progress", "assignee": "main",
                       "owner_role": "main", "target_role": "main", "turn_id": "turn-h1"}
        sql(f"UPDATE ticket_board.ticket_notification_queue SET payload = '{json.dumps(older_shape)}'::jsonb "
            f"WHERE ticket_id = '{H}' AND kind = 'unresolved_turn_repair';")
        historical = prompts(H)
        check(len(historical) == 1 and "grace" not in json.loads(historical[0]["payload"]),
              f"{label}: the queued row now has an older payload: {historical}")
        check(deliverable(H, historical[0]), f"{label}: in its own round an older-shaped prompt is delivered")
        round_trip(H, "main")
        check(not deliverable(H, prompts(H)[0]), f"{label}: after a return an older-shaped prompt is not")

        act(H, "defer", "director")

        # 6. A BOARD WITHOUT SYRD-514's COLUMN keeps delivering as it did: with
        #    no assignment time there is nothing to bound by, and nothing is made up.
        G = "PGU-305"
        seed(G, "in_progress", "app")
        turn_ended("app", "turn-g1")
        act(G, "reassign", "director", assignee="ops", reason="Away.")
        act(G, "reassign", "director", assignee="app", reason="And back.")
        check(not deliverable(G, prompts(G)[0]), f"{label}: stale with the column present")
        sql("ALTER TABLE ticket_board.ticket_notification_state RENAME COLUMN current_assignment_at TO syrd527_hidden;")
        try:
            try:
                verdict = deliverable(G, prompts(G)[0])
            except psycopg.Error as exc:
                verdict = f"the listener could not judge it: {type(exc).__name__}: {str(exc).splitlines()[0]}"
            check(verdict is True, f"{label}: with no assignment time recorded, delivered as before: {verdict}")
        finally:
            sql("ALTER TABLE ticket_board.ticket_notification_state RENAME COLUMN syrd527_hidden TO current_assignment_at;")

        act(G, "defer", "director")

        # 7. The Director's escalation keeps SYRD-514's own bound, unchanged.
        E = "PGU-306"
        seed(E, "in_progress", "main")
        turn_ended("main", "turn-e1")
        as_listener("SELECT ticket_board.notify_unresolved_turn_end('{}'::jsonb, "
                    "clock_timestamp() + interval '11 minutes', interval '10 minutes');")
        escalation = json.loads(sql("SELECT coalesce(json_agg(json_build_object('id', id, 'target', target_role, "
                                    "'payload', payload::text)), '[]')::text FROM ticket_board.ticket_notification_queue "
                                    f"WHERE ticket_id='{E}' AND kind = 'unresolved_turn';"))
        check(len(escalation) == 1 and deliverable(E, escalation[0]), f"{label}: a current escalation is delivered")
        round_trip(E, "main")
        check(not deliverable(E, escalation[0]), f"{label}: a previous round's escalation is not")
    finally:
        server.shutdown()
        server.server_close()


def main() -> int:
    # Roles are cluster-wide, so each board gets its own cluster.
    for dbname, migrated in (("syrd527_fresh", False), ("syrd527_migrated", True)):
        with temporary_cluster(prefix="syrd527.", shutdown="immediate") as cluster:
            exercise(cluster, dbname, migrated=migrated)
    print(f"unresolved_turn_repair_current_round_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
