#!/usr/bin/env python3
"""SYRD-549: a declared workflow's roles could not record the waits it gives them.

Reported from otto (release 2951c5c9). OTTO-9 was a Draft ticket owned by the
declared `designer`, waiting on the User's answers. The turn-end prompt told the
designer to `request-dependency` if it needed another role. It did,
`--role user`, and nothing was recorded. At grace expiry the Director was told
the turn was unresolved, by when the User had answered, and acted on that stale
escalation. Three refusals were named in `ticket_board.set_awaiting_role`:
1. a fixed actor list, applied even under a declared workflow;
2. an awaited role must have a pane target, and the User has none;
3. Draft is not a stage a wait may be recorded in.

The prompt also offered a blocker (Director-only) and "continue work" (which no
role can reach).

The cases, on a production-built board (companion roles, schema.sql, the real
migrate runner, rbac.sql) with an otto-shaped declared workflow:
- `designer` owns Draft and may release a draft;
- `uiux` is a custom-named implementer holding `await_role`;
- `scribe` is a custom-named implementer that does not hold it;
- `user` is the declared User, with no pane.

Every write goes through the real `ticket-board-write` CLI, as that role, over
the board's HTTP API. The turn-end cases use the real notify listener, driven
one loop iteration at a time, and the real PaneActivityGate over real hook-state
files written by the real PaneHookStateStore. Only the gate's tmux and /proc
probes are the scenario's. Each pane role is registered as production does it:
a runtime assignment naming a live process, here a sleeping child. A second
database on the same cluster carries no declared workflow, for the legacy
cases.

It runs on main, from a git archive in a child process, and on this tree.

SYRD-662 later made a wait on the User the Director's alone (`await-user`, in a
stage the workflow's `user_waits` lists): the designer's and the database's
waits on the User are refused here now, and the clearing and stale-escalation
cases wait on pane roles instead.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "d5461813f283116a1451aef3b92ec9ad988aed6e"  # main before SYRD-549
GRACE = 4.0
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
CUSTOM = ("designer", "uiux", "scribe")
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def clean_env() -> dict[str, str]:
    for key in [k for k in os.environ if k.startswith(PANE_ENV_PREFIXES)]:
        os.environ.pop(key)
    return dict(os.environ)


def otto_shaped(root: Path) -> dict:
    cfg = json.loads((root / "examples/workflows/inspection.json").read_text())
    cfg["project"] = "pgu"
    roles = {role["name"]: role for role in cfg["roles"]}
    for role in cfg["roles"]:
        if role.get("target"):
            role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
    roles["designer"].update(active=True, target="pgu-designer:0.0")
    for name, caps in (("uiux", roles["app"]["capabilities"]),
                       ("scribe", [c for c in roles["app"]["capabilities"] if c not in ("await_role", "clear_awaiting_role")])):
        cfg["roles"].append({**roles["app"], "name": name, "label": name.upper(), "capabilities": list(caps),
                             "target": f"pgu-{name}:0.0", "slot": None})
    for stage in cfg["stages"]:
        if stage["name"] == "draft":
            stage["owners"] = ["designer"]
        if stage["name"] == "in_progress":
            stage["owners"] = [*stage["owners"], "uiux", "scribe"]
    for tr in cfg["transitions"]:
        if tr["from"] == "in_progress" and "app" in tr["actors"]:
            tr["actors"] = [*tr["actors"], "uiux", "scribe"]
        if tr["action"] == "release_draft":
            tr["actors"] = [*tr["actors"], "designer"]
    cfg["reassign"]["draft"] = "designer"
    return cfg


def scenario(root: Path, prefix: str, before_root: Path | None = None) -> dict:
    for extra in (str(root), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import psycopg
    import ticket_board_write_api_test as fixture
    from scripts.ticket_board.notify_listener import ActivityTrace, PaneActivityGate, PaneHookStateStore, TicketBoardNotifyListener
    from temporary_cluster import temporary_cluster

    class Gate(PaneActivityGate):
        """The real gate over real hook files; only what it would read from tmux and /proc is the scenario's."""

        def _full_activity_trace(self, target, check_trusted_working=False):
            return ActivityTrace(False, "scenario_idle")

        def background_work_roles(self, roles=None):
            return set()

        def composer_snapshot(self, target):
            return None

    seen: dict = {}
    with temporary_cluster(prefix=prefix, shutdown="immediate") as cluster, tempfile.TemporaryDirectory() as panes:
        fixture.create_roles(fixture.conninfo(cluster.socket_dir, cluster.port, "postgres"))

        def board(dbname: str, declared: dict | None):
            admin = fixture.conninfo(cluster.socket_dir, cluster.port, dbname)
            fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", dbname])
            fixture.psql(admin, (root / "scripts/ticket_board/schema.sql").read_text())
            fixture.psql(admin, (root / "scripts/ticket_board/rbac.sql").read_text())
            result = subprocess.run(["bash", str(root / "scripts/ticket-board-migrate")], capture_output=True, text=True,
                                    env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": admin}, timeout=300)
            assert result.returncode == 0, result.stderr[-1500:]
            fixture.psql(admin, (root / "scripts/ticket_board/rbac.sql").read_text())
            app = fixture.TicketBoardApp(cluster.root / f"frames-{dbname}", cluster.root / f"assets-{dbname}", project="pgu",
                                         ticket_prefix="PGU",
                                         database_url=fixture.conninfo(cluster.socket_dir, cluster.port, dbname, fixture.SERVICE_ROLE))
            server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
            threading.Thread(target=server.serve_forever, daemon=True).start()
            if declared is not None:
                app.apply_workflow(declared, expected_revision=0, dry_run=False, caller_role="director")
            return admin, server

        admin, server = board("board", otto_shaped(root))
        legacy_admin, legacy_server = board("legacy", None)

        def cli(srv, role: str, *args: str) -> str:
            env = {"PATH": "/usr/bin:/bin", "HOME": str(cluster.root), "LANG": "C.UTF-8",
                   "TICKET_BOARD_URL": f"http://127.0.0.1:{srv.server_port}", "TICKET_BOARD_WRITE_TOKEN": srv.write_token,
                   "TICKET_BOARD_CALLER_ROLE": role}
            done = subprocess.run([sys.executable, str(root / "scripts" / "ticket-board-write"), *args], env=env,
                                  cwd=str(cluster.root), capture_output=True, text=True, timeout=120, check=False)
            return "ok" if done.returncode == 0 else " ".join((done.stdout + done.stderr).split())[-300:]

        def sql(conn: str, statement: str) -> str:
            return fixture.psql(conn, statement).strip()

        def waiting(conn: str, ticket: str) -> str:
            return sql(conn, f"SELECT awaiting_role FROM ticket_board.ticket_notification_state WHERE ticket_id = '{ticket}';")

        def queued(conn: str, ticket: str) -> list:
            return json.loads(sql(conn, "SELECT coalesce(json_agg(json_build_array(target_role, payload->>'kind') ORDER BY id), '[]') "
                                        f"FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{ticket}';"))

        def comments(conn: str, ticket: str) -> int:
            return int(sql(conn, f"SELECT count(*) FROM ticket_board.ticket_comments WHERE ticket_id = '{ticket}';"))

        tickets = {"PGU-1": ("draft", "designer"), "PGU-2": ("draft", "designer"), "PGU-3": ("in_progress", "uiux"),
                   "PGU-4": ("in_progress", "scribe"), "PGU-5": ("draft", "designer"), "PGU-7": ("in_progress", "uiux")}
        for ticket, (state, owner) in tickets.items():
            fixture.seed_postgres_ticket(admin, ticket, title=ticket, state=state, assignee=owner, commit_exempt=True)
        seen["seeded"] = {t: json.loads(sql(admin, f"SELECT json_build_array(state, assignee)::text FROM ticket_board.tickets WHERE id = '{t}';"))
                          for t in tickets}

        # The three refusals, each on its own.
        seen["designer_awaits_user_in_draft"] = cli(server, "designer", "request-dependency", "PGU-1", "--role", "user",
                                                     "--reason", "Five decisions for the User: see the questions above.")
        seen["pgu1"] = {"waiting": waiting(admin, "PGU-1"), "comments": comments(admin, "PGU-1"), "queued": queued(admin, "PGU-1")}
        seen["designer_awaits_director_in_draft"] = cli(server, "designer", "await-role", "PGU-2", "--role", "director")
        seen["pgu2"] = {"waiting": waiting(admin, "PGU-2"), "queued": queued(admin, "PGU-2")}
        seen["uiux_awaits_app"] = cli(server, "uiux", "request-dependency", "PGU-3", "--role", "app",
                                      "--reason", "The API shape is App's call.")
        seen["pgu3"] = {"waiting": waiting(admin, "PGU-3"), "queued": queued(admin, "PGU-3")}
        # Who may not record a wait.
        seen["scribe_awaits_app"] = cli(server, "scribe", "await-role", "PGU-4", "--role", "app")
        seen["perf_awaits_app"] = cli(server, "perf", "await-role", "PGU-4", "--role", "app")
        seen["designer_awaits_itself"] = cli(server, "designer", "await-role", "PGU-2", "--role", "designer")
        seen["designer_awaits_inactive"] = cli(server, "designer", "await-role", "PGU-2", "--role", "perf")
        seen["pgu4"] = waiting(admin, "PGU-4")
        # Backlog is a draft stage nobody owns: it holds no work to wait.
        fixture.seed_postgres_ticket(admin, "PGU-8", title="PGU-8", state="backlog", assignee="unassigned", commit_exempt=True)
        seen["director_awaits_in_backlog"] = cli(server, "director", "await-role", "PGU-8", "--role", "app")
        # The moves the guard offers an owner, asked of the board directly: (state, assignee, owner). Main has no such question.
        def asked(statement: str) -> str:
            try:
                return sql(admin, statement)
            except AssertionError as refused:
                return " ".join(str(refused).split())[:200]

        seen["choices"] = {f"{st}/{asg}/{own}": asked(f"SELECT ticket_board.unresolved_turn_choices('{st}', '{asg}', '{own}')::text;")
                           for st, asg, own in (("draft", "designer", "designer"), ("in_progress", "scribe", "scribe"),
                                                ("in_progress", "uiux", "scribe"), ("analysis", "director", "director"))}
        seen["no_move_text"] = asked("SELECT ticket_board.unresolved_turn_repair_options('{}');")

        def as_role(conn: str, role: str, statement: str) -> str:
            """The database alone, as the service with `role` as the caller: the API's admission is not consulted."""
            try:
                fixture.psql(conn, f"SET ticket_board.caller_role = '{role}'; {statement}")
            except AssertionError as refused:
                return " ".join(str(refused).split())[:300]
            return "ok"

        service = fixture.conninfo(cluster.socket_dir, cluster.port, "board", fixture.SERVICE_ROLE)
        seen["sql_scribe"] = as_role(service, "scribe", "SELECT ticket_board.set_awaiting_role('PGU-4', 'app');")
        seen["sql_uiux_user"] = as_role(service, "uiux", "SELECT ticket_board.set_awaiting_role('PGU-7', 'user');")
        seen["sql_uiux_app"] = as_role(service, "uiux", "SELECT ticket_board.set_awaiting_role('PGU-7', 'app');")
        seen["pgu7"] = {"waiting": waiting(admin, "PGU-7"), "queued": queued(admin, "PGU-7")}
        # Clearing a wait, and the User acting on the board, as for any wait.
        seen["designer_clears"] = cli(server, "designer", "clear-awaiting-role", "PGU-2")
        seen["pgu1_cleared"] = waiting(admin, "PGU-2")
        seen["scribe_clears"] = cli(server, "scribe", "clear-awaiting-role", "PGU-7")
        seen["pgu7_after_scribe"] = waiting(admin, "PGU-7")
        seen["user_comments"] = cli(server, "user", "add-comment", "PGU-7", "--text", "Use the second layout.")
        seen["pgu7_after_user"] = waiting(admin, "PGU-7")

        # Legacy: no declared workflow.
        for ticket in ("PGU-1", "PGU-2"):
            fixture.seed_postgres_ticket(legacy_admin, ticket, title=ticket, state="in_progress", assignee="app", commit_exempt=True)
        seen["legacy_app_awaits_director"] = cli(legacy_server, "app", "request-dependency", "PGU-1", "--role", "director",
                                                 "--reason", "Needs a Director decision.")
        seen["legacy_pgu1"] = {"waiting": waiting(legacy_admin, "PGU-1"), "queued": queued(legacy_admin, "PGU-1")}
        seen["legacy_app_awaits_user"] = cli(legacy_server, "app", "await-role", "PGU-2", "--role", "user")
        seen["legacy_user_awaits_app"] = cli(legacy_server, "user", "await-role", "PGU-2", "--role", "app")
        seen["legacy_pgu2"] = waiting(legacy_admin, "PGU-2")
        seen["legacy_cleared"] = cli(legacy_server, "app", "clear-awaiting-role", "PGU-1")
        seen["legacy_pgu1_cleared"] = waiting(legacy_admin, "PGU-1")

        # The turn end: what the owner is told, and whether a wait recorded
        # within the grace keeps the Director from a stale escalation.
        store = PaneHookStateStore(panes)
        gate = Gate(state_store=store)
        sent: list[tuple[str, str]] = []
        listener_url = fixture.conninfo(cluster.socket_dir, cluster.port, "board", "ticket_board_listener")
        listener = TicketBoardNotifyListener(conninfo=listener_url, sender=lambda target, message: sent.append((target, message)),
                                             activity_gate=gate.is_working, target_exists=lambda _target: True,
                                             submission_witness=lambda _target, _since: True, poll_seconds=0, project="pgu")
        listener.unresolved_turn_grace_seconds = GRACE
        # Each pane role registered the production way: a runtime assignment naming a live process, here a sleeper.
        from scripts.ticket_board.peer_identity import read_process

        sleepers = []
        for role in ("director", "main", "app", "ops", *CUSTOM):
            sleeper = subprocess.Popen(["sleep", "3600"])
            sleepers.append(sleeper)
            sql(admin, "INSERT INTO ticket_board.role_runtime_assignments (role, runtime, actual_target, worktree, session_dir, "
                       f"process_pid, process_start_time, process_uid) VALUES ('{role}', 'claude', 'pgu-{role}:0.0', '/w/{role}', "
                       f"'/s/{role}', {sleeper.pid}, {read_process(sleeper.pid).start_time}, {os.getuid()});")

        def target(role: str) -> str:
            return f"pgu-{role}:0.0"

        def one_pass() -> None:
            with psycopg.connect(listener_url, autocommit=True) as conn:
                listener.refresh_workflow(conn)
                gate.role_targets = dict(listener.role_targets)
                for step in ("process_reminder_snooze_due", "process_idle_turn_end_nudges", "process_idle_stall_nudges",
                             "process_serial_focus_queue_wakeups"):
                    getattr(listener, step)(conn)
                listener.process_due_notifications(conn)

        def turn(role: str) -> None:
            store.write(target(role), "busy", source="codex.UserPromptSubmit")
            time.sleep(0.05)
            store.write(target(role), "idle", source="codex.Stop")

        def prompts(ticket: str, role: str) -> list[str]:
            return [m for t, m in sent if t == target(role) and m.startswith(f"{ticket} is still yours")]

        def escalations(ticket: str) -> list[str]:
            return [m for t, m in sent if t == target("director") and f"without resolving {ticket} " in m]

        for role in ("director", "app", "main", "ops", *CUSTOM):
            store.write(target(role), "idle", source="codex.Stop")
        one_pass()
        one_pass()
        for role in ("designer", "scribe"):
            turn(role)
        one_pass()
        seen["designer_prompt"] = prompts("PGU-5", "designer")
        seen["scribe_prompt"] = prompts("PGU-4", "scribe")
        seen["designer_waits"] = cli(server, "designer", "request-dependency", "PGU-5", "--role", "director",
                                     "--reason", "Waiting on the Director's five decisions.")
        seen["pgu5_waiting"] = waiting(admin, "PGU-5")
        start = time.time()
        while time.time() - start < GRACE + 3:
            one_pass()
            time.sleep(1.0)
        seen["pgu5_escalations"] = escalations("PGU-5")
        seen["to_user"] = [m for t, m in sent if t not in {target(r) for r in ("director", "app", "main", "ops", *CUSTOM)}]
        seen["user_queue"] = json.loads(sql(admin, "SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE target_role = 'user';"))
        # The User has answered: the wait is cleared, and the next unresolved turn is the owner's again.
        seen["designer_resumes"] = cli(server, "designer", "clear-awaiting-role", "PGU-5")
        turn("designer")
        one_pass()
        seen["designer_prompts_after_resume"] = len(prompts("PGU-5", "designer"))
        # schema.sql and rbac.sql install what the migrations do; and a board
        # built on main takes this migration in place.
        shape = ("SELECT coalesce(json_agg(json_build_array(p.oid::regprocedure::text, md5(pg_get_functiondef(p.oid)), "
                 "pg_get_userbyid(p.proowner), coalesce(p.proacl::text, '')) ORDER BY p.oid::regprocedure::text), '[]')::text "
                 "FROM pg_proc p WHERE p.pronamespace = 'ticket_board'::regnamespace AND p.proname IN ('workflow_wait_stage', "
                 "'wait_target_kind', 'enqueue_awaiting_role_handoff', 'set_awaiting_role', 'unresolved_turn_choices', "
                 "'unresolved_turn_repair_options', 'notify_unresolved_turn_end');")
        seen["migrated_shape"] = json.loads(sql(admin, shape))
        fresh = fixture.conninfo(cluster.socket_dir, cluster.port, "fresh")
        fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "fresh"])
        fixture.psql(fresh, (root / "scripts/ticket_board/schema.sql").read_text())
        fixture.psql(fresh, (root / "scripts/ticket_board/rbac.sql").read_text())
        seen["fresh_shape"] = json.loads(sql(fresh, shape))
        if before_root is not None:
            upgraded = fixture.conninfo(cluster.socket_dir, cluster.port, "upgraded")
            fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "upgraded"])
            for tree in (before_root, root):
                if tree == before_root:
                    fixture.psql(upgraded, (tree / "scripts/ticket_board/schema.sql").read_text())
                    fixture.psql(upgraded, (tree / "scripts/ticket_board/rbac.sql").read_text())
                done = subprocess.run(["bash", str(tree / "scripts/ticket-board-migrate")], capture_output=True, text=True,
                                      env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": upgraded}, timeout=300)
                assert done.returncode == 0, done.stderr[-1500:]
                fixture.psql(upgraded, (tree / "scripts/ticket_board/rbac.sql").read_text())
                seen.setdefault("upgrade_shapes", []).append(json.loads(sql(upgraded, shape)))
        for sleeper in sleepers:
            sleeper.kill()
            sleeper.wait()
        for srv in (server, legacy_server):
            srv.shutdown()
            srv.server_close()
    return seen


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "tests", "examples", "config"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def main() -> int:
    clean_env()
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), "syrd549b-")))
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd549.") as tmp:
        before_root = tree_at(BEFORE, Path(tmp) / "before")
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(before_root)],
                               capture_output=True, text=True, env={**clean_env(), "PYTHONDONTWRITEBYTECODE": "1"})
        assert child.returncode == 0, child.stderr[-3000:]
        before = json.loads(child.stdout.strip().splitlines()[-1])
        after = scenario(ROOT, "syrd549a-", before_root)
    refused_user = "invalid awaiting_role: user"
    check(before["seeded"] == after["seeded"] == {t: list(v) for t, v in {
              "PGU-1": ("draft", "designer"), "PGU-2": ("draft", "designer"), "PGU-3": ("in_progress", "uiux"),
              "PGU-4": ("in_progress", "scribe"), "PGU-5": ("draft", "designer"), "PGU-7": ("in_progress", "uiux")}.items()},
          f"each ticket is seeded where its case needs it: {before['seeded']} {after['seeded']}")

    # Main, as otto reported it.
    check(before["designer_awaits_user_in_draft"].startswith(refused_user) and before["pgu1"]["waiting"] == ""
          and before["pgu1"]["comments"] == 0,
          f"main: the designer's request-dependency on the User is refused, and nothing is recorded, not even the "
          f"reason: {before['designer_awaits_user_in_draft']} {before['pgu1']}")
    check(before["designer_awaits_director_in_draft"].startswith("active ticket not found for awaiting_role: PGU-2"),
          f"main: in Draft, a wait even on a pane role is refused: {before['designer_awaits_director_in_draft']}")
    check(before["uiux_awaits_app"] == "ok" and before["pgu3"]["waiting"] == "app",
          f"main: a custom-named implementer holding await_role already records a wait -- the fixed list is the legacy "
          f"board's, and a declared board admits by capability: {before['uiux_awaits_app']} {before['pgu3']}")
    check(len(before["designer_prompt"]) == 1 and "record a blocker" in before["designer_prompt"][0]
          and "continue work" in before["designer_prompt"][0] and "request-dependency" in before["scribe_prompt"][0],
          f"main: the prompt offers a blocker the designer cannot set, continue work, which nothing reaches, and a "
          f"request-dependency the scribe cannot make: {before['designer_prompt']} {before['scribe_prompt']}")
    check(before["designer_waits"].startswith("active ticket not found for awaiting_role: PGU-5")
          and len(before["pgu5_escalations"]) == 1,
          f"main: the designer cannot record that it waits, so the Director is escalated at grace "
          f"expiry: {before['pgu5_escalations']}")

    # This tree.
    check("the Director's operator:<name> blocker CONTEXT: PL/pgSQL function set_awaiting_role"  # the CLI keeps the tail
          in after["designer_awaits_user_in_draft"]
          and after["pgu1"] == {"waiting": "", "comments": 0, "queued": [["designer", "transition"]]},
          f"SYRD-662: the designer's wait on the User is refused, and nothing is recorded: "
          f"{after['designer_awaits_user_in_draft']} {after['pgu1']}")
    check(after["designer_awaits_director_in_draft"] == "ok"
          and after["pgu2"]["queued"] == [["designer", "transition"], *[["director", "awaiting_role"]] * 4],
          f"in Draft, a wait on a pane role is recorded and handed over on the usual schedule: {after['pgu2']}")
    check(after["uiux_awaits_app"] == "ok"
          and after["pgu3"]["queued"] == [["uiux", "transition"], *[["app", "awaiting_role"]] * 3, ["director", "awaiting_role"]],
          f"a custom-named implementer awaits a pane role, which is handed the wait: {after['pgu3']}")
    check(after["sql_uiux_user"].startswith("ERROR: a dependency cannot name the User: PGU-7 is in Implementation")
          and after["pgu7"] == {"waiting": "app", "queued": [*[["app", "awaiting_role"]] * 3, ["director", "awaiting_role"]]}
          and after["user_queue"] == 0 and after["to_user"] == [],
          f"SYRD-662: the database alone refuses a wait on the User too, and nothing is ever addressed to the User: "
          f"{after['sql_uiux_user']} {after['pgu7']} {after['user_queue']}")
    check(after["scribe_awaits_app"] == "scribe cannot call await_role"
          and after["sql_scribe"].startswith("ERROR: role scribe cannot call set_awaiting_role")
          and after["perf_awaits_app"] == "invalid caller role: perf" and after["pgu4"] == "",
          f"a declared role without await_role is refused by the API and by the database alone, and an undeclared "
          f"one by the API: {after['scribe_awaits_app']} | {after['sql_scribe']} | {after['perf_awaits_app']}")
    check(after["designer_awaits_itself"].startswith("awaiting_role cannot be the caller role: designer")
          and after["designer_awaits_inactive"].startswith("invalid awaiting_role: perf"),
          f"a role cannot await itself, nor an inactive role: {after['designer_awaits_itself']} {after['designer_awaits_inactive']}")
    check(before["director_awaits_in_backlog"].startswith("active ticket not found for awaiting_role: PGU-8")
          and after["director_awaits_in_backlog"].startswith("active ticket not found for awaiting_role: PGU-8"),
          f"Backlog, a draft stage nobody owns, admits no wait, before or after: {after['director_awaits_in_backlog']}")
    check(after["choices"] == {"draft/designer/designer": "{transition,request-dependency}", "in_progress/scribe/scribe": "{transition}",
                               "in_progress/uiux/scribe": "{}", "analysis/director/director": "{transition,request-dependency,blocker}"},
          f"the moves offered are the owner's own: its transitions from the stage, a wait where it holds await_role and "
          f"the stage takes one, blockers only for a role that may set them: {after['choices']}")
    check(after["no_move_text"] == "No move on it from this stage is yours to make: say in a comment what it needs.",
          f"an owner with no move is told so, not offered one it cannot make: {after['no_move_text']}")
    check(after["designer_clears"] == "ok" and after["pgu1_cleared"] == ""
          and after["scribe_clears"] == "scribe cannot call clear_awaiting_role" and after["pgu7_after_scribe"] == "app",
          f"the owner clears its wait; a role without the capability cannot: {after['scribe_clears']}")
    legacy = ("legacy_app_awaits_director", "legacy_pgu1", "legacy_app_awaits_user", "legacy_user_awaits_app",
              "legacy_pgu2", "legacy_cleared", "legacy_pgu1_cleared")
    def said(value):  # a refusal without its CONTEXT, whose line numbers move with any edit to the function
        return value.split(" CONTEXT:")[0] if isinstance(value, str) else value

    check({k: said(after[k]) for k in legacy} == {k: said(before[k]) for k in legacy}
          and after["legacy_pgu1"]["waiting"] == "director" and after["legacy_app_awaits_user"].startswith(refused_user),
          f"a board with no declared workflow answers exactly as main: {[after[k] for k in legacy]}")
    check(len(after["designer_prompt"]) == 1 and "transition it if the work is done" in after["designer_prompt"][0]
          and "`request-dependency` if it is waiting on another role." in after["designer_prompt"][0]
          and "on the User" not in after["designer_prompt"][0]
          and "blocker" not in after["designer_prompt"][0] and "continue" not in after["designer_prompt"][0],
          f"the designer's prompt names the moves it can make in Draft and no others: {after['designer_prompt']}")
    check(len(after["scribe_prompt"]) == 1 and "Do this: transition it if the work is done." in after["scribe_prompt"][0]
          and "request-dependency" not in after["scribe_prompt"][0],
          f"a role without await_role is not told to request a dependency: {after['scribe_prompt']}")
    check(after["designer_waits"] == "ok" and after["pgu5_waiting"] == "director" and after["pgu5_escalations"] == [],
          f"a wait recorded within the grace keeps the Director from a stale escalation: "
          f"{after['pgu5_escalations']}")
    check(after["designer_resumes"] == "ok" and after["designer_prompts_after_resume"] == 2,
          f"once the wait is cleared, the next unresolved turn is the owner's to answer again: "
          f"{after['designer_prompts_after_resume']}")
    names = sorted({row[0].split("(")[0] for row in after["migrated_shape"]})
    check(after["migrated_shape"] == after["fresh_shape"] == after["upgrade_shapes"][1] and len(names) == 7
          and after["upgrade_shapes"][0] != after["upgrade_shapes"][1],
          f"schema.sql and rbac.sql install what the migrations do, and a board built on main takes the migration in "
          f"place: {names}")
    print(f"declared_wait_admission_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
