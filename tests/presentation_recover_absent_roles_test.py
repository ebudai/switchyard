#!/usr/bin/env python3
"""SYRD-580: `present recover` brings back one missing role while another is still gone.

MEFP's live UAT on 2026-10-11 had Audit and Main both absent. The Director ran
`switchyard present mefp recover audit`, then `... recover main`, and both
refused before launching anything: "no live runtime assignment for configured
role(s): audit, main". Recovery resolved every configured role's live
assignment first, so a missing role blocked its own recovery, and two missing
roles could not be recovered in either order.

This builds that shape for real and recovers through the real
`presentation_action` (and once through the real `switchyard present`
command, which prints the presentation report afterwards):

* an isolated tmux server -- TMUX removed, TMUX_TMPDIR in a temporary
  directory, the socket asserted before anything is created or killed;
* workers started by the real launcher start, whose real pane command runs the
  real `ticket-board-register-runtime` before exec'ing the role's CLI (a stand
  in that sleeps under the runtime's name);
* a board standing in for the real one on both of its channels: the Unix
  socket takes registrations, bound to the registering process by SO_PEERCRED,
  and refuses any role, runtime or target the workflow does not declare; HTTP
  serves the runtime assignments filtered exactly as `runtime_targets` filters
  them, and the declared workflow;
* real display sessions, whose nested attach is checked from tmux itself.

Only the worker's pane state, session store, layout path and presentation
state are pointed into the temporary directory; nothing outside it is written.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import pwd
import shutil
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import presentation_commands  # noqa: E402
from scripts import presentation_controller as pc  # noqa: E402
from scripts import team_launcher as tl  # noqa: E402

CHECKS = 0
PROJECT = "demo"
ROLES = {"director": "claude", "main": "codex", "audit": "claude", "ops": "claude"}
SLOTS = {"director": 0, "main": 1, "audit": 2, "ops": 3}
DIRECTOR = {"TICKET_BOARD_CALLER_ROLE": "director", "TICKET_BOARD_PROJECT": PROJECT}


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def tmux(*args: str, check_rc: bool = True) -> str:
    proc = subprocess.run(["tmux", *args], capture_output=True, text=True)
    if check_rc and proc.returncode != 0:
        raise AssertionError(f"tmux {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def wait_for(predicate, what: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def sessions() -> set[str]:
    out = tmux("list-sessions", "-F", "#{session_name}", check_rc=False)
    return {line for line in out.splitlines() if line}


def worker_sessions() -> set[str]:
    return {name for name in sessions() if not name.startswith(f"{PROJECT}-display-")}


def pane_pid(target: str) -> str:
    return tmux("display-message", "-p", "-t", f"={target}", "#{pane_pid}", check_rc=False)


def display(slot: int) -> str:
    return f"{PROJECT}-display-{slot}"


def clients_of(session: str) -> set[str]:
    out = tmux("list-clients", "-t", f"={session}", "-F", "#{client_tty}", check_rc=False)
    return {line for line in out.splitlines() if line}


def nested_attached(role: str) -> bool:
    """Independent of the code under test: the slot's pane tty is a client of the worker."""
    target = f"{display(SLOTS[role])}:0.0"
    dead = tmux("display-message", "-p", "-t", f"={target}", "#{pane_dead}", check_rc=False)
    tty = tmux("display-message", "-p", "-t", f"={target}", "#{pane_tty}", check_rc=False)
    return dead == "0" and bool(tty) and tty in clients_of(f"{PROJECT}-{role}")


def start_ticks(pid: int) -> int:
    stat = Path(f"/proc/{pid}/stat").read_text()
    return int(stat[stat.rindex(")") + 2:].split()[19])


def process_tree(pid: int) -> set[int]:
    children: dict[int, list[int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        parent = int(stat[stat.rindex(")") + 2:].split()[1])
        children.setdefault(parent, []).append(int(entry.name))
    seen, stack = set(), [pid]
    while stack:
        current = stack.pop()
        if current not in seen:
            seen.add(current)
            stack.extend(children.get(current, []))
    return seen


class Board:
    """The board's two channels, holding what the real one holds.

    Registrations arrive on the Unix socket from the pane's own registering
    process; the pid is the kernel's (SO_PEERCRED), never the request's. A
    role, runtime or target the workflow does not declare active is refused,
    as `register_role_runtime` refuses it. The HTTP assignments are filtered as
    `runtime_targets` filters them.
    """

    def __init__(self, root: Path) -> None:
        self.socket_path = root / "board.sock"
        self.declared = {
            name: {"name": name, "active": True, "runtime": runtime, "target": f"{PROJECT}-{name}:0.0"}
            for name, runtime in ROLES.items()
        }
        self.rows: dict[str, dict] = {}
        self.registrations: list[str] = []
        self.publish = True
        #: A board with no workflow document declares a role at its first registration.
        self.legacy = False
        self.workflow_status = 200
        #: Added to a registration's recorded start time: a row naming a reused pid.
        self.skew = 0
        self.lock = threading.Lock()
        board = self

        class Http(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path == "/api/runtime-assignments":
                    with board.lock:
                        visible = {
                            name: dict(row) for name, row in board.rows.items()
                            if board.declared.get(name, {}).get("active")
                            and board.declared[name]["runtime"] == row["runtime"]
                            and board.declared[name]["target"] == row["actual_target"]
                        }
                    body = {"project": PROJECT, "authority_mode": "process", "assignments": visible}
                elif self.path == "/api/workflow":
                    if board.workflow_status != 200:
                        self.send_error(board.workflow_status)
                        return
                    with board.lock:
                        document = None if board.legacy else {"roles": [dict(v) for v in board.declared.values()]}
                        body = {"revision": 0 if board.legacy else 115, "document": document}
                else:
                    self.send_error(404)
                    return
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args):
                pass

        class Register(BaseHTTPRequestHandler):
            def address_string(self):
                return "unix"

            def do_POST(self):  # noqa: N802
                creds = self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
                pid, uid, _gid = struct.unpack("3i", creds)
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                role = payload["role"]
                with board.lock:
                    if board.legacy and role not in board.declared:
                        board.declared[role] = {
                            "name": role, "active": True, "runtime": payload["runtime"], "target": payload["target"],
                        }
                    spec = board.declared.get(role) or {}
                    ok = (
                        self.path == "/api/register-caller" and spec.get("active")
                        and spec.get("runtime") == payload["runtime"] and spec.get("target") == payload["target"]
                    )
                    if ok:
                        board.registrations.append(role)
                        if board.publish:
                            previous = board.rows.get(role, {}).get("generation", 0)
                            board.rows[role] = {
                                "role": role, "runtime": payload["runtime"], "actual_target": payload["target"],
                                "worktree": payload["worktree"], "session_dir": payload["session_dir"],
                                "process_pid": pid, "process_start_time": start_ticks(pid) + board.skew,
                                "process_uid": uid, "generation": previous + 1,
                            }
                data = b"{}" if ok else b"role/runtime/target is not active workflow configuration"
                self.send_response(200 if ok else 409)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args):
                pass

        class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Http)
        self.http.daemon_threads = True
        self.unix = UnixServer(str(self.socket_path), Register)
        self.url = f"http://127.0.0.1:{self.http.server_address[1]}/"
        for server in (self.http, self.unix):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def snapshot(self) -> tuple[dict, list[str]]:
        with self.lock:
            return json.loads(json.dumps(self.rows)), list(self.registrations)

    def close(self) -> None:
        for server in (self.http, self.unix):
            server.shutdown()
            server.server_close()


class Harness:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir()
        for runtime in ("claude", "codex"):
            fake = self.bin / runtime
            # A worker that stays up under its runtime's name; arguments ignored.
            fake.write_text(f"#!/bin/bash\nexec -a {runtime} sleep 100000\n", encoding="utf-8")
            fake.chmod(0o755)
        # The real registration program, found where the pane command looks for it.
        (self.bin / "ticket-board-register-runtime").symlink_to(ROOT / "scripts" / "ticket-board-register-runtime")
        (self.bin / "ticket-board-directorctl").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        self.board: Board | None = None
        self.case = 0

    def fresh(self) -> tuple[tl.ProjectConfig, Path, Path]:
        """A new case: no tmux server, a new board, a new project directory."""
        tmux("kill-server", check_rc=False)
        if self.board is not None:
            self.board.close()
        self.case += 1
        case = self.root / f"case{self.case}"
        case.mkdir()
        self.board = Board(case)
        os.environ["TICKET_BOARD_PANE_STATE_DIR"] = str(case / "pane-state")
        roles = []
        for name, runtime in ROLES.items():
            workdir = case / "work" / name
            workdir.mkdir(parents=True)
            roles.append({
                "role": name, "cli": str(self.bin / runtime), "slot": SLOTS[name], "workdir": str(workdir),
                "env": {"TICKET_BOARD_DIRECTORCTL": str(self.bin / "ticket-board-directorctl")},
            })
        path = case / f"{PROJECT}.json"
        path.write_text(json.dumps({
            "project": PROJECT,
            "desktop_access": {"mode": "headless"},
            "board_url": self.board.url,
            "board_socket": str(self.board.socket_path),
            "session_dir": str(case / "pane-sessions"),
            "run_as_user": tl.current_user_name(),
            "role_state_isolation": True,
            "roles": roles,
        }), encoding="utf-8")
        config = tl.load_project_config(PROJECT, path)
        check(config.role_state_isolation and config.board_url == self.board.url, "the fixture is a process-authority tenant")
        return config, path, case / "layout" / "presentation.json"

    def start(self, config: tl.ProjectConfig, name: str) -> None:
        """A worker started by the launcher's own start, as `switchyard launch` would."""
        role = pc._role_by_name(config, name)
        result = tl.ensure_visible_role_session_for_viewer(
            role, mode="attach-or-start", session_dir=tl.role_session_dir(config, role),
            pane_state_dir=Path(os.environ["TICKET_BOARD_PANE_STATE_DIR"]), bin_user=config.run_as_user,
        )
        check(result == 0, f"{name} started")
        wait_for(lambda: name in self.board.snapshot()[0], f"{name} to register")

    def displays(self, config: tl.ProjectConfig) -> None:
        for name, slot in SLOTS.items():
            pc._configure_display_session(config, slot, name, runner=subprocess.run, presentation_ttys=set())
        for name in ("director", "ops"):
            wait_for(lambda n=name: nested_attached(n), f"{name}'s display to attach")

    def tenant(self, *, live=("director", "ops"), legacy=False):
        """MEFP's shape: some roles live and registered, the rest absent."""
        config, path, state = self.fresh()
        if legacy:
            self.board.legacy = True
            self.board.declared.clear()
        for name in live:
            self.start(config, name)
        self.displays(config)
        return config, path, state


def recover(config, path, state, role, *, environ=DIRECTOR, timeout=None):
    return pc.presentation_action(
        config, config_path=path, action="recover", role_name=role, state_path=state,
        environ=environ, runner=subprocess.run,
        **({"registration_timeout": timeout} if timeout is not None else {}),
    )


def refused(call) -> str:
    try:
        call()
    except SystemExit as exc:
        return str(exc)
    raise AssertionError("expected a refusal")


def frozen(h: Harness, state: Path) -> dict:
    """Everything a refused recovery must leave exactly as it was."""
    rows, registrations = h.board.snapshot()
    return {
        "sessions": sorted(sessions()),
        "panes": {name: pane_pid(f"{name}:0.0") for name in sorted(sessions())},
        "rows": rows,
        "registrations": registrations,
        "state": state.read_bytes() if state.exists() else None,
    }


def assert_recovered(h: Harness, name: str, before: dict, result: dict) -> None:
    """``name`` -- and nothing else -- was started, registered and attached."""
    rows, registrations = h.board.snapshot()
    check(worker_sessions() == {f"{PROJECT}-{n}" for n in before["workers"]} | {f"{PROJECT}-{name}"},
          f"exactly {name}'s worker was started: {sorted(worker_sessions())}")
    check(registrations == before["registrations"] + [name],
          f"and only {name} registered: {registrations}")
    row = rows[name]
    pid = pane_pid(f"{PROJECT}-{name}:0.0")
    check(row["process_pid"] in process_tree(int(pid)),
          f"{name}'s registration is the process in its own pane: {row['process_pid']} under {pid}")
    check(nested_attached(name), f"{name}'s display is a live client of its new worker")
    for other in before["workers"]:
        check(pane_pid(f"{PROJECT}-{other}:0.0") == before["panes"][other], f"{other}'s worker was not restarted")
        check(nested_attached(other), f"{other}'s display is still attached")
    check(set(rows) == set(before["rows"]) | {name}, f"no other role was registered: {sorted(rows)}")
    last = result["history"][-1]
    check(last["action"] == "recover" and last["actor"] == "director" and last["detail"]["role"] == name,
          f"the recovery is recorded for {name}: {last}")


def baseline(h: Harness) -> dict:
    rows, registrations = h.board.snapshot()
    workers = sorted(n.removeprefix(f"{PROJECT}-") for n in worker_sessions())
    return {
        "workers": workers, "rows": rows, "registrations": registrations,
        "panes": {name: pane_pid(f"{PROJECT}-{name}:0.0") for name in workers},
    }


def test_two_missing_roles_recover_audit_then_main(h: Harness) -> None:
    config, path, state = h.tenant()
    check(set(h.board.snapshot()[0]) == {"director", "ops"} and worker_sessions() == {"demo-director", "demo-ops"},
          "MEFP's shape: Audit and Main have no worker and no assignment")
    before = baseline(h)
    result = recover(config, path, state, "audit")
    assert_recovered(h, "audit", before, result)
    check(f"{PROJECT}-main" not in sessions() and "main" not in h.board.snapshot()[0],
          "Main, still absent, was not started by Audit's recovery")

    report = pc.presentation_report(config, config_path=path, state_path=state, runner=subprocess.run)
    slots = {item["desired_role"]: item for item in report["slots"]}
    check(slots["main"]["worker"]["state"] == "unregistered" and slots["main"]["client_state"] == "unregistered",
          f"the report names Main as unregistered rather than refusing: {slots['main']}")
    check(slots["audit"]["worker"]["state"] == "live" and slots["audit"]["client_state"] == "connected",
          f"and shows Audit live and connected: {slots['audit']}")

    before = baseline(h)
    result = recover(config, path, state, "main")
    assert_recovered(h, "main", before, result)
    report = pc.presentation_report(config, config_path=path, state_path=state, runner=subprocess.run)
    check(all(item["worker"]["state"] == "live" and item["client_state"] == "connected" for item in report["slots"]),
          f"every slot is live and connected: {report['slots']}")


@contextlib.contextmanager
def director_command(state: Path):
    """`switchyard present` as the Director's pane runs it: role from the env,
    state at the project's own path (pointed into the case directory)."""
    launcher = pc.team_launcher
    saved = launcher.default_layout_output_path
    launcher.default_layout_output_path = lambda _config, *, config_path: state.with_name("layout.json")
    os.environ.update(DIRECTOR)
    try:
        yield
    finally:
        launcher.default_layout_output_path = saved
        for key in DIRECTOR:
            os.environ.pop(key, None)


def present(config, path, role) -> list[str]:
    said: list[str] = []
    args = argparse.Namespace(action="recover", role=role, json=False)
    answer = presentation_commands.switchyard_present_command(
        config, config_path=path, args=args, runner=subprocess.run, print_func=said.append,
    )
    check(answer == 0, f"`switchyard present {PROJECT} recover {role}` succeeded")
    return said


def test_two_missing_roles_recover_main_then_audit_through_the_command(h: Harness) -> None:
    config, path, state = h.tenant()
    with director_command(state):
        before = baseline(h)
        said = present(config, path, "main")
        result = json.loads(state.read_text())
        assert_recovered(h, "main", before, result)
        check(any("slot 2: audit worker=unregistered client=unregistered" in line for line in said),
              f"the command's report shows Audit still unregistered: {said}")
        check(any("slot 1: main worker=live client=connected" in line for line in said),
              f"and Main live and connected: {said}")
        check(f"{PROJECT}-audit" not in sessions(), "Audit was not started by Main's recovery")

        before = baseline(h)
        said = present(config, path, "audit")
        result = json.loads(state.read_text())
        assert_recovered(h, "audit", before, result)
        check(sum("worker=live client=connected" in line for line in said) == 4,
              f"all four slots live and connected: {said}")


def test_a_divergent_requested_role_is_named_and_nothing_changes(h: Harness) -> None:
    config, path, state = h.tenant()
    # Audit's declared pane holds a live Codex worker; the workflow declares Claude.
    tmux("new-session", "-d", "-s", f"{PROJECT}-audit", str(h.bin / "codex"), "x")
    wait_for(lambda: Path(f"/proc/{pane_pid(f'{PROJECT}-audit:0.0')}/cmdline").read_bytes().startswith(b"codex"),
             "the Codex worker in Audit's pane")
    before = frozen(h, state)
    message = refused(lambda: recover(config, path, state, "audit"))
    pid = pane_pid(f"{PROJECT}-audit:0.0")
    check(f"audit: declared claude at {PROJECT}-audit:0.0, but the live worker in that pane runs codex (pid {pid})"
          in message, f"the divergence is named: {message}")
    check("main" not in message, f"an unrelated absent role is not part of it: {message}")
    check(frozen(h, state) == before, "nothing was started, restarted, registered or recorded")


def test_an_unrelated_divergent_role_does_not_block_recovery(h: Harness) -> None:
    config, path, state = h.tenant()
    # Main's declared pane runs Claude, though the workflow declares Codex.
    tmux("new-session", "-d", "-s", f"{PROJECT}-main", str(h.bin / "claude"), "x")
    wait_for(lambda: Path(f"/proc/{pane_pid(f'{PROJECT}-main:0.0')}/cmdline").read_bytes().startswith(b"claude"),
             "the Claude worker in Main's pane")
    main_pid = pane_pid(f"{PROJECT}-main:0.0")
    before = baseline(h)
    result = recover(config, path, state, "audit")
    check("audit" in h.board.snapshot()[0] and nested_attached("audit"), "Audit was recovered")
    check(pane_pid(f"{PROJECT}-main:0.0") == main_pid, "Main's pane was not touched")
    check("main" not in h.board.snapshot()[0], "and Main is still unregistered")
    check(result["history"][-1]["detail"]["role"] == "audit", "the recovery is Audit's")
    check(set(h.board.snapshot()[1]) - set(before["registrations"]) == {"audit"}, "only Audit registered")


def test_a_saved_configuration_the_workflow_disowns_is_not_started(h: Harness) -> None:
    config, path, state = h.tenant()
    with h.board.lock:
        h.board.declared["audit"]["runtime"] = "codex"
    before = frozen(h, state)
    message = refused(lambda: recover(config, path, state, "audit"))
    check(f"workflow declares audit as codex at {PROJECT}-audit:0.0, but its saved configuration would start "
          f"claude at {PROJECT}-audit:0.0" in message, f"both sides are named: {message}")
    check("Nothing was changed" in message, message)
    # The same runtime, declared at another target.
    with h.board.lock:
        h.board.declared["audit"].update(runtime="claude", target=f"{PROJECT}-audit-r2:0.0")
    message = refused(lambda: recover(config, path, state, "audit"))
    check(f"workflow declares audit as claude at {PROJECT}-audit-r2:0.0, but its saved configuration would start "
          f"claude at {PROJECT}-audit:0.0" in message, f"a target the board would refuse: {message}")
    check(frozen(h, state) == before, "nothing was started or recorded")


def test_a_role_the_workflow_does_not_declare_active_is_not_started(h: Harness) -> None:
    config, path, state = h.tenant()
    before = frozen(h, state)
    with h.board.lock:
        h.board.declared["audit"]["active"] = False
    message = refused(lambda: recover(config, path, state, "audit"))
    check(f"{PROJECT}'s workflow declares audit inactive" in message, message)
    with h.board.lock:
        del h.board.declared["audit"]
    message = refused(lambda: recover(config, path, state, "audit"))
    check(f"{PROJECT}'s workflow does not declare audit" in message, message)
    h.board.workflow_status = 500
    message = refused(lambda: recover(config, path, state, "audit"))
    check("its declared workflow could not be read" in message and "Nothing was changed" in message, message)
    check(frozen(h, state) == before, "none of them started or recorded anything")


def test_a_board_with_no_workflow_document_recovers(h: Harness) -> None:
    """Such a board declares a role at its first registration: nothing to disagree with yet."""
    config, path, state = h.tenant(legacy=True)
    before = baseline(h)
    result = recover(config, path, state, "audit")
    assert_recovered(h, "audit", before, result)


def test_a_registration_not_bound_to_the_new_process_is_not_accepted(h: Harness) -> None:
    config, path, state = h.tenant()
    h.board.skew = 1
    slot_pid = pane_pid(f"{display(SLOTS['audit'])}:0.0")
    message = refused(lambda: recover(config, path, state, "audit", timeout=3.0))
    check("did not register a live runtime with the board within 3s" in message, message)
    check("the pid has been reused" in message, f"and why the registration it found does not count: {message}")
    check(pane_pid(f"{display(SLOTS['audit'])}:0.0") == slot_pid and not state.exists(),
          "the display was not attached to it and nothing was recorded")


def test_a_worker_that_exits_during_the_wait_ends_it(h: Harness) -> None:
    config, path, state = h.tenant()
    h.board.publish = False

    def kill_once_registered() -> None:
        wait_for(lambda: "audit" in h.board.snapshot()[1], "audit's registration request", timeout=30)
        time.sleep(0.5)
        tmux("kill-session", "-t", f"={PROJECT}-audit", check_rc=False)

    killer = threading.Thread(target=kill_once_registered, daemon=True)
    killer.start()
    began = time.monotonic()
    message = refused(lambda: recover(config, path, state, "audit", timeout=60.0))
    killer.join(timeout=5)
    check(f"its pane {PROJECT}-audit:0.0 exited before it registered with the board" in message, message)
    check(time.monotonic() - began < 30, f"promptly, not after the full wait: {time.monotonic() - began:.1f}s")
    check(not state.exists(), "nothing was recorded")


def test_a_running_unregistered_pane_is_not_replaced(h: Harness) -> None:
    config, path, state = h.tenant()
    tmux("new-session", "-d", "-s", f"{PROJECT}-audit", str(h.bin / "claude"), "x")
    before = frozen(h, state)
    message = refused(lambda: recover(config, path, state, "audit"))
    check(f"Its pane {PROJECT}-audit:0.0 is running but has not registered" in message, message)
    check(frozen(h, state) == before, "the running pane was left alone and nothing was recorded")


def test_a_start_that_never_registers_is_not_reported_as_recovered(h: Harness) -> None:
    config, path, state = h.tenant()
    h.board.publish = False
    slot_pid = pane_pid(f"{display(SLOTS['audit'])}:0.0")
    message = refused(lambda: recover(config, path, state, "audit", timeout=3.0))
    check("audit was started for recovery, but it did not register a live runtime with the board within 3s"
          in message, message)
    check("Its display was not touched and nothing was recorded" in message, message)
    check(pane_pid(f"{display(SLOTS['audit'])}:0.0") == slot_pid, "Audit's display slot was not respawned")
    check(not state.exists(), "no recovery was recorded")


def test_a_caller_who_could_not_act_gets_the_old_refusal(h: Harness) -> None:
    config, path, state = h.tenant()
    before = frozen(h, state)
    # Another role, no role, and the Director of another project.
    for environ in ({"TICKET_BOARD_CALLER_ROLE": "ops"}, {}, {**DIRECTOR, "TICKET_BOARD_PROJECT": "other"}):
        message = refused(lambda e=environ: recover(config, path, state, "audit", environ=e))
        check(message == "switchyard: no live runtime assignment for configured role(s): audit, main",
              f"the refusal is unchanged for {environ}: {message}")
    check(frozen(h, state) == before, "nothing was started or probed into being for them")
    # And with nothing absent, the authority refusal is the one it always was.
    h.start(config, "audit")
    h.start(config, "main")
    message = refused(lambda: recover(config, path, state, "audit", environ={"TICKET_BOARD_CALLER_ROLE": "ops"}))
    check("runtime presentation changes require TICKET_BOARD_CALLER_ROLE=director" in message, message)


def test_a_live_assignment_the_projection_does_not_run_is_still_refused(h: Harness) -> None:
    config, path, state = h.tenant()
    # Ops is declared, and registered, as Codex; the saved configuration runs Claude.
    with h.board.lock:
        h.board.declared["ops"]["runtime"] = "codex"
        h.board.rows["ops"]["runtime"] = "codex"
    before = frozen(h, state)
    message = refused(lambda: recover(config, path, state, "audit"))
    check("runtime assignment for ops is 'codex', which differs from the launcher projection" in message, message)
    check(frozen(h, state) == before, "nothing was started")


def isolate() -> Path:
    for key in list(os.environ):
        if key.startswith(("TICKET_BOARD_", "PGU_", "SWITCHYARD_", "TMUX")):
            os.environ.pop(key)
    tmpdir = Path(tempfile.mkdtemp(prefix="sy580-tmux-", dir="/tmp"))
    os.environ["TMUX_TMPDIR"] = str(tmpdir)
    expected = tmpdir / f"tmux-{os.getuid()}" / "default"
    probe = subprocess.run(["tmux", "list-sessions"], capture_output=True, text=True)
    assert probe.returncode != 0 and str(expected) in probe.stderr, (
        f"refusing to run: tmux would not use the isolated socket {expected}: {probe.stderr!r}"
    )
    return expected


def owner_state() -> list[Path]:
    """The real owner's project state for this fixture's slug, which must never appear."""
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    return [
        path for path in (home / ".local" / "state" / f"{PROJECT}-ticket-board", home / ".config" / "switchyard" / PROJECT)
        if path.exists()
    ]


def main() -> int:
    if not shutil.which("tmux"):
        raise SystemExit("presentation_recover_absent_roles_test: tmux is required")
    present_before = owner_state()
    expected = isolate()
    root = Path(tempfile.mkdtemp(prefix="sy580-"))
    h = Harness(root)
    try:
        for case in (
            test_two_missing_roles_recover_audit_then_main,
            test_two_missing_roles_recover_main_then_audit_through_the_command,
            test_a_divergent_requested_role_is_named_and_nothing_changes,
            test_an_unrelated_divergent_role_does_not_block_recovery,
            test_a_saved_configuration_the_workflow_disowns_is_not_started,
            test_a_role_the_workflow_does_not_declare_active_is_not_started,
            test_a_board_with_no_workflow_document_recovers,
            test_a_registration_not_bound_to_the_new_process_is_not_accepted,
            test_a_worker_that_exits_during_the_wait_ends_it,
            test_a_running_unregistered_pane_is_not_replaced,
            test_a_start_that_never_registers_is_not_reported_as_recovered,
            test_a_caller_who_could_not_act_gets_the_old_refusal,
            test_a_live_assignment_the_projection_does_not_run_is_still_refused,
        ):
            case(h)
            print(f"  {case.__name__}: ok", flush=True)
    finally:
        socket_path = tmux("display-message", "-p", "#{socket_path}", check_rc=False)
        if socket_path == str(expected):
            tmux("kill-server", check_rc=False)
        if h.board is not None:
            h.board.close()
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(expected.parent.parent, ignore_errors=True)
    check(owner_state() == present_before, "the real owner's project state was not touched")
    print(f"presentation_recover_absent_roles_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
