#!/usr/bin/env python3
"""SYRD-65: a presentation is only recovered when somebody can see it.

A failed identity transaction called the whole-project stop, which killed the
viewer and all six display slots before it killed the workers. The rollback
restarted the workers and re-pointed the slots, so `present list` reported
every slot's worker and client connected -- and no terminal was displaying any
of it. `present bootstrap` then returned zero and moved all six sessions to
`session_attached=1` while the user still saw nothing, because the clients it
made were the viewer's own panes and the viewer is not a window.

So these cover the distinction the incident turned on: a worker being reachable
is not a presentation being visible, and a client existing is not a window. The
transaction stops only the workers, a launch that cannot prove a window is a
failure rather than a recovery, and the passwordless control bridge carries the
desktop identity that made `sudo` the only route back.
"""

from __future__ import annotations

import json
import shlex
import stat
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from team_launcher_test_helpers import *
from team_launcher_upgrade_cutover_test import _RunningTenant, _declarative_tenant

from scripts import presentation_controller as presentation
from scripts.ticket_board import project_provision as provision

PROJECT = "porter"
ROLES = ("designer", "director", "audit", "ops", "app", "main")
VIEWER = f"{PROJECT}-viewer"
SLOTS = tuple(f"{PROJECT}-display-{slot}" for slot in range(len(ROLES)))
DIRECTOR_ENV = {"TICKET_BOARD_CALLER_ROLE": "director", "TICKET_BOARD_PROJECT": PROJECT}
DESKTOP_TTY = "/dev/pts/90"


class TmuxWorld:
    """A tmux server that remembers sessions, their panes and their clients.

    The three facts this suite turns on are exactly the three tmux reports
    separately: which sessions exist, which terminals each session's panes own,
    and which terminals are attached to it. Conflating the last two is the
    production bug, so nothing here derives one from the other.
    """

    def __init__(self, sessions: set[str] | None = None) -> None:
        self.sessions: set[str] = set(sessions or set())
        self.pane_ttys: dict[str, list[str]] = {}
        self.clients: dict[str, list[str]] = {}
        self.options: dict[tuple[str, str], str] = {}
        self.calls: list[list[str]] = []
        self.pane_commands: dict[str, str] = {}

    @staticmethod
    def _session(target: str) -> str:
        return target.removeprefix("=").split(":", 1)[0]

    def attach_desktop_terminal(self, session: str, tty: str = DESKTOP_TTY) -> None:
        """What a terminal tab does: become a client that is nobody's pane."""
        self.clients.setdefault(session, []).append(tty)

    def build_viewer(self) -> None:
        """The nested topology bootstrap builds: slots, plus a viewer over them.

        Each viewer pane holds a terminal, and that terminal is the client of
        the slot it observes -- so every slot is attached, by presentation.
        """
        self.sessions.update(SLOTS)
        self.sessions.add(VIEWER)
        self.pane_ttys[VIEWER] = [f"/dev/pts/1{slot}" for slot in range(len(SLOTS))]
        for slot, session in enumerate(SLOTS):
            self.pane_ttys[session] = [f"/dev/pts/2{slot}"]
            self.clients[session] = [f"/dev/pts/1{slot}"]

    def __call__(self, args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        argv = list(args)
        if argv[:2] == ["sudo", "-u"] and "tmux" in argv:
            argv = argv[argv.index("tmux") :]
        self.calls.append(argv)
        if argv[0] != "tmux":
            return subprocess.CompletedProcess(args, 0, stdout="")
        command = argv[1]
        target = argv[argv.index("-t") + 1] if "-t" in argv else ""
        session = self._session(target)
        if command == "has-session":
            return subprocess.CompletedProcess(args, 0 if session in self.sessions else 1)
        if command == "new-session":
            created = argv[argv.index("-s") + 1]
            self.sessions.add(created)
            self.pane_commands[created] = str(argv[-1])
            return subprocess.CompletedProcess(args, 0)
        if command == "kill-session":
            self.sessions.discard(session)
            self.pane_ttys.pop(session, None)
            self.clients.pop(session, None)
            return subprocess.CompletedProcess(args, 0)
        if command == "respawn-pane":
            self.pane_commands[session] = str(argv[-1])
            return subprocess.CompletedProcess(args, 0)
        if command == "list-panes":
            if session not in self.sessions:
                return subprocess.CompletedProcess(args, 1, stdout="")
            return subprocess.CompletedProcess(
                args, 0, stdout="".join(f"{tty}\n" for tty in self.pane_ttys.get(session, []))
            )
        if command == "list-clients":
            if session not in self.sessions:
                return subprocess.CompletedProcess(args, 1, stdout="")
            return subprocess.CompletedProcess(
                args, 0, stdout="".join(f"{tty}\n" for tty in self.clients.get(session, []))
            )
        if command == "set-option":
            self.options[(target, argv[-2])] = argv[-1]
            return subprocess.CompletedProcess(args, 0)
        if command == "display-message":
            template = argv[-1]
            if template == "#{pane_dead}":
                return subprocess.CompletedProcess(args, 0, stdout="0\n")
            if template == "#{pane_pid}":
                return subprocess.CompletedProcess(args, 0, stdout="4242\n")
            if template == "#{@switchyard_project}":
                project = session.split("-display-", 1)[0] if "-display-" in session else ""
                return subprocess.CompletedProcess(args, 0, stdout=f"{project}\n")
            return subprocess.CompletedProcess(args, 0, stdout="\n")
        return subprocess.CompletedProcess(args, 0, stdout="")


class DesktopWindow:
    """A window launcher whose window actually arrives and attaches."""

    def __init__(self, world: TmuxWorld, *sessions: str) -> None:
        self.world = world
        self.targets = sessions
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **_kwargs: Any) -> object:
        self.calls.append(list(args))
        for index, session in enumerate(self.targets):
            self.world.attach_desktop_terminal(session, f"/dev/pts/9{index}")

        class Process:
            pid = 4242

            def poll(self) -> int | None:
                return None

        return Process()


class WindowThatNeverAppears:
    """A terminal that starts and is never seen: the production failure."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **_kwargs: Any) -> object:
        self.calls.append(list(args))

        class Process:
            pid = 4243

            def poll(self) -> int | None:
                return None

        return Process()


def _window_proc_root(tmp: Path, *, alive: bool = True, uid: int | None = None) -> Path:
    """A /proc in which a Konsole showing this project's layout is (or is not) running."""
    proc_root = tmp / ("proc-alive" if alive else "proc-dead")
    proc_root.mkdir(exist_ok=True)
    if not alive:
        return proc_root
    entry = proc_root / "4242"
    entry.mkdir(exist_ok=True)
    argv = ["konsole", "--separate", "--layout", f"/x/{PROJECT}-presentation-layout.json"]
    (entry / "cmdline").write_bytes(("\0".join(argv) + "\0").encode("utf-8"))
    resolved = os.getuid() if uid is None else uid
    (entry / "status").write_text(
        f"Name:\tkonsole\nUid:\t{resolved}\t{resolved}\t{resolved}\t{resolved}\n", encoding="utf-8"
    )
    return proc_root


def _presentation_config(tmp: Path) -> tuple[Any, Path]:
    config_path = _write_six_visible_role_config(tmp, project=PROJECT)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["presentation"] = {
        "slot_count": len(ROLES),
        "layouts": {"default": {str(index): role for index, role in enumerate(ROLES)}},
    }
    config_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return team_launcher.load_project_config(PROJECT, config_path), config_path


def _no_window() -> dict[str, Any]:
    """Bootstrap arguments that do not sit through the real attach wait."""
    return {"window_timeout": 0.0, "window_poll": 0.0, "sleep": lambda _seconds: None}


def test_display_slot_clients_are_not_a_window_but_a_desktop_terminal_is() -> None:
    """Worker connectivity and an external attachment are different facts."""
    with tempfile.TemporaryDirectory(prefix="syrd65-attachment.") as tmp:
        config, config_path = _presentation_config(Path(tmp))
        world = TmuxWorld({f"{PROJECT}-{role}" for role in ROLES})
        world.build_viewer()

        # Exactly the state the incident left: every slot has a client.
        assert all(world.clients[session] for session in SLOTS)
        assert presentation.external_presentation_clients(config, len(SLOTS), runner=world) == set()
        assert not presentation.presentation_window_attached(config, config_path=config_path, runner=world)

        report = presentation.presentation_report(config, config_path=config_path, runner=world)
        assert report["window_attached"] is False
        assert report["external_clients"] == []
        printed: list[str] = []
        presentation.print_presentation_report(report, json_output=False, print_func=printed.append)
        assert any("NOT displayed by any terminal" in line for line in printed), printed

        # One terminal tab, and the same topology is a presentation.
        world.attach_desktop_terminal(VIEWER)
        assert presentation.external_presentation_clients(config, len(SLOTS), runner=world) == {DESKTOP_TTY}
        assert presentation.presentation_window_attached(config, config_path=config_path, runner=world)
        report = presentation.presentation_report(config, config_path=config_path, runner=world)
        assert report["window_attached"] is True
        assert report["external_clients"] == [DESKTOP_TTY]


def test_a_separate_layout_window_is_an_attachment_without_any_viewer() -> None:
    """The other supported layout: tabs attached straight to the slots."""
    with tempfile.TemporaryDirectory(prefix="syrd65-separate-attachment.") as tmp:
        config, config_path = _presentation_config(Path(tmp))
        world = TmuxWorld({f"{PROJECT}-{role}" for role in ROLES} | set(SLOTS))
        for slot, session in enumerate(SLOTS):
            world.pane_ttys[session] = [f"/dev/pts/2{slot}"]
        assert not presentation.presentation_window_attached(config, config_path=config_path, runner=world)
        for slot, session in enumerate(SLOTS):
            world.attach_desktop_terminal(session, f"/dev/pts/3{slot}")
        assert presentation.presentation_window_attached(config, config_path=config_path, runner=world)


def test_a_bootstrap_that_opens_no_window_fails_and_leaves_nothing_pretending() -> None:
    """Six headless clients are not a recovery, and must not be left behind."""
    with tempfile.TemporaryDirectory(prefix="syrd65-false-success.") as tmp:
        config, config_path = _presentation_config(Path(tmp))
        world = TmuxWorld({f"{PROJECT}-{role}" for role in ROLES})
        launcher = WindowThatNeverAppears()
        try:
            presentation.presentation_action(
                config, config_path=config_path, action="bootstrap", layout="viewer",
                environ=DIRECTOR_ENV, runner=world, process_launcher=launcher, **_no_window(),
            )
        except SystemExit as exc:
            message = str(exc)
        else:
            raise AssertionError("a bootstrap with no window reported success")
        assert "not on any screen" in message, message
        assert f"switchyard {PROJECT}" in message, message
        # The slots are durable state and stay; the viewer was only ever the
        # thing claiming to display them, so it does not.
        assert set(SLOTS) <= world.sessions
        assert VIEWER not in world.sessions
        assert not presentation.presentation_window_attached(config, config_path=config_path, runner=world)


def test_bootstrap_defaults_to_the_native_window_not_the_nested_viewer() -> None:
    """The viewer builds sessions and opens nothing, so it is not the default."""
    parser = team_launcher._build_switchyard_present_parser()
    assert parser.parse_args([PROJECT, "bootstrap"]).layout == team_launcher.LAYOUT_MODE_SEPARATE


def test_bootstrap_into_the_viewer_is_refused_because_it_opens_no_window() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd65-viewer-refusal.") as tmp:
        config, config_path = _presentation_config(Path(tmp))
        world = TmuxWorld({f"{PROJECT}-{role}" for role in ROLES})
        try:
            presentation.presentation_action(
                config, config_path=config_path, action="bootstrap", layout="viewer",
                environ=DIRECTOR_ENV, runner=world, **_no_window(),
            )
        except SystemExit as exc:
            message = str(exc)
        else:
            raise AssertionError("a viewer bootstrap reported a window it did not open")
        assert "opens no window of its own" in message, message
        assert f"switchyard {PROJECT}" in message, message


def test_the_native_bootstrap_lays_one_window_out_two_rows_by_three() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd65-native-window.") as tmp:
        root = Path(tmp)
        config, config_path = _presentation_config(root)
        world = TmuxWorld({f"{PROJECT}-{role}" for role in ROLES})
        window = DesktopWindow(world, *SLOTS)
        presentation.presentation_action(
            config, config_path=config_path, action="bootstrap", layout="separate",
            environ=DIRECTOR_ENV, runner=world, process_launcher=window,
            proc_root=_window_proc_root(root),
        )
        # One terminal, not one per slot.
        assert len(window.calls) == 1
        assert presentation.presentation_window_attached(config, config_path=config_path, runner=world)
        layout_path = team_launcher.desktop_presentation_layout_path(
            config, config_path=config_path, gui_user=team_launcher.current_user_name()
        )
        payload = json.loads(layout_path.read_text(encoding="utf-8"))
        # Two rows of three, laid out by the terminal itself.
        assert payload["Orientation"] == "Vertical"
        assert [len(row["Widgets"]) for row in payload["Widgets"]] == [3, 3]
        leaves = team_launcher._layout_leaves(payload)
        assert [shlex.split(leaf["Command"])[-1] for leaf in leaves] == [f"={session}" for session in SLOTS]
        # Every tab ends inert rather than at the shell that opened the window.
        for leaf in leaves:
            assert Path(shlex.split(leaf["Command"])[0]).name == team_launcher.PANE_WINDOW_NAME


def test_each_tab_starts_in_the_desktop_home_not_the_launching_one() -> None:
    """Under sudo the launching home is /root, which the desktop cannot enter."""
    with tempfile.TemporaryDirectory(prefix="syrd65-workdir.") as tmp:
        root = Path(tmp)
        config, config_path = _presentation_config(root)
        desktop = team_launcher.current_user_name()
        original = os.environ.get("HOME")
        os.environ["HOME"] = "/root"
        try:
            recorded: list[dict[str, Any]] = []
            original_launch = team_launcher.launch_konsole_window
            try:
                team_launcher.launch_konsole_window = lambda output, **kwargs: (
                    recorded.append({"output": output, **kwargs}) or 0
                )
                presentation._launch_separate(
                    config,
                    {"slot_count": len(ROLES)},
                    config_path=config_path,
                    output_path=root / "presentation-layout.json",
                    runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0),
                    process_launcher=None,
                )
            finally:
                team_launcher.launch_konsole_window = original_launch
        finally:
            if original is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = original
        leaves = team_launcher._layout_leaves(
            json.loads((root / "presentation-layout.json").read_text(encoding="utf-8"))
        )
        assert len(leaves) == len(ROLES)
        for leaf in leaves:
            assert leaf["WorkingDirectory"] == team_launcher._gui_home(desktop), leaf
            assert leaf["WorkingDirectory"] != "/root", leaf


def test_a_terminal_that_aborted_is_not_a_window_however_many_clients_remain() -> None:
    """The observed failure: Konsole exits -6 and the clients outlive it briefly."""
    with tempfile.TemporaryDirectory(prefix="syrd65-aborted-window.") as tmp:
        root = Path(tmp)
        config, config_path = _presentation_config(root)
        world = TmuxWorld({f"{PROJECT}-{role}" for role in ROLES})
        window = DesktopWindow(world, *SLOTS)
        try:
            presentation.presentation_action(
                config, config_path=config_path, action="bootstrap", layout="separate",
                environ=DIRECTOR_ENV, runner=world, process_launcher=window,
                proc_root=_window_proc_root(root, alive=False), **_no_window(),
            )
        except SystemExit as exc:
            message = str(exc)
        else:
            raise AssertionError("a launch whose terminal died reported success")
        assert "not on any screen" in message, message


def test_the_worker_only_stop_leaves_every_slot_and_the_window_standing() -> None:
    """The stop the identity transaction uses, against the one it used to."""
    with tempfile.TemporaryDirectory(prefix="syrd65-worker-stop.") as tmp:
        config, config_path = _presentation_config(Path(tmp))
        world = TmuxWorld({f"{PROJECT}-{role}" for role in ROLES})
        world.build_viewer()
        world.attach_desktop_terminal(VIEWER)

        assert team_launcher.stop_role_sessions(config, runner=world, print_func=lambda _t: None) == 0
        during_the_stop = list(world.calls)
        assert not {f"{PROJECT}-{role}" for role in ROLES} & world.sessions
        assert set(SLOTS) | {VIEWER} <= world.sessions
        assert presentation.presentation_window_attached(config, config_path=config_path, runner=world)
        # Not merely surviving: never addressed. A command aimed at a display
        # session is a command that could take the window down.
        presentation_targets = set(SLOTS) | {VIEWER}
        assert not [
            call for call in during_the_stop
            if any(part.removeprefix("=").split(":", 1)[0] in presentation_targets for part in call)
        ], during_the_stop

        # The whole-project stop is what the transaction used to call, and this
        # is what it did to the window (SYRD-65).
        world.sessions.update(f"{PROJECT}-{role}" for role in ROLES)
        assert team_launcher.stop_project(config, runner=world, print_func=lambda _t: None) == 0
        assert not (set(SLOTS) | {VIEWER}) & world.sessions


class _PresentationTenant:
    """A running tenant whose display slots and window are real to the probes."""

    def __init__(self, config_path: Path, *, account_uid: int, visible: bool = True) -> None:
        self.tenant = _RunningTenant(config_path, account_uid=account_uid)
        self.config_path = config_path
        self.world = TmuxWorld()
        self.world.build_viewer()
        if visible:
            self.world.attach_desktop_terminal(VIEWER)
        self.windows_opened: list[str] = []

    def __enter__(self) -> "_PresentationTenant":
        self.tenant.__enter__()
        self._konsole = team_launcher.launch_konsole_window
        team_launcher.launch_konsole_window = (
            lambda output, **kwargs: (self.windows_opened.append(str(output)) or 0)
        )
        # This tenant's owner is a real account, so the state path derives from
        # a real home. A test must not write there: the presentation state it
        # would leave behind outlives the run and is read by the next one.
        state = self.config_path.parent / "owner-state"
        self._layout_path = team_launcher.default_layout_output_path
        team_launcher.default_layout_output_path = (
            lambda config, *, config_path: state / f"{config.project}-team-layout.json"
        )
        return self

    def __exit__(self, *exc: object) -> bool:
        team_launcher.default_layout_output_path = self._layout_path
        team_launcher.launch_konsole_window = self._konsole
        self.tenant.__exit__(*exc)
        return False

    def runner(self):
        """The tenant's runner, with tmux questions about presentation answered."""
        inner = self.tenant.runner()
        world = self.world

        def call(args, **kwargs):
            argv = list(args)
            bare = argv[argv.index("tmux"):] if "tmux" in argv else argv
            if bare[:1] == ["tmux"] and len(bare) > 1:
                target = bare[bare.index("-t") + 1] if "-t" in bare else ""
                session = target.removeprefix("=").split(":", 1)[0]
                if session.startswith(f"{PROJECT}-display-") or session == VIEWER:
                    return world(argv, **kwargs)
                if bare[1] in {"list-clients", "list-panes"}:
                    return world(argv, **kwargs)
            return inner(argv, **kwargs)

        return call


def test_a_failed_identity_transaction_leaves_exactly_one_prior_window() -> None:
    """The rollback the incident produced, with the window kept this time."""
    with tempfile.TemporaryDirectory(prefix="syrd65-rollback-window.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp))
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        payload["presentation"] = {
            "slot_count": len(ROLES),
            "layouts": {"default": {str(index): role for index, role in enumerate(ROLES)}},
        }
        config_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        before = config_path.read_bytes()
        printed: list[str] = []
        stopped_whole_project: list[str] = []
        original_stop_project = team_launcher.stop_project
        try:
            team_launcher.stop_project = lambda config, **kwargs: (
                stopped_whole_project.append(config.project) or 0
            )
            with _PresentationTenant(config_path, account_uid=os.getuid() + 4242) as tenant:
                result = team_launcher.cutover_role_identities_command(
                    team_launcher.load_project_config(PROJECT, config_path),
                    config_path=config_path,
                    tooling_dir=config_path.parent / "tooling" / PROJECT,
                    runner=tenant.runner(),
                    print_func=printed.append,
                )
                world = tenant.world
                windows = list(tenant.windows_opened)
        finally:
            team_launcher.stop_project = original_stop_project
        output = "\n".join(printed)
        assert result == 1, output
        assert config_path.read_bytes() == before, "the configuration was not put back"
        # The presentation was never the transaction's to stop.
        assert stopped_whole_project == [], stopped_whole_project
        # Exactly one window's worth of slots, all of them, and one viewer.
        assert {session for session in world.sessions if "-display-" in session} == set(SLOTS)
        assert VIEWER in world.sessions
        # No second six-pane window: the rollback re-points, it does not relaunch.
        assert windows == [], windows
        # The window the tenant had is the window it still has.
        assert world.clients[VIEWER] == [DESKTOP_TTY]
        assert "the presentation window" not in output, output
        # No slot adds a status row of its own to the worker's, and the frame
        # around them does not either.
        assert {
            world.options[(f"={session}:", "status")] for session in (*SLOTS, VIEWER)
            if (f"={session}:", "status") in world.options
        } == {"off"}
        # Every slot ends at the inert proxy, never at a shell a paste would run in.
        for session in SLOTS:
            argv = shlex.split(world.pane_commands[session])
            assert argv[:2] == ["sh", "-lc"], argv
            assert "sudo" not in world.pane_commands[session], session


def test_a_rollback_the_user_cannot_see_is_reported_as_one() -> None:
    """Re-pointed is not visible, and only the transaction can say which."""
    with tempfile.TemporaryDirectory(prefix="syrd65-blind-rollback.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp))
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        payload["presentation"] = {
            "slot_count": len(ROLES),
            "layouts": {"default": {str(index): role for index, role in enumerate(ROLES)}},
        }
        config_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        printed: list[str] = []
        with _PresentationTenant(config_path, account_uid=os.getuid() + 4242) as tenant:
            world = tenant.world
            original_stop = team_launcher.stop_role_sessions

            def _stop_that_loses_the_window(config, **kwargs):
                # Somebody closed the window while the transaction was running.
                world.clients.pop(VIEWER, None)
                return original_stop(config, **kwargs)

            team_launcher.stop_role_sessions = _stop_that_loses_the_window
            try:
                result = team_launcher.cutover_role_identities_command(
                    team_launcher.load_project_config(PROJECT, config_path),
                    config_path=config_path,
                    tooling_dir=config_path.parent / "tooling" / PROJECT,
                    runner=tenant.runner(),
                    print_func=printed.append,
                )
            finally:
                team_launcher.stop_role_sessions = original_stop
        output = "\n".join(printed)
        assert result == 1, output
        assert "no terminal is displaying them" in output, output
        assert f"switchyard {PROJECT}" in output, output
        config = team_launcher.load_project_config(PROJECT, config_path)
        journal = team_launcher.read_upgrade_journal(config, config_path=config_path)
        assert team_launcher.upgrade_phase_state(journal, "identities") == "rolled back", journal


def _bridged_environment(caller: str, owner: str) -> dict[str, str]:
    """Exactly what the control bridge builds before it drops to the owner."""
    return {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": f"/home/{owner}",
        "USER": owner,
        "LOGNAME": owner,
        "SHELL": "/bin/sh",
        "LANG": "C.UTF-8",
        team_launcher.TENANT_CONTROL_CALLER_ENV: caller,
    }


def test_the_control_bridge_carries_the_desktop_identity_the_owner_lacks() -> None:
    """The bridge already resolves the human; the launcher now uses them."""
    # Deliberately neither this process's account nor the owner's: a fallback to
    # either would answer this test correctly by accident.
    caller = "desktop-human"
    owner = f"{PROJECT}-owner"
    environment = _bridged_environment(caller, owner)
    original = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(environment)
        assert team_launcher.default_gui_user() == caller
        # And never root, whatever the environment says: a window opened as
        # root is a root shell behind every tab.
        os.environ["SUDO_USER"] = "root"
        try:
            with tempfile.TemporaryDirectory(prefix="syrd65-no-root-window.") as inner:
                config, _ = _presentation_config(Path(inner))
                assert team_launcher.presentation_gui_user(config) == caller
        finally:
            os.environ.pop("SUDO_USER", None)
        with tempfile.TemporaryDirectory(prefix="syrd65-bridge-gui.") as tmp:
            config, _ = _presentation_config(Path(tmp))
            owned = replace(config, run_as_user=owner)
            assert team_launcher.presentation_gui_user(owned) == caller
        # A desktop is looked for under the human, not under the account that
        # is executing: the owner has no graphical session, and answering "no
        # desktop" chose the one layout that opens no window.
        asked: list[list[str]] = []

        def loginctl(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            asked.append(args)
            if args[:2] == ["loginctl", "show-user"]:
                return subprocess.CompletedProcess(args, 0, stdout="7\n")
            return subprocess.CompletedProcess(args, 0, stdout="Desktop=KDE\n")

        assert team_launcher.detected_invoking_desktop(environ=environment, runner=loginctl) == "KDE"
        assert asked[0][2] == caller, asked
        assert team_launcher.resolve_layout_mode(
            team_launcher.LAYOUT_MODE_AUTO, environ=environment, runner=loginctl
        ) == team_launcher.LAYOUT_MODE_SEPARATE
    finally:
        os.environ.clear()
        os.environ.update(original)


def test_a_bridged_window_is_opened_on_the_callers_display_not_the_owners() -> None:
    """The owner account owns the sessions; it does not own a screen."""
    caller = team_launcher.current_user_name()
    owner = f"{PROJECT}-owner"
    assert team_launcher.uid_for_user(owner) is None, "the fixture owner must not be a real account"
    original = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(_bridged_environment(caller, owner))
        with tempfile.TemporaryDirectory(prefix="syrd65-bridge-window.") as tmp:
            config, config_path = _presentation_config(Path(tmp))
            owned = replace(config, run_as_user=owner)
            recorded: list[dict[str, Any]] = []
            original_launch = team_launcher.launch_konsole_window
            try:
                team_launcher.launch_konsole_window = lambda output, **kwargs: (
                    recorded.append(kwargs) or 0
                )
                presentation._launch_separate(
                    owned,
                    {"slot_count": len(ROLES)},
                    config_path=config_path,
                    output_path=Path(tmp) / "presentation-layout.json",
                    runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0),
                    process_launcher=None,
                )
            finally:
                team_launcher.launch_konsole_window = original_launch
            assert [entry["gui_user"] for entry in recorded] == [caller], recorded

            # And the layout it hands that terminal is one the terminal can
            # read: chosen from the desktop account, not from the tenant whose
            # state directory the desktop user may not even enter.
            desktop_home = Path(tmp) / "desktop-home"
            original_home = team_launcher._gui_home
            recorded.clear()
            try:
                team_launcher._gui_home = lambda user: (
                    str(desktop_home) if user == caller else original_home(user)
                )
                team_launcher.launch_konsole_window = lambda output, **kwargs: (
                    recorded.append({"output": Path(output), **kwargs}) or 0
                )
                presentation._launch_separate(
                    owned,
                    {"slot_count": len(ROLES)},
                    config_path=config_path,
                    runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0),
                    process_launcher=None,
                )
            finally:
                team_launcher._gui_home = original_home
                team_launcher.launch_konsole_window = original_launch
            written = recorded[0]["output"]
            assert desktop_home in written.parents, written
            tenant_state = team_launcher.default_layout_output_path(
                owned, config_path=config_path
            ).parent
            assert tenant_state not in written.parents, written
            assert written.exists() and stat.S_IMODE(written.stat().st_mode) == 0o600
            # And the window really is aimed at that person's compositor: the
            # owner has no runtime directory, so naming it produced a refusal
            # rather than a window.
            args = team_launcher.konsole_launch_args(
                Path(tmp) / "presentation-layout.json", gui_user=caller
            )
            assert args[:2] != ["sh", "-lc"], args
            assert any(f"/run/user/{os.getuid()}/" in part for part in args), args
            refused = team_launcher.konsole_launch_args(
                Path(tmp) / "presentation-layout.json", gui_user=owner
            )
            assert refused[:2] == ["sh", "-lc"], refused
    finally:
        os.environ.clear()
        os.environ.update(original)


def test_the_native_layout_is_written_where_its_terminal_can_read_it() -> None:
    """Konsole is dropped to the desktop user and reads this file as them."""
    with tempfile.TemporaryDirectory(prefix="syrd65-layout-readable.") as tmp:
        root = Path(tmp)
        config, config_path = _presentation_config(root)
        owner = f"{PROJECT}-owner"
        owned = replace(config, run_as_user=owner)
        desktop = team_launcher.current_user_name()
        # Owner and desktop are the same account: no boundary, tenant location.
        assert team_launcher.desktop_presentation_layout_path(
            replace(config, run_as_user=desktop), config_path=config_path, gui_user=desktop
        ).parent == team_launcher.default_layout_output_path(
            replace(config, run_as_user=desktop), config_path=config_path
        ).parent
        # A desktop user who is not the owner gets it under their own state
        # directory instead of below the tenant's 0700 one, which they cannot
        # even enter -- a terminal handed a path it cannot read aborts.
        crossing = team_launcher.desktop_presentation_layout_path(
            owned, config_path=config_path, gui_user=desktop
        )
        assert str(crossing).startswith(team_launcher._gui_home(desktop) + "/"), crossing
        tenant_state = team_launcher.default_layout_output_path(owned, config_path=config_path).parent
        assert not str(crossing).startswith(str(tenant_state)), crossing

        # And it is written protected: 0600 in a 0700 directory of their own.
        target = root / "desktop-state" / f"{PROJECT}-presentation-layout.json"
        assert team_launcher.write_desktop_layout(
            target, {"Orientation": "Vertical"}, gui_user=desktop,
            runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0),
        ) == ""
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert json.loads(target.read_text(encoding="utf-8")) == {"Orientation": "Vertical"}

        # An unprivileged process cannot hand another account a readable file,
        # and says so instead of opening a terminal that will abort.
        refusal = team_launcher.write_desktop_layout(
            root / "other" / "layout.json", {"Orientation": "Vertical"}, gui_user="root",
            runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0),
        )
        assert "only root can write into another account's state directory" in refusal, refusal


def test_each_native_tab_reaches_its_slot_through_the_narrow_display_bridge() -> None:
    """Not six password prompts, and not a blanket grant on the owner."""
    with tempfile.TemporaryDirectory(prefix="syrd65-display-bridge.") as tmp:
        config, _ = _presentation_config(Path(tmp))
        desktop = team_launcher.current_user_name()
        owned = replace(config, run_as_user=f"{PROJECT}-owner")

        # Within one account there is no boundary to cross.
        assert presentation.display_attach_args(config, 0, gui_user=desktop) == [
            "env", "TMUX=", "tmux", "attach", "-t", f"={PROJECT}-display-0"
        ]
        # Across one, the tab runs the per-project bridge and passes it a slot
        # number: no session name, no tmux argument, no command to run.
        for slot in range(len(ROLES)):
            argv = presentation.display_attach_args(owned, slot, gui_user=desktop)
            assert argv[:2] == ["sudo", "-n"], argv
            assert argv[2] == f"/usr/local/lib/switchyard/{PROJECT}/switchyard-display-attach", argv
            assert argv[3:] == [PROJECT, str(slot)], argv
            assert not any(part.startswith("-u") for part in argv), argv

        # And the grant that makes it passwordless names that one program.
        document = provision.tenant_control_sudoers_document(PROJECT, desktop)
        grants = [line for line in document.splitlines() if line and not line.startswith("#")]
        assert f"{desktop} ALL=(root) NOPASSWD: /usr/local/lib/switchyard/{PROJECT}/switchyard-display-attach" in grants
        # Nothing here widens into a general grant on the owner account.
        assert not any("/usr/bin/tmux" in line or "ALL) " in line for line in grants), grants


def test_the_display_bridge_refuses_everything_but_one_slot_of_one_project() -> None:
    bridge = ROOT / "scripts" / "switchyard-display-attach"
    assert os.access(bridge, os.X_OK)
    for argv, expected in (
        ([PROJECT], "usage"),
        ([PROJECT, "0", "extra"], "usage"),
        (["../other", "0"], "plain slug"),
        ([PROJECT, "0; sh"], "slot must be a number"),
        ([PROJECT, "9"], "slot must be within"),
    ):
        result = subprocess.run(
            [sys.executable, str(bridge), *argv], capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin"},
        )
        assert result.returncode != 0, argv
        assert expected in result.stderr, (argv, result.stderr)
    # A well-formed call from a caller sudo did not resolve is still refused,
    # before any grant file is read.
    # A well-formed call is still refused when this host has no grant for the
    # project: the authorized user is root-owned data, never the caller's word.
    result = subprocess.run(
        [sys.executable, str(bridge), PROJECT, "0"], capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "SUDO_UID": "0"},
    )
    assert result.returncode != 0
    assert "no control grant is installed" in result.stderr, result.stderr


def test_a_cutover_that_succeeds_but_loses_the_window_is_not_a_success() -> None:
    """The forward pass owes the same answer the rollback does."""
    with tempfile.TemporaryDirectory(prefix="syrd65-forward-window.") as tmp:
        config_path, _ = _declarative_tenant(Path(tmp), accounts=True)
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        payload["presentation"] = {
            "slot_count": len(ROLES),
            "layouts": {"default": {str(index): role for index, role in enumerate(ROLES)}},
        }
        config_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        printed: list[str] = []
        with _PresentationTenant(config_path, account_uid=os.getuid()) as tenant:
            world = tenant.world
            original_reconnect = team_launcher.reconnect_presentation

            def _reconnect_into_an_empty_room(config, **kwargs):
                # Everything re-points; the window closed while it happened.
                world.clients.pop(VIEWER, None)
                return original_reconnect(config, **kwargs)

            team_launcher.reconnect_presentation = _reconnect_into_an_empty_room
            try:
                result = team_launcher.cutover_role_identities_command(
                    team_launcher.load_project_config(PROJECT, config_path),
                    config_path=config_path,
                    tooling_dir=config_path.parent / "tooling" / PROJECT,
                    runner=tenant.runner(),
                    print_func=printed.append,
                )
            finally:
                team_launcher.reconnect_presentation = original_reconnect
        output = "\n".join(printed)
        assert result == 1, output
        assert "the presentation window that was visible before the cutover is gone" in output, output


if __name__ == "__main__":
    run_module_tests(globals())
    print("presentation_window_recovery_test: ok")
