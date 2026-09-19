#!/usr/bin/env python3
"""`switchyard stop <tenant>` must close that tenant's window, and say it stopped.

Live UAT for SYRD-193 (2026-09-17) ran start/status/stop/status/start/status as
eric and got: the original testing Konsole still open with five detached, inert
panes; a second window from the final start; two Konsole processes (1684154 and
1687389) both on
`/home/eric/.local/state/switchyard/projects/testing/testing-presentation-layout.json`;
and no `stop` anywhere in the tenant-control journal.

Two separate defects, so two separate groups of tests below.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from team_launcher_test_helpers import *  # noqa: F401,F403
from standalone_test_runner import run_module_tests

PROJECT = "porter"
ROLES = ("designer", "director", "audit", "ops", "app", "main")
DESKTOP_USER = "eric"


def _tenant(tmp: Path):
    """A tenant this account owns, whose screen belongs to somebody else."""
    config_path = _write_six_visible_role_config(tmp, project=PROJECT)  # noqa: F405
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["presentation"] = {
        "slot_count": len(ROLES),
        "layouts": {"default": {str(index): role for index, role in enumerate(ROLES)}},
    }
    payload["run_as_user"] = team_launcher.current_user_name()  # noqa: F405
    config_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return team_launcher.load_project_config(PROJECT, config_path), config_path  # noqa: F405


def _konsole(proc_root: Path, pid: int, layout: str, *, uid: int | None = None, split: bool = True) -> None:
    """One Konsole process in a fake /proc, showing `layout`."""
    entry = proc_root / str(pid)
    entry.mkdir(parents=True, exist_ok=True)
    argv = ["/usr/sbin/konsole", "--separate", "--qwindowtitle", PROJECT]
    argv += ["--layout", layout] if split else [f"--layout={layout}"]
    (entry / "cmdline").write_bytes(("\0".join(argv) + "\0").encode("utf-8"))
    resolved = os.getuid() if uid is None else uid
    (entry / "status").write_text(
        f"Name:\tkonsole\nUid:\t{resolved}\t{resolved}\t{resolved}\t{resolved}\n", encoding="utf-8"
    )


def _desktop_layout(config, config_path: Path) -> str:
    return str(
        team_launcher.desktop_presentation_layout_path(  # noqa: F405
            config, config_path=config_path, gui_user=DESKTOP_USER
        )
    )


# --------------------------------------------------------------------------
# Defect 1: the window the human is looking at is not the window stop looks for.
# --------------------------------------------------------------------------

def _desktop_konsole(proc_root: Path, pid: int, home: Path, project: str, *, split: bool = True) -> str:
    """A Konsole started the way the desktop half starts one, and its layout arg."""
    layout = str(home / ".local" / "state" / "switchyard" / "projects" / project
                 / f"{project}-presentation-layout.json")
    _konsole(proc_root, pid, layout, uid=1000, split=split)
    return layout


def test_stop_closes_the_window_this_account_actually_owns() -> None:
    """The defect, stated as the behaviour that was missing.

    The live window names the DESKTOP account's copy of the layout, because that
    is the only one Konsole can read (SYRD-65). The stop ran entirely on the
    owner side, where the scan asks for the TENANT's path, so it matched nothing
    and reported "already closed" over a window still on screen.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        home = root / "home" / DESKTOP_USER
        proc_root = root / "proc"
        _desktop_konsole(proc_root, 1687389, home, PROJECT)

        signalled: list[int] = []
        original = team_launcher._gui_home  # noqa: F405
        try:
            team_launcher._gui_home = lambda user: str(root / "home" / user)  # noqa: F405
            code = team_launcher.close_desktop_presentation(  # noqa: F405
                PROJECT, caller=DESKTOP_USER, proc_root=proc_root,
                signaller=lambda pid, sig: signalled.append(pid), print_func=lambda _m: None,
            )
        finally:
            team_launcher._gui_home = original  # noqa: F405
        assert code == 0, code
        assert signalled == [1687389], signalled


def test_stop_closes_every_duplicate_window_and_nothing_else() -> None:
    """Stale duplicates all go; a sibling tenant and look-alike paths stay.

    The UAT left two Konsoles on the same layout, so closing "the" window is not
    enough -- and a substring match would take `porter-staging` with it.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        home = root / "home" / DESKTOP_USER
        proc_root = root / "proc"
        _desktop_konsole(proc_root, 1684154, home, PROJECT)
        _desktop_konsole(proc_root, 1687389, home, PROJECT, split=False)
        _desktop_konsole(proc_root, 540400, home, "syrd")
        _desktop_konsole(proc_root, 999002, home, f"{PROJECT}-staging")
        layout = str(home / ".local" / "state" / "switchyard" / "projects" / PROJECT
                     / f"{PROJECT}-presentation-layout.json")
        _konsole(proc_root, 999001, layout + ".backup", uid=1000)

        signalled: list[int] = []
        original = team_launcher._gui_home  # noqa: F405
        try:
            team_launcher._gui_home = lambda user: str(root / "home" / user)  # noqa: F405
            code = team_launcher.close_desktop_presentation(  # noqa: F405
                PROJECT, caller=DESKTOP_USER, proc_root=proc_root,
                signaller=lambda pid, sig: signalled.append(pid), print_func=lambda _m: None,
            )
        finally:
            team_launcher._gui_home = original  # noqa: F405
        assert code == 0, code
        assert sorted(signalled) == [1684154, 1687389], signalled


def test_a_window_stop_could_not_close_is_reported_not_swallowed() -> None:
    """Stop must not report success over a window that is still on screen."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        home = root / "home" / DESKTOP_USER
        proc_root = root / "proc"
        _desktop_konsole(proc_root, 1687389, home, PROJECT)

        def refuse(pid: int, sig: int) -> None:
            raise PermissionError(pid)

        said: list[str] = []
        original = team_launcher._gui_home  # noqa: F405
        try:
            team_launcher._gui_home = lambda user: str(root / "home" / user)  # noqa: F405
            code = team_launcher.close_desktop_presentation(  # noqa: F405
                PROJECT, caller=DESKTOP_USER, proc_root=proc_root,
                signaller=refuse, print_func=said.append,
            )
        finally:
            team_launcher._gui_home = original  # noqa: F405
        assert code == 1, code
        assert any("1687389" in line for line in said), said


def test_the_stop_verb_takes_the_closing_half_not_the_opening_one() -> None:
    """A stop must never run the branch that OPENS a window."""
    opened: list[str] = []
    closed: list[str] = []
    original_open = team_launcher.complete_desktop_presentation  # noqa: F405
    original_close = team_launcher.close_desktop_presentation  # noqa: F405
    # A normally provisioned tenant, which is this test's precondition: since
    # SYRD-211 the bridge verifies the staged helper before asking root to run
    # it, so a fixture with no helper at all is refused before either desktop
    # half is reached. A sandbox cannot create root-owned files, and the
    # entitled owner of those paths is root -- so the stand-in uid is declared
    # here rather than being whatever this process happens to be.
    staged = tempfile.TemporaryDirectory(prefix="tenant-stop-helper.")
    helper_root = Path(staged.name)
    helper_dir = helper_root / PROJECT
    helper_dir.mkdir(parents=True)
    # Both protocol programs. Since SYRD-211's DAT rejection the launch checks
    # every one of them rather than only the bridge, so a fixture staging just
    # the bridge is a half-staged tenant -- which is a different subject from
    # this test's, and would be refused before either desktop half is reached.
    for name in ("switchyard-tenant-control", "switchyard-display-attach"):
        staged_program = helper_dir / name
        staged_program.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        staged_program.chmod(0o755)
    helper = helper_dir / "switchyard-tenant-control"
    verify_in_sandbox = lambda *a, **k: team_launcher.ensure_tenant_control_helper(  # noqa: E731,F405
        *a, **k, root=helper_root, owner_uid=os.getuid()
    )
    try:
        team_launcher.complete_desktop_presentation = (  # noqa: F405
            lambda project, **kw: opened.append(project) or 0
        )
        team_launcher.close_desktop_presentation = (  # noqa: F405
            lambda project, **kw: closed.append(project) or 0
        )
        for operation, expect_open, expect_close in (
            ("start", [PROJECT], []),
            ("stop", [], [PROJECT]),
        ):
            opened.clear()
            closed.clear()
            try:
                team_launcher._switchyard_exec_through_tenant_control(  # noqa: F405
                    PROJECT, operation,
                    grant={"authorized_user": team_launcher.current_user_name()},  # noqa: F405
                    runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 0),
                    ensure_helper=verify_in_sandbox,
                )
            except SystemExit as exit_code:
                assert exit_code.code == 0, (operation, exit_code.code)
            assert opened == expect_open, (operation, opened)
            assert closed == expect_close, (operation, closed)
    finally:
        team_launcher.complete_desktop_presentation = original_open  # noqa: F405
        team_launcher.close_desktop_presentation = original_close  # noqa: F405
        staged.cleanup()


# --------------------------------------------------------------------------
# Defect 2: the journal must name the verb that actually ran.
# --------------------------------------------------------------------------

def _bridge():
    """The bridge module, which has no .py suffix."""
    spec = importlib.util.spec_from_loader(
        "switchyard_tenant_control",
        importlib.machinery.SourceFileLoader(
            "switchyard_tenant_control", str(ROOT / "scripts" / "switchyard-tenant-control")
        ),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_journal_names_the_verb_that_ran() -> None:
    """Never `start` for a stop: attribution is the point of a boundary record."""
    bridge = _bridge()
    for operation in ("start", "stop", "status"):
        calls: list[list[str]] = []

        class _Recorder:
            @staticmethod
            def run(argv, **_kwargs):
                calls.append([str(part) for part in argv])
                return subprocess.CompletedProcess(argv, 0)

        real_is_file = type(bridge.ROLLOUT_RECORDER).is_file
        try:
            type(bridge.ROLLOUT_RECORDER).is_file = lambda _self: True
            sys.modules["subprocess"].run, saved = _Recorder.run, sys.modules["subprocess"].run
            bridge.record_privileged_boundary(
                PROJECT, operation, caller=DESKTOP_USER, owner="porter-agent", code=0
            )
        finally:
            sys.modules["subprocess"].run = saved
            type(bridge.ROLLOUT_RECORDER).is_file = real_is_file

        assert len(calls) == 1, calls
        argv = calls[0]
        action = argv[argv.index("--action") + 1]
        assert action == f"switchyard {operation} {PROJECT}", action
        assert argv[argv.index("--operator") + 1] == DESKTOP_USER, argv



def test_a_boundary_that_could_not_be_recorded_says_so() -> None:
    """Silence is not evidence.

    The record is best effort about the OUTCOME -- a journal that cannot be
    written must not turn a completed stop into a failure. It was also best
    effort about the EVIDENCE: stderr went to DEVNULL and a non-zero recorder
    was discarded, so a boundary that went unjournalled left no trace anywhere,
    including no trace that recording had been attempted. That is why the live
    UAT could not say why its stop is missing from the journal.
    """
    bridge = _bridge()
    said: list[str] = []

    class _Failing:
        @staticmethod
        def run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 3, stderr="journal is read-only\n")

    real_is_file = type(bridge.ROLLOUT_RECORDER).is_file
    saved_run = sys.modules["subprocess"].run
    saved_stderr = sys.stderr

    class _Capture:
        @staticmethod
        def write(text):
            said.append(text)

        @staticmethod
        def flush():
            return None

    try:
        type(bridge.ROLLOUT_RECORDER).is_file = lambda _self: True
        sys.modules["subprocess"].run = _Failing.run
        sys.stderr = _Capture
        bridge.record_privileged_boundary(
            PROJECT, "stop", caller=DESKTOP_USER, owner="porter-agent", code=0
        )
    finally:
        sys.stderr = saved_stderr
        sys.modules["subprocess"].run = saved_run
        type(bridge.ROLLOUT_RECORDER).is_file = real_is_file

    message = "".join(said)
    assert "boundary record failed" in message, message
    assert "journal is read-only" in message, message
    assert f"switchyard stop {PROJECT}" in message, message


if __name__ == "__main__":
    run_module_tests(globals())
    print("tenant_stop_presentation_window_test: ok")
