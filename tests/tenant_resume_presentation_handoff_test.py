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


REAL_PINNED_HELPER = Path("/usr/local/lib/switchyard/syrd/switchyard-display-attach")


def test_auto_layout_on_a_non_kde_desktop_resolves_to_viewer() -> None:
    """Zorin's shape, from the product's own resolver rather than an assumption."""
    check(
        launcher.resolve_layout_mode(
            "auto", environ={"XDG_CURRENT_DESKTOP": "X-Cinnamon"},
            runner=lambda *_a, **_k: __import__("subprocess").CompletedProcess([], 1),
        ) == launcher.LAYOUT_MODE_VIEWER,
        "a non-KDE desktop gets the viewer layout",
    )
    check(
        launcher.resolve_layout_mode(
            "auto", environ={"XDG_CURRENT_DESKTOP": "KDE"},
            runner=lambda *_a, **_k: __import__("subprocess").CompletedProcess([], 1),
        ) == launcher.LAYOUT_MODE_SEPARATE,
        "and KDE gets the separate one",
    )


def test_the_bridged_viewer_hands_off_a_viewer_and_the_caller_opens_one_tab() -> None:
    """The missed path, end to end through the real renderer and validator.

    The owner builds one tiled tmux session; the caller has to be told that,
    because five tabs onto display sessions that do not exist is a window of
    five errors. The payload is rendered by the product, validated by the
    product, and turned into a layout by the product.
    """
    if not REAL_PINNED_HELPER.is_file():
        print("  (skipped: this host has no staged display-attach helper to pin)")
        return
    read_fd, write_fd = os.pipe()
    said: list[str] = []
    try:
        with tempfile.TemporaryDirectory(prefix="syrd211-viewer.") as tmp:
            config = _config(Path(tmp))
            with _Bridged(handoff_fd=write_fd):
                handed = launcher.hand_presentation_back_to_the_caller(
                    config,
                    slot_count=1,
                    window_title="Syrd",
                    layout=launcher.LAYOUT_MODE_VIEWER,
                    slot_titles=["Syrd"],
                    pane_program=REAL_PINNED_HELPER,
                    print_func=said.append,
                )
        check(handed is True, "the viewer branch hands the display back")
        payload = json.loads(os.read(read_fd, 65536).decode("utf-8"))
        check(payload["layout"] == launcher.LAYOUT_MODE_VIEWER,
              f"and says it is a viewer: {payload}")
        check(payload["slot_count"] == 1, f"with one thing to show: {payload}")
    finally:
        os.close(read_fd)

    # The caller validates it again and builds its own layout from it.
    validated, problem = launcher.validated_presentation_handoff(
        payload, project=payload["project"]
    )
    check(not problem, f"the caller accepts it: {problem}")
    check(validated["layout"] == launcher.LAYOUT_MODE_VIEWER,
          "and keeps the layout through validation")
    from scripts import presentation_controller

    built = presentation_controller.presentation_layout_payload(
        payload["project"], slot_count=validated["slot_count"],
        owner="otto-agent", gui_user="eric",
        pane_program=Path(validated["pane_program"]),
        slot_titles=validated["slot_titles"], window_title=validated["window_title"],
        layout_mode=validated["layout"],
    )
    leaves = launcher._layout_leaves(built)
    check(len(leaves) == 1, f"one tab, not one per absent display session: {len(leaves)}")
    command = leaves[0]["Command"]
    project = payload["project"]
    check(command.rstrip().endswith("viewer"),
          f"the tab asks the helper for the viewer target: {command[-80:]}")
    # Not "-display-" anywhere: that substring is in the helper's own filename,
    # `switchyard-display-attach`, so the check has to name the SESSION.
    check(f"{project}-display-" not in command,
          f"never a per-slot display session: {command}")


def test_the_bridged_separate_layout_still_opens_one_tab_per_slot() -> None:
    """The audited path, unchanged: KDE keeps its per-slot window."""
    if not REAL_PINNED_HELPER.is_file():
        print("  (skipped: this host has no staged display-attach helper to pin)")
        return
    payload = launcher.render_presentation_handoff(
        "syrd", slot_count=5, pane_program=REAL_PINNED_HELPER,
        slot_titles=["a", "b", "c", "d", "e"], window_title="Syrd",
    )
    check(payload["layout"] == launcher.LAYOUT_MODE_SEPARATE,
          f"the default is the separate layout: {payload['layout']}")
    validated, problem = launcher.validated_presentation_handoff(payload, project="syrd")
    check(not problem, f"which validates: {problem}")
    from scripts import presentation_controller

    built = presentation_controller.presentation_layout_payload(
        "syrd", slot_count=validated["slot_count"], owner="syrd-agent", gui_user="eric",
        pane_program=Path(validated["pane_program"]),
        slot_titles=validated["slot_titles"], window_title=validated["window_title"],
        layout_mode=validated["layout"],
    )
    check(len(launcher._layout_leaves(built)) == 5,
          "and still opens one tab per slot")


def test_a_handoff_naming_an_unknown_layout_is_refused() -> None:
    if not REAL_PINNED_HELPER.is_file():
        print("  (skipped: this host has no staged display-attach helper to pin)")
        return
    payload = launcher.render_presentation_handoff(
        "syrd", slot_count=1, pane_program=REAL_PINNED_HELPER,
        slot_titles=["Syrd"], window_title="Syrd",
    )
    payload["layout"] = "something-else"
    _, problem = launcher.validated_presentation_handoff(payload, project="syrd")
    check("unknown layout" in problem, f"an unknown layout is refused: {problem}")
    # And a payload from an older owner half, with no layout at all, still works.
    payload.pop("layout")
    validated, problem = launcher.validated_presentation_handoff(payload, project="syrd")
    check(not problem, f"an older payload is still understood: {problem}")
    check(validated["layout"] == launcher.LAYOUT_MODE_SEPARATE,
          "and reads as the layout that predates the field")


def test_the_privileged_helper_accepts_the_viewer_target_and_nothing_else_new() -> None:
    """The boundary keeps validating: one more target, not a way to name sessions."""
    import types

    source = (ROOT / "scripts" / "switchyard-display-attach").read_text(encoding="utf-8")
    helper = types.ModuleType("display_attach_under_test")
    helper.__dict__["__name__"] = "display_attach_under_test"
    exec(compile(source, "switchyard-display-attach", "exec"), helper.__dict__)  # noqa: S102
    check(helper.session_name("test", "viewer") == "test-viewer",
          "the viewer target names this tenant's viewer session")
    check(helper.session_name("test", 3) == "test-display-3",
          "and a slot still names its display session")
    # The lock is applied to whatever was selected, viewer included.
    argv = helper.lock_argv(helper.session_name("test", "viewer"))
    check(argv[:4] == ["tmux", "set-option", "-t", "=test-viewer:"],
          f"the key lock targets the viewer session exactly: {argv[:4]}")


def test_launch_project_on_auto_non_kde_emits_the_viewer_handoff() -> None:
    """Driven through `launch_project` itself, because the branch is the bug.

    The previous candidate added the handoff only to the separate branch, and a
    test that calls the handoff helper directly cannot tell the difference. This
    resolves the layout the way the product does -- auto, on a desktop that is
    not KDE -- and reads what came out of the bridge descriptor.
    """
    import subprocess as _sp

    read_fd, write_fd = os.pipe()
    saved_desktop = launcher.detected_invoking_desktop
    saved_user = launcher.current_user_name
    said: list[str] = []
    try:
        with tempfile.TemporaryDirectory(prefix="syrd211-launch.") as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            (repo / "worktrees" / "ops").mkdir(parents=True)
            layout_file = tmp_path / "layout.json"
            # Built by the product's own generator, so the fixture's layout is
            # the shape the launcher expects rather than a guess at it.
            layout_file.write_text(
                json.dumps(launcher._new_project_layout_payload(1)) + "\n",
                encoding="utf-8",
            )
            config_path = tmp_path / "porter.json"
            config_path.write_text(
                json.dumps({
                    # Headless here on purpose: the branch under test is the
                    # bridged VIEWER path, which does not consult the desktop
                    # policy, and a wayland policy would pull this fixture into
                    # consent validation that is somebody else's subject.
                    "desktop_access": {"mode": "headless"},
                    "project": "porter",
                    "run_as_user": launcher.current_user_name(),
                    "layout": str(layout_file),
                    "repository": str(repo),
                    "roles": [
                        {"role": "ops", "slot": 0, "cli": ["codex"],
                         "target": "porter-ops:0.0",
                         "workdir": str(repo / "worktrees" / "ops")},
                    ],
                }) + "\n",
                encoding="utf-8",
            )
            config = launcher.load_project_config("porter", config_path)

            # A desktop that is not KDE, so `auto` resolves to the viewer.
            launcher.detected_invoking_desktop = lambda **_k: "X-Cinnamon"
            check(
                launcher.resolve_layout_mode("auto", environ={"XDG_CURRENT_DESKTOP": "X-Cinnamon"},
                                             runner=lambda *_a, **_k: _sp.CompletedProcess([], 1))
                == launcher.LAYOUT_MODE_VIEWER,
                "the fixture really is on the viewer branch",
            )

            class _Ok:
                """Everything the launch asks of the system says yes."""

                def __init__(self) -> None:
                    self.calls: list[list[str]] = []

                def __call__(self, args, **_kwargs):
                    self.calls.append(list(args))
                    return _sp.CompletedProcess(list(args), 0, stdout="", stderr="")

            runner = _Ok()
            with _Bridged(handoff_fd=write_fd):
                try:
                    launcher.launch_project(
                        config,
                        config_path=config_path,
                        mode="start",
                        script_path=ROOT / "scripts" / "team-launcher",
                        runner=runner,
                        layout_output=tmp_path / "layout-output.json",
                        layout_mode="auto",
                        print_func=said.append,
                    )
                except BaseException as exc:  # noqa: BLE001
                    # The launch may not complete in a fixture, but the handoff
                    # is emitted at the viewer branch and that is the subject.
                    said.append(f"(launch raised {type(exc).__name__}: {exc})")
        # The writer takes ownership of the descriptor and closes it, so this
        # is only for the case where it never got that far.
        try:
            os.close(write_fd)
        except OSError:
            pass
        write_fd = -1
        raw = os.read(read_fd, 65536).decode("utf-8").strip()
    finally:
        launcher.detected_invoking_desktop = saved_desktop
        launcher.current_user_name = saved_user
        for fd in (read_fd, write_fd):
            if fd and fd > 0:
                try:
                    os.close(fd)
                except OSError:
                    pass

    check(raw, f"the viewer branch wrote a handoff: {said[-3:]}")
    payload = json.loads(raw.splitlines()[0])
    check(payload["project"] == "porter", f"for this tenant: {payload}")
    check(payload["layout"] == launcher.LAYOUT_MODE_VIEWER,
          f"and says it is the viewer layout: {payload}")
    check(payload["slot_count"] == 1,
          f"with one thing for the caller to show: {payload}")
    check("switchyard-display-attach" in payload["pane_program"],
          f"opened through the privileged attach helper: {payload}")


def test_the_viewer_session_is_named_when_the_caller_attaches_directly() -> None:
    """Owner and desktop the same account: no helper, a direct tmux attach.

    The sudo form never mentions the session -- the helper derives it from
    pinned data -- so this is the path where getting the viewer session's NAME
    wrong is visible.
    """
    from scripts import presentation_controller

    args = presentation_controller.display_attach_args_for(
        "porter", presentation_controller.VIEWER_ATTACH_TARGET,
        owner="eric", gui_user="eric",
    )
    check("porter-viewer" in " ".join(args),
          f"it attaches this tenant's viewer session: {args}")
    check("porter-display-" not in " ".join(args),
          f"and not a display session: {args}")
    slot_args = presentation_controller.display_attach_args_for(
        "porter", 0, owner="eric", gui_user="eric",
    )
    check("porter-display-0" in " ".join(slot_args),
          f"a slot still names its display session: {slot_args}")


def test_the_helper_refuses_a_target_that_is_neither_a_slot_nor_the_viewer() -> None:
    """One more validated target, not a way to name sessions."""
    import types

    source = (ROOT / "scripts" / "switchyard-display-attach").read_text(encoding="utf-8")
    helper = types.ModuleType("display_attach_argv")
    helper.__dict__["__name__"] = "display_attach_argv"
    exec(compile(source, "switchyard-display-attach", "exec"), helper.__dict__)  # noqa: S102
    for bogus in ("viewerr", "../viewer", "0x1", "", "display-0"):
        try:
            helper.main(["test", bogus])
        except SystemExit as exc:
            message = str(exc)
            check("slot must be a number" in message or "usage:" in message,
                  f"{bogus!r} is refused by name: {message}")
        except BaseException as exc:  # noqa: BLE001
            raise AssertionError(
                f"{bogus!r} should be refused cleanly, not raise {type(exc).__name__}: {exc}"
            ) from exc
        else:
            raise AssertionError(f"{bogus!r} should not be accepted")


def _module_from(path: Path, name: str):
    """Load a program from the bytes at `path`, which is the point.

    Executing the checkout's source proves what the release would install.
    Executing the STAGED file proves what this tenant would actually run, and
    those are the two different things the DAT rejection was about.
    """
    import types

    module = types.ModuleType(name)
    module.__dict__["__name__"] = name
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)  # noqa: S102
    return module


def test_the_viewer_discriminator_survives_the_real_root_reserializer() -> None:
    """Owner -> ROOT -> caller, through the shipped `publish_handoff`.

    My earlier cases read the owner's descriptor or fed the owner's payload
    straight to the caller's validator, and so could not see that root
    reserializes a fixed set of fields and dropped this one. This runs the real
    root function and reads what it actually wrote.
    """
    if not REAL_PINNED_HELPER.is_file():
        print("  (skipped: this host has no staged display-attach helper to pin)")
        return
    import pwd as _pwd

    bridge = _module_from(ROOT / "scripts" / "switchyard-tenant-control", "tenant_control_root")
    me = _pwd.getpwuid(os.getuid()).pw_name
    destination = (
        Path.home() / ".local" / "state" / "switchyard" / "projects" / "syrd"
        / "syrd-presentation-handoff.json"
    )

    def publish(payload: dict) -> dict | None:
        if destination.exists():
            destination.unlink()
        bridge.publish_handoff("syrd", json.dumps(payload), caller=me)
        if not destination.exists():
            return None
        written = json.loads(destination.read_text(encoding="utf-8"))
        destination.unlink()
        return written

    owner_payload = launcher.render_presentation_handoff(
        "syrd", slot_count=1, pane_program=REAL_PINNED_HELPER,
        slot_titles=["Syrd"], window_title="Syrd",
        layout=launcher.LAYOUT_MODE_VIEWER,
    )
    republished = publish(owner_payload)
    check(republished is not None, "root published the viewer handoff")
    check(republished["layout"] == launcher.LAYOUT_MODE_VIEWER,
          f"and kept the discriminator: {republished}")

    # The caller then validates root's copy -- not the owner's -- and builds
    # from it. That is the chain the live run takes.
    validated, problem = launcher.validated_presentation_handoff(republished, project="syrd")
    check(not problem, f"the caller accepts root's copy: {problem}")
    check(validated["layout"] == launcher.LAYOUT_MODE_VIEWER,
          "still a viewer after two validations")
    from scripts import presentation_controller

    built = presentation_controller.presentation_layout_payload(
        "syrd", slot_count=validated["slot_count"], owner="syrd-agent", gui_user=me,
        pane_program=Path(validated["pane_program"]),
        slot_titles=validated["slot_titles"], window_title=validated["window_title"],
        layout_mode=validated["layout"],
    )
    leaves = launcher._layout_leaves(built)
    check(len(leaves) == 1, f"one tab: {len(leaves)}")
    check(leaves[0]["Command"].rstrip().endswith("viewer"),
          f"asking the helper for the viewer target: {leaves[0]['Command'][-60:]}")

    # An older owner half, with no layout at all, still reaches the caller.
    older = {k: v for k, v in owner_payload.items() if k != "layout"}
    republished_older = publish(older)
    check(republished_older is not None, "an older owner half is still published")
    check(republished_older["layout"] == launcher.LAYOUT_MODE_SEPARATE,
          f"as the layout that predates the field: {republished_older}")

    # And root refuses an arbitrary discriminator rather than relaying it.
    hostile = dict(owner_payload)
    hostile["layout"] = "anything-at-all"
    check(publish(hostile) is None,
          "root publishes nothing for a layout it does not know")


def test_the_staged_helper_the_tenant_would_actually_run_accepts_the_viewer() -> None:
    """The last layer: the per-tenant copy, not the release's source."""
    if not REAL_PINNED_HELPER.is_file():
        print("  (skipped: this host has no staged display-attach helper)")
        return
    staged = _module_from(REAL_PINNED_HELPER, "staged_display_attach")
    if not hasattr(staged, "VIEWER_TARGET"):
        # Exactly the preserved tenant's situation. The layer is still proved,
        # by the upgrade case below, which stages that older helper itself and
        # then runs the upgraded file.
        print("  (this host's staged helper predates the viewer target, as Zorin's did;"
              " the upgrade case proves this layer)")
        return
    check(staged.session_name("syrd", staged.VIEWER_TARGET) == "syrd-viewer",
          "the staged helper resolves the viewer target to this tenant's viewer session")
    check(staged.session_name("syrd", 2) == "syrd-display-2",
          "and a slot still names its display session")


def test_a_tenant_staged_by_an_older_release_is_restaged_before_it_is_used() -> None:
    """The preserved Zorin tenant's shape: present, correct, and out of date.

    It begins with the exact `d6bed23` display helper -- numeric-only, which
    refuses `viewer` -- and the launch has to bring it up to this release
    through the recorded privileged path before the window is built.
    """
    old_source = ROOT / ".git"  # presence check only; bytes come from git below
    import subprocess as _sp

    old_bytes = _sp.run(
        ["git", "show", "d6bed23c98fd8127302d4b9fd994c1582f57fcdf:scripts/switchyard-display-attach"],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    if old_bytes.returncode != 0:
        print("  (skipped: the d6bed23 helper is not reachable from this checkout)")
        return
    del old_source

    with tempfile.TemporaryDirectory(prefix="syrd211-stale.") as tmp:
        root = Path(tmp)
        release = root / "release"
        (release / "scripts").mkdir(parents=True)
        staging_root = root / "staging"
        mine = staging_root / "test"
        mine.mkdir(parents=True)
        neighbour = staging_root / "other"
        neighbour.mkdir(parents=True)

        # This release's copies.
        for name in launcher.ROLE_STAGED_EXECUTABLES:
            source = ROOT / "scripts" / name
            if source.is_file():
                (release / "scripts" / name).write_bytes(source.read_bytes())

        # The tenant's staged copies: correct shape, previous release.
        for name in launcher.ROLE_STAGED_EXECUTABLES:
            source = ROOT / "scripts" / name
            if source.is_file():
                target = mine / name
                target.write_bytes(source.read_bytes())
                target.chmod(0o755)
        stale_helper = mine / "switchyard-display-attach"
        stale_helper.write_text(old_bytes.stdout, encoding="utf-8")
        stale_helper.chmod(0o755)
        untouched = neighbour / "switchyard-display-attach"
        untouched.write_text(old_bytes.stdout, encoding="utf-8")
        before_neighbour = untouched.read_bytes()

        stale = launcher.staged_tooling_out_of_date(
            "test", release_root=str(release), root=staging_root
        )
        check(stale == ["switchyard-display-attach"],
              f"exactly the out-of-date program is named: {stale}")

        staged_old = _module_from(stale_helper, "zorin_staged_helper")
        check(not hasattr(staged_old, "VIEWER_TARGET"),
              "and it is the numeric-only one the preserved tenant had")

        commands: list[str] = []

        def restaging_runner(args, **_kwargs):
            commands.append(" ".join(args))
            # What the recorded staging step does: install this release's bytes.
            for name in launcher.ROLE_STAGED_EXECUTABLES:
                source = release / "scripts" / name
                if source.is_file():
                    target = mine / name
                    target.write_bytes(source.read_bytes())
                    target.chmod(0o755)
            return _sp.CompletedProcess(args, 0)

        said: list[str] = []
        launcher.ensure_tenant_control_helper(
            "test",
            grant={"project": "test", "authorized_user": launcher.current_user_name()},
            release_root=str(release), root=staging_root,
            owner_uid=os.getuid(), runner=restaging_runner, print_func=said.append,
        )
        check(len(commands) == 1, f"one recorded privileged step: {commands}")
        check("switchyard-record-rollout" in commands[0],
              f"through the rollout journal: {commands[0][:120]}")
        check(any("older release" in line for line in said),
              f"and says why it is doing it: {said}")

        upgraded = _module_from(mine / "switchyard-display-attach", "upgraded_staged_helper")
        check(hasattr(upgraded, "VIEWER_TARGET"),
              "the tenant now runs this release's helper")
        check(upgraded.session_name("test", upgraded.VIEWER_TARGET) == "test-viewer",
              "which accepts the viewer target and resolves this tenant's session")

        # Idempotent: a second launch restages nothing.
        commands.clear()
        launcher.ensure_tenant_control_helper(
            "test",
            grant={"project": "test", "authorized_user": launcher.current_user_name()},
            release_root=str(release), root=staging_root,
            owner_uid=os.getuid(), runner=restaging_runner, print_func=lambda _l: None,
        )
        check(commands == [], f"a second launch runs nothing privileged: {commands}")

        # Cross-tenant isolation: the neighbour was never touched.
        check(untouched.read_bytes() == before_neighbour,
              "another tenant's staged helper is byte-for-byte what it was")


def test_a_hostile_staged_helper_is_refused_rather_than_restaged() -> None:
    """Out of date is repaired; the wrong shape is not."""
    with tempfile.TemporaryDirectory(prefix="syrd211-hostile-stale.") as tmp:
        root = Path(tmp)
        staging_root = root / "staging"
        mine = staging_root / "test"
        mine.mkdir(parents=True)
        helper = mine / "switchyard-tenant-control"
        helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        helper.chmod(0o777)

        def refuse(_args, **_kwargs):
            raise AssertionError("a hostile staged helper must not be restaged")

        try:
            launcher.ensure_tenant_control_helper(
                "test", root=staging_root, owner_uid=os.getuid(),
                runner=refuse, print_func=lambda _l: None,
            )
        except SystemExit as exc:
            check("refusing to run" in str(exc), f"it is refused: {str(exc)[:90]}")
            check("group- or world-writable" in str(exc),
                  f"for its shape, not its age: {str(exc)[:160]}")
        else:
            raise AssertionError("a world-writable staged helper must stop the launch")


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
