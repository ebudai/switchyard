#!/usr/bin/env python3
"""SYRD-237: the Director withdraws a candidate they found a defect in.

On SYRD-233 the Director moved an Audit-approved candidate from DAT to User
Review, then found a concrete implementation defect while preparing the live
UAT — before the User had accepted or rejected anything. `director_dat_kick_back`
correctly refuses outside DAT, and User Review offered only the User's own
kick-back, a relay that would put the Director's finding in the User's mouth,
cancel, defer, and an emergency override. The Director narrated an override back
to DAT and took the ordinary kick-back from there. Twice, on the same ticket.

The missing move is the Director's OWN withdrawal. It is deliberately not a
relay: `relays_decision_of` is absent, so the record carries the Director's
reason under the Director's name and claims nothing about the User. That is how
the three outcomes stay distinguishable in history — `user_kick_back`,
`relay_user_kick_back`, `director_withdraw` — with an override still meaning
what it always meant.

Driven through the real HTTP handler against a real cluster, on a board
provisioned with the shipped document and again on a board that predates the
action and reaches it by migration.
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
from scripts.ticket_board.workflow_config import available_transitions, validate  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

MIGRATION_PATH = ROOT / "scripts/ticket_board/migrations/pgu955_syrd237_director_withdrawal.sql"
MIGRATION = MIGRATION_PATH.read_text()
LATER_MIGRATIONS = sorted(
    p.read_text()
    for p in (ROOT / "scripts/ticket_board/migrations").glob("*.sql")
    if p.name > MIGRATION_PATH.name
)
SHIPPED = json.loads((ROOT / "examples/workflows/inspection.json").read_text())

WITHDRAW = "director_withdraw"
RELAY = "relay_user_kick_back"
CHECKS = 0


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def without_withdrawal(cfg: dict) -> dict:
    """The document of every board provisioned before this action existed."""
    stripped = json.loads(json.dumps(cfg))
    stripped["transitions"] = [
        tr for tr in stripped["transitions"] if tr.get("action") != WITHDRAW
    ]
    (stripped.get("migrations") or {}).pop("director_withdrawal", None)
    return stripped


def withdrawals(document: dict) -> list[dict]:
    return [tr for tr in document["transitions"] if tr["action"] == WITHDRAW]


def main() -> int:
    global CHECKS
    with temporary_cluster(prefix="syrd237-", shutdown="immediate") as cluster:
        root, sock, port = cluster.root, cluster.socket_dir, cluster.port
        db = "director_withdrawal_test"
        admin = t.conninfo(sock, port, db)
        t.run(["createdb", "-h", str(sock), "-p", str(port), "-U", "postgres", db])
        t.psql(admin, t.SCHEMA_PATH.read_text())
        t.create_roles(admin)
        t.psql(admin, t.RBAC_PATH.read_text())
        (root / "frames").mkdir(exist_ok=True)
        (root / "assets").mkdir(exist_ok=True)
        app = t.TicketBoardApp(
            root / "frames", root / "assets", project="cerulean", ticket_prefix="PGU",
            database_url=t.conninfo(sock, port, db, t.SERVICE_ROLE),
        )
        server = t.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=t.QuietNotifier())
        t.TEST_WRITE_TOKEN = server.write_token
        threading.Thread(target=server.serve_forever, daemon=True).start()
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
                f"ORDER BY position),'[]') FROM ticket_board.ticket_comments "
                f"WHERE ticket_id='{ticket_id}';"))

        def queued(ticket_id: str = "PGU-1") -> list[dict]:
            return json.loads(sql(
                "SELECT coalesce(jsonb_agg(jsonb_build_object('role',target_role,'kind',kind) "
                f"ORDER BY id),'[]') FROM ticket_board.ticket_notification_queue "
                f"WHERE ticket_id='{ticket_id}';"))

        def drain(ticket_id: str = "PGU-1") -> None:
            sql(f"DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id='{ticket_id}';")

        def revision() -> int:
            return int(sql("SELECT coalesce(max(revision), 0) FROM ticket_board.workflow_configuration;"))

        def document() -> dict:
            return json.loads(sql(
                "SELECT document FROM ticket_board.workflow_configuration WHERE singleton;"))

        def configure(doc: dict) -> None:
            t.post_json(
                base, "/api/tickets/actions/configure_workflow",
                {"document": doc, "expected_revision": revision()}, caller="director",
            )

        def at_user_review(ticket_id: str, *, commit: str = "a" * 40) -> None:
            """A candidate the Audit passed, waiting on the User and nothing else."""
            sql(f"DELETE FROM ticket_board.tickets WHERE id='{ticket_id}';")
            t.seed_postgres_ticket(
                admin, ticket_id, title="Tenant launch repair", state="user_review",
                assignee="user", needs_audit=True, audit_signoff=True,
                needs_user_signoff=True, commit_hash=commit,
            )
            # The implementer the work came from, which is what a return has to
            # find its way back to.
            sql(
                "UPDATE ticket_board.ticket_notification_state "
                f"SET last_implementer_assignee='main' WHERE ticket_id='{ticket_id}';"
            )

        def withdraw_once(phase: str, attempt: int) -> None:
            """One withdrawal, checked down to the record."""
            drain()
            before = ticket()
            check(
                before["state"] == "user_review" and before["audit_signoff"] is True,
                f"{phase}: a passed candidate awaiting the User: {before['state']}",
            )
            check(before["user_signoff"] is False, f"{phase}: and no User decision yet")
            check(before["commit_hash"] != "", f"{phase}: carrying a candidate")

            reason = (
                f"Director found a defect preparing UAT ({phase}, attempt {attempt}): "
                "the staged helper is never referenced."
            )
            out = act("PGU-1", WITHDRAW, "director", reason=reason)["ticket"]

            # Ordinary kick-back semantics, none of them restated by the action.
            check(out["state"] == "in_progress", f"{phase}: returned to implementation: {out}")
            check(out["assignee"] == "main", f"{phase}: to the implementation owner: {out}")
            now = ticket()
            check(
                now["audit_signoff"] is False and now["user_signoff"] is False,
                f"{phase}: stale sign-offs cleared: {now}",
            )
            check(now["commit_hash"] == "", f"{phase}: candidate provenance cleared: {now}")

            # The record is the Director's, and says nothing about the User.
            last = comments()[-1]
            check(last["who"] == "director", f"{phase}: attributed to the director: {last}")
            check(reason in last["text"], f"{phase}: carries the required reason")
            lowered = last["text"].lower()
            for claim in (
                "relayed this decision",
                "recorded as the decision of user",
                "user sign-off",
            ):
                check(claim not in lowered, f"{phase}: claims nothing of the User: {claim!r}")

            # Exactly one notification, to the implementer taking it back.
            notes = queued()
            check(len(notes) == 1, f"{phase}: notified exactly once: {notes}")
            check(notes[0]["role"] == "main", f"{phase}: and the right role: {notes}")

        def fences(phase: str) -> None:
            at_user_review("PGU-1")
            # A reason is the whole record of why a passed candidate went back.
            missing = act("PGU-1", WITHDRAW, "director", expect=400)
            check("reason" in str(missing).lower(), f"{phase}: a reason is required: {missing}")
            check(ticket()["state"] == "user_review", f"{phase}: and nothing moved")

            # It is the Director's alone. Nobody else may withdraw on their behalf.
            for actor in ("user", "main", "ops", "audit"):
                denied = act("PGU-1", WITHDRAW, actor, expect=403, reason="not mine to take")
                check(
                    f"{actor} cannot call {WITHDRAW}" in str(denied),
                    f"{phase}: {actor} refused: {denied}",
                )
            check(ticket()["state"] == "user_review", f"{phase}: still waiting on the User")

        try:
            shipped = validate(json.loads(json.dumps(SHIPPED)))

            # -- the three outcomes are distinct in the document ---------------
            doc_withdraw = next(tr for tr in shipped["transitions"] if tr["action"] == WITHDRAW)
            doc_relay = next(tr for tr in shipped["transitions"] if tr["action"] == RELAY)
            doc_user = next(tr for tr in shipped["transitions"] if tr["action"] == "user_kick_back")
            check(
                doc_withdraw["relays_decision_of"] is None,
                "a withdrawal is the Director's own decision, not a relay",
            )
            check(
                doc_relay["relays_decision_of"] == "user",
                "and the relay still says whose decision it carries",
            )
            check(doc_user["actors"] == ["user"], "and the User keeps their own action")
            check(
                doc_withdraw["to"] == doc_user["to"]
                and doc_withdraw["clear_signoffs"] == doc_user["clear_signoffs"],
                "the withdrawal lands where a rejection lands and clears what it clears",
            )
            check(doc_withdraw["require_reason"] is True, "and carries its reason")
            check(
                len({doc_withdraw["action"], doc_relay["action"], doc_user["action"]}) == 3,
                "three outcomes, three names in history",
            )

            # -- a board provisioned with the shipped document ------------------
            configure(shipped)
            at_user_review("PGU-1")
            withdraw_once("fresh", 1)

            # -- repeated post-DAT corrections, with no override in sight -------
            #
            # The second occurrence on SYRD-233 is the reason this ticket is a
            # regression rather than a request. Going round again must need
            # nothing special.
            for attempt in (2, 3):
                at_user_review("PGU-1", commit=("b" * 40) if attempt == 2 else ("c" * 40))
                withdraw_once("repeat", attempt)
            offered = {
                tr["action"] for tr in available_transitions(document(), app.get_ticket("PGU-1"), "director")
            }
            check(
                not any(name in offered for name in ("override_move", "force_move")),
                f"no override was needed at any point: {sorted(offered)}",
            )

            fences("fresh")

            # -- not after a real User decision ---------------------------------
            #
            # A User decision moves the ticket out of User Review, and the
            # withdrawal exists only there -- so "after a decision" is a state
            # this action cannot be reached from. The invariant is what makes
            # that true, so it is asserted rather than assumed: sign-offs are
            # not editable gates, so nothing can record a User acceptance while
            # the ticket sits here.
            at_user_review("PGU-2")
            sql("UPDATE ticket_board.ticket_notification_state "
                "SET last_implementer_assignee='main' WHERE ticket_id='PGU-2';")
            accepted = act("PGU-2", "user_sign_off", "user")["ticket"]
            check(accepted["state"] == "director_review", f"the User decided: {accepted}")
            check(ticket("PGU-2")["user_signoff"] is True, "and it is recorded")
            after = act("PGU-2", WITHDRAW, "director", expect=403, reason="too late")
            check(
                f"director cannot call {WITHDRAW}" in str(after)
                or "unauthorized" in str(after).lower(),
                f"the withdrawal is not available once the User has decided: {after}",
            )
            check(ticket("PGU-2")["state"] == "director_review", "and the decision stands")
            gated = t.post_json(
                base, "/api/tickets/PGU-2/actions/set_workflow_flags",
                {"patch": {"user_signoff": False}}, caller="director", expect=400,
            )
            check(
                "gate" in str(gated).lower(),
                f"and a sign-off is not an editable gate, so it cannot be staged: {gated}",
            )

            # -- a withdrawal does not jump the implementer's queue -------------
            #
            # Serial focus reserves the one ticket an implementer is on. A
            # withdrawal is an ordinary return, so it is held the same way
            # rather than barging into an occupied slot -- and the holding
            # destination says so instead of silently landing elsewhere.
            # Cleared first, so the one ticket holding main's slot is the one
            # this case puts there. Earlier phases left their returns in
            # implementation, and a reservation held by the wrong ticket would
            # make this pass for a reason it is not about.
            sql("DELETE FROM ticket_board.tickets;")
            t.seed_postgres_ticket(
                admin, "PGU-9", title="Work main already has", state="in_progress",
                assignee="main",
            )
            at_user_review("PGU-3")
            held = act("PGU-3", WITHDRAW, "director", reason="defect found while main is busy")["ticket"]
            check(held["state"] != "in_progress", f"it is not forced in: {held['state']}")
            check(held["queued_for_assignee"] == "main", f"and says who it waits for: {held}")
            check(held["queued_behind_ticket"] == "PGU-9", f"and behind what: {held}")
            check(ticket("PGU-3")["audit_signoff"] is False, "while still clearing the sign-offs")
            sql("DELETE FROM ticket_board.tickets WHERE id IN ('PGU-3','PGU-9');")

            # PGU-2 reached a stage that keeps main's reservation, which would
            # otherwise queue every later return in this suite behind it.
            sql("DELETE FROM ticket_board.tickets WHERE id='PGU-2';")

            # -- a board that predates the action -------------------------------
            configure(without_withdrawal(document()))
            at_user_review("PGU-1")
            refused = act("PGU-1", WITHDRAW, "director", expect=403, reason="found a defect")
            check(f"director cannot call {WITHDRAW}" in str(refused), f"{refused}")
            borrowed = act("PGU-1", "user_kick_back", "director", expect=403, reason="borrowing")
            check("director cannot call user_kick_back" in str(borrowed), f"{borrowed}")
            # The regression itself: the Director has no honest way back to
            # implementation from here. The relay exists, but it would record a
            # User decision that never happened.
            paths = {
                tr["action"]: tr["to"]
                for tr in available_transitions(document(), app.get_ticket("PGU-1"), "director")
            }
            honest = [
                action for action, to in paths.items()
                if to == "in_progress" and action != RELAY
            ]
            check(not honest, f"nothing but the relay returns it: {paths}")
            check(ticket()["state"] == "user_review", "so the candidate stays put")

            # -- the upgrade ------------------------------------------------------
            before = revision()
            t.psql(admin, MIGRATION)
            granted = revision()
            check(granted == before + 1, f"one revision, not more: {before} -> {granted}")
            for later in LATER_MIGRATIONS:
                t.psql(admin, later)
            added = withdrawals(document())
            check(len(added) == 1, f"exactly one withdrawal was added: {added}")
            model = next(tr for tr in document()["transitions"] if tr["action"] == "user_kick_back")
            check(added[0]["to"] == model["to"], f"modelled on the User's own: {added[0]}")
            check(added[0]["clear_signoffs"] == model["clear_signoffs"], f"{added[0]}")
            check(added[0]["actors"] == ["director"], f"{added[0]}")
            check(
                added[0]["primitive"] == "return" and added[0]["require_reason"] is True,
                f"{added[0]}",
            )
            check(
                added[0].get("relays_decision_of") is None,
                f"the migrated copy is not a relay either: {added[0]}",
            )
            check(
                (document().get("migrations") or {}).get("director_withdrawal") is True,
                "and the document records that it has been considered",
            )

            # The upgraded board behaves exactly like the provisioned one.
            at_user_review("PGU-1")
            withdraw_once("migrated", 1)
            fences("migrated")

            # -- re-applying the migration changes nothing ------------------------
            settled = revision()
            t.psql(admin, MIGRATION)
            check(revision() == settled, f"idempotent: {settled} -> {revision()}")
            check(len(withdrawals(document())) == 1, "and no duplicate transition")

            # -- a tenant that already has this move, under its own name ----------
            #
            # The guard is keyed on the SHAPE -- a non-relayed return from user
            # review by somebody other than the user -- rather than on the
            # action name, so a tenant that spells it differently keeps what it
            # has instead of growing a second way to do the same thing.
            sql("DELETE FROM ticket_board.tickets;")
            named = without_withdrawal(document())
            own = dict(next(tr for tr in named["transitions"] if tr["action"] == "user_kick_back"))
            own.update({
                "action": "take_it_back",
                "label": "Take it back",
                "actors": ["director"],
                "require_reason": True,
                "owner_scoped": False,
            })
            named["transitions"] = named["transitions"] + [own]
            configure(named)
            settled_named = revision()
            t.psql(admin, MIGRATION)
            check(revision() == settled_named, "a tenant with its own spelling gains no revision")
            check(not withdrawals(document()), "and no second action doing the same job")
            check(
                any(tr["action"] == "take_it_back" for tr in document()["transitions"]),
                "while keeping the one it had",
            )

            # -- a tenant whose control role is ambiguous is left alone ------------
            #
            # Handing the authority to withdraw a passed candidate to a guessed
            # role is worse than leaving a tenant to grant it deliberately, and
            # picking whichever sorts first is exactly that mistake with a tidier
            # shape.
            two_controllers = without_withdrawal(document())
            two_controllers["transitions"] = [
                tr for tr in two_controllers["transitions"] if tr["action"] != "take_it_back"
            ]
            control = ["set_manually_controlled", "merge"]
            deputy = None
            for role in two_controllers["roles"]:
                if role["name"] == "director":
                    check(
                        all(c in role["capabilities"] for c in control),
                        f"the director holds the control capabilities: {role['capabilities']}",
                    )
                if role["name"] == "ops":
                    deputy = role
            check(deputy is not None, "the fixture has a second role to promote")
            deputy["capabilities"] = sorted(set(deputy["capabilities"]) | set(control))
            configure(two_controllers)
            ambiguous = revision()
            t.psql(admin, MIGRATION)
            check(revision() == ambiguous, "an ambiguous control role gains no revision")
            check(
                not withdrawals(document()),
                "and the migration declines to guess which role may withdraw",
            )

            # -- a tenant with no User Review is left alone -----------------------
            # A real tenant without a User Review stage, not merely this one with
            # a stage deleted: DAT hands straight to director review, so the
            # stage after it still has an entry and the document validates.
            no_review = json.loads(json.dumps(shipped))
            rewired = []
            for tr in no_review["transitions"]:
                if tr["from"] == "dat" and tr["to"] == "user_review":
                    tr = {**tr, "to": "director_review"}
                elif "user_review" in (tr["from"], tr["to"]):
                    continue
                rewired.append(tr)
            no_review["transitions"] = rewired
            no_review["stages"] = [s for s in no_review["stages"] if s["name"] != "user_review"]
            no_review["remove_stages"] = ["user_review"]
            sql("DELETE FROM ticket_board.tickets;")
            configure(no_review)
            quiet = revision()
            t.psql(admin, MIGRATION)
            check(revision() == quiet, "a tenant with nothing to withdraw from gains nothing")
            check(not withdrawals(document()), "and no action it never needed")
        finally:
            server.shutdown()

    print(f"director_withdrawal_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
