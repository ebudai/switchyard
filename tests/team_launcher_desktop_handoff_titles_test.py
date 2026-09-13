#!/usr/bin/env python3
"""SYRD-130: the desktop half must name its splits too.

SYRD-122 gave every split a title and proved it against a real Konsole -- and
proved it through `materialize_layout`, which is not the path `switchyard
<slug>` takes. The production start crosses accounts: the owner half reports a
handoff, root validates and relays it, and the desktop half builds its own
layout from `presentation_layout_payload`. That builder called
`inert_pane_command` with no title and wrote `leaf["Title"]`, which Konsole
does not read. So the window the User opened still read `~ : switchyard-pane`
in every header, and the suite that was supposed to catch that passed.

The titles now cross the boundary with everything else, and are checked on both
sides of it and again by root in between. They are the only field here that a
terminal renders, so they are checked for what is in them: a control character
in a title is not a display problem, it is whatever else that terminal does
with the escape sequence around it.

These cases drive the desktop half itself, `complete_desktop_presentation`,
from a handoff file, and read the Command out of the layout it writes.
"""

from __future__ import annotations

import stat

from team_launcher_test_helpers import *

import pwd

#: The desktop half writes into its own caller's home and refuses to write into
#: anybody else's, which is the boundary SYRD-90 put there. So the caller in
#: this case is this account: the layout is the subject here, not the refusal.
CALLER = pwd.getpwuid(os.getuid()).pw_name

ROLES = ("inspector", "director", "audit", "main", "app", "ops")


def _config(tmp: Path, *, project: str = "porter", project_name: str = "Porter Team") -> tuple[object, Path]:
    leaf = lambda index: {"Command": "", "SessionRestoreId": index, "Title": "", "WorkingDirectory": ""}
    row = lambda first: {"Orientation": "Horizontal", "Widgets": [leaf(first + n) for n in range(3)]}
    layout = tmp / "layout.json"
    layout.write_text(json.dumps({"Orientation": "Vertical", "Widgets": [row(0), row(3)]}) + "\n", encoding="utf-8")
    for role in ROLES:
        (tmp / "work" / role).mkdir(parents=True, exist_ok=True)
    config_path = tmp / f"{project}.json"
    config_path.write_text(
        json.dumps(
            {
                "desktop_access": {"mode": "headless"},
                "project": project,
                "project_name": project_name,
                "layout": str(layout),
                "roles": [
                    {
                        "role": role, "slot": slot, "cli": ["claude"],
                        "target": f"{project}-{role}:0.0", "workdir": str(tmp / "work" / role),
                    }
                    for slot, role in enumerate(ROLES)
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return load_project_config(project, config_path), config_path


def _expected_titles(project_name: str = "Porter Team") -> list[str]:
    return [f"{project_name} -- {role[:1].upper()}{role[1:]}" for role in ROLES]


def test_the_owner_half_reports_what_each_slot_is_called() -> None:
    """The desktop account knows the slug and nothing about the roles.

    So if the owner half does not say, there is nowhere else for the titles to
    come from -- which is why the handoff carries them now.
    """
    with tempfile.TemporaryDirectory(prefix="pgu-handoff-titles.") as tmp:
        tmp_path = Path(tmp)
        config, _ = _config(tmp_path)
        titles = team_launcher.presentation_slot_titles(config, 6)
    assert titles == _expected_titles(), titles

    # A slot no role occupies is still the project's window.
    with tempfile.TemporaryDirectory(prefix="pgu-handoff-spare-slot.") as tmp:
        tmp_path = Path(tmp)
        config, _ = _config(tmp_path)
        spare = team_launcher.presentation_slot_titles(config, 8)
    assert spare[:6] == _expected_titles(), spare
    assert spare[6:] == ["Porter Team", "Porter Team"], spare


def test_a_title_that_could_reprogram_a_terminal_does_not_cross() -> None:
    """The one field here that a terminal renders, checked for its content.

    A title is emitted inside an escape sequence in somebody else's session, so
    a control character in one is not a display problem.
    """
    assert team_launcher.presentation_title_problem("Switchyard -- Director") == ""
    for rejected in (
        "Switchyard\033]0;something else\007",
        "Switchyard\nDirector",
        "Switchyard\x00Director",
        "Switchyard\x9bDirector",
        "",
        "   ",
        "x" * (team_launcher.PRESENTATION_TITLE_MAX_LENGTH + 1),
        42,
        None,
    ):
        assert team_launcher.presentation_title_problem(rejected), repr(rejected)


def test_the_handoff_is_refused_when_the_titles_are_not_checkable() -> None:
    """Checked again on arrival: having been through another account is not trust."""
    with tempfile.TemporaryDirectory(prefix="pgu-handoff-validate.") as tmp:
        tmp_path = Path(tmp)
        program = tmp_path / "switchyard-pane-window"
        program.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        program.chmod(0o755)

        def accept(_path, owner_uid=0, runner=None):
            return []

        original = team_launcher.untrusted_root_executable_reasons
        try:
            team_launcher.untrusted_root_executable_reasons = accept
            good = team_launcher.render_presentation_handoff(
                "porter", slot_count=2, pane_program=program,
                slot_titles=["Porter Team -- Inspector", "Porter Team -- Director"],
            )
            checked, problem = team_launcher.validated_presentation_handoff(good, project="porter")
            assert not problem, problem
            assert checked["slot_titles"] == ["Porter Team -- Inspector", "Porter Team -- Director"]

            for label, mutated in (
                ("missing", {key: value for key, value in good.items() if key != "slot_titles"}),
                ("not a list", {**good, "slot_titles": "Porter Team -- Inspector"}),
                ("wrong length", {**good, "slot_titles": ["Porter Team -- Inspector"]}),
                ("control character", {**good, "slot_titles": ["Porter Team -- Inspector", "a\033]0;b\007"]}),
                ("empty", {**good, "slot_titles": ["Porter Team -- Inspector", "  "]}),
            ):
                _, refused = team_launcher.validated_presentation_handoff(mutated, project="porter")
                assert refused, label
        finally:
            team_launcher.untrusted_root_executable_reasons = original


def test_the_owner_half_actually_puts_them_on_the_wire() -> None:
    """The owner side, through the pipe the bridge hands it.

    Everything else here checks what happens once the titles have crossed. This
    is the half that has to send them, and a mutation that sends an empty list
    survives every other case in this file: the desktop side would then build a
    layout with no titles and be perfectly consistent about it, which is the
    original regression exactly.
    """
    from scripts import presentation_controller

    with tempfile.TemporaryDirectory(prefix="pgu-owner-half.") as tmp:
        tmp_path = Path(tmp)
        config, _ = _config(tmp_path)
        read_fd, write_fd = os.pipe()
        original_env = os.environ.get(team_launcher.PRESENTATION_HANDOFF_FD_ENV)
        original_program = team_launcher.switchyard_pane_launcher_for
        try:
            os.environ[team_launcher.PRESENTATION_HANDOFF_FD_ENV] = str(write_fd)
            team_launcher.switchyard_pane_launcher_for = lambda _config: tmp_path / "team-launcher"
            handed = presentation_controller._hand_off_desktop_half(
                config, {"slot_count": 6}, gui_user="eric",
            )
            assert handed is True
            with os.fdopen(read_fd, "r", encoding="utf-8") as handle:
                payload = json.loads(handle.readline())
        finally:
            if original_env is None:
                os.environ.pop(team_launcher.PRESENTATION_HANDOFF_FD_ENV, None)
            else:
                os.environ[team_launcher.PRESENTATION_HANDOFF_FD_ENV] = original_env
            team_launcher.switchyard_pane_launcher_for = original_program

    assert payload["schema"] == team_launcher.PRESENTATION_HANDOFF_SCHEMA, payload
    assert payload["slot_count"] == 6, payload
    assert payload["slot_titles"] == _expected_titles(), payload


def test_the_desktop_half_writes_a_layout_that_names_every_role() -> None:
    """The path `switchyard <slug>` actually takes, driven from a handoff file.

    Not `materialize_layout`: that is the owner-side builder, it was already
    right, and it is why this regression reached a User through a suite that
    was passing.
    """
    with tempfile.TemporaryDirectory(prefix="pgu-desktop-half.") as tmp:
        tmp_path = Path(tmp)
        home = tmp_path / "home" / CALLER
        home.mkdir(parents=True)
        program = tmp_path / "switchyard-pane-window"
        program.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        program.chmod(0o755)

        handoff = {
            "schema": team_launcher.PRESENTATION_HANDOFF_SCHEMA,
            "project": "porter",
            "slot_count": 6,
            "pane_program": str(program),
            "slot_titles": _expected_titles(),
        }
        handoff_path = tmp_path / "home" / CALLER / ".local" / "state" / "switchyard" / "projects" / "porter" / "porter-presentation-handoff.json"
        handoff_path.parent.mkdir(parents=True)
        handoff_path.write_text(json.dumps(handoff) + "\n", encoding="utf-8")

        launched: list[list[str]] = []
        printed: list[str] = []
        originals = (
            team_launcher._gui_home,
            team_launcher.untrusted_root_executable_reasons,
            team_launcher._tenant_control_grant,
            team_launcher.launch_konsole_window,
            team_launcher.write_desktop_layout,
        )
        try:
            team_launcher._gui_home = lambda user: str(home)
            team_launcher.untrusted_root_executable_reasons = lambda *a, **k: []
            team_launcher._tenant_control_grant = lambda project: {"owner": "porter-agent"}
            team_launcher.launch_konsole_window = lambda *a, **k: launched.append(list(a)) or 0
            status = team_launcher.complete_desktop_presentation(
                "porter", caller=CALLER, print_func=printed.append,
            )
        finally:
            (
                team_launcher._gui_home,
                team_launcher.untrusted_root_executable_reasons,
                team_launcher._tenant_control_grant,
                team_launcher.launch_konsole_window,
                team_launcher.write_desktop_layout,
            ) = originals

        assert status == 0, (status, printed)
        written = home / ".local" / "state" / "switchyard" / "projects" / "porter" / "porter-presentation-layout.json"
        assert written.is_file(), printed
        leaves = team_launcher._layout_leaves(json.loads(written.read_text(encoding="utf-8")))

    assert len(leaves) == 6, leaves
    for slot, expected in enumerate(_expected_titles()):
        argv = shlex.split(leaves[slot]["Command"])
        assert Path(argv[0]).name == team_launcher.PANE_WINDOW_NAME, argv
        # The regression, stated as the thing that must not come back: the
        # wrapper is told the title before it is told what to run.
        assert argv[1:3] == ["--title", expected], (slot, argv)
        assert leaves[slot]["Title"] == expected, leaves[slot]


def test_the_layout_builder_still_names_slots_with_no_role() -> None:
    """A slot count larger than the titles given is not a crash or a blank."""
    from scripts import presentation_controller

    layout = presentation_controller.presentation_layout_payload(
        "porter", slot_count=3, owner="porter-agent", gui_user="eric",
        pane_program=Path("/usr/local/lib/switchyard/porter/switchyard-pane-window"),
        slot_titles=["Porter Team -- Inspector"],
    )
    leaves = team_launcher._layout_leaves(layout)
    first = shlex.split(leaves[0]["Command"])
    assert first[1:3] == ["--title", "Porter Team -- Inspector"], first
    # No title for this slot, so the wrapper is not told one and behaves as it
    # always did rather than being handed an empty string to print.
    rest = shlex.split(leaves[1]["Command"])
    assert "--title" not in rest, rest
    assert leaves[1]["Title"] == "porter slot 1", leaves[1]


def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_desktop_handoff_titles_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
