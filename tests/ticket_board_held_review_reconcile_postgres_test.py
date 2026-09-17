#!/usr/bin/env python3
"""SYRD-180: a sign-off given under a hold was spent and never paid.

SYRD-146 is manually controlled and uses the declared workflow. The Inspector
signed off: `inspector_signoff` became true, the ticket stayed in inspection
assigned to Inspector, no Audit handoff followed, and clearing the hold did
nothing with the recorded approval. `recover_stalled_ticket` refused -- by its
own rule, since no no-code transition out of inspection belongs to the Inspector
-- and pointed at route/reassign/defer, none of which can finish a completed
review. The only way out was to ask the Inspector to sign off a second time.

The declared executor already computes the transition an approval earns, and
then discards it to keep the deliberate hold. It now records it instead, so
releasing the hold replays that exact step once, as the role that earned it.

Everything here runs against a real cluster through the real HTTP API with the
live board's own workflow document shape, because the defect lives in what the
database does with an approval -- not in the shape of a query. The legacy half
runs first, on the same database before a workflow is declared, so parity is
shown rather than asserted.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

import ticket_board_write_api_test as fixture  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    assert condition, message
    CHECKS += 1


def main() -> int:
    with temporary_cluster(prefix="syrd180-", shutdown="immediate") as cluster:
        dbname = "held_reconcile_test"
        admin = fixture.conninfo(cluster.socket_dir, cluster.port, dbname)
        fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port),
                     "-U", "postgres", dbname])
        fixture.psql(admin, fixture.SCHEMA_PATH.read_text())
        fixture.create_roles(admin)
        fixture.psql(admin, fixture.RBAC_PATH.read_text())

        app = fixture.TicketBoardApp(
            cluster.root / "frames", cluster.root / "assets",
            project="pgu", ticket_prefix="PGU",
            database_url=fixture.conninfo(cluster.socket_dir, cluster.port, dbname, fixture.SERVICE_ROLE),
        )
        server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = server.write_token
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"

        def act(ticket: str, operation: str, actor: str, expect: int = 200, **payload):
            return fixture.post_json(base, f"/api/tickets/{ticket}/actions/{operation}",
                                     payload, caller=actor, expect=expect)

        def hold(ticket: str, value: bool):
            return act(ticket, "set_manually_controlled", "director", manually_controlled=value)

        def sql(query: str) -> str:
            return fixture.psql(admin, query).strip()

        def pending(ticket: str) -> str:
            return sql(f"SELECT coalesce(ticket_board.pending_held_review('{ticket}'),'')")

        def row(ticket: str) -> dict:
            got = app.get_ticket(ticket)
            return {key: got.get(key) for key in
                    ("state", "assignee", "inspector_signoff", "audit_signoff",
                     "manually_controlled", "awaiting_role")}

        def notified(ticket: str) -> list[str]:
            return json.loads(sql(
                "SELECT coalesce(jsonb_agg(DISTINCT target_role)::text,'[]') "
                f"FROM ticket_board.ticket_notification_queue WHERE ticket_id='{ticket}'"))

        try:
            # ---------------------------------------------------------------
            # LEGACY BOARD FIRST. The hardcoded trigger re-evaluates audit and
            # user review on the next unheld write, but never inspection -- so
            # the one stage with a declared inspector stranded there too.
            # ---------------------------------------------------------------
            fixture.seed_postgres_ticket(admin, "PGU-1", title="Legacy held inspection",
                                         state="inspection", assignee="inspector",
                                         needs_inspection=True, commit_exempt=True)
            hold("PGU-1", True)
            act("PGU-1", "inspector_sign_off", "inspector")
            legacy_held = row("PGU-1")
            check(legacy_held["inspector_signoff"] is True and legacy_held["state"] == "inspection",
                  f"legacy: the hold keeps the approved ticket where it is: {legacy_held}")
            check(pending("PGU-1") == "inspector_sign_off",
                  "legacy: and the board can say what the hold still owes")
            hold("PGU-1", False)
            legacy_paid = row("PGU-1")
            check(legacy_paid["state"] == "audit" and legacy_paid["assignee"] == "audit",
                  f"legacy: releasing the hold pays the deferred transition: {legacy_paid}")
            check(pending("PGU-1") == "", "legacy: and nothing is owed afterwards")

            # ---------------------------------------------------------------
            # DECLARED BOARD. SYRD-146's own shape, through the supported API.
            # ---------------------------------------------------------------
            cfg = json.loads((ROOT / "examples/workflows/inspection.json").read_text())
            cfg["project"] = "pgu"
            for role in cfg["roles"]:
                if role.get("target"):
                    role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
            app.apply_workflow(cfg, expected_revision=0, dry_run=False, caller_role="director")
            check(sql("SELECT ticket_board.declared_workflow() IS NOT NULL") == "t",
                  "the declared workflow is live for everything below")
            destination = {
                (t["from"], t["action"]): t["to"] for t in cfg["transitions"]
                if t.get("primitive") == "approve"
            }
            check(destination[("inspection", "inspector_sign_off")] == "audit",
                  "the document says where an inspection approval goes")

            # 1. REPRODUCTION: held, declared, Inspector signs off.
            fixture.seed_postgres_ticket(admin, "PGU-2", title="SYRD-146 shape",
                                         state="inspection", assignee="inspector",
                                         needs_inspection=True, commit_exempt=True)
            hold("PGU-2", True)
            act("PGU-2", "inspector_sign_off", "inspector")
            held = row("PGU-2")
            check(held["inspector_signoff"] is True, f"the approval is recorded: {held}")
            check((held["state"], held["assignee"]) == ("inspection", "inspector"),
                  f"and the hold keeps stage and owner, as it is meant to: {held}")
            check(held["awaiting_role"] == "audit",
                  f"the existing held handoff still names the next role: {held}")
            check(pending("PGU-2") == "inspector_sign_off",
                  "and the deferred transition is retained rather than consumed")

            # 2. RECOVERY REFUSES WITHOUT CONSUMING, naming the command that works.
            said = act("PGU-2", "recover_stalled_ticket", "director", expect=400,
                       reason="stalled after sign-off")
            check("already recorded inspector_sign_off" in said, f"it says what happened: {said}")
            check("set_manually_controlled('PGU-2', false)" in said,
                  f"and names the supported command exactly: {said}")
            check("route, reassign or defer" not in said,
                  f"instead of advice that cannot finish a completed review: {said}")
            check(row("PGU-2")["inspector_signoff"] is True,
                  "and the refusal did not consume the sign-off")

            # 3. RELEASING THE HOLD PAYS IT, ONCE, with the Audit handoff.
            hold("PGU-2", False)
            paid = row("PGU-2")
            check(paid["state"] == "audit", f"the deferred transition is taken: {paid}")
            check(paid["assignee"] == "audit", f"and handed to the stage owner: {paid}")
            check(paid["inspector_signoff"] is True, f"without undoing the review: {paid}")
            check("audit" in notified("PGU-2"), f"Audit is told: {notified('PGU-2')}")
            check(pending("PGU-2") == "", "and nothing is left owed")
            hold("PGU-2", True)
            hold("PGU-2", False)
            check(row("PGU-2")["state"] == "audit",
                  f"a second release replays nothing: {row('PGU-2')}")

            # 4. THE SAME FOR AUDIT, independently. Where it lands is the
            #    workflow's business, not this test's: the declared destination
            #    is gated (`dat` is skipped when no user sign-off is wanted), so
            #    the claim is that a held approval ends exactly where the same
            #    approval ends without a hold -- proved against a control ticket
            #    rather than against a second copy of the gate rules.
            fixture.seed_postgres_ticket(admin, "PGU-3", title="Held audit",
                                         state="audit", assignee="audit", commit_exempt=True)
            fixture.seed_postgres_ticket(admin, "PGU-9", title="Unheld audit control",
                                         state="audit", assignee="audit", commit_exempt=True)
            hold("PGU-3", True)
            act("PGU-3", "audit_sign_off", "audit", text="Approved while held.")
            held_audit = row("PGU-3")
            check(held_audit["audit_signoff"] is True and held_audit["state"] == "audit",
                  f"audit's approval is recorded and held: {held_audit}")
            check(pending("PGU-3") == "audit_sign_off", "and owed, not consumed")
            check(destination[("audit", "audit_sign_off")] == "dat",
                  "the transition the document declares is the one that was deferred")
            act("PGU-9", "audit_sign_off", "audit", text="Approved with no hold.")
            check(pending("PGU-9") == "", "an unheld approval defers nothing")
            hold("PGU-3", False)
            check((row("PGU-3")["state"], row("PGU-3")["assignee"])
                  == (row("PGU-9")["state"], row("PGU-9")["assignee"]),
                  f"and the hold changes only when, not where: {row('PGU-3')} vs {row('PGU-9')}")

            # 5. A REPEATED APPROVAL UNDER THE HOLD IS STILL ONE DECISION.
            fixture.seed_postgres_ticket(admin, "PGU-4", title="Repeated approval",
                                         state="inspection", assignee="inspector",
                                         needs_inspection=True, commit_exempt=True)
            hold("PGU-4", True)
            act("PGU-4", "inspector_sign_off", "inspector")
            generation = sql("SELECT awaiting_since_at FROM ticket_board.ticket_notification_state "
                             "WHERE ticket_id='PGU-4'")
            act("PGU-4", "inspector_sign_off", "inspector")
            check(sql("SELECT awaiting_since_at FROM ticket_board.ticket_notification_state "
                      "WHERE ticket_id='PGU-4'") == generation,
                  "a repeated held approval does not mint a new wait")
            check(sql("SELECT count(*) FROM ticket_board.ticket_deferred_review "
                      "WHERE ticket_id='PGU-4'") == "1",
                  "nor a second deferred transition")
            hold("PGU-4", False)
            check(row("PGU-4")["state"] == "audit", "and it is still paid exactly once")

            # 6. A BLOCKED TICKET IS NOT PROMOTED BY A RELEASE, and its approval
            #    is not spent: it stays owed until the blocker clears. Promoting
            #    here would be SYRD-148 in reverse.
            fixture.seed_postgres_ticket(admin, "PGU-5", title="Blocked held review",
                                         state="inspection", assignee="inspector",
                                         needs_inspection=True, commit_exempt=True)
            fixture.seed_postgres_ticket(admin, "PGU-6", title="The blocker",
                                         state="in_progress", assignee="app", commit_exempt=True)
            hold("PGU-5", True)
            act("PGU-5", "inspector_sign_off", "inspector")
            act("PGU-5", "set_blockers", "director", blocked_by=["PGU-6"],
                blocked_reason="waiting on PGU-6")
            hold("PGU-5", False)
            blocked = row("PGU-5")
            check(blocked["state"] == "inspection", f"a blocked ticket stays put: {blocked}")
            check(pending("PGU-5") == "inspector_sign_off",
                  "and its approval is still owed rather than spent")
            said = act("PGU-5", "recover_stalled_ticket", "director", expect=400,
                       reason="finish the held review")
            check("unresolved blocker" in said, f"recovery names the real obstacle: {said}")
            check(pending("PGU-5") == "inspector_sign_off",
                  "and still does not consume the approval")

            # 7. WHEN THE BLOCKER CLEARS, THE RECORDED APPROVAL IS RECOVERABLE.
            #    The hold is already gone, so `set_manually_controlled` has
            #    nothing left to release: recovery is the command that finishes
            #    it, which is what acceptance 4 asks for.
            act("PGU-5", "set_blockers", "director", blocked_by=[], blocked_reason="")
            act("PGU-5", "recover_stalled_ticket", "director", reason="paying the held sign-off")
            recovered = row("PGU-5")
            check(recovered["state"] == "audit" and recovered["assignee"] == "audit",
                  f"recovery takes the step the Inspector already took: {recovered}")
            check(pending("PGU-5") == "", "exactly once")

            # 8. A TICKET MOVED BY HAND WHILE HELD DOES NOT REPLAY A STALE STEP.
            fixture.seed_postgres_ticket(admin, "PGU-7", title="Moved under the hold",
                                         state="inspection", assignee="inspector",
                                         needs_inspection=True, commit_exempt=True)
            hold("PGU-7", True)
            act("PGU-7", "inspector_sign_off", "inspector")
            # `ops` rather than `app`: `app` already owns reserved implementation
            # work here, and force_move would queue this into backlog instead --
            # correct, and not the thing this case is about.
            act("PGU-7", "force_move", "director", state="in_progress", assignee="ops")
            check(row("PGU-7")["state"] == "in_progress",
                  f"the Director's override lands: {row('PGU-7')}")
            hold("PGU-7", False)
            after = row("PGU-7")
            check(after["state"] == "in_progress",
                  f"and releasing the hold does not drag it back into audit: {after}")
            check(pending("PGU-7") == "", "the stale deferral is dropped, not replayed")

            # 8b. A KICK-BACK UNDER THE HOLD RETIRES THE DEFERRAL IMMEDIATELY.
            #     The reviewer changed their mind before the hold lifted; there
            #     is nothing left to pay, and it is retired where it is decided
            #     rather than left to be recognised as stale later.
            fixture.seed_postgres_ticket(admin, "PGU-10", title="Approved then returned",
                                         state="inspection", assignee="inspector",
                                         needs_inspection=True, commit_exempt=True)
            hold("PGU-10", True)
            act("PGU-10", "inspector_sign_off", "inspector")
            check(pending("PGU-10") == "inspector_sign_off", "the approval is owed")
            act("PGU-10", "inspector_kick_back", "inspector", text="Second look: not ready.")
            check(pending("PGU-10") == "",
                  "and the kick-back retires it there and then, not lazily")
            hold("PGU-10", False)
            check(row("PGU-10")["state"] == "in_progress",
                  f"so the release takes it nowhere: {row('PGU-10')}")

            # 9. ACCEPTANCE 5: what the API advertises to the Director, the live
            #    board must accept. Both override operations are exposed by the
            #    write CLI; both are exercised here against real RBAC.
            check("force-move" in fixture.run(
                [sys.executable, str(ROOT / "scripts/ticket_board/write_client.py"), "--help"]
            ).stdout, "the CLI advertises force-move")
            fixture.seed_postgres_ticket(admin, "PGU-8", title="Override reachability",
                                         state="inspection", assignee="inspector",
                                         needs_inspection=True, commit_exempt=True)
            act("PGU-8", "override_move", "director", state="backlog", assignee="unassigned")
            check(row("PGU-8")["state"] == "backlog",
                  f"and the declared board accepts it from the control role: {row('PGU-8')}")

            # 10. THE UPGRADE PATH. The live board is migrated, not rebuilt, so
            #     the migration is the thing that has to carry this -- applied to
            #     a database built from the schema currently on main, and applied
            #     twice, because a migration that only works once is a migration
            #     that fails on the second deploy.
            upgrade_db = "held_reconcile_upgrade"
            upgrade_admin = fixture.conninfo(cluster.socket_dir, cluster.port, upgrade_db)
            fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port),
                         "-U", "postgres", upgrade_db])
            before = fixture.run(["git", "show", "origin/main:scripts/ticket_board/schema.sql"],
                                 capture=True).stdout
            fixture.psql(upgrade_admin, before)
            # Anything this branch adds that main's schema does not have yet is
            # applied before the grants, because rbac.sql is the CURRENT grant
            # list and every function it names has to exist by then. That is the
            # order the deploy uses -- schema, migrations, grants -- and without
            # it this case fails on whichever function the branch happens to add
            # rather than on the thing it is testing.
            on_main = set(fixture.run(
                ["git", "ls-tree", "--name-only", "origin/main",
                 "scripts/ticket_board/migrations/"], capture=True
            ).stdout.split())
            for path in sorted((ROOT / "scripts/ticket_board/migrations").glob("*.sql")):
                if f"scripts/ticket_board/migrations/{path.name}" not in on_main:
                    fixture.psql(upgrade_admin, path.read_text(encoding="utf-8"))
            fixture.psql(upgrade_admin, fixture.RBAC_PATH.read_text())
            migration = (ROOT / "scripts/ticket_board/migrations"
                         / "pgu944_syrd180_held_review_reconcile.sql").read_text()
            fixture.psql(upgrade_admin, migration)
            fixture.psql(upgrade_admin, migration)
            upgraded = fixture.TicketBoardApp(
                cluster.root / "frames2", cluster.root / "assets2",
                project="pgu", ticket_prefix="PGU",
                database_url=fixture.conninfo(cluster.socket_dir, cluster.port,
                                              upgrade_db, fixture.SERVICE_ROLE),
            )
            upgraded_server = fixture.TicketBoardServer(
                ("127.0.0.1", 0), upgraded, director_notifier=fixture.QuietNotifier())
            upgraded_thread = threading.Thread(target=upgraded_server.serve_forever, daemon=True)
            upgraded_thread.start()
            try:
                upgraded_base = f"http://127.0.0.1:{upgraded_server.server_port}"
                token_was = fixture.TEST_WRITE_TOKEN
                fixture.TEST_WRITE_TOKEN = upgraded_server.write_token
                upgraded.apply_workflow(cfg, expected_revision=0, dry_run=False,
                                        caller_role="director")
                fixture.seed_postgres_ticket(upgrade_admin, "PGU-20", title="Upgraded board",
                                             state="inspection", assignee="inspector",
                                             needs_inspection=True, commit_exempt=True)
                for value in (True, False):
                    if value:
                        fixture.post_json(upgraded_base,
                                          "/api/tickets/PGU-20/actions/set_manually_controlled",
                                          {"manually_controlled": True}, caller="director")
                        fixture.post_json(upgraded_base,
                                          "/api/tickets/PGU-20/actions/inspector_sign_off",
                                          {}, caller="inspector")
                        check(upgraded.get_ticket("PGU-20")["state"] == "inspection",
                              "upgraded: the hold still defers the approval")
                    else:
                        fixture.post_json(upgraded_base,
                                          "/api/tickets/PGU-20/actions/set_manually_controlled",
                                          {"manually_controlled": False}, caller="director")
                final = upgraded.get_ticket("PGU-20")
                check((final["state"], final["assignee"]) == ("audit", "audit"),
                      f"upgraded: and releasing it pays the deferred transition: {final}")
                check(fixture.psql(upgrade_admin,
                                   "SELECT coalesce(ticket_board.pending_held_review('PGU-20'),'')"
                                   ).strip() == "",
                      "upgraded: exactly once")
                fixture.TEST_WRITE_TOKEN = token_was
            finally:
                upgraded_server.shutdown()
                upgraded_server.server_close()
                upgraded_thread.join()

            print(f"ticket_board_held_review_reconcile_postgres_test: {CHECKS} checks ok")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
