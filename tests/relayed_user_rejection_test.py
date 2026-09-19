#!/usr/bin/env python3
"""SYRD-214: the Director records a failed User UAT without an override.

The User reports UAT results in conversation, not on the board. `user_kick_back`
is the user role's own action and correctly refuses the Director, so the only
way to honour a rejection was a narrated `override_move` back to director_review
followed by an ordinary kick-back -- twice on SYRD-211, and once is already one
too many for an override.

Driven through the real HTTP handler against a real cluster, twice over: once on
a board provisioned from schema.sql, and once on a board that predates the
action and reaches it by migration. The two install separate copies of the same
two functions, as this project's migrations always have, so both copies get the
same exercise and a guard that stops them drifting apart.
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

from tmux_bus_isolation import isolate_tmux_bus

isolate_tmux_bus()
import ticket_board_write_api_test as t
from schema_function_drift import assert_no_drift
from scripts.ticket_board.workflow_config import available_transitions, validate
from temporary_cluster import temporary_cluster

MIGRATION_PATH = ROOT / "scripts/ticket_board/migrations/pgu952_syrd214_relay_user_rejection.sql"
MIGRATION = MIGRATION_PATH.read_text()
#: Applied after pgu952 on any board that takes the upgrade path, and it is the
#: later of the two that decides which function bodies an upgraded board runs.
LATER_MIGRATIONS = sorted(
    p.read_text()
    for p in (ROOT / "scripts/ticket_board/migrations").glob("*.sql")
    if p.name > MIGRATION_PATH.name
)

RELAY = "relay_user_kick_back"

#: What the actor may not be allowed to leave out, however hurried. The record
#: has to carry the attribution itself; a reason that happens to mention the
#: User is not the same thing.
ATTRIBUTION = [
    "director relayed this decision from user",
    "recorded as the decision of user",
    "it is not user sign-off",
    "director did not perform the review behind it",
]

#: Installed by schema.sql on a fresh board and by a migration on an existing
#: one. Nothing makes the two agree except saying so.
SHARED_FUNCTIONS = ("validate_declared_workflow", "perform_workflow_action_as")


def legacy_document(cfg: dict) -> dict:
    """The document every board provisioned before relaying existed.

    Every relay goes, not just this ticket's: the field cannot simply be
    stripped in place, because a relayed approval with its provenance removed
    is a plain director sign-off, which the capability floor refuses -- rightly,
    and that refusal is not the one this suite is here to reproduce.
    """
    stripped = json.loads(json.dumps(cfg))
    stripped["transitions"] = [
        dict(tr)
        for tr in stripped["transitions"]
        if not tr.get("relays_decision_of")
    ]
    for tr in stripped["transitions"]:
        tr.pop("relays_decision_of", None)
    stripped.pop("migrations", None)
    return stripped


def relays(document: dict) -> list[dict]:
    return [tr for tr in document["transitions"] if tr["action"] == RELAY]


def main() -> int:
    with temporary_cluster(prefix="syrd214-", shutdown="immediate") as cluster:
        root, sock, port = cluster.root, cluster.socket_dir, cluster.port
        db = "relayed_user_rejection_test"
        admin = t.conninfo(sock, port, db)
        t.run(["createdb", "-h", str(sock), "-p", str(port), "-U", "postgres", db])
        t.psql(admin, t.SCHEMA_PATH.read_text())
        t.create_roles(admin)
        t.psql(admin, t.RBAC_PATH.read_text())
        (root / "frames").mkdir(exist_ok=True)
        (root / "assets").mkdir(exist_ok=True)
        t.seed_postgres_ticket(
            admin, "PGU-1", title="Tenant launch repair", state="user_review",
            assignee="user", needs_audit=True, audit_signoff=True,
            needs_user_signoff=True, regression=True,
        )
        app = t.TicketBoardApp(
            root / "frames", root / "assets", project="cerulean", ticket_prefix="PGU",
            database_url=t.conninfo(sock, port, db, t.SERVICE_ROLE),
        )
        server = t.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=t.QuietNotifier())
        t.TEST_WRITE_TOKEN = server.write_token
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"

        def sql(query: str) -> str:
            return t.psql(admin, query)

        def act(ticket_id: str, operation: str, actor: str, expect: int = 200, **payload):
            return t.post_json(
                base, f"/api/tickets/{ticket_id}/actions/{operation}", payload,
                caller=actor, expect=expect,
            )

        def ticket(ticket_id: str = "PGU-1") -> dict:
            return json.loads(sql(
                f"SELECT to_jsonb(x) FROM ticket_board.tickets x WHERE id='{ticket_id}';"))

        def comments(ticket_id: str = "PGU-1") -> list[dict]:
            return json.loads(sql(
                "SELECT coalesce(jsonb_agg(jsonb_build_object('who',who,'text',text) "
                f"ORDER BY position),'[]') FROM ticket_board.ticket_comments WHERE ticket_id='{ticket_id}';"))

        def queued(ticket_id: str = "PGU-1") -> list[dict]:
            return json.loads(sql(
                "SELECT coalesce(jsonb_agg(jsonb_build_object('role',target_role,'kind',kind) "
                f"ORDER BY id),'[]') FROM ticket_board.ticket_notification_queue WHERE ticket_id='{ticket_id}';"))

        def drain(ticket_id: str = "PGU-1") -> None:
            sql(f"DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id='{ticket_id}';")

        def revision() -> int:
            # Empty until the first document lands: no row, revision zero.
            return int(sql(
                "SELECT coalesce(max(revision), 0) FROM ticket_board.workflow_configuration;"))

        def document() -> dict:
            return json.loads(sql(
                "SELECT document FROM ticket_board.workflow_configuration WHERE singleton;"))

        def configure(doc: dict) -> None:
            t.post_json(
                base, "/api/tickets/actions/configure_workflow",
                {"document": doc, "expected_revision": revision()}, caller="director",
            )

        def relay_once(phase: str, attempt: int) -> dict:
            """One relayed rejection, checked all the way down to the record."""
            drain()
            at = ticket()
            assert at["state"] == "user_review" and at["audit_signoff"] is True, at
            reason = f"User ran UAT on the Zorin tenant ({phase}): attempt {attempt} still fails at login."
            relayed = act("PGU-1", RELAY, "director", reason=reason)["ticket"]
            assert relayed["state"] == "in_progress", relayed
            assert relayed["assignee"] in ("main", "app", "ops"), relayed
            # The same stale sign-offs the User's own kick-back clears.
            now = ticket()
            assert now["audit_signoff"] is False and now["user_signoff"] is False, now
            # Provenance: the Director's name on the act, the User's on the
            # decision, and no claim to have done the review or signed off.
            last = comments()[-1]
            assert last["who"] == "director", last
            body = last["text"].lower()
            for phrase in ATTRIBUTION:
                assert phrase in body, (phase, phrase, last["text"])
            assert reason in last["text"], last["text"]
            # Exactly one notification, to the implementer holding the work.
            assert queued() == [{"role": relayed["assignee"], "kind": "transition"}], queued()
            return relayed

        def round_trip(owner: str, tag: str) -> None:
            """Back to user_review along the real path -- no override anywhere."""
            act("PGU-1", "submit_to_audit_without_commit", owner, reason=f"fix {tag}")
            assert ticket()["state"] == "audit", ticket()
            act("PGU-1", "audit_sign_off", "audit")
            act("PGU-1", "director_dat_sign_off", "director")
            assert ticket()["state"] == "user_review", ticket()
            assert ticket()["user_signoff"] is False, "a relay never signs off for the User"

        def exercise(phase: str) -> None:
            # Repeated failed UAT. Twice is what SYRD-211 actually did, and the
            # second pass is the one that used to need a second override.
            for attempt in (1, 2):
                relayed = relay_once(phase, attempt)
                round_trip(relayed["assignee"], f"{phase}-{attempt}")
            # A relay is the Director's alone, and it is never silent.
            for role in ("app", "main", "ops", "audit", "inspector", "user"):
                denied = act("PGU-1", RELAY, role, expect=403, reason="not mine to relay")
                assert f"{role} cannot call {RELAY}" in str(denied), denied
            assert ticket()["state"] == "user_review", "a refused relay moved nothing"
            silent = act("PGU-1", RELAY, "director", expect=400, reason="   ")
            assert "reason required" in str(silent).lower(), silent
            assert ticket()["state"] == "user_review", "a reasonless relay moved nothing"

        def refuse(bad: dict, why: str, *, says: str) -> None:
            """Both validators must refuse it, and for the relay's own reason.

            A document reaches the database through the handler and reaches the
            repository through the Python loader, so a fence only one of them
            holds is a fence with a way round it. Matching the message as well
            as the refusal is what distinguishes the relay rule biting from an
            unrelated rule happening to catch the same edit.
            """
            try:
                validate(json.loads(json.dumps(bad)))
            except Exception as exc:
                assert says in str(exc).lower(), (why, exc)
            else:
                raise AssertionError(f"python validator accepted {why}")
            rejected = t.post_json(
                base, "/api/tickets/actions/configure_workflow",
                {"document": bad, "expected_revision": revision(), "dry_run": True},
                caller="director", expect=400,
            )
            assert says in str(rejected).lower(), (why, rejected)
            # The handler refuses in Python before SQL sees the document, so the
            # database fence needs its own exercise or it is only ever reached
            # by a writer that has already been stopped. It is the one the
            # migration leans on, and the one a direct database write meets.
            literal = json.dumps(bad).replace("'", "''")
            try:
                sql(f"SELECT ticket_board.validate_declared_workflow('{literal}'::jsonb);")
            except AssertionError as exc:
                assert "invalid relayed decision policy" in str(exc).lower(), (why, exc)
            else:
                raise AssertionError(f"sql validator accepted {why}")

        def fences(phase: str) -> None:
            live = document()

            def variant(**changes) -> dict:
                doc = json.loads(json.dumps(live))
                for tr in doc["transitions"]:
                    if tr["action"] == RELAY:
                        tr.update(changes)
                return doc

            # SYRD-217 replaced "a relay may only return" with a narrower rule:
            # a relay must mirror a move the relayed role can make from here.
            # A rejection relay that turns itself into an approval is then
            # caught by what an approval has to carry, not by the primitive.
            refuse(variant(primitive="approve", to="director_review", clear_signoffs=[]),
                   f"a rejection relay recast as an approval ({phase})",
                   says="must name the commit it accepts")
            # Carrying the ticket past User review by any other primitive.
            refuse(variant(primitive="move", to="director_review", clear_signoffs=[]),
                   f"a relay that advances ({phase})", says="only return or approve work")
            # Returning somewhere the User's own kick-back does not go, or
            # clearing something it does not clear: a relay is that role's move
            # under another hand, so it may not differ in effect.
            refuse(variant(clear_signoffs=["user_signoff"]),
                   f"a relay that clears less than the User's own move ({phase})",
                   says="land exactly where")
            refuse(variant(relays_decision_of="audit"),
                   f"a relay of a decision that role never makes here ({phase})",
                   says="must mirror a move")
            refuse(variant(require_reason=False), f"a relay with no reason ({phase})",
                   says="must carry its reason")
            refuse(variant(actors=["user"]), f"a role relaying its own decision ({phase})",
                   says="does not relay its own decision")
            refuse(variant(owner_scoped=True), f"an owner-scoped relay ({phase})",
                   says="somebody other than the owner")
            refuse(variant(relays_decision_of="nobody"), f"a relay from an unknown role ({phase})",
                   says="must name a known role")
            refuse(variant(relays_decision_of=7), f"a relayed role that is not a name ({phase})",
                   says="must name a known role")
            # The refusals changed nothing.
            assert document() == live, phase

        shipped = validate(json.loads((ROOT / "examples/workflows/inspection.json").read_text()))
        assert len(relays(shipped)) == 1, "the shipped document carries the action"

        # Fresh provisioning installs schema.sql's copy of these functions and
        # an upgrade installs a migration's, which is how every migration in
        # this tree works. Nothing but this makes them the same function, and a
        # board that took the other path would quietly behave differently.
        # A later migration may take ownership of a body; this one keeps what it
        # shipped, so the comparison is against whichever migration runs last.
        assert_no_drift(*SHARED_FUNCTIONS)

        try:
            # -- A board provisioned from schema.sql, running its bodies. -------
            configure(shipped)
            exercise("fresh")
            fences("fresh")

            # -- A board provisioned before this existed. -----------------------
            configure(legacy_document(shipped))
            refused = act("PGU-1", RELAY, "director", expect=403, reason="User reports UAT failed")
            assert f"director cannot call {RELAY}" in str(refused), refused
            # The User's own action is not a way round it either: it is theirs.
            borrowed = act("PGU-1", "user_kick_back", "director", expect=403, reason="relaying")
            assert "director cannot call user_kick_back" in str(borrowed), borrowed
            # And the board offers the Director no correction path at all out of
            # user_review -- which is the whole of the regression. What is left
            # is defer, cancel, and the narrated override this ticket exists to
            # stop needing.
            offered = {
                tr["action"]: tr["to"]
                for tr in available_transitions(document(), app.get_ticket("PGU-1"), "director")
            }
            assert not any(to == "in_progress" for to in offered.values()), offered
            assert ticket()["state"] == "user_review" and ticket()["audit_signoff"] is True

            # -- The upgrade. ---------------------------------------------------
            before = revision()
            t.psql(admin, MIGRATION)
            # Anything that ships after this one is part of the same upgrade, so
            # it is applied too: a board sitting on pgu952's bodies alone is a
            # state no real board is ever left in.
            granted = revision()
            assert granted == before + 1, (before, granted)
            for later in LATER_MIGRATIONS:
                t.psql(admin, later)
            after_all = revision()
            added = relays(document())
            assert len(added) == 1, added
            model = next(tr for tr in document()["transitions"] if tr["action"] == "user_kick_back")
            assert added[0]["to"] == model["to"], added
            assert added[0]["clear_signoffs"] == model["clear_signoffs"], added
            assert added[0]["actors"] == ["director"], added
            assert added[0]["primitive"] == "return" and added[0]["require_reason"] is True
            assert added[0]["owner_scoped"] is False and added[0]["relays_decision_of"] == "user"
            assert sql(
                "SELECT document->'migrations'->>'relay_user_rejection' "
                "FROM ticket_board.workflow_configuration WHERE singleton;"
            ) == "true"
            # Idempotent: a second run of the whole sequence grants nothing
            # twice and writes no revision nobody asked for.
            t.psql(admin, MIGRATION)
            for later in LATER_MIGRATIONS:
                t.psql(admin, later)
            assert revision() == after_all, (after_all, revision())
            assert len(relays(document())) == 1, document()["transitions"]

            # -- The same behaviour, now on the migration's bodies. -------------
            exercise("upgraded")
            fences("upgraded")

            # -- Tenant shapes the upgrade must leave alone. --------------------
            # A migration that grants an authority has to be sure who it is
            # granting it to. Where the answer is not one role, or where the
            # shape this repairs is not the shape the tenant has, it does
            # nothing at all rather than guess.
            def declines(bad: dict, why: str) -> None:
                configure(bad)
                at, before_transitions = revision(), document()["transitions"]
                t.psql(admin, MIGRATION)
                assert revision() == at, (why, revision(), at)
                # Nothing added, nothing edited -- compared whole rather than by
                # action name, because one of these tenants already uses the
                # name and "declined" has to mean the document is untouched.
                assert document()["transitions"] == before_transitions, why

            def without_relay(**edit) -> dict:
                doc = legacy_document(shipped)
                for tr in doc["transitions"]:
                    if tr["action"] == "user_kick_back":
                        tr.update(edit.get("kick_back", {}))
                for role in doc["roles"]:
                    if role["name"] in edit.get("grant_control", ()):
                        role["capabilities"] = sorted(
                            set(role["capabilities"]) | {"merge", "set_manually_controlled"})
                doc["transitions"] += edit.get("extra", [])
                return validate(doc)

            # Nobody's rejection to relay: the User does not review here.
            declines(without_relay(kick_back={"action": "director_early_kick_back",
                                              "actors": ["director"], "owner_scoped": False}),
                     "no user-owned return out of user_review")
            # Two roles could be the controller, so none of them is chosen.
            declines(without_relay(grant_control=("audit",)), "ambiguous control role")
            # The name is already taken by something the tenant declared -- and
            # by something that is not a relay at all, which is the case worth
            # protecting: the migration must not overwrite a name whose meaning
            # here is the tenant's own.
            declines(without_relay(extra=[{
                "from": "director_review", "to": "in_progress", "action": RELAY,
                "label": "Tenant's own kick back", "actors": ["director"],
                "primitive": "return", "owner_scoped": False, "require_commit": False,
                "require_reason": True, "clear_signoffs": [], "allow_no_code": False,
                "relays_decision_of": None,
            }]), "the action name already means something else")
            # The tenant already relays the User's rejection, under its own
            # name. Matching on the name alone would hand it a second one.
            declines(without_relay(extra=[{
                "from": "user_review", "to": "in_progress", "action": "record_user_rejection",
                "label": "Record the User's rejection", "actors": ["director"],
                "primitive": "return", "owner_scoped": False, "require_commit": False,
                "require_reason": True, "clear_signoffs": ["audit_signoff", "user_signoff"],
                "allow_no_code": False, "relays_decision_of": "user",
            }]), "the tenant already relays the User's rejection")

            # And on the ordinary shape it still grants, so the guards above are
            # refusals rather than a migration that never fires.
            configure(legacy_document(shipped))
            at = revision()
            t.psql(admin, MIGRATION)
            assert revision() == at + 1 and len(relays(document())) == 1, document()["transitions"]
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    print("relayed_user_rejection_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
