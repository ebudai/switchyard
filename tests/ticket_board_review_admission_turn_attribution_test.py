#!/usr/bin/env python3
"""SYRD-513: a reviewer's turn end was blamed on a ticket it had never been given.

Live on MEFP-51. Serial review admission held MEFP-51's Audit notice ("finish
current") behind MEFP-53, which Audit was reviewing. Audit ended a turn on
MEFP-53, and the unresolved-turn guard prompted Audit about MEFP-51 too and,
after the grace, told the Director that Audit had left MEFP-51 unresolved.
Audit had never been handed it.

The guard attributes a role's turn end to every ticket that role owns unless
ticket_turn_is_resolved excuses it, and nothing asked whether the owner had
been told about the ticket at all. While the ticket's current-stage transition
notice to its owner is still queued -- held by admission, or waiting for a
busy pane -- no turn the owner ends is about it. Once the notice is delivered,
the ticket is judged exactly as before; a dead-lettered notice is the failure
path's to report and does not excuse anything.

Both copies are exercised: schema.sql alone (a fresh board) and schema.sql plus
every migration (production, where the newest migration's function wins). The
real listener delivers, with an injected sender.
"""

from __future__ import annotations

import json
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
from schema_function_drift import assert_no_drift, owning_migration  # noqa: E402
from scripts.ticket_board.notify_listener import TicketBoardNotifyListener  # noqa: E402
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

    def turn_ended(role: str, turn: str) -> int:
        return int(as_listener(f"SELECT ticket_board.notify_unresolved_turn_end('{json.dumps({role: turn})}'::jsonb, "
                               "clock_timestamp(), interval '10 minutes');"))

    def pass_at(offset: str) -> None:
        as_listener("SELECT ticket_board.notify_unresolved_turn_end('{}'::jsonb, "
                    f"clock_timestamp() + interval '{offset}', interval '10 minutes');")

    def keys(ticket: str, kind: str) -> list[str]:
        found = sql("SELECT coalesce(string_agg(detail ->> 'dedupe_key', ' ' ORDER BY id), '') FROM ticket_board.notification_trace "
                    f"WHERE ticket_id = '{ticket}' AND kind = '{kind}' AND event = 'enqueue';")
        return found.split() if found else []

    def pending_transition(ticket: str, role: str) -> str:
        return sql("SELECT coalesce(string_agg(coalesce(last_error, ''), ' '), '') FROM ticket_board.ticket_notification_queue "
                   f"WHERE ticket_id = '{ticket}' AND kind = 'transition' AND target_role = '{role}';")

    def resolved(ticket: str) -> bool:
        return sql(f"SELECT ticket_board.ticket_turn_is_resolved('{ticket}', clock_timestamp())::text;") == "true"

    sent: list[tuple[str, str]] = []
    busy = {"value": False}
    listener = TicketBoardNotifyListener(conninfo=listener_url, sender=lambda target, message: sent.append((target, message)),
                                         activity_gate=lambda _target: busy["value"], target_exists=lambda _target: True,
                                         poll_seconds=0, project="pgu")

    def deliver() -> list[str]:
        before = len(sent)
        listener.listen_once(max_notifications=10)
        return [message for _target, message in sent[before:]]

    def repair_is_deliverable(ticket: str, role: str, turn: str, state: str) -> bool:
        payload = json.dumps({"kind": "unresolved_turn_repair", "id": ticket, "state": state, "assignee": role,
                              "owner_role": role, "target_role": role, "turn_id": turn})
        with psycopg.connect(listener_url, autocommit=True, row_factory=dict_row) as conn:
            listener.refresh_workflow(conn)
            return listener.eligibility._notification_is_current(conn, ticket, role, payload)

    try:
        cfg = json.loads((ROOT / "examples/workflows/inspection.json").read_text())
        cfg["project"] = "pgu"
        for role in cfg["roles"]:
            if role.get("target"):
                role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
            if role["name"] == "audit":
                role["serial"] = True
        app.apply_workflow(cfg, expected_revision=0, dry_run=False, caller_role="director")

        # 1. MEFP-51'S SHAPE. A reaches Audit and is delivered; B arrives
        #    second and serial review admission holds its notice behind A.
        A, B = "PGU-153", "PGU-151"
        for ticket in (A, B):
            fixture.seed_postgres_ticket(admin, ticket, title=ticket, state="inspection", assignee="inspector",
                                         commit_exempt=True)
        deliver()
        act(A, "inspector_sign_off", "inspector")
        check(any(f"{A} entered Audit" in message for message in deliver()), f"{label}: A is delivered to Audit")
        act(B, "inspector_sign_off", "inspector")
        check(deliver() == [], f"{label}: B is not delivered while Audit has A")
        check(pending_transition(B, "audit") == "finish current", f"{label}: B's notice is held by admission")
        check(as_listener(f"SELECT ticket_board.finish_current_stage_blocker('{B}', 'audit', 'audit');") == A,
              f"{label}: behind A")
        check(not resolved(A) and resolved(B), f"{label}: only the delivered ticket can have an unresolved turn")

        # 2. AUDIT ENDS A TURN ON A. A is prompted and, after the grace,
        #    escalated, as before; B is neither.
        check(turn_ended("audit", "turn-1") == 1, f"{label}: one ticket's turn is reported")
        check(keys(A, "unresolved_turn_repair") == [f"repair:unresolved-turn:{A}:audit:audit:turn-1"],
              f"{label}: Audit is prompted about the ticket it has")
        check(keys(B, "unresolved_turn_repair") == [], f"{label}: and not about one it was never given")
        pass_at("11 minutes")
        check(keys(A, "unresolved_turn") == [f"unresolved-turn:{A}:audit:audit:turn-1"],
              f"{label}: A still escalates after the grace")
        check(keys(B, "unresolved_turn") == [], f"{label}: the Director is not told B was abandoned")
        # A prompt already queued for B (e.g. before the upgrade) is not delivered while B is held.
        check(not repair_is_deliverable(B, "audit", "turn-0", "audit"),
              f"{label}: a queued prompt about a held ticket is dropped at delivery")

        # 3. THE QUEUE DRAINS. A leaves Audit; B's held notice is delivered on
        #    its next due attempt, and from then on B is judged as before.
        act(A, "audit_sign_off", "audit", text="Audited.")
        deliver()
        sql(f"UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() "
            f"WHERE ticket_id = '{B}' AND kind = 'transition';")
        check(any(f"{B} entered Audit" in message for message in deliver()), f"{label}: B is admitted once A leaves")
        check(pending_transition(B, "audit") == "", f"{label}: nothing is left queued for B")
        check(not resolved(B), f"{label}: B is now Audit's work")
        check(repair_is_deliverable(B, "audit", "turn-2", "audit"), f"{label}: and prompts about it are delivered")
        check(turn_ended("audit", "turn-2") == 1, f"{label}: Audit's next unresolved turn is reported")
        check(keys(B, "unresolved_turn_repair") == [f"repair:unresolved-turn:{B}:audit:audit:turn-2"],
              f"{label}: as B's")
        pass_at("2 minutes")
        check(keys(B, "unresolved_turn") == [], f"{label}: not escalated inside the grace")
        pass_at("11 minutes")
        check(keys(B, "unresolved_turn") == [f"unresolved-turn:{B}:audit:audit:turn-2"],
              f"{label}: escalated once after it")
        pass_at("25 minutes")
        check(len(keys(B, "unresolved_turn")) == 1, f"{label}: and not again")

        # 4. NOT ONLY ADMISSION. A notice waiting for a busy pane is just as
        #    undelivered; once it is delivered, the owner's turns count.
        C = "PGU-160"
        fixture.seed_postgres_ticket(admin, C, title=C, state="analysis", assignee="director", commit_exempt=True)
        busy["value"] = True
        act(C, "route", "director", state="in_progress", assignee="ops")
        deliver()
        check(pending_transition(C, "ops") == "pane busy", f"{label}: C's notice waits for a busy pane")
        check(turn_ended("ops", "turn-c1") == 0, f"{label}: a turn end before ops was told about C is not C's")
        busy["value"] = False
        sql(f"UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() "
            f"WHERE ticket_id = '{C}' AND kind = 'transition';")
        check(any(C in message for message in deliver()), f"{label}: C is delivered when ops is free")
        check(turn_ended("ops", "turn-c2") == 1, f"{label}: and ops's next unresolved turn is C's")

        # 5. A DEAD-LETTERED NOTICE excuses nothing: the owner is still prompted.
        D = "PGU-161"
        fixture.seed_postgres_ticket(admin, D, title=D, state="analysis", assignee="director", commit_exempt=True)
        act(D, "route", "director", state="in_progress", assignee="app")
        sql(f"UPDATE ticket_board.ticket_notification_queue SET dead_lettered_at = clock_timestamp(), terminal_reason = 'fixture' "
            f"WHERE ticket_id = '{D}' AND kind = 'transition' AND target_role = 'app';")
        check(sql(f"SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{D}' AND kind = 'transition' "
                  "AND target_role = 'app' AND dead_lettered_at IS NOT NULL;") == "1", f"{label}: D's notice is dead-lettered")
        check(turn_ended("app", "turn-d1") == 1, f"{label}: a dead-lettered notice does not hide an unresolved turn")

        # 6. A NOTICE FOR ANOTHER STAGE excuses nothing either.
        E = "PGU-162"
        fixture.seed_postgres_ticket(admin, E, title=E, state="analysis", assignee="director", commit_exempt=True)
        act(E, "route", "director", state="in_progress", assignee="main")
        deliver()
        sql("INSERT INTO ticket_board.ticket_notification_queue (ticket_id, kind, target_role, message, payload, dedupe_key) "
            f"VALUES ('{E}', 'transition', 'main', 'old', jsonb_build_object('kind', 'transition', 'id', '{E}', "
            "'new_state', 'analysis', 'assignee', 'main'), 'syrd513-old-stage');")
        check(turn_ended("main", "turn-e1") == 1, f"{label}: a leftover notice for another stage does not hide the turn")

        # 7. ANOTHER ROLE'S NOTICE excuses nothing: only the owner's own
        #    handover counts. (No writer on a declared board queues another
        #    role a notice for the owner's stage today; the row is synthetic,
        #    like case 6's, so the rule is pinned rather than implied.)
        act(E, "defer", "director")
        owner = "main"
        F = "PGU-163"
        fixture.seed_postgres_ticket(admin, F, title=F, state="analysis", assignee="director", commit_exempt=True)
        act(F, "route", "director", state="in_progress", assignee=owner)
        deliver()
        check(pending_transition(F, owner) == "", f"{label}: the owner's copy is delivered")
        sql("INSERT INTO ticket_board.ticket_notification_queue (ticket_id, kind, target_role, message, payload, dedupe_key) "
            f"VALUES ('{F}', 'transition', 'director', 'copy', jsonb_build_object('kind', 'transition', 'id', '{F}', "
            f"'new_state', 'in_progress', 'assignee', '{owner}'), 'syrd513-other-role');")
        check(turn_ended(owner, "turn-f1") == 1, f"{label}: another role's pending notice does not hide the owner's turn")

        # 8. ANOTHER KIND excuses nothing: only a transition notice is a handover.
        G = "PGU-164"
        fixture.seed_postgres_ticket(admin, G, title=G, state="analysis", assignee="director", commit_exempt=True)
        act(F, "defer", "director")
        act(G, "route", "director", state="in_progress", assignee=owner)
        deliver()
        sql("INSERT INTO ticket_board.ticket_notification_queue (ticket_id, kind, target_role, message, payload, dedupe_key) "
            f"VALUES ('{G}', 'idle_reminder', '{owner}', 'still yours', jsonb_build_object('kind', 'idle_reminder', 'id', '{G}', "
            f"'new_state', 'in_progress', 'assignee', '{owner}'), 'syrd513-other-kind');")
        check(turn_ended(owner, "turn-g1") == 1, f"{label}: a queued reminder is not an undelivered handover")
    finally:
        server.shutdown()
        server.server_close()


def main() -> int:
    # Roles are cluster-wide, so each board gets its own cluster.
    for dbname, migrated in (("admission_fresh", False), ("admission_migrated", True)):
        with temporary_cluster(prefix="syrd513-admission.", shutdown="immediate") as cluster:
            exercise(cluster, dbname, migrated=migrated)
    # Last, so a fault in either copy is first caught by the board that runs it.
    # SYRD-537 redefines it later (one added snooze condition), so the copy an
    # upgraded board runs is the newest one: it must still carry SYRD-513's clause.
    owner = owning_migration("ticket_turn_is_resolved")
    check(owner.name >= "pgu968_syrd513_undelivered_assignment_is_not_a_turn.sql"
          and "AND q.kind = 'transition'" in owner.read_text().split("FUNCTION ticket_board.ticket_turn_is_resolved(")[-1].split("$$;")[0],
          "the SYRD-513 rule is in the copy an upgraded board runs")
    assert_no_drift("ticket_turn_is_resolved")
    print(f"ticket_board_review_admission_turn_attribution_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
