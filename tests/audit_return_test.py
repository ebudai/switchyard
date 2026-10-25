#!/usr/bin/env python3
"""SYRD-673: Audit's kick-back returns work to the producer of the revision it audited.

On pie, a design-review ticket reached Audit from the designer's draft, and
`audit_kick_back` -- declared `audit -> in_progress` -- would have handed Audit's
findings to an implementer who never touched it. The Director moved it back to
Draft by override, every round.

The cases run on boards built the production way (companion roles, schema.sql,
the real migrate runner, rbac.sql): the shipped inspection workflow with an
active designer owning Draft, the Librarian's documentation gate and a designer's
`submit_design` into Audit. Every write goes through the board's HTTP API as that role, or the real
`ticket-board-write`, or, where the database alone is the question, the board
service's own connection. The real notify listener delivers.

- **Implementation to Audit:** the kick-back returns to the implementer that
  submitted, clears exactly the sign-offs main clears, and tells only them.
- **Draft to Audit:** by the designer's own submission, and by release into
  triage and the Director's override: the designer gets it back, every round.
- **Documentation to Audit:** with no change, the implementer; with the
  Librarian's own commit, the Librarian, keeping that commit.
- **User Review to Audit:** the Director's override back with the candidate
  unchanged returns it to its author.
- **A revision nobody known produced, a retired or changed owner, a ticket in
  Audit from before the upgrade:** refused, moving nothing, with the Director's
  repair; `set-audit-return` records the return and Audit kicks back as usual.

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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKS = 0
A, B, C = "a" * 40, "b" * 40, "c" * 40
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
SIGNOFFS = ("inspector_signoff", "audit_signoff", "user_signoff")


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


def design_loop(root: Path) -> dict:
    """pie's shape: a designer owns Draft and may release it or submit it to Audit; the Librarian's documentation
    gate sits between Implementation and Audit."""
    from scripts.ticket_board.documentation_gate import with_documentation_gate

    cfg = with_documentation_gate(example(root), librarian={"runtime": "claude", "template_role": "main"})
    for role in cfg["roles"]:
        if role["name"] == "designer":
            role["active"] = True
    for stage in cfg["stages"]:
        if stage["name"] == "draft":
            stage["owners"] = ["designer"]
    cfg["reassign"] = {}
    route = next(t for t in cfg["transitions"] if t["action"] == "route" and t["from"] == "analysis")
    for tr in cfg["transitions"]:
        if tr["action"] == "release_draft":
            tr["actors"] = ["designer", "director"]
    cfg["transitions"] += [
        {**copy.deepcopy(route), "from": "draft", "to": "audit", "action": "submit_design", "label": "Submit design",
         "actors": ["designer"], "owner_scoped": True},
    ]
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

    def short(answer: dict) -> str:
        """"ok", or the refusal's own message: post_json reports (path, status, body)."""
        if "ok" in answer:
            return "ok"
        text = answer.get("refused", "")
        try:
            text = str(ast.literal_eval(text)[2])
        except (ValueError, SyntaxError, IndexError, TypeError):
            pass
        return " ".join(text.split("\nCONTEXT:")[0].split())[:700]

    seen: dict = {}
    with temporary_cluster(prefix=prefix, shutdown="immediate") as cluster:
        boards: list = []

        def board(name: str, document: dict):
            b = Board(cluster, name, document)
            b.app._resolve_known_commit = lambda value: (value.lower(), [])  # this board's copy knows every commit
            boards.append(b)
            return b

        def cli(b, role: str, *args: str) -> str:
            env = {"PATH": "/usr/bin:/bin", "HOME": str(cluster.root), "LANG": "C.UTF-8", "TICKET_BOARD_URL": b.base,
                   "TICKET_BOARD_WRITE_TOKEN": b.server.write_token, "TICKET_BOARD_CALLER_ROLE": role}
            done = subprocess.run([sys.executable, str(root / "scripts" / "ticket-board-write"), *args], env=env,
                                  cwd=str(cluster.root), capture_output=True, text=True, timeout=120, check=False)
            return "ok" if done.returncode == 0 else " ".join((done.stdout + done.stderr).split())[-900:]

        def state(b, ticket: str) -> dict:
            """Where it is, what it carries, and what a refused write must leave alone."""
            row = json.loads(b.sql(
                "SELECT json_build_object('where', state || '/' || assignee, 'commit', commit_hash, 'exempt', commit_exempt, "
                "'flags', json_build_object('inspector_signoff', inspector_signoff, 'audit_signoff', audit_signoff, "
                "'user_signoff', user_signoff, 'documentation_signoff', coalesce((workflow_flags->>'documentation_signoff')::boolean, false)), "
                "'comments', (SELECT count(*) FROM ticket_board.ticket_comments c WHERE c.ticket_id = t.id), "
                "'queued', (SELECT count(*) FROM ticket_board.ticket_notification_queue q WHERE q.ticket_id = t.id))::text "
                f"FROM ticket_board.tickets t WHERE id = '{ticket}';"))
            return row

        def returns(b, ticket: str) -> dict | None:
            return b.ticket(ticket).get("audit_return")

        def kick_action(b, ticket: str) -> dict:
            return next((a for a in b.ticket(ticket).get("workflow_actions") or [] if a["action"] == "audit_kick_back"), {})

        def finished(b, ticket: str) -> None:
            """The case is over: the ticket stops holding its implementer's one slot."""
            answer = short(b.act(ticket, "defer", "director"))
            assert answer == "ok", (ticket, answer, state(b, ticket))

        def delivered(b) -> list:
            b.busy.clear()  # every pane has finished its turn
            # Who is told, by the notices themselves: a pane moving to another ticket's conversation is cleared first.
            return sorted(target for target, message in b.one_pass() if message != "/clear")

        try:
            b = board("board", design_loop(root))
            delivered(b)

            # --- Implementation to Audit: the implementer that submitted, and only the sign-offs main clears. ---
            fixture.seed_postgres_ticket(b.admin, "PGU-1", title="Implemented", state="in_progress", assignee="app",
                                         commit_hash=A, inspector_signoff=True, user_signoff=True)
            seen["impl_submit"] = short(b.act("PGU-1", "submit_to_audit", "app", commit_hash=A))
            seen["impl_at_audit"] = state(b, "PGU-1")
            seen["impl_status"] = returns(b, "PGU-1")
            seen["impl_action"] = kick_action(b, "PGU-1")
            seen["impl_card"] = read_client.format_ticket(b.ticket("PGU-1"))
            delivered(b)
            seen["impl_before_wrong"] = state(b, "PGU-1")
            seen["impl_wrong_target"] = cli(b, "audit", "audit-kick-back", "PGU-1", "--reason", "Fix the parser.",
                                            "--target-assignee", "main")
            seen["impl_after_wrong"] = state(b, "PGU-1")
            seen["impl_kick"] = cli(b, "audit", "audit-kick-back", "PGU-1", "--reason", "Fix the parser.",
                                    "--target-assignee", "app")
            seen["impl_returned"] = state(b, "PGU-1")
            seen["impl_told"] = delivered(b)
            seen["impl_reserved"] = b.reservations()
            finished(b, "PGU-1")

            # --- Draft to Audit, by the designer's own submission, round after round. ---
            fixture.seed_postgres_ticket(b.admin, "PGU-2", title="Design set", state="draft", assignee="designer")
            rounds = []
            for n in (1, 2):
                rounds.append({"submit": short(b.act("PGU-2", "submit_design", "designer")),
                               "status": returns(b, "PGU-2"), "told_audit": delivered(b),
                               "kick": short(b.act("PGU-2", "audit_kick_back", "audit", reason="Round 1: fix B1.")) if n == 1
                               else cli(b, "audit", "audit-kick-back", "PGU-2", "--reason", "Round 2: fix S3.",
                                        "--target-assignee", "designer"),
                               "returned": state(b, "PGU-2"), "told": delivered(b)})
            seen["design_rounds"] = rounds
            finished(b, "PGU-2")
            # ... and by release into triage and the Director's override into Audit, as pie does today.
            fixture.seed_postgres_ticket(b.admin, "PGU-12", title="Design set, released", state="draft", assignee="designer")
            seen["design_release"] = short(b.act("PGU-12", "release_draft", "designer"))
            delivered(b)
            seen["design_override"] = short(b.act("PGU-12", "force_move", "director", state="audit", assignee="audit"))
            seen["design_override_status"] = returns(b, "PGU-12")
            delivered(b)
            seen["design_override_kick"] = short(b.act("PGU-12", "audit_kick_back", "audit", reason="Fix S4."))
            seen["design_override_returned"] = state(b, "PGU-12")
            seen["design_override_told"] = delivered(b)
            finished(b, "PGU-12")

            # --- Documentation to Audit: no change keeps the implementer; the Librarian's commit is the Librarian's. ---
            fixture.seed_postgres_ticket(b.admin, "PGU-3", title="Documented", state="in_progress", assignee="ops")
            b.act("PGU-3", "set_workflow_flags", "director", needs_documentation=True)
            seen["doc_submit"] = [short(b.act("PGU-3", "submit_to_audit", "ops", commit_hash=A)), state(b, "PGU-3")["where"]]
            seen["doc_no_change"] = short(b.act("PGU-3", "documentation_no_change", "librarian",
                                                reason="The operator guide already says this."))
            seen["doc_no_change_status"] = returns(b, "PGU-3")
            delivered(b)
            seen["doc_no_change_kick"] = short(b.act("PGU-3", "audit_kick_back", "audit", reason="Handle the empty file."))
            seen["doc_no_change_returned"] = state(b, "PGU-3")
            seen["doc_no_change_told"] = delivered(b)
            finished(b, "PGU-3")
            fixture.seed_postgres_ticket(b.admin, "PGU-4", title="Documented", state="in_progress", assignee="ops")
            b.act("PGU-4", "set_workflow_flags", "director", needs_documentation=True)
            seen["doc_signed_submit"] = short(b.act("PGU-4", "submit_to_audit", "ops", commit_hash=A))
            # The Librarian's sign-off with its own commit, as the database takes it (the git checks before it are
            # SYRD-624's, and pass only for a commit published at its candidate ref).
            seen["doc_signed"] = b.as_role_sql("librarian", "SELECT ticket_board.perform_workflow_action('PGU-4', "
                                                            f"'documentation_sign_off', '{{\"commit_hash\": \"{B}\"}}');")
            seen["doc_signed_at"] = state(b, "PGU-4")
            seen["doc_signed_status"] = returns(b, "PGU-4")
            delivered(b)
            # The Director turns the gate off meanwhile: the return is to the producer's stage all the same.
            seen["doc_gate_off"] = short(b.act("PGU-4", "set_workflow_flags", "director", needs_documentation=False))
            seen["doc_signed_kick"] = short(b.act("PGU-4", "audit_kick_back", "audit", reason="The guide's example is wrong."))
            seen["doc_signed_returned"] = state(b, "PGU-4")
            seen["doc_signed_told"] = delivered(b)
            finished(b, "PGU-4")

            # --- User Review to Audit, the candidate unchanged: its author. ---
            fixture.seed_postgres_ticket(b.admin, "PGU-5", title="Accepted, then doubted", state="in_progress",
                                         assignee="main", needs_user_signoff=True)
            walk = [short(b.act("PGU-5", "submit_to_audit", "main", commit_hash=C)),
                    short(b.act("PGU-5", "audit_sign_off", "audit", text="Reviewed.")),
                    short(b.act("PGU-5", "set_uat_instructions", "director", do_this="Run it.", you_should_see="It works.",
                                then_choose="")),
                    short(b.act("PGU-5", "director_dat_sign_off", "director", text="Ready.")),
                    state(b, "PGU-5")["where"],
                    short(b.act("PGU-5", "force_move", "director", state="audit", assignee="audit"))]
            seen["uat_walk"] = walk
            seen["uat_status"] = returns(b, "PGU-5")
            delivered(b)
            seen["uat_kick"] = short(b.act("PGU-5", "audit_kick_back", "audit", reason="The report is right."))
            seen["uat_returned"] = state(b, "PGU-5")
            seen["uat_told"] = delivered(b)
            finished(b, "PGU-5")

            # --- A revision nobody known produced: refused, nothing moves; the Director's record, then as usual. ---
            fixture.seed_postgres_ticket(b.admin, "PGU-6", title="Edited in Audit", state="in_progress", assignee="app")
            seen["edited_submit"] = short(b.act("PGU-6", "submit_to_audit", "app", commit_hash=A))
            seen["edited"] = short(b.act("PGU-6", "director_edit", "director", reason="nothing to build after all",
                                         patch={"commit_exempt": True}))
            seen["edited_status"] = returns(b, "PGU-6")
            delivered(b)
            before = state(b, "PGU-6")
            seen["edited_kick"] = short(b.act("PGU-6", "audit_kick_back", "audit", reason="Wrong."))
            seen["edited_unchanged"] = state(b, "PGU-6") == before
            seen["edited_needs_director"] = read_client.needs_director(b.ticket("PGU-6"))
            seen["edited_card"] = read_client.format_ticket(b.ticket("PGU-6"))
            seen["edited_action"] = kick_action(b, "PGU-6")
            # Who may record it, and what.
            seen["set_by_audit"] = short(b.act("PGU-6", "set_audit_return", "audit", stage="in_progress", assignee="app",
                                               reason="r"))
            seen["set_by_app_sql"] = b.as_role_sql("app", "SELECT ticket_board.set_audit_return('PGU-6', 'in_progress', "
                                                          "'app', 'r');")
            seen["set_refusals"] = {f"{stage}/{owner}/{reason}": short(b.act("PGU-6", "set_audit_return", "director",
                                                                              stage=stage, assignee=owner, reason=reason))
                                    for stage, owner, reason in (("audit", "audit", "r"), ("backlog", "director", "r"),
                                                                 ("done", "director", "r"), ("nowhere", "app", "r"),
                                                                 ("draft", "main", "r"), ("in_progress", "perf", "r"),
                                                                 ("in_progress", "app", " "))}
            seen["set_refused_unchanged"] = state(b, "PGU-6") == before and returns(b, "PGU-6") == seen["edited_status"]
            seen["set_elsewhere"] = short(b.act("PGU-5", "set_audit_return", "director", stage="in_progress",
                                                assignee="app", reason="r"))
            seen["set_cli"] = cli(b, "director", "set-audit-return", "PGU-6", "--stage", "in_progress", "--assignee", "ops",
                                  "--reason", "Ops takes the rework; the exemption was my mistake.")
            seen["set_status"] = returns(b, "PGU-6")
            seen["set_moved_nothing"] = state(b, "PGU-6")["where"]
            seen["set_kick"] = short(b.act("PGU-6", "audit_kick_back", "audit", reason="Wrong."))
            seen["set_returned"] = state(b, "PGU-6")
            finished(b, "PGU-6")

            # A revision cleared by an edit is no longer the one its producer made.
            fixture.seed_postgres_ticket(b.admin, "PGU-13", title="No code, then code after all", state="in_progress",
                                         assignee="app")
            seen["cleared_submit"] = short(b.act("PGU-13", "submit_to_audit_without_commit", "app", reason="Config only."))
            seen["cleared_edit"] = short(b.act("PGU-13", "director_edit", "director", reason="it needs code after all",
                                               patch={"commit_exempt": False}))
            seen["cleared_status"] = returns(b, "PGU-13")
            seen["cleared_kick"] = short(b.act("PGU-13", "audit_kick_back", "audit", reason="Needs code."))
            seen["cleared_where"] = state(b, "PGU-13")["where"]
            finished(b, "PGU-13")

            # One Director edit that both makes a new revision and moves the ticket out of Implementation into
            # Audit: the revision is the Director's, whoever held the stage it left (Audit's SYRD-673 kick-back).
            fixture.seed_postgres_ticket(b.admin, "PGU-14", title="Edited into Audit", state="in_progress", assignee="app")
            seen["edited_in"] = short(b.act("PGU-14", "director_edit", "director", reason="config only, straight to Audit",
                                            patch={"state": "audit", "assignee": "audit", "commit_exempt": True}))
            seen["edited_in_status"] = returns(b, "PGU-14")
            delivered(b)
            before = state(b, "PGU-14")
            seen["edited_in_kick"] = short(b.act("PGU-14", "audit_kick_back", "audit", reason="Needs code."))
            seen["edited_in_after"] = state(b, "PGU-14")
            seen["edited_in_unchanged"] = seen["edited_in_after"] == before
            finished(b, "PGU-14")

            # --- Other returns are unchanged: the Director's acceptance kick-back goes to the recorded implementer. ---
            fixture.seed_postgres_ticket(b.admin, "PGU-7", title="Accepted", state="in_progress", assignee="ops",
                                         needs_user_signoff=True)
            seen["dat_kick"] = [short(b.act("PGU-7", "submit_to_audit", "ops", commit_hash=A)),
                                short(b.act("PGU-7", "audit_sign_off", "audit", text="Reviewed.")),
                                short(b.act("PGU-7", "director_dat_kick_back", "director", reason="Not this way.")),
                                state(b, "PGU-7")["where"]]
            finished(b, "PGU-7")

            # --- The workflow changes under a ticket in Audit: its producer is retired, or no longer an owner. ---
            for ticket, stage, owner, action in (("PGU-8", "draft", "designer", "submit_design"),
                                                 ("PGU-9", "in_progress", "app", "submit_to_audit")):
                fixture.seed_postgres_ticket(b.admin, ticket, title="Changed under it", state=stage, assignee=owner)
                seen.setdefault("changed_submit", []).append(
                    short(b.act(ticket, action, owner, **({"commit_hash": A} if stage == "in_progress" else {}))))
            changed = design_loop(root)
            changed["roles"] = [{**r, "active": False} if r["name"] == "designer" else r for r in changed["roles"]]
            changed["transitions"] = [t for t in changed["transitions"] if t["action"] != "submit_design"]
            for tr in changed["transitions"]:
                if tr["action"] == "release_draft":
                    tr["actors"] = ["director"]
            for stage in changed["stages"]:
                if stage["name"] == "draft":
                    stage["owners"] = ["director"]
                if stage["name"] == "in_progress":
                    stage["owners"] = ["main", "ops"]
            revision = int(b.sql("SELECT revision FROM ticket_board.workflow_configuration WHERE singleton;"))
            b.app.apply_workflow(changed, expected_revision=revision, dry_run=False, caller_role="director")
            delivered(b)
            seen["changed"] = {}
            for ticket in ("PGU-8", "PGU-9"):
                before = state(b, ticket)
                seen["changed"][ticket] = {"status": returns(b, ticket),
                                           "kick": short(b.act(ticket, "audit_kick_back", "audit", reason="Redo it.")),
                                           "unchanged": state(b, ticket) == before}
            seen["changed_retired"] = short(b.act("PGU-8", "set_audit_return", "director", stage="draft",
                                                  assignee="designer", reason="r"))
            seen["changed_set"] = short(b.act("PGU-9", "set_audit_return", "director", stage="in_progress", assignee="main",
                                              reason="App left the project; main owns the parser now."))
            seen["changed_kick"] = [short(b.act("PGU-9", "audit_kick_back", "audit", reason="Redo it.")),
                                    state(b, "PGU-9")["where"]]

            # --- A ticket in Audit when the board is upgraded from main: no record, the Director told once. ---
            if before_root is not None:
                u_admin = build_board(cluster, "upgraded", before=before_root)
                u = Board.__new__(Board)
                u.admin = u_admin
                u.service = fixture.conninfo(cluster.socket_dir, cluster.port, "upgraded", fixture.SERVICE_ROLE)
                u.app = fixture.TicketBoardApp(cluster.root / "u-frames", cluster.root / "u-assets", project="pgu",
                                               ticket_prefix="PGU", database_url=u.service)
                u.app.apply_workflow(example(root), expected_revision=0, dry_run=False, caller_role="director")
                fixture.seed_postgres_ticket(u_admin, "PGU-10", title="In Audit at the upgrade", state="audit",
                                             assignee="audit", commit_hash=A)
                fixture.seed_postgres_ticket(u_admin, "PGU-11", title="Elsewhere at the upgrade", state="in_progress",
                                             assignee="main")
                migrate(u_admin)
                u.server = fixture.TicketBoardServer(("127.0.0.1", 0), u.app, director_notifier=fixture.QuietNotifier())
                threading.Thread(target=u.server.serve_forever, daemon=True).start()
                u.base = f"http://127.0.0.1:{u.server.server_port}"
                u.sleepers = []
                boards.append(u)
                pgu = u.ticket("PGU-10")
                seen["upgraded"] = {"where": u.where("PGU-10"), "status": pgu.get("audit_return"),
                                    "needs_director": read_client.needs_director(pgu),
                                    "notices": json.loads(u.sql(
                                        "SELECT coalesce(json_agg(json_build_array(ticket_id, target_role, kind, "
                                        "payload->>'kind', message) ORDER BY id), '[]') FROM ticket_board.ticket_notification_queue "
                                        "WHERE payload->>'kind' = 'audit_return_missing';"))}
                seen["upgraded_text"] = read_client.format_ticket(pgu)
                from scripts.ticket_board.notify_listener import TicketBoardNotifyListener
                from scripts.ticket_board.peer_identity import read_process
                sleeper = subprocess.Popen(["sleep", "3600"])
                u.sleepers.append(sleeper)
                u.sql("INSERT INTO ticket_board.role_runtime_assignments (role, runtime, actual_target, worktree, session_dir, "
                      f"process_pid, process_start_time, process_uid) VALUES ('director', 'claude', 'pgu-director:0.0', "
                      f"'/w/director', '/s/director', {sleeper.pid}, {read_process(sleeper.pid).start_time}, {os.getuid()});")
                sent: list = []
                listener_url = fixture.conninfo(cluster.socket_dir, cluster.port, "upgraded", "ticket_board_listener")
                listener = TicketBoardNotifyListener(conninfo=listener_url, sender=lambda t, m: sent.append((t, m)),
                                                     activity_gate=lambda _t: False, target_exists=lambda _t: True,
                                                     submission_witness=lambda _t, _s: True, poll_seconds=0, project="pgu")
                import psycopg
                for _ in range(2):
                    with psycopg.connect(listener_url, autocommit=True) as conn:
                        listener.refresh_workflow(conn)
                        listener.process_due_notifications(conn)
                seen["upgraded_delivered"] = [m for t, m in sent if t == "pgu-director:0.0" and "PGU-10" in m]
                seen["upgraded_kick"] = short(u.act("PGU-10", "audit_kick_back", "audit", reason="Fix it."))
                seen["upgraded_where"] = u.where("PGU-10")
                seen["upgraded_set"] = short(u.act("PGU-10", "set_audit_return", "director", stage="in_progress",
                                                   assignee="main", reason="Main submitted it before the upgrade."))
                seen["upgraded_after"] = [short(u.act("PGU-10", "audit_kick_back", "audit", reason="Fix it.")),
                                          u.where("PGU-10")]

                # schema.sql and rbac.sql install what the migrations do.
                shape = ("SELECT coalesce(json_agg(json_build_array(p.oid::regprocedure::text, md5(pg_get_functiondef(p.oid)), "
                         "pg_get_userbyid(p.proowner), coalesce(p.proacl::text, '')) ORDER BY p.oid::regprocedure::text), '[]')::text "
                         "FROM pg_proc p WHERE p.pronamespace = 'ticket_board'::regnamespace AND p.proname IN ("
                         "'enforce_declared_ticket_update', 'audit_return_transition', 'audit_return_stage', 'producing_holder', "
                         "'audit_return_problem', 'record_audit_return', 'audit_return_status', 'set_audit_return');")
                table = ("SELECT json_build_array((SELECT json_agg(attname::text || ':' || format_type(atttypid, atttypmod) "
                         "ORDER BY attnum) FROM pg_attribute WHERE attrelid = 'ticket_board.ticket_audit_returns'::regclass "
                         "AND attnum > 0), (SELECT relacl::text FROM pg_class WHERE oid = 'ticket_board.ticket_audit_returns'::regclass), "
                         "(SELECT tgenabled::text FROM pg_trigger WHERE tgname = 'tickets_z_audit_return'))::text;")
                seen["migrated_shape"] = [json.loads(b.sql(shape)), b.sql(table)]
                seen["upgraded_shape"] = [json.loads(u.sql(shape)), u.sql(table)]
                fresh = fixture.conninfo(cluster.socket_dir, cluster.port, "fresh")
                fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "fresh"])
                fixture.psql(fresh, (root / "scripts/ticket_board/schema.sql").read_text())
                fixture.psql(fresh, (root / "scripts/ticket_board/rbac.sql").read_text())
                seen["fresh_shape"] = [json.loads(fixture.psql(fresh, shape).strip()), fixture.psql(fresh, table).strip()]
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
                            "scripts/ticket_board/migrations/pgu1013_syrd673_audit_return.sql"],
                           check=True, capture_output=True, text=True).stdout.split()
    assert found, "pgu1013 is not committed; the run of main needs the commit that added it"
    return found[-1] + "^"


def main() -> int:
    clean_env()
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), "syrd673b-")))
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd673.") as tmp:
        before_root = tree_at(base_commit(), Path(tmp) / "before")
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(before_root)],
                               capture_output=True, text=True, env={**clean_env(), "PYTHONDONTWRITEBYTECODE": "1"})
        assert child.returncode == 0, child.stderr[-3000:]
        before = json.loads(child.stdout.strip().splitlines()[-1])
        after = scenario(ROOT, "syrd673a-", before_root)

    # Main, as pie found it: the designer's work goes to an implementer who never touched it.
    done = before["design_rounds"][0]
    check(done["submit"] == "ok" and done["kick"] == "ok" and done["returned"]["where"] == "in_progress/main"
          and done["told"] == ["pgu-main:0.0"],
          f"main: the design loop hands Audit's findings to main, who never touched it, not the designer: {done}")
    check(before["design_override"] == before["design_override_kick"] == "ok"
          and before["design_override_returned"]["where"] == "in_progress/main",
          f"main: so does the override pie uses: {before['design_override_returned']}")
    check(before["doc_signed_returned"]["where"] == "in_progress/ops"
          and before["doc_signed_returned"]["commit"] == "",
          f"main: the Librarian's own commit goes to the implementer, and is dropped: {before['doc_signed_returned']}")
    check(before["changed"]["PGU-8"]["kick"] == "ok" and before["edited_kick"] == "ok",
          f"main: a kick-back is never refused for want of a producer: {before['changed']} {before['edited_kick']}")

    for side, run in (("main", before), ("this tree", after)):
        check(run["doc_signed_submit"] == run["edited_submit"] == "ok" and run["changed_submit"] == ["ok", "ok"]
              and run["dat_kick"][:3] == ["ok", "ok", "ok"],
              f"{side}: every submission the cases build on is taken: {run['doc_signed_submit']} {run['edited_submit']} "
              f"{run['changed_submit']} {run['dat_kick']}")

    # Implementation to Audit.
    status = after["impl_status"] or {}
    check(after["impl_submit"] == "ok" and after["impl_at_audit"]["where"] == "audit/audit"
          and status.get("valid") is True and status.get("stage") == "in_progress" and status.get("assignee") == "app"
          and status.get("action") == "submit_to_audit" and status.get("commit") == A
          and status.get("entered_from") == {"stage": "in_progress", "assignee": "app", "action": "submit_to_audit",
                                             "at": (status.get("entered_from") or {}).get("at"), "commit": A},
          f"an implementer's submission records them as the producer of the revision, and how it entered Audit: {status}")
    action = after["impl_action"]
    check(action.get("to") == "in_progress" and action.get("returns_to") == {"stage": "in_progress", "assignee": "app"}
          and "Kick-back returns to:\n  in_progress/app (by submit_to_audit)" in after["impl_card"],
          f"the ticket offers the same declared action, saying where it returns this ticket: {action}")
    check("audit_kick_back returns PGU-1 to the producer of its revision, app; it cannot send it to main"
          in after["impl_wrong_target"] and after["impl_after_wrong"] == after["impl_before_wrong"],
          f"a --target-assignee naming anyone else is refused before anything changes: {after['impl_wrong_target']}")
    returned = after["impl_returned"]
    check(after["impl_kick"] == "ok" and returned["where"] == "in_progress/app" and returned["commit"] == ""
          and returned["flags"] == before["impl_returned"]["flags"]
          == {"inspector_signoff": True, "audit_signoff": False, "user_signoff": False, "documentation_signoff": False},
          f"it returns to app, clearing exactly what main clears: {returned} vs main {before['impl_returned']}")
    check(after["impl_told"] == ["pgu-app:0.0"] and after["impl_reserved"].get("app") == "PGU-1",
          f"only app is told, and the ticket holds app's slot: {after['impl_told']} {after['impl_reserved']}")

    # Draft to Audit.
    for n, done in enumerate(after["design_rounds"], 1):
        status = done["status"] or {}
        check(done["submit"] == "ok" and status.get("valid") is True
              and (status.get("stage"), status.get("assignee"), status.get("action")) == ("draft", "designer", "submit_design")
              and done["told_audit"] == ["pgu-audit:0.0"] and done["kick"] == "ok"
              and done["returned"]["where"] == "draft/designer" and done["told"] == ["pgu-designer:0.0"],
              f"round {n}: the designer's submission comes back to the designer, and only the designer is told: {done}")
    status = after["design_override_status"] or {}
    check(after["design_release"] == "ok" and after["design_override"] == "ok"
          and (status.get("stage"), status.get("assignee"), status.get("action")) == ("draft", "designer", "release_draft")
          and (status.get("entered_from") or {}).get("stage") == "analysis"
          and (status.get("entered_from") or {}).get("action") == "force_move"
          and after["design_override_kick"] == "ok" and after["design_override_returned"]["where"] == "draft/designer"
          and after["design_override_told"] == ["pgu-designer:0.0"],
          f"released into triage and moved into Audit by the Director, the unchanged draft is still the designer's: "
          f"{status} {after['design_override_returned']}")

    # Documentation to Audit.
    status = after["doc_no_change_status"] or {}
    check(after["doc_submit"] == ["ok", "documentation/librarian"] and after["doc_no_change"] == "ok"
          and (status.get("stage"), status.get("assignee")) == ("in_progress", "ops")
          and (status.get("entered_from") or {}).get("action") == "documentation_no_change"
          and after["doc_no_change_kick"] == "ok" and after["doc_no_change_returned"]["where"] == "in_progress/ops"
          and after["doc_no_change_told"] == ["pgu-ops:0.0"],
          f"through the Librarian with no change, the implementer's revision returns to the implementer: {status}")
    status = after["doc_signed_status"] or {}
    check(after["doc_signed"] == {"ok": True} and after["doc_signed_at"]["commit"] == B
          and (status.get("stage"), status.get("assignee"), status.get("commit")) == ("documentation", "librarian", B),
          f"the Librarian's own commit makes the Librarian its producer: {status}")
    returned = after["doc_signed_returned"]
    check(after["doc_gate_off"] == "ok" and after["doc_signed_kick"] == "ok"
          and returned["where"] == "documentation/librarian" and returned["commit"] == B
          and returned["flags"]["documentation_signoff"] is False and returned["flags"]["audit_signoff"] is False
          and after["doc_signed_told"] == ["pgu-librarian:0.0"],
          f"and gets it back with that commit to review on, its sign-off owed again: {returned} {after['doc_signed_told']}")

    # User Review to Audit.
    status = after["uat_status"] or {}
    check(after["uat_walk"] == ["ok", "ok", "ok", "ok", "user_review/user", "ok"]
          and (status.get("stage"), status.get("assignee"), status.get("commit")) == ("in_progress", "main", C)
          and (status.get("entered_from") or {}).get("stage") == "user_review"
          and after["uat_kick"] == "ok" and after["uat_returned"]["where"] == "in_progress/main"
          and after["uat_told"] == ["pgu-main:0.0"],
          f"sent back from User Review with the candidate unchanged, it returns to its author: {status} {after['uat_walk']}")

    # A revision nobody known produced.
    status = after["edited_status"] or {}
    told = "Ask the Director (ticket-board-write request-dependency PGU-6 --role director)"
    check(after["edited"] == "ok" and status.get("valid") is False
          and status.get("problem") == f"its revision ({A}) was made by director_edit, not by the holder of a stage that produces work"
          and status.get("repair", "").startswith("the Director records where it returns (ticket-board-write set-audit-return PGU-6"),
          f"a revision the Director's edit made has no producer: {status}")
    check(after["edited_kick"].startswith(f"PGU-6 cannot be kicked back: its revision ({A}) was made by director_edit")
          and "Nothing was moved." in after["edited_kick"] and told in after["edited_kick"]
          and after["edited_unchanged"] is True,
          f"Audit's kick-back is refused, naming the repair, and nothing changes: {after['edited_kick']}")
    check(after["edited_needs_director"] is True and "Kick-back refused:\n  problem: its revision" in after["edited_card"]
          and after["edited_action"].get("returns_to", "absent") is None
          and after["edited_action"].get("refused", "").startswith("its revision"),
          f"the Director's queue lists it, and the ticket says why: {after['edited_action']}")
    check("set_audit_return" in after["set_by_audit"]
          and "only the control role may record where a kick-back returns a ticket, not app"
          in after["set_by_app_sql"].get("refused", ""),
          f"only the Director records a return: {after['set_by_audit']} {after['set_by_app_sql']}")
    refusals = after["set_refusals"]
    check(all(v.startswith("set-audit-return refused for PGU-6: ") for k, v in refusals.items() if not k.endswith("/ "))
          and "not a stage work is returned to" in refusals["audit/audit/r"]
          and "not a stage work is returned to" in refusals["backlog/director/r"]
          and "not a stage work is returned to" in refusals["done/director/r"]
          and "does not declare a stage nowhere" in refusals["nowhere/app/r"]
          and "main does not own draft (owners: designer)" in refusals["draft/main/r"]
          and "perf is not an active role" in refusals["in_progress/perf/r"]
          and "requires a reason" in refusals["in_progress/app/ "]
          and after["set_refused_unchanged"] is True
          and "which Audit's kick-back does not leave" in after["set_elsewhere"],
          f"nor anywhere the workflow does not admit: {refusals} {after['set_elsewhere']}")
    status = after["set_status"] or {}
    check(after["set_cli"] == "ok" and status.get("valid") is True
          and (status.get("stage"), status.get("assignee"), status.get("recorded_by"), status.get("action"))
          == ("in_progress", "ops", "director", "set_audit_return")
          and after["set_moved_nothing"] == "audit/audit"
          and after["set_kick"] == "ok" and after["set_returned"]["where"] == "in_progress/ops",
          f"the Director's record moves nothing; Audit then kicks back as usual, to the owner the Director named "
          f"rather than the recorded implementer: {status} {after['set_returned']}")
    status = after["cleared_status"] or {}
    check(after["cleared_submit"] == after["cleared_edit"] == "ok"
          and status.get("problem") == "the board has no record of who produced the revision it carries (none)"
          and after["cleared_kick"].startswith("PGU-13 cannot be kicked back") and after["cleared_where"] == "audit/audit",
          f"a revision an edit cleared is not the one its producer made: {status} {after['cleared_kick']}")
    status = after["edited_in_status"] or {}
    check(before["edited_in"] == "ok" and before["edited_in_kick"] == "ok"
          and before["edited_in_after"]["where"] == "in_progress/app",
          f"main: a Director's edit into Audit is kicked back to the implementer who held it: {before['edited_in_after']}")
    check(after["edited_in"] == "ok" and status.get("valid") is False
          and status.get("problem") == "its revision (no code) was made by director_edit, not by the holder of a stage "
                                       "that produces work"
          and status.get("entered_from") == {"stage": "in_progress", "assignee": "app", "action": "director_edit",
                                             "at": (status.get("entered_from") or {}).get("at"), "commit": ""}
          and after["edited_in_kick"].startswith("PGU-14 cannot be kicked back: its revision (no code) was made by director_edit")
          and after["edited_in_unchanged"] is True and after["edited_in_after"]["where"] == "audit/audit",
          f"one Director edit that changes the revision and moves it into Audit leaves no producer, even leaving app's "
          f"stage; the Audit entry is kept as evidence, and the kick-back is refused, moving nothing: {status} "
          f"{after['edited_in_kick']}")
    check(after["dat_kick"] == before["dat_kick"] == ["ok", "ok", "ok", "in_progress/ops"],
          f"the other returns are unchanged: {after['dat_kick']} {before['dat_kick']}")

    # The workflow changes under it.
    retired, moved_away = after["changed"]["PGU-8"], after["changed"]["PGU-9"]
    check((retired["status"] or {}).get("problem") == "designer is not an active role in the workflow"
          and retired["kick"].startswith("PGU-8 cannot be kicked back: designer is not an active role")
          and retired["unchanged"] is True
          and (moved_away["status"] or {}).get("problem") == "app does not own in_progress (owners: main, ops)"
          and moved_away["kick"].startswith("PGU-9 cannot be kicked back: app does not own in_progress")
          and moved_away["unchanged"] is True,
          f"a retired producer, or one no longer an owner, refuses the kick-back and nothing moves: {after['changed']}")
    check("designer is not an active role" in after["changed_retired"] and after["changed_set"] == "ok"
          and after["changed_kick"] == ["ok", "in_progress/main"],
          f"the Director records a return the workflow admits, and Audit kicks back: {after['changed_kick']}")

    # Upgraded with a ticket in Audit.
    u = after["upgraded"]
    status = u["status"] or {}
    check(u["where"] == ["audit", "audit"] and status.get("valid") is False
          and status.get("problem") == f"the board has no record of who produced the revision it carries ({A})"
          and u["needs_director"] is True and "Kick-back refused:" in after["upgraded_text"],
          f"upgraded, a ticket already in Audit has no producer and is in the Director's queue: {u}")
    check(len(u["notices"]) == 1 and u["notices"][0][:4] == ["PGU-10", "director", "ticket_update", "audit_return_missing"]
          and "Nothing was moved." in u["notices"][0][4],
          f"and the Director is told once, about that ticket only: {u['notices']}")
    check(len(after["upgraded_delivered"]) == 1 and "Audit's kick-back cannot return it" in after["upgraded_delivered"][0],
          f"the listener delivers it to the Director, once: {after['upgraded_delivered']}")
    check(after["upgraded_kick"].startswith("PGU-10 cannot be kicked back") and after["upgraded_where"] == ["audit", "audit"]
          and after["upgraded_set"] == "ok" and after["upgraded_after"] == ["ok", ["in_progress", "main"]],
          f"its kick-back waits for the Director's record: {after['upgraded_kick']} {after['upgraded_after']}")
    alerts = card_alerts([{"id": "PGU-6", "audit_return": after["edited_status"]},
                          {"id": "PGU-1", "audit_return": after["impl_status"]}, {"id": "PGU-3", "audit_return": None}])
    check(alerts == [[{"kind": "blocked", "title": "Kick-back would be refused",
                       "text": f"{after['edited_status']['problem']}. Repair: {after['edited_status']['repair']}."}], [], []],
          f"the board's card shows a kick-back that would be refused, with its repair: {alerts}")
    names = sorted({row[0].split("(")[0] for row in after["migrated_shape"][0]})
    check(after["migrated_shape"] == after["fresh_shape"] == after["upgraded_shape"] and len(names) == 8,
          f"schema.sql and rbac.sql install what the migration does, fresh and upgraded from main: {names}")
    print(f"audit_return_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
