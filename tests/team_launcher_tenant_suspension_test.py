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
               ppid: int = 1, cgroup: str = "") -> None:
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
    # The kernel's own two shapes, copied from this host rather than invented:
    #   0::/system.slice/testing-ticket-board.service
    #   0::/user.slice/user-1006.slice/user@1006.service/app.slice/<unit>
    entry.joinpath("cgroup").write_text(
        f"1:net_cls:/\n0::{cgroup or '/user.slice/user-1006.slice/user@1006.service/app.slice/tmux-spawn.scope'}\n",
        encoding="utf-8",
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
                   environ={"TICKET_BOARD_PROJECT": "atlas", "TICKET_BOARD_CALLER_ROLE": "main",
                            "HOME": "/home/atlas-agent"})
        # Another tenant's work, same uid, same binary.
        _fake_proc(proc, 301, argv=["/usr/bin/python3", "train.py"],
                   environ={"TICKET_BOARD_PROJECT": "atlas-staging", "TICKET_BOARD_CALLER_ROLE": "main"})
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
                   environ={"TICKET_BOARD_PROJECT": "atlas", "TICKET_BOARD_CALLER_ROLE": "app"},
                   ppid=parent)
        _fake_proc(proc, parent, argv=["/bin/bash"],
                   environ={"TICKET_BOARD_PROJECT": "atlas", "TICKET_BOARD_CALLER_ROLE": "app"},
                   ppid=1)
        _fake_proc(proc, 400, argv=["/usr/bin/python3", "work.py"],
                   environ={"TICKET_BOARD_PROJECT": "atlas", "TICKET_BOARD_CALLER_ROLE": "ops"})
        residual = team_launcher.residual_project_processes(config, proc_root=proc)
    pids = sorted(process.pid for process in residual)
    check(mine not in pids, f"the running stop is not residual work: {pids}")
    check(parent not in pids, f"nor is the shell that launched it: {pids}")
    check(pids == [400], f"the genuinely escaped process still is: {pids}")


def test_the_managed_board_and_listener_are_never_signalled_by_containment() -> None:
    """The Director's finding, reproduced from this host's real unit metadata.

    `testing-ticket-board.service` sets `TICKET_BOARD_PROJECT=testing` in the
    unit file, and the listener's process carries the same marker. Containment
    ran BEFORE the unit-scoped stops, so on the project marker alone it would
    SIGTERM both services directly: systemd would see a main process die on its
    own, and the `systemctl stop` that follows would be observing something this
    command had already killed, out of the documented order.

    The two shapes below are copied from this host:
      board     0::/system.slice/atlas-ticket-board.service
      listener  0::/user.slice/user-1006.slice/user@1006.service/app.slice/
                  atlas-ticket-board-notify-listener.service
    """
    with tempfile.TemporaryDirectory(prefix="syrd193-services.") as tmp_name:
        tmp = Path(tmp_name)
        config, config_path = _project(tmp)
        proc = tmp / "proc"
        proc.mkdir()
        # Exactly what the unit file sets, and nothing else: no caller role.
        _fake_proc(proc, 600, argv=["/usr/bin/python3", "ticket-board.py", "--port", "25310"],
                   environ={"TICKET_BOARD_PROJECT": "atlas", "TICKET_BOARD_PROCESS_AUTHORITY": "1"},
                   cgroup="/system.slice/atlas-ticket-board.service")
        _fake_proc(proc, 601, argv=["/usr/bin/python3", "notify_listener.py"],
                   environ={"TICKET_BOARD_PROJECT": "atlas"},
                   cgroup="/user.slice/user-1006.slice/user@1006.service/app.slice/"
                          "atlas-ticket-board-notify-listener.service")
        # A service that somehow also carries a caller role is STILL a service:
        # the cgroup is the kernel's record of unit membership and decides.
        _fake_proc(proc, 602, argv=["/usr/bin/python3", "canary.py"],
                   environ={"TICKET_BOARD_PROJECT": "atlas", "TICKET_BOARD_CALLER_ROLE": "director"},
                   cgroup="/system.slice/atlas-ticket-board-canary.service")
        # The thing containment is actually for.
        _fake_proc(proc, 603, argv=["/usr/bin/python3", "escaped.py"],
                   environ={"TICKET_BOARD_PROJECT": "atlas", "TICKET_BOARD_CALLER_ROLE": "ops"},
                   cgroup="/user.slice/user-1006.slice/user@1006.service/app.slice/tmux-spawn.scope")

        residual = team_launcher.residual_project_processes(config, proc_root=proc)
        signalled: list[int] = []
        team_launcher.contain_residual_project_processes(
            config, proc_root=proc, signaller=lambda pid, _sig: signalled.append(pid),
            print_func=lambda _line: None,
        )
    pids = sorted(process.pid for process in residual)
    check(pids == [603], f"only the escaped role workload is residual: {pids}")
    check(signalled == [603], f"and only it is signalled: {signalled}")
    for pid, what in ((600, "board"), (601, "listener"), (602, "canary")):
        check(pid not in signalled, f"the managed {what} is stopped through its manager, not killed")


def test_a_service_outside_any_unit_is_still_not_a_role_workload() -> None:
    """Where the cgroup cannot help, the role marker still has to.

    A board or listener started by hand -- during a repair, or before its unit
    existed -- carries `TICKET_BOARD_PROJECT` and belongs to no unit at all, so
    the cgroup exclusion has nothing to match. It is still not an escaped role
    workload, and terminating it is still stopping a service out of order by
    killing its process. The caller role is what separates them: a pane and
    everything it starts carry one; a service does not.
    """
    with tempfile.TemporaryDirectory(prefix="syrd193-byhand.") as tmp_name:
        tmp = Path(tmp_name)
        config, config_path = _project(tmp)
        proc = tmp / "proc"
        proc.mkdir()
        _fake_proc(proc, 800, argv=["/usr/bin/python3", "ticket-board.py", "--port", "25310"],
                   environ={"TICKET_BOARD_PROJECT": "atlas", "TICKET_BOARD_PROCESS_AUTHORITY": "1"},
                   cgroup="/user.slice/user-1006.slice/session-9.scope")
        _fake_proc(proc, 801, argv=["/usr/bin/python3", "escaped.py"],
                   environ={"TICKET_BOARD_PROJECT": "atlas", "TICKET_BOARD_CALLER_ROLE": "ops"},
                   cgroup="/user.slice/user-1006.slice/session-9.scope")
        residual = team_launcher.residual_project_processes(config, proc_root=proc)
    pids = sorted(process.pid for process in residual)
    check(pids == [801], f"a hand-started service is not swept up with the panes: {pids}")


def test_the_managed_unit_names_are_the_ones_the_stop_uses() -> None:
    """The exclusion and the stop must name the same units, or it protects nothing."""
    with tempfile.TemporaryDirectory(prefix="syrd193-units.") as tmp_name:
        tmp = Path(tmp_name)
        config, _ = _project(tmp)
        managed = team_launcher.managed_unit_names(config)
    check(team_launcher._board_system_unit(config) in managed,
          f"the board unit the system-scope stop acts on: {managed}")
    check(team_launcher._listener_user_unit(config) in managed,
          f"the listener unit the owner's manager stops: {managed}")
    check(team_launcher._canary_system_unit(config) in managed,
          f"and the canary the deploy starts: {managed}")


def test_a_units_membership_is_read_from_the_kernel_not_from_a_name() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd193-cgroup.") as tmp_name:
        tmp = Path(tmp_name)
        proc = tmp / "proc"
        proc.mkdir()
        _fake_proc(proc, 700, argv=["x"], cgroup="/system.slice/atlas-ticket-board.service")
        _fake_proc(proc, 701, argv=["x"],
                   cgroup="/user.slice/user-1006.slice/user@1006.service/app.slice/"
                          "atlas-ticket-board-notify-listener.service")
        _fake_proc(proc, 702, argv=["x"], cgroup="/user.slice/user-1006.slice/session-3.scope")
        reads = {pid: team_launcher.process_systemd_unit(proc / str(pid)) for pid in (700, 701, 702)}
    check(reads[700] == "atlas-ticket-board.service", f"a system unit: {reads[700]}")
    check(reads[701] == "atlas-ticket-board-notify-listener.service", f"a user unit: {reads[701]}")
    check(reads[702] == "session-3.scope", f"and a plain session scope is not a service: {reads[702]}")


def test_residual_processes_that_cannot_be_stopped_are_reported_precisely() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd193-refuse.") as tmp_name:
        tmp = Path(tmp_name)
        config, config_path = _project(tmp)
        proc = tmp / "proc"
        proc.mkdir()
        _fake_proc(proc, 500, argv=["/usr/bin/python3", "long.py"],
                   environ={"TICKET_BOARD_PROJECT": "atlas", "TICKET_BOARD_CALLER_ROLE": "ops"})

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


def test_the_recorder_can_witness_a_boundary_it_did_not_wrap() -> None:
    """The lifecycle bridge cannot hand its run to the recorder, so it reports it.

    The bridge forks, drops to the owner and gives the child the terminal,
    because a lifecycle verb may attach a tmux client and a captured stdout is
    not a terminal (SYRD-90). Wrapping it would take that away, so the boundary
    is journalled after it closes -- by the same root-owned recorder, with the
    same operator resolution, running no command and inventing none.
    """
    recorder = ROOT / "scripts" / "switchyard-record-rollout"
    with tempfile.TemporaryDirectory(prefix="syrd193-journal.") as tmp_name:
        journal = Path(tmp_name) / "rollout"
        env = {
            "PATH": os.environ["PATH"],
            "HOME": os.environ["HOME"],
            "SWITCHYARD_ROLLOUT_JOURNAL_ROOT": str(journal),
            "SUDO_UID": str(os.getuid()),
            "SWITCHYARD_PRIVILEGED_PROVISION_ROOT": str(Path(tmp_name) / "etc"),
        }
        # As root, because the recorder refuses to write a journal the tenant
        # account could have written -- and it is right to. A user namespace
        # gives it a real euid 0 without this suite needing any privilege on the
        # host, so the kernel enforces the boundary rather than a mock.
        command = [sys.executable, str(recorder), "atlas", "--record-only", "ok",
                   "--exit-status", "0", "--action", "switchyard stop atlas",
                   "--operator", "eric", "--label", "tenant-control stop",
                   "--detail", "eric ran `switchyard stop atlas` as atlas-agent"]
        result = subprocess.run(
            ["unshare", "--user", "--map-root-user", *command],
            capture_output=True, text=True, env=env,
        )
        if result.returncode != 0 and "unshare" in result.stderr:
            result = subprocess.run(command, capture_output=True, text=True, env=env)
        entries = sorted((journal / "atlas").glob("[0-9]*")) if (journal / "atlas").is_dir() else []
        index = (journal / "atlas" / "index.jsonl")
        body = index.read_text(encoding="utf-8") if index.is_file() else ""
    if "must run as root" in (result.stdout + result.stderr):
        # This case proves the journal shape, and only root may write it. Where
        # the suite does not run as root, the refusal itself is the boundary
        # being honoured rather than something to work around.
        check("journal" in (result.stdout + result.stderr),
              f"an unprivileged record-only says where the journal lives: {result.stdout}{result.stderr}")
        return
    check(result.returncode == 0, f"the boundary is recorded: {result.stdout}{result.stderr}")
    check(len(entries) == 1, f"as exactly one attempt: {entries}")
    check("switchyard" in body and "stop" in body,
          f"the record names the action, not merely its outcome: {body[:300]}")
    check('"operator":"eric"' in body.replace(" ", ""),
          f"and who crossed the boundary, from the uid the kernel reported: {body[:400]}")
    check("tenant-control stop" in body, f"labelled as the lifecycle action: {body[:400]}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"team_launcher_tenant_suspension_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
