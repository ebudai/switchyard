#!/usr/bin/env python3
"""SYRD-193: `stop` suspends a tenant, reversibly, and only that tenant.

`switchyard stop` killed the role, viewer and display sessions and left the
board and notification listener running and the Konsole window standing. The
operator got a stranded multi-pane window over live services, and a process that
had escaped its pane survived the whole thing. Closing the window was already
the presentation-only operation, so the public verb had no distinct meaning
between that and destructive `teardown`.

It is now a reversible whole-tenant suspension: window, sessions, escaped
processes, listener, board -- in the order each consumer must go before what it
consumes -- preserving every byte of restart state.

Two identities do the scoping work here and they are deliberately different.
The presentation window is found by the absolute layout path in its own argv,
because Konsole never execs away from it. An escaped role process is found by
`TICKET_BOARD_PROJECT` in its ENVIRONMENT, because environment survives exec and
argv does not -- which is the mistake SYRD-169 was. Using either one for the
other's job is how a stop reaches another tenant or misses its own work.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from team_launcher_test_helpers import *

CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    assert condition, message
    CHECKS += 1


def _fake_proc(root: Path, pid: int, *, argv: list[str], environ: dict[str, str] | None = None,
               ppid: int = 1) -> None:
    entry = root / str(pid)
    entry.mkdir(parents=True, exist_ok=True)
    entry.joinpath("cmdline").write_bytes(("\0".join(argv) + "\0").encode("utf-8"))
    environment = environ or {}
    entry.joinpath("environ").write_bytes(
        ("\0".join(f"{k}={v}" for k, v in environment.items()) + "\0").encode("utf-8")
    )
    # A comm with a space and a closing parenthesis: the parent is field 4 after
    # the LAST ')', and a whitespace split reads the wrong one.
    fields = ["0"] * 50
    fields[1] = str(ppid)
    entry.joinpath("stat").write_text(
        f"{pid} (claude (pane)) S {ppid} " + " ".join(fields) + "\n", encoding="utf-8"
    )


def _project(tmp: Path, project: str = "atlas"):
    config_path = _write_launcher_config(tmp, project=project)
    return team_launcher.load_project_config(project, config_path), config_path


def test_the_window_is_found_by_its_layout_not_by_the_project_name() -> None:
    """A sibling tenant's window must not be closed by a prefix match."""
    with tempfile.TemporaryDirectory(prefix="syrd193-window.") as tmp_name:
        tmp = Path(tmp_name)
        config, config_path = _project(tmp)
        layout = str(team_launcher.desktop_presentation_layout_path(
            config, config_path=config_path, gui_user=""
        ))
        proc = tmp / "proc"
        proc.mkdir()
        _fake_proc(proc, 100, argv=["/usr/sbin/konsole", "--separate", "--layout", layout])
        # The same shape for a tenant whose slug has this one as a prefix, and a
        # window whose title matches but whose layout does not.
        _fake_proc(proc, 101, argv=["/usr/sbin/konsole", "--layout", layout.replace("atlas", "atlas-staging")])
        _fake_proc(proc, 102, argv=["/usr/sbin/konsole", "--qwindowtitle", config.project_name])
        _fake_proc(proc, 103, argv=["/usr/bin/vim", layout])
        # An argument that CONTAINS the layout path without being it. A
        # substring match would close this window; it is not this project's.
        _fake_proc(proc, 104, argv=["/usr/sbin/konsole", "--layout", layout + ".backup"])
        _fake_proc(proc, 105, argv=["/usr/sbin/konsole", f"--layout={layout}"])
        found = team_launcher.presentation_window_processes(
            config, config_path=config_path, proc_root=proc
        )
    pids = sorted(window.pid for window in found)
    check(pids == [100, 105], f"only this project's windows are matched: {pids}")
    check(104 not in pids, "a path that merely starts with the layout's is a different file")
    check(101 not in pids, "and a sibling tenant's layout is not this one")


def test_closing_the_window_signals_only_that_window() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd193-close.") as tmp_name:
        tmp = Path(tmp_name)
        config, config_path = _project(tmp)
        layout = str(team_launcher.desktop_presentation_layout_path(
            config, config_path=config_path, gui_user=""
        ))
        proc = tmp / "proc"
        proc.mkdir()
        _fake_proc(proc, 200, argv=["/usr/sbin/konsole", "--layout", layout])
        _fake_proc(proc, 201, argv=["/usr/sbin/konsole", "--layout", "/other/tenant-layout.json"])
        signalled: list[tuple[int, int]] = []
        said: list[str] = []
        problems = team_launcher.close_presentation_window(
            config, config_path=config_path, proc_root=proc,
            signaller=lambda pid, sig: signalled.append((pid, sig)),
            print_func=said.append,
        )
    check(problems == [], f"the window closes cleanly: {problems}")
    check(signalled == [(200, 15)], f"exactly one window, terminated not killed: {signalled}")
    check(any("closed presentation window" in line for line in said), f"and says so: {said}")


def test_closing_an_already_closed_window_is_a_clean_no_op() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd193-idem.") as tmp_name:
        tmp = Path(tmp_name)
        config, config_path = _project(tmp)
        proc = tmp / "proc"
        proc.mkdir()
        said: list[str] = []
        problems = team_launcher.close_presentation_window(
            config, config_path=config_path, proc_root=proc,
            signaller=lambda pid, sig: (_ for _ in ()).throw(AssertionError("signalled nothing")),
            print_func=said.append,
        )
    check(problems == [], "a closed window is not a failure")
    check(any("already closed" in line for line in said), f"and is reported as such: {said}")


def test_an_escaped_pane_child_is_found_by_environment_not_argv() -> None:
    """The SYRD-169 lesson, applied the other way round."""
    with tempfile.TemporaryDirectory(prefix="syrd193-escape.") as tmp_name:
        tmp = Path(tmp_name)
        config, config_path = _project(tmp)
        proc = tmp / "proc"
        proc.mkdir()
        # Exec'd away from any marker in its argv; the environment is inherited.
        _fake_proc(proc, 300, argv=["/usr/bin/python3", "train.py"],
                   environ={"TICKET_BOARD_PROJECT": "atlas", "HOME": "/home/atlas-agent"})
        # Another tenant's work, same uid, same binary.
        _fake_proc(proc, 301, argv=["/usr/bin/python3", "train.py"],
                   environ={"TICKET_BOARD_PROJECT": "atlas-staging"})
        # Something with the slug in argv but not in its environment.
        _fake_proc(proc, 302, argv=["/usr/bin/grep", "atlas"], environ={})
        residual = team_launcher.residual_project_processes(config, proc_root=proc)
    pids = sorted(process.pid for process in residual)
    check(pids == [300], f"only this project's escaped work is in scope: {pids}")


def test_a_stop_run_from_inside_a_pane_does_not_stop_itself() -> None:
    """The command inherits the marker it is searching for."""
    with tempfile.TemporaryDirectory(prefix="syrd193-self.") as tmp_name:
        tmp = Path(tmp_name)
        config, config_path = _project(tmp)
        proc = tmp / "proc"
        proc.mkdir()
        mine = os.getpid()
        parent = 4242
        _fake_proc(proc, mine, argv=["switchyard", "stop", "atlas"],
                   environ={"TICKET_BOARD_PROJECT": "atlas"}, ppid=parent)
        _fake_proc(proc, parent, argv=["/bin/bash"],
                   environ={"TICKET_BOARD_PROJECT": "atlas"}, ppid=1)
        _fake_proc(proc, 400, argv=["/usr/bin/python3", "work.py"],
                   environ={"TICKET_BOARD_PROJECT": "atlas"})
        residual = team_launcher.residual_project_processes(config, proc_root=proc)
    pids = sorted(process.pid for process in residual)
    check(mine not in pids, f"the running stop is not residual work: {pids}")
    check(parent not in pids, f"nor is the shell that launched it: {pids}")
    check(pids == [400], f"the genuinely escaped process still is: {pids}")


def test_residual_processes_that_cannot_be_stopped_are_reported_precisely() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd193-refuse.") as tmp_name:
        tmp = Path(tmp_name)
        config, config_path = _project(tmp)
        proc = tmp / "proc"
        proc.mkdir()
        _fake_proc(proc, 500, argv=["/usr/bin/python3", "long.py"],
                   environ={"TICKET_BOARD_PROJECT": "atlas"})

        def refuse(pid: int, sig: int) -> None:
            raise PermissionError("not permitted")

        problems = team_launcher.contain_residual_project_processes(
            config, proc_root=proc, signaller=refuse, print_func=lambda _line: None
        )
    check(len(problems) == 1, f"the survivor is reported: {problems}")
    check("500" in problems[0] and "long.py" in problems[0],
          f"naming the pid and what it is: {problems[0]}")


def test_the_board_is_acted_on_in_the_system_scope() -> None:
    """A `--user` stop for a system unit stops nothing and returns cleanly."""
    with tempfile.TemporaryDirectory(prefix="syrd193-scope.") as tmp_name:
        tmp = Path(tmp_name)
        config, _ = _project(tmp)
        seen: list[list[str]] = []

        def runner(args, **_kwargs):
            seen.append(list(args))
            stdout = "inactive" if args[:2] == ["systemctl", "is-active"] else ""
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

        problems = team_launcher._board_system_unit_action(config, "stop", runner=runner)
    check(problems == [], f"the board stops: {problems}")
    check(seen[0] == ["systemctl", "stop", "atlas-ticket-board.service"],
          f"in the system scope, with no --user: {seen[0]}")
    check(all("--user" not in args for args in seen), f"never the owner's manager: {seen}")
    check(["systemctl", "is-active", "atlas-ticket-board.service"] in seen,
          f"and the state is read back rather than assumed: {seen}")


def test_a_board_that_says_it_is_still_active_is_not_reported_stopped() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd193-liar.") as tmp_name:
        tmp = Path(tmp_name)
        config, _ = _project(tmp)

        def runner(args, **_kwargs):
            stdout = "active" if args[:2] == ["systemctl", "is-active"] else ""
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

        problems = team_launcher._board_system_unit_action(config, "stop", runner=runner)
    check(len(problems) == 1 and "is active after stop" in problems[0],
          f"a clean exit is not proof the unit went down: {problems}")


def test_resume_stops_at_the_boundary_that_failed() -> None:
    """Sessions started against a board that did not come up cannot register."""
    with tempfile.TemporaryDirectory(prefix="syrd193-boundary.") as tmp_name:
        tmp = Path(tmp_name)
        config, config_path = _project(tmp)

        def runner(args, **_kwargs):
            if args[:2] == ["systemctl", "is-active"]:
                return subprocess.CompletedProcess(args, 3, stdout="inactive", stderr="")
            if args[:2] == ["systemctl", "start"]:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="unit failed")
            raise AssertionError(f"nothing after the board should run: {args}")

        problems = team_launcher.resume_tenant(
            config, config_path=config_path, runner=runner, print_func=lambda _line: None
        )
    check(any("could not start" in problem for problem in problems), f"the failure is named: {problems}")
    check(any("nothing after it was started" in problem for problem in problems),
          f"and so is the boundary it stopped at: {problems}")


def test_resuming_a_running_tenant_does_not_bounce_it() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd193-noop.") as tmp_name:
        tmp = Path(tmp_name)
        config, config_path = _project(tmp)
        seen: list[list[str]] = []

        def runner(args, **_kwargs):
            seen.append(list(args))
            if args[:2] == ["systemctl", "is-active"]:
                return subprocess.CompletedProcess(args, 0, stdout="active", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="active", stderr="")

        said: list[str] = []
        problems = team_launcher.resume_tenant(
            config, config_path=config_path, runner=runner, print_func=said.append
        )
    check(problems == [], f"a running tenant resumes cleanly: {problems}")
    check(not any(args[:2] == ["systemctl", "start"] for args in seen),
          f"without restarting the board: {seen}")
    # Joined, not element-wise: the owner's manager is reached through an
    # `env ... sh -c "systemctl --user restart UNIT"`, so the verb is inside a
    # string rather than an argument of its own.
    check(not any("restart" in " ".join(str(part) for part in args) for args in seen),
          f"and without bouncing the listener that is already delivering: {seen}")
    check(any("already running" in line for line in said), f"and says it was already up: {said}")


def test_the_status_vocabulary_separates_the_five_states() -> None:
    def runtime(**kwargs):
        return team_launcher.TenantRuntime(project="atlas", **kwargs)

    check(runtime(board_active=True, listener_active=True, live_sessions=("a",),
                  presentation_open=True).state == "running", "all up is running")
    check(runtime(board_active=True, listener_active=True, live_sessions=("a",),
                  presentation_open=False).state == "presentation-closed",
          "a closed window over live services is presentation-closed")
    check(runtime(board_active=False, listener_active=False, live_sessions=(),
                  presentation_open=False).state == "suspended", "nothing up is suspended")
    check(runtime(board_active=True, listener_active=False, live_sessions=(),
                  presentation_open=False).state == "partially-stopped",
          "a board still serving is not a suspension")
    check(runtime(board_active=False, listener_active=False, live_sessions=(),
                  presentation_open=False, residual=(77,)).state == "partially-stopped",
          "and neither is one with work still running outside its panes")


def test_a_status_row_does_not_invent_a_suspension_from_a_silent_probe() -> None:
    """"Not answered" is a third answer, and must not read as "down"."""
    row = team_launcher.SwitchyardProjectStatus(
        name="atlas", slug="atlas", state="stopped", panes_up=0, panes_total=3,
        config_path=Path("/x"), board_active=False, listener_active=None,
        presentation_open=False,
    )
    check(row.runtime_state == "stopped",
          f"an unanswered listener leaves the base state standing: {row.runtime_state}")
    proved = team_launcher.SwitchyardProjectStatus(
        name="atlas", slug="atlas", state="stopped", panes_up=0, panes_total=3,
        config_path=Path("/x"), board_active=False, listener_active=False,
        presentation_open=False,
    )
    check(proved.runtime_state == "suspended", f"a proved one does not: {proved.runtime_state}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"team_launcher_tenant_suspension_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
