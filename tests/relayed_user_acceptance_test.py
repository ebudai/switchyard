#!/usr/bin/env python3
"""SYRD-217: the Director records a successful User UAT without an override.

The counterpart to SYRD-214, and the case that ticket's reasoning missed. It
admitted relayed rejections only, on the argument that a relayed approval is a
forged sign-off -- but the User's acceptance is exactly as unreachable as their
rejection, and refusing to record it protected nobody. On SYRD-211 the User
accepted in conversation, the Director narrated an override to director_review
and closed the ticket, and `user_signoff` stayed false on a shipped ticket the
User had in fact accepted.

So a relay may now approve, under a fence tighter than the one it replaces, and
this drives the whole of it through the real HTTP handler against a real
cluster: on a board provisioned from schema.sql and again on one that reaches
the action by migration, since those install separate copies of the same
functions.
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
from publication_cache_fixture import build_cache, commit_file, publish
from schema_function_drift import assert_no_drift
from scripts.ticket_board.workflow_config import available_transitions, validate
from temporary_cluster import temporary_cluster

MIGRATIONS = ROOT / "scripts/ticket_board/migrations"
#: Every migration from the one that introduced relaying onward. An upgraded
#: board applies the sequence, not one file, and the last body installed wins.
UPGRADE = [p.read_text() for p in sorted(MIGRATIONS.glob("*.sql")) if p.name >= "pgu952"]

RELAY = "relay_user_sign_off"
KICK_BACK_RELAY = "relay_user_kick_back"
SHARED_FUNCTIONS = ("signoff_reset_key", "validate_declared_workflow", "perform_workflow_action_as")


#: An approval relayed for somebody IS that person's sign-off, so the record
#: says so -- what it still refuses to say is that the relayer reviewed
#: anything. The rejection relay's disclaimer would be a lie here.
ATTRIBUTION = [
    "director relayed this decision from user",
    "recorded as user sign-off",
    "decided by user and entered by director",
    "director did not perform the review behind it",
]


def legacy_document(cfg: dict) -> dict:
    """The document a board provisioned before relaying existed is running."""
    stripped = json.loads(json.dumps(cfg))
    stripped["transitions"] = [
        dict(tr) for tr in stripped["transitions"] if not tr.get("relays_decision_of")
    ]
    for tr in stripped["transitions"]:
        tr.pop("relays_decision_of", None)
    stripped.pop("migrations", None)
    return stripped


def relays(document: dict) -> list[dict]:
    return [tr for tr in document["transitions"] if tr["action"] == RELAY]


def main() -> int:
    with temporary_cluster(prefix="syrd217-", shutdown="immediate") as cluster:
        root, sock, port = cluster.root, cluster.socket_dir, cluster.port
        db = "relayed_user_acceptance_test"
        admin = t.conninfo(sock, port, db)
        t.run(["createdb", "-h", str(sock), "-p", str(port), "-U", "postgres", db])
        t.psql(admin, t.SCHEMA_PATH.read_text())
        t.create_roles(admin)
        t.psql(admin, t.RBAC_PATH.read_text())
        (root / "frames").mkdir(exist_ok=True)
        (root / "assets").mkdir(exist_ok=True)
        # The board resolves any commit it is handed against its own copy of the
        # project repository, so the candidate this acceptance names has to be a
        # commit that really exists -- as it would on a real board.
        cache, work = build_cache(root)
        COMMIT = commit_file(work, "candidate")
        OTHER_COMMIT = commit_file(work, "something-else")
        publish(cache, work, "app/syrd-211", COMMIT)
        publish(cache, work, "app/unrelated", OTHER_COMMIT)
        for ticket_id, title in (("PGU-1", "Tenant launch repair"), ("PGU-2", "Direct user review")):
            t.seed_postgres_ticket(
                admin, ticket_id, title=title, state="user_review", assignee="user",
                needs_audit=True, audit_signoff=True, needs_user_signoff=True,
                commit_hash=COMMIT, regression=True,
            )
        # A ticket whose deliverable was a finding, not code.
        t.seed_postgres_ticket(
            admin, "PGU-3", title="Spike, no code", state="user_review", assignee="user",
            needs_audit=True, audit_signoff=True, needs_user_signoff=True,
            commit_hash="", commit_exempt=True,
        )
        app = t.TicketBoardApp(
            root / "frames", root / "assets", project="cerulean", ticket_prefix="PGU",
            commit_git_dir=str(cache),
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

        def ticket(ticket_id: str) -> dict:
            return json.loads(sql(
                f"SELECT to_jsonb(x) FROM ticket_board.tickets x WHERE id='{ticket_id}';"))

        def comments(ticket_id: str) -> list[dict]:
            return json.loads(sql(
                "SELECT coalesce(jsonb_agg(jsonb_build_object('who',who,'text',text) "
                f"ORDER BY position),'[]') FROM ticket_board.ticket_comments WHERE ticket_id='{ticket_id}';"))

        def queued(ticket_id: str) -> list[dict]:
            return json.loads(sql(
                "SELECT coalesce(jsonb_agg(jsonb_build_object('role',target_role,'kind',kind) "
                f"ORDER BY id),'[]') FROM ticket_board.ticket_notification_queue WHERE ticket_id='{ticket_id}';"))

        def drain(ticket_id: str) -> None:
            sql(f"DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id='{ticket_id}';")

        def revision() -> int:
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

        def reset(ticket_id: str, **fields) -> None:
            """Put a ticket back at user_review, as its last submission left it.

            Re-seeded rather than updated: a direct UPDATE goes through the
            workflow trigger, which rightly refuses to move a ticket without a
            caller and a declared transition. Varying the gate and sign-off
            flags is a fixture's job, not a move, so the row is replaced.
            """
            columns = {
                "title": "Tenant launch repair", "state": "user_review",
                "assignee": "user", "user_signoff": False, "audit_signoff": True,
                "needs_user_signoff": True, "needs_audit": True, "commit_hash": COMMIT,
            }
            columns.update(fields)
            sql(f"DELETE FROM ticket_board.tickets WHERE id='{ticket_id}';")
            t.seed_postgres_ticket(admin, ticket_id, **columns)

        def refuse(bad: dict, why: str, *, says: str) -> None:
            """Both validators must refuse it, and for the same stated reason."""
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
            # by a writer that has already been stopped.
            literal = json.dumps(bad).replace("'", "''")
            try:
                sql(f"SELECT ticket_board.validate_declared_workflow('{literal}'::jsonb);")
            except AssertionError as exc:
                assert "sign-off authority" in str(exc).lower() or \
                       "invalid relayed decision policy" in str(exc).lower(), (why, exc)
            else:
                raise AssertionError(f"sql validator accepted {why}")

        def accept(phase: str, ticket_id: str = "PGU-1", **payload) -> dict:
            """The relayed acceptance, checked down to the record it leaves."""
            drain(ticket_id)
            before = ticket(ticket_id)
            assert before["state"] == "user_review" and before["user_signoff"] is False
            reason = f'User completed the acceptance steps and replied "ok all good" ({phase}).'
            relayed = act(ticket_id, RELAY, "director", reason=reason, **payload)["ticket"]
            after = ticket(ticket_id)
            # The exact transition: the normal post-UAT stage, and not an inch
            # further. Director final review still has to happen.
            assert after["state"] == "director_review", after
            assert after["assignee"] == "director", after
            assert relayed["state"] == "director_review", relayed
            # The sign-off is written, as the User's own would have been.
            assert after["user_signoff"] is True, after
            # And the candidate is untouched.
            assert after["commit_hash"] == before["commit_hash"], (before, after)
            assert after["commit_exempt"] == before["commit_exempt"], (before, after)
            # Provenance: recorded as the User's sign-off, entered by Director,
            # with no claim that Director reviewed anything.
            last = comments(ticket_id)[-1]
            assert last["who"] == "director", last
            body = last["text"].lower()
            for phrase in ATTRIBUTION:
                assert phrase in body, (phase, phrase, last["text"])
            assert "it is not user sign-off" not in body, (
                "the rejection relay's disclaimer would be false here", last["text"])
            assert reason in last["text"], last["text"]
            return after

        def exercise(phase: str) -> None:
            accept(phase, "PGU-1", commit_hash=COMMIT)
            # One notification, to whoever now owns the work -- and here that is
            # the Director who entered it, so the board's own rule against
            # paging the actor applies and there is nothing to send. What must
            # never happen is a second copy of it.
            assert queued("PGU-1") == [], queued("PGU-1")
            # It stopped at Director review: closing is still a separate act,
            # and still the Director's own.
            assert ticket("PGU-1")["state"] == "director_review"
            closed = act("PGU-1", "mark_done", "director", commit_hash=COMMIT)["ticket"]
            assert closed["state"] == "done" and ticket("PGU-1")["user_signoff"] is True

            # A commit-exempt ticket has no candidate to name, and naming one
            # would be accepting an artefact it never had.
            reset("PGU-3", title="Spike, no code", commit_exempt=True, commit_hash="")
            named = act("PGU-3", RELAY, "director", expect=400,
                        reason="relaying", commit_hash=COMMIT)
            assert "commit-exempt" in str(named).lower(), named
            accept(phase, "PGU-3")

            # Direct User sign-off is untouched: same destination, same flag,
            # and none of the relay's provenance, because none of it is true.
            reset("PGU-2", title="Direct user review")
            before = len(comments("PGU-2"))
            signed = act("PGU-2", "user_sign_off", "user")["ticket"]
            assert signed["state"] == "director_review", signed
            assert ticket("PGU-2")["user_signoff"] is True
            assert len(comments("PGU-2")) == before, "a direct sign-off is not a relay record"
            assert queued("PGU-2") == [{"role": "director", "kind": "transition"}], queued("PGU-2")

            # A relay is the Director's alone.
            reset("PGU-1", commit_hash=COMMIT)
            for role in ("app", "main", "ops", "audit", "inspector", "user"):
                denied = act("PGU-1", RELAY, role, expect=403,
                             reason="not mine to relay", commit_hash=COMMIT)
                assert f"{role} cannot call {RELAY}" in str(denied), denied
            assert ticket("PGU-1")["state"] == "user_review", "a refused relay moved nothing"

            # What it will not accept on.
            for payload, expected, why in (
                ({"reason": "  ", "commit_hash": COMMIT}, "reason required", "no reason"),
                ({"reason": "ok"}, "must name the recorded candidate commit", "no commit named"),
                ({"reason": "ok", "commit_hash": OTHER_COMMIT},
                 "must name the recorded candidate commit", "a different commit"),
            ):
                refused = act("PGU-1", RELAY, "director", expect=400, **payload)
                assert expected in str(refused).lower(), (why, refused)
                assert ticket("PGU-1")["user_signoff"] is False, why
                assert ticket("PGU-1")["commit_hash"] == COMMIT, ("commit altered by", why)

            # The stage's own gate has to be open, and every other review this
            # ticket is subject to has to have been given already.
            reset("PGU-1", commit_hash=COMMIT, needs_user_signoff=False)
            ungated = act("PGU-1", RELAY, "director", expect=400, reason="ok", commit_hash=COMMIT)
            assert "gated on" in str(ungated).lower(), ungated
            reset("PGU-1", commit_hash=COMMIT, audit_signoff=False)
            unaudited = act("PGU-1", RELAY, "director", expect=400, reason="ok", commit_hash=COMMIT)
            assert "earlier reviews" in str(unaudited).lower(), unaudited
            assert ticket("PGU-1")["user_signoff"] is False

            # Wrong stage: there is no relayed acceptance anywhere but the
            # review it stands in for.
            reset("PGU-1", commit_hash=COMMIT, state="audit", assignee="audit")
            elsewhere = act("PGU-1", RELAY, "director", expect=403, reason="ok", commit_hash=COMMIT)
            assert f"director cannot call {RELAY}" in str(elsewhere), elsewhere
            reset("PGU-1", commit_hash=COMMIT)

        def fences(phase: str) -> None:
            live = document()

            def variant(action: str = RELAY, **changes) -> dict:
                doc = json.loads(json.dumps(live))
                for tr in doc["transitions"]:
                    if tr["action"] == action:
                        tr.update(changes)
                return doc

            # The one that matters most: strip the provenance and it is simply
            # the Director approving the work it directs, which SYRD-82's floor
            # refuses exactly as it always did. The floor was narrowed by the
            # relayed case, not opened.
            refuse(variant(relays_decision_of=None),
                   f"a plain director approval ({phase})",
                   says="must not be granted sign-off authority")
            # An approval relayed for somebody names what it accepts.
            refuse(variant(require_commit=False),
                   f"a relayed approval naming no commit ({phase})",
                   says="must name the commit it accepts")
            # And lands where the User's own sign-off lands, not elsewhere.
            refuse(variant(to="in_progress"),
                   f"a relayed approval landing elsewhere ({phase})",
                   says="land exactly where")
            refuse(variant(clear_signoffs=["audit_signoff"]),
                   f"a relayed approval clearing what the User's does not ({phase})",
                   says="land exactly where")
            # Only for a role that cannot act for itself. Reached by giving
            # the User a pane rather than by renaming the relayed role: the
            # mirror rule catches a swap to `audit` first, since audit has no
            # approval out of this stage to mirror.
            paned = json.loads(json.dumps(live))
            for role in paned["roles"]:
                if role["name"] == "user":
                    role["runtime"] = "claude"
                    role["target"] = "cerulean-user:0.0"
            refuse(paned, f"a relayed approval for a role with a pane ({phase})",
                   says="no pane of its own")
            refuse(variant(actors=["user"]), f"the User relaying itself ({phase})",
                   says="does not relay its own decision")
            refuse(variant(owner_scoped=True), f"an owner-scoped relay ({phase})",
                   says="somebody other than the owner")
            refuse(variant(require_reason=False), f"a relay with no reason ({phase})",
                   says="must carry its reason")

            # Relaying is for decisions, so a `move` is not relayable even when
            # the relayed role has one to mirror -- which is the only situation
            # where this rule is the one doing the work.
            movable = json.loads(json.dumps(live))
            movable["transitions"].append({
                "from": "user_review", "to": "backlog", "action": "user_defer",
                "label": "User defer", "actors": ["user"], "primitive": "move",
                "owner_scoped": False, "require_commit": False, "require_reason": False,
                "clear_signoffs": [], "allow_no_code": False, "relays_decision_of": None,
            })
            movable["transitions"].append({
                "from": "user_review", "to": "backlog", "action": "relay_user_defer",
                "label": "Relay the User's deferral", "actors": ["director"],
                "primitive": "move", "owner_scoped": False, "require_commit": False,
                "require_reason": True, "clear_signoffs": [], "allow_no_code": False,
                "relays_decision_of": "user",
            })
            refuse(movable, f"a relayed move ({phase})", says="only return or approve work")

            # A relay may not be the move that ends a ticket. Isolating this one
            # needs the User's own sign-off to end the ticket too, or the mirror
            # rule catches the edit first -- and director_review then needs an
            # entry, since nothing else declares one.
            terminal = json.loads(json.dumps(live))
            for tr in terminal["transitions"]:
                if tr["action"] in (RELAY, "user_sign_off"):
                    tr["to"] = "done"
            terminal["transitions"].append({
                "from": "dat", "to": "director_review", "action": "route",
                "label": "Route", "actors": ["director"], "primitive": "move",
                "owner_scoped": False, "require_commit": False, "require_reason": False,
                "clear_signoffs": [], "allow_no_code": False, "relays_decision_of": None,
            })
            refuse(terminal, f"a relayed approval that closes the ticket ({phase})",
                   says="may not be the move that ends a ticket")

            # The refusals changed nothing.
            assert document() == live, phase

            # ...and the mirror rule compares what a transition does, not the
            # order somebody wrote it in. Both relays reset the same sign-offs
            # as the moves they stand in for; listing them the other way round
            # is the same document and has to be accepted as one.
            permuted = json.loads(json.dumps(live))
            model = next(tr for tr in permuted["transitions"] if tr["action"] == "user_kick_back")
            assert len(model["clear_signoffs"]) > 1, "nothing to permute"
            for tr in permuted["transitions"]:
                if tr["action"] == KICK_BACK_RELAY:
                    tr["clear_signoffs"] = list(reversed(model["clear_signoffs"]))
                    assert tr["clear_signoffs"] != model["clear_signoffs"], "not a permutation"
            validate(json.loads(json.dumps(permuted)))
            t.post_json(
                base, "/api/tickets/actions/configure_workflow",
                {"document": permuted, "expected_revision": revision(), "dry_run": True},
                caller="director",
            )
            literal = json.dumps(permuted).replace("'", "''")
            sql(f"SELECT ticket_board.validate_declared_workflow('{literal}'::jsonb);")

        shipped = validate(json.loads((ROOT / "examples/workflows/inspection.json").read_text()))
        assert len(relays(shipped)) == 1, "the shipped document carries the action"
        assert_no_drift(*SHARED_FUNCTIONS)

        try:
            # -- A board provisioned from schema.sql, running its bodies. -------
            configure(shipped)
            exercise("fresh")
            fences("fresh")

            # "Notify the next owner exactly once" only has teeth where the next
            # owner is somebody else. On the shipped document it is the Director
            # who just acted, and the board does not page the actor -- so the
            # handoff is checked on a tenant whose post-UAT review belongs to
            # another role, where exactly one notification has to go out.
            handed_on = json.loads(json.dumps(shipped))
            for stage in handed_on["stages"]:
                if stage["name"] == "director_review":
                    stage["owners"] = ["main"]
            # Nothing may be sitting in the stage whose owners are changing, or
            # the board rightly refuses to orphan it.
            reset("PGU-1", commit_hash=COMMIT)
            reset("PGU-2", title="Direct user review")
            reset("PGU-3", title="Spike, no code", commit_hash="", commit_exempt=True)
            configure(validate(handed_on))
            drain("PGU-1")
            passed_on = act("PGU-1", RELAY, "director",
                            reason="User accepted; handing on", commit_hash=COMMIT)["ticket"]
            assert passed_on["assignee"] == "main", passed_on
            assert queued("PGU-1") == [{"role": "main", "kind": "transition"}], queued("PGU-1")
            assert ticket("PGU-1")["user_signoff"] is True
            # Put it back where the next configuration can own it, for the same
            # reason the reconfiguration above needed the stage empty.
            reset("PGU-1", commit_hash=COMMIT)

            # -- A board provisioned before relaying existed. -------------------
            configure(legacy_document(shipped))
            reset("PGU-1", commit_hash=COMMIT)
            refused = act("PGU-1", RELAY, "director", expect=403,
                          reason="User accepted", commit_hash=COMMIT)
            assert f"director cannot call {RELAY}" in str(refused), refused
            # The User's own action is no way round it either: it is theirs.
            borrowed = act("PGU-1", "user_sign_off", "director", expect=403)
            assert "director cannot call user_sign_off" in str(borrowed), borrowed
            # And nothing the Director can do from user_review records an
            # acceptance: defer and cancel are not it, and the only completion
            # left is the narrated override this ticket exists to stop needing.
            offered = {
                tr["action"]: tr["to"]
                for tr in available_transitions(document(), app.get_ticket("PGU-1"), "director")
            }
            assert not any(to == "director_review" for to in offered.values()), offered
            assert ticket("PGU-1")["user_signoff"] is False

            # -- The upgrade. ---------------------------------------------------
            before = revision()
            for step in UPGRADE:
                t.psql(admin, step)
            after_all = revision()
            assert after_all > before, (before, after_all)
            added = relays(document())
            assert len(added) == 1, added
            model = next(tr for tr in document()["transitions"] if tr["action"] == "user_sign_off")
            assert added[0]["to"] == model["to"], added
            assert added[0]["clear_signoffs"] == model["clear_signoffs"], added
            assert added[0]["actors"] == ["director"], added
            assert added[0]["primitive"] == "approve", added
            assert added[0]["require_commit"] is True and added[0]["require_reason"] is True
            assert added[0]["owner_scoped"] is False and added[0]["relays_decision_of"] == "user"
            # The rejection relay came along too: one upgrade, both halves.
            assert any(tr["action"] == KICK_BACK_RELAY for tr in document()["transitions"])
            assert sql(
                "SELECT document->'migrations'->>'relay_user_acceptance' "
                "FROM ticket_board.workflow_configuration WHERE singleton;"
            ) == "true"
            # Idempotent: a second run grants nothing twice.
            for step in UPGRADE:
                t.psql(admin, step)
            assert revision() == after_all, (after_all, revision())
            assert len(relays(document())) == 1, document()["transitions"]

            # -- The same behaviour, now on the migrations' bodies. -------------
            reset("PGU-1", commit_hash=COMMIT)
            reset("PGU-2", title="Direct user review")
            reset("PGU-3", title="Spike, no code", commit_hash="", commit_exempt=True)
            exercise("upgraded")
            fences("upgraded")

            # -- Tenant shapes the upgrade must leave alone. --------------------
            def declines(bad: dict, why: str) -> None:
                """The upgrade grants no relayed approval on this shape.

                Stated as "no relayed approval afterwards" rather than "the
                revision did not move", because the sequence is more than one
                migration: pgu952 may legitimately add a rejection relay, and
                on a User that has its own pane pgu953 then takes it back out
                again. What must hold either way is that nobody gained the
                power to enter this User's acceptance.
                """
                def relayed_approvals() -> list[dict]:
                    return [
                        x for x in document()["transitions"]
                        if x.get("relays_decision_of") and x["primitive"] == "approve"
                    ]

                configure(bad)
                before_upgrade = relayed_approvals()
                for step in UPGRADE:
                    t.psql(admin, step)
                doc = document()
                # Nothing gained the power to enter this User's acceptance.
                # Stated as a delta rather than as "none exist", because one of
                # these tenants already has its own and keeping it is the point.
                assert relayed_approvals() == before_upgrade, (why, relayed_approvals())
                # And the whole sequence is still idempotent on this shape.
                settled = revision()
                for step in UPGRADE:
                    t.psql(admin, step)
                assert revision() == settled, (why, revision(), settled)
                assert document() == doc, why

            def without_relays(**edit) -> dict:
                doc = legacy_document(shipped)
                for tr in doc["transitions"]:
                    if tr["action"] == "user_sign_off":
                        tr.update(edit.get("sign_off", {}))
                    if tr["action"] == "user_kick_back":
                        tr.update(edit.get("kick_back", {}))
                for role in doc["roles"]:
                    if role["name"] in edit.get("grant_control", ()):
                        role["capabilities"] = sorted(
                            set(role["capabilities"]) | {"merge", "set_manually_controlled"})
                    if role["name"] in edit.get("give_pane", ()):
                        # A pane is a runtime and a target together; a target
                        # alone is not a surface anybody can be driven through.
                        role["runtime"] = "claude"
                        role["target"] = "cerulean-user:0.0"
                doc["transitions"] += edit.get("extra", [])
                return validate(doc)

            # A User with a board surface of its own signs off directly, and the
            # upgrade leaves that installation alone -- which is the same
            # condition the fence checks, reached from the other side.
            # A User with a board surface signs off directly. The rejection
            # relay pgu952 grants is left alone: the pane bound is on approvals
            # only, precisely so that tightening it does not invalidate what an
            # already-shipped migration produced.
            declines(without_relays(give_pane=("user",)), "the User has its own pane")
            # Nobody's acceptance to relay: the User does not review here.
            declines(
                without_relays(
                    sign_off={"action": "director_early_sign_off", "actors": ["audit"],
                              "owner_scoped": False},
                    kick_back={"action": "director_early_kick_back", "actors": ["director"],
                               "owner_scoped": False},
                ),
                "no user-owned approval out of user_review",
            )
            # Two roles could be the controller, so none of them is chosen.
            declines(without_relays(grant_control=("audit",)), "ambiguous control role")
            # The tenant already relays the User's acceptance, under its own
            # name. Matching on the action name alone would hand it a second.
            declines(without_relays(extra=[{
                "from": "user_review", "to": "director_review",
                "action": "record_user_acceptance", "label": "Record the User's acceptance",
                "actors": ["director"], "primitive": "approve", "owner_scoped": False,
                "require_commit": True, "require_reason": True, "clear_signoffs": [],
                "allow_no_code": False, "relays_decision_of": "user",
            }]), "the tenant already relays the User's acceptance")
            # The name is already taken by something the tenant declared.
            declines(without_relays(extra=[{
                "from": "director_review", "to": "in_progress", "action": RELAY,
                "label": "Tenant's own move", "actors": ["director"], "primitive": "return",
                "owner_scoped": False, "require_commit": False, "require_reason": True,
                "clear_signoffs": [], "allow_no_code": False, "relays_decision_of": None,
            }]), "the action name already means something else")

            # And on the ordinary shape it still grants, so the guards above are
            # refusals rather than a migration that never fires.
            configure(legacy_document(shipped))
            at = revision()
            for step in UPGRADE:
                t.psql(admin, step)
            assert revision() > at and len(relays(document())) == 1, document()["transitions"]
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    print("relayed_user_acceptance_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
