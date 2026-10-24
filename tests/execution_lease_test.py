#!/usr/bin/env python3
"""SYRD-616: a long command under a role's current turn is leased, and the Director hears of it once.

A role can start a test that loops forever. While it runs under the role's
current turn, nothing that watches later deliveries fires (SYRD-557, SYRD-583,
SYRD-636): the work looks like the role's own, and the ticket stays active for
good.

Everything here is real except tmux's own answers and the send:
* a production-built board (schema.sql, the real ticket-board-migrate, rbac.sql)
  with the example workflow, its HTTP server, and its Unix socket with the
  production `ProcessRoleAuthority`;
* each pane a real process under a process named `tmux: server`, registered
  through the real `ticket-board-register-runtime`, which then execs a provider
  stand-in that starts each command in a session of its own, as a provider's
  tool does, with the pane's environment;
* `switchyard-bounded` itself, from the tree under test;
* the real notify listener, PaneActivityGate and hook-state store, sampling the
  real /proc.

Four timings are scaled down from minutes: the observation threshold, through
its real setting (`TICKET_BOARD_LEASE_OBSERVE_SECONDS`), and the pass's minimum
age, interrupt grace and hard-deadline slack. The provider's start-up window
is kept as it is.

The before-run is main's code and SQL, in a child built from a git archive.
"""

from __future__ import annotations

import getpass
import io
import json
import os
import re
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "d24a15cf"  # main before SYRD-616
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
RULE = "─" * 100
CHECKS = 0
OBSERVE_SECONDS = 20
EXPIRED, STALE = "has run past its lease", "outlived the ticket it was started for"
#: Values that must never be recorded: after a secret-named flag in a script, and in a wrapped command's argv.
SECRETS = ("s3cr3t-616-a", "trailing-secret-616", "hunterpw", "6161616", "zzsecret", "alpha616", "beta616",
           "gamma616", "delta616", "epsilon616", "secret-path-616")

TMUX_SERVER = """
import json, os, signal, subprocess, sys, time
open('/proc/self/comm', 'w').write('tmux: server')
panes = [subprocess.Popen(argv, start_new_session=True) for argv in json.loads(sys.argv[1])]
open(sys.argv[2], 'w').write(json.dumps([p.pid for p in panes]))
def stop(*_):
    for p in panes:
        try: p.terminate()
        except OSError: pass
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
while True:
    try:
        while os.waitpid(-1, os.WNOHANG)[0]: pass
    except ChildProcessError: pass
    time.sleep(0.1)
"""

PROVIDER = """
import json, os, subprocess, sys, time
from pathlib import Path
ctl = Path(sys.argv[1])
if len(sys.argv) > 2:  # a daemon of its own, in a session of its own, as Codex starts its app-server (SYRD-589)
    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(86400)', sys.argv[2]], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
while True:
    for request in sorted(ctl.glob('spawn-*.json')):
        job = json.loads(request.read_text())
        child = subprocess.Popen(['bash', '-c', job['command']], cwd=job['cwd'], start_new_session=True,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        request.with_suffix('.pid').write_text(str(child.pid))
        request.unlink()
    try:
        while os.waitpid(-1, os.WNOHANG)[0]: pass
    except ChildProcessError: pass
    time.sleep(0.05)
"""


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        label = re.split(r": [\[{(]", detail, maxsplit=1)[0]
        raise AssertionError(f"{detail}\nFAILED CHECK: {label}")
    CHECKS += 1


def alive(pid: int, start: int | None = None) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    fields = stat[stat.rfind(")") + 2:].split()
    return fields[0] != "Z" and (start is None or int(fields[19]) == start)


def start_time(pid: int) -> int:
    stat = Path(f"/proc/{pid}/stat").read_text()
    return int(stat[stat.rfind(")") + 2:].split()[19])


def scenario(root: Path, full: bool) -> dict:
    for extra in (str(root), str(root / "tests"), str(ROOT / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import ticket_board_write_api_test as fixture
    from assigned_implementation_queue_test import build_board, workflow
    from scripts.ticket_board import notify_listener as nl
    from scripts.ticket_board.board_notifications import TicketBoardEventHub
    from scripts.ticket_board.local_peer_authority import ProcessRoleAuthority
    from scripts.ticket_board.server import TicketBoardUnixServer
    from temporary_cluster import temporary_cluster

    in_pass = [False]  # whether the lease pass is what is asking tmux
    try:
        from scripts.ticket_board import execution_lease_pass as lp
        lp.MIN_AGE_SECONDS, lp.OBSERVE_EVERY_SECONDS, lp.INTERRUPT_GRACE_SECONDS, lp.HARD_SLACK_SECONDS = 8.0, 1.0, 2.0, 40.0
        lease_pass = lp.run

        def watched(listener, conn):
            in_pass[0] = True
            try:
                return lease_pass(listener, conn)
            finally:
                in_pass[0] = False

        lp.run = watched
    except ImportError:  # main has no lease pass
        pass
    wrapper = root / "scripts" / "switchyard-bounded"
    seen: dict = {}
    tmux_server = None
    jobs: list[int] = []
    with temporary_cluster(prefix="s616", shutdown="immediate") as cluster, \
            tempfile.TemporaryDirectory(prefix="syrd616.") as tmp:
        tmp_path = Path(tmp)
        state_dir, sessions, scratch, homes = (tmp_path / name for name in ("state", "sessions", "scratch", "homes"))
        for path in (state_dir, sessions, scratch, homes):
            path.mkdir()
        (tmp_path / "provider.py").write_text(PROVIDER)
        os.environ["TICKET_BOARD_LEASE_OBSERVE_SECONDS"] = f"{OBSERVE_SECONDS}s"
        os.environ["TICKET_BOARD_PANE_STATE_DIR"] = str(state_dir)  # what a pane is given, so the wrapper finds it

        admin = build_board(fixture, cluster, root, "board")
        service = fixture.conninfo(cluster.socket_dir, cluster.port, "board", fixture.SERVICE_ROLE)
        app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                     database_url=service)
        server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = server.write_token
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        app.apply_workflow(workflow(root), expected_revision=0, dry_run=False, caller_role="director")
        sock = tmp_path / "board.sock"
        unix = TicketBoardUnixServer(sock, app, events=TicketBoardEventHub(app), director_notifier=fixture.QuietNotifier(),
                                     role_authority=ProcessRoleAuthority(app, getpass.getuser()))
        threading.Thread(target=unix.serve_forever, daemon=True).start()
        listener_url = fixture.conninfo(cluster.socket_dir, cluster.port, "board", "ticket_board_listener")

        def sql(statement: str) -> str:
            return fixture.psql(admin, statement).strip()

        roles = ("app", "director")
        targets = {role: f"pgu-{role}:0.0" for role in roles}
        for role in roles:
            (tmp_path / f"ctl-{role}").mkdir()
            (homes / f"{role}-home").mkdir()
        panes = [[sys.executable, str(root / "scripts/ticket-board-register-runtime"), "--socket", str(sock), "--role", role,
                  "--runtime", "claude", "--target", targets[role], "--worktree", str(homes / f"{role}-home"),
                  "--session-dir", str(sessions), "--", sys.executable, str(tmp_path / "provider.py"),
                  str(tmp_path / f"ctl-{role}")] + (["app-server-daemon-616"] if role == "app" else []) for role in roles]
        tmux_server = subprocess.Popen([sys.executable, "-c", TMUX_SERVER, json.dumps(panes), str(tmp_path / "panes.json")],
                                       stdin=subprocess.DEVNULL)

        def registered() -> dict:
            return json.loads(sql("SELECT coalesce(json_object_agg(role, json_build_array(process_pid, process_start_time, "
                                  "generation)), '{}')::text FROM ticket_board.role_runtime_assignments;"))

        deadline = time.time() + 30
        while (len(registered()) < 2 or not (tmp_path / "panes.json").exists()) and time.time() < deadline:
            time.sleep(0.1)
        pane_pids = dict(zip(roles, json.loads((tmp_path / "panes.json").read_text())))
        seen["registered"] = registered()
        provider = {role: (pid, start_time(pid)) for role, pid in pane_pids.items()}
        registered_at = time.time()

        def spawn(role: str, cwd: Path, command: str) -> tuple[int, int]:
            ctl = tmp_path / f"ctl-{role}"
            n = len(list(ctl.glob("spawn-*")))
            request = ctl / f"spawn-{n:03d}.json"
            request.write_text(json.dumps({"cwd": str(cwd), "command": command}))
            for _ in range(200):
                if request.with_suffix(".pid").exists():
                    pid = int(request.with_suffix(".pid").read_text())
                    jobs.append(pid)
                    time.sleep(0.2)
                    try:
                        return pid, start_time(pid)
                    except OSError:  # already gone: main has no switchyard-bounded to run
                        return pid, 0
                time.sleep(0.05)
            raise AssertionError("the provider did not start the job")

        def tmux(argv, **_kwargs):
            target = argv[argv.index("-t") + 1] if "-t" in argv else ""
            if target not in targets.values():
                raise AssertionError(f"no fake pane for {target!r}: {argv}")
            if argv[:2] == ["tmux", "capture-pane"]:
                lines = ["", "  the last answer, finished", "", RULE, "❯\xa0", RULE, "  ⏵⏵ status"] + [""] * 13
                return subprocess.CompletedProcess(argv, 0, "".join(line + "\n" for line in lines), "")
            if argv[:2] == ["tmux", "display-message"]:
                fmt = argv[-1]
                if "cursor_x" in fmt:
                    return subprocess.CompletedProcess(argv, 0, "2 4 20\n", "")
                if fmt == "#{pane_pid}":
                    return subprocess.CompletedProcess(argv, 0, f"{pane_pids[target[4:-4]]}\n", "")
                return subprocess.CompletedProcess(argv, 0, "0\n", "")
            raise AssertionError(f"unexpected tmux call {argv}")  # anything that could interrupt a pane

        store = nl.PaneHookStateStore(state_dir)
        sends: list[tuple[str, str]] = []
        captures: list[str] = []

        def sender(target: str, message: str) -> dict:
            sends.append((target, message))
            store.write(target, "busy", source="claude.UserPromptSubmit")
            return {}

        listener = None
        import logging

        failures: list[str] = []

        class Failures(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                if "lease" in record.getMessage().lower():
                    failures.append(record.getMessage())

        def restart() -> None:
            nonlocal listener

            def counting_tmux(argv, **kwargs):
                if argv[:2] == ["tmux", "capture-pane"] and in_pass[0]:
                    captures.append(argv[argv.index("-t") + 1])
                return tmux(argv, **kwargs)

            gate = nl.PaneActivityGate(state_store=store, client_activity_runner=counting_tmux,
                                       cursor_position_runner=counting_tmux, capture_pane_runner=counting_tmux,
                                       pane_pid_runner=counting_tmux, sleeper=time.sleep,
                                       role_runtimes={role: "claude" for role in roles})
            store.write(targets["director"], "idle", source="claude.Stop")
            listener = nl.TicketBoardNotifyListener(
                conninfo=listener_url, project="pgu", sender=sender, activity_gate=gate.is_working,
                target_exists=lambda target: target in targets.values(), submission_witness=lambda *_a: True,
                poll_seconds=0)
            listener._wait_for_notification = lambda _conn: False
            listener.logger.addHandler(Failures(level=logging.WARNING))

        def one_pass() -> None:
            sql("UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() "
                "WHERE dead_lettered_at IS NULL;")
            listener.listen_once(max_notifications=listener.delivered_count + 20)
            store.write(targets["director"], "idle", source="claude.Stop")

        def passes(seconds: float) -> None:
            until = time.time() + seconds
            while time.time() < until:
                one_pass()
                time.sleep(0.5)

        def route(ticket: str, role: str) -> None:
            fixture.seed_postgres_ticket(admin, ticket, title=ticket, state="analysis", assignee="director", commit_exempt=True)
            fixture.post_json(base, f"/api/tickets/{ticket}/actions/route", {"state": "in_progress", "assignee": role},
                              caller="director")

        def finish(ticket: str) -> None:
            fixture.post_json(base, f"/api/tickets/{ticket}/actions/force_move", {"state": "done", "assignee": "unassigned"},
                              caller="director")

        def director_heard() -> list:
            waiting = json.loads(sql("SELECT coalesce(json_agg(message ORDER BY id), '[]')::text "
                                     "FROM ticket_board.ticket_notification_queue WHERE target_role = 'director' "
                                     "AND payload->>'kind' IN ('execution_lease', 'background_child');"))
            return [m for t, m in sends if t == targets["director"] and (EXPIRED in m or STALE in m or "was handed" in m
                                                                         or "went out past" in m)] + waiting

        def about(pid: int, messages: list) -> list:
            return [m for m in messages if f"pid {pid} (process group {pid})" in m]

        def leases() -> dict:
            try:
                rows = json.loads(sql("SELECT coalesce(json_agg(json_build_object('id', id, 'pid', pid, 'state', state, "
                                      "'ticket', ticket_id, 'source', deadline_source, 'hard', hard, 'deadline', "
                                      "extract(epoch FROM deadline), 'ext', extensions, 'alert', alert_kind, 'cleared', "
                                      "alert_clear_reason, 'interrupt', interrupt, 'end', end_reason, 'evidence', "
                                      "jsonb_array_length(evidence))), '[]')::text FROM ticket_board.execution_leases;"))
            except AssertionError:
                return {}
            return {row["pid"]: row for row in rows}

        def story(ticket: str) -> list:
            return json.loads(sql("SELECT coalesce(json_agg(json_build_array(kind, event, coalesce(busy_reason, '')) "
                                  "ORDER BY id), '[]')::text FROM ticket_board.notification_trace WHERE ticket_id = '"
                                  + ticket + "' AND target_role = 'app' AND event NOT IN ('claim', 'listener_claim', "
                                  "'listener_ack');"))

        def leases_full() -> list:
            try:
                return json.loads(sql("SELECT coalesce(json_agg(json_build_object('command', command)), '[]')::text "
                                      "FROM ticket_board.execution_leases;"))
            except AssertionError:  # main has none
                return []

        def to_app(text: str) -> list:
            waiting = json.loads(sql("SELECT coalesce(json_agg(message ORDER BY id), '[]')::text FROM "
                                     "ticket_board.ticket_notification_queue WHERE target_role = 'app';"))
            return [m for t, m in sends if t == targets["app"] and text in m] + [m for m in waiting if text in m]

        try:
            restart()
            store.write(targets["app"], "idle", source="claude.Stop")
            sql("DO $$ BEGIN IF to_regclass('ticket_board.execution_leases') IS NOT NULL THEN "  # main has none
                "INSERT INTO ticket_board.execution_leases (role, child, pid, pgid, start_ticks, started_at, deadline, "
                "deadline_source, state, ended_at, end_reason) VALUES ('app', '1@1', 1, 1, 1, now() - interval '9 days', "
                "now() - interval '9 days', 'default', 'exited', now() - interval '8 days', 'long ago'); END IF; END $$;")
            # 1. PGU-700 is App's; its notice starts App's turn, which keeps running.
            route("PGU-700", "app")
            one_pass()
            seen["first"] = [m for t, m in sends if t == targets["app"]]
            while time.time() < registered_at + 12:  # past the provider's start-up, as real work is
                time.sleep(0.5)
            # 2. App's turn starts commands: an unbounded loop, a quiet suite with a declared budget, a short
            #    command, a loop that shrugs off SIGINT, and a loop under a hard deadline. Beside the panes,
            #    an unrelated service runs.
            # A quoted secret of two words inside a script, and one of three words as one argument of a wrapped command.
            # Secrets no flag names: a header and a JSON config as arguments of a wrapped command, and inside a script.
            loop = spawn("app", scratch, f"while :; do sleep 0.5; done # unbounded-616 --token '{SECRETS[0]} {SECRETS[1]}' "
                                         f"-H 'Authorization: Bearer {SECRETS[9]}'")
            secret_file = scratch / f"{SECRETS[10]}.txt"  # it exists: still no proof, as an option's value
            secret_file.write_text("")
            quiet = spawn("app", scratch, f"{sys.executable} {wrapper} --expect 10m --label 'quiet suite' -- bash -c "
                                          f"'sleep 1000.6171' --token={secret_file} "
                                          f"--password '{SECRETS[2]} {SECRETS[3]} {SECRETS[4]}' "
                                          f"-H 'Authorization: Bearer {SECRETS[5]} {SECRETS[6]}' "
                                          f"--config '{{\"password\": \"{SECRETS[7]} {SECRETS[8]}\"}}'")
            short = spawn("app", scratch, "sleep 6")  # seen by several passes, gone before it is old enough to record
            stubborn = spawn("app", scratch, "trap '' INT; while :; do sleep 0.5; done # stubborn-616")
            hard = spawn("app", scratch, f"{sys.executable} {wrapper} --deadline 5s --grace 1 -- bash -c 'while :; do :; done'")
            forged = spawn("app", scratch, "while :; do sleep 0.5; done # forged-hard-616")
            try:
                from scripts.ticket_board import execution_lease as lease_files
            except ImportError:  # main has no leases to declare
                lease_files = None
            if lease_files is not None:
                lease_files.declare(state_dir / "leases", forged[0], forged[1], deadline=time.time() + 3, hard=True,
                                    command="forged")  # a wrapper that claims a hard deadline and never enforces it
            director_loop = spawn("director", scratch, "while :; do sleep 0.5; done # director-616")
            decoy = spawn("app", scratch, "while :; do sleep 0.5; done # decoy-616")
            service_proc = subprocess.Popen(["sleep", "1000.6179"], start_new_session=True)
            jobs.append(service_proc.pid)
            spawned_at = time.time()
            children = {"loop": loop, "quiet": quiet, "short": short, "stubborn": stubborn, "hard": hard, "forged": forged,
                        "decoy": decoy}
            seen["children"] = {name: pid for name, (pid, _s) in children.items()} | {"service": service_proc.pid,
                                                                                      "director": director_loop[0]}
            passes(OBSERVE_SECONDS - 6)
            seen["inside"] = {"heard": director_heard(), "leases": leases()}
            passes(10)
            heard = director_heard()
            seen["expired"] = {"heard": heard, "per": {name: about(pid, heard) for name, (pid, _s) in children.items()},
                               "leases": leases(), "alive": {name: alive(*child) for name, child in children.items()},
                               "providers_alive": all(alive(*provider[r]) for r in roles), "captures": len(captures),
                               "status": get_json(base + "/api/execution-leases") if full else {}}
            seen["director_leased"] = director_loop[0] in leases()
            seen["daemon_leased"] = any("app-server-daemon-616" in str(row.get("command", "")) for row in leases_full())
            os.killpg(director_loop[0], signal.SIGKILL)  # it holds the Director's pane, which is not under test here
            if not full:
                return seen

            # 3. A restarted listener tells nobody again.
            restart()
            passes(3)
            seen["restarted"] = director_heard()
            # A new turn of App's clears the alerts it was given: the next wave is a new episode.
            store.write(targets["app"], "busy", source="claude.UserPromptSubmit")
            passes(2)
            seen["new_turn"] = leases()
            # 4. The Director extends the quiet suite, and the loop it was told about; App may not, and nobody
            #    may extend by more than four hours at a time.
            ids = {name: row["id"] for name, row in ((n, leases().get(p)) for n, (p, _s) in children.items()) if row}
            refused = fixture.post_json(base, "/api/tickets/actions/extend_lease",
                                        {"lease": ids["quiet"], "minutes": 30, "reason": "mine"}, caller="app", expect=403)
            passes(max(0.0, spawned_at + 45 - time.time()))  # past the forged hard deadline's slack
            try:
                too_long = fixture.post_json(base, "/api/tickets/actions/extend_lease",
                                             {"lease": ids["quiet"], "minutes": 999, "reason": "forever"},
                                             caller="director", expect=400)
            except AssertionError as exc:  # accepted: recorded for the check that names it
                too_long = {"accepted": str(exc)[:300]}
            before = leases()[quiet[0]]["deadline"]
            extended = fixture.post_json(base, "/api/tickets/actions/extend_lease",
                                         {"lease": ids["quiet"], "minutes": 30, "reason": "the suite is slow today"},
                                         caller="director")
            fixture.post_json(base, "/api/tickets/actions/extend_lease",
                              {"lease": ids["loop"], "minutes": 5, "reason": "let it finish its retries"}, caller="director")
            passes(2)
            seen["extended"] = {"refused": refused, "too_long": too_long, "response": extended, "before": before, "leases": leases(),
                                "quiet_alive": alive(*quiet), "heard": director_heard()}
            # The decoy's recorded leader no longer matches (its start tick, as if its pid were reused): the
            # interrupt sends nothing.
            sql(f"UPDATE ticket_board.execution_leases SET start_ticks = start_ticks + 1 WHERE pid = {decoy[0]};")
            fixture.post_json(base, "/api/tickets/actions/interrupt_lease",
                              {"lease": ids["decoy"], "reason": "a reused number"}, caller="director")
            # 5. The Director interrupts the loop that shrugs off SIGINT: only its group is stopped, and App is told.
            fixture.post_json(base, "/api/tickets/actions/interrupt_lease",
                              {"lease": ids["stubborn"], "reason": "it loops on PGU-700"}, caller="director")
            passes(10)
            seen["interrupted"] = {"leases": leases(), "alive": {name: alive(*child) for name, child in children.items()},
                                   "decoy_alive": alive(*decoy),
                                   "told_app": to_app("was interrupted by the director"),
                                   "providers_alive": all(alive(*provider[r]) for r in roles)}
            # 6. PGU-700 is finished while its commands live on; a CPU-bound one would hold App's pane.
            busy = spawn("app", scratch, "while :; do :; done # stale-616")
            passes(10)  # old enough to be recorded
            store.write(targets["app"], "idle", source="claude.Stop")
            time.sleep(1.5)
            told_before = set(director_heard())
            finish("PGU-700")
            passes(3)
            for _ in range(10):  # App takes what it is owed -- each notice a turn -- until nothing waits for it
                store.write(targets["app"], "idle", source="claude.Stop")
                time.sleep(1.5)
                one_pass()
                if sql("SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE target_role = 'app';") == "0":
                    break
            store.write(targets["app"], "idle", source="claude.Stop")
            time.sleep(1.5)
            route("PGU-701", "app")
            passes(10)  # one Director notice per pass: each starts the Director's turn, and the next pass ends it
            heard = director_heard()
            seen["stale"] = {"heard": [m for m in heard if m not in told_before], "busy": about(busy[0], heard), "loop": about(loop[0], heard),
                             "leases": leases(), "busy_alive": alive(*busy), "loop_alive": alive(*loop),
                             "701": [m for t, m in sends if t == targets["app"] and m.startswith("PGU-701 -- ")],
                             "story": story("PGU-701"),
                             "trace": sql("SELECT coalesce(string_agg(notification_id || ':' || event || ':' || coalesce(busy_reason, '') "
                                          "|| ':' || coalesce(detail->>'reason', detail->>'phase', ''), ' ' ORDER BY id), '') FROM "
                                          "ticket_board.notification_trace WHERE notification_id IN (SELECT alert_notification_id "
                                          "FROM ticket_board.execution_leases WHERE alert_kind = 'stale');"),
                             "waiting": sql("SELECT count(*) || ' ' || coalesce(string_agg(DISTINCT payload->>'kind', ','), '') "
                                            "FROM ticket_board.ticket_notification_queue WHERE target_role = 'director';"),
                             "records": sql("SELECT coalesce(string_agg(child || '=' || ticket_id, ' '), '') FROM "
                                            "ticket_board.background_child_alerts;")}
            seen["pass_failures"] = sorted(set(failures))
            # What the board kept: the lease rows, the status a Director reads, every notice sent or waiting, the trace.
            kept = {"leases": sql("SELECT coalesce(string_agg(command, ' | '), '') FROM ticket_board.execution_leases;"),
                    "status": json.dumps(get_json(base + "/api/execution-leases?all=1")),
                    "sent": json.dumps(sends),
                    "queued": sql("SELECT coalesce(string_agg(message || payload::text, ' | '), '') "
                                  "FROM ticket_board.ticket_notification_queue;"),
                    "trace": sql("SELECT coalesce(string_agg(detail::text, ' | '), '') FROM ticket_board.notification_trace;")}
            seen["secrets_kept"] = {name: [secret for secret in SECRETS if secret in text] for name, text in kept.items()}
            seen["masked_commands"] = [line for line in kept["leases"].split(" | ") if "[redacted]" in line]
            seen["service_leased"] = service_proc.pid in leases()
            seen["pruned"] = sql("SELECT count(*) FROM ticket_board.execution_leases WHERE child = '1@1';")
            new = ("observe_execution_lease(text, text, integer, integer, bigint, timestamptz, text, text, timestamptz, "
                   "timestamptz, text, boolean, jsonb)")
            seen["grantees"] = {name: sql("SELECT coalesce(string_agg(DISTINCT CASE WHEN a.grantee = 0 THEN 'PUBLIC' "
                                          "ELSE a.grantee::regrole::text END, '+'), '') FROM pg_proc p, aclexplode(p.proacl) a "
                                          f"WHERE p.oid = 'ticket_board.{name}'::regprocedure AND a.privilege_type = 'EXECUTE' "
                                          "AND a.grantee <> p.proowner;")
                                for name in (new, "extend_execution_lease(bigint, integer, text)",
                                             "request_execution_lease_interrupt(bigint, text)")}
            if os.environ.get("SYRD616_MUTATION_SKIP_COPY_PARITY") != "1":
                fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "fresh"])
                fresh = fixture.conninfo(cluster.socket_dir, cluster.port, "fresh")
                fixture.psql(fresh, (root / "scripts/ticket_board/schema.sql").read_text())
                definition = ("SELECT md5(string_agg(pg_get_functiondef(p.oid), '' ORDER BY p.proname)) FROM pg_proc p "
                              "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'ticket_board' "
                              "AND p.proname LIKE '%execution_lease%';")
                columns = ("SELECT string_agg(column_name || ' ' || data_type, ', ' ORDER BY ordinal_position) "
                           "FROM information_schema.columns WHERE table_schema = 'ticket_board' "
                           "AND table_name = 'execution_leases';")
                seen["fresh_equals_migrated"] = [fixture.psql(fresh, definition).strip() == sql(definition),
                                                 fixture.psql(fresh, columns).strip() == sql(columns) != ""]
        finally:
            for pid in jobs:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except OSError:
                    pass
            if tmux_server is not None:
                tmux_server.terminate()
                tmux_server.wait(timeout=10)
            unix.shutdown()
            unix.server_close()
            server.shutdown()
            server.server_close()
    return seen


def get_json(url: str) -> dict:
    import urllib.request

    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "examples", "tests"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def main() -> int:
    for key in [k for k in os.environ if k.startswith(PANE_ENV_PREFIXES)]:
        os.environ.pop(key)
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), full=False), default=str))
        return 0
    after = json.loads(json.dumps(scenario(ROOT, full=True), default=str))
    if os.environ.get("SYRD616_MUTATION_SKIP_BEFORE") != "1":
        before_run()
    candidate(after)
    print(f"execution_lease_test: {CHECKS} checks ok")
    return 0


def before_run() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd616.") as tmp:
        before_root = tree_at(BEFORE, Path(tmp) / "before")
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(before_root)],
                               capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=900)
        assert child.returncode == 0, child.stderr[-3000:]
        before = json.loads(child.stdout.strip().splitlines()[-1])
    expired = before["expired"]
    check(expired["heard"] == [] and expired["alive"]["loop"] and expired["alive"]["stubborn"],
          f"before: an unbounded loop runs past the threshold under App's turn and nobody tells the Director: {expired}")


def candidate(after: dict) -> None:
    check(len(after["first"]) == 1 and "entered Implementation" in after["first"][0],
          f"PGU-700 reaches App, whose turn then runs on: {after['first']}")
    inside = after["inside"]
    check(inside["heard"] == [], f"inside the threshold nobody is told anything: {inside['heard']}")
    expired = after["expired"]
    per, rows = expired["per"], expired["leases"]
    children = after["children"]
    check({name: len(told) for name, told in per.items()}
          == {"loop": 1, "quiet": 0, "short": 0, "stubborn": 1, "hard": 0, "forged": 0, "decoy": 1}
          and len(expired["heard"]) == 3,
          f"past the threshold the Director is told once about each unbounded command, and nothing else: {per}")
    loop = per["loop"][0] if per["loop"] else ""
    lease_id = rows[str(children["loop"])]["id"] if str(children["loop"]) in rows else 0
    check(loop.startswith("app's command has run past its lease: ") and f"lease {lease_id} on PGU-700" in loop
          and "running " in loop and "past the default observation threshold" in loop
          and "`bash -c [redacted]`" in loop and "CPU ticks" in loop
          and "evidence only" in loop and "Nothing was stopped." in loop
          and f"`ticket-board-read leases --role app`" in loop and f"`ticket-board-write extend-lease {lease_id}" in loop
          and f"`ticket-board-write interrupt-lease {lease_id}" in loop and "leave it running" in loop,
          f"the alert names the role, ticket, age, command, pid and group, the evidence, and every action: {loop}")
    check(all(expired["alive"][name] for name in ("loop", "quiet", "stubborn")) and expired["providers_alive"]
          and not expired["alive"]["hard"],
          f"nothing unbounded is killed and the providers stand; only the hard deadline's own loop is stopped: {expired['alive']}")
    quiet, hard = rows.get(str(children["quiet"])) or {}, rows.get(str(children["hard"])) or {}
    check(quiet.get("source") == "declared" and quiet.get("state") == "running" and hard.get("source") == "hard"
          and hard.get("hard") is True and hard.get("state") == "exited" and str(children["short"]) not in rows
          and expired["captures"] == 0,
          f"the quiet suite holds its declared budget; the hard run ended at its deadline; a short command is never "
          f"leased; and no pane text was read: {quiet} {hard} {expired['captures']}")
    status = expired["status"].get("leases") or []
    check({row["pid"] for row in status} >= {children["loop"], children["quiet"], children["stubborn"]}
          and all(row["role"] == "app" for row in status),
          f"the leases can be read without looking at any pane: {status}")
    check(sorted(after["restarted"]) == sorted(expired["heard"]),
          f"a restarted listener tells nobody again: {after['restarted']}")
    forged = rows.get(str(children["forged"])) or {}
    check(forged.get("source") == "hard" and forged.get("alert") is None and expired["alive"]["forged"],
          f"a hard deadline is its wrapper's to enforce: past it, within the slack, nobody is told: {forged}")
    turn = after["new_turn"]
    check(turn[str(children["stubborn"])]["cleared"] == "the role began another turn"
          and turn[str(children["loop"])]["cleared"] == "the role began another turn",
          f"a new turn clears the alerts of the last one: {turn}")

    ext = after["extended"]
    quiet_row = ext["leases"][str(children["quiet"])]
    loop_row = ext["leases"][str(children["loop"])]
    check(isinstance(ext["too_long"], str) and "1 minute to 4 hours" in ext["too_long"],
          f"nobody extends a lease by more than four hours at a time: {ext['too_long']}")
    check(isinstance(ext["refused"], str) and quiet_row["source"] == "extended"
          and 30 * 60 - 5 <= quiet_row["deadline"] - ext["before"] <= 30 * 60 + 5
          and quiet_row["ext"][-1]["by"] == "director" and quiet_row["ext"][-1]["reason"] == "the suite is slow today"
          and ext["response"]["lease"]["deadline_source"] == "extended" and ext["quiet_alive"],
          f"the Director extends a live lease durably, with who and why, without restarting it; App may not: {ext}")
    forged_pid = after["children"]["forged"]
    new = [m for m in ext["heard"] if m not in expired["heard"]]
    check(loop_row["state"] == "running" and loop_row["ext"][-1]["reason"] == "let it finish its retries"
          and all(f"pid {forged_pid} " in m for m in new),
          f"extending the expired loop puts it back to running, recorded, and tells nobody anything new: {loop_row} {new}")
    check(len(new) == 1 and "past its own hard deadline" in new[0],
          f"a hard deadline its wrapper never enforced is reported once its slack has passed: {new}")

    gone = after["interrupted"]
    stubborn_row = gone["leases"][str(children["stubborn"])]
    check(stubborn_row["state"] == "interrupted" and stubborn_row["interrupt"]["stage"] == "done"
          and "SIGTERM" in stubborn_row["interrupt"]["result"] and not gone["alive"]["stubborn"]
          and gone["alive"]["loop"] and gone["alive"]["quiet"] and gone["providers_alive"],
          f"the interrupt stops only that lease's group, escalating past its ignored SIGINT: {stubborn_row} {gone['alive']}")
    decoy_row = gone["leases"][str(children["decoy"])]
    check(gone["decoy_alive"] and decoy_row["state"] == "interrupted"
          and decoy_row["interrupt"]["result"] == "no signal; its leader was gone before any signal",
          f"an interrupt whose recorded leader no longer matches sends nothing: {decoy_row}")
    check(len(gone["told_app"]) == 2 and any("it loops on PGU-700" in m for m in gone["told_app"]),
          f"App is told once, after, who stopped it and why: {gone['told_app']}")

    check(after.get("pass_failures") == [], f"the lease pass never failed: {after.get('pass_failures')}")
    check(all(found == [] for found in after["secrets_kept"].values())
          and "bash -c [redacted]" in after["masked_commands"]
          and any("--token=[redacted]" in line and "--password [redacted]" in line and "-H [redacted]" in line
                  and "--config [redacted]" in line for line in after["masked_commands"]),
          f"no secret from argv is kept -- not in a lease, the status, a notice or the trace -- and both commands are "
          f"recorded masked: {after['secrets_kept']} {after['masked_commands']}")
    stale = after["stale"]
    busy_row = stale["leases"][str(after["children"]["loop"])]
    check(len(stale["busy"]) == 1 and STALE in stale["busy"][0] and "started" in stale["busy"][0]
          and "for PGU-700, which is now done" in stale["busy"][0] and stale["busy_alive"] and stale["loop_alive"]
          and busy_row["state"] == "stale",
          f"once PGU-700 is done its commands are stale; the Director is told once each; nothing is killed: {stale['heard']} "
          f"{ {pid: (row['state'], row['alert'], row['end']) for pid, row in stale['leases'].items()} } {stale['records']} WAITING {stale['waiting']} TRACE {stale['trace']}")
    check(len(stale["701"]) == 1 and len(stale["loop"]) == 2,
          f"the stale CPU-bound command holds nothing: PGU-701 is handed over, and no child is named twice: {stale}")
    check(after["service_leased"] is False and after["director_leased"] is False and after["daemon_leased"] is False,
          "an unrelated service beside the panes, the Director's own pane, and the provider's own daemon are never leased")
    check(after["pruned"] == "0", f"an ended lease older than a week is pruned: {after['pruned']}")
    grants = after["grantees"]
    check(list(grants.values()) == ["ticket_board_listener", "ticket_board_service", "ticket_board_service"],
          f"only the listener records leases; only the service, for the Director, extends or interrupts: {grants}")
    if os.environ.get("SYRD616_MUTATION_SKIP_COPY_PARITY") != "1":
        check(after["fresh_equals_migrated"] == [True, True],
              f"a schema.sql board installs exactly what the migration does: {after['fresh_equals_migrated']}")


if __name__ == "__main__":
    raise SystemExit(main())
