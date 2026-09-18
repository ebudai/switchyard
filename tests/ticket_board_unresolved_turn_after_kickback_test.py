#!/usr/bin/env python3
"""SYRD-203: a Director kickback disarmed the recovery it should have started.

Live on SYRD-202. The Director returned it from director_review to the
implementer with a concrete instruction; the implementer picked it up, worked,
and then stopped with the ticket still actionable in Implementation. No
unresolved-turn nudge reached the implementer and no escalation reached the
Director. The User noticed the stall.

The cause is not the staged-pointer `directorctl send` the incident went through.
It is the kickback itself. Returning a ticket enqueues a `transition`
notification to the DIRECTOR as well as to the new owner, and the guard skipped
any ticket for which the Director had ANYTHING queued:

    AND NOT EXISTS (SELECT 1 FROM ticket_notification_queue q
                    WHERE q.ticket_id = t.id AND q.target_role = 'director')

So the Director's own return armed a suppression that lasted until that row was
delivered and acked -- and because the two notifications were enqueued in one
loop, it took the implementer's repair prompt with it. The comment above that
prompt claimed the opposite: "the Director is told whether or not the owner ever
reads this". Independence was claimed in one direction and implemented in
neither.

"You returned this ticket" is not the same news as "the owner then ended a turn
without resolving it". The dedupe now names the kinds that genuinely duplicate
it, and each audience is deduped on its own identity.
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
    with temporary_cluster(prefix="syrd203-turnend.", shutdown="immediate") as cluster:
        dbname = "unresolved_turn_kickback"
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

        def act(ticket: str, operation: str, actor: str, **payload):
            return fixture.post_json(base, f"/api/tickets/{ticket}/actions/{operation}",
                                     payload, caller=actor)

        def as_listener(statement: str) -> str:
            raw = fixture.psql(admin, f"SET ROLE ticket_board_listener;\n{statement}\nRESET ROLE;")
            return "\n".join(line for line in raw.splitlines()
                             if line.strip() not in {"SET", "RESET"}).strip()

        def turn_ended(ticket_owner: str, turn: str = "turn-1", grace: str = "10 minutes") -> int:
            payload = json.dumps({ticket_owner: turn})
            return int(as_listener(
                f"SELECT ticket_board.notify_unresolved_turn_end('{payload}'::jsonb, "
                f"clock_timestamp(), interval '{grace}');"
            ))

        def pass_only(grace: str = "10 minutes") -> int:
            """A listener pass with no turn ending: only the escalation half runs."""
            return int(as_listener(
                "SELECT ticket_board.notify_unresolved_turn_end('{}'::jsonb, "
                f"clock_timestamp(), interval '{grace}');"
            ))

        def queued(ticket: str) -> list[dict]:
            return json.loads(fixture.psql(admin, f"""
SELECT coalesce(jsonb_agg(jsonb_build_object('kind', kind, 'role', target_role) ORDER BY id), '[]')::text
FROM ticket_board.ticket_notification_queue WHERE ticket_id='{ticket}';"""))

        def kinds_for(ticket: str, role: str) -> set[str]:
            return {row["kind"] for row in queued(ticket) if row["role"] == role}

        def owner_role_of(ticket: str) -> str:
            """Who the board says owns it, after any serial-focus redirect.

            Seeding a second in_progress ticket for an implementer that already
            holds one sends it to the queue stage instead, so a case that
            assumes its own assignee is testing a ticket the director owns.
            """
            return fixture.psql(admin, "SELECT ticket_board.transition_target_role(state, assignee) "
                                       f"FROM ticket_board.tickets WHERE id='{ticket}';").strip()

        def deliver(ticket: str, role: str) -> None:
            """What the role receiving its transition notification looks like."""
            fixture.psql(admin, "DELETE FROM ticket_board.ticket_notification_queue "
                                f"WHERE ticket_id='{ticket}' AND target_role='{role}';")

        try:
            cfg = json.loads((ROOT / "examples/workflows/inspection.json").read_text())
            cfg["project"] = "pgu"
            for role in cfg["roles"]:
                if role.get("target"):
                    role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
            app.apply_workflow(cfg, expected_revision=0, dry_run=False, caller_role="director")
            returns = [tr for tr in cfg["transitions"]
                       if tr["from"] == "director_review" and tr.get("primitive") == "return"]
            check(len(returns) == 1, f"the document has one director return: {returns}")
            kick_back = returns[0]["action"]

            # 1. SYRD-202'S SHAPE. Audited, in director_review, returned to the
            #    implementer with an instruction -- a comment on the transition,
            #    which the incident also had.
            fixture.seed_postgres_ticket(admin, "PGU-202", title="Deploy the audited commit",
                                         state="director_review", assignee="director",
                                         audit_signoff=True, commit_exempt=True)
            act("PGU-202", kick_back, "director",
                text="Deploy a1456ee and prepare final live UAT.")
            ticket = app.get_ticket("PGU-202")
            owner = ticket["assignee"]
            check(ticket["state"] == "in_progress", f"the kickback lands in Implementation: {ticket}")
            check(ticket["audit_signoff"] is False or ticket["audit_signoff"] is True,
                  "whatever the audit flag does, it is not what decides recovery")
            check(not ticket.get("awaiting_role"), f"with no awaiting role: {ticket.get('awaiting_role')!r}")
            check("director" in {row["role"] for row in queued("PGU-202")},
                  f"and the Director is notified of their own return: {queued('PGU-202')}")

            # The owner reads its transition notification and starts work.
            deliver("PGU-202", owner)
            check(kinds_for("PGU-202", "director") == {"transition"},
                  f"the Director's copy is still queued, as it was live: {queued('PGU-202')}")

            # 2. THE TURN ENDS WITH THE TICKET STILL ACTIONABLE. Staged
            #    recovery: the person who can fix it is prompted, and nobody
            #    else. Telling the Director in the same breath makes every
            #    ordinary forgotten turn an escalation.
            enqueued = turn_ended(owner)
            check(enqueued == 1, f"the unresolved turn is reported: {enqueued}")
            check("unresolved_turn_repair" in kinds_for("PGU-202", owner),
                  f"the implementer is prompted to repair it: {queued('PGU-202')}")
            check("unresolved_turn" not in kinds_for("PGU-202", "director"),
                  f"and the Director is NOT told in the same breath: {queued('PGU-202')}")

            # 2b. THE DIRECTOR IS TOLD WHEN THE PROMPT DOES NOT WORK. Not on a
            #     second turn end -- a silent owner produces no events at all --
            #     so the escalation runs on an ordinary pass, on elapsed time.
            check(pass_only() == 0, "inside the grace period, nothing escalates")
            check("unresolved_turn" not in kinds_for("PGU-202", "director"),
                  f"the Director is still not told: {queued('PGU-202')}")
            check(pass_only(grace="0 seconds") == 1,
                  "once the grace has passed and it is still unresolved, it escalates")
            check("unresolved_turn" in kinds_for("PGU-202", "director"),
                  f"and now the Director is told: {queued('PGU-202')}")
            check(pass_only(grace="0 seconds") == 0, "once, not on every pass afterwards")
            # And still once after it is DELIVERED. The queued row is what the
            # second guard reads; when the Director acknowledges it the row is
            # gone, and only the trace marker stands between an unresolved
            # ticket and a fresh escalation on every pass forever.
            delivered_id = fixture.psql(admin,
                "SELECT id FROM ticket_board.ticket_notification_queue "
                "WHERE ticket_id='PGU-202' AND kind='unresolved_turn';").strip()
            as_listener(f"SELECT ticket_board.ack_notification({delivered_id});")
            check("unresolved_turn" not in kinds_for("PGU-202", "director"),
                  f"the escalation has been delivered and removed: {queued('PGU-202')}")
            check(pass_only(grace="0 seconds") == 0,
                  "and a delivered escalation is not sent again on the next pass")
            check("unresolved_turn" not in kinds_for("PGU-202", "director"),
                  f"the Director is not re-escalated to: {queued('PGU-202')}")

            # 2c. A RESOLVED TICKET STOPS THE STAGED ESCALATION. The prompt
            #     worked, so the Director never hears about it at all.
            # Its own implementer, and asserted rather than assumed: a case
            # guarded by `if the fixture landed where I wanted` is a case that
            # can silently not run, which is how a mutant lives.
            repaired_by = [role["name"] for role in cfg["roles"]
                           if role.get("kind") == "implementer" and role["name"] != owner][0]
            fixture.seed_postgres_ticket(admin, "PGU-210", title="Prompted and repaired",
                                         state="in_progress", assignee=repaired_by,
                                         commit_exempt=True)
            deliver("PGU-210", repaired_by)
            check(owner_role_of("PGU-210") == repaired_by,
                  f"the fixture is owned by the implementer it names: {owner_role_of('PGU-210')!r}")
            turn_ended(repaired_by, "turn-repaired")
            check("unresolved_turn_repair" in kinds_for("PGU-210", repaired_by),
                  f"the owner is prompted: {queued('PGU-210')}")
            act("PGU-210", "set_manually_controlled", "director", manually_controlled=True)
            check(pass_only(grace="0 seconds") == 0,
                  "a turn that stopped being actionable escalates to nobody")
            check("unresolved_turn" not in kinds_for("PGU-210", "director"),
                  f"and the Director hears nothing about it: {queued('PGU-210')}")
            act("PGU-210", "set_manually_controlled", "director", manually_controlled=False)
            # Freed, so the next case can have this implementer: serial focus
            # gives one implementer one in_progress ticket, and a fixture that
            # quietly lands in the queue stage is a case about the Director's
            # ticket rather than the one it names.
            fixture.psql(admin, "DELETE FROM ticket_board.tickets WHERE id='PGU-210';")

            # 3. THE ORIGINAL TRANSITION IS NOT DUPLICATED.
            rows = queued("PGU-202")
            check(sum(1 for row in rows if row["kind"] == "transition") == 1,
                  f"the Director's transition row is not minted again: {rows}")
            check(sum(1 for row in rows if row["kind"] == "unresolved_turn_repair") == 1,
                  f"and the owner holds exactly one repair prompt: {rows}")

            # 4. ONCE PER TURN IDENTITY, PER AUDIENCE. Running the same turn end
            #    again adds nothing, and acknowledging one does not re-arm it.
            before = queued("PGU-202")
            check(turn_ended(owner) == 0, "the same turn end is not reported twice")
            check(queued("PGU-202") == before, f"and nothing is added: {queued('PGU-202')}")

            # 5. AN ACTIVE OWNER IS NOT NUDGED. A turn that has not ended is not
            #    in the map at all, which is what this path reads.
            implementers = [role["name"] for role in cfg["roles"]
                            if role.get("kind") == "implementer" and role["name"] != owner]
            check(len(implementers) >= 2,
                  f"the document has other implementers to give these cases: {implementers}")
            working, escalated = implementers[0], implementers[1]
            fixture.seed_postgres_ticket(admin, "PGU-203", title="Still working",
                                         state="in_progress", assignee=working, commit_exempt=True)
            deliver("PGU-203", working)
            check(owner_role_of("PGU-203") == working,
                  f"the fixture is owned by the implementer it names: {owner_role_of('PGU-203')}")
            check(turn_ended("audit", "turn-9") == 0,
                  "a turn ending for a role that owns nothing actionable reports nothing")
            check(not any(row["kind"].startswith("unresolved_turn") for row in queued("PGU-203")),
                  f"and the working implementer is left alone: {queued('PGU-203')}")

            # 6. THE DEDUPE THAT REMAINS IS THE ONE THAT WAS MEANT: an
            #    escalation from the reminder path IS this news arriving again,
            #    so the Director is spared the second copy -- and the owner's
            #    repair prompt is NOT, because it was never the Director's news.
            fixture.seed_postgres_ticket(admin, "PGU-204", title="Already escalated",
                                         state="in_progress", assignee=escalated, commit_exempt=True)
            deliver("PGU-204", escalated)
            check(owner_role_of("PGU-204") == escalated,
                  f"and so is this one: {owner_role_of('PGU-204')}")
            fixture.psql(admin, """
DO $$
BEGIN
    PERFORM set_config('ticket_board.caller_role', 'director', true);
    PERFORM ticket_board.enqueue_notification(
        'PGU-204', 'escalation', 'director', 'already escalated by the reminder path',
        jsonb_build_object('kind', 'escalation', 'id', 'PGU-204'), 'escalation:PGU-204:1');
END $$;""")
            check(turn_ended(escalated, "turn-2") == 1,
                  "the owner is still prompted; the reminder path is not their prompt")
            check("unresolved_turn" not in kinds_for("PGU-204", "director"),
                  f"and no second escalation is queued for the Director: {queued('PGU-204')}")
            check(pass_only(grace="0 seconds") == 0,
                  "nor does the staged escalation add one the reminder path already sent")
            check("unresolved_turn_repair" in kinds_for("PGU-204", escalated),
                  f"but the owner still gets its one repair prompt: {queued('PGU-204')}")
            # ONE prompt, and the asymmetry is the point: the Director's copy was
            # never enqueued for this identity, so only the owner's own marker
            # can stop a repeat. A dedupe that read the Director's marker here
            # would prompt the owner again on every pass of an unresolved turn
            # the Director had already heard about by another route.
            repeated = [row for row in queued("PGU-204")
                        if row["kind"] == "unresolved_turn_repair"]
            check(turn_ended(escalated, "turn-2") == 0, "the same turn end reports nothing new")
            check([row for row in queued("PGU-204") if row["kind"] == "unresolved_turn_repair"]
                  == repeated,
                  f"and the owner is not prompted twice for one turn: {queued('PGU-204')}")

            # 7. THE SUPPRESSING ROW, ISOLATED. Case 1 proves the whole
            #    lifecycle; this states the mechanism on its own, because the
            #    regression was one clause: ANY director-targeted row disarmed
            #    the guard, and a kickback always leaves one. Here the row is
            #    put there directly, so nothing about the redirect or the
            #    comment can be what the case is really testing.
            fixture.psql(admin, "DELETE FROM ticket_board.tickets WHERE id='PGU-203';")
            free = working
            fixture.seed_postgres_ticket(admin, "PGU-205", title="Director copy unread",
                                         state="in_progress", assignee=free, commit_exempt=True)
            deliver("PGU-205", free)
            check(owner_role_of("PGU-205") == free,
                  f"the fixture is the implementer's: {owner_role_of('PGU-205')}")
            fixture.psql(admin, """
DO $$
BEGIN
    PERFORM set_config('ticket_board.caller_role', 'director', true);
    PERFORM ticket_board.enqueue_notification(
        'PGU-205', 'transition', 'director',
        'PGU-205 moved to in_progress',
        jsonb_build_object('kind', 'transition', 'id', 'PGU-205'),
        'transition:PGU-205:director:1');
END $$;""")
            check(kinds_for("PGU-205", "director") == {"transition"},
                  f"one unread director row, of the kind a kickback leaves: {queued('PGU-205')}")
            check(turn_ended(free, "turn-3") == 1,
                  "an unread transition row does not disarm the recovery")
            check("unresolved_turn_repair" in kinds_for("PGU-205", free),
                  f"the implementer is prompted: {queued('PGU-205')}")
            check(pass_only(grace="0 seconds") == 1,
                  "and the staged escalation still reaches the Director afterwards")
            check("unresolved_turn" in kinds_for("PGU-205", "director"),
                  f"which an unread transition row must not have suppressed: {queued('PGU-205')}")

            print(f"ticket_board_unresolved_turn_after_kickback_test: {CHECKS} checks ok")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
