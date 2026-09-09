#!/usr/bin/env python3
"""SYRD-90: the layout a desktop terminal is handed must be one it can open.

From Eric's desktop, `sudo switchyard syrd` wrote
`/home/switchyard-agent/.local/state/switchyard/projects/syrd/syrd-presentation-layout.json`,
chowned it to Eric, and left it behind the tenant's 0700 project-state
directory. Konsole, correctly running as Eric, could not traverse to it: "A
problem occurred when loading the Layout", and a blank window.

`_launch_separate` already asks `desktop_presentation_layout_path()` where the
layout belongs (SYRD-65). The ordinary launch overrode that answer with a path
derived from the tenant's own presentation state, so the one path that runs on
every start was the one that got it wrong.

The same launch then reported a root presentation window that does not exist:
the scan counted the `sudo -u eric ... konsole ...` wrapper, whose argv names
konsole, rather than the terminal it started, which runs as Eric.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from team_launcher_test_helpers import *
from scripts import presentation_controller as presentation

PROJECT = "porter"
ROLES = ("designer", "director", "audit", "ops", "app", "main")
DESKTOP_USER = "eric"


def _sandboxed_state(tmp: Path) -> Path:
    """This tenant's presentation state, inside the sandbox.

    Never the real one. `presentation_state_path()` resolves to the running
    account's own state directory, and a suite that writes there leaves its
    fixtures in a live tenant's home.
    """
    state = tmp / "tenant-state"
    state.mkdir(exist_ok=True)
    return state / "presentation.json"


def _tenant(tmp: Path):
    """A tenant whose owner is this account and whose screen belongs elsewhere."""
    config_path = _write_six_visible_role_config(tmp, project=PROJECT)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["presentation"] = {
        "slot_count": len(ROLES),
        "layouts": {"default": {str(index): role for index, role in enumerate(ROLES)}},
    }
    payload["run_as_user"] = team_launcher.current_user_name()
    config_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return team_launcher.load_project_config(PROJECT, config_path), config_path


class _Desktop:
    """A desktop account that is not the tenant owner, with a home of its own."""

    def __init__(self, tmp: Path, user: str = DESKTOP_USER):
        self.user = user
        self.home = tmp / "desktop-home"
        self.home.mkdir(exist_ok=True)

    def __enter__(self):
        self._gui_home = team_launcher._gui_home
        self._gui_user = team_launcher.presentation_gui_user
        self._uid_for_user = team_launcher.uid_for_user
        team_launcher._gui_home = lambda user: (
            str(self.home) if user == self.user else self._gui_home(user)
        )
        team_launcher.presentation_gui_user = lambda _config: self.user
        # Not this process, so the write really has to cross an account
        # boundary, which is the condition the layout path exists for.
        team_launcher.uid_for_user = lambda name: (
            os.getuid() + 1 if name == self.user else self._uid_for_user(name)
        )
        return self

    def __exit__(self, *_exc):
        team_launcher._gui_home = self._gui_home
        team_launcher.presentation_gui_user = self._gui_user
        team_launcher.uid_for_user = self._uid_for_user
        return False


def _stub_write(path: Path, payload: dict, **_kwargs) -> str:
    """Stand in for the handover itself, which needs root to cross accounts.

    These cases are about which path the launch chooses, so the file is written
    exactly where it decided it goes and nothing is chowned.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return ""


def _launched_layout(config, config_path: Path, *, state_path: Path) -> Path:
    """Run the ordinary launch and report the layout it handed the terminal."""
    recorded: list[Path] = []
    original_launch = team_launcher.launch_konsole_window
    original_write = team_launcher.write_desktop_layout
    try:
        team_launcher.launch_konsole_window = lambda output, **_kwargs: (
            recorded.append(Path(output)) or 0
        )
        # Crossing to another account needs root; what this case is about is
        # which path is chosen, so the handover itself is stood down and the
        # file is written where the launch decided it goes.
        team_launcher.write_desktop_layout = _stub_write
        presentation.launch_presentation(
            config,
            config_path=config_path,
            state_path=state_path,
            layout="separate",
            runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", ""),
            process_launcher=None,
        )
    finally:
        team_launcher.launch_konsole_window = original_launch
        team_launcher.write_desktop_layout = original_write
    assert len(recorded) == 1, recorded
    return recorded[0]


def test_the_ordinary_launch_stages_the_layout_where_the_terminal_can_read_it() -> None:
    """The launch that runs on every start, not only the bootstrap one."""
    with tempfile.TemporaryDirectory(prefix="desktop-layout.") as raw:
        tmp = Path(raw)
        config, config_path = _tenant(tmp)
        state_path = _sandboxed_state(tmp)
        with _Desktop(tmp) as desktop:
            written = _launched_layout(config, config_path, state_path=state_path)
            expected = team_launcher.desktop_presentation_layout_path(
                config, config_path=config_path, gui_user=desktop.user
            )
        assert written == expected, written
        assert desktop.home in written.parents, written
        # And not beside the tenant's presentation state, which is where the
        # ordinary launch used to put it.
        assert written.parent != state_path.parent, written
        assert not state_path.with_name(f"{PROJECT}-presentation-layout.json").exists()


def test_both_launch_paths_choose_the_same_place() -> None:
    """Bootstrap answered this correctly; the ordinary launch did not."""
    with tempfile.TemporaryDirectory(prefix="desktop-layout-parity.") as raw:
        tmp = Path(raw)
        config, config_path = _tenant(tmp)
        state_path = _sandboxed_state(tmp)
        recorded: list[Path] = []
        original_launch = team_launcher.launch_konsole_window
        original_write = team_launcher.write_desktop_layout
        with _Desktop(tmp):
            try:
                team_launcher.launch_konsole_window = lambda output, **_kwargs: (
                    recorded.append(Path(output)) or 0
                )
                team_launcher.write_desktop_layout = _stub_write
                presentation._launch_separate(
                    config,
                    {"slot_count": len(ROLES)},
                    config_path=config_path,
                    runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0),
                    process_launcher=None,
                )
                bootstrap = recorded[-1]
                presentation.launch_presentation(
                    config,
                    config_path=config_path,
                    state_path=state_path,
                    layout="separate",
                    runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", ""),
                    process_launcher=None,
                )
                ordinary = recorded[-1]
            finally:
                team_launcher.launch_konsole_window = original_launch
                team_launcher.write_desktop_layout = original_write
        assert bootstrap == ordinary, (bootstrap, ordinary)


# --------------------------------------------------------------------------
# The bridge handoff: the owner half reports, the caller opens its own window.
# --------------------------------------------------------------------------


def _pinned_pane_program() -> Path:
    """A program that really is root's, all the way up: this repo runs as one."""
    return Path("/usr/bin/env")


def test_the_owner_half_hands_the_desktop_half_back_instead_of_refusing() -> None:
    """Under the bridge this process is the owner: no state, no compositor."""
    with tempfile.TemporaryDirectory(prefix="handoff-emit.") as raw:
        tmp = Path(raw)
        config, config_path = _tenant(tmp)
        read_fd, write_fd = os.pipe()
        opened: list[Path] = []
        original_launch = team_launcher.launch_konsole_window
        original_env = os.environ.get(team_launcher.PRESENTATION_HANDOFF_FD_ENV)
        with _Desktop(tmp):
            try:
                os.environ[team_launcher.PRESENTATION_HANDOFF_FD_ENV] = str(write_fd)
                team_launcher.launch_konsole_window = lambda output, **_kwargs: (
                    opened.append(Path(output)) or 0
                )
                presentation._launch_separate(
                    config,
                    {"slot_count": len(ROLES)},
                    config_path=config_path,
                    runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0),
                    process_launcher=None,
                )
            finally:
                team_launcher.launch_konsole_window = original_launch
                if original_env is None:
                    os.environ.pop(team_launcher.PRESENTATION_HANDOFF_FD_ENV, None)
                else:
                    os.environ[team_launcher.PRESENTATION_HANDOFF_FD_ENV] = original_env
        with os.fdopen(read_fd, "r", encoding="utf-8") as handle:
            payload = json.loads(handle.read())
        # It reported facts, not a document, and it opened nothing.
        assert sorted(payload) == ["pane_program", "project", "schema", "slot_count"], payload
        assert payload["project"] == PROJECT and payload["slot_count"] == len(ROLES)
        assert opened == [], opened


def _handoff(tmp: Path, caller: str, **overrides) -> Path:
    payload = {
        "schema": team_launcher.PRESENTATION_HANDOFF_SCHEMA,
        "project": PROJECT,
        "slot_count": len(ROLES),
        "pane_program": str(_pinned_pane_program()),
    }
    payload.update(overrides)
    path = team_launcher.presentation_handoff_path(PROJECT, caller)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class _Caller:
    """This account, standing in for the human whose desktop it is."""

    def __init__(self, tmp: Path):
        self.user = team_launcher.current_user_name()
        self.home = tmp / "caller-home"
        self.home.mkdir(exist_ok=True)

    def __enter__(self):
        self._gui_home = team_launcher._gui_home
        self._grant = team_launcher._tenant_control_grant
        team_launcher._gui_home = lambda user: (
            str(self.home) if user == self.user else self._gui_home(user)
        )
        team_launcher._tenant_control_grant = lambda project, **_kwargs: {
            "project": project, "owner": "porter-agent", "authorized_user": self.user,
        }
        return self

    def __exit__(self, *_exc):
        team_launcher._gui_home = self._gui_home
        team_launcher._tenant_control_grant = self._grant
        return False


def _complete(caller: _Caller, opened: list[dict]) -> int:
    original_launch = team_launcher.launch_konsole_window
    try:
        team_launcher.launch_konsole_window = lambda output, **kwargs: (
            opened.append({"output": Path(output), **kwargs}) or 0
        )
        return team_launcher.complete_desktop_presentation(
            PROJECT, caller=caller.user, print_func=lambda _line: None
        )
    finally:
        team_launcher.launch_konsole_window = original_launch


def test_the_caller_stages_the_layout_in_its_own_state_and_opens_its_own_window() -> None:
    """No privilege to drop, no password to ask for, nothing crossing an account."""
    with tempfile.TemporaryDirectory(prefix="handoff-consume.") as raw:
        tmp = Path(raw)
        with _Caller(tmp) as caller:
            handoff = _handoff(tmp, caller.user)
            opened: list[dict] = []
            code = _complete(caller, opened)
            assert code == 0, opened
            assert len(opened) == 1, opened
            written = opened[0]["output"]
            expected = (
                team_launcher.desktop_state_dir(PROJECT, caller.user)
                / f"{PROJECT}-presentation-layout.json"
            )
            assert written == expected, written
            assert caller.home in written.parents, written
            # Written by its owner, so it is private and needs no handover.
            assert stat.S_IMODE(written.stat().st_mode) == 0o600
            assert stat.S_IMODE(written.parent.stat().st_mode) == 0o700
            # Its own session: no privilege drop is asked for.
            assert opened[0]["gui_user"] is None, opened
            # And the handoff is consumed rather than left lying about.
            assert not handoff.exists()
            layout = json.loads(written.read_text(encoding="utf-8"))
            leaves = team_launcher._layout_leaves(layout)
            assert len(leaves) == len(ROLES), layout
            assert all(str(caller.home) == leaf["WorkingDirectory"] for leaf in leaves), leaves


def test_nothing_to_open_is_not_a_failure() -> None:
    """A viewer launch, or any verb that asked for no window, leaves no handoff."""
    with tempfile.TemporaryDirectory(prefix="handoff-absent.") as raw:
        tmp = Path(raw)
        with _Caller(tmp) as caller:
            opened: list[dict] = []
            assert _complete(caller, opened) == 0
            assert opened == [], opened


def test_a_handoff_that_is_not_one_is_refused_and_opens_nothing() -> None:
    """It has been through another account; arriving is not being trustworthy."""
    cases = {
        "schema": {"schema": "switchyard.something-else.v1"},
        "project": {"project": "somebody-else"},
        "slot count": {"slot_count": 99},
        "slot count type": {"slot_count": "six"},
        "relative program": {"pane_program": "scripts/switchyard-pane-window"},
    }
    for label, override in cases.items():
        with tempfile.TemporaryDirectory(prefix="handoff-refused.") as raw:
            tmp = Path(raw)
            with _Caller(tmp) as caller:
                handoff = _handoff(tmp, caller.user, **override)
                opened: list[dict] = []
                assert _complete(caller, opened) == 1, label
                assert opened == [], (label, opened)
                assert not handoff.exists(), label
                assert not (
                    team_launcher.desktop_state_dir(PROJECT, caller.user)
                    / f"{PROJECT}-presentation-layout.json"
                ).exists(), label


def test_a_pane_program_that_is_not_roots_is_refused() -> None:
    """Every tab of that window runs it, so the whole path to it must be root's."""
    with tempfile.TemporaryDirectory(prefix="handoff-program.") as raw:
        tmp = Path(raw)
        theirs = tmp / "switchyard-pane-window"
        theirs.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        theirs.chmod(0o755)
        with _Caller(tmp) as caller:
            _handoff(tmp, caller.user, pane_program=str(theirs))
            opened: list[dict] = []
            assert _complete(caller, opened) == 1
            assert opened == [], opened


def test_both_halves_render_the_same_window() -> None:
    """One definition, so what the caller opens is what the owner would have."""
    with tempfile.TemporaryDirectory(prefix="handoff-parity.") as raw:
        tmp = Path(raw)
        config, _config_path = _tenant(tmp)
        program = _pinned_pane_program()
        owner_side = presentation.presentation_layout_payload(
            PROJECT, slot_count=len(ROLES), owner="porter-agent",
            gui_user=DESKTOP_USER, pane_program=program,
        )
        caller_side = presentation.presentation_layout_payload(
            config.project, slot_count=len(ROLES), owner="porter-agent",
            gui_user=DESKTOP_USER, pane_program=program,
        )
        assert owner_side == caller_side


def _fake_process(proc_root: Path, pid: int, *, uid: int, argv: list[str]) -> None:
    entry = proc_root / str(pid)
    entry.mkdir(parents=True, exist_ok=True)
    (entry / "cmdline").write_bytes(("\0".join(argv) + "\0").encode("utf-8"))
    (entry / "status").write_text(
        f"Name:\t{Path(argv[0]).name}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n", encoding="utf-8"
    )


def _privileged_launch(proc_root: Path, layout: str) -> None:
    """The two processes a `sudo switchyard <project>` launch really leaves.

    sudo stays as the parent to relay the exit status; the terminal it started
    is the child, and it is the one running as the desktop account.
    """
    _fake_process(
        proc_root,
        900,
        uid=0,
        argv=[
            "sudo", "-u", DESKTOP_USER, "-H", "--",
            "env", "-i", "QT_QPA_PLATFORM=wayland",
            "/usr/bin/konsole", "--separate", "--layout", layout,
        ],
    )
    _fake_process(
        proc_root,
        901,
        uid=1000,
        argv=["/usr/bin/konsole", "--separate", "--layout", layout],
    )


def test_a_privilege_dropping_wrapper_is_not_a_terminal() -> None:
    """It has no tabs, so nothing falls back to a shell behind it."""
    with tempfile.TemporaryDirectory(prefix="wrapper-scan.") as raw:
        tmp = Path(raw)
        config, config_path = _tenant(tmp)
        proc_root = tmp / "proc"
        proc_root.mkdir()
        layout = str(tmp / f"{PROJECT}-presentation-layout.json")
        _privileged_launch(proc_root, layout)
        windows = team_launcher.presentation_window_processes(
            config, config_path=config_path, proc_root=proc_root
        )
        assert [(window.pid, window.uid) for window in windows] == [(901, 1000)], windows
        assert team_launcher.unsafe_root_presentation_windows(
            config, config_path=config_path, proc_root=proc_root
        ) == []


def test_a_wrapper_whose_terminal_has_not_started_reports_no_window() -> None:
    """There is no window yet, so there is nothing to call unsafe."""
    with tempfile.TemporaryDirectory(prefix="wrapper-only.") as raw:
        tmp = Path(raw)
        config, config_path = _tenant(tmp)
        proc_root = tmp / "proc"
        proc_root.mkdir()
        layout = str(tmp / f"{PROJECT}-presentation-layout.json")
        _privileged_launch(proc_root, layout)
        (proc_root / "901" / "cmdline").unlink()
        assert team_launcher.presentation_window_processes(
            config, config_path=config_path, proc_root=proc_root
        ) == []
        assert team_launcher.unsafe_root_presentation_windows(
            config, config_path=config_path, proc_root=proc_root
        ) == []


def test_a_terminal_that_really_is_root_is_still_caught() -> None:
    """The hazard this scan exists for has not moved."""
    with tempfile.TemporaryDirectory(prefix="root-terminal.") as raw:
        tmp = Path(raw)
        config, config_path = _tenant(tmp)
        proc_root = tmp / "proc"
        proc_root.mkdir()
        layout = str(tmp / f"{PROJECT}-presentation-layout.json")
        _fake_process(proc_root, 910, uid=0, argv=["konsole", "--separate", "--layout", layout])
        found = team_launcher.unsafe_root_presentation_windows(
            config, config_path=config_path, proc_root=proc_root
        )
        assert [window.pid for window in found] == [910], found
        # Including one a shell started: the shell is not the terminal, and the
        # terminal is caught on its own.
        _fake_process(
            proc_root, 911, uid=0,
            argv=["/bin/sh", "-lc", f"konsole --separate --layout {layout}"],
        )
        found = team_launcher.unsafe_root_presentation_windows(
            config, config_path=config_path, proc_root=proc_root
        )
        assert [window.pid for window in found] == [910], found


def test_a_launch_that_dropped_privilege_leaves_no_unsafe_report() -> None:
    """What start, status and upgrade say about a correctly privileged launch."""
    with tempfile.TemporaryDirectory(prefix="no-false-report.") as raw:
        tmp = Path(raw)
        config, config_path = _tenant(tmp)
        proc_root = tmp / "proc"
        proc_root.mkdir()
        _privileged_launch(proc_root, str(tmp / f"{PROJECT}-presentation-layout.json"))
        original_proc = team_launcher.PROC_ROOT
        try:
            team_launcher.PROC_ROOT = proc_root
            windows = team_launcher.unsafe_root_presentation_windows(
                config, config_path=config_path
            )
        finally:
            team_launcher.PROC_ROOT = original_proc
        assert windows == [], windows


def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_desktop_layout_path_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
