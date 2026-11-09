#!/usr/bin/env python3
"""SYRD-750: `switchyard restart-role` restarts exactly one role's pane, safely.

Against the command's real code, with its seams stood in -- the tmux runner (session and pane pid), the board's GETs,
the launcher's single-role reload, a /proc in the kernel's own `stat` and `environ` formats, the activity gate and
SYRD-713's successor:

1. **Another role**: the launcher's reload runs for that role alone, in `reload` mode on its own session; the command
   waits for the board to register a live process other than the old one, and reports success only when the board
   variables the launcher gives the role now -- a report-token file provisioned since its start among them -- reached
   the new process. A replacement still missing one is a failure, not a restart.
2. **Refused, nothing changed**: a live turn; a leased command; a quota hold and a safety hold on the role's account;
   a held ticket not named (and accepted when named); a pane whose process is not the board's registration; a role
   not active in the workflow; a role not in the configuration; a board that does not answer; a second restart of
   the same role while one is in flight. In none is the reload run, nor the successor armed.
3. **Disconnected**: a role whose pane is gone while the board is up is started -- `attach-or-start` -- and must
   register as a new process; the board's dead registration is not mistaken for the new one.
4. **The Director's own pane**: never reloaded in-process. The successor is armed and released; if it cannot be armed
   nothing is ended and the operator's exact command is printed.
5. **Discoverable**: `switchyard --help`, the command tables, the upgrade report, the report-link messages and the
   Director guide name it; the guide says when a whole-project stop/start is still needed, and that reopening the
   window restarts no pane.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

from scripts import role_restart, role_runtime, role_self_restart  # noqa: E402
from scripts import team_launcher as launcher  # noqa: E402

CHECKS = 0
TOKEN_FILE = "/home/owner/.config/p750/report-credentials/up.env"


def check(condition: object, detail: object) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


class Kernel:
    """A /proc of processes, each with a start time and an environment."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def spawn(self, pid: int, start: int, env: dict[str, str] | None = None, comm: str = "claude", ppid: int = 1,
              state: str = "S") -> None:
        d = self.root / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        fields = [state, str(ppid)] + ["0"] * 17 + [str(start)] + ["0"] * 5
        (d / "stat").write_text(f"{pid} ({comm}) {' '.join(fields)}\n")
        (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in (env or {}).items()) + b"\0")

    def kill(self, pid: int) -> None:
        import shutil
        shutil.rmtree(self.root / str(pid), ignore_errors=True)


class Board:
    """The five GETs, answered from dicts the case changes."""

    def __init__(self, role: str) -> None:
        self.role = role
        self.down = False
        self.workflow = {"document": {"roles": [
            {"name": role, "active": True, "capabilities": ["add_comment"]},
            {"name": "director", "active": True, "capabilities": ["set_manually_controlled", "merge", "add_comment"]}]}}
        if role == "director":
            self.workflow["document"]["roles"] = self.workflow["document"]["roles"][1:]
        self.assignments: dict = {}
        self.reservations: dict = {role: None}
        self.leases: list = []
        self.holds: dict = {"holds": []}

    def opener(self, url: str, timeout: float = 0):
        if self.down:
            raise OSError("connection refused")
        path = url.split("/api/", 1)[1]
        body = {"workflow": self.workflow, "runtime-assignments": {"assignments": self.assignments},
                "reservations": {"reservations": self.reservations}, "execution-leases": {"leases": self.leases},
                "provider-holds": self.holds}[path]
        return io.BytesIO(json.dumps(body).encode())


@contextmanager
def project(*, report_link: bool = False):
    with tempfile.TemporaryDirectory(prefix="syrd750-") as raw:
        base = Path(raw)
        path = base / "p750.json"
        doc = {"project": "p750", "board_url": "http://127.0.0.1:1/",
               "roles": [{"role": "app", "slot": 0, "workdir": f"{raw}/app", "cli": ["claude"]},
                         {"role": "director", "slot": 1, "workdir": f"{raw}/director", "cli": ["codex"]}]}
        if report_link:
            doc.update({"upstream_report_url": "http://127.0.0.1:2/", "upstream_report_token_file": TOKEN_FILE})
        path.write_text(json.dumps(doc))
        config = launcher.load_project_config("p750", path)
        (base / "proc").mkdir()
        yield config, path, Kernel(base / "proc"), base


class Rig:
    """One restart's seams: the pane (pid, alive), the reload's effect, the gate, the successor."""

    def __init__(self, config, path: Path, kernel: Kernel, base: Path, role: str = "app") -> None:
        self.config, self.path, self.kernel, self.role = config, path, kernel, role
        self.board = Board(role)
        self.pane_pid = 4100
        self.alive = True
        self.busy = False
        self.own = False
        self.arm_problem = ""
        self.reloads: list[tuple[str, str]] = []
        self.armed: list[str] = []
        self.released: list[str] = []
        self.next = (4200, 900)
        self.new_env: dict[str, str] | None = None  # None: what the launcher gives the role now
        self.said: list[str] = []
        self.lockdir = base / "locks"
        # Who is asking: the Director's registered pane by default; None is a process in no pane.
        self.caller: str | None = "director"
        self.caller_pane = (3900, 300)
        self.operator = False
        self.lock_free_at_arm: bool | None = None
        self.me = 999_999  # this command's own process: in no pane of the stand-in kernel unless a case puts it there
        self.keep_old = False  # the reload leaves the old pane's root running
        self.ensure_then = lambda: None  # what else the reload leaves behind
        if role != "director":
            kernel.spawn(*self.caller_pane)
            self.board.assignments["director"] = {"process_pid": 3900, "process_start_time": 300, "generation": 3}

    def runner(self, argv, **_kw):
        if "has-session" in argv:
            return subprocess.CompletedProcess(argv, 0 if self.alive else 1, "", "")
        if "display-message" in argv:
            return subprocess.CompletedProcess(argv, 0 if self.alive else 1, f"{self.pane_pid}\n", "")
        raise AssertionError(f"unexpected tmux call {argv}")

    def ensure(self, role, *, mode, **_kw):
        self.reloads.append((role.role, mode))
        old = self.pane_pid
        if self.alive and old and not self.keep_old:
            self.kernel.kill(old)
        pid, start = self.next
        env = self.new_env if self.new_env is not None else role_restart.expected_board_environment(self.config, role)
        # As a shell such as fish starts it: the pane root is the shell, and the command runs as its child.
        self.kernel.spawn(pid, start, {"SHELL": "/usr/bin/fish"}, comm="fish")
        self.kernel.spawn(pid + 1, start + 1, env, ppid=pid)
        self.board.assignments[role.role] = {"process_pid": pid, "process_start_time": start, "generation": 8}
        self.pane_pid, self.alive = pid, True
        self.ensure_then()
        return 0

    def own_pane_role(self, config, roles, **_kw):
        if len(roles) > 1:  # whose pane the caller is in, among every role
            return next((r for r in roles if r.role == self.caller), None)
        return roles[0] if self.own else None  # whether the target is the caller's own pane

    def arm(self, config, role, **_kw):
        import fcntl
        with open(self.lockdir / f"{role.role}.lock", "a+") as probe:  # the successor takes the role's lock
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.lock_free_at_arm = True
            except OSError:
                self.lock_free_at_arm = False
        self.armed.append(role.role)
        return self.arm_problem

    def run(self, **kw) -> int:
        lock = lambda config, role: self.lockdir / f"{role.role}.lock"  # noqa: E731
        pane_root = (lambda: None) if self.caller is None else (lambda: SimpleNamespace(
            pid=self.caller_pane[0], start_time=self.caller_pane[1]))
        with mock.patch.object(role_runtime, "_is_busy", lambda *a, **k: self.busy), \
                mock.patch.object(role_self_restart, "own_pane_role", self.own_pane_role), \
                mock.patch.object(role_self_restart, "caller_pane_root", pane_root), \
                mock.patch.object(role_restart, "root_parent_ran_me", lambda proc_root: self.operator), \
                mock.patch.object(role_restart, "_me", lambda: self.me), \
                mock.patch.object(role_self_restart, "arm_successor", self.arm), \
                mock.patch.object(role_self_restart, "release_armed",
                                  lambda config, role, **k: self.released.append(role.role) or ""), \
                mock.patch.object(role_self_restart, "lock_path", lock):
            return role_restart.restart_role_command(
                self.config, config_path=self.path, role_name=self.role, runner=self.runner,
                opener=self.board.opener, ensure=self.ensure, sleep=lambda s: None,
                clock=iter(range(0, 10_000)).__next__, proc_root=self.kernel.root, print_func=self.said.append, **kw)


def registered(rig: Rig, pid: int = 4100, start: int = 500) -> None:
    rig.kernel.spawn(pid, start, {"TICKET_BOARD_CALLER_ROLE": rig.role})
    rig.board.assignments[rig.role] = {"process_pid": pid, "process_start_time": start, "generation": 7}


def test_another_role_is_restarted_registered_and_current() -> None:
    with project(report_link=True) as (config, path, kernel, base):
        rig = Rig(config, path, kernel, base)
        registered(rig)
        code = rig.run()
        check(code == 0 and rig.reloads == [("app", "reload")] and rig.armed == [],
              f"one reload, of that role alone, in its own session: {rig.reloads} {rig.said}")
        check(any("pid 4200" in s and "replaces 4100" in s and "registered by the board" in s for s in rig.said),
              f"it says the new process replaced the old one and the board registered it: {rig.said}")
        wanted = role_restart.expected_board_environment(config, config.roles[0])
        check(wanted.get("TICKET_BOARD_TENANT_REPORT_TOKEN_FILE") == TOKEN_FILE,
              f"a report link provisioned since the last start is among what the launcher gives the role now: {wanted}")
        # A replacement that came back without it is no restart.
        rig2 = Rig(config, path, kernel, base)
        registered(rig2, 4300, 600)
        rig2.pane_pid, rig2.next = 4300, (4400, 950)
        rig2.new_env = {k: v for k, v in wanted.items() if k != "TICKET_BOARD_TENANT_REPORT_TOKEN_FILE"}
        check(rig2.run() == 1 and any("without the current TICKET_BOARD_TENANT_REPORT_TOKEN_FILE" in s for s in rig2.said),
              f"a new pane missing the report link is reported as a failed restart: {rig2.said}")


def test_a_reload_that_replaced_nothing_is_no_restart() -> None:
    with project() as (config, path, kernel, base):
        rig = Rig(config, path, kernel, base)
        registered(rig)
        rig.ensure = lambda role, **kw: rig.reloads.append((role.role, kw["mode"])) or 0  # says 0, ends and starts nothing
        check(rig.run() == 1 and any("did not come back registered" in s for s in rig.said),
              f"the old process still registered and running is not reported as its own replacement: {rig.said}")


def test_refusals_change_nothing() -> None:
    cases = {
        "a live turn": lambda r: setattr(r, "busy", True),
        "a leased command": lambda r: r.board.leases.append({"id": 9, "role": "app", "command": "pytest"}),
        "a quota hold": lambda r: r.board.holds["holds"].append({"role": "app", "current": True}),
        "a safety hold on its account": lambda r: r.board.holds.update(
            {"accounts": {"held": [{"runtime": "claude", "roles": ["app"]}]}}),
        "a held ticket not named": lambda r: r.board.reservations.update(
            {"app": {"ticket": "P750-3", "state": "in_progress"}}),
        "a pane that is not the registration": lambda r: r.board.assignments["app"].update({"process_pid": 9999}),
        "a role not active in the workflow": lambda r: r.board.workflow["document"]["roles"][0].update({"active": False}),
        "a board that does not answer": lambda r: setattr(r.board, "down", True),
    }
    words = {"a live turn": "working", "a leased command": "lease 9", "a quota hold": "held for quota",
             "a safety hold on its account": "claude account is held", "a held ticket not named": "--while-holding P750-3",
             "a pane that is not the registration": "not the registered process",
             "a role not active in the workflow": "not an active role", "a board that does not answer": "did not answer"}
    for label, setup in cases.items():
        with project() as (config, path, kernel, base):
            rig = Rig(config, path, kernel, base)
            registered(rig)
            setup(rig)
            code = rig.run()
            check(code == 1 and rig.reloads == [] and rig.armed == [] and any(words[label] in s for s in rig.said),
                  f"{label}: refused, saying why, and nothing reloaded or armed: {rig.said}")
    with project() as (config, path, kernel, base):
        rig = Rig(config, path, kernel, base)
        registered(rig)
        rig.board.reservations["app"] = {"ticket": "P750-3", "state": "in_progress"}
        check(rig.run(while_holding=["P750-3"]) == 0 and rig.reloads == [("app", "reload")],
              f"a held ticket named with --while-holding goes ahead; its conversation resumes: {rig.said}")
    with project() as (config, path, kernel, base):
        rig = Rig(config, path, kernel, base, role="ghost")
        try:
            rig.run()
            refused = ""
        except SystemExit as exc:
            refused = str(exc)
        check("has no role ghost" in refused and "Nothing was changed" in refused, f"a role not configured: {refused}")


def test_only_the_registered_control_role_or_the_operator_may() -> None:
    """Audit on dc37fe16: authority is the kernel's and the board's, never the caller's say."""
    cases = {
        "a sibling role's pane": dict(caller="app", target_is="director"),
        "a process in no pane, not root's operator": dict(caller=None),
        "the Director's pane, but not the process the board registered": dict(caller="director", stale=True),
        "a pane of no role of the project": dict(caller="stranger"),
    }
    words = {"a sibling role's pane": "may not restart roles",
             "a process in no pane, not root's operator": "`sudo switchyard restart-role p750 <role>`",
             "the Director's pane, but not the process the board registered": "not the process the board registered as director",
             "a pane of no role of the project": "not one of this project's roles"}
    for label, how in cases.items():
        with project() as (config, path, kernel, base):
            target = "director" if how.get("target_is") == "director" else "app"
            rig = Rig(config, path, kernel, base, role=target)
            registered(rig, 4600, 650)
            rig.pane_pid = 4600
            rig.board.reservations[target] = None
            if target == "director":
                rig.board.assignments["app"] = {"process_pid": 3900, "process_start_time": 300, "generation": 2}
                rig.board.workflow["document"]["roles"].append({"name": "app", "active": True, "capabilities": ["add_comment"]})
            rig.caller = how.get("caller")
            if how.get("stale"):
                rig.board.assignments["director"]["process_start_time"] = 299
            code = rig.run()
            check(code == 1 and rig.reloads == [] and rig.armed == [] and any(words[label] in s for s in rig.said),
                  f"{label}: refused, nothing reloaded or armed: {rig.said}")
    with project() as (config, path, kernel, base):
        rig = Rig(config, path, kernel, base)
        registered(rig)
        rig.caller, rig.operator = None, True
        check(rig.run() == 0 and rig.reloads == [("app", "reload")],
              f"started by root's runuser as the operator, from no pane: allowed: {rig.said}")


def test_the_operator_is_proved_from_the_parent_process() -> None:
    """Only root's runuser running exactly this command is the operator; anything else of the account is not."""
    import pwd

    me = pwd.getpwuid(os.getuid()).pw_name
    argv = [sys.executable, "-I", "-B", "/x/scripts/role_restart.py", "operator", "p750", "/x/p750.json", "app"]
    with tempfile.TemporaryDirectory(prefix="syrd750-ppid.") as raw:
        proc = Path(raw)

        def parent(uid: str, cmdline: list[str]) -> bool:
            d = proc / "4242"
            d.mkdir(exist_ok=True)
            (d / "status").write_text(f"Name:\tx\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
            (d / "cmdline").write_bytes(b"\0".join(w.encode() for w in cmdline) + b"\0")
            with mock.patch.object(os, "getppid", lambda: 4242), mock.patch.object(sys, "orig_argv", argv):
                return role_restart.root_parent_ran_me(proc)

        check(parent("0", ["runuser", "-u", me, "--", *argv]) is True, "root's runuser running exactly this: the operator")
        check(parent("1006", ["runuser", "-u", me, "--", *argv]) is False, "the same command line from a parent not root's")
        check(parent("0", ["runuser", "-u", me, "--", *argv[:-1], "director"]) is False, "root's runuser, but not this command")
        check(parent("0", ["/usr/lib/systemd/systemd", "--switched-root"]) is False,
              "an orphan adopted by init, root's but no runuser of this command")
        check(parent("0", ["runuser", "-u", "someone-else", "--", *argv]) is False, "a runuser for another account")


def test_root_is_never_served_as_root() -> None:
    with project() as (config, path, kernel, base):
        config = __import__("dataclasses").replace(config, run_as_user="p750-owner")
        ran: list = []
        with mock.patch.object(os, "geteuid", lambda: 0):
            code = role_restart.restart_role_command(
                config, config_path=path, role_name="app", while_holding=["P750-3"],
                runner=lambda argv, **k: ran.append(argv) or subprocess.CompletedProcess(argv, 7), print_func=lambda s: None)
        check(code == 7 and len(ran) == 1 and ran[0][:4] == ["runuser", "-u", "p750-owner", "--"]
              and ran[0][4:8] == [sys.executable, "-I", "-B", str(ROOT / "scripts" / "role_restart.py")]
              and ran[0][8:] == ["operator", "p750", str(path), "app", "--while-holding", "P750-3"],
              f"root re-runs it as the owner, through runuser, with exactly what was asked: {ran}")


def test_a_live_pane_the_board_has_not_registered_is_not_restarted() -> None:
    """Audit on dc37fe16: no registration is unverified identity, not a disconnected role."""
    with project() as (config, path, kernel, base):
        rig = Rig(config, path, kernel, base)
        kernel.spawn(4100, 500, {"TICKET_BOARD_CALLER_ROLE": "app"})
        rig.board.assignments.pop("app", None)
        code = rig.run()
        check(code == 1 and rig.reloads == [] and any("no registration for app" in s for s in rig.said),
              f"a live pane the board has not bound to the role is refused: {rig.said}")


def test_a_second_restart_in_flight_is_refused() -> None:
    """Audit on dc37fe16: one lock for every path -- the successor's own, held through its whole restart."""
    import fcntl
    with project() as (config, path, kernel, base):
        rig = Rig(config, path, kernel, base)
        registered(rig)
        rig.lockdir.mkdir()
        with open(rig.lockdir / "app.lock", "a+") as held:  # a successor (or another restart) in flight
            fcntl.flock(held, fcntl.LOCK_EX)
            code = rig.run()
        check(code == 1 and rig.reloads == [] and any("in flight" in s for s in rig.said),
              f"a restart while the role's successor or another restart holds its lifecycle lock is refused: {rig.said}")


def test_a_disconnected_role_is_started_and_must_register_anew() -> None:
    with project() as (config, path, kernel, base):
        rig = Rig(config, path, kernel, base)
        rig.board.assignments["app"] = {"process_pid": 4100, "process_start_time": 500, "generation": 7}  # dead
        rig.alive, rig.pane_pid = False, 0
        code = rig.run()
        check(code == 0 and rig.reloads == [("app", "attach-or-start")]
              and any(s.startswith("switchyard: started app: pid 4200") for s in rig.said),
              f"a pane gone while the board is up is started, and registers as a new process: {rig.reloads} {rig.said}")
        rig2 = Rig(config, path, kernel, base)
        rig2.board.assignments["app"] = {"process_pid": 4100, "process_start_time": 500, "generation": 7}
        rig2.alive, rig2.pane_pid = False, 0
        rig2.ensure = lambda role, **kw: rig2.reloads.append((role.role, kw["mode"])) or 0  # starts nothing
        check(rig2.run() == 1 and any("did not come back registered" in s for s in rig2.said),
              f"a start the board never registers is a failure, not a recovery: {rig2.said}")


def in_own_pane(rig: Rig) -> None:
    """The command runs in the Director's own registered pane: pane root 4100 -> its CLI -> the tool's shell -> this."""
    registered(rig)
    rig.caller_pane = (4100, 500)
    rig.board.reservations["director"] = None
    rig.own = True
    rig.kernel.spawn(4101, 501, comm="codex", ppid=4100)
    rig.kernel.spawn(4102, 502, comm="bash", ppid=4101)
    rig.kernel.spawn(4103, 503, comm="python3", ppid=4102)
    rig.me = 4103


def test_the_directors_own_pane_goes_through_a_successor() -> None:
    with project() as (config, path, kernel, base):
        rig = Rig(config, path, kernel, base, role="director")
        in_own_pane(rig)
        code = rig.run()
        check(code == 0 and rig.reloads == [] and rig.armed == ["director"] and rig.released == ["director"]
              and any("ends once this command exits" in s for s in rig.said),
              f"its own pane is never reloaded in-process: a successor is armed and released: {rig.said}")
        check(rig.lock_free_at_arm is True,
              "the command hands the role's lifecycle lock to the successor, which holds it for the whole restart")


def test_the_directors_own_turn_is_no_refusal_but_other_work_is() -> None:
    """Audit on 007a5f84: the turn that runs the command is busy until Stop; the successor waits it out."""
    with project() as (config, path, kernel, base):
        rig = Rig(config, path, kernel, base, role="director")
        in_own_pane(rig)
        rig.busy = True  # UserPromptSubmit wrote busy; Stop has not come
        kernel.spawn(4104, 504, comm="git", ppid=4103)  # this command's own child: part of it
        kernel.spawn(4105, 505, comm="sleep", ppid=4101, state="Z")  # a finished tool command: ended
        code = rig.run()
        check(code == 0 and rig.armed == ["director"] and rig.released == ["director"],
              f"from the normal busy state of the turn that runs it, its own pane goes through the successor: {rig.said}")
    with project() as (config, path, kernel, base):
        rig = Rig(config, path, kernel, base, role="director")
        in_own_pane(rig)
        rig.busy = True
        kernel.spawn(4110, 510, comm="pytest", ppid=4101)  # a background command of the CLI's
        code = rig.run()
        check(code == 1 and rig.armed == [] and any("runs work besides this command (pid 4110)" in s for s in rig.said),
              f"a background command still running from its own pane is refused, nothing armed: {rig.said}")
    with project() as (config, path, kernel, base):
        rig = Rig(config, path, kernel, base, role="director")
        in_own_pane(rig)
        rig.board.leases.append({"id": 4, "role": "director", "command": "sweep"})
        check(rig.run() == 1 and rig.armed == [] and any("under lease 4" in s for s in rig.said),
              f"its own leased command still refuses: {rig.said}")
    with project() as (config, path, kernel, base):
        rig = Rig(config, path, kernel, base)  # another role: its turn is never this command's
        registered(rig)
        rig.busy = True
        check(rig.run() == 1 and rig.reloads == [] and any("is working" in s for s in rig.said),
              f"another role's live turn still refuses: {rig.said}")


def test_success_is_said_only_once_the_old_pane_has_ended() -> None:
    """Audit on 007a5f84: a new registration is not proof the old process is gone."""
    with project() as (config, path, kernel, base):
        rig = Rig(config, path, kernel, base)
        registered(rig)
        rig.keep_old = True
        code = rig.run()
        check(code == 1 and any("old pane's process(es) 4100 are still running" in s for s in rig.said)
              and not any("restarted app" in s for s in rig.said),
              f"the old pane process still running past the bound: a failed restart, never success: {rig.said}")
    with project() as (config, path, kernel, base):
        rig = Rig(config, path, kernel, base)
        registered(rig)
        kernel.spawn(4101, 501, ppid=4100)  # the old pane's command, under its shell
        rig.ensure_then = lambda: kernel.spawn(4101, 501, ppid=1)  # it outlives the hang-up, reparented
        code = rig.run()
        check(code == 1 and any("process(es) 4101 are still running" in s for s in rig.said),
              f"the old pane's command outliving its root is a failed restart too: {rig.said}")
    with project() as (config, path, kernel, base):
        rig = Rig(config, path, kernel, base)
        registered(rig)
        rig.keep_old = True
        rig.ensure_then = lambda: kernel.spawn(4100, 500, comm="fish", state="Z")  # ended; not yet collected
        code = rig.run()
        check(code == 0 and any("replaces 4100, which has ended" in s for s in rig.said),
              f"a zombie has ended: only its exit status waits for a parent: {rig.said}")
        rig2 = Rig(config, path, kernel, base)
        registered(rig2, 4300, 600)
        rig2.pane_pid, rig2.next, rig2.keep_old = 4300, (4400, 950), True
        rig2.ensure_then = lambda: kernel.spawn(4300, 601)  # its pid, reused by another process
        check(rig2.run() == 0, f"a pid reused by a new process is not the old process: {rig2.said}")


def test_every_depth_of_the_pane_is_counted() -> None:
    """Audit on c35705da: a walk capped at eight generations missed the ninth and below."""
    with project() as (config, path, kernel, base):
        kernel.spawn(4000, 100, comm="fish")
        for depth in range(1, 13):
            kernel.spawn(4000 + depth, 100 + depth, ppid=4000 + depth - 1)
        tree = role_restart.pane_tree(4000, kernel.root)
        check(sorted(tree) == list(range(4000, 4013)) and tree[4012] == 112,
              f"the pane's root and all twelve generations below it: {sorted(tree)}")
    with project() as (config, path, kernel, base):
        rig = Rig(config, path, kernel, base)
        registered(rig)
        for depth in range(1, 11):  # the old pane's chain, ten generations deep
            kernel.spawn(4100 + depth, 500 + depth, ppid=4100 + depth - 1)

        def reparent_the_deepest() -> None:  # the reload ended all of it but the tenth, which init adopted
            for depth in range(1, 10):
                kernel.kill(4100 + depth)
            kernel.spawn(4110, 510, ppid=1)
        rig.ensure_then = reparent_the_deepest
        code = rig.run()
        check(code == 1 and any("process(es) 4110 are still running" in s for s in rig.said)
              and not any("which has ended" in s for s in rig.said),
              f"an old process below the former depth cap that survives is a failed restart: {rig.said}")
    with project() as (config, path, kernel, base):
        rig = Rig(config, path, kernel, base, role="director")
        in_own_pane(rig)
        for depth in range(1, 11):  # this command's own work, ten generations below it: part of it
            kernel.spawn(4200 + depth, 600 + depth, ppid=4103 if depth == 1 else 4200 + depth - 1)
        parent = 4101
        for depth in range(1, 11):  # a background command of the CLI's, its leaf ten generations down
            kernel.spawn(4300 + depth, 700 + depth, ppid=parent)
            parent = 4300 + depth
        code = rig.run()
        check(code == 1 and rig.armed == [] and any("4309, 4310)" in s for s in rig.said)
              and not any("421" in s for s in rig.said),
              f"live work deep in its own pane is refused, its own deep children are not: {rig.said}")


def test_the_directors_own_pane_with_no_successor() -> None:
    with project() as (config, path, kernel, base):
        rig2 = Rig(config, path, kernel, base, role="director")
        in_own_pane(rig2)
        rig2.arm_problem = "no recorded conversation"
        check(rig2.run() == 1 and rig2.reloads == [] and rig2.released == []
              and any("`sudo switchyard restart-role p750 director`" in s and "outside the pane" in s for s in rig2.said),
              f"no successor: nothing ended, and the operator's exact command: {rig2.said}")


def test_discoverable_where_a_restart_is_needed() -> None:
    from scripts import switchyard_commands

    help_text = switchyard_commands.switchyard_help_text()
    check("restart-role" in launcher.SWITCHYARD_COMMANDS and "restart-role" in switchyard_commands.SWITCHYARD_UNPRIVILEGED_COMMANDS
          and "restart-role     restart one role's pane" in help_text,
          "switchyard --help names restart-role, an owner's (unprivileged) command")
    for path, needle in (("scripts/tenant_release_report.py", "switchyard restart-role"),
                         ("scripts/upstream_report_sinks.py", "switchyard restart-role"),
                         ("scripts/upstream_report.py", "switchyard restart-role"),
                         ("scripts/ticket_board/write_cli.py", "switchyard restart-role"),
                         ("docs/board-skill.md", "switchyard restart-role"),
                         ("skills/switchyard-director/SKILL.md", "switchyard restart-role")):
        check(needle in (ROOT / path).read_text(), f"{path} names the command where a restart is needed")
    guide = (ROOT / "docs/onboarding/switchyard-director-guide.md").read_text()
    check("## Restarting a role's pane" in guide and "restarts no pane" in guide
          and "needed only when the board, its listener or the" in guide and "restart that role (`switchyard restart-role" in guide,
          "the Director guide names it, says when stop/start is still needed, and that reopening the window is no restart")


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"role_restart_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
