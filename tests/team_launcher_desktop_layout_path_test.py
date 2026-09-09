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
