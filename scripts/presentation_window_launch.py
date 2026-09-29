"""Build and open the desktop presentation window for owner-side slots.

The owner and desktop bridge share the same attach argv and layout payload.
The tenant side either hands window facts back through its supplied descriptor
or checks, writes and launches a separate window in its own desktop session.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from scripts import team_launcher
from scripts.presentation_display_session import display_session_name
from scripts.presentation_runtime_assignments import _exact_tmux_target

#: What the caller asks the privileged helper for when the tenant's layout is
#: the single tiled viewer session rather than one display session per slot.
VIEWER_ATTACH_TARGET = "viewer"


def display_attach_args(
    config: team_launcher.ProjectConfig,
    slot: int,
    *,
    gui_user: str,
) -> list[str]:
    """What one presentation tab runs to display one slot.

    Within one account there is no boundary and the tab attaches directly. When
    the window belongs to a desktop user and the sessions belong to the tenant
    owner, it goes through the per-project display bridge: one root-owned
    program, one NOPASSWD grant, and a slot number as its only input. The
    alternative that shipped was `sudo -u <owner> tmux attach` in every tab,
    which asks a human for their password six times as a window opens, and the
    only way to avoid that without this bridge is a blanket sudo grant on the
    owner account (SYRD-65).
    """
    return display_attach_args_for(
        config.project, slot, owner=config.run_as_user or "", gui_user=gui_user
    )


def display_attach_args_for(
    project: str, slot: int | str, *, owner: str, gui_user: str
) -> list[str]:
    """The same answer from primitives, for a caller with no tenant config.

    The bridge caller builds its own layout and has never been able to read the
    tenant's configuration; what it knows is the project, its owner from the
    root-owned grant, and itself. One definition, so the tab the desktop half
    opens runs what the owner half would have given it (SYRD-90).
    """
    session = (
        team_launcher.viewer_session_for_project(project)
        if slot == VIEWER_ATTACH_TARGET
        else display_session_name(project, slot)
    )
    direct = ["env", "TMUX=", "tmux", "attach", "-t", _exact_tmux_target(session)]
    owner = (owner or "").strip()
    desktop = (gui_user or team_launcher.current_user_name()).strip()
    if not owner or owner == desktop:
        return direct
    return [
        os.environ.get("SWITCHYARD_SUDO_BIN", "sudo"),
        "-n",
        team_launcher.display_attach_helper_path(project),
        project,
        str(slot),
    ]


def presentation_layout_payload(
    project: str,
    *,
    slot_count: int,
    owner: str,
    gui_user: str,
    pane_program: Path,
    slot_titles: Sequence[str] = (),
    window_title: str = "",
    layout_mode: str = "",
) -> dict[str, Any]:
    """The Konsole layout for one project's presentation window.

    Rendered from primitives so the owner half and the bridge caller produce
    the same document, rather than one of them shipping the other a document to
    write (SYRD-90).

    The titles are passed to the pane wrapper, not written into the layout and
    hoped for: Konsole's layout parser has no key for a split's title, which is
    why `leaf["Title"]` alone left every header reading the fallback
    cwd-and-program text on the desktop handoff path (SYRD-122, SYRD-130).
    """
    titles = list(slot_titles)
    # The viewer layout is ONE tab: the owner built a single tiled tmux session
    # holding every role, so there are no per-slot display sessions to open
    # tabs on, and asking for slot 0..4 of something that does not exist is a
    # window of five errors (SYRD-211 live UAT).
    viewer = layout_mode == team_launcher.LAYOUT_MODE_VIEWER
    layout = team_launcher._new_project_layout_payload(1 if viewer else slot_count)
    for slot, leaf in enumerate(team_launcher._layout_leaves(layout)):
        title = titles[slot] if slot < len(titles) else ""
        leaf["Command"] = team_launcher.inert_pane_command(
            pane_program,
            display_attach_args_for(
                project,
                VIEWER_ATTACH_TARGET if viewer else slot,
                owner=owner,
                gui_user=gui_user,
            ),
            title=title,
            window_title=window_title,
        )
        # The desktop account's own directory, not this process's. Under a
        # privileged invocation `Path.home()` is root's, and the tab recorded
        # `WorkingDirectory=/root` -- a directory the desktop user cannot even
        # enter (SYRD-65).
        leaf["WorkingDirectory"] = team_launcher._gui_home(gui_user) if gui_user else str(Path.home())
        leaf["Title"] = title or f"{project} slot {slot}"
    return layout


def _hand_off_desktop_half(
    config: team_launcher.ProjectConfig,
    state: Mapping[str, Any],
    *,
    gui_user: str,
) -> bool:
    """Report what the bridge caller needs, when this process cannot do it.

    Only when the bridge asked for it: it opens the descriptor and names it in
    the environment it built. Nothing here decides where anything is written --
    this end writes to a pipe it was handed, and root decides what becomes of
    it (SYRD-90).
    """
    raw_fd = os.environ.get(team_launcher.PRESENTATION_HANDOFF_FD_ENV, "").strip()
    if not raw_fd.isdigit():
        return False
    payload = team_launcher.render_presentation_handoff(
        config.project,
        slot_count=int(state["slot_count"]),
        pane_program=team_launcher.pane_window_program(
            team_launcher.switchyard_pane_launcher_for(config)
        ),
        slot_titles=team_launcher.presentation_slot_titles(config, int(state["slot_count"])),
        window_title=team_launcher.project_window_title(config),
    )
    try:
        with os.fdopen(int(raw_fd), "w", encoding="utf-8", closefd=True) as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
    except OSError as exc:
        raise SystemExit(
            f"switchyard: could not hand {config.project}'s presentation window to "
            f"{gui_user or 'the desktop account'}: {exc}"
        ) from exc
    print(
        f"switchyard: {config.project}'s display slots are up; its window opens in "
        f"{gui_user or 'the desktop account'}'s own session."
    )
    return True


def _launch_separate(
    config: team_launcher.ProjectConfig,
    state: Mapping[str, Any],
    *,
    config_path: Path,
    output_path: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    process_launcher: Callable[..., Any] | None,
) -> None:
    # The account that owns the sessions does not necessarily own a screen.
    # Naming it as the GUI user is what sent a launch at the owner's own
    # runtime directory, where there is no compositor listening (SYRD-65).
    gui_user = team_launcher.presentation_gui_user(config)
    layout = presentation_layout_payload(
        config.project,
        slot_count=state["slot_count"],
        owner=config.run_as_user or "",
        gui_user=gui_user,
        pane_program=team_launcher.pane_window_program(
            team_launcher.switchyard_pane_launcher_for(config)
        ),
        slot_titles=team_launcher.presentation_slot_titles(config, state["slot_count"]),
    )
    # Under the control bridge this process is the project owner: it can
    # neither write into the desktop account's state directory nor reach that
    # person's compositor. It does the tenant half, reports the two facts the
    # desktop half needs, and the caller -- who owns both -- does the rest
    # (SYRD-90).
    if _hand_off_desktop_half(config, state, gui_user=gui_user):
        return
    # After the handoff: a caller that reached this process through the
    # tenant-control bridge was admitted by the very grant the tabs need, and
    # opens its own window. This is the path where this process opens it.
    #
    # Before anything opens: a window whose every tab will be refused is worse
    # than no window, because it looks like a presentation and answers nothing.
    # Live mefp opened four tabs that each exited "sudo: a password is
    # required" (SYRD-233). The workers are already up; only the window stops.
    # A tab whose program will not start is a shell, not a pane: Konsole falls
    # back to the profile's shell with no error, so four roles become four
    # ordinary prompts (SYRD-233 live UAT). Checked before the layout is
    # written, for the same reason the bridge handoff checks it.
    program_problem = team_launcher.presentation_pane_program_problem(config, runner=runner)
    if program_problem:
        raise SystemExit(
            f"switchyard: not opening {config.project}'s presentation window: {program_problem}. "
            f"Its roles are running. Run `sudo switchyard upgrade {config.project}` to stage this "
            f"release's pane program, then `switchyard {config.project}` to open the window; "
            f"`switchyard attach {config.project} <role>` reaches any role now."
        )
    if gui_user and gui_user != (config.run_as_user or team_launcher.current_user_name()):
        bridge = team_launcher.display_bridge_launch_problem(config, gui_user=gui_user)
        if bridge:
            raise SystemExit(
                f"switchyard: not opening {config.project}'s presentation window: {bridge}. "
                f"Its roles are running. Run `sudo switchyard upgrade {config.project}` to install "
                f"the display bridge for {gui_user}, then `switchyard {config.project}` to open "
                f"the window; `switchyard attach {config.project} <role>` reaches any role now."
            )
    output = output_path or team_launcher.desktop_presentation_layout_path(
        config, config_path=config_path, gui_user=gui_user
    )
    refusal = team_launcher.write_desktop_layout(
        output,
        layout,
        gui_user=gui_user or team_launcher.current_user_name(),
        runner=runner,
        # Named so a crossing write can be held to this project's own state
        # root under the desktop account and nowhere else (SYRD-233).
        project=config.project,
    )
    if refusal:
        # Named precisely, because the two ways to arrive here need different
        # things said. Through the control bridge this process is the project
        # owner: it has no way into the desktop account's state directory and no
        # way into that person's compositor either, so the answer is the
        # privileged route rather than anything this invocation can retry
        # (SYRD-90).
        through_bridge = bool(os.environ.get(team_launcher.TENANT_CONTROL_CALLER_ENV, "").strip())
        arrived = (
            " This invocation came through the tenant control bridge, which runs as "
            f"{team_launcher.current_user_name()}: only root can hand a layout to another "
            "account."
            if through_bridge
            else ""
        )
        raise SystemExit(
            f"switchyard: refusing to open {config.project}'s presentation window: {refusal}."
            f"{arrived} A terminal handed a layout it cannot read aborts before anything appears; "
            f"run `sudo switchyard {config.project}` from "
            f"{gui_user or 'the desktop account'}'s own session, which can."
        )
    result = team_launcher.launch_konsole_window(
        output,
        project=config.project,
        window_title=team_launcher.project_window_title(config),
        gui_user=gui_user or None,
        runner=runner,
        process_launcher=process_launcher,
    )
    if result != 0:
        raise SystemExit(f"switchyard: could not launch separate presentation window (exit {result})")
