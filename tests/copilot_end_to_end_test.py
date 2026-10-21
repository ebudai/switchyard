#!/usr/bin/env python3
"""SYRD-634: a disposable end-to-end run of a real GitHub Copilot CLI as a Switchyard role pane.

Everything is the shipped path except the model. The real `copilot` runs in a
private tmux server with a throwaway HOME, started from the argv
`role_command.cli_command_for_role` builds for a role parsed by the launcher's
own config reader. Its hooks are the ones `ticket-board-install-pane-hooks`
writes, running the real `ticket-board-pane-idle-hook`. The model is a local
fake OpenAI-compatible server reached through Copilot's own offline
bring-your-own-key mode (`COPILOT_OFFLINE`), so no GitHub account, token or
model request leaves the host. The board is a stub that serves one in-progress
ticket.

Walked in one pane, in order:
folder trust (answered as a person would) -> a turn -> `/clear` -> a tool
permission prompt -> a 429 the CLI retries -> `/exit` -> a resumed relaunch ->
a missing session -> an unknown resume id.

Needs `copilot` (the standalone CLI, `copilot --version` = "GitHub Copilot CLI
...") and tmux. `SWITCHYARD_COPILOT_BIN` names the binary when it is not on
PATH. Without one this prints that it was skipped and exits 0, like the other
vendor-CLI suites.
"""

from __future__ import annotations

import http.server
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

for _key in [key for key in os.environ
             if key.startswith(("TICKET_BOARD_", "PGU_", "SWITCHYARD_TEST_", "COPILOT_")) or key in ("TMUX", "TMUX_PANE")]:
    del os.environ[_key]
ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

from scripts import copilot_runtime, role_command  # noqa: E402
from scripts import team_launcher as launcher  # noqa: E402
from scripts.ticket_board.pane_activity_gate import PaneActivityGate  # noqa: E402
from scripts.ticket_board.pane_state import PaneHookStateStore  # noqa: E402

TARGET = "pgu-main:0.0"
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def copilot_binary() -> str | None:
    found = os.environ.get("SWITCHYARD_COPILOT_BIN") or shutil.which("copilot")
    if not found:
        return None
    done = subprocess.run([found, "--version"], capture_output=True, text=True, timeout=60,
                          env={"PATH": "/usr/bin:/bin", "HOME": tempfile.mkdtemp(prefix="copilot-version.")})
    return found if done.stdout.startswith(copilot_runtime.VERSION_PREFIX) else None


class Model(http.server.BaseHTTPRequestHandler):
    """An OpenAI-compatible chat endpoint: text, one tool call, or 429, as `mode` says; every request kept."""

    mode = "text"
    requests: list[dict] = []

    def log_message(self, *_args) -> None:
        pass

    def _send(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        self._send(200, {"object": "list", "data": [{"id": "fake-model", "object": "model"}]})

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        request = json.loads(self.rfile.read(length) or b"{}")
        Model.requests.append(request)
        if Model.mode == "429":
            self._send(429, {"error": {"message": "Rate limit reached for requests", "type": "rate_limit_exceeded"}})
            return
        answered = any(message.get("role") == "tool" for message in request.get("messages", []))
        if Model.mode == "tool" and not answered:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_1", "type": "function", "function": {"name": "bash", "arguments": json.dumps(
                    {"command": "touch made-by-the-probe", "description": "make a file", "mode": "sync",
                     "initial_wait": 10})}}]}
            finish = "tool_calls"
        else:
            message, finish = {"role": "assistant", "content": "E2E-REPLY ok"}, "stop"
        usage = {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}
        if request.get("stream"):
            delta = dict(message)
            if "tool_calls" in delta:
                delta["tool_calls"] = [{**call, "index": 0} for call in delta["tool_calls"]]
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            for chunk in ({"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                          {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}], "usage": usage}):
                self.wfile.write(f"data: {json.dumps({'id': 'c', 'object': 'chat.completion.chunk', 'model': 'fake-model', **chunk})}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return
        self._send(200, {"id": "c", "object": "chat.completion", "model": "fake-model", "usage": usage,
                         "choices": [{"index": 0, "message": message, "finish_reason": finish}]})


class BoardStub(http.server.BaseHTTPRequestHandler):
    """The board the hook asks what a cleared or resumed session holds: PGU-7, in progress for main."""

    def log_message(self, *_args) -> None:
        pass

    def do_GET(self) -> None:
        body = json.dumps({
            "workflow": {"stages": [{"name": "in_progress", "terminal": False, "kind": "implementation", "owners": ["main"]}]},
            "tickets": [{"id": "PGU-7", "title": "Seven", "state": "in_progress", "assignee": "main",
                         "body": "E2E-TICKET-BODY"}]}).encode()
        self.send_response(200)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(handler: type) -> http.server.ThreadingHTTPServer:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class Pane:
    def __init__(self, root: Path, binary: str) -> None:
        self.root = root
        self.home = root / "home"
        self.work = self.home / "work"
        self.states = root / "state"
        self.sessions = self.home / ".local" / "state" / "pgu-ticket-board" / "pane-sessions"
        self.bin = root / "bin"
        for path in (self.work, self.bin):
            path.mkdir(parents=True)
        (self.bin / "copilot").symlink_to(Path(binary).resolve())
        subprocess.run(["git", "init", "-q", str(self.work)], check=True)
        self.model = serve(Model)
        self.board = serve(BoardStub)
        self.tmux_env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home), "LANG": "C.UTF-8", "SHELL": "/bin/sh",
                         "TERM": "xterm-256color", "TMUX_TMPDIR": str(root)}
        os.environ["TMUX_TMPDIR"] = str(root)  # the gate's own `tmux` calls reach this server

    def install_hooks(self, *extra: str) -> None:
        env = {**self.tmux_env, "TICKET_BOARD_PROJECT": "pgu", "TICKET_BOARD_PANE_STATE_DIR": str(self.states),
               "TICKET_BOARD_PANE_SESSION_DIR": str(self.sessions)}
        done = subprocess.run([sys.executable, "-B", str(ROOT / "scripts" / "ticket-board-install-pane-hooks"), "install",
                               "--home", str(self.home), "--bin-path",
                               str(self.home / ".local" / "bin" / "ticket-board-pane-idle-hook"), *extra],
                              capture_output=True, text=True, env=env, timeout=120)
        check(done.returncode == 0, f"the real installer ran: {done.stderr[-1500:]}")

    def role(self, *, yolo: bool = False, copilot_home: str = ""):
        raw = {"role": "main", "cli": ["copilot"], "model": "fake-model", "effort": "high", "yolo": yolo,
               "env": {**({"COPILOT_HOME": copilot_home} if copilot_home else {}),"PATH": f"{self.bin}:/usr/bin:/bin", "TICKET_BOARD_PROJECT": "pgu",
                       "TICKET_BOARD_URL": f"http://127.0.0.1:{self.board.server_port}",
                       "COPILOT_OFFLINE": "true", "COPILOT_PROVIDER_TYPE": "openai",
                       "COPILOT_PROVIDER_BASE_URL": f"http://127.0.0.1:{self.model.server_port}/v1",
                       "COPILOT_PROVIDER_API_KEY": "e2e-placeholder", "COPILOT_AUTO_UPDATE": "false"}}
        return launcher._role_from_json("pgu", raw, base=self.root, default_workdir=self.work)

    def tmux(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["tmux", "-f", "/dev/null", *args], capture_output=True, text=True, env=self.tmux_env,
                              timeout=30)

    def launch(self, argv: list[str]) -> None:
        self.tmux("kill-server")
        for _ in range(20):
            started = self.tmux("new-session", "-d", "-s", "pgu-main", "-x", "160", "-y", "45", "-c", str(self.work),
                                f"{shlex.join(argv)}; sleep 30")
            # A server just told to exit can still be answering on its socket; ask again.
            if started.returncode == 0 or "server exited unexpectedly" not in started.stderr:
                break
            time.sleep(0.25)
        check(started.returncode == 0, f"tmux started the pane: {started.stderr}")

    def screen(self) -> str:
        return self.tmux("capture-pane", "-p", "-t", TARGET).stdout

    def wait_screen(self, state: str, timeout: float = 60.0) -> str:
        deadline, screen = time.monotonic() + timeout, ""
        while time.monotonic() < deadline:
            screen = self.screen()
            if copilot_runtime.classify_screen(screen) == state:
                return screen
            time.sleep(0.3)
        raise AssertionError(f"screen never became {state}; last screen:\n{screen}")

    def keys(self, *keys: str) -> None:
        for key in keys:
            if key.startswith("KEY:"):
                self.tmux("send-keys", "-t", TARGET, key[4:])
            else:
                self.tmux("send-keys", "-t", TARGET, "-l", key)
            time.sleep(0.4)

    def state(self) -> dict:
        try:
            return json.loads((self.states / "pgu-main_0.0.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def record(self) -> dict:
        try:
            return json.loads((self.sessions / "pgu-main_0.0.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def wait_state(self, predicate, what: str, timeout: float = 90.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.state()
            if predicate(state):
                return state
            time.sleep(0.3)
        raise AssertionError(f"pane state never showed {what}: {self.state()}\nscreen:\n{self.screen()}")

    def close(self) -> None:
        self.tmux("kill-server")
        self.model.shutdown()
        self.board.shutdown()


def deliver_one_ticket(pane: "Pane", gate: PaneActivityGate) -> None:
    """A production-built board hands main a ticket, and the real listener types it into the live pane once."""
    import psycopg
    import copilot_runtime_board_test as boards
    import ticket_board_write_api_test as fixture
    from scripts.ticket_board.notify_listener import TicketBoardNotifyListener
    from scripts.ticket_board.peer_identity import read_process
    from temporary_cluster import temporary_cluster

    with temporary_cluster(prefix="cp634e2e-", shutdown="immediate") as cluster:
        admin = boards.build_board(cluster, "e2e")
        app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu",
                                     ticket_prefix="PGU", database_url=fixture.conninfo(
                                         cluster.socket_dir, cluster.port, "e2e", fixture.SERVICE_ROLE))
        server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            app.apply_workflow(boards.document("copilot"), expected_revision=0, dry_run=False, caller_role="director")
            pid = int(pane.tmux("display-message", "-p", "-t", TARGET, "#{pane_pid}").stdout.strip())
            fixture.psql(admin, "INSERT INTO ticket_board.role_runtime_assignments (role, runtime, actual_target, "
                                "worktree, session_dir, process_pid, process_start_time, process_uid) VALUES ('main', "
                                f"'copilot', '{TARGET}', '{pane.work}', '{pane.sessions}', {pid}, "
                                f"{read_process(pid).start_time}, {os.getuid()});")
            sent: list[tuple[str, str]] = []

            def send(target: str, message: str) -> None:
                sent.append((target, message))
                pane.keys(" ".join(message.split()), "KEY:Enter")

            listener_url = fixture.conninfo(cluster.socket_dir, cluster.port, "e2e", "ticket_board_listener")
            listener = TicketBoardNotifyListener(conninfo=listener_url, sender=send, activity_gate=gate.is_working,
                                                 target_exists=lambda _target: True,
                                                 submission_witness=gate.submission_witnessed, poll_seconds=0,
                                                 project="pgu")

            def one_pass() -> None:
                with psycopg.connect(listener_url, autocommit=True) as conn:
                    listener.refresh_workflow(conn)
                    listener.process_due_notifications(conn)

            fixture.seed_postgres_ticket(admin, "PGU-901", title="Copilot e2e", state="analysis", assignee="director",
                                         commit_exempt=True)
            one_pass()
            sent.clear()
            fixture.TEST_WRITE_TOKEN = server.write_token
            fixture.post_json(f"http://127.0.0.1:{server.server_port}", "/api/tickets/PGU-901/actions/route",
                              {"state": "in_progress", "assignee": "main"}, caller="director")
            check(listener.role_runtimes.get("main") in (None, "copilot"), listener.role_runtimes)
            since = time.time()
            one_pass()
            check(len(sent) == 1 and sent[0][0] == TARGET and "PGU-901" in sent[0][1],
                  f"the listener delivered the ticket to the Copilot pane once: {sent}")
            pane.wait_state(lambda s: s.get("source") == "copilot.agentStop" and s.get("turn_started_at", 0) >= since,
                            "the delivered ticket's turn")
            for _ in range(3):
                one_pass()
            check(len(sent) == 1, f"and never again: {sent}")
            left = fixture.psql(admin, "SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE "
                                       "ticket_id = 'PGU-901' AND target_role = 'main';")
            check(left.strip() == "0", f"nothing for it is left queued or dead-lettered: {left}")
        finally:
            server.shutdown()
            server.server_close()


def run(binary: str) -> None:
    root = Path(tempfile.mkdtemp(prefix="cp634.", dir="/tmp"))
    pane = Pane(root, binary)
    try:
        # The owner's own Copilot hooks and settings, which installing must leave byte for byte.
        own_hooks = pane.home / ".copilot" / "hooks" / "mine.json"
        own_hooks.parent.mkdir(parents=True)
        own_hooks.write_text('{"version": 1, "hooks": {"sessionEnd": [{"type": "command", "bash": "true"}]}}\n')
        settings = pane.home / ".copilot" / "settings.json"
        settings.write_text('{"theme": "dark"}\n')
        before = {path: path.read_bytes() for path in (own_hooks, settings)}
        pane.install_hooks()
        pane.install_hooks()
        check(all(path.read_bytes() == data for path, data in before.items()),
              "installing twice left the owner's hook file and settings exactly as they were")
        ours = json.loads((pane.home / ".copilot" / "hooks" / "ticket-board-pane-state.json").read_text())
        check(ours["version"] == 1 and set(ours["hooks"]) == set(
            ["sessionStart", "userPromptSubmitted", "postToolUse", "notification", "errorOccurred", "agentStop",
             "sessionEnd"]), f"our own hook file holds the seven events: {sorted(ours['hooks'])}")
        check("e2e-placeholder" not in json.dumps(ours), "no provider credential is written into a hook command")

        gate = PaneActivityGate(state_store=PaneHookStateStore(pane.states), role_runtimes={"main": "copilot"})
        gate.role_targets = {"main": TARGET}

        # 1. A fresh launch in an untrusted folder: the trust question is the person's.
        role = pane.role()
        argv = role_command.cli_command_for_role(role, session_dir=pane.sessions, pane_state_dir=pane.states)
        tail = argv[argv.index("copilot"):]
        check(tail == ["copilot", "--model", "fake-model", "--reasoning-effort", "high"],
              f"the launcher's argv is copilot's own, from its installed --help: {tail}")
        check(not copilot_runtime.workdir_is_trusted(pane.work, pane.home), "the work folder starts untrusted")
        pane.launch(argv)
        pane.wait_screen("trust")
        pane.keys("KEY:Down", "KEY:Enter")  # "Yes, and remember this folder for future sessions"
        pane.wait_screen("ready")
        check(copilot_runtime.workdir_is_trusted(pane.work, pane.home),
              "remembered trust is read back from Copilot's own config.json")
        check(pane.state() == {}, "a started pane has said nothing yet: Copilot defers sessionStart to the first prompt")

        # 2. One turn: busy, then idle at its end, with the session recorded.
        since = time.time()
        pane.keys("Say hello", "KEY:Enter")
        idle = pane.wait_state(lambda s: s.get("source") == "copilot.agentStop", "the turn's end")
        check(idle["state"] == "idle" and idle.get("turn_started_at", 0) >= since,
              f"the turn ended idle and its start was recorded: {idle}")
        check(gate.submission_witnessed(TARGET, since) is True, "the board sees the prompt start a turn")
        first = pane.record().get("session_id", "")
        check(re.fullmatch(r"[0-9a-f-]{36}", first) and idle["last_session_start"]["session_id"] == first
              and idle["last_session_start"]["source"] == "startup",
              f"the session Copilot opened is recorded, as a startup: {pane.record()} / {idle.get('last_session_start')}")
        found, where = copilot_runtime.session_store_exists(pane.home, first)
        check(found, f"Copilot's transcript for it exists at {where}")
        # Copilot's own helpers finish their work just after the turn ends; the
        # gate may hold for that moment, and then must call the pane idle.
        reasons = []
        for _ in range(20):
            busy = gate.is_busy(TARGET)
            reasons.append(gate.last_trace(TARGET).reason)
            if not busy:
                break
            time.sleep(1)
        check(not busy, f"an idle Copilot pane is not held busy: {reasons}")

        # 2b. The board hands main a ticket: one delivery, taken as a turn, never repeated.
        deliver_one_ticket(pane, gate)

        # 3. /clear: a new session in the same process is recorded as a clear, and told its ticket.
        Model.requests.clear()
        pane.keys("/clear", "KEY:Enter")
        pane.wait_screen("ready")
        pane.keys("After clear", "KEY:Enter")
        cleared = pane.wait_state(lambda s: s.get("source") == "copilot.agentStop"
                                  and (s.get("last_session_start") or {}).get("session_id") not in (None, first),
                                  "the cleared session's turn end")
        second = cleared["last_session_start"]["session_id"]
        check(cleared["last_session_start"]["source"] == "clear",
              f"Copilot's `new` after /clear is recorded as `clear`: {cleared['last_session_start']}")
        check(pane.record().get("session_id") == second,
              f"the pane's own provider switched sessions, so the new one is what resumes: {pane.record()}"
              + "".join(path.read_text() for path in pane.sessions.glob("*.resume.log")))
        told = json.dumps(Model.requests)
        check("PGU-7" in told, "the cleared session was told its in-progress ticket through additionalContext")

        # 4. A tool that needs permission: blocked, and held, until a person answers.
        Model.mode = "tool"
        pane.keys("Make the file", "KEY:Enter")
        blocked = pane.wait_state(lambda s: s.get("state") == "blocked", "the permission prompt")
        check(blocked["source"] == "copilot.notification.permission_prompt"
              and "main" in gate.permission_prompt_waits(), f"the pane is blocked on a permission prompt: {blocked}")
        check(copilot_runtime.classify_screen(pane.screen()) == "permission", "the screen is the permission question")
        Model.mode = "text"
        pane.keys("KEY:Escape")
        pane.wait_screen("ready")

        # 5. A 429: Copilot retries behind Working; the pane is busy on capacity, never idle.
        Model.mode = "429"
        pane.keys("Rate limited", "KEY:Enter")
        limited = pane.wait_state(lambda s: s.get("source") == "copilot.errorOccurred.capacity", "the capacity error")
        check(limited["state"] == "busy" and gate.is_busy(TARGET),
              f"a retried 429 keeps the pane busy: {limited} {gate.last_trace(TARGET)}")
        Model.mode = "text"
        pane.wait_state(lambda s: s.get("source") == "copilot.agentStop", "the retried turn's end", timeout=180)

        # 6. /exit ends the session idle.
        pane.keys("/exit", "KEY:Enter")
        pane.wait_state(lambda s: s.get("source") == "copilot.sessionEnd", "the session end")

        # 7. A relaunch resumes the recorded session, and the board knows it is the same one.
        ok, why = launcher._resume_preflight_allows_attempt(role, second, session_dir=pane.sessions)
        check(ok and not why, f"the recorded session passes the resume preflight: {why}")
        argv = role_command.cli_command_for_role(role, session_dir=pane.sessions, pane_state_dir=pane.states,
                                                 resume=True)
        tail = argv[argv.index("copilot"):]
        check(tail[:3] == ["copilot", "--resume", second], f"the relaunch passes copilot --resume <id>: {tail}")
        pane.launch(argv)
        pane.wait_screen("ready")
        pane.keys("Resumed", "KEY:Enter")
        resumed = pane.wait_state(lambda s: s.get("source") == "copilot.agentStop"
                                  and (s.get("last_session_start") or {}).get("source") == "resume",
                                  "the resumed turn's end")
        check(resumed["last_session_start"]["session_id"] == second and pane.record()["session_id"] == second,
              f"the resumed session is the recorded one: {resumed['last_session_start']}")
        pane.keys("/exit", "KEY:Enter")
        pane.wait_state(lambda s: s.get("source") == "copilot.sessionEnd", "the resumed session's end")

        # 8. A session Copilot no longer has is reported, not passed to --resume.
        store = copilot_runtime.copilot_home(pane.home) / copilot_runtime.SESSION_STATE_DIR
        (store / second).rename(store / f"{second}.moved")
        ok, why = launcher._resume_preflight_allows_attempt(role, second, session_dir=pane.sessions)
        check(not ok and second in why and "copilot --resume" in why, f"a missing session is reported: {why}")

        # 9. And an id Copilot does not know stops it at once, which the screen names.
        pane.launch(["env", *[f"{k}={v}" for k, v in role.env.items()], "copilot", "--model", "fake-model",
                     "--resume", "99999999-0000-4000-8000-000000000000"])
        pane.wait_screen("fatal")

        # 10. A role with its own COPILOT_HOME: Copilot reads hooks, trust and sessions only there, and so does
        #     Switchyard -- the hook installer is told the root the launcher configures (SYRD-634 Audit).
        import types

        from scripts import first_run_setup, pane_hooks

        custom = root / "copilot-root"
        homed = pane.role(copilot_home=str(custom))
        args = pane_hooks._copilot_home_args(types.SimpleNamespace(roles=[homed], role_state_isolation=True,
                                                                   run_as_user="e2e"), "e2e", pane.home)
        check(args == ["--copilot-home", str(custom)], f"the launcher names the role's root: {args}")
        # First launch, before any installer or refresh has run for this root: the pane prepares it itself,
        # from the tenant's staged tooling (a stand-in here, linked to this tree).
        staged = root / "control" / "pgu"
        staged.mkdir(parents=True)
        for name in ("ticket-board-install-pane-hooks", "switchyard-board-skill"):
            (staged / name).symlink_to(ROOT / "scripts" / name)
        os.environ["SWITCHYARD_TENANT_CONTROL_ROOT"] = str(root / "control")
        check(not custom.exists(), "the role's root does not exist before its first launch")
        record_before = pane.record().get("session_id")
        argv = role_command.cli_command_for_role(homed, session_dir=pane.sessions, pane_state_dir=pane.states)
        check("switchyard-copilot-root" in argv, "the pane command prepares the root before copilot")
        tail = argv[argv.index("copilot"):]
        pane.launch(argv)
        pane.wait_screen("trust")  # the default root trusts the folder; this root has never been asked
        check(not launcher._workdir_is_trusted("copilot", owner_home=pane.home, workdir=pane.work,
                                               **launcher._trust_kwargs("copilot", homed)),
              "and Switchyard reads it from the same root")
        pane.keys("KEY:Down", "KEY:Enter")
        pane.wait_screen("ready")
        check(launcher._workdir_is_trusted("copilot", owner_home=pane.home, workdir=pane.work,
                                           **launcher._trust_kwargs("copilot", homed))
              and first_run_setup._first_run_trust_command(homed)[:2] == ["env", f"COPILOT_HOME={custom}"],
              "the answer is recorded in the role's root, where the first-run trust step also runs Copilot")
        since = time.time()
        pane.keys("From the role's own root", "KEY:Enter")
        ended = pane.wait_state(lambda s: s.get("source") == "copilot.agentStop" and s.get("turn_started_at", 0) >= since,
                                "the turn in the role's own root")
        homed_session = ended["last_session_start"]["session_id"]
        check(homed_session != record_before and (custom / "session-state" / homed_session / "events.jsonl").is_file()
              and not (pane.home / ".copilot" / "session-state" / homed_session).exists(),
              f"Switchyard's hooks ran from the role's root and the session lives there: {tail} {ended}")
        check((custom / "skills" / "switchyard-board" / "SKILL.md").is_file(), "and the board skill is there for it")
        ok, why = launcher._resume_preflight_allows_attempt(homed, homed_session, session_dir=pane.sessions)
        check(ok and not why, f"and its resume is checked there: {why}")
        pane.keys("/exit", "KEY:Enter")
        pane.wait_state(lambda s: s.get("source") == "copilot.sessionEnd", "the session end in the role's own root")
    finally:
        pane.close()
        keep = os.environ.get("SWITCHYARD_E2E_KEEP")
        if keep:
            shutil.copytree(root, Path(keep) / root.name, symlinks=True, ignore_dangling_symlinks=True,
                            ignore=shutil.ignore_patterns("pkg", "*.sock", "default"))
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    binary = copilot_binary()
    if binary is None or shutil.which("tmux") is None:
        print("copilot_end_to_end_test: skipped, no standalone copilot (SWITCHYARD_COPILOT_BIN or PATH) or no tmux")
        return 0
    run(binary)
    print(f"copilot_end_to_end_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
