#!/usr/bin/env python3
"""SYRD-629: Send to Director moves a ticket to the Director's decision stage, not to implementation.

The User's way out of User Review was `user_kick_back`, a return straight to
the implementer. The User asked for something else: their message goes to the
Director, who decides what follows -- clarify and return it to the User, send
it to the implementer, route it elsewhere, defer or cancel it. So:

* `user_send_to_director` (User, message required) moves the ticket from
  User Review to `user_feedback`, a system stage the Director alone holds. The
  move keeps the review's record: no sign-off is needed for it, none is cleared,
  and the candidate commit stays -- handing work to the Director is not a
  verdict. Exactly one notice goes out, to the Director.
* From there the Director returns it to the User (with instructions replaced
  or reconfirmed), sends it to the implementer (a return: the last implementer,
  sign-offs and commit cleared, one notice), routes it to Analysis, defers or
  cancels it.
* The exemption is the destination's shape and no wider: a move out of User
  Review to a review stage still needs the User's sign-off.
* Boards already running the shipped workflow get the change by migration; a
  board of another shape, or one already changed, is left alone.

Driven through the real HTTP handler against a real cluster.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()
import ticket_board_write_api_test as t  # noqa: E402
from scripts.ticket_board.workflow_config import advertised_transitions, director_hold_stage_names, validate  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402
from workflow_document_eras import before_user_feedback as before_syrd629  # noqa: E402

SHIPPED = json.loads((ROOT / "examples/workflows/inspection.json").read_text())
MIGRATION = (ROOT / "scripts/ticket_board/migrations/pgu1002_syrd629_uat_instructions.sql").read_text()
A = "a" * 40
NEW_ACTIONS = ("user_send_to_director", "director_return_to_uat", "director_send_to_implementer",
               "director_route_feedback")
CHECKS = 0


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def main() -> int:
    shipped = validate(json.loads(json.dumps(SHIPPED)))
    actions = {tr["action"]: tr for tr in shipped["transitions"] if "user_feedback" in (tr["from"], tr["to"])}
    send = actions["user_send_to_director"]
    check((send["from"], send["to"], send["actors"], send["primitive"], send["require_reason"], send["clear_signoffs"])
          == ("user_review", "user_feedback", ["user"], "move", True, []),
          f"Send to Director is the User's move to the Director's stage, with a message, clearing nothing: {send}")
    check(not any(tr["action"] in ("user_kick_back", "relay_user_kick_back") for tr in shipped["transitions"]),
          "and the User no longer returns work to the implementer directly")
    check(director_hold_stage_names(shipped) == {"draft", "analysis", "user_feedback"},
          f"the Director's hold stages, by shape: {director_hold_stage_names(shipped)}")
    stage = next(s for s in shipped["stages"] if s["name"] == "user_feedback")
    check(stage["owners"] == ["director"] and stage["label"] == "Director reviewing the User's message",
          f"a stage that says plainly who has it: {stage}")
    check(validate(before_syrd629(shipped)) is not None, "the earlier shape still validates (the migration's input)")

    with temporary_cluster(prefix="syrd629f-", shutdown="immediate") as cluster:
        root, sock, port = cluster.root, cluster.socket_dir, cluster.port
        db = "user_feedback_workflow_test"
        admin = t.conninfo(sock, port, db)
        t.run(["createdb", "-h", str(sock), "-p", str(port), "-U", "postgres", db])
        t.psql(admin, t.SCHEMA_PATH.read_text())
        t.create_roles(admin)
        t.psql(admin, t.RBAC_PATH.read_text())
        (root / "frames").mkdir(exist_ok=True)
        (root / "assets").mkdir(exist_ok=True)
        app = t.TicketBoardApp(root / "frames", root / "assets", project="cerulean", ticket_prefix="PGU",
                               database_url=t.conninfo(sock, port, db, t.SERVICE_ROLE))
        server = t.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=t.QuietNotifier())
        t.TEST_WRITE_TOKEN = server.write_token
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"

        def sql(query: str) -> str:
            return t.psql(admin, query)

        def act(ticket_id: str, operation: str, actor: str, expect: int = 200, **payload):
            return t.post_json(base, f"/api/tickets/{ticket_id}/actions/{operation}", payload, caller=actor,
                               expect=expect)

        def ticket(ticket_id: str) -> dict:
            return app.get_ticket(ticket_id)

        def queued(ticket_id: str) -> list[dict]:
            return json.loads(sql("SELECT coalesce(jsonb_agg(jsonb_build_object('role',target_role,'kind',kind) "
                                  f"ORDER BY id),'[]') FROM ticket_board.ticket_notification_queue WHERE ticket_id='{ticket_id}';"))

        def drain(ticket_id: str) -> None:
            sql(f"DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id='{ticket_id}';")

        def document() -> dict:
            return json.loads(sql("SELECT document FROM ticket_board.workflow_configuration WHERE singleton;"))

        def revision() -> int:
            return int(sql("SELECT coalesce(max(revision), 0) FROM ticket_board.workflow_configuration;"))

        def configure(doc: dict) -> None:
            t.post_json(base, "/api/tickets/actions/configure_workflow",
                        {"document": doc, "expected_revision": revision()}, caller="director")

        def at_user_review(ticket_id: str) -> None:
            """A candidate the Audit passed, with its UAT steps, in front of the User."""
            sql(f"DELETE FROM ticket_board.tickets WHERE id='{ticket_id}';")
            t.seed_postgres_ticket(admin, ticket_id, title="Tenant launch repair", state="dat", assignee="director",
                                   needs_audit=True, audit_signoff=True, needs_user_signoff=True, commit_hash=A)
            sql(f"UPDATE ticket_board.ticket_notification_state SET last_implementer_assignee='main' "
                f"WHERE ticket_id='{ticket_id}';")
            act(ticket_id, "set_uat_instructions", "director", do_this="Run `switchyard status otto`.",
                you_should_see="`otto: running`.")
            act(ticket_id, "director_dat_sign_off", "director")
            check(ticket(ticket_id)["state"] == "user_review", f"{ticket_id} is in front of the User")

        try:
            # -- a board running the shipped workflow ------------------------------------------------------
            configure(shipped)
            at_user_review("PGU-1")
            offered = [tr["action"] for tr in ticket("PGU-1")["workflow_actions"]]
            check("user_send_to_director" in offered and "user_kick_back" not in offered,
                  f"User Review offers Send to Director: {offered}")
            user_offered = [tr["action"] for tr in advertised_transitions(document(), ticket("PGU-1"), "user")]
            check("user_send_to_director" in user_offered,
                  f"and it is advertised to the User although they have not signed off: {user_offered}")
            missing = act("PGU-1", "user_send_to_director", "user", expect=400)
            check("reason" in str(missing).lower(), f"a message is required: {missing}")
            for actor in ("director", "main"):
                denied = act("PGU-1", "user_send_to_director", actor, expect=403, reason="not mine")
                check("cannot call user_send_to_director" in str(denied), f"{actor} may not send it: {denied}")

            drain("PGU-1")
            message = "The status said 'stopped' after I restarted otto."
            sent = act("PGU-1", "user_send_to_director", "user", reason=message)["ticket"]
            check((sent["state"], sent["assignee"]) == ("user_feedback", "director"),
                  f"it goes to the Director: {sent['state']} {sent['assignee']}")
            now = ticket("PGU-1")
            check(now["commit_hash"] == A and now["audit_signoff"] is True and now["user_signoff"] is False,
                  f"keeping the candidate and Audit's sign-off, with no User decision: {now['commit_hash']}")
            check(any(c["who"] == "user" and message in c["text"] for c in now["comments"]),
                  "the User's message is on the ticket, as theirs")
            notes = queued("PGU-1")
            check(len(notes) == 1 and notes[0]["role"] == "director",
                  f"exactly one notice, the Director's; none to the implementer: {notes}")
            check(now["uat"]["instructions"]["current"] is False, "the steps the User was given are superseded")

            # -- the Director decides: back to the User, with the steps reconfirmed ------------------------
            stale = act("PGU-1", "director_return_to_uat", "director", expect=400)
            check("UAT instructions required" in str(stale), f"not with the superseded steps: {stale}")
            act("PGU-1", "set_uat_instructions", "director", do_this="Restart otto, wait 30 seconds, then run "
                "`switchyard status otto`.", you_should_see="`otto: running`.")
            drain("PGU-1")
            back = act("PGU-1", "director_return_to_uat", "director")["ticket"]
            check((back["state"], back["assignee"]) == ("user_review", "user"), f"back to the User: {back['state']}")
            check(ticket("PGU-1")["commit_hash"] == A, "on the same candidate")
            accepted = act("PGU-1", "user_sign_off", "user")["ticket"]
            check(accepted["state"] == "director_review", f"which the User can now accept: {accepted['state']}")

            # -- or to the implementer -------------------------------------------------------------------------
            at_user_review("PGU-2")
            act("PGU-2", "user_send_to_director", "user", reason="It crashed on start.")
            drain("PGU-2")
            missing = act("PGU-2", "director_send_to_implementer", "director", expect=400)
            check("reason" in str(missing).lower(), f"the Director says why: {missing}")
            returned = act("PGU-2", "director_send_to_implementer", "director",
                           reason="The User saw a crash on start; see their message.")["ticket"]
            check((returned["state"], returned["assignee"]) == ("in_progress", "main"),
                  f"to the implementer it came from: {returned['state']} {returned['assignee']}")
            now = ticket("PGU-2")
            check(now["commit_hash"] == "" and now["audit_signoff"] is False,
                  f"as any return: the candidate and sign-offs cleared: {now['commit_hash']}")
            notes = queued("PGU-2")
            check(len(notes) == 1 and notes[0]["role"] == "main", f"and exactly one notice, the implementer's: {notes}")

            # -- or elsewhere ----------------------------------------------------------------------------------
            at_user_review("PGU-3")
            act("PGU-3", "user_send_to_director", "user", reason="This needs a design decision first.")
            from_stage = {tr["action"] for tr in ticket("PGU-3")["workflow_actions"]}
            check({"director_return_to_uat", "director_send_to_implementer", "director_route_feedback", "defer",
                   "cancel"} <= from_stage, f"the Director's choices from the decision stage: {sorted(from_stage)}")
            routed = act("PGU-3", "director_route_feedback", "director", reason="Split into a design ticket.")["ticket"]
            check(routed["state"] == "analysis", f"routed to Analysis: {routed['state']}")

            # -- the exemption is the destination's shape, no wider ----------------------------------------------
            at_user_review("PGU-4")
            bypass = json.loads(json.dumps(document()))
            bypass["transitions"].append({
                "from": "user_review", "to": "director_review", "action": "director_skip_user", "label": "Skip",
                "actors": ["director"], "primitive": "move", "owner_scoped": False, "require_commit": False,
                "require_reason": False, "clear_signoffs": [], "allow_no_code": False})
            configure(bypass)
            refused = act("PGU-4", "director_skip_user", "director", expect=400)
            check("stage signoff required" in str(refused),
                  f"a move to a review stage still needs the User's sign-off: {refused}")
            # Nor does a hold excuse another review: Audit's work moved to Analysis still needs Audit's sign-off.
            sql("DELETE FROM ticket_board.tickets WHERE id='PGU-8';")
            t.seed_postgres_ticket(admin, "PGU-8", title="In Audit", state="audit", assignee="audit", needs_audit=True,
                                   audit_signoff=False, commit_hash=A)
            elsewhere = json.loads(json.dumps(bypass))
            elsewhere["transitions"].append({
                "from": "audit", "to": "analysis", "action": "director_audit_to_triage", "label": "To triage",
                "actors": ["director"], "primitive": "move", "owner_scoped": False, "require_commit": False,
                "require_reason": False, "clear_signoffs": [], "allow_no_code": False})
            configure(elsewhere)
            refused = act("PGU-8", "director_audit_to_triage", "director", expect=400)
            check("stage signoff required" in str(refused) and ticket("PGU-8")["state"] == "audit",
                  f"a move from Audit to Analysis still needs Audit's sign-off: {refused}")
            check("director_audit_to_triage" not in {tr["action"] for tr in advertised_transitions(
                validate(elsewhere), ticket("PGU-8"), "director")}, "and is not offered")
            configure(shipped)
            # A hold is the Director's alone: a non-review stage the Director shares is no hold, here or in Python.
            shared = json.loads(json.dumps(document()))
            next(s for s in shared["stages"] if s["name"] == "analysis")["owners"] = ["director", "main"]
            configure(shared)
            check(sql("SELECT ticket_board.declared_director_hold('analysis');") == "f"
                  and "analysis" not in director_hold_stage_names(validate(shared))
                  and sql("SELECT ticket_board.declared_director_hold('user_feedback');") == "t",
                  "a stage the Director shares is not a hold, in the database as in Python")
            configure(shipped)
            # Handing a blocked ticket to the Director promotes nothing, so a blocker does not stand in the way.
            at_user_review("PGU-6")
            t.seed_postgres_ticket(admin, "PGU-7", title="Upstream fix", state="in_progress", assignee="main")
            act("PGU-6", "set_blockers", "director", blocked_by=["PGU-7"], blocked_reason="waits on the upstream fix")
            check(ticket("PGU-6")["blocked_by"] == ["PGU-7"], f"PGU-6 is blocked: {ticket('PGU-6')['blocked_by']}")
            blocked = act("PGU-6", "user_send_to_director", "user", reason="It still fails, but PGU-7 is open.")["ticket"]
            check(blocked["state"] == "user_feedback", f"a blocked ticket can still go to the Director: {blocked['state']}")

            # -- boards provisioned before: the migration -----------------------------------------------------------
            sql("DELETE FROM ticket_board.tickets;")
            # Back to the earlier shape on this board, which means removing the new stage explicitly.
            configure(before_syrd629(shipped) | {"remove_stages": ["user_feedback"]})
            old = document()
            check(any(tr["action"] == "user_kick_back" for tr in old["transitions"])
                  and not any(s["name"] == "user_feedback" for s in old["stages"]), "a board on the earlier workflow")
            prior = revision()
            t.psql(admin, MIGRATION)
            migrated = document()
            check(revision() == prior + 1 and sql(f"SELECT actor FROM ticket_board.workflow_revisions "
                                                  f"WHERE revision={revision()};") == "pgu1002",
                  "the migration changed it, once, as itself")
            names = {tr["action"] for tr in migrated["transitions"]}
            check(set(NEW_ACTIONS) <= names and not {"user_kick_back", "relay_user_kick_back"} & names
                  and any(s["name"] == "user_feedback" for s in migrated["stages"]),
                  f"to the shipped shape: {sorted(names)}")
            check(sql("SELECT count(*) FROM ticket_board.workflow_stages WHERE name='user_feedback';") == "1",
                  "with the stage projected, so tickets can be in it")
            check(migrated.get("migrations", {}).get("user_feedback") is True, "and marked")
            at_user_review("PGU-5")
            sent = act("PGU-5", "user_send_to_director", "user", reason="Looks wrong.")["ticket"]
            check(sent["state"] == "user_feedback", f"and it works on the migrated board: {sent['state']}")
            again = revision()
            t.psql(admin, MIGRATION)
            check(revision() == again, "run again, it changes nothing")

            # A board of another shape is left alone.
            sql("DELETE FROM ticket_board.tickets;")
            other = before_syrd629(shipped)
            other["transitions"] = [tr for tr in other["transitions"] if tr["action"] != "user_kick_back"
                                    and tr["action"] != "relay_user_kick_back"]
            configure(other | {"remove_stages": ["user_feedback"]})
            untouched = revision()
            t.psql(admin, MIGRATION)
            check(revision() == untouched and not any(s["name"] == "user_feedback" for s in document()["stages"]),
                  "a board without the User's kick-back is not changed")
            # Nor one whose control is not exactly the Director's, nor one missing a stage the new moves lead to.
            two = before_syrd629(shipped)
            for role in two["roles"]:
                if role["name"] == "audit":
                    role["capabilities"] = sorted(set(role["capabilities"]) | {"merge", "set_manually_controlled"})
            short = before_syrd629(shipped)
            short["stages"] = [s for s in short["stages"] if s["name"] != "cancelled"]
            short["transitions"] = [tr for tr in short["transitions"] if "cancelled" not in (tr["from"], tr["to"])]
            for label, variant in (("two control roles", two), ("no cancelled stage", short)):
                configure(variant | {"remove_stages": sorted({s["name"] for s in document()["stages"]}
                                                             - {s["name"] for s in variant["stages"]})})
                untouched = revision()
                t.psql(admin, MIGRATION)
                check(revision() == untouched and not any(s["name"] == "user_feedback" for s in document()["stages"]),
                      f"a board with {label} is not changed")
        finally:
            server.shutdown()
            server.server_close()
    print(f"user_feedback_workflow_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
