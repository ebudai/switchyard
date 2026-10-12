#!/usr/bin/env python3
"""SYRD-451: a Director waiting for a person, on the board the skills describe.

MEFP-9 sat in DAT, owned by the Director, awaiting Eric's decision. The
installed Director and board skills (snapshot 4470716) say to record that as
`set-blockers MEFP-9 --blocked-by operator:eric`. On 2026-09-28 MEFP's board
refused it: "invalid blocked_by ticket id: operator:eric". The Director held
the ticket with manual control instead, which bypasses every gate.

Releases before SYRD-273 (a8a3e06, 2026-09-25) give exactly that refusal; the
skill snapshot came after it. This suite runs the person wait end to end on a
production-built board (companion roles, schema.sql, the real migrate runner,
rbac.sql) with MEFP's own composed workflow, and on a board with no declared
workflow. Every write goes through the real `ticket-board-write`, as that role,
over HTTP. The listener is the real one, driven one loop iteration at a time,
over the real PaneActivityGate and real hook files, with each pane role
registered as production does it. Only the gate's tmux and /proc probes are
the scenario's.

It runs on main, from a git archive in a child process, and on this tree; and
the setting step alone on the release just before SYRD-273.
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
BEFORE = "eb7f888a8e0ef1b92ecf77919979502ba33bac21"  # main before SYRD-451
BEFORE_OPERATOR_WAITS = "a8a3e06ee10f428c1a882a56d1ea24f27f2b7b6c"  # SYRD-273; its parent is the last release without it
GRACE = 4.0
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
PANES = ("director", "main", "ops", "audit")
REASON = "Eric decides: prepare the controlled diagnostic event test, or defer it."
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def clean_env() -> dict[str, str]:
    for key in [k for k in os.environ if k.startswith(PANE_ENV_PREFIXES)]:
        os.environ.pop(key)
    return dict(os.environ)


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "tests", "examples", "config"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def scenario(root: Path, prefix: str, setting_only: bool = False, before_root: Path | None = None) -> dict:
    for extra in (str(root), str(root / "scripts"), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    import ticket_board_write_api_test as fixture
    from temporary_cluster import temporary_cluster

    seen: dict = {}
    with temporary_cluster(prefix=prefix, shutdown="immediate") as cluster, tempfile.TemporaryDirectory() as panes:
        fixture.create_roles(fixture.conninfo(cluster.socket_dir, cluster.port, "postgres"))

        def board(dbname: str, declared: bool):
            admin = fixture.conninfo(cluster.socket_dir, cluster.port, dbname)
            fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", dbname])
            fixture.psql(admin, (root / "scripts/ticket_board/schema.sql").read_text())
            fixture.psql(admin, (root / "scripts/ticket_board/rbac.sql").read_text())
            result = subprocess.run(["bash", str(root / "scripts/ticket-board-migrate")], capture_output=True, text=True,
                                    env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": admin}, timeout=300)
            assert result.returncode == 0, result.stderr[-1500:]
            fixture.psql(admin, (root / "scripts/ticket_board/rbac.sql").read_text())
            app = fixture.TicketBoardApp(cluster.root / f"frames-{dbname}", cluster.root / f"assets-{dbname}", project="mefp",
                                         ticket_prefix="MEFP",
                                         database_url=fixture.conninfo(cluster.socket_dir, cluster.port, dbname, fixture.SERVICE_ROLE))
            server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
            threading.Thread(target=server.serve_forever, daemon=True).start()
            if declared:
                import legacy_workflow_equivalence_test as equivalence
                from ticket_board import legacy_workflow as lw
                from ticket_board import project_provision as pv

                plan = equivalence.mefp_plan()
                document = lw.compose_legacy_workflow(
                    plan, canonical=lw.load_canonical(root), stage_seeds=pv.project_workflow_stages(plan),
                    transition_seeds=pv.project_workflow_transitions(plan), panes=equivalence.mefp_panes(),
                ).document
                fixture.psql(admin, pv.render_workflow_sql(plan))
                with app._pg_connect() as conn:
                    app._pg_set_caller_role(conn, "director")
                    conn.execute("SELECT ticket_board.apply_declared_workflow(%s::jsonb)", (json.dumps(document),))
            return admin, server

        def cli(srv, role: str, *args: str) -> str:
            env = {"PATH": "/usr/bin:/bin", "HOME": str(cluster.root), "LANG": "C.UTF-8",
                   "TICKET_BOARD_URL": f"http://127.0.0.1:{srv.server_port}", "TICKET_BOARD_WRITE_TOKEN": srv.write_token,
                   "TICKET_BOARD_CALLER_ROLE": role}
            done = subprocess.run([sys.executable, str(root / "scripts" / "ticket-board-write"), *args], env=env,
                                  cwd=str(cluster.root), capture_output=True, text=True, timeout=120, check=False)
            return "ok" if done.returncode == 0 else " ".join((done.stdout + done.stderr).split())[:400]

        def api(srv, role: str, ticket: str, operation: str, payload: dict) -> str:
            """The board's HTTP API, as the web UI calls it."""
            try:
                fixture.TEST_WRITE_TOKEN = srv.write_token
                fixture.post_json(f"http://127.0.0.1:{srv.server_port}", f"/api/tickets/{ticket}/actions/{operation}",
                                  payload, caller=role)
            except Exception as refused:  # noqa: BLE001 - the refusal is the observation
                return " ".join(str(refused).split())[:400]
            return "ok"

        def sql(conn: str, statement: str) -> str:
            return fixture.psql(conn, statement).strip()

        def ticket(conn: str, ticket_id: str) -> dict:
            return json.loads(sql(conn, "SELECT json_build_object('state', t.state, 'assignee', t.assignee, "
                                        "'manually_controlled', t.manually_controlled, 'blocked_reason', t.blocked_reason, "
                                        "'blocked_by', (SELECT coalesce(json_agg(b.blocker_ticket_id ORDER BY b.position), '[]') "
                                        "FROM ticket_board.ticket_blockers b WHERE b.ticket_id = t.id), "
                                        "'comments', (SELECT count(*) FROM ticket_board.ticket_comments c WHERE c.ticket_id = t.id))::text "
                                        f"FROM ticket_board.tickets t WHERE t.id = '{ticket_id}';"))

        admin, server = board("board", declared=not setting_only)
        fixture.seed_postgres_ticket(admin, "MEFP-9", title="MEFP-9", state="dat", assignee="director", commit_exempt=True)
        seen["help"] = [" ".join(subprocess.run([sys.executable, str(root / "scripts" / "ticket-board-write"), verb, "--help"],
                                                capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "COLUMNS": "400"}).stdout.split())
                        for verb in ("set-blockers", "release-external-blocker")]
        seen["set"] = cli(server, "director", "set-blockers", "MEFP-9", "--blocked-by", "Operator:Eric", "--blocked-reason", REASON)
        seen["after_set"] = ticket(admin, "MEFP-9")
        if setting_only:
            server.shutdown()
            server.server_close()
            return seen

        from scripts.ticket_board.notify_listener import ActivityTrace, PaneActivityGate, PaneHookStateStore, TicketBoardNotifyListener
        from scripts.ticket_board.peer_identity import read_process
        from tmux_bus_isolation import isolate_tmux_bus
        import psycopg

        isolate_tmux_bus()

        class Gate(PaneActivityGate):
            """The real gate over real hook files; only what it would read from tmux and /proc is the scenario's."""

            def _full_activity_trace(self, target, check_trusted_working=False):
                return ActivityTrace(False, "scenario_idle")

            def background_work_roles(self, roles=None):
                return set()

            def composer_snapshot(self, target):
                return None

        for other in ("MEFP-10", "MEFP-11", "MEFP-12", "MEFP-13", "MEFP-14", "MEFP-15"):
            fixture.seed_postgres_ticket(admin, other, title=other, state="dat", assignee="director", commit_exempt=True)
        # Nobody but the Director records it, and nobody gains authority from it.
        seen["main_sets"] = cli(server, "main", "set-blockers", "MEFP-10", "--blocked-by", "operator:eric", "--blocked-reason", REASON)
        seen["user_releases"] = cli(server, "user", "release-external-blocker", "MEFP-9", "--ref", "operator:eric", "--reason", "done")
        seen["operator_acts"] = cli(server, "operator", "add-comment", "MEFP-9", "--text", "I am Eric.")
        # The gate it waits in still holds.
        seen["sign_off_while_blocked"] = cli(server, "director", "workflow-action", "MEFP-9", "director_dat_sign_off")
        # A wait on the User (SYRD-549) is the other form: it lapses after its window, the blocker does not.
        seen["user_wait"] = cli(server, "director", "request-dependency", "MEFP-11", "--role", "user", "--reason", REASON)

        # Removing it any way but the release.
        for t in ("MEFP-10", "MEFP-12"):
            cli(server, "director", "set-blockers", t, "--blocked-by", "operator:eric", "--blocked-reason", REASON)
        seen["replaced"] = cli(server, "director", "set-blockers", "MEFP-10", "--blocked-by", "MEFP-11", "--blocked-reason", "Another wait.")
        seen["after_replaced"] = ticket(admin, "MEFP-10")
        seen["cleared"] = api(server, "director", "MEFP-12", "set_blockers", {"blocked_by": [], "blocked_reason": ""})
        seen["after_cleared"] = ticket(admin, "MEFP-12")
        seen["kept_and_added"] = cli(server, "director", "set-blockers", "MEFP-12", "--blocked-by", "Operator:Eric",
                                     "--blocked-by", "MEFP-11", "--blocked-reason", REASON)
        seen["after_kept_and_added"] = ticket(admin, "MEFP-12")
        # The database alone, as the service: the API normalises references first, and the database must not rely on it.
        service = fixture.conninfo(cluster.socket_dir, cluster.port, "board", fixture.SERVICE_ROLE)
        try:
            fixture.psql(service, "SET ticket_board.caller_role = 'director'; SELECT ticket_board.set_blockers('MEFP-12', "
                                  f"ARRAY[' Operator:ERIC ', 'MEFP-11'], '{REASON}');")
            seen["sql_kept"] = "ok"
        except AssertionError as refused:
            seen["sql_kept"] = " ".join(str(refused).split())[:300]
        seen["after_sql_kept"] = ticket(admin, "MEFP-12")["blocked_by"]
        # Another board's work is the same kind of wait; a blocker on this board resolves itself and may be dropped.
        cli(server, "director", "set-blockers", "MEFP-14", "--blocked-by", "syrd:SYRD-269", "--blocked-reason", "Waits on SYRD-269.")
        seen["cross_board_cleared"] = api(server, "director", "MEFP-14", "set_blockers", {"blocked_by": [], "blocked_reason": ""})
        seen["after_cross_board_cleared"] = ticket(admin, "MEFP-14")["blocked_by"]
        cli(server, "director", "set-blockers", "MEFP-15", "--blocked-by", "MEFP-11", "--blocked-reason", "Waits on MEFP-11.")
        seen["local_cleared"] = api(server, "director", "MEFP-15", "set_blockers", {"blocked_by": [], "blocked_reason": ""})
        seen["after_local_cleared"] = ticket(admin, "MEFP-15")["blocked_by"]

        # Quiet: the Director's turns end, again and again, past the grace.
        store = PaneHookStateStore(panes)
        gate = Gate(state_store=store)
        sent: list[tuple[str, str]] = []
        listener_url = fixture.conninfo(cluster.socket_dir, cluster.port, "board", "ticket_board_listener")
        listener = TicketBoardNotifyListener(conninfo=listener_url, sender=lambda target, message: sent.append((target, message)),
                                             activity_gate=gate.is_working, target_exists=lambda _target: True,
                                             submission_witness=lambda _target, _since: True, poll_seconds=0, project="mefp")
        listener.unresolved_turn_grace_seconds = GRACE
        listener.idle_stall_grace_seconds = GRACE
        listener.idle_stall_nudge_cadence_seconds = GRACE
        sleepers = []
        for role in PANES:
            sleeper = subprocess.Popen(["sleep", "3600"])
            sleepers.append(sleeper)
            sql(admin, "INSERT INTO ticket_board.role_runtime_assignments (role, runtime, actual_target, worktree, session_dir, "
                       f"process_pid, process_start_time, process_uid) SELECT '{role}', r.definition->>'runtime', "
                       f"r.definition->>'target', '/w/{role}', '/s/{role}', {sleeper.pid}, "
                       f"{read_process(sleeper.pid).start_time}, {os.getuid()} FROM ticket_board.workflow_roles r "
                       f"WHERE r.name = '{role}';")
        targets = json.loads(sql(admin, "SELECT json_object_agg(role, actual_target) FROM ticket_board.role_runtime_assignments;"))

        def one_pass() -> None:
            with psycopg.connect(listener_url, autocommit=True) as conn:
                listener.refresh_workflow(conn)
                gate.role_targets = dict(listener.role_targets)
                for step in ("process_reminder_snooze_due", "process_idle_turn_end_nudges", "process_idle_stall_nudges",
                             "process_serial_focus_queue_wakeups"):
                    getattr(listener, step)(conn)
                listener.process_due_notifications(conn)

        def about(ticket_id: str) -> list[str]:
            return [m for _t, m in sent if m.startswith(f"{ticket_id} ") or f" {ticket_id} " in m or f"{ticket_id}:" in m]

        for role in PANES:
            store.write(targets[role], "idle", source="codex.Stop")
        one_pass()
        before_watch = len(sent)
        start = time.time()
        while time.time() - start < 2 * GRACE + 3:
            store.write(targets["director"], "busy", source="codex.UserPromptSubmit")
            time.sleep(0.05)
            store.write(targets["director"], "idle", source="codex.Stop")
            one_pass()
            time.sleep(1.0)
        seen["watched"] = [m[:160] for _t, m in sent[before_watch:] if m.startswith("MEFP-9") or "MEFP-9 " in m]
        seen["watched_all"] = [m[:160] for _t, m in sent[before_watch:]]
        # The control: MEFP-13 waits on nothing recorded, in the same stage, for the same owner.
        seen["control_prompted"] = any(m.startswith("MEFP-13 is still yours") for _t, m in sent[before_watch:])
        # Three days on, with every notice delivered: the blocker still holds the
        # ticket quiet, and a wait on the User has lapsed after its window.
        sql(admin, "UPDATE ticket_board.ticket_notification_state SET awaiting_since_at = awaiting_since_at - interval '5 hours' "
                   "WHERE ticket_id = 'MEFP-11';")
        seen["held_later"] = {t: sql(admin, f"SELECT ticket_board.ticket_turn_is_resolved('{t}', clock_timestamp() + interval '3 days')::text;")
                              for t in ("MEFP-9", "MEFP-11")}
        seen["user_wait_cleared"] = cli(server, "director", "clear-awaiting-role", "MEFP-11")

        # Eric answers: the release records it, tells the owner once, and the gate is the owner's again.
        mark = len(sent)
        seen["released"] = cli(server, "director", "release-external-blocker", "MEFP-9", "--ref", "operator:eric",
                               "--reason", "Eric: defer the diagnostic event test until the next outage window.")
        seen["after_release"] = ticket(admin, "MEFP-9")
        seen["release_comment"] = sql(admin, "SELECT text FROM ticket_board.ticket_comments WHERE ticket_id = 'MEFP-9' "
                                             "ORDER BY id DESC LIMIT 1;")
        for _ in range(3):
            one_pass()
        seen["release_notices"] = [m[:160] for _t, m in sent[mark:] if "MEFP-9" in m]
        seen["sign_off_after_release"] = cli(server, "director", "workflow-action", "MEFP-9", "director_dat_sign_off")
        seen["after_sign_off"] = ticket(admin, "MEFP-9")

        # A board with no declared workflow: the same wait.
        legacy_admin, legacy_server = board("legacy", declared=False)
        fixture.seed_postgres_ticket(legacy_admin, "MEFP-9", title="MEFP-9", state="dat", assignee="director", commit_exempt=True)
        fixture.seed_postgres_ticket(legacy_admin, "MEFP-10", title="MEFP-10", state="dat", assignee="director", commit_exempt=True)
        seen["legacy_set"] = cli(legacy_server, "director", "set-blockers", "MEFP-9", "--blocked-by", "operator:eric",
                                 "--blocked-reason", REASON)
        seen["legacy_after_set"] = ticket(legacy_admin, "MEFP-9")
        seen["legacy_main_sets"] = cli(legacy_server, "main", "set-blockers", "MEFP-10", "--blocked-by", "operator:eric",
                                       "--blocked-reason", REASON)
        seen["legacy_cleared"] = api(legacy_server, "director", "MEFP-9", "set_blockers", {"blocked_by": [], "blocked_reason": ""})
        seen["legacy_after_cleared"] = ticket(legacy_admin, "MEFP-9")
        seen["legacy_released"] = cli(legacy_server, "director", "release-external-blocker", "MEFP-9", "--ref", "operator:eric",
                                      "--reason", "Eric: go ahead.")
        seen["legacy_after_release"] = ticket(legacy_admin, "MEFP-9")

        # schema.sql and rbac.sql install what the migrations do; a board built on main takes the migration in place.
        shape = ("SELECT json_build_array(md5(pg_get_functiondef(p.oid)), pg_get_userbyid(p.proowner), coalesce(p.proacl::text, ''))::text "
                 "FROM pg_proc p WHERE p.oid = 'ticket_board.apply_blockers(text,text[],text)'::regprocedure;")
        seen["migrated_shape"] = json.loads(sql(admin, shape))
        fresh = fixture.conninfo(cluster.socket_dir, cluster.port, "fresh")
        fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "fresh"])
        fixture.psql(fresh, (root / "scripts/ticket_board/schema.sql").read_text())
        fixture.psql(fresh, (root / "scripts/ticket_board/rbac.sql").read_text())
        seen["fresh_shape"] = json.loads(sql(fresh, shape))
        if before_root is not None:
            upgraded = fixture.conninfo(cluster.socket_dir, cluster.port, "upgraded")
            fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "upgraded"])
            fixture.psql(upgraded, (before_root / "scripts/ticket_board/schema.sql").read_text())
            for tree in (before_root, root):
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


def main() -> int:
    clean_env()
    if len(sys.argv) >= 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), "syrd451b-", setting_only=sys.argv[3:] == ["--setting-only"])))
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd451.") as tmp:
        runs = {}
        for name, commit, extra in (("before", BEFORE, []), ("pre_operator", f"{BEFORE_OPERATOR_WAITS}^", ["--setting-only"])):
            tree = tree_at(commit, Path(tmp) / name)
            child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(tree), *extra],
                                   capture_output=True, text=True, env={**clean_env(), "PYTHONDONTWRITEBYTECODE": "1"})
            assert child.returncode == 0, child.stderr[-3000:]
            runs[name] = json.loads(child.stdout.strip().splitlines()[-1])
        after = scenario(ROOT, "syrd451a-", before_root=Path(tmp) / "before")
    before, pre = runs["before"], runs["pre_operator"]
    stands = "external blocker operator:eric stands until it is released: release-external-blocker"
    waiting = {"state": "dat", "assignee": "director", "manually_controlled": False, "blocked_reason": REASON,
               "blocked_by": ["operator:eric"], "comments": 0}

    check(pre["set"] == "invalid blocked_by ticket id: Operator:Eric" and pre["after_set"]["blocked_by"] == [],
          f"the release before SYRD-273 refuses the skill's form with exactly MEFP's message: {pre['set']}")
    check(before["set"] == after["set"] == "ok" and before["after_set"] == after["after_set"] == waiting,
          f"main and this tree record a person's wait on a Director-owned DAT ticket, with no fake ticket and no "
          f"manual hold: the ticket keeps its stage and owner: {after['after_set']}")
    check(after["main_sets"] == "main cannot call set_blockers" and after["legacy_main_sets"] == "main cannot call set_blockers"
          and after["user_releases"] == "user cannot call release_external_blocker"
          and after["operator_acts"] == "invalid caller role: operator",
          f"only the Director records or releases it, and the person named gains no authority: {after['user_releases']} "
          f"{after['operator_acts']}")
    check(after["sign_off_while_blocked"].startswith("unresolved blocker prevents forward promotion: operator:eric"),
          f"the gate the ticket waits in still holds: {after['sign_off_while_blocked'][:120]}")
    check(after["control_prompted"] and after["watched"] == [] and before["watched"] == [],
          f"while the Director's turns end, again and again, past the grace, nothing is sent about MEFP-9 -- no prompt, "
          f"no escalation -- while MEFP-13, waiting on nothing recorded, is prompted: {after['watched_all']}")
    check(before["held_later"] == after["held_later"] == {"MEFP-9": "true", "MEFP-11": "false"},
          f"three days on, the blocker still holds the ticket quiet, where a wait on the User (SYRD-549) has lapsed "
          f"after its window: {after['held_later']}")
    check(before["replaced"] == before["cleared"] == before["legacy_cleared"] == "ok"
          and before["after_replaced"]["blocked_by"] == ["MEFP-11"] and before["after_cleared"]["blocked_by"] == []
          and before["after_cleared"]["comments"] == 0 and before["legacy_released"].startswith("ticket MEFP-9 has no external blocker"),
          f"main: replacing or clearing the blockers drops the person's wait with no reason and no record, and the "
          f"release then has nothing to release: {before['after_replaced']} {before['after_cleared']}")
    check(after["replaced"].startswith(stands) and after["cleared"].count(stands) == 1
          and after["after_replaced"] == after["after_cleared"] == waiting,
          f"replacing or clearing the blockers must still name the person's wait, which stands, and the refusal names "
          f"the release: {after['replaced'][:160]}")
    check(after["cross_board_cleared"].count("external blocker syrd:SYRD-269 stands until it is released") == 1
          and after["after_cross_board_cleared"] == ["syrd:SYRD-269"] and before["after_cross_board_cleared"] == []
          and before["local_cleared"] == after["local_cleared"] == "ok" and after["after_local_cleared"] == [],
          f"another board's work stands the same way; a blocker on this board is still dropped freely: "
          f"{after['cross_board_cleared'][:120]} {after['after_local_cleared']}")
    check(after["sql_kept"] == "ok" and after["after_sql_kept"] == ["operator:eric", "MEFP-11"],
          f"the database alone reads the new list as the board normalises it: {after['sql_kept']}")
    check(after["kept_and_added"] == "ok" and after["after_kept_and_added"]["blocked_by"] == ["operator:eric", "MEFP-11"],
          f"another blocker is added beside it by naming both: {after['after_kept_and_added']}")
    check(after["released"] == "ok" and after["after_release"]["blocked_by"] == []
          and after["release_comment"] == "External blocker operator:eric released: Eric: defer the diagnostic event test "
                                          "until the next outage window."
          and after["sign_off_after_release"] == "ok" and after["after_sign_off"]["state"] == "director_review",
          f"the release records Eric's answer and the gate is the Director's to take again: {after['release_comment']}")
    check(after["legacy_set"] == "ok" and after["legacy_after_set"] == waiting
          and after["legacy_cleared"].count(stands) == 1 and after["legacy_after_cleared"] == waiting
          and after["legacy_released"] == "ok" and after["legacy_after_release"]["blocked_by"] == []
          and after["legacy_after_release"]["comments"] == 1,
          f"a board with no declared workflow: the same wait, the same refusal to drop it, the same release: "
          f"{after['legacy_after_release']}")
    check(all("operator:<name>" in text for text in after["help"]) and not any("operator" in text for text in before["help"]),
          f"the CLI's own help names the person form for both verbs: {[t[-220:] for t in after['help']]}")
    check(after["migrated_shape"] == after["fresh_shape"] == after["upgrade_shapes"][1]
          and after["upgrade_shapes"][0] != after["upgrade_shapes"][1],
          f"schema.sql installs what the migration does, and a board built on main takes it in place: {after['upgrade_shapes']}")
    print(f"operator_person_wait_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
