#!/usr/bin/env python3
"""SYRD-662: Implementation could await the User without entering User Review or passing its gates.

SYRD-222 sat in Implementation, owned by Ops. Ops judged, correctly, that only
the User could run its live acceptance, and ran `request-dependency --role user`.
The board took it and moved nothing: the ticket stayed in Implementation with
Ops, `awaiting_role` became `user`, Ops' serial slot stayed taken, and the
ticket never reached User Review, where the User looks. The User found the
stall by hand. Nothing stopped a ticket claiming to wait on the User before
Audit either, and the Director's override could put an unaudited ticket in
User Review.

The cases run on boards built the production way (companion roles, schema.sql,
the real migrate runner, rbac.sql), with the shipped inspection workflow, the
same without User Review, the same with a Librarian's documentation gate and
the Publisher, and the same declaring `user_waits`. Every write goes through
the board's HTTP API as that role, or the real `ticket-board-write`, or, where
the database alone is the question, the board service's own connection.

- **The SYRD-222 shape:** a no-code UAT ticket in Implementation. Ops' wait on
  the User is refused, by every route, and changes nothing. The ticket then
  takes its declared no-code submission, Audit, and the Director's acceptance
  into User Review, with UAT steps, in the User's queue, and Ops is free.
- **An audited code ticket:** no wait on the User from Audit, and no way into
  User Review without Audit's sign-off for the commit it carries, the
  Director's override and edit included.
- **Documentation and Publisher gates:** User Review needs the documentation
  sign-off where the gate is on, and the publication of the ticket's commit.
- **A workflow with no User Review:** the refusal says so.
- **A person outside the board:** an `operator:` blocker is still the
  Director's to set and release, and moves nothing.
- **The Director's wait on the User:** only where `user_waits` lists the stage,
  with a reason and the User's requested action; never from Implementation.
- **A historical wait on the User:** a board upgraded from main keeps it,
  reports it not valid, puts it in the Director's queue, tells the Director
  once, and moves nothing; clearing it is the ordinary repair.

It runs on main, from a git archive in a child process, and on this tree.
"""

from __future__ import annotations

import ast
import copy
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKS = 0
A, B = "a" * 40, "b" * 40
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def clean_env() -> dict[str, str]:
    for key in [k for k in os.environ if k.startswith(PANE_ENV_PREFIXES)]:
        os.environ.pop(key)
    return dict(os.environ)


def example(root: Path) -> dict:
    cfg = json.loads((root / "examples/workflows/inspection.json").read_text())
    cfg["project"] = "pgu"
    cfg["queue"] = {"stage": "analysis", "assignee": "director"}
    for role in cfg["roles"]:
        if role.get("target"):
            role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
    return cfg


def without_user_review(cfg: dict) -> dict:
    """The same pipeline with no User at the end: Audit approves into the Director's review."""
    cfg = copy.deepcopy(cfg)
    gone = {"dat", "user_review", "user_feedback"}
    cfg["stages"] = [s for s in cfg["stages"] if s["name"] not in gone]
    for stage in cfg["stages"]:
        if stage["skip_to"] in gone:
            stage["skip_to"] = "director_review"
    kept = []
    for tr in cfg["transitions"]:
        if tr["from"] in gone:
            continue
        if tr["to"] in gone:
            tr["to"] = "director_review"
        kept.append(tr)
    cfg["transitions"] = kept
    cfg["remove_stages"] = sorted(gone)  # a fresh board starts with them
    return cfg


def with_hold_before_user_review(cfg: dict) -> dict:
    """A Director's hold before User Review, and branches that are not on the way there (Audit's topology).

    - `final_check`: a review the Director's acceptance now goes to; its gate is off by default, and then it
      skips to the hold -- the only way into the hold from Implementation;
    - `pre_user_hold`: a system stage the Director releases into User Review, and returns the User's feedback to;
    - `parked`: a parking stage (no owner, tells nobody) Audit and an early review can park work in; resumed, it
      goes back to the Director's acceptance or to that early review;
    - `triage`: a gated review before Implementation;
    - `rework`: a gated review the hold can send work to, which approves it back into Implementation;
    - `fix_review`: a gated review only after User Review, back into it.
    """
    cfg = copy.deepcopy(cfg)
    stages = {s["name"]: s for s in cfg["stages"]}
    route = next(t for t in cfg["transitions"] if t["action"] == "route" and t["from"] == "analysis")
    approve = next(t for t in cfg["transitions"] if t["action"] == "audit_sign_off")

    def stage(name: str, like: str, **fields) -> dict:
        return {**copy.deepcopy(stages[like]), "name": name, "label": name.replace("_", " ").capitalize(), **fields}

    def move(source: str, target: str, action: str, like: dict = route, actor: str = "director") -> dict:
        return {**copy.deepcopy(like), "from": source, "to": target, "action": action, "label": action,
                "actors": [actor]}

    def review(name: str, gate: str, skip_to: str) -> dict:
        return stage(name, "audit", owners=["inspector"], gate=f"needs_{gate}", signoff=f"{gate}_signoff", skip_to=skip_to)

    position = next(i for i, s in enumerate(cfg["stages"]) if s["name"] == "user_review")
    cfg["stages"][position:position] = [review("final_check", "final_check", "pre_user_hold"),
                                        stage("pre_user_hold", "analysis"),
                                        stage("parked", "analysis", owners=[], notify={"kind": "none", "role": None})]
    position = next(i for i, s in enumerate(cfg["stages"]) if s["name"] == "in_progress")
    cfg["stages"].insert(position, review("triage", "triage", "in_progress"))
    cfg["stages"].insert(position + 2, review("rework", "rework", "in_progress"))
    cfg["stages"].append(review("fix_review", "fix_review", "user_review"))
    for gate, on in (("final_check", False), ("triage", True), ("rework", True), ("fix_review", True)):
        cfg["flags"].update({f"needs_{gate}": {"kind": "gate", "default": on},
                             f"{gate}_signoff": {"kind": "signoff", "default": False}})
    for tr in cfg["transitions"]:
        if tr["to"] == "user_review" and tr["from"] != "user_feedback":
            tr["to"] = "final_check"
    cfg["transitions"] += [
        move("final_check", "director_review", "final_check_sign_off", approve, "inspector"),
        move("pre_user_hold", "user_review", "release_to_user"), move("user_feedback", "pre_user_hold", "hold_for_user"),
        move("audit", "parked", "park"), move("triage", "parked", "park"),
        move("parked", "dat", "resume"), move("parked", "triage", "retriage"), move("parked", "cancelled", "cancel"),
        move("analysis", "triage", "send_to_triage"), move("triage", "in_progress", "triage_sign_off", approve, "inspector"),
        move("pre_user_hold", "rework", "send_to_rework"), move("rework", "in_progress", "rework_sign_off", approve, "inspector"),
        move("user_feedback", "fix_review", "send_to_fix_review"),
        move("fix_review", "user_review", "fix_review_sign_off", approve, "inspector"),
    ]
    for name in ("final_check", "pre_user_hold", "triage", "rework", "fix_review"):
        cfg["transitions"] += [move(name, "backlog", "defer"), move(name, "cancelled", "cancel")]
    return cfg


def scenario(root: Path, prefix: str, before_root: Path | None = None) -> dict:
    for extra in (str(root / "tests"), str(root)):
        if extra in sys.path:
            sys.path.remove(extra)
        sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    from router_role_board_test import Board, build_board, fixture, migrate, temporary_cluster
    from scripts.ticket_board import read_client
    from scripts.ticket_board.documentation_gate import with_documentation_gate
    from scripts.ticket_board.publisher_stage import with_publisher
    from scripts.ticket_board.workflow_config import validate

    def short(answer: dict) -> str:
        """"ok", or the refusal's own message: post_json reports (path, status, body)."""
        if "ok" in answer:
            return "ok"
        text = answer.get("refused", "")
        try:
            text = str(ast.literal_eval(text)[2])
        except (ValueError, SyntaxError, IndexError, TypeError):
            pass
        return " ".join(text.split())[:600]

    seen: dict = {}
    with temporary_cluster(prefix=prefix, shutdown="immediate") as cluster:
        boards: list = []

        def board(name: str, document: dict):
            b = Board(cluster, name, document)
            boards.append(b)
            return b

        def snapshot(b, ticket: str) -> dict:
            """Everything about a ticket and its owner a refused wait must leave alone."""
            return {"ticket": b.snapshot(ticket),
                    "wait": b.sql("SELECT row(awaiting_role, awaiting_since_at, wait_recorded_at, nudge_count, "
                                  "last_activity_at)::text FROM ticket_board.ticket_notification_state "
                                  f"WHERE ticket_id = '{ticket}';"),
                    "queued": b.sql(f"SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{ticket}';"),
                    "reservations": b.reservations()}

        def raw(b, statement: str) -> str:
            try:
                b.sql(statement)
                return "ok"
            except AssertionError as refused:
                return " ".join(str(refused).split())[:400]

        def cli(b, role: str, *args: str) -> str:
            env = {"PATH": "/usr/bin:/bin", "HOME": str(cluster.root), "LANG": "C.UTF-8", "TICKET_BOARD_URL": b.base,
                   "TICKET_BOARD_WRITE_TOKEN": b.server.write_token, "TICKET_BOARD_CALLER_ROLE": role}
            done = subprocess.run([sys.executable, str(root / "scripts" / "ticket-board-write"), *args], env=env,
                                  cwd=str(cluster.root), capture_output=True, text=True, timeout=120, check=False)
            return "ok" if done.returncode == 0 else " ".join((done.stdout + done.stderr).split())[-1500:]

        try:
            # --- The shipped workflow. ---
            b = board("board", example(root))
            fixture.seed_postgres_ticket(b.admin, "PGU-1", title="Live Zorin acceptance", state="in_progress", assignee="ops",
                                         needs_user_signoff=True, commit_exempt=True)
            fixture.seed_postgres_ticket(b.admin, "PGU-6", title="An ordinary dependency", state="in_progress", assignee="app")
            before = snapshot(b, "PGU-1")
            seen["reserved"] = before["reservations"]
            reason = "Only the User can perform the live Zorin acceptance."
            seen["ops_dependency"] = short(b.act("PGU-1", "request_dependency", "ops", role="user", reason=reason))
            seen["ops_dependency_cli"] = cli(b, "ops", "request-dependency", "PGU-1", "--role", "user", "--reason", reason)
            seen["pgu1_after_main_route"] = b.sql("SELECT awaiting_role FROM ticket_board.ticket_notification_state "
                                                  "WHERE ticket_id = 'PGU-1';")
            b.sql("UPDATE ticket_board.ticket_notification_state SET awaiting_role = '', awaiting_since_at = NULL "
                  "WHERE ticket_id = 'PGU-1';")  # main recorded it; put the ticket back for the next route
            before = snapshot(b, "PGU-1")
            seen["ops_await_role"] = short(b.act("PGU-1", "await_role", "ops", role="user"))
            seen["ops_sql"] = b.as_role_sql("ops", "SELECT ticket_board.set_awaiting_role('PGU-1', 'user');")
            seen["ops_direct"] = b.as_role_sql("ops", "UPDATE ticket_board.ticket_notification_state SET awaiting_role = 'user', "
                                                      "awaiting_since_at = clock_timestamp() WHERE ticket_id = 'PGU-1';")
            seen["admin_direct"] = raw(b, "UPDATE ticket_board.ticket_notification_state SET awaiting_role = 'user', "
                                          "awaiting_since_at = clock_timestamp() WHERE ticket_id = 'PGU-1';")
            b.sql("UPDATE ticket_board.ticket_notification_state SET awaiting_role = '', awaiting_since_at = NULL "
                  "WHERE ticket_id = 'PGU-1' AND awaiting_role <> '';")  # main lets the direct writes through
            before = snapshot(b, "PGU-1")
            seen["director_dependency"] = short(b.act("PGU-1", "request_dependency", "director", role="user", reason=reason))
            seen["director_await_user"] = short(b.act("PGU-1", "await_user", "director", reason=reason,
                                                      request="Run the live acceptance on Zorin."))
            seen["unchanged"] = snapshot(b, "PGU-1") == before
            seen["app_awaits_director"] = short(b.act("PGU-6", "request_dependency", "app", role="director",
                                                      reason="Which database does the Director want?"))
            seen["choices"] = {f"{st}/{who}": b.sql(f"SELECT ticket_board.unresolved_turn_choices('{st}', '{who}', '{who}')::text;")
                               for st, who in (("in_progress", "ops"), ("analysis", "director"), ("audit", "audit"))}
            seen["options"] = b.sql("SELECT ticket_board.unresolved_turn_repair_options('{transition,request-dependency}');")
            # The declared way: Implementation's no-code submission, Audit, the Director's acceptance, User Review.
            b.sql("UPDATE ticket_board.ticket_notification_state SET awaiting_role = '', awaiting_since_at = NULL "
                  "WHERE ticket_id = 'PGU-1';")
            seen["submit"] = short(b.act("PGU-1", "submit_to_audit_without_commit", "ops",
                                         reason="No code: the live acceptance is the User's, in User Review."))
            seen["at_audit"] = b.where("PGU-1")
            seen["audited"] = short(b.act("PGU-1", "audit_sign_off", "audit", text="Nothing to review in code; the steps are right."))
            seen["at_dat"] = b.where("PGU-1")
            seen["steps"] = short(b.act("PGU-1", "set_uat_instructions", "director", do_this="Open the Zorin console.",
                                        you_should_see="The tenant answers.", then_choose=""))
            seen["accepted"] = short(b.act("PGU-1", "director_dat_sign_off", "director", text="Ready for the User."))
            user_review = b.ticket("PGU-1")
            seen["user_review"] = {"where": b.where("PGU-1"), "awaiting": user_review.get("awaiting_role"),
                                   "steps": ((user_review.get("uat") or {}).get("instructions") or {}).get("do_this"),
                                   "reservations": b.reservations()}
            with urllib.request.urlopen(f"{b.base}/api/board") as response:  # what `ticket-board-read queue user` reads
                seen["user_queue"] = [t["id"] for t in read_client.queue_payload(json.loads(response.read()), "user").get("user", [])]

            # An audited code ticket.
            def at(ticket: str, state: str, assignee: str, **flags) -> None:
                b.sql(f"DELETE FROM ticket_board.tickets WHERE id = '{ticket}';")
                fixture.seed_postgres_ticket(b.admin, ticket, title="Audited code", state=state, assignee=assignee,
                                             needs_user_signoff=True, commit_hash=A, **flags)
                b.sql(f"UPDATE ticket_board.ticket_notification_state SET last_implementer_assignee = 'app' WHERE ticket_id = '{ticket}';")
                b.act(ticket, "set_uat_instructions", "director", do_this="Run it.", you_should_see="It works.", then_choose="")

            at("PGU-2", "audit", "audit")
            seen["audit_dependency"] = short(b.act("PGU-2", "request_dependency", "audit", role="user",
                                                   reason="The User should look at this first."))
            b.sql("UPDATE ticket_board.ticket_notification_state SET awaiting_role = '', awaiting_since_at = NULL WHERE ticket_id = 'PGU-2';")
            seen["forced_unaudited"] = short(b.act("PGU-2", "force_move", "director", state="user_review", assignee="user"))
            seen["forced_where"] = b.where("PGU-2")
            at("PGU-2", "audit", "audit")
            seen["edited_unaudited"] = short(b.act("PGU-2", "director_edit", "director", reason="straight to the User",
                                                   patch={"state": "user_review", "assignee": "user"}))
            seen["edited_where"] = b.where("PGU-2")
            at("PGU-2", "audit", "audit")
            seen["audit_signs"] = short(b.act("PGU-2", "audit_sign_off", "audit", text="Reviewed this commit."))
            seen["to_user"] = short(b.act("PGU-2", "director_dat_sign_off", "director", text="Ready."))
            seen["audited_where"] = b.where("PGU-2")
            # A sign-off follows its commit: a return clears it, and the next commit owes Audit again.
            at("PGU-3", "dat", "director", audit_signoff=True)
            seen["returned"] = short(b.act("PGU-3", "director_dat_kick_back", "director", reason="Change the copy.",
                                           target_assignee="app"))
            b.act("PGU-3", "set_uat_instructions", "director", do_this="Run it.", you_should_see="It works.", then_choose="")
            seen["forced_after_return"] = short(b.act("PGU-3", "force_move", "director", state="user_review", assignee="user"))
            seen["returned_where"] = b.where("PGU-3")
            # A person outside the board: an operator blocker, as before.
            fixture.seed_postgres_ticket(b.admin, "PGU-5", title="Export on the operator machine", state="in_progress", assignee="ops")
            seen["operator"] = short(b.act("PGU-5", "set_blockers", "director", blocked_by=["operator:eric"],
                                           blocked_reason="Eric runs the reviewed export tonight."))
            seen["operator_where"] = [*b.where("PGU-5"), b.sql("SELECT awaiting_role FROM ticket_board.ticket_notification_state "
                                                                "WHERE ticket_id = 'PGU-5';")]
            seen["operator_released"] = short(b.act("PGU-5", "release_external_blocker", "director", ref="operator:eric",
                                                    reason="Eric ran it; the export is in."))

            # --- A workflow with no User Review. ---
            n = board("nouat", without_user_review(example(root)))
            fixture.seed_postgres_ticket(n.admin, "PGU-1", title="No User here", state="in_progress", assignee="ops")
            seen["nouat_dependency"] = short(n.act("PGU-1", "request_dependency", "ops", role="user", reason=reason))

            # --- The Librarian's documentation gate and the Publisher. ---
            full = with_publisher(with_documentation_gate(example(root), librarian={"runtime": "claude", "template_role": "main"}))
            f = board("full", full)
            for ticket, state, owner in (("PGU-1", "in_progress", "app"), ("PGU-2", "publication", "publisher")):
                fixture.seed_postgres_ticket(f.admin, ticket, title="Gated", state=state, assignee=owner, commit_hash=A,
                                             needs_user_signoff=True, audit_signoff=state == "publication")
                f.act(ticket, "set_uat_instructions", "director", do_this="Run it.", you_should_see="It works.", then_choose="")
            # The Director turns the gate on while the work is in Implementation, and it enters the Librarian's stage.
            seen["documentation_gate"] = [short(f.act("PGU-1", "set_workflow_flags", "director", needs_documentation=True)),
                                          short(f.act("PGU-1", "force_move", "director", state="documentation", assignee="librarian"))]
            seen["forced_undocumented"] = short(f.act("PGU-1", "force_move", "director", state="user_review", assignee="user"))
            seen["forced_unpublished"] = short(f.act("PGU-2", "force_move", "director", state="user_review", assignee="user"))
            seen["gated_where"] = [f.where("PGU-1"), f.where("PGU-2")]
            if f.sql("SELECT to_regclass('ticket_board.publication_attempts') IS NOT NULL;") == "t":
                # The Publisher's verified journal, as publisher_finish requires before it records one: first for
                # another commit, which is not this one's publication, then for this one.
                f.sql("INSERT INTO ticket_board.publication_attempts (ticket_id, round_at, candidate, state, step) "
                      f"VALUES ('PGU-2', clock_timestamp(), '{B}', 'published', 'verified');")
                seen["forced_other_published"] = short(f.act("PGU-2", "force_move", "director", state="user_review",
                                                             assignee="user"))
                f.sql("INSERT INTO ticket_board.publication_attempts (ticket_id, round_at, candidate, state, step) "
                      f"VALUES ('PGU-2', clock_timestamp(), '{A}', 'published', 'verified');")
                seen["forced_published"] = short(f.act("PGU-2", "force_move", "director", state="user_review", assignee="user"))
                seen["published_where"] = f.where("PGU-2")

            # --- A Director's hold before User Review (Audit's topology), with the Librarian and the Publisher. ---
            # The way into User Review passes a gate skip and a system stage; a parking stage, a review before
            # Implementation and a review only after User Review are not on it, so their gates, on by default
            # here, are not owed.
            h = board("hold", with_publisher(with_documentation_gate(with_hold_before_user_review(example(root)),
                                                                     librarian={"runtime": "claude", "template_role": "main"})))

            def held(ticket: str, *, audit: bool, documented: bool = False, published: bool = True) -> None:
                fixture.seed_postgres_ticket(h.admin, ticket, title="Held", state="in_progress", assignee="app",
                                             commit_hash=A, needs_user_signoff=True, audit_signoff=audit)
                h.act(ticket, "set_uat_instructions", "director", do_this="Run it.", you_should_see="It works.", then_choose="")
                if documented:
                    h.act(ticket, "set_workflow_flags", "director", needs_documentation=True)
                h.act(ticket, "force_move", "director", state="pre_user_hold", assignee="director")
                if published:
                    h.sql("INSERT INTO ticket_board.publication_attempts (ticket_id, round_at, candidate, state, step) "
                          f"VALUES ('{ticket}', clock_timestamp(), '{A}', 'published', 'verified');")

            def owed(ticket: str) -> str:
                return h.sql("SELECT ticket_board.uat_missing_gates(ticket_board.declared_workflow(), to_jsonb(t), "
                             f"'user_review')::text FROM ticket_board.tickets t WHERE t.id = '{ticket}';")

            held("PGU-1", audit=False)
            held("PGU-2", audit=True, documented=True)
            held("PGU-3", audit=False, published=False)
            seen["hold_seeded"] = {t: [*h.where(t), h.sql("SELECT audit_signoff::text || '|' || workflow_flags::text "
                                                          f"FROM ticket_board.tickets WHERE id = '{t}';")]
                                   for t in ("PGU-1", "PGU-2", "PGU-3")}
            if h.sql("SELECT to_regprocedure('ticket_board.uat_missing_gates(jsonb,jsonb,text)') IS NOT NULL;") == "t":
                seen["hold_owed"] = {t: owed(t) for t in ("PGU-1", "PGU-2", "PGU-3")}
            seen["hold_unaudited"] = [
                short(h.act("PGU-1", "force_move", "director", state="user_review", assignee="user")),
                short(h.act("PGU-1", "director_edit", "director", reason="straight to the User",
                            patch={"state": "user_review", "assignee": "user"})),
                short(h.act("PGU-1", "release_to_user", "director", text="Ready."))]
            seen["hold_undocumented"] = short(h.act("PGU-2", "force_move", "director", state="user_review", assignee="user"))
            seen["hold_unpublished"] = short(h.act("PGU-3", "director_edit", "director", reason="straight to the User",
                                                   patch={"state": "user_review", "assignee": "user"}))
            seen["hold_refused_where"] = [h.where(t) for t in ("PGU-1", "PGU-2", "PGU-3")]
            # With Audit's sign-off for its commit and its publication, each way in admits it, the triage, rework
            # and post-UAT gates notwithstanding.
            for ticket in ("PGU-4", "PGU-5", "PGU-6"):
                held(ticket, audit=True)
            seen["hold_owed_ready"] = owed("PGU-4") if "hold_owed" in seen else None
            seen["hold_ready"] = [
                short(h.act("PGU-4", "force_move", "director", state="user_review", assignee="user")),
                short(h.act("PGU-5", "director_edit", "director", reason="ready for the User",
                            patch={"state": "user_review", "assignee": "user"})),
                short(h.act("PGU-6", "release_to_user", "director", text="Ready."))]
            seen["hold_ready_where"] = [h.where(t) for t in ("PGU-4", "PGU-5", "PGU-6")]

            # --- The Director's wait on the User, where the workflow lets one stand. ---
            declared = example(root)
            declared["user_waits"] = ["analysis"]
            try:
                d = board("declared", declared)
            except Exception as refused:  # main's validator does not know the field
                seen["declared"] = " ".join(str(refused).split())[:300]
                d = None
            if d is not None:
                seen["declared"] = "ok"
                fixture.seed_postgres_ticket(d.admin, "PGU-1", title="A product decision", state="analysis", assignee="director")
                fixture.seed_postgres_ticket(d.admin, "PGU-2", title="Implementation", state="in_progress", assignee="ops")
                seen["user_await_user"] = short(d.act("PGU-1", "await_user", "user", reason="r", request="q"))
                seen["user_await_user_sql"] = d.as_role_sql("user", "SELECT ticket_board.await_user('PGU-1', 'r', 'q');")
                seen["ops_await_user_sql"] = d.as_role_sql("ops", "SELECT ticket_board.await_user('PGU-1', 'r', 'q');")
                seen["director_await_user_blank"] = short(d.act("PGU-1", "await_user", "director", reason=" ", request="Pick one."))
                seen["director_await_user_cli"] = cli(d, "director", "await-user", "PGU-1", "--reason",
                                                      "Two layouts are equally good; it is the User's product.",
                                                      "--request", "Choose layout A or B, in a comment.")
                pgu1 = d.ticket("PGU-1")
                seen["declared_wait"] = {"awaiting": pgu1.get("awaiting_role"), "user_wait": pgu1.get("user_wait"),
                                         "where": d.where("PGU-1"), "needs_director": read_client.needs_director(pgu1),
                                         "queued": json.loads(d.sql(
                                             "SELECT coalesce(json_agg(json_build_array(target_role, payload->>'kind') ORDER BY id), "
                                             "'[]') FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-1';")),
                                         "comment": d.sql("SELECT text FROM ticket_board.ticket_comments WHERE ticket_id = 'PGU-1' "
                                                          "ORDER BY position DESC LIMIT 1;")}
                seen["declared_text"] = read_client.format_ticket(pgu1)
                seen["declared_choices"] = d.sql("SELECT ticket_board.unresolved_turn_choices('analysis', 'director', 'director')::text;")
                seen["declared_options"] = d.sql("SELECT ticket_board.unresolved_turn_repair_options('{transition,await-user}');")
                seen["declared_impl_ops"] = short(d.act("PGU-2", "request_dependency", "ops", role="user", reason=reason))
                seen["declared_director_dependency"] = short(d.act("PGU-1", "request_dependency", "director", role="user",
                                                                   reason="The User decides."))
                # The service cannot forge await-user's admission: a session setting means nothing, and its mark is
                # in a table only await-user writes.
                seen["forged_setting"] = d.as_role_sql("director", "SELECT set_config('ticket_board.user_wait_ticket', "
                                                                   "'PGU-1', true); SELECT ticket_board.set_awaiting_role('PGU-1', 'user');")
                seen["forged_mark"] = d.as_role_sql("director", "INSERT INTO ticket_board.ticket_user_wait_requests (ticket_id, "
                                                                "role, requested_by, reason, request, pending_xact) VALUES ('PGU-1', "
                                                                "'user', 'director', 'r', 'q', txid_current());")
                seen["declared_impl_director"] = short(d.act("PGU-2", "await_user", "director", reason=reason, request="Run it."))
                seen["declared_cleared"] = short(d.act("PGU-1", "clear_awaiting_role", "director"))
                seen["declared_after_clear"] = d.ticket("PGU-1").get("user_wait")
                # The workflow stops letting the Director wait on the User there: the wait standing is reported, not moved.
                seen["declared_again"] = short(d.act("PGU-1", "await_user", "director", reason="Still the User's call.",
                                                     request="Choose layout A or B."))
                d.sql("UPDATE ticket_board.workflow_configuration SET document = jsonb_set(document, '{user_waits}', '[]') "
                      "WHERE singleton;")
                seen["declared_withdrawn"] = {"user_wait": d.ticket("PGU-1").get("user_wait"), "where": d.where("PGU-1")}
                d.act("PGU-1", "clear_awaiting_role", "director")
                # What workflow_config refuses to list, the database ignores even if a document said it.
                refusals = {}
                for listed in (["in_progress"], ["user_review"], ["done"], ["backlog"], ["nowhere"], ["analysis", "analysis"]):
                    try:
                        validate({**declared, "user_waits": listed})
                        refusals[",".join(listed)] = "accepted"
                    except ValueError as refused:
                        refusals[",".join(listed)] = str(refused)[:120]
                lonely = without_user_review(declared)
                lonely["roles"] = [{**r, "active": False} if r["name"] == "user" else r for r in lonely["roles"]]
                try:
                    validate(lonely)
                    refusals["inactive user"] = "accepted"
                except ValueError as refused:
                    refusals["inactive user"] = str(refused)[:120]
                seen["validate"] = refusals
                stored = json.loads(d.sql("SELECT document::text FROM ticket_board.workflow_configuration WHERE singleton;"))
                stored["user_waits"] = ["analysis", "in_progress", "user_review", "done", "backlog"]
                for stage in stored["stages"]:
                    if stage["name"] == "done":
                        stage["owners"] = ["director"]  # a terminal stage someone owns is still terminal
                d.sql("UPDATE ticket_board.workflow_configuration SET document = '"
                      + json.dumps(stored).replace("'", "''") + "'::jsonb WHERE singleton;")
                seen["sql_ignores"] = {s: d.sql(f"SELECT ticket_board.workflow_user_wait_stage('{s}');")
                                       for s in ("analysis", "in_progress", "user_review", "done", "backlog")}

            # --- A board built on main, with a wait on the User already standing, upgraded to this tree. ---
            if before_root is not None:
                u_admin = build_board(cluster, "upgraded", before=before_root)
                u = Board.__new__(Board)
                u.admin = u_admin
                u.service = fixture.conninfo(cluster.socket_dir, cluster.port, "upgraded", fixture.SERVICE_ROLE)
                u.app = fixture.TicketBoardApp(cluster.root / "u-frames", cluster.root / "u-assets", project="pgu",
                                               ticket_prefix="PGU", database_url=u.service)
                u.app.apply_workflow(example(root), expected_revision=0, dry_run=False, caller_role="director")
                fixture.seed_postgres_ticket(u_admin, "PGU-222", title="SYRD-222 as it stood", state="in_progress", assignee="ops",
                                             commit_exempt=True)
                seen["historical_recorded"] = u.as_role_sql("ops", "SELECT ticket_board.request_dependency('PGU-222', 'user', "
                                                                   "'Only the User can perform the live acceptance.');")
                migrate(u_admin)
                u.server = fixture.TicketBoardServer(("127.0.0.1", 0), u.app, director_notifier=fixture.QuietNotifier())
                threading.Thread(target=u.server.serve_forever, daemon=True).start()
                u.base = f"http://127.0.0.1:{u.server.server_port}"
                u.sleepers = []
                boards.append(u)
                pgu = u.ticket("PGU-222")
                seen["historical"] = {"where": u.where("PGU-222"), "awaiting": pgu.get("awaiting_role"),
                                      "user_wait": pgu.get("user_wait"), "needs_director": read_client.needs_director(pgu),
                                      "notices": json.loads(u.sql(
                                          "SELECT coalesce(json_agg(json_build_array(target_role, kind, payload->>'kind', message) "
                                          "ORDER BY id), '[]') FROM ticket_board.ticket_notification_queue "
                                          "WHERE ticket_id = 'PGU-222' AND payload->>'kind' = 'invalid_user_wait';"))}
                seen["historical_text"] = read_client.format_ticket(pgu)
                seen["historical_choices"] = u.sql("SELECT ticket_board.unresolved_turn_choices('in_progress', 'ops', 'ops')::text;")
                # Ordinary activity on the ticket leaves the wait standing, as any wait does.
                seen["historical_comment"] = short(u.act("PGU-222", "add_comment", "ops", text="Still waiting, as recorded."))
                seen["historical_after_comment"] = u.ticket("PGU-222").get("awaiting_role")
                # The real listener delivers the Director's notice, once.
                from scripts.ticket_board.notify_listener import TicketBoardNotifyListener
                from scripts.ticket_board.peer_identity import read_process
                sleeper = subprocess.Popen(["sleep", "3600"])
                u.sleepers.append(sleeper)
                u.sql("INSERT INTO ticket_board.role_runtime_assignments (role, runtime, actual_target, worktree, session_dir, "
                      f"process_pid, process_start_time, process_uid) VALUES ('director', 'claude', 'pgu-director:0.0', "
                      f"'/w/director', '/s/director', {sleeper.pid}, {read_process(sleeper.pid).start_time}, {os.getuid()});")
                delivered: list = []
                listener_url = fixture.conninfo(cluster.socket_dir, cluster.port, "upgraded", "ticket_board_listener")
                listener = TicketBoardNotifyListener(conninfo=listener_url, sender=lambda t, m: delivered.append((t, m)),
                                                     activity_gate=lambda _t: False, target_exists=lambda _t: True,
                                                     submission_witness=lambda _t, _s: True, poll_seconds=0, project="pgu")
                import psycopg
                for _ in range(2):
                    with psycopg.connect(listener_url, autocommit=True) as conn:
                        listener.refresh_workflow(conn)
                        listener.process_due_notifications(conn)
                seen["historical_delivered"] = [m for t, m in delivered if t == "pgu-director:0.0" and "PGU-222" in m]
                seen["historical_cleared"] = short(u.act("PGU-222", "clear_awaiting_role", "director"))
                seen["historical_after"] = {"where": u.where("PGU-222"), "user_wait": u.ticket("PGU-222").get("user_wait")}

                # schema.sql and rbac.sql install what the migrations do.
                shape = ("SELECT coalesce(json_agg(json_build_array(p.oid::regprocedure::text, md5(pg_get_functiondef(p.oid)), "
                         "pg_get_userbyid(p.proowner), coalesce(p.proacl::text, '')) ORDER BY p.oid::regprocedure::text), '[]')::text "
                         "FROM pg_proc p WHERE p.pronamespace = 'ticket_board'::regnamespace AND p.proname IN ('set_awaiting_role', "
                         "'unresolved_turn_choices', 'unresolved_turn_repair_options', 'workflow_user_wait_stage', "
                         "'user_wait_refusal', 'user_wait_guidance', 'await_user', 'guard_user_wait', 'user_wait_status', "
                         "'uat_missing_gates', 'require_uat_gates', 'user_wait_pending');")
                seen["migrated_shape"] = json.loads(b.sql(shape))
                seen["upgraded_shape"] = json.loads(u.sql(shape))
                fresh = fixture.conninfo(cluster.socket_dir, cluster.port, "fresh")
                fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "fresh"])
                fixture.psql(fresh, (root / "scripts/ticket_board/schema.sql").read_text())
                fixture.psql(fresh, (root / "scripts/ticket_board/rbac.sql").read_text())
                seen["fresh_shape"] = json.loads(fixture.psql(fresh, shape).strip())
        finally:  # a pane process left running would hold the parent's pipe open
            for each in boards:
                each.close()
    return seen


def card_alerts(tickets: list[dict]) -> list[list[dict]]:
    """The served page's alertStackForTicket, run under node over the tickets the board returned."""
    from scripts.ticket_board.frontend import HTML

    start = HTML.index("function alertStackForTicket(")
    depth, end = 0, None
    for index in range(HTML.index("{", start), len(HTML)):
        depth += {"{": 1, "}": -1}.get(HTML[index], 0)
        if depth == 0:
            end = index + 1
            break
    program = ("const userSignoffSummary = () => ''; const manualBlockedSummary = () => '';\n" + HTML[start:end]
               + f"\nprocess.stdout.write(JSON.stringify({json.dumps(tickets)}.map(alertStackForTicket)));")
    done = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "tests", "examples", "config"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def base_commit() -> str:
    """Main as it stood before this ticket: the commit before the one that added its migration."""
    found = subprocess.run(["git", "-C", str(ROOT), "log", "--format=%H", "--diff-filter=A", "--",
                            "scripts/ticket_board/migrations/pgu1012_syrd662_user_waits.sql"],
                           check=True, capture_output=True, text=True).stdout.split()
    assert found, "pgu1012 is not committed; the run of main needs the commit that added it"
    return found[-1] + "^"


def main() -> int:
    clean_env()
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), "syrd662b-")))
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd662.") as tmp:
        before_root = tree_at(base_commit(), Path(tmp) / "before")
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(before_root)],
                               capture_output=True, text=True, env={**clean_env(), "PYTHONDONTWRITEBYTECODE": "1"})
        assert child.returncode == 0, child.stderr[-3000:]
        before = json.loads(child.stdout.strip().splitlines()[-1])
        after = scenario(ROOT, "syrd662a-", before_root)

    # Main, as SYRD-222 found it.
    check(before["ops_dependency"] == "ok" and before["pgu1_after_main_route"] == "user",
          f"main: Ops' request-dependency on the User from Implementation is accepted: {before['ops_dependency']}")
    check(before["ops_await_role"] == "ok" and before["ops_sql"] == {"ok": True} and before["admin_direct"] == "ok",
          f"main: so is await-role, the database alone, and a direct write: {before['ops_await_role']} {before['ops_sql']}")
    check(before["audit_dependency"] == "ok", f"main: Audit may say an unaudited ticket waits on the User: {before}")
    check(before["forced_unaudited"] == "ok" and before["forced_where"] == ["user_review", "user"]
          and before["edited_unaudited"] == "ok" and before["edited_where"] == ["user_review", "user"],
          f"main: the Director's override and edit put an unaudited ticket in User Review: {before['forced_where']}")
    check(before["forced_after_return"] == "ok" and before["forced_undocumented"] == "ok"
          and before["forced_unpublished"] == "ok",
          f"main: nor does the override wait for a returned ticket's next Audit, the documentation sign-off or "
          f"publication: {before['forced_after_return']} {before['forced_undocumented']} {before['forced_unpublished']}")
    check(before["nouat_dependency"] == "ok", f"main: a workflow with no User Review still records a wait on the User")
    check(before["hold_unaudited"][0] == "ok" and before["hold_undocumented"] == "ok" and before["hold_unpublished"] == "ok"
          and before["hold_refused_where"] == [["user_review", "user"]] * 3,
          f"main: past a Director's hold, the override and the edit skip every gate: {before['hold_unaudited']}")

    # This tree: the SYRD-222 shape, by every route, changes nothing.
    told = ("is in Implementation, and work does not wait on the User from there. Finish Implementation through its "
            "declared transition, with the User's UAT steps on the ticket: it reaches the User in User review once "
            "every gate it owes has passed")
    check("a dependency cannot name the User: PGU-1 " + told in after["ops_dependency"]
          and "operator:<name> blocker" in after["ops_dependency"] and after["pgu1_after_main_route"] == ""
          and told in after["ops_dependency_cli"],
          f"Ops' wait on the User is refused, over HTTP and from the CLI, with what to do instead: {after['ops_dependency']}")
    check(told in after["ops_await_role"] and told in after["ops_sql"].get("refused", ""),
          f"await-role, and the database alone, refuse it the same way: {after['ops_await_role']} {after['ops_sql']}")
    check(after["ops_direct"] == before["ops_direct"] == {"refused": "permission denied for table ticket_notification_state"}
          and "only the Director's await-user records a wait on the User; PGU-1 was not changed" in after["admin_direct"],
          f"the service has no direct write, and the database's owner -- a migration, a hand edit -- is refused one: "
          f"{after['ops_direct']} {after['admin_direct']}")
    check(told in after["director_dependency"]
          and "await-user refused for PGU-1: Implementation is not a stage this workflow lets the Director wait on "
              "the User in" in after["director_await_user"],
          f"the Director cannot either, from Implementation: {after['director_dependency']} {after['director_await_user']}")
    check(after["unchanged"] is True and after["reserved"] == {"ops": "PGU-1", "app": "PGU-6", "main": None},
          f"nothing changed: no wait, assignee, reservation, comment or notification: {after['reserved']}")
    check(after["app_awaits_director"] == "ok", f"an ordinary dependency on a pane role is recorded as before")
    check(after["choices"] == {"in_progress/ops": "{transition,request-dependency}",
                               "analysis/director": "{transition,request-dependency,blocker}",
                               "audit/audit": "{transition,request-dependency}"}
          and after["options"] == "Do one of: transition it if the work is done; or `request-dependency` if it is "
                                  "waiting on another role.",
          f"no prompt offers a wait on the User where none may be recorded: {after['choices']} {after['options']}")
    check(after["submit"] == "ok" and after["at_audit"] == ["audit", "audit"] and after["audited"] == "ok" and after["at_dat"] == ["dat", "director"] and after["steps"] == "ok"
          and after["accepted"] == "ok",
          f"the ticket takes its declared no-code submission, Audit and the Director's acceptance: {after}")
    check(after["user_review"] == {"where": ["user_review", "user"], "awaiting": "", "steps": "Open the Zorin console.",
                                   "reservations": {"app": "PGU-6", "main": None, "ops": None}}
          and after["user_queue"] == ["PGU-1"],
          f"and reaches User Review, with its UAT steps, in the User's queue, Ops free: {after['user_review']}")
    for phase in (before, after):
        check(phase["submit"] == "ok" and phase["user_review"]["where"] == ["user_review", "user"],
              "the declared path itself is unchanged from main")

    # An audited code ticket.
    check("PGU-2 is in Audit, and work does not wait on the User from there. Finish Audit through its declared "
          "transition" in after["audit_dependency"],
          f"Audit cannot say the ticket waits on the User: {after['audit_dependency']}")
    check("PGU-2 cannot enter user_review: it does not have audit_signoff, and it reaches the User only after every "
          "gate it owes" in after["forced_unaudited"]
          and after["forced_where"] == ["audit", "audit"]
          and "it does not have audit_signoff" in after["edited_unaudited"] and after["edited_where"] == ["audit", "audit"],
          f"neither the Director's override nor its edit puts an unaudited ticket in User Review: {after['forced_unaudited']}")
    check(after["audit_signs"] == "ok" and after["to_user"] == "ok" and after["audited_where"] == ["user_review", "user"],
          f"after Audit's sign-off the declared path reaches User Review: {after['audited_where']}")
    check(after["returned"] == "ok" and "it does not have audit_signoff" in after["forced_after_return"]
          and after["returned_where"] == ["in_progress", "app"],
          f"a sign-off follows its commit: returned, the ticket owes Audit again: {after['forced_after_return']}")
    check(after["operator"] == "ok" and after["operator_where"] == ["in_progress", "ops", ""]
          and after["operator_released"] == "ok" and before["operator"] == "ok",
          f"a person outside the board is still an operator blocker, which moves nothing: {after['operator_where']}")

    # No User Review, Documentation, Publisher.
    check("a dependency cannot name the User: PGU-1 is in Implementation, and this workflow has no User Review stage, "
          "so nothing on it waits on the User." in after["nouat_dependency"],
          f"a workflow with no User Review says so: {after['nouat_dependency']}")
    check("it does not have audit_signoff, documentation_signoff, publication" in after["forced_undocumented"]
          and "it does not have publication" in after["forced_unpublished"]
          and after["gated_where"] == [["documentation", "librarian"], ["publication", "publisher"]],
          f"User Review needs the documentation sign-off and the publication: {after['forced_undocumented']} "
          f"{after['forced_unpublished']}")
    check("it does not have publication" in after["forced_other_published"]
          and after["forced_published"] == "ok" and after["published_where"] == ["user_review", "user"],
          f"published, with its sign-offs, the ticket may enter User Review: {after['forced_published']}")

    # A Director's hold before User Review (Audit's topology): the gates before it are still owed, by every way in,
    # and the reviews reached only by parking, before Implementation, back into it or after User Review are not.
    owes = "cannot enter user_review: it does not have {}, and it reaches the User only after every gate it owes"
    check(all(seeded[:2] == ["pre_user_hold", "director"] for seeded in after["hold_seeded"].values())
          and after["hold_seeded"]["PGU-1"][2] == "false|{}"
          and after["hold_seeded"]["PGU-2"][2] == 'true|{"needs_documentation": true}',
          f"the held tickets stand as seeded: {after['hold_seeded']}")
    check(after.get("hold_owed") == {"PGU-1": "{audit_signoff}", "PGU-2": "{documentation_signoff}",
                                     "PGU-3": "{audit_signoff,publication}"} and after["hold_owed_ready"] == "{}",
          f"through the hold, each owes exactly the gates before it: {after.get('hold_owed')} {after['hold_owed_ready']}")
    check(all(refusal.startswith("PGU-1 " + owes.format("audit_signoff")) for refusal in after["hold_unaudited"])
          and len(after["hold_unaudited"]) == 3,
          f"no Audit sign-off: force_move, director_edit and the hold's own release are refused: {after['hold_unaudited']}")
    check(after["hold_undocumented"].startswith("PGU-2 " + owes.format("documentation_signoff"))
          and after["hold_unpublished"].startswith("PGU-3 " + owes.format("audit_signoff, publication"))
          and after["hold_refused_where"] == [["pre_user_hold", "director"]] * 3,
          f"... nor without the documentation sign-off or the publication, and nothing moved: "
          f"{after['hold_undocumented']} {after['hold_unpublished']}")
    check(after["hold_ready"] == ["ok", "ok", "ok"] and after["hold_ready_where"] == [["user_review", "user"]] * 3,
          f"with them, each way in admits it, the triage, rework and post-UAT gates notwithstanding: {after['hold_ready']}")

    # The Director's wait on the User.
    check(before["declared"].startswith("workflow") or "unknown workflow configuration field" in before["declared"],
          f"main has no user_waits: {before['declared']}")
    check(after["declared"] == "ok" and "user cannot call await_user" in after["user_await_user"]
          and "only the Director waits on the User, and user is not the Director" in after["user_await_user_sql"].get("refused", "")
          and "only the Director waits on the User, and ops is not the Director" in after["ops_await_user_sql"].get("refused", "")
          and "await-user needs a reason and the action or decision the User is asked for" in after["director_await_user_blank"],
          f"only the Director waits on the User, with a reason and a request: {after['user_await_user']} "
          f"{after['user_await_user_sql']} {after['ops_await_user_sql']} {after['director_await_user_blank']}")
    wait = after["declared_wait"]
    wait["user_wait"] = wait["user_wait"] or {}  # absent from the JSON: the checks below say so
    check(after["director_await_user_cli"] == "ok" and wait["awaiting"] == "user" and wait["where"] == ["analysis", "director"]
          and wait["user_wait"].get("valid") is True and wait["user_wait"].get("request") == "Choose layout A or B, in a comment."
          and wait["user_wait"].get("requested_by") == "director" and wait["user_wait"].get("problem") is None
          and wait["queued"] == [["director", "transition"]]
          and wait["comment"].startswith("Waiting on the User. Asked of the User: Choose layout A or B, in a comment.")
          and "Waiting on the User:\n  asked: Choose layout A or B, in a comment." in after["declared_text"],
          f"in a stage user_waits lists, the Director records it, with its reason and request; nothing is sent: {wait}")
    check(after["declared_choices"] == "{transition,request-dependency,await-user,blocker}"
          and "`await-user` if only the User can act or decide next" in after["declared_options"],
          f"there, the Director's prompt offers it: {after['declared_choices']} {after['declared_options']}")
    check(told in after["declared_impl_ops"]
          and "await-user refused for PGU-2: Implementation is not a stage" in after["declared_impl_director"],
          f"never from Implementation, whatever user_waits says: {after['declared_impl_ops']} {after['declared_impl_director']}")
    check(after["declared_cleared"] == "ok" and after["declared_after_clear"] is None,
          f"the Director clears it like any wait: {after['declared_after_clear']}")
    check("a wait on the User is the Director's decision, with its reason and what the User is asked to do: "
          "ticket-board-write await-user PGU-1 --reason ... --request ..." in after["declared_director_dependency"],
          f"the Director's own request-dependency on the User is pointed at await-user: {after['declared_director_dependency']}")
    check("a dependency cannot name the User: a wait on the User is the Director's decision" in
          after["forged_setting"].get("refused", "")
          and after["forged_mark"] == {"refused": "permission denied for table ticket_user_wait_requests"},
          f"the service cannot forge await-user's admission: {after['forged_setting']} {after['forged_mark']}")
    withdrawn = after["declared_withdrawn"]["user_wait"] or {}
    check(after["declared_again"] == "ok" and withdrawn["valid"] is False
          and withdrawn["problem"] == "Analysis does not let the Director wait on the User"
          and after["declared_withdrawn"]["where"] == ["analysis", "director"],
          f"when the workflow stops allowing it, the wait standing is reported not valid and nothing moves: {withdrawn}")
    check(all(v.startswith("user_waits") for k, v in after["validate"].items()),
          f"a document may not list Implementation, User Review, a terminal or unowned stage, an undeclared one or a "
          f"repeat: {after['validate']}")
    check(after["sql_ignores"] == {"analysis": "t", "in_progress": "f", "user_review": "f", "done": "f", "backlog": "f"},
          f"and the database ignores such an entry if a stored document had one: {after['sql_ignores']}")

    # A historical wait on the User.
    h = after["historical"]
    h["user_wait"] = h["user_wait"] or {}
    check(after["historical_recorded"] == {"ok": True} and h["where"] == ["in_progress", "ops"] and h["awaiting"] == "user"
          and h["user_wait"].get("valid") is False
          and h["user_wait"].get("problem") == "it was recorded without the Director's await-user, the only way work waits on the User"
          and h["user_wait"].get("repair") == ("the Director clears it (ticket-board-write clear-awaiting-role PGU-222), or ops "
                                           "finishes Implementation through its declared transition and every gate after it")
          and h["needs_director"] is True,
          f"upgraded, a wait main recorded stays where it is, reported not valid, in the Director's queue: {h}")
    check(len(h["notices"]) == 1 and h["notices"][0][:3] == ["director", "ticket_update", "invalid_user_wait"]
          and "which the board no longer admits" in h["notices"][0][3] and "Nothing was moved or cleared" in h["notices"][0][3],
          f"and the Director is told once: {h['notices']}")
    check("Waiting on the User (not valid):" in after["historical_text"]
          and after["historical_choices"] == "{transition,request-dependency}",
          f"the read client says so: {after['historical_text']}")
    check(after["historical_comment"] == "ok" and after["historical_after_comment"] == "user"
          and len(after["historical_delivered"]) == 1 and "which the board no longer admits" in after["historical_delivered"][0],
          f"ordinary activity leaves it standing, and the listener delivers the notice to the Director once: "
          f"{after['historical_comment']} {after['historical_delivered']}")
    check(after["historical_cleared"] == "ok" and after["historical_after"] == {"where": ["in_progress", "ops"], "user_wait": None},
          f"clearing it is the ordinary repair, and moves nothing: {after['historical_after']}")
    alerts = card_alerts([{"id": "PGU-1", "user_wait": wait["user_wait"]}, {"id": "PGU-222", "user_wait": h["user_wait"]},
                          {"id": "PGU-3", "user_wait": None}])
    check(alerts == [[{"kind": "blocked", "title": "Waiting on the User",
                       "text": "The Director asks: Choose layout A or B, in a comment."}],
                     [{"kind": "blocked", "title": "Invalid wait on the User",
                       "text": f"{h['user_wait']['problem']}. Repair: {h['user_wait']['repair']}."}], []],
          f"the board's card shows the Director's request wherever the ticket is, and an invalid wait with its "
          f"repair: {alerts}")
    names = sorted({row[0].split("(")[0] for row in after["migrated_shape"]})
    check(after["migrated_shape"] == after["fresh_shape"] == after["upgraded_shape"] and len(names) == 12,
          f"schema.sql and rbac.sql install what the migrations do, fresh and upgraded from main: {names}")
    print(f"user_wait_gate_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
