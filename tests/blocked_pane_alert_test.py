#!/usr/bin/env python3
"""SYRD-633: a pane whose hook says `blocked` is one episode, and the Director hears of it once.

Live syrd, 2026-10-20. App's response was stopped by its provider's safeguards
at 11:23:51Z, and 35 s later its pane hook wrote `blocked` from
`claude.Notification.permission_prompt`. Nothing replaced it until a Director
recovery prompt at 21:28Z. SYRD-234's generator escalated once, at 07:26 EDT --
on SYRD-624, App's lowest id, which was blocked on another ticket -- and the
listener dropped that as stale before the Director saw it. Its dedupe key, per
role and blocked instant, then kept every later pass silent, while SYRD-629's
activation (notification 7024) was requeued 111 times behind the pane. The
trace kept only `hook_blocked`, and two finished-ticket VMs under the pane
were never named.

Everything here is real except tmux's own answers and the send:
* a production-built board (schema.sql, the real ticket-board-migrate, rbac.sql)
  with the example workflow, its HTTP server, and its Unix socket with the
  production `ProcessRoleAuthority`;
* each pane a real process under a process named `tmux: server`, registered
  through the real `ticket-board-register-runtime`, which then execs the
  provider stand-in -- so the board decides who is registered;
* the leftover child a real loop the provider started in a session of its own
  from the finished ticket's worktree;
* the real notify listener, PaneActivityGate and hook-state store, sampling the
  real /proc, with the bound configured the way production configures it.

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
BEFORE = "457a84cf"  # main before SYRD-633
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
#: The configured short bound: production's TICKET_BOARD_PERMISSION_PROMPT_GRACE_SECONDS, here seconds not minutes.
GRACE = 4
GRACE_ENV = "TICKET_BOARD_PERMISSION_PROMPT_GRACE_SECONDS"
#: A blocked source outside SYRD-234's two.
UNKNOWN = "claude.StopFailure"
RULE = "─" * 100
CHECKS = 0

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
    if not condition:  # the label again, last, so a truncated tail still says which check failed
        label = re.split(r": [\[{(]", detail, maxsplit=1)[0]
        raise AssertionError(f"{detail}\nFAILED CHECK: {label}")
    CHECKS += 1


def alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat[stat.rfind(")") + 2:].split()[0] != "Z"


def ticket_worktree(path: Path) -> Path:
    """A real ticket worktree, cut by `git worktree add` from a repository beside it: only a worktree git registered
    names a child's ticket, never a directory's name alone (SYRD-636)."""
    repo = path.parent / ".repo"
    run = lambda *argv, cwd: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *argv], cwd=cwd,
                                             check=True, capture_output=True)
    if not repo.exists():
        repo.mkdir()
        run("init", "-q", cwd=repo)
        run("commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
    run("worktree", "add", "-q", "-b", path.name, str(path), cwd=repo)
    return path


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

    seen: dict = {}
    os.environ[GRACE_ENV] = str(GRACE)
    tmux_server = None
    jobs: list[int] = []
    with temporary_cluster(prefix="s633", shutdown="immediate") as cluster, \
            tempfile.TemporaryDirectory(prefix="syrd633.") as tmp:
        tmp_path = Path(tmp)
        state_dir, sessions, worktrees = tmp_path / "state", tmp_path / "sessions", tmp_path / "worktrees"
        for path in (state_dir, sessions, worktrees):
            path.mkdir()
        (tmp_path / "provider.py").write_text(PROVIDER)

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
            (worktrees / f"{role}-home").mkdir()
        panes = [[sys.executable, str(root / "scripts/ticket-board-register-runtime"), "--socket", str(sock), "--role", role,
                  "--runtime", "claude", "--target", targets[role], "--worktree", str(worktrees / f"{role}-home"),
                  "--session-dir", str(sessions), "--", sys.executable, str(tmp_path / "provider.py"),
                  str(tmp_path / f"ctl-{role}")] for role in roles]
        tmux_server = subprocess.Popen([sys.executable, "-c", TMUX_SERVER, json.dumps(panes), str(tmp_path / "panes.json")],
                                       stdin=subprocess.DEVNULL)

        def registered() -> dict:
            return json.loads(sql("SELECT coalesce(json_object_agg(role, json_build_array(process_pid, process_start_time, "
                                  "generation)), '{}')::text FROM ticket_board.role_runtime_assignments;"))

        deadline = time.time() + 30
        while (len(registered()) < 2 or not (tmp_path / "panes.json").exists()) and time.time() < deadline:
            time.sleep(0.1)
        pane_pids = dict(zip(roles, json.loads((tmp_path / "panes.json").read_text())))
        seen["registered"] = len(registered()) == 2

        def spawn(role: str, cwd: Path, command: str) -> int:
            ctl = tmp_path / f"ctl-{role}"
            request = ctl / f"spawn-{len(jobs):03d}.json"
            request.write_text(json.dumps({"cwd": str(cwd), "command": command}))
            for _ in range(200):
                if request.with_suffix(".pid").exists():
                    pid = int(request.with_suffix(".pid").read_text())
                    jobs.append(pid)
                    return pid
                time.sleep(0.05)
            raise AssertionError("the provider did not start the job")

        tmux_calls: list[list[str]] = []

        def tmux(argv, **_kwargs):
            tmux_calls.append(list(argv))
            target = argv[argv.index("-t") + 1] if "-t" in argv else ""
            if target not in targets.values():
                raise AssertionError(f"no fake pane for {target!r}: {argv}")
            if argv[:2] == ["tmux", "capture-pane"]:  # Claude at its empty ready prompt: no question on screen
                lines = ["", "  the last answer, finished", "", RULE, "❯\xa0", RULE, "  ⏵⏵ status"] + [""] * 13
                return subprocess.CompletedProcess(argv, 0, "".join(line + "\n" for line in lines), "")
            if argv[:2] == ["tmux", "display-message"]:
                fmt = argv[-1]
                if "cursor_x" in fmt:
                    return subprocess.CompletedProcess(argv, 0, "2 4 20\n", "")
                if fmt == "#{pane_pid}":
                    return subprocess.CompletedProcess(argv, 0, f"{pane_pids[target[4:-4]]}\n", "")
                return subprocess.CompletedProcess(argv, 0, "0\n", "")
            raise AssertionError(f"unexpected tmux call {argv}")  # anything that could type into or interrupt a pane

        store = nl.PaneHookStateStore(state_dir)
        sends: list[tuple[str, str]] = []

        def hook(target: str, state: str, source: str, *, at: float | None = None) -> float:
            """A pane hook write, as the installed hook makes it: a turn start dated, the last SessionStart carried."""
            path = state_dir / (target.replace(":", "_") + ".json")
            previous = json.loads(path.read_text()) if path.exists() else {}
            written = time.time() if at is None else at
            record = {"target": target, "state": state, "updated_at": written, "source": source}
            if state == "busy" and source.endswith(".UserPromptSubmit"):
                record["turn_started_at"] = written
            elif "turn_started_at" in previous:
                record["turn_started_at"] = previous["turn_started_at"]
            if "last_session_start" in previous:
                record["last_session_start"] = previous["last_session_start"]
            path.write_text(json.dumps(record))
            return written

        def sender(target: str, message: str) -> dict:
            sends.append((target, message))
            hook(target, "busy", "claude.UserPromptSubmit")  # the notice starts a turn
            return {}

        listener = None

        def restart() -> None:
            """A new listener and gate: nothing remembered but what the board and the hook files keep."""
            nonlocal listener
            gate = nl.PaneActivityGate(state_store=store, client_activity_runner=tmux, cursor_position_runner=tmux,
                                       capture_pane_runner=tmux, pane_pid_runner=tmux, sleeper=time.sleep,
                                       role_runtimes={role: "claude" for role in roles})
            listener = nl.TicketBoardNotifyListener(
                conninfo=listener_url, project="pgu", sender=sender, activity_gate=gate.is_working,
                target_exists=lambda target: target in targets.values(), submission_witness=lambda *_a: True,
                poll_seconds=0)
            listener._wait_for_notification = lambda _conn: False

        director_free = [True]

        def one_pass() -> None:
            if director_free[0]:
                hook(targets["director"], "idle", "claude.Stop")  # whatever the last alert started is over
            sql("UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() "
                "WHERE dead_lettered_at IS NULL;")  # the requeue delays, elapsed
            listener.listen_once(max_notifications=20)

        def route(ticket: str, role: str) -> None:
            fixture.seed_postgres_ticket(admin, ticket, title=ticket, state="analysis", assignee="director", commit_exempt=True)
            fixture.post_json(base, f"/api/tickets/{ticket}/actions/route", {"state": "in_progress", "assignee": role},
                              caller="director")

        def finish(ticket: str) -> None:
            fixture.post_json(base, f"/api/tickets/{ticket}/actions/force_move", {"state": "done", "assignee": "unassigned"},
                              caller="director")

        def blocked(source: str, *, at: float | None = None) -> float:
            """app takes a prompt, and its hook then writes `blocked` from this source."""
            hook(targets["app"], "busy", "claude.UserPromptSubmit")
            time.sleep(0.2)
            return hook(targets["app"], "blocked", source, at=at)

        def session_start() -> None:
            """app's SessionStart, as the hook records it: its session and its own provider process."""
            stat = Path(f"/proc/{pane_pids['app']}/stat").read_text()
            start_ticks = int(stat[stat.rfind(")") + 2:].split()[19])
            hook(targets["app"], "idle", "claude.SessionStart")
            path = state_dir / "pgu-app_0.0.json"
            record = json.loads(path.read_text())
            record["last_session_start"] = {"session_id": "S-app-1", "source": "resume", "at": time.time(),
                                            "pane_pid": pane_pids["app"], "pane_start_time": start_ticks}
            path.write_text(json.dumps(record))

        def idle() -> None:
            hook(targets["app"], "idle", "claude.Stop")

        def to_app() -> list:
            return [m for t, m in sends if t == targets["app"]]

        def sent_to_app(ticket: str) -> list:
            return [m for t, m in sends if t == targets["app"] and m.startswith(f"{ticket} -- ")]

        def held(ticket: str) -> list:
            return json.loads(sql("SELECT coalesce(json_agg(json_build_array(id, kind, coalesce(last_error, '')) ORDER BY id), "
                                  "'[]')::text FROM ticket_board.ticket_notification_queue "
                                  f"WHERE ticket_id = '{ticket}' AND target_role = 'app';"))

        def told() -> list:
            """Every notice about app's pane the Director was sent, or still waits to be: [ticket, kind, message]."""
            return json.loads(sql(
                "SELECT coalesce(json_agg(json_build_array(ticket_id, detail->'payload'->>'kind', "
                "coalesce(detail->'payload'->>'message', ''), id) ORDER BY id), '[]')::text "
                "FROM ticket_board.notification_trace WHERE event = 'enqueue' AND target_role = 'director' "
                "AND detail->'payload'->>'kind' IN ('blocked_pane', 'escalation', 'background_hold', 'background_child');"))

        def director_sent() -> list:
            return [m for t, m in sends if t == targets["director"]]

        def fate(notice_kind: str) -> list:
            """What happened to each Director notice of this payload kind after it was enqueued."""
            return json.loads(sql(
                "SELECT coalesce(json_agg(json_build_array(t.ticket_id, t.event, coalesce(t.busy_reason, '')) ORDER BY t.id), "
                "'[]')::text FROM ticket_board.notification_trace t WHERE t.target_role = 'director' "
                "AND t.event IN ('send', 'drop', 'discard') AND t.notification_id IN (SELECT e.notification_id FROM "
                "ticket_board.notification_trace e WHERE e.event = 'enqueue' "
                f"AND e.detail->'payload'->>'kind' = '{notice_kind}');"))

        def defers(ticket: str) -> list:
            return json.loads(sql(
                "SELECT coalesce(json_agg(json_build_array(busy_reason, detail->'anti_clobber') ORDER BY id), '[]')::text "
                f"FROM ticket_board.notification_trace WHERE ticket_id = '{ticket}' AND event = 'gate_defer';"))

        def episodes() -> list:
            if sql("SELECT to_regclass('ticket_board.blocked_pane_episodes') IS NOT NULL;") != "t":
                return []
            return json.loads(sql(
                "SELECT coalesce(json_agg(json_build_array(source, classification, alerted_at IS NOT NULL, "
                "coalesce(clear_reason, 'open')) ORDER BY id), '[]')::text FROM ticket_board.blocked_pane_episodes;"))

        def escalations(ticket: str) -> list:
            return json.loads(sql("SELECT coalesce(json_agg(busy_reason ORDER BY id), '[]')::text FROM "
                                  f"ticket_board.notification_trace WHERE ticket_id = '{ticket}' AND event = 'gate_escalate';"))

        def story(ticket: str) -> list:
            """What happened to the ticket's notices, in order: a failing check's own evidence."""
            return json.loads(sql("SELECT coalesce(json_agg(json_build_array(notification_id, target_role, kind, event, "
                                  "coalesce(busy_reason, '')) ORDER BY id), '[]')::text FROM ticket_board.notification_trace "
                                  f"WHERE ticket_id = '{ticket}' AND event NOT IN ('claim', 'listener_claim', 'listener_ack');"))

        def wait_past(since: float) -> None:
            time.sleep(max(0.0, since + GRACE + 0.5 - time.time()))

        try:
            restart()
            session_start()
            # 1. The acceptance shape. PGU-627 goes to app, whose turn starts a
            #    long-lived child from the ticket's worktree, and ends. PGU-627 is
            #    finished. Then app's hook writes `blocked` from a source outside
            #    SYRD-234's two, the listener restarts, and PGU-629 goes to app.
            route("PGU-627", "app")
            one_pass()
            seen["first_delivered"] = sent_to_app("PGU-627")
            wt627 = ticket_worktree(worktrees / "pgu-627-app")
            # Busy the whole time, as a VM is: a loop that only sleeps is sometimes sampled between its sleeps,
            # read as stale and let past without the lease (SYRD-583 measured it), which is not what is tested here.
            vm = spawn("app", wt627, "while :; do :; done  # disposable VM for pgu-627")
            time.sleep(1.5)
            idle()
            time.sleep(1.5)
            finish("PGU-627")
            one_pass()
            since = blocked(UNKNOWN)
            sends_at_block = len(to_app())
            restart()
            route("PGU-629", "app")
            one_pass()
            seen["inside_bound"] = {"held": held("PGU-629"), "sent": sent_to_app("PGU-629"), "told": told(),
                                    "defers": defers("PGU-629"), "episodes": episodes()}
            wait_past(since)
            for n in range(3):
                one_pass()
                if n == 1:
                    restart()
            seen["past_bound"] = {"held": held("PGU-629"), "sent": sent_to_app("PGU-629"), "told": told(),
                                  "director_sent": director_sent(), "episodes": episodes(), "vm_alive": alive(vm),
                                  "vm_pgid": os.getpgid(vm), "fate": fate("blocked_pane"),
                                  "provider": pane_pids["app"], "to_app_while_blocked": to_app()[sends_at_block:]}
            # 2. The Director's recovery: app's next turn ends with a fresh idle hook.
            idle()
            for _ in range(3):
                one_pass()
                idle()  # the turn the notice starts ends
            seen["recovered"] = {"held": held("PGU-629"), "sent": sent_to_app("PGU-629"), "told": told(),
                                 "episodes": episodes(), "vm_alive": alive(vm), "escalations": escalations("PGU-629"),
                                 "fate": fate("blocked_pane"), "story": story("PGU-629")}
            os.killpg(vm, signal.SIGTERM)
            finish("PGU-629")
            one_pass()
            idle()

            # 3. The live shape. app works on PGU-625, with PGU-624 queued behind
            #    it and then blocked on another ticket, and PGU-631 queued too. Its
            #    hook writes `blocked` from a known permission prompt and nothing
            #    replaces it. PGU-625 is finished, and PGU-631's activation waits
            #    behind the pane.
            fixture.seed_postgres_ticket(admin, "PGU-600", title="PGU-600", state="analysis", assignee="director",
                                         commit_exempt=True)
            route("PGU-625", "app")
            one_pass()
            idle()
            route("PGU-624", "app")
            fixture.post_json(base, "/api/tickets/PGU-624/actions/set_blockers",
                              {"blocked_by": ["PGU-600"], "blocked_reason": "Waits on PGU-600."}, caller="director")
            route("PGU-631", "app")
            one_pass()
            told_before = len(told())
            since = blocked("claude.Notification.permission_prompt")
            sends_at_block = len(to_app())
            wait_past(since)
            one_pass()
            one_pass()
            finish("PGU-625")
            for _ in range(3):
                one_pass()
            seen["live_shape"] = {"held": held("PGU-631"), "sent": sent_to_app("PGU-631"), "told": told()[told_before:],
                                  "episodes": episodes(), "fate": fate("blocked_pane") + fate("escalation"),
                                  "defers": defers("PGU-631"), "to_app_while_blocked": to_app()[sends_at_block:],
                                  "story": story("PGU-631") + story("PGU-624"),
                                  "tickets": json.loads(sql("SELECT json_agg(json_build_array(id, state, assignee, "
                                                            "queued_for_assignee) ORDER BY id)::text FROM ticket_board.tickets "
                                                            "WHERE id IN ('PGU-624', 'PGU-625', 'PGU-631');"))}
            idle()
            for _ in range(2):
                one_pass()
                idle()
            seen["live_recovered"] = {"held": held("PGU-631"), "sent": sent_to_app("PGU-631"), "episodes": episodes()}
            if not full:
                return seen

            # 4. A record restored from before the provider now in the pane started.
            started = nl.PaneActivityGate(state_store=store, pane_pid_runner=tmux).provider_started_at(targets["app"])
            told_before = len(told())
            blocked("codex.PermissionRequest", at=(started or time.time()) - 600)
            one_pass()
            seen["restored"] = {"told": told()[told_before:], "episodes": episodes(), "fate": fate("blocked_pane")}
            idle()
            one_pass()

            # 5. A blocked hook released before the Director's pane is free: the alert is withdrawn.
            director_free[0] = False
            hook(targets["director"], "busy", "claude.UserPromptSubmit")
            told_before, sent_before = len(told()), len(director_sent())
            since = blocked(UNKNOWN)
            wait_past(since)
            one_pass()
            waiting = json.loads(sql("SELECT coalesce(json_agg(message), '[]')::text FROM ticket_board.ticket_notification_queue "
                                     "WHERE target_role = 'director' AND payload->>'kind' = 'blocked_pane';"))
            idle()
            one_pass()
            director_free[0] = True
            one_pass()
            seen["withdrawn"] = {"told": told()[told_before:], "waiting": waiting, "director_sent": director_sent()[sent_before:],
                                 "episodes": episodes(), "fate": fate("blocked_pane")}

            # 6. Blocked again, at a new instant: a new episode, told once more.
            told_before = len(told())
            since = blocked(UNKNOWN)
            wait_past(since)
            one_pass()
            first = blocked(UNKNOWN)
            wait_past(first)
            one_pass()
            one_pass()
            seen["replaced"] = {"told": told()[told_before:], "episodes": episodes()}
            idle()
            one_pass()

            seen["tmux_verbs"] = sorted({" ".join(call[:2]) for call in tmux_calls})
            seen["grantees"] = {name: sql("SELECT coalesce(string_agg(DISTINCT CASE WHEN a.grantee = 0 THEN 'PUBLIC' "
                                          "ELSE a.grantee::regrole::text END, '+'), '') "
                                          f"FROM pg_proc p, aclexplode(p.proacl) a WHERE p.oid = 'ticket_board.{name}'::regprocedure "
                                          "AND a.privilege_type = 'EXECUTE' AND a.grantee <> p.proowner;")
                                for name in ("note_blocked_panes(jsonb, interval)", "close_blocked_pane(bigint, text)")}
            if os.environ.get("SYRD633_MUTATION_SKIP_COPY_PARITY") != "1":
                fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "fresh"])
                fresh = fixture.conninfo(cluster.socket_dir, cluster.port, "fresh")
                fixture.psql(fresh, (root / "scripts/ticket_board/schema.sql").read_text())
                same = []
                for name in ("note_blocked_panes(jsonb, interval)", "close_blocked_pane(bigint, text)"):
                    definition = f"SELECT md5(pg_get_functiondef('ticket_board.{name}'::regprocedure));"
                    same.append(fixture.psql(fresh, definition).strip() == sql(definition))
                columns = ("SELECT string_agg(column_name || ' ' || data_type, ', ' ORDER BY ordinal_position) "
                           "FROM information_schema.columns WHERE table_schema = 'ticket_board' "
                           "AND table_name = 'blocked_pane_episodes';")
                indexes = ("SELECT string_agg(indexdef, '; ' ORDER BY indexname) FROM pg_indexes "
                           "WHERE schemaname = 'ticket_board' AND tablename = 'blocked_pane_episodes';")
                same.append(fixture.psql(fresh, columns).strip() == sql(columns) != "")
                same.append(fixture.psql(fresh, indexes).strip() == sql(indexes) != "")
                seen["fresh_equals_migrated"] = same
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


def units() -> None:
    """The pure parts: what a record is classified as, and that the trace keeps it."""
    sys.path.insert(0, str(ROOT))
    from scripts.ticket_board import blocked_pane as bp
    from scripts.ticket_board import notify_listener as nl
    from scripts.ticket_board.notification_activity_hold import BlockedHoldTrace

    class Identity:
        pid, start_time = 40, 900

    class Gate:
        role_identities = {"app": Identity()}

    def state(source: str, updated: float, start: dict | None = None) -> nl.PaneHookState:
        return nl.PaneHookState("pgu-app:0.0", "blocked", updated, source, last_session_start=start)

    ours = {"session_id": "S1", "pane_pid": 40, "pane_start_time": 900}
    theirs = {"session_id": "S0", "pane_pid": 39, "pane_start_time": 800}
    got = {
        "claude prompt": bp._classify(Gate(), "app", state("claude.Notification.permission_prompt", 2000.0, ours), 1000.0)[0],
        "codex prompt": bp._classify(Gate(), "app", state("codex.PermissionRequest", 2000.0), None)[0],
        "unknown": bp._classify(Gate(), "app", state(UNKNOWN, 2000.0, ours), 1000.0)[0],
        "before the provider": bp._classify(Gate(), "app", state("codex.PermissionRequest", 900.0, ours), 1000.0)[0],
        "another process's session": bp._classify(Gate(), "app", state("claude.Notification.permission_prompt", 2000.0,
                                                                         theirs), 1000.0)[0],
    }
    check(got == {"claude prompt": "permission_prompt", "codex prompt": "permission_prompt", "unknown": "unknown_source",
                  "before the provider": "restored", "another process's session": "restored"},
          f"a record is a prompt only from a known source and the live provider; restored when it predates it: {got}")
    class Conn:
        sent: list = []

        def execute(self, statement, params=None):
            self.sent.append(json.loads(params[0]))
            return type("Row", (), {"fetchone": lambda _self: (0,)})()

    class Log:
        def warning(self, *_a):
            pass

        info = warning

    class Panes:
        role_targets = {"app": "pgu-app:0.0", "main": "pgu-main:0.0", "director": "pgu-director:0.0"}

        class state_store:
            @staticmethod
            def read(target):
                if target == "pgu-main:0.0":
                    raise RuntimeError("unreadable")
                return None

    conn = Conn()
    bp.process(type("Owner", (), {"logger": Log()})(), conn, Panes(), 120.0)
    check(conn.sent == [{"observed": ["app"], "blocked": {}}],
          f"a pane that cannot be read is not reported, so its episode is neither opened nor closed: {conn.sent}")
    trace = BlockedHoldTrace(True, "hook_blocked", "", (), {"source": UNKNOWN})
    check(trace.reason == "hook_blocked" and trace.busy and trace.blocked == {"source": UNKNOWN},
          f"the blocked hold keeps its reason and carries its record: {trace}")


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
    if len(sys.argv) == 2 and sys.argv[1] == "--dump":
        print(json.dumps(scenario(ROOT, full=True), default=str, indent=1))
        return 0
    after = json.loads(json.dumps(scenario(ROOT, full=True), default=str))
    if os.environ.get("SYRD633_MUTATION_SKIP_BEFORE") != "1":
        before_run()
    candidate(after)
    units()
    print(f"blocked_pane_alert_test: {CHECKS} checks ok")
    return 0


def before_run() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd633.") as tmp:
        before_root = tree_at(BEFORE, Path(tmp) / "before")
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(before_root)],
                               capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=900)
        assert child.returncode == 0, child.stderr[-3000:]
        before = json.loads(child.stdout.strip().splitlines()[-1])
    check(before["registered"] and len(before["first_delivered"]) == 1,
          f"before: the panes register themselves and PGU-627 reaches app: {before['first_delivered']}")
    past = before["past_bound"]
    check([kind for _id, kind, _e in past["held"]] == ["transition"] and past["sent"] == []
          and not any(kind == "blocked_pane" or "blocked" in message for _t, kind, message, _i in past["told"])
          and past["vm_alive"],
          f"before: PGU-629 is held behind an unknown blocked source, and the Director hears nothing of it: {past}")
    defer = before["inside_bound"]["defers"]
    check(len(defer) == 1 and defer[0][0] == "hook_blocked" and "blocked" not in defer[0][1] and "holders" not in defer[0][1],
          f"before: its trace keeps only `hook_blocked`: no source, and not the child under the pane: {defer}")
    recovered = before["recovered"]
    check(len(recovered["sent"]) == 1 and recovered["held"] == [],
          f"before: only a fresh idle hook releases it: {recovered}")
    live = before["live_shape"]
    escalated = [(t, kind) for t, kind, _m, _i in live["told"]]
    check(escalated == [("PGU-624", "escalation")] and live["fate"] == [["PGU-624", "drop", "stale_notification"]],
          f"before: a known prompt's one escalation goes on the lowest id, PGU-624, which is blocked, and is dropped: {live}")
    check([kind for _id, kind, _e in live["held"]] == ["transition"] and live["sent"] == [],
          f"before: and PGU-631's activation waits behind the pane with the Director never told: {live}")


def candidate(after: dict) -> None:
    check(after["registered"] and len(after["first_delivered"]) == 1,
          f"each pane registered itself and PGU-627 reached app: {after['first_delivered']}")
    inside = after["inside_bound"]
    check([kind for _id, kind, _e in inside["held"]] == ["transition"] and inside["sent"] == [] and inside["told"] == []
          and inside["episodes"] == [[UNKNOWN, "unknown_source", False, "open"]],
          f"inside the bound PGU-629 is held, one episode is open, and nobody is told yet: {inside}")
    defer = inside["defers"]
    record = defer[0][1].get("blocked", {}) if defer else {}
    past = after["past_bound"]
    check(len(defer) == 1 and defer[0][0] == "hook_blocked" and record.get("source") == UNKNOWN
          and record.get("classification") == "unknown_source" and record.get("session") == "S-app-1"
          and record.get("since") and record.get("provider_started_at")
          and [h["pgid"] for h in record.get("holders", [])] == [past["vm_pgid"]]
          and "pgu-627-app" in record["holders"][0]["cwd"] and record["holders"][0]["finished_turn"] is True,
          f"the trace keeps the reason, and what wrote it, when, its classification, session and the child under it: {defer}")
    alerts = [message for _t, kind, message, _i in past["told"] if kind == "blocked_pane"]
    notice = past["held"][0][0] if past["held"] else None
    message = alerts[0] if alerts else ""
    check(len(alerts) == 1 and [t for t, kind, _m, _i in past["told"]] == ["PGU-629"]
          and message.startswith("app's pane hook has said `blocked` since ")
          and f"written by `{UNKNOWN}`" in message and "classified unknown_source" in message
          and f"1 ticket notice waits behind it: PGU-629 (notification {notice})." in message
          and f"pid {past['vm_pgid']} (process group {past['vm_pgid']})" in message
          and "left running by a finished turn, started for PGU-627, which is now done (unassigned)" in message
          and f"`kill -TERM -- -{past['vm_pgid']}`" in message and f"never the provider (pid {past['provider']})" in message
          and "Its screen shows its empty ready prompt and no question" in message
          and "`tmux capture-pane -p -t pgu-app:0.0`" in message and "`directorctl send pgu-app:0.0 <message>`" in message
          and "Nothing was typed into the pane" in message,
          f"past the bound the Director is told once: source, age, notice, child, its ticket, and the safe recovery: {past['told']}")
    check(past["director_sent"] == alerts and [f[:2] for f in past["fate"]] == [["PGU-629", "send"]],
          f"and that alert is delivered, through a listener restart, never dropped or repeated: {past}")
    check([kind for _id, kind, _e in past["held"]] == ["transition"] and past["sent"] == [] and past["vm_alive"]
          and past["to_app_while_blocked"] == [] and past["episodes"] == [[UNKNOWN, "unknown_source", True, "open"]],
          f"nothing is typed into the blocked pane, app is not nudged, nothing is stopped, the episode stays one: {past}")
    recovered = after["recovered"]
    check(recovered["held"] == [] and len(recovered["sent"]) == 1 and "entered Implementation" in recovered["sent"][0]
          and recovered["episodes"] == [[UNKNOWN, "unknown_source", True, "cleared"]] and recovered["vm_alive"]
          and recovered["fate"] == past["fate"],
          f"a fresh idle hook closes the episode and PGU-629 goes out once: {recovered}")
    check(recovered["escalations"] and set(recovered["escalations"]) == {"completed_ticket_child"}
          and len([m for _t, kind, m, _i in recovered["told"] if kind == "background_child"]) == 1,
          f"and goes out past the finished ticket's child by SYRD-583's lease, the Director told once about the child: {recovered}")

    live = after["live_shape"]
    alerts = [(t, m) for t, kind, m, _i in live["told"] if kind in ("blocked_pane", "escalation")]
    held = [n for n, kind, _e in live["held"] if kind == "transition"]
    # SYRD-631: app's "changed by director: blockers" notice about PGU-624 -- queued behind PGU-625, never handed
    # over, then blocked -- is dropped as stale rather than sent, so app's pane is not made busy by it and
    # PGU-625's repair prompt goes out before the prompt blocks the pane. Nothing waits when the alert is due,
    # so it is said on the blocked ticket app owns, and still delivered.
    stale = [s for s in live["story"] if s[2] == "ticket_update" and s[1] == "app"]
    check([s[3:] for s in stale] == [["enqueue", ""], ["drop", "stale_notification"], ["ack", ""]],
          f"app's notice about blocked, queued PGU-624 is dropped as stale, never sent: {stale}")
    check(len(alerts) == 1 and alerts[0][0] == "PGU-624" and "written by `claude.Notification.permission_prompt`" in alerts[0][1]
          and "classified permission_prompt" in alerts[0][1] and "No ticket notice is waiting for it." in alerts[0][1]
          and [f[:2] for f in live["fate"][1:]] == [["PGU-624", "send"]],
          f"a known prompt, beside a blocked ticket, is told once, on the ticket app owns, and delivered: {live}")
    record = live["defers"][0][1].get("blocked", {}) if live["defers"] else {}
    check(len(held) == 1 and live["sent"] == [] and live["to_app_while_blocked"] == []
          and live["defers"][0][0] == "hook_blocked" and record.get("classification") == "permission_prompt"
          and record.get("source") == "claude.Notification.permission_prompt"
          and live["episodes"][-1] == ["claude.Notification.permission_prompt", "permission_prompt", True, "open"],
          f"PGU-631's activation joins that episode, held and traced with its source, and nobody is told twice: {live}")
    released = after["live_recovered"]
    check(released["held"] == [] and len(released["sent"]) == 1 and "now your active ticket" in released["sent"][0]
          and released["episodes"][-1] == ["claude.Notification.permission_prompt", "permission_prompt", True, "cleared"],
          f"its release closes the episode and delivers the activation once: {released}")
    restored = after["restored"]
    check(len(restored["told"]) == 1 and "classified restored" in restored["told"][0][2]
          and "before the process now in the pane started" in restored["told"][0][2]
          and restored["episodes"][-1] == ["codex.PermissionRequest", "restored", True, "open"],
          f"a record older than the live provider is classified restored and told at once, its age already past the bound: {restored}")
    check(restored["told"][0][0] == "PGU-624" and restored["fate"][-1][:2] == ["PGU-624", "send"],
          f"said on PGU-624, which is blocked on another ticket, the alert is still delivered: {restored['fate']}")
    withdrawn = after["withdrawn"]
    check(len(withdrawn["waiting"]) == 1 and withdrawn["director_sent"] == []
          and withdrawn["episodes"][-1] == [UNKNOWN, "unknown_source", False, "cleared"]
          and withdrawn["fate"][-1][1:] == ["discard", "blocked_pane_cleared"],
          f"an alert still waiting when the pane is released is withdrawn, and the Director never hears of it: {withdrawn}")
    replaced = after["replaced"]
    check(len([1 for _t, kind, _m, _i in replaced["told"] if kind == "blocked_pane"]) == 2
          and [e[3] for e in replaced["episodes"][-2:]] == ["replaced", "open"],
          f"blocked from a later instant is a new episode, told once more: {replaced}")
    check(after["tmux_verbs"] == ["tmux capture-pane", "tmux display-message"],
          f"no tmux call could type into or interrupt a pane: {after['tmux_verbs']}")
    check(after["grantees"] == {"note_blocked_panes(jsonb, interval)": "ticket_board_listener",
                                "close_blocked_pane(bigint, text)": ""},
          f"only the listener may note blocked panes, and nobody may close one directly: {after['grantees']}")
    if os.environ.get("SYRD633_MUTATION_SKIP_COPY_PARITY") != "1":
        check(after["fresh_equals_migrated"] == [True, True, True, True],
              f"a schema.sql board installs exactly what the migration does: {after['fresh_equals_migrated']}")


if __name__ == "__main__":
    raise SystemExit(main())
