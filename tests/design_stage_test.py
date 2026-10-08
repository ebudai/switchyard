#!/usr/bin/env python3
"""SYRD-567: an optional declared design stage, owned by the designer and reviewed by the Director.

Otto's designer had no work stage -- its only transition was release_draft -- so
design work ran outside ownership, review and sign-off. Measured on a board built
the production way, a declared designer-owned stage already owned, notified,
handed off and reminded; what differed was the Director's kick-back from design
review, refused while blocked because a `return` could only target
implementation. And the only way to add the stage was a hand-edited document.

Driven on boards built the production way (companion roles, schema.sql, then
rbac.sql), through the real HTTP API: once with schema.sql's validator, and once
after the real ticket-board-migrate, where the newest migration's copy is the one
every live board runs. Then through `switchyard design-stage` itself, against the
same board and a real tenant projection.
"""

from __future__ import annotations

import contextlib
import copy
import importlib
import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests"), str(ROOT / "scripts")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()
import legacy_workflow_equivalence_test as equivalence  # noqa: E402
import ticket_board_write_api_test as t  # noqa: E402
from scripts.ticket_board import legacy_workflow as lw, project_provision as pv  # noqa: E402
from scripts.ticket_board.design_stage import designer_unreachable, with_design_stage  # noqa: E402
from scripts.ticket_board.workflow_config import validate  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

SHIPPED = json.loads((ROOT / "examples/workflows/inspection.json").read_text())
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def refusal(fn) -> str:
    try:
        fn()
    except (ValueError, AssertionError) as exc:
        return str(exc)
    return ""


def added(document: dict) -> dict:
    """with_design_stage, where a refusal is a failed check rather than a crash."""
    try:
        return with_design_stage(document)
    except ValueError as exc:
        check(False, f"the design stage is added: {exc}")


def with_designer(document: dict) -> dict:
    """The shipped document with its designer turned on, as a tenant provisioned with one has it."""
    document = copy.deepcopy(document)
    next(role for role in document["roles"] if role["name"] == "designer")["active"] = True
    return document


def provisioned() -> dict:
    """A provisioned tenant's document (MEFP's shape): an active designer that runs without a pane."""
    plan = equivalence.mefp_plan()
    return lw.compose_legacy_workflow(
        plan, canonical=lw.load_canonical(ROOT), stage_seeds=pv.project_workflow_stages(plan),
        transition_seeds=pv.project_workflow_transitions(plan), panes=equivalence.mefp_panes(),
    ).document


def moves(document: dict) -> set[tuple[str, str, str, tuple[str, ...], str]]:
    return {(tr["from"], tr["to"], tr["action"], tuple(tr["actors"]), tr["primitive"]) for tr in document["transitions"]}


def returning(document: dict, source: str, destination: str) -> dict:
    """`document` with one more Director `return` from `source` to `destination`."""
    document = copy.deepcopy(document)
    document["transitions"].append({
        "from": source, "to": destination, "action": "probe_return", "label": "Probe return", "actors": ["director"],
        "primitive": "return", "owner_scoped": False, "require_commit": False, "require_reason": True,
        "clear_signoffs": [], "allow_no_code": False,
    })
    return document


#: Returns the rule must refuse, each with why: a return goes back, never onward.
REFUSED_RETURNS = (
    ("design_review", "analysis", "triage submits nothing into design review"),
    ("design", "draft", "design is not a review"),
    ("dat", "audit", "a review is no place to hand work back to, though audit approves into DAT"),
    ("audit", "design", "design submits into design review, not audit"),
)


def test_the_command_adds_the_stage_to_a_document_with_a_designer() -> None:
    designed = added(with_designer(SHIPPED))
    names = [stage["name"] for stage in designed["stages"]]
    check(names[:4] == ["draft", "design", "design_review", "backlog"], f"design follows draft: {names}")
    stages = {stage["name"]: stage for stage in designed["stages"]}
    check((stages["design"]["owners"], stages["design"]["kind"], stages["design"]["notify"]["kind"])
          == (["designer"], "system", "assignee"), f"the designer owns design and is notified: {stages['design']}")
    check((stages["design_review"]["owners"], stages["design_review"]["kind"]) == (["director"], "review"),
          f"the Director owns its review: {stages['design_review']}")
    new_moves = moves(designed) - moves(validate(with_designer(SHIPPED)))
    check(new_moves == {
        ("draft", "design", "start_design", ("director",), "move"),
        ("analysis", "design", "start_design", ("director",), "move"),
        ("design", "design_review", "submit_design", ("designer",), "move"),
        ("design_review", "design", "return_design", ("director",), "return"),
        ("design_review", "analysis", "accept_design", ("director",), "move"),
        ("design", "backlog", "defer", ("director",), "move"),
        ("design_review", "backlog", "defer", ("director",), "move"),
    }, f"exactly the design path, nothing else: {sorted(new_moves)}")
    submit = next(tr for tr in designed["transitions"] if tr["action"] == "submit_design")
    back = next(tr for tr in designed["transitions"] if tr["action"] == "return_design")
    check(submit["owner_scoped"] and back["require_reason"], "only the owner submits; a return says why")
    check(("draft", "analysis", "release_draft") in {m[:3] for m in moves(designed)},
          "release_draft is untouched: a draft that needs no design still goes to triage")


def test_a_designer_without_a_pane_gets_a_silent_stage_not_a_refusal() -> None:
    document = provisioned()
    check(designer_unreachable(document), "MEFP's provisioned designer has no pane")
    designed = added(document)
    design = next(stage for stage in designed["stages"] if stage["name"] == "design")
    check(design["notify"]["kind"] == "none" and design["owners"] == ["designer"],
          f"owned, and explicitly silent rather than notifying a role nothing can reach: {design}")
    check(not designer_unreachable(with_designer(SHIPPED)), "a designer with a pane is reachable")
    import argparse
    from scripts import workflow_manage

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        through_command = workflow_manage._document_with_design_stage({"document": document}, argparse.ArgumentParser())
    check(through_command == designed and "the designer has no pane" in err.getvalue(),
          f"and the command says so instead of leaving the Director to find out: {err.getvalue()!r}")


def test_the_command_refuses_what_it_cannot_add_safely() -> None:
    said = refusal(lambda: with_design_stage(SHIPPED))
    check("designer is not an active role" in said, f"no active designer, no stage: {said}")
    designed = added(with_designer(SHIPPED))
    said = refusal(lambda: with_design_stage(designed))
    check("already has design, design_review" in said, f"never added twice: {said}")
    second_park = with_designer(SHIPPED)
    second_park["queue"] = None
    second_park["stages"].insert(2, {**copy.deepcopy(next(s for s in second_park["stages"] if s["name"] == "backlog")),
                                     "name": "icebox", "label": "Icebox"})
    second_park["transitions"] += [
        {**tr, "from": "icebox" if tr["from"] == "backlog" else tr["from"], "to": "icebox" if tr["to"] == "backlog" else tr["to"]}
        for tr in second_park["transitions"] if "backlog" in (tr["from"], tr["to"])
    ]
    check(refusal(lambda: validate(copy.deepcopy(second_park))) == "", "a document with two parking stages is valid")
    said = refusal(lambda: with_design_stage(second_park))
    check("cannot tell which stage deferred work waits in" in said, f"two parking stages and no queue: {said}")
    second_park["queue"] = {"stage": "icebox", "assignee": "unassigned"}
    parked = {(tr["from"], tr["to"]) for tr in added(second_park)["transitions"] if tr["action"] == "defer"}
    check({("design", "icebox"), ("design_review", "icebox")} <= parked and ("design", "backlog") not in parked,
          f"with a queue, deferred design work waits where the queue is: {sorted(parked)}")


def test_existing_documents_are_still_valid_and_returns_still_go_back_only() -> None:
    for name, document in (("shipped", SHIPPED), ("provisioned", provisioned())):
        check(validate(copy.deepcopy(document)) == validate(copy.deepcopy(document)), f"{name} still validates")
    designed = added(with_designer(SHIPPED))
    check(refusal(lambda: validate(copy.deepcopy(designed))) == "", "the design review's return validates")
    for source, destination, why in REFUSED_RETURNS:
        said = refusal(lambda: validate(returning(designed, source, destination)))
        check("return must target the implementation stage" in said, f"{why}: {said}")
    check(refusal(lambda: validate(returning(designed, "dat", "in_progress"))) == "",
          "a review still returns into implementation")


class Board:
    """A board built the production way; `migrate` runs the real ticket-board-migrate before rbac.sql."""

    def __init__(self, cluster, db: str, *, migrate: bool) -> None:
        self.admin = t.conninfo(cluster.socket_dir, cluster.port, db)
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", db])
        t.psql(self.admin, "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='ticket_board_service') THEN "
                           "CREATE ROLE ticket_board_service LOGIN; END IF; IF NOT EXISTS (SELECT FROM pg_roles WHERE "
                           "rolname='ticket_board_listener') THEN CREATE ROLE ticket_board_listener LOGIN; END IF; END $$;")
        with contextlib.suppress(AssertionError):
            t.create_roles(self.admin)
        t.psql(self.admin, t.SCHEMA_PATH.read_text())
        if migrate:
            done = subprocess.run(["bash", str(ROOT / "scripts/ticket-board-migrate")], capture_output=True, text=True,
                                  env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": self.admin})
            check(done.returncode == 0, done.stderr[-800:])
        t.psql(self.admin, t.RBAC_PATH.read_text())
        self.listener = t.conninfo(cluster.socket_dir, cluster.port, db, "ticket_board_listener")
        frames, assets = cluster.root / f"{db}-frames", cluster.root / f"{db}-assets"
        frames.mkdir(), assets.mkdir()
        self.app = t.TicketBoardApp(frames, assets, project="cerulean", ticket_prefix="PGU",
                                    database_url=t.conninfo(cluster.socket_dir, cluster.port, db, t.SERVICE_ROLE))
        self.server = t.TicketBoardServer(("127.0.0.1", 0), self.app, director_notifier=t.QuietNotifier())
        t.TEST_WRITE_TOKEN = self.server.write_token
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def post(self, path: str, payload: dict, caller: str, expect: int = 200):
        return t.post_json(self.base, path, payload, caller=caller, expect=expect)

    def configure(self, document: dict, *, expect: int = 200, dry_run: bool = False):
        revision = int(t.psql(self.admin, "SELECT coalesce(max(revision),0) FROM ticket_board.workflow_configuration;"))
        return self.post("/api/tickets/actions/configure_workflow",
                         {"document": document, "expected_revision": revision, "dry_run": dry_run}, "director", expect)

    def sql_validates(self, document: dict) -> tuple[int, str]:
        """The board's own validator on `document`, without the app's Python check in front of it."""
        done = subprocess.run(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-v", f"doc={json.dumps(document)}", "-d", self.admin],
                              input="SELECT ticket_board.validate_declared_workflow(:'doc'::jsonb);\n",
                              capture_output=True, text=True)
        return done.returncode, done.stderr

    def act(self, ticket: str, action: str, actor: str, expect: int = 200, **payload):
        return self.post(f"/api/tickets/{ticket}/actions/{action}", payload, actor, expect)

    def ticket(self, ticket: str) -> dict:
        return json.loads(t.psql(self.admin, f"SELECT to_jsonb(x) FROM ticket_board.tickets x WHERE id='{ticket}';"))

    def queued(self, ticket: str) -> list[tuple[str, str]]:
        raw = t.psql(self.admin, "SELECT coalesce(jsonb_agg(jsonb_build_array(target_role, kind) ORDER BY id),'[]') "
                                 f"FROM ticket_board.ticket_notification_queue WHERE ticket_id='{ticket}';")
        t.psql(self.admin, f"DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id='{ticket}';")
        return [tuple(item) for item in json.loads(raw)]

    def remind(self, ticket: str, role: str) -> list[tuple[str, str]]:
        """Every reminder the board generates for `role`, run as hours overdue."""
        self.queued(ticket)
        t.psql(self.admin, "UPDATE ticket_board.ticket_notification_state SET entered_current_state_at = "
                           f"clock_timestamp() - interval '4 hours' WHERE ticket_id='{ticket}';")
        t.psql(self.listener, f"""
SELECT ticket_board.notify_idle_stall_nudges(jsonb_build_object('{role}', (clock_timestamp() - interval '3 hours')::text), clock_timestamp());
SELECT ticket_board.notify_idle_turn_end_nudges(jsonb_build_object('{role}', (clock_timestamp() - interval '3 hours')::text),
    clock_timestamp(), interval '0 seconds', '{{}}'::jsonb);""")
        t.psql(self.admin, "SELECT ticket_board.notify_due_nudges(clock_timestamp() + interval '6 hours');")
        return self.queued(ticket)

    def block(self, ticket: str, blocked: bool) -> None:
        fields = {"blocked_by": ["PGU-9"], "blocked_reason": "waits on PGU-9"} if blocked else {"blocked_by": [], "blocked_reason": ""}
        self.app.update_ticket(ticket, fields, caller_role="director")

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def exercise_board(board: Board, label: str) -> None:
    designed = added(with_designer(SHIPPED))
    for source, destination, why in REFUSED_RETURNS:
        code, said = board.sql_validates(returning(designed, source, destination))
        check(code != 0 and "return must target implementation" in said, f"{label}: SQL refuses it too, {why}: {said}")
    code, said = board.sql_validates(designed)
    check(code == 0, f"{label}: SQL accepts the design review's return to design: {said}")
    code, said = board.sql_validates(returning(designed, "dat", "in_progress"))
    check(code == 0, f"{label}: and still accepts a return into implementation: {said}")
    board.configure(designed)
    t.seed_postgres_ticket(board.admin, "PGU-1", title="Spec the scheduler page", state="draft")
    t.seed_postgres_ticket(board.admin, "PGU-9", title="A decision the spec waits on", state="analysis")
    board.queued("PGU-1")

    board.act("PGU-1", "start_design", "director")
    check((board.ticket("PGU-1")["state"], board.ticket("PGU-1")["assignee"]) == ("design", "designer"),
          f"{label}: the designer owns design work: {board.ticket('PGU-1')}")
    check(("designer", "transition") in board.queued("PGU-1"), f"{label}: and is told it is waiting")
    check(("designer", "idle_reminder") in board.remind("PGU-1", "designer"),
          f"{label}: an idle designer is reminded like any owner")

    board.block("PGU-1", True)
    said = str(board.act("PGU-1", "submit_design", "designer", expect=400))
    check("unresolved blocker prevents forward promotion" in said, f"{label}: a blocked spec is not submitted: {said}")
    check(board.remind("PGU-1", "designer") == [], f"{label}: and nobody is reminded about blocked work")
    board.block("PGU-1", False)
    said = str(board.act("PGU-1", "submit_design", "director", expect=403))
    check("cannot" in said, f"{label}: only the designer submits its own work: {said}")
    board.act("PGU-1", "submit_design", "designer")
    check((board.ticket("PGU-1")["state"], board.ticket("PGU-1")["assignee"]) == ("design_review", "director"),
          f"{label}: the hand-off reaches the Director's review: {board.ticket('PGU-1')}")
    board.queued("PGU-1")

    board.block("PGU-1", True)
    said = str(board.act("PGU-1", "return_design", "director", expect=400))
    check("reason" in said, f"{label}: a return says why: {said}")
    board.act("PGU-1", "return_design", "director", reason="Missing the empty states.")
    check((board.ticket("PGU-1")["state"], board.ticket("PGU-1")["assignee"]) == ("design", "designer"),
          f"{label}: the review hands a blocked spec back, as Audit hands back implementation: {board.ticket('PGU-1')}")
    held_design = board.queued("PGU-1")
    said = str(board.act("PGU-1", "accept_design", "director", expect=403))
    check("cannot call accept_design" in said, f"{label}: nothing is accepted that is not in review: {said}")
    board.block("PGU-1", False)
    told_design = board.queued("PGU-1")
    # The same moment for implementation: the Director's DAT kick-back of a blocked candidate.
    t.seed_postgres_ticket(board.admin, "PGU-2", title="A candidate", state="dat", assignee="director", commit_hash="a" * 40)
    t.psql(board.admin, "UPDATE ticket_board.ticket_notification_state SET last_implementer_assignee='main' WHERE ticket_id='PGU-2';")
    board.block("PGU-2", True)
    board.queued("PGU-2")
    board.act("PGU-2", "director_dat_kick_back", "director", reason="Fails on an empty week.")
    held_implementation = board.queued("PGU-2")
    board.block("PGU-2", False)
    told_implementation = board.queued("PGU-2")
    check(board.ticket("PGU-2")["state"] == "in_progress", f"{label}: the implementation kick-back went through blocked")
    check((held_design, told_design) == ([], [("designer", "ticket_update")])
          and (held_implementation, told_implementation) == ([], [("main", "ticket_update")]),
          f"{label}: blocked returned work is held and announced on release, for the designer exactly as for an "
          f"implementer: design {held_design} {told_design}, implementation {held_implementation} {told_implementation}")

    board.act("PGU-1", "submit_design", "designer")
    board.act("PGU-1", "accept_design", "director")
    check((board.ticket("PGU-1")["state"], board.ticket("PGU-1")["assignee"]) == ("analysis", "director"),
          f"{label}: an accepted spec goes to triage, to be routed: {board.ticket('PGU-1')}")
    board.act("PGU-1", "start_design", "director")
    board.block("PGU-1", True)
    board.act("PGU-1", "defer", "director")
    check(board.ticket("PGU-1")["state"] == "backlog", f"{label}: blocked design work can be put down")


def exercise_command(cluster, board: Board) -> None:
    """`switchyard design-stage`, against the board and a real tenant projection."""
    launcher_dir = cluster.root / "launcher"
    launcher_dir.mkdir()
    project_dir = cluster.root / "project"
    (project_dir / "docs" / "onboarding").mkdir(parents=True)
    (cluster.root / "cerulean.project.json").write_text(json.dumps({"project": {"repository": str(project_dir)}}))
    config_path = launcher_dir / "cerulean.json"
    config_path.write_text(json.dumps({
        "project": "cerulean", "layout": "layout.json", "roles": [{"role": "ops", "cli": ["/nonexistent/claude"], "slot": 0}], "board_url": board.base,
        "worktree_base": str(cluster.root / "worktrees"), "repository": str(project_dir),
    }))
    board.configure(with_designer(SHIPPED))
    os.environ["TICKET_BOARD_WRITE_TOKEN"] = board.server.write_token
    os.environ["TICKET_BOARD_CALLER_ROLE"] = "director"
    importlib.reload(importlib.import_module("scripts.ticket_board.write_client"))
    importlib.reload(importlib.import_module("scripts.workflow_manage"))
    launcher = importlib.import_module("scripts.team_launcher")
    check("design-stage" in launcher.SWITCHYARD_UNPRIVILEGED_COMMANDS, "the Director runs it without root")
    previous = launcher.DEFAULT_CONFIG_DIR
    launcher.DEFAULT_CONFIG_DIR = launcher_dir
    try:
        def run(*extra: str) -> tuple[int, str, str]:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = launcher.switchyard_main(["design-stage", "--project", "cerulean", *extra])
                except SystemExit as exc:
                    code = exc.code
                except Exception as exc:  # a fault in the command is a failed check, not a crash
                    code = f"{type(exc).__name__}: {exc}"
            return code, out.getvalue(), err.getvalue()

        before = board.configure(with_designer(SHIPPED), dry_run=True)
        code, out, _ = run("--dry-run")
        check(code == 0 and "design_review" in out, f"a dry run shows the document: {code} {out[-300:]}")
        check(json.loads(t.psql(board.admin, "SELECT document FROM ticket_board.workflow_configuration WHERE singleton;"))
              == validate(with_designer(SHIPPED)), f"and changes nothing: {before}")
        code, out, _ = run()
        stored = json.loads(t.psql(board.admin, "SELECT document FROM ticket_board.workflow_configuration WHERE singleton;"))
        wanted = added(with_designer(SHIPPED))
        # apply's own tail may also seed the director's onboarding, as it does for role-prompt.
        check(code == 0 and (stored["stages"], stored["transitions"]) == (wanted["stages"], wanted["transitions"]),
              f"the board holds exactly the design path: {out}")
        projected = json.loads((launcher_dir / "workflow.json").read_text())
        check({s["name"] for s in projected["stages"]} >= {"design", "design_review"}, "and so does the tenant projection")
        journal = Path(json.loads(out)["journal"])
        previous_document = json.loads(journal.read_text())["previous"]["document"]
        check(not {s["name"] for s in previous_document["stages"]} & {"design", "design_review"}
              and previous_document["transitions"] == validate(with_designer(SHIPPED))["transitions"],
              f"with a journal that undoes it: {journal}")
        code, _, err = run()
        check(code != 0 and "already has design" in err, f"a second run changes nothing: {code} {err[-300:]}")
    finally:
        launcher.DEFAULT_CONFIG_DIR = previous


def main() -> int:
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    with temporary_cluster(prefix="syrd567-", shutdown="immediate") as cluster:
        for db, migrate in (("schema_copy", False), ("migration_copy", True)):
            board = Board(cluster, db, migrate=migrate)
            try:
                exercise_board(board, db)
            finally:
                board.close()
        board = Board(cluster, "command", migrate=True)
        try:
            exercise_command(cluster, board)
        finally:
            board.close()
    print(f"design_stage_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
