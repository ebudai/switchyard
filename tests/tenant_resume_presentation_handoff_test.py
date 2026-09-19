#!/usr/bin/env python3
"""SYRD-211 live UAT: a resumed tenant has to end with a window, not a shell.

The Zorin run got everything right up to the last step:

    tenant-control/helper recovery progressed
    project/worktree refresh completed
    Main started fresh; Designer, Director, Audit and Ops attached
    ... the expected nonfatal deferred-hook warning ...
    and then the shell came back, with no presentation window

Two faults met in the middle.

The owner half never handed the window back. `_hand_off_desktop_half` is
reached only through `launch_presentation`, which `presentation_enabled` gates
on the tenant config carrying a `presentation` section. A provisioned tenant
carries `desktop_access` and a `layout` and no such section, so a bridged
launch fell through to opening Konsole in the OWNER account -- which has no
screen.

The caller then treated the missing handoff as "nothing to open" and returned
zero. A tenant with a desktop policy that opened no window is not a success,
and saying so is the difference between a bug someone can report and a command
that looks like it worked.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from scripts import team_launcher as launcher  # noqa: E402
from first_run_login_inheritance_test import uat_config  # noqa: E402

CHECKS = 0


def check(condition: object, what: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(what)
    CHECKS += 1


class _Bridged:
    """The environment the tenant control bridge builds for the owner half."""

    def __init__(self, caller: str = "eric", handoff_fd: int | None = None) -> None:
        self.caller = caller
        self.handoff_fd = handoff_fd

    def __enter__(self):
        self._saved = {
            key: os.environ.get(key)
            for key in (launcher.TENANT_CONTROL_CALLER_ENV, launcher.PRESENTATION_HANDOFF_FD_ENV)
        }
        os.environ[launcher.TENANT_CONTROL_CALLER_ENV] = self.caller
        if self.handoff_fd is None:
            os.environ.pop(launcher.PRESENTATION_HANDOFF_FD_ENV, None)
        else:
            os.environ[launcher.PRESENTATION_HANDOFF_FD_ENV] = str(self.handoff_fd)
        return self

    def __exit__(self, *_exc):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return False


def _config(tmp: Path):
    """A real provisioned tenant's shape, loaded the way the launcher loads one.

    Built through the project loader rather than by constructing RoleConfig by
    hand, so the fixture cannot drift from what a tenant actually is.
    """
    return uat_config(tmp)


def test_the_owner_half_hands_the_window_back_when_it_came_through_the_bridge() -> None:
    """The half that was missing: a tenant with no `presentation` section."""
    read_fd, write_fd = os.pipe()
    said: list[str] = []
    try:
        with tempfile.TemporaryDirectory(prefix="syrd211-handoff.") as tmp:
            config = _config(Path(tmp))
            with _Bridged(handoff_fd=write_fd):
                handed = launcher.hand_presentation_back_to_the_caller(
                    config, slot_count=5, window_title="Test", print_func=said.append
                )
        check(handed is True, "the owner half reported the window rather than opening it")
        payload = json.loads(os.read(read_fd, 65536).decode("utf-8"))
        check(payload["project"] == config.project, f"it names the tenant: {payload}")
        check(payload["slot_count"] == 5, f"and every visible role: {payload}")
        check(payload.get("pane_program"), f"and the program each tab runs: {payload}")
        check(len(payload["slot_titles"]) == 5,
              f"one title per visible role: {payload['slot_titles']}")
        check(any("owns the screen" in line for line in said),
              f"and says who opens it: {said}")
    finally:
        for fd in (read_fd,):
            try:
                os.close(fd)
            except OSError:
                pass


def test_the_owner_half_does_not_try_to_own_the_desktop_window() -> None:
    """Konsole started from the owner account goes nowhere; it must not be tried."""
    with tempfile.TemporaryDirectory(prefix="syrd211-nokonsole.") as tmp:
        config = _config(Path(tmp))
        read_fd, write_fd = os.pipe()
        try:
            with _Bridged(handoff_fd=write_fd):
                handed = launcher.hand_presentation_back_to_the_caller(
                    config, slot_count=5, print_func=lambda _l: None
                )
            check(handed, "the handoff was taken")
            check(os.read(read_fd, 65536), "and something was written for the caller")
        finally:
            os.close(read_fd)

    # And with no descriptor offered, it declines rather than guessing: an
    # invocation that is not bridged opens its own window as it always did.
    with tempfile.TemporaryDirectory(prefix="syrd211-unbridged.") as tmp:
        config = _config(Path(tmp))
        with _Bridged(handoff_fd=None):
            check(
                launcher.hand_presentation_back_to_the_caller(
                    config, slot_count=5, print_func=lambda _l: None
                )
                is False,
                "no descriptor means no handoff, and the ordinary path is left alone",
            )


def test_a_desktop_tenant_with_no_handoff_is_a_failure_not_a_silent_success() -> None:
    """The caller half: the exact shape the User was given."""
    said: list[str] = []
    original = launcher._tenant_has_desktop_access
    launcher._tenant_has_desktop_access = lambda _project, **_k: True
    try:
        code = launcher.complete_desktop_presentation(
            "test", caller="eric", print_func=said.append
        )
    finally:
        launcher._tenant_has_desktop_access = original
    check(code != 0, f"a tenant whose window never opened is not a success: {code}")
    check(any("no presentation window was handed back" in line for line in said),
          f"and it says what did not happen: {said}")
    check(any("The tenant is up; its window is not" in line for line in said),
          f"separating what worked from what did not: {said}")


def test_a_headless_tenant_with_no_handoff_is_still_nothing_to_open() -> None:
    """The other half: a tenant with no window must not be reported as broken."""
    said: list[str] = []
    original = launcher._tenant_has_desktop_access
    launcher._tenant_has_desktop_access = lambda _project, **_k: False
    try:
        code = launcher.complete_desktop_presentation(
            "test", caller="eric", print_func=said.append
        )
    finally:
        launcher._tenant_has_desktop_access = original
    check(code == 0, f"a headless tenant opens nothing and that is correct: {code}")
    check(said == [], f"and says nothing about it: {said}")


def test_desktop_access_has_three_answers_against_the_real_registry() -> None:
    """Unknown is its own answer, and this host has one of each.

    Most tenants' configs live under their owner's home and cannot be read from
    here -- that is the boundary working. Folding that into "no window" would
    put the silent success straight back for exactly the tenants that have one.
    """
    check(launcher._tenant_has_desktop_access("syrd") is True,
          "a readable config with a desktop policy answers yes")
    check(launcher._tenant_has_desktop_access("definitely-not-a-tenant") is None,
          "an unregistered name answers unknown, not no")
    check(launcher._tenant_has_desktop_access("testing") is None,
          "and so does a registered tenant whose config this account cannot read")
    # The caller's OWN state directory is readable, and says this account has
    # had this project's window before.
    check(launcher._tenant_has_desktop_access(
        "testing", caller=launcher.current_user_name()) is True,
        "which the caller's own desktop state directory can still settle")


def test_a_readable_config_without_a_desktop_policy_answers_no() -> None:
    """The third answer, through the real parsing rather than a stub of it.

    No tenant on this host has a readable headless config, so one is written
    and handed to the same lookup the product uses: the file is real, the read
    is real, and only where the entry comes from is arranged.
    """
    with tempfile.TemporaryDirectory(prefix="syrd211-headless.") as tmp:
        config_path = Path(tmp) / "headless.json"
        config_path.write_text(
            json.dumps({"project": "headless", "layout": "headless-layout.json"}),
            encoding="utf-8",
        )

        class _Entry:
            def __init__(self, path: Path) -> None:
                self.config_path = str(path)

        original = launcher._usable_switchyard_entry_for_project
        launcher._usable_switchyard_entry_for_project = (
            lambda _project, **_kwargs: (_Entry(config_path), [])
        )
        try:
            answer = launcher._tenant_has_desktop_access("headless")
        finally:
            launcher._usable_switchyard_entry_for_project = original
    check(answer is False,
          f"a config that is readable and has no desktop policy answers no: {answer!r}")

    # And the same lookup with a config carrying one answers yes.
    with tempfile.TemporaryDirectory(prefix="syrd211-withdesktop.") as tmp:
        config_path = Path(tmp) / "desktop.json"
        config_path.write_text(
            json.dumps({"project": "withdesktop", "desktop_access": {"mode": "wayland"}}),
            encoding="utf-8",
        )

        class _Entry2:
            def __init__(self, path: Path) -> None:
                self.config_path = str(path)

        original = launcher._usable_switchyard_entry_for_project
        launcher._usable_switchyard_entry_for_project = (
            lambda _project, **_kwargs: (_Entry2(config_path), [])
        )
        try:
            answer = launcher._tenant_has_desktop_access("withdesktop")
        finally:
            launcher._usable_switchyard_entry_for_project = original
    check(answer is True, f"and one that has a desktop policy answers yes: {answer!r}")


def test_an_unknown_tenant_shape_is_not_reported_as_a_fault() -> None:
    """Silence is right when this account genuinely cannot tell."""
    said: list[str] = []
    original = launcher._tenant_has_desktop_access
    launcher._tenant_has_desktop_access = lambda _project, **_k: None
    try:
        code = launcher.complete_desktop_presentation(
            "test", caller="eric", print_func=said.append
        )
    finally:
        launcher._tenant_has_desktop_access = original
    check(code == 0, f"an unknown shape is not a failure: {code}")
    check(said == [], f"and nothing is claimed about it: {said}")


def test_an_ordinary_unbridged_launch_does_not_hand_its_window_away() -> None:
    """The branch is for the owner half only; a desktop launch opens its own."""
    saved = os.environ.pop(launcher.TENANT_CONTROL_CALLER_ENV, None)
    try:
        check(launcher.running_through_tenant_control() is False,
              "with no bridge marker, this is not the owner half")
    finally:
        if saved is not None:
            os.environ[launcher.TENANT_CONTROL_CALLER_ENV] = saved
    with _Bridged(caller="eric"):
        check(launcher.running_through_tenant_control() is True,
              "and with one, it is")


def test_a_deferred_hook_warning_does_not_suppress_the_window() -> None:
    """The warning the UAT saw is nonfatal, and must stay that way.

    A missing deferred Codex SessionStart hook is reported and the launch goes
    on; it must not be what stops the presentation being handed back.
    """
    source = (ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8")
    start = source.index("        elif running_through_tenant_control()")
    window_branch = source[start : start + 700]
    check("hand_presentation_back_to_the_caller" in window_branch,
          "the bridged branch hands the window back")
    check("launch_result = 0" in window_branch,
          "and reports success for the half it did")
    # The hook check is a warning path: nothing in the window branch consults it.
    check("hook" not in window_branch.casefold(),
          f"the window does not depend on any hook result: {window_branch[:200]}")


def main() -> int:
    failures = 0
    for name, value in sorted(globals().items()):
        if not (name.startswith("test_") and callable(value)):
            continue
        try:
            value()
        except BaseException as exc:  # noqa: BLE001
            failures += 1
            print(f"FAILED {name}: {type(exc).__name__}: {exc}")
    if failures:
        print(f"tenant_resume_presentation_handoff_test: {failures} failed")
        return 1
    print(f"tenant_resume_presentation_handoff_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
