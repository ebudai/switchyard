#!/usr/bin/env python3
"""SYRD-599: what Router is allowed to be, how it is turned on, and what it is told.

* Each rule of a Router declaration is enforced twice and independently: by
  workflow_config (the app's check) and by the board's own
  validate_declared_workflow, called directly so the app cannot answer for it,
  on a schema.sql board and on a board after the real ticket-board-migrate.
* schema.sql's final copy of every function the migration changes is the
  migration's copy, so a fresh board and an upgraded one run the same code.
* `switchyard router-role enable|disable`, against a board and a real tenant
  projection: the role lands detached, with its own runtime and prompt, under
  the project account, and turns off again.
* The real `ticket-board-write router-assign` / `router-return` reach the board.
* Router's prompt and the shared skill say what the User decided, and no more.
"""

from __future__ import annotations

import contextlib
import copy
import importlib
import io
import json
import os
import re
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
import ticket_board_write_api_test as t  # noqa: E402
from scripts.ticket_board.router_setup import ROUTER_PROMPT, with_router, without_router  # noqa: E402
from scripts.ticket_board.workflow_config import validate  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

SHIPPED = json.loads((ROOT / "examples/workflows/inspection.json").read_text())
MIGRATION = ROOT / "scripts/ticket_board/migrations/pgu997_syrd599_router_role.sql"
SCHEMA = ROOT / "scripts/ticket_board/schema.sql"
CHANGED = ("router_holds(", "ticket_valid_assignee(", "require_stage_owner_assignee(",
           "apply_stage_default_assignee_update(", "transition_target_role(", "remember_declared_implementer(",
           "validate_router_declaration(", "validate_declared_workflow(", "apply_declared_workflow(cfg jsonb)",
           "enforce_declared_ticket_update(", "router_ticket_revision(", "router_available(", "router_assignable_roles(",
           "require_router_ticket(", "router_assign(", "router_return(", "require_router_change(", "ticket_routing(",
           "note_router_outcome(")
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def routed() -> dict:
    return with_router(SHIPPED, runtime="claude")


def role(doc: dict, name: str) -> dict:
    return next(r for r in doc["roles"] if r["name"] == name)


def transition(doc: dict, action: str) -> dict:
    return next(x for x in doc["transitions"] if x["action"] == action)


def stage(doc: dict, name: str) -> dict:
    return next(s for s in doc["stages"] if s["name"] == name)


def broken(change) -> dict:
    doc = copy.deepcopy(routed())
    change(doc)
    return doc


#: Each rule, a document that breaks only it, and the words both validators must say.
VIOLATIONS = (
    ("a second router-kind role",
     lambda d: d["roles"].append({**copy.deepcopy(role(d, "router")), "name": "router2", "label": "Router 2",
                                  "active": False, "slot": None, "target": None, "runtime": None}),
     "only the role named router may be of kind router"),
    ("router_assign on an implementer", lambda d: role(d, "main")["capabilities"].append("router_assign"),
     "router_assign belongs to the router role alone"),
    ("router may comment", lambda d: role(d, "router")["capabilities"].append("add_comment"),
     "its capabilities are exactly router_assign"),
    ("router may reassign", lambda d: role(d, "router")["capabilities"].append("reassign"),
     "its capabilities are exactly router_assign"),
    ("router is serial", lambda d: role(d, "router").update(serial=True), "router never holds a serial slot"),
    ("router owns a review", lambda d: stage(d, "audit")["owners"].append("router"), "router owns no stage"),
    ("router may defer", lambda d: transition(d, "defer")["actors"].append("router"), "router_return is its only move"),
    ("router_return parks", lambda d: transition(d, "router_return").update(to="backlog"),
     "router_return moves Implementation to analysis"),
    ("router_return needs no reason", lambda d: transition(d, "router_return").update(require_reason=False),
     "router_return moves Implementation to analysis"),
    ("router_return for anyone", lambda d: transition(d, "router_return").update(owner_scoped=False),
     "router_return moves Implementation to analysis"),
    ("router_return clears a sign-off", lambda d: transition(d, "router_return").update(clear_signoffs=["audit_signoff"]),
     "router_return moves Implementation to analysis"),
    ("router_return shared with main", lambda d: transition(d, "router_return")["actors"].append("main"),
     "router_return moves Implementation to analysis"),
    ("a router_return without a router",
     lambda d: (d.__setitem__("roles", [r for r in d["roles"] if r["name"] != "router"]),
                transition(d, "router_return").update(actors=["main"])),
     "router_return needs the router role"),
    ("an active router nobody can reach", lambda d: role(d, "router").update(runtime=None, target=None),
     "an active router needs a runtime and pane target"),
    ("Implementation is silent", lambda d: stage(d, "in_progress").update(notify={"kind": "none", "role": None}),
     "Implementation must notify its assignee"),
    ("triage is shared", lambda d: stage(d, "analysis")["owners"].append("main"),
     "router returns work to analysis, which must be the director's alone"),
    ("no misroute escape", lambda d: d.__setitem__("transitions", [x for x in d["transitions"]
                                                                   if x["action"] != "implementer_kick_back"]),
     "every Implementation owner needs implementer_kick_back"),
    ("ops cannot hand a misroute back", lambda d: transition(d, "implementer_kick_back")["actors"].remove("ops"),
     "every Implementation owner needs implementer_kick_back"),
)


def refusal(fn) -> str:
    try:
        fn()
    except (ValueError, AssertionError) as exc:
        return str(exc)
    return ""


def test_the_shipped_shape_validates_and_turns_off_cleanly() -> None:
    doc = routed()
    check(refusal(lambda: validate(copy.deepcopy(doc))) == "", "an enabled Router validates")
    check(role(doc, "router") == {
        "name": "router", "label": "Router", "kind": "router", "active": True, "capabilities": ["router_assign"],
        "runtime": "claude", "target": "cerulean-router:0.0", "slot": None, "serial": False, "template_role": "main",
        "onboarding_prompt": ROUTER_PROMPT, "presentation_label": "Router"}, f"Router as declared: {role(doc, 'router')}")
    check({k: transition(doc, "router_return")[k] for k in ("from", "to", "actors", "primitive", "owner_scoped",
                                                             "require_reason", "require_commit", "clear_signoffs")}
          == {"from": "in_progress", "to": "analysis", "actors": ["router"], "primitive": "move", "owner_scoped": True,
              "require_reason": True, "require_commit": False, "clear_signoffs": []}, "router_return as declared")
    check(sorted(transition(doc, "implementer_kick_back")["actors"]) == ["app", "main", "ops"]
          and transition(doc, "implementer_kick_back")["to"] == "analysis", "every implementer can hand a misroute back")
    untouched = validate(copy.deepcopy(SHIPPED))
    added = {r["name"] for r in doc["roles"]} - {r["name"] for r in untouched["roles"]}
    check(added == {"router"} and [x for x in doc["transitions"] if x["action"] not in ("router_return", "implementer_kick_back")]
          == untouched["transitions"] and doc["stages"] == untouched["stages"], "nothing else in the document moves")
    off = without_router(doc)
    check(not role(off, "router")["active"] and not any(x["action"] == "router_return" for x in off["transitions"])
          and any(x["action"] == "implementer_kick_back" for x in off["transitions"]),
          "off: Router stays declared but inactive, its move is gone, the implementers' escape stays")
    check(role(with_router(off, runtime="codex"), "router")["runtime"] == "codex", "and it turns back on")
    for args, words in (((doc,), "already has an active router"), ((off,), "no active router")):
        fn = (lambda a=args: with_router(*a, runtime="claude")) if words.startswith("already") else (lambda a=args: without_router(*a))
        check(words in refusal(fn), f"{words}: {refusal(fn)}")
    check("runtime must be one of" in refusal(lambda: with_router(SHIPPED, runtime="gpt")), "an unknown runtime is refused")
    no_triage = copy.deepcopy(SHIPPED)
    stage(no_triage, "analysis")["owners"] = ["director", "main"]
    check("director's alone" in refusal(lambda: with_router(no_triage, runtime="claude")),
          "Router needs a triage that is the Director's alone")


def test_python_refuses_each_broken_declaration() -> None:
    for label, change, words in VIOLATIONS:
        said = refusal(lambda c=change: validate(broken(c)))
        check(words in said, f"workflow_config: {label}: {said!r}")


def test_router_is_told_two_operations_and_nothing_else() -> None:
    check(all(w in ROUTER_PROMPT for w in ("ticket-board-read ticket <ID> --json", "ticket-board-write router-assign",
                                             "ticket-board-write router-return", "--revision", "`assignable`")),
          "the prompt names its one read and its two writes")
    lowered = ROUTER_PROMPT.lower()
    check(not re.search(r"parent|blocker|depend|order|priorit|gate|flag|hold|comment|edit|reassign|route\b", lowered),
          f"nothing else Router could suggest or write appears in its prompt: "
          f"{re.findall(r'parent|blocker|depend|order|priorit|gate|flag|hold|comment|edit|reassign|route', lowered)}")
    check("not re-read tickets, poll the board, or watch panes" in ROUTER_PROMPT, "it reads once and yields")
    skill = (ROOT / "skills/switchyard-board/SKILL.md").read_text()
    head = skill[:skill.index("## What the environment already tells you")]
    check("If `TICKET_BOARD_CALLER_ROLE` is `router`, stop here" in head,
          "the shared skill turns Router away before it teaches anything")
    for path in (ROOT / "skills/switchyard-board/SKILL.md", ROOT / "docs/onboarding/switchyard-board-guide.md"):
        text = " ".join(path.read_text().split())
        check("ticket-board-write implementer-kick-back <id> --reason" in text and "never" in text
              and "back through Router" in text and "misroute" in text,
              f"{path.name}: implementers hand a misroute back to the Director, not to Router")


def drift_check() -> None:
    """Run last, so a planted fault in one copy is caught by what it changes before this sees the copies differ."""
    schema, migration = SCHEMA.read_text(), MIGRATION.read_text()
    for signature in CHANGED:
        starts = [m.start() for m in re.finditer(r"^CREATE OR REPLACE FUNCTION ticket_board\." + re.escape(signature),
                                                schema, re.M)]
        final = schema[starts[-1]:schema.index("\n$$;\n", starts[-1]) + 5]
        check(final in migration, f"schema.sql's final {signature} is pgu997's copy")
    # Not "no migration comes after pgu997" -- every later ticket adds one -- but none redefines what it ships.
    later = [(p.name, s) for p in sorted(MIGRATION.parent.glob("*.sql")) if p.name > MIGRATION.name for s in CHANGED
             if f"CREATE OR REPLACE FUNCTION ticket_board.{s}" in p.read_text()]
    check(later == [], f"no later migration redefines what pgu997 ships: {later}")


class Board:
    def __init__(self, cluster, db: str, *, migrate: bool) -> None:
        self.admin = t.conninfo(cluster.socket_dir, cluster.port, db)
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", db])
        t.psql(self.admin, SCHEMA.read_text())
        if t.psql(self.admin, "SELECT count(*) FROM pg_roles WHERE rolname = 'director';") == "0":
            t.create_roles(self.admin)
        t.psql(self.admin, (ROOT / "scripts/ticket_board/rbac.sql").read_text())
        if migrate:
            done = subprocess.run(["bash", str(ROOT / "scripts/ticket-board-migrate")], capture_output=True, text=True,
                                  env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": self.admin}, timeout=300)
            check(done.returncode == 0, done.stderr[-800:])
            t.psql(self.admin, (ROOT / "scripts/ticket_board/rbac.sql").read_text())
        frames, assets = cluster.root / f"{db}-frames", cluster.root / f"{db}-assets"
        self.app = t.TicketBoardApp(frames, assets, project="cerulean", ticket_prefix="PGU",
                                    database_url=t.conninfo(cluster.socket_dir, cluster.port, db, t.SERVICE_ROLE))
        self.server = t.TicketBoardServer(("127.0.0.1", 0), self.app, director_notifier=t.QuietNotifier())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def sql_validates(self, document: dict) -> str:
        """The board's own validator on `document`, without the app's Python check in front of it."""
        done = subprocess.run(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-v", f"doc={json.dumps(document)}",
                               "-d", self.admin], input="SELECT ticket_board.validate_declared_workflow(:'doc'::jsonb);\n",
                              capture_output=True, text=True)
        return "" if done.returncode == 0 else done.stderr

    def document(self) -> dict:
        return json.loads(t.psql(self.admin, "SELECT document FROM ticket_board.workflow_configuration WHERE singleton;"))

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def exercise_sql_validator(board: Board, label: str) -> None:
    check(board.sql_validates(routed()) == "", f"{label}: the board accepts an enabled Router")
    check(board.sql_validates(validate(copy.deepcopy(SHIPPED))) == "", f"{label}: and a document without one")
    for name, change, words in VIOLATIONS:
        said = board.sql_validates(broken(change))
        check(words in said, f"{label}: validate_declared_workflow: {name}: {said[-300:]!r}")


def exercise_command(cluster, board: Board) -> None:
    """`switchyard router-role`, against the board and a real tenant projection."""
    launcher_dir = cluster.root / "launcher"
    launcher_dir.mkdir()
    project_dir = cluster.root / "project"
    (project_dir / "docs" / "onboarding").mkdir(parents=True)
    (cluster.root / "cerulean.project.json").write_text(json.dumps({"project": {"repository": str(project_dir)}}))
    config_path = launcher_dir / "cerulean.json"
    config_path.write_text(json.dumps({
        "project": "cerulean", "layout": "layout.json", "board_url": board.base,
        "roles": [{"role": "main", "cli": ["/nonexistent/claude"], "slot": 3, "model": "the-template-model"}],
        "worktree_base": str(cluster.root / "worktrees"), "repository": str(project_dir),
    }))
    t.TEST_WRITE_TOKEN = board.server.write_token
    t.post_json(board.base, "/api/tickets/actions/configure_workflow",
                {"document": validate(copy.deepcopy(SHIPPED)), "expected_revision": 0, "dry_run": False}, caller="director")
    os.environ["TICKET_BOARD_WRITE_TOKEN"] = board.server.write_token
    os.environ["TICKET_BOARD_CALLER_ROLE"] = "director"
    importlib.reload(importlib.import_module("scripts.ticket_board.write_client"))
    importlib.reload(importlib.import_module("scripts.workflow_manage"))
    launcher = importlib.import_module("scripts.team_launcher")
    check("router-role" in launcher.SWITCHYARD_UNPRIVILEGED_COMMANDS, "the Director turns Router on without root")
    previous = launcher.DEFAULT_CONFIG_DIR
    launcher.DEFAULT_CONFIG_DIR = launcher_dir
    try:
        def run(*extra: str) -> tuple[object, str, str]:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = launcher.switchyard_main(["router-role", *extra, "--project", "cerulean"])
                except SystemExit as exc:
                    code = exc.code
                except Exception as exc:  # a fault in the command is a failed check, not a crash
                    code = f"{type(exc).__name__}: {exc}"
            return code, out.getvalue(), err.getvalue()

        before = board.document()
        code, out, err = run("enable")
        check(code != 0 and "requires --runtime" in err, f"enabling names its runtime: {code} {err[-200:]}")
        code, out, _ = run("enable", "--runtime", "codex", "--dry-run")
        check(code == 0 and '"router_return"' in out and board.document() == before,
              f"a dry run shows the document and changes nothing: {code}")
        code, out, err = run("enable", "--runtime", "codex")
        stored = board.document()
        check(code == 0 and role(stored, "router")["active"] and role(stored, "router")["runtime"] == "codex"
              and any(x["action"] == "router_return" for x in stored["transitions"]),
              f"the board holds an active Router: {code} {err[-300:]}")
        launched = json.loads(config_path.read_text())
        entry = next((r for r in launched["roles"] if r["role"] == "router"), None)
        check(entry is not None and entry.get("detached") is True and "slot" not in entry
              and entry.get("cli") == ["codex"] and entry.get("model") == "the-template-model"
              and "run_as_user" not in entry,
              f"the launcher runs Router detached, on its own runtime, from its template's model, as the project "
              f"account: {entry}")
        env = (entry or {}).get("env") or {}
        check(env.get("TICKET_BOARD_ROLE_ONBOARDING_PROMPT") == ROUTER_PROMPT,
              f"Router's fresh session starts with its prompt: {sorted(env)}")
        projected = json.loads((launcher_dir / "workflow.json").read_text())
        check(role(projected, "router")["kind"] == "router", "the tenant's copy of the workflow has Router too")
        from scripts import role_runtime
        cheaper = role_runtime.document_with_runtime(stored, role="router", runtime="claude")
        check(role(cheaper, "router")["runtime"] == "claude" and validate(copy.deepcopy(cheaper)),
              "set-role-runtime can move Router like any role")
        code, _, err = run("enable", "--runtime", "codex")
        check(code != 0 and "already has an active router" in err, f"enabling twice changes nothing: {err[-200:]}")
        code, out, err = run("disable")
        stored = board.document()
        launched = json.loads(config_path.read_text())
        check(code == 0 and not role(stored, "router")["active"]
              and not any(r["role"] == "router" for r in launched["roles"]),
              f"disable turns it off on the board and in the launcher: {code} {err[-300:]}")
    finally:
        launcher.DEFAULT_CONFIG_DIR = previous


def exercise_cli(board: Board) -> None:
    """The real ticket-board-write, as Router's pane runs it."""
    t.TEST_WRITE_TOKEN = board.server.write_token
    revision = int(t.psql(board.admin, "SELECT revision FROM ticket_board.workflow_configuration;"))
    t.post_json(board.base, "/api/tickets/actions/configure_workflow",
                {"document": with_router(board.document(), runtime="claude"), "expected_revision": revision,
                 "dry_run": False}, caller="director")
    # Router's pane registers its runtime as any pane does; without it the plain route is refused.
    from scripts.ticket_board.peer_identity import read_process
    sleeper = subprocess.Popen(["sleep", "3600"])
    router = role(board.document(), "router")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "TICKET_BOARD_URL": board.base,
           "TICKET_BOARD_CALLER_ROLE": "router", "TICKET_BOARD_WRITE_TOKEN": board.server.write_token}

    def cli(*args: str, caller: str = "router") -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(ROOT / "scripts" / args[0]), *args[1:]], capture_output=True, text=True, timeout=60,
                              env={**env, "TICKET_BOARD_CALLER_ROLE": caller}, stdin=subprocess.DEVNULL)

    def where(ticket: str) -> str:
        return t.psql(board.admin, f"SELECT state || '/' || assignee FROM ticket_board.tickets WHERE id = '{ticket}';")

    for ticket in ("PGU-901", "PGU-902", "PGU-903"):
        t.seed_postgres_ticket(board.admin, ticket, title=ticket, state="analysis", assignee="director", commit_exempt=True)
    down = cli("ticket-board-write", "route", "PGU-901", "--state", "in_progress", caller="director")
    check(down.returncode != 0 and "Router is not running" in down.stderr and where("PGU-901") == "analysis/director",
          f"the Director's plain route is refused while Router has no pane: {down.stderr[-300:]}")
    try:
        t.psql(board.admin, "INSERT INTO ticket_board.role_runtime_assignments (role, runtime, actual_target, worktree, "
                            "session_dir, process_pid, process_start_time, process_uid) VALUES ('router', "
                            f"'{router['runtime']}', '{router['target']}', '/w/router', '/s/router', {sleeper.pid}, "
                            f"{read_process(sleeper.pid).start_time}, {os.getuid()});")
        for ticket in ("PGU-901", "PGU-902"):
            routed = cli("ticket-board-write", "route", ticket, "--state", "in_progress", caller="director")
            check(routed.returncode == 0 and where(ticket) == "in_progress/router",
                  f"the Director's plain CLI route goes through Router: {routed.stderr[-300:]} {where(ticket)}")
        direct = cli("ticket-board-write", "route", "PGU-903", "--state", "in_progress", "--assignee", "ops", caller="director")
        check(direct.returncode == 0 and where("PGU-903") == "in_progress/ops",
              f"and naming an owner is the direct route: {direct.stderr[-300:]} {where('PGU-903')}")
    finally:
        sleeper.kill()
        sleeper.wait()

    for args in (("add-comment", "PGU-901", "--text", "main, I think"), ("set-blockers", "PGU-901", "--blocked-by", "PGU-902",
                 "--blocked-reason", "x"), ("route", "PGU-901", "--state", "analysis", "--assignee", "director")):
        refused = cli("ticket-board-write", *args)
        check(refused.returncode != 0 and "router may use only router-assign and router-return" in refused.stderr,
              f"the CLI offers Router nothing but its two writes: {args[0]}: {refused.stderr[-200:]}")
    read = cli("ticket-board-read", "ticket", "PGU-901", "--json")
    routing = json.loads(read.stdout).get("routing") or {}
    check(read.returncode == 0 and len(routing.get("revision", "")) == 32, f"Router's one read: {read.stderr[-300:]}")
    done = cli("ticket-board-write", "router-assign", "PGU-901", "--assignee", "ops", "--revision", routing["revision"])
    check(done.returncode == 0 and where("PGU-901") == "in_progress/ops", f"router-assign: {done.stderr[-300:]} {where('PGU-901')}")
    again = cli("ticket-board-write", "router-assign", "PGU-901", "--assignee", "app", "--revision", routing["revision"])
    check(again.returncode != 0 and "is not router work" in again.stderr, f"and only once: {again.stderr[-300:]}")
    second = json.loads(cli("ticket-board-read", "ticket", "PGU-902", "--json").stdout)["routing"]["revision"]
    done = cli("ticket-board-write", "router-return", "PGU-902", "--reason", "Needs a decision first.", "--revision", second)
    check(done.returncode == 0 and where("PGU-902") == "analysis/director", f"router-return: {done.stderr[-300:]} {where('PGU-902')}")


def main() -> int:
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    for key in [k for k in os.environ if k.startswith(("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL"))]:
        os.environ.pop(key)
    with temporary_cluster(prefix="syrd599c-", shutdown="immediate") as cluster:
        for db, migrate in (("schema_copy", False), ("migration_copy", True)):
            board = Board(cluster, db, migrate=migrate)
            try:
                exercise_sql_validator(board, db)
            finally:
                board.close()
        board = Board(cluster, "command", migrate=True)
        try:
            exercise_command(cluster, board)
            exercise_cli(board)
        finally:
            board.close()
    drift_check()
    print(f"router_role_command_test: ok ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
