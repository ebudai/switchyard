#!/usr/bin/env python3
"""SYRD-539: atomic claims, the Audit-approval release, and pinned same-author rework, on real boards.

Every board is built the way production builds one -- schema.sql, roles,
rbac.sql, and on the migrated and upgraded shapes the real migration runner --
with the shipped example workflow opted into `scheduling: pull`. Claims run as
the listener and as the board service, through the SQL function both use, and
concurrently over separate connections.
"""

from __future__ import annotations

import json
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

import ticket_board_write_api_test as fixture  # noqa: E402
from pull_scheduling_policy_test import pull_document  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

CHECKS = 0
MIGRATION = ROOT / "scripts/ticket_board/migrations/pgu972_syrd539_pull_scheduling.sql"


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def build(cluster, dbname: str, shape: str):
    admin = fixture.conninfo(cluster.socket_dir, cluster.port, dbname)
    fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", dbname])
    if shape == "upgraded":
        from schema_function_drift import schema_before
        before = schema_before(MIGRATION)
        check("claim_ready_ticket" not in before, "the before-schema predates this change")
        fixture.psql(admin, before)
    else:
        fixture.psql(admin, fixture.SCHEMA_PATH.read_text())
    fixture.create_roles(admin)
    if shape == "upgraded":
        # The board as the previous release left it: its own rbac.sql. This
        # release's rbac.sql comes after the migration, as the runner applies it.
        from schema_function_drift import rbac_before
        before_rbac = rbac_before(MIGRATION)
        check("claim_ready_ticket" not in before_rbac, "the previous release's rbac.sql predates this change")
        fixture.psql(admin, before_rbac)
    else:
        fixture.psql(admin, fixture.RBAC_PATH.read_text())
    if shape == "migrated":
        result = subprocess.run(["bash", str(ROOT / "scripts/ticket-board-migrate")], capture_output=True, text=True,
                                env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": admin}, timeout=300)
        check(result.returncode == 0, f"every migration applies: {result.stderr[-1500:]}")
        fixture.psql(admin, fixture.RBAC_PATH.read_text())
    if shape == "upgraded":
        # This migration and every later one, as the runner applies them before
        # this release's rbac.sql (SYRD-541's pgu974 granted after it); then
        # this one replayed, which must change nothing.
        from schema_function_drift import migrations_from
        for migration in migrations_from(MIGRATION):
            fixture.psql(admin, migration.read_text())
        fixture.psql(admin, MIGRATION.read_text())
        fixture.psql(admin, fixture.RBAC_PATH.read_text())
    return admin


def exercise(cluster, dbname: str, shape: str) -> tuple[str, str]:
    admin = build(cluster, dbname, shape)
    app = fixture.TicketBoardApp(cluster.root / f"frames-{dbname}", cluster.root / f"assets-{dbname}",
                                 project="pgu", ticket_prefix="PGU",
                                 database_url=fixture.conninfo(cluster.socket_dir, cluster.port, dbname, fixture.SERVICE_ROLE))
    server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
    fixture.TEST_WRITE_TOKEN = server.write_token
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    listener_url = fixture.conninfo(cluster.socket_dir, cluster.port, dbname, "ticket_board_listener")
    label = shape

    def act(ticket: str, operation: str, actor: str, **payload):
        return fixture.post_json(base, f"/api/tickets/{ticket}/actions/{operation}", payload, caller=actor)

    def sql(statement: str) -> str:
        return fixture.psql(admin, statement).strip()

    def claim(role: str) -> dict:
        with psycopg.connect(listener_url, autocommit=True) as conn:
            return conn.execute("SELECT ticket_board.claim_ready_ticket(%s)", (role,)).fetchone()[0]

    def where(ticket: str) -> str:
        return sql(f"SELECT state || '/' || assignee || '/' || coalesce(nullif(queued_for_assignee,''),'-') FROM ticket_board.tickets WHERE id='{ticket}';")

    def reserved(role: str) -> str:
        return sql(f"SELECT coalesce(ticket_board.ticket_current_reserved_ticket('{role}'),'-');")

    def admitted(ticket: str) -> None:
        fixture.seed_postgres_ticket(admin, ticket, title=ticket, state="analysis", assignee="director",
                                     needs_inspection=True, commit_exempt=True)
        act(ticket, "admit", "director")

    def submit(ticket: str, author: str) -> None:
        # No repository behind these boards: the work is exempt from a commit,
        # as a Director's commit exemption would make it.
        sql(f"SET ticket_board.caller_role = 'director'; UPDATE ticket_board.tickets SET commit_exempt = true WHERE id='{ticket}';")
        act(ticket, "submit_to_inspection", author)
        if where(ticket).split("/")[0] == "inspection":
            act(ticket, "inspector_sign_off", "inspector")
        check(where(ticket).split("/")[0] == "audit", f"{label}: {ticket} reached Audit: {where(ticket)}")

    def through_audit(ticket: str, author: str) -> None:
        submit(ticket, author)
        act(ticket, "audit_sign_off", "audit", text="Audited.")

    try:
        cfg = pull_document()
        cfg["project"] = "pgu"
        for role in cfg["roles"]:
            if role.get("target"):
                role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
        # 0. A CLAIM THE BOARD COULD NOT TAKE HONESTLY IS NEVER DECLARED (Director review of a72f5489):
        #    the canonical claim refuses without a reason or for a non-owner, so the automatic one would
        #    take what the declared transition forbids. Refused by the Python validator on apply, and by
        #    the stored-document trigger on a direct write.
        for field in ("require_reason", "owner_scoped"):
            bad = json.loads(json.dumps(cfg))
            next(x for x in bad["transitions"] if x["action"] == "claim")[field] = True
            try:
                app.apply_workflow(bad, expected_revision=0, dry_run=True, caller_role="director")
                refusal = ""
            except ValueError as exc:
                refusal = str(exc)
            check(refusal == "the claim transition cannot require a reason or be owner-scoped: a pulled claim has neither", f"{label}: apply refuses a claim with {field}: {refusal!r}")
        # ...but a Director's own move between the same stages is not the claim, and may require a reason:
        # both validators accept it (Director review of 608f366f, validation parity).
        reasoned = json.loads(json.dumps(cfg))
        next(x for x in reasoned["transitions"] if x["action"] == "director_start")["require_reason"] = True
        accepted = app.apply_workflow(reasoned, expected_revision=0, dry_run=True, caller_role="director")
        check(accepted.get("dry_run") is True,
              f"{label}: Python and the stored-document SQL validator both accept a reason-gated Director start")
        app.apply_workflow(cfg, expected_revision=0, dry_run=False, caller_role="director")
        stored = json.loads(sql("SELECT document::text FROM ticket_board.workflow_configuration;"))
        claim_at = next(i for i, x in enumerate(stored["transitions"]) if x["action"] == "claim")
        for field in ("require_reason", "owner_scoped"):
            tampered = subprocess.run(["psql", admin, "-v", "ON_ERROR_STOP=1", "-c",
                                       "UPDATE ticket_board.workflow_configuration SET document = "
                                       f"jsonb_set(document, '{{transitions,{claim_at},{field}}}', 'true');"],
                                      capture_output=True, text=True)
            check(tampered.returncode != 0 and "the claim transition cannot require a reason or be owner-scoped: a pulled claim has neither" in tampered.stderr,
                  f"{label}: a stored document whose claim has {field} is refused: {tampered.stderr[-300:]}")

        # 1. ADMISSION: the Director's transition puts work in the ready stage, unparked.
        for t in ("PGU-1", "PGU-2", "PGU-3"):
            admitted(t)
        check(where("PGU-1") == "ready/unassigned/-" and sql("SELECT parked::text FROM ticket_board.tickets WHERE id='PGU-1';") == "false",
              f"{label}: admitted work waits in ready, not parked: {where('PGU-1')}")

        # 2. CLAIMS: oldest first, one active ticket per worker, idempotent.
        first = claim("main")
        check(first == {"claimed": True, "ticket": "PGU-1", "via": "listener"} and where("PGU-1") == "in_progress/main/-",
              f"{label}: main claims the oldest ready ticket: {first} {where('PGU-1')}")
        again = claim("main")
        check(again == {"claimed": False, "ticket": "PGU-1", "reason": "holding"} and where("PGU-2") == "ready/unassigned/-",
              f"{label}: a worker holding work claims nothing more, and the retry is a no-op: {again}")
        check(claim("app")["ticket"] == "PGU-2", f"{label}: the next worker takes the next ticket")

        # 3. RELEASE ONLY ON THE DECLARED AUDIT APPROVAL.
        submit("PGU-1", "main")
        check(reserved("main") == "PGU-1" and claim("main")["reason"] == "holding",
              f"{label}: submitted and in Audit, the author is still held")
        act("PGU-1", "audit_kick_back", "audit", reason="Fix it.")
        check(where("PGU-1") == "in_progress/main/-" and reserved("main") == "PGU-1",
              f"{label}: an Audit kickback returns to the author, who stays held: {where('PGU-1')}")
        submit("PGU-1", "main")
        act("PGU-1", "audit_sign_off", "audit", text="Audited.")
        check(where("PGU-1").split("/")[0] in {"dat", "user_review", "director_review"} and reserved("main") == "-",
              f"{label}: the Audit approval releases the author while the later gates continue: {where('PGU-1')}")
        check(claim("main") == {"claimed": True, "ticket": "PGU-3", "via": "listener"},
              f"{label}: the released author picks up the next ready ticket")

        # 4. A LATER KICKBACK WHILE THE AUTHOR IS BUSY waits, pinned to that author.
        for stage, step, actor in (("dat", "director_dat_sign_off", "director"), ("user_review", "user_sign_off", "user")):
            if where("PGU-1").split("/")[0] == stage:
                act("PGU-1", step, actor, text="Accepted.")
        check(where("PGU-1").split("/")[0] == "director_review" and reserved("main") == "PGU-3",
              f"{label}: final review holds no author; main holds only its new ticket: {where('PGU-1')}")
        admitted("PGU-11")
        admitted("PGU-12")
        act("PGU-1", "director_kick_back", "director", reason="Final review found a gap.")
        check(where("PGU-1") == "ready/unassigned/main",
              f"{label}: the final kickback waits in ready, pinned to its busy author: {where('PGU-1')}")
        stolen = claim("ops")
        check(stolen["ticket"] == "PGU-11" and where("PGU-1") == "ready/unassigned/main",
              f"{label}: another worker cannot take pinned rework, and takes fresh work instead: {stolen}")
        check(claim("main")["reason"] == "holding", f"{label}: the busy author is not interrupted")
        through_audit("PGU-3", "main")
        with psycopg.connect(listener_url, autocommit=True) as conn:
            woke = conn.execute("SELECT ticket_board.notify_serial_focus_queue_wakeups()").fetchone()[0]
        check(woke == 0, f"{label}: no 'can be routed now' notice to the Director: under pull the claim does it: {woke}")
        resumed = claim("main")
        check(resumed == {"claimed": True, "ticket": "PGU-1", "via": "listener"} and where("PGU-1") == "in_progress/main/-"
              and where("PGU-12") == "ready/unassigned/-",
              f"{label}: once the author's current ticket passes Audit, its pinned rework comes back first, ahead of older open work: {resumed}")
        act("PGU-12", "withdraw", "director")

        # 5. FORCED AND ADMINISTRATIVE MOVES DO NOT RELEASE.
        admitted("PGU-5")
        check(claim("app")["reason"] == "holding", f"{label}: app still holds PGU-2")
        submit("PGU-2", "app")
        sql("SET ticket_board.caller_role = 'director'; SELECT set_config('ticket_board.force_move','on',false); "
            "SELECT set_config('ticket_board.workflow_action','audit_sign_off',false); "
            "UPDATE ticket_board.tickets SET state='dat', assignee='director' WHERE id='PGU-2';")
        check(reserved("app") == "PGU-2",
              f"{label}: a forced move out of Audit releases nothing, even carrying the approval's name: {reserved('app')}")

        # 6. EXCLUSIONS: blocked, manually controlled, awaiting, pinned elsewhere.
        for t in ("PGU-6", "PGU-7", "PGU-8"):
            admitted(t)
        sql("INSERT INTO ticket_board.ticket_blockers(ticket_id, blocker_ticket_id, position) VALUES ('PGU-5','PGU-2',0);")
        sql("SET ticket_board.caller_role = 'director'; UPDATE ticket_board.tickets SET manually_controlled = true WHERE id='PGU-6';")
        sql("UPDATE ticket_board.ticket_notification_state SET awaiting_role='director' WHERE ticket_id='PGU-7';")
        sql("SET ticket_board.caller_role = 'director'; UPDATE ticket_board.tickets SET queued_for_assignee='app' WHERE id='PGU-8';")
        check(sql("SELECT ticket_board.ticket_ready_for_pull('PGU-5','ops')::text;") == "false",
              f"{label}: blocked admitted work is not ready, for the alert and the published queue as well as the claim")
        release_ops = "PGU-11"
        through_audit(release_ops, "ops")
        check(claim("ops") == {"claimed": False, "ticket": None, "reason": "nothing ready"},
              f"{label}: blocked, held, awaiting and someone else's pinned work are not claimable: "
              + " ".join(f"{t}={where(t)}" for t in ("PGU-5", "PGU-6", "PGU-7", "PGU-8")))

        # 7. A WORKER CLAIMS ONLY FOR ITSELF.
        refused = subprocess.run(["psql", admin, "-v", "ON_ERROR_STOP=1", "-c",
                                  f"SET ROLE {fixture.SERVICE_ROLE}; SET ticket_board.caller_role = 'ops'; "
                                  "SELECT ticket_board.claim_ready_ticket('app');"], capture_output=True, text=True)
        check(refused.returncode != 0 and "a worker can claim only for itself" in refused.stderr,
              f"{label}: claiming for another role is refused: {refused.stderr[-300:]}")

        # 8. CONCURRENCY: two workers, one ticket; one worker, two simultaneous claims.
        sql("DELETE FROM ticket_board.ticket_blockers WHERE ticket_id='PGU-5';")
        submit("PGU-1", "main")
        check(reserved("main") == "PGU-1" and sql("SELECT count(*) FROM ticket_board.pull_releases WHERE ticket_id='PGU-1';") == "0",
              f"{label}: resumed rework holds its author again through its new Audit: {reserved('main')}")
        act("PGU-1", "audit_sign_off", "audit", text="Audited.")
        barrier = threading.Barrier(2)
        results: list = []

        def racer(role: str) -> None:
            with psycopg.connect(listener_url, autocommit=True) as conn:
                barrier.wait()
                results.append((role, conn.execute("SELECT ticket_board.claim_ready_ticket(%s)", (role,)).fetchone()[0]))

        threads = [threading.Thread(target=racer, args=(r,)) for r in ("main", "ops")]
        [t.start() for t in threads]
        [t.join(30) for t in threads]
        won = [r for r in results if r[1]["claimed"]]
        check(len(results) == 2 and len(won) == 1 and won[0][1]["ticket"] == "PGU-5",
              f"{label}: two workers racing for one ticket: exactly one claims it: {results}")
        loser = next(role for role, r in results if not r["claimed"])
        admitted("PGU-9")
        admitted("PGU-10")
        results.clear()
        barrier = threading.Barrier(2)
        threads = [threading.Thread(target=racer, args=(loser,)) for _ in range(2)]
        [t.start() for t in threads]
        [t.join(30) for t in threads]
        held = sql(f"SELECT count(*) FROM ticket_board.tickets WHERE state='in_progress' AND assignee='{loser}';")
        check(held == "1" and sum(1 for _, r in results if r["claimed"]) == 1,
              f"{label}: one worker's two simultaneous claims take one ticket between them: {results} held={held}")

        # 9. THROUGH THE SERVICE: a worker claims for itself, and the queue reads truthfully.
        held_by_loser = next(t for t in ("PGU-1", "PGU-5", "PGU-9", "PGU-10", "PGU-11") if where(t) == f"in_progress/{loser}/-")
        through_audit(held_by_loser, loser)
        refused_payload = str(fixture.post_json(base, "/api/tickets/actions/claim_next", {"role": "app"}, caller=loser, expect=400))
        check("claims only for itself" in refused_payload,
              f"{label}: claim_next names no role; a worker claims only for itself: {refused_payload}")
        mine = fixture.post_json(base, "/api/tickets/actions/claim_next", {}, caller=loser)
        check(mine.get("claimed") is True and where(mine["ticket"]) == f"in_progress/{loser}/-"
              and sql(f"SELECT via FROM ticket_board.pull_claims WHERE ticket_id='{mine['ticket']}' ORDER BY id DESC LIMIT 1;") == "self",
              f"{label}: the worker's own claim through the service, recorded as its own: {mine}")
        import urllib.request
        with urllib.request.urlopen(base + "/api/reservations", timeout=10) as response:
            queue = json.loads(response.read().decode())["pull_queue"]
        ready_ids = [entry["id"] for entry in queue["ready_work"]]
        claimed = {entry["id"]: entry["role"] for entry in queue["claimed"]}
        check(queue["enabled"] and claimed.get(mine["ticket"]) == loser
              and set(ready_ids) == set(sql(f"SELECT coalesce(string_agg(id, ' '), '') FROM ticket_board.tickets WHERE state='ready';").split())
              and next(e for e in queue["ready_work"] if e["id"] == "PGU-8")["waiting_for"] == "app"
              and next(e for e in queue["ready_work"] if e["id"] == "PGU-6")["claimable"] is False,
              f"{label}: the published queue matches the board: ready, waiting-for-author and pulled work: {queue}")

        # 10. PREVIEW: turning pull off is shown against in-flight work, and a dry run changes nothing.
        doc = json.loads(sql("SELECT document::text FROM ticket_board.workflow_configuration;"))
        rev = int(sql("SELECT revision FROM ticket_board.workflow_configuration;"))
        released = [t for t in ("PGU-1", "PGU-3", "PGU-5", "PGU-9", "PGU-10", "PGU-11")
                    if sql(f"SELECT count(*) FROM ticket_board.pull_releases WHERE ticket_id='{t}';") == "1"
                    and sql(f"SELECT ticket_board.declared_stage_kind(state) FROM ticket_board.tickets WHERE id='{t}';") == "review"]
        holds = sql("SELECT string_agg(implementer || '=' || coalesce(ticket_id,'-'), ',' ORDER BY implementer) FROM ticket_board.serial_reservations();")
        waiting = sql("SELECT coalesce(string_agg(id, ' ' ORDER BY id), '') FROM ticket_board.tickets WHERE state='ready';").split()
        doc.pop("scheduling")
        preview = app.apply_workflow(doc, expected_revision=rev, dry_run=True, caller_role="director")
        after = sql("SELECT string_agg(implementer || '=' || coalesce(ticket_id,'-'), ',' ORDER BY implementer) FROM ticket_board.serial_reservations();")
        check(after == holds and int(sql("SELECT revision FROM ticket_board.workflow_configuration;")) == rev,
              f"{label}: a dry run changes no hold and no revision: {holds} -> {after}")
        changes = preview["reservation_changes"]
        check(all(change["after"] in released for change in changes["holds"]) and (not released or changes["holds"]),
              f"{label}: the preview names exactly the holds that disabling pull would restore: {changes} released={released}")
        check(changes["ready_work_left_unpulled"] == sorted(waiting) and waiting,
              f"{label}: and the admitted work nobody would pull any more: {changes} waiting={waiting}")

        # 11. THE CANONICAL CLAIM AND THE PULLED ONE AGREE: the declared transition, taken by a free worker
        #     that is NOT the implementation stage's default owner, with no payload, lands on that worker --
        #     as claim_ready_ticket puts it -- and no reason is asked for. (Before this, it landed on the
        #     stage's default owner whoever claimed.)
        held_by_ops = reserved("ops")
        if held_by_ops != "-" and where(held_by_ops).startswith("in_progress/ops"):
            through_audit(held_by_ops, "ops")
        check(reserved("ops") == "-", f"{label}: ops is free before its canonical claim: holds {reserved('ops')}")
        admitted("PGU-20")
        act("PGU-20", "claim", "ops")
        check(where("PGU-20") == "in_progress/ops/-" and reserved("ops") == "PGU-20",
              f"{label}: the canonical claim with no reason lands on its claimant, not the stage default: {where('PGU-20')}")

        # 12. A DIRECTOR'S EXPLICIT ASSIGNMENT IS KEPT: its own move between the same stages is not the
        #     claim, so it is not rewritten to the actor (Director review of 608f366f, which landed it on main).
        through_audit("PGU-20", "ops")
        admitted("PGU-21")
        act("PGU-21", "director_start", "director", assignee="ops")
        check(where("PGU-21") == "in_progress/ops/-",
              f"{label}: the Director's explicit assignment to ops is kept: {where('PGU-21')}")

        return (
            sql("SELECT md5(string_agg(pg_get_functiondef(p.oid), '' ORDER BY p.proname)) FROM pg_proc p JOIN pg_namespace n "
                "ON n.oid=p.pronamespace WHERE n.nspname='ticket_board' AND p.proname IN ('claim_ready_ticket','ticket_ready_for_pull',"
                "'record_pull_release','lock_pull_assignment','unassign_ready_work','validate_declared_scheduling','declared_scheduling',"
                "'declared_parking_stage','ticket_current_reserved_ticket','notify_serial_focus_queue_wakeups','notify_pull_idle_capacity','pull_queue_status','scheduling_claim_transitions');"),
            sql("SELECT string_agg(p.proname || ':' || a.grantee::regrole::text || ':' || a.privilege_type, ',' "
                "ORDER BY p.proname, a.grantee::regrole::text, a.privilege_type) "
                "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace, aclexplode(p.proacl) a WHERE n.nspname='ticket_board' "
                "AND p.proname IN ('claim_ready_ticket','ticket_ready_for_pull','declared_scheduling');"),
        )
    finally:
        server.shutdown()
        server.server_close()


def main() -> int:
    seen = {}
    for shape in ("fresh", "migrated", "upgraded"):
        with temporary_cluster(prefix="syrd539-pull.", shutdown="immediate") as cluster:
            seen[shape] = exercise(cluster, f"pull_{shape}", shape)
    check(len(set(seen.values())) == 1, f"schema.sql, every migration and an upgrade end identical, grants included: {seen}")
    check("-:EXECUTE" not in seen["fresh"][1] and "claim_ready_ticket:ticket_board_listener:EXECUTE" in seen["fresh"][1],
          f"the listener and service may claim; PUBLIC may not: {seen['fresh'][1]}")
    print(f"pull_claim_postgres_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
