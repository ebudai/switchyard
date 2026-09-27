"""The desktop account's half of the presentation hand-off: checking what crossed, choosing a terminal, and opening the window.

- **What crosses:** `validated_presentation_handoff` checks the hand-off the
  bridge leaves at `presentation_handoff_path` against
  `PRESENTATION_HANDOFF_SCHEMA`: the layout, slot count and titles
  (`presentation_title_problem`), and a pane program pinned to root.
  `presentation_pane_program_problem` says why a tenant's window cannot be
  opened at all.
- **Choosing a terminal:** `available_presentation_terminal` picks the first
  of `PRESENTATION_TERMINALS` the desktop has; `terminal_launch_args` builds
  its argv, and `launch_presentation_terminal` starts it and reports only what
  is known (`TERMINAL_STAYS`, `TERMINAL_RETURNS`).
- **Opening the window:** `complete_desktop_presentation` reads and consumes
  the hand-off, checks it again, and opens the window as this account;
  `_tenant_has_desktop_access` decides whether a missing hand-off is a fault.

The grant, registry, desktop state directory, Konsole launch and pane program
stay in `scripts/team_launcher.py`, and writing the layout is in
`scripts/desktop_layout_writer.py`; all are read through the launcher when a
function runs. The suites patch the entry points on the launcher, and their
callers -- here, in the launcher and in the presentation controller -- reach
them there.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-317). `team_launcher`
imports this module at its top and still exports every name callers read there.
This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from scripts.layout_modes import LAYOUT_MODE_SEPARATE, LAYOUT_MODE_VIEWER



#: Terminals that can be asked to run one command, and how each one takes it.
#:
#: They do not agree: some want `-e`, some `--`, some `-x`, and kitty takes the
#: command with no flag at all. The title flag differs too. Ordered by how
#: likely a desktop is to have them, with Konsole first because a KDE host does.
#:
#: This exists because the presentation window was always Konsole, and the
#: viewer layout -- the one a NON-KDE desktop selects -- then died with
#: `env: 'konsole': No such file or directory` on a host that had four other
#: terminals installed (SYRD-211 live UAT).
#: How a terminal behaves once it has been started, which is not the same
#: question as how to hand it a command.
#:
#: `stays` -- the process lives as long as the window, so its exit is evidence.
#: `returns` -- it is a client: it hands the request to a session server and
#: exits 0 straight away, and its exit says nothing about the window at all.
TERMINAL_STAYS = "stays"


TERMINAL_RETURNS = "returns"


#: Terminals that can be asked to run one command, how each one takes it, and
#: what its exit means.
#:
#: They do not agree on any of it: some want `-e`, some `--`, some `-x`, and
#: kitty takes the command with no flag. The title flag differs too. And
#: gnome-terminal and kgx are clients -- they return 0 immediately once the
#: session server has the request -- so reading that as "no window opened" both
#: reported a failure and returned success (SYRD-211 DAT rejection).
#:
#: Ordered by how likely a desktop is to have them, Konsole first because a KDE
#: host does. This exists because the presentation window was always Konsole,
#: and the viewer layout -- the one a NON-KDE desktop selects -- then died with
#: `env: 'konsole': No such file or directory` on a host with four other
#: terminals installed (SYRD-211 live UAT).
PRESENTATION_TERMINALS: tuple[tuple[str, str, str, str], ...] = (
    ("konsole", "-e", "--qwindowtitle", TERMINAL_STAYS),
    ("gnome-terminal", "--", "--title", TERMINAL_RETURNS),
    ("kgx", "--", "--title", TERMINAL_RETURNS),
    ("xfce4-terminal", "-x", "--title", TERMINAL_RETURNS),
    ("mate-terminal", "-x", "--title", TERMINAL_RETURNS),
    ("tilix", "-e", "--title", TERMINAL_RETURNS),
    ("alacritty", "-e", "--title", TERMINAL_STAYS),
    ("kitty", "", "--title", TERMINAL_STAYS),
    ("xterm", "-e", "-T", TERMINAL_STAYS),
)


def available_presentation_terminal(
    *, which: Callable[..., str | None] = shutil.which
) -> tuple[str, str, str, str] | None:
    """The first terminal this desktop actually has, or nothing."""
    for entry in PRESENTATION_TERMINALS:
        if which(entry[0]):
            return entry
    return None


def missing_terminal_refusal(project: str) -> str:
    """Said instead of exiting 127 from inside a terminal that is not there."""
    return (
        f"switchyard: cannot open {project}'s presentation window: none of "
        f"{', '.join(entry[0] for entry in PRESENTATION_TERMINALS)} is installed. "
        "Install one of them, or run this from a desktop that has one; the panes are "
        "running either way and nothing has been changed"
    )


def terminal_launch_args(
    command: Sequence[str],
    *,
    terminal: tuple[str, str, str, str],
    gui_user: str | None = None,
    window_title: str = "",
) -> list[str]:
    """One terminal, opened on one command, in the desktop's own session."""
    from scripts import team_launcher as launcher

    prefix, refusal = launcher._gui_launch_prefix(gui_user=gui_user)
    if refusal:
        return launcher._refusal_command(refusal)
    name, command_flag, title_flag, _lifecycle = terminal
    args = [*prefix, launcher.gui_program_path(name)]
    title = window_title.strip()
    if title and title_flag:
        args.extend([title_flag, title])
    if command_flag:
        args.append(command_flag)
    args.extend(str(part) for part in command)
    return args


def launch_presentation_terminal(
    args: Sequence[str],
    *,
    project: str,
    terminal: tuple[str, str, str, str],
    process_launcher: Callable[..., Any] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> int:
    """Start one terminal on one command, and report what is actually known.

    Three outcomes, because an exit means different things for different
    terminals:

    * still running -- the window is up, for a terminal whose process lives as
      long as it;
    * exited 0, from a CLIENT -- the session server has the request. That is an
      acknowledgement, not a failure. Reading it as one said "no window opened"
      and returned success in the same breath, which is both wrong and
      self-contradictory (SYRD-211 DAT rejection);
    * exited nonzero, or exited 0 from a terminal that should have stayed --
      the window did not open, and the status and log say so.

    Nothing here claims a window failed to open without evidence, and nothing
    returns success after saying something failed.
    """
    from scripts import team_launcher as launcher

    name, _command_flag, _title_flag, lifecycle = terminal
    if list(args[:2]) == ["sh", "-lc"]:
        return int(runner(list(args)).returncode)
    launch_process = process_launcher or subprocess.Popen
    try:
        with tempfile.NamedTemporaryFile(
            mode="ab", prefix=f"{project}-presentation.", suffix=".log", delete=False
        ) as handle:
            log_path = Path(handle.name)
            launcher._make_konsole_log_readable(log_path)
            proc = launch_process(
                list(args),
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=handle,
                start_new_session=True,
            )
    except OSError as exc:
        print_func(f"switchyard: could not start {name} for {project}: {exc}")
        return 1
    time.sleep(0.2)
    returncode = proc.poll()
    if returncode is None:
        print_func(
            f"switchyard: opened {project}'s presentation in {name}; it shows every "
            "role in one window"
        )
        return 0
    if returncode == 0 and lifecycle == TERMINAL_RETURNS:
        print_func(
            f"switchyard: {name} accepted {project}'s presentation and returned, as it "
            "does; the window opens in the desktop's own session"
        )
        return 0
    print_func(
        f"switchyard: {name} exited immediately (status {returncode}) without opening "
        f"{project}'s window; its output is in {log_path}"
    )
    # Never success after saying that. A terminal that should have stayed and
    # exited 0 is still a window that did not open.
    return int(returncode) or 1


#: v2 carries the slot titles. The shape changed, so the name changed with it:
#: a half that predates the titles would otherwise accept a payload it cannot
#: honour and open a window with the fallback titles the User reported
#: (SYRD-130). Both halves ship out of one release tree.
PRESENTATION_HANDOFF_SCHEMA = "switchyard.presentation-handoff.v2"


#: A title crosses from the tenant into the desktop account's terminal and is
#: rendered there through an escape sequence. A control character in one is not
#: a display problem, it is whatever else that terminal does with it, so titles
#: are checked for content and not only for type. Bounded for the same reason a
#: header is bounded.
PRESENTATION_TITLE_MAX_LENGTH = 120


PRESENTATION_TITLE_REJECTED = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def presentation_handoff_path(project: str, user: str) -> Path:
    """Where the bridge leaves the desktop half's inputs for its caller."""
    from scripts import team_launcher as launcher

    return launcher.desktop_state_dir(project, user) / f"{project}-presentation-handoff.json"


def presentation_title_problem(value: Any) -> str:
    """Why one slot title may not cross the boundary, or "" if it may."""
    if not isinstance(value, str):
        return "a presentation slot title is not a string"
    if not value.strip():
        return "a presentation slot title is empty"
    if len(value) > PRESENTATION_TITLE_MAX_LENGTH:
        return f"a presentation slot title is longer than {PRESENTATION_TITLE_MAX_LENGTH} characters"
    if PRESENTATION_TITLE_REJECTED.search(value):
        return "a presentation slot title carries a control character"
    return ""


def presentation_pane_program_problem(
    config: "ProjectConfig",
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> str:
    """Why this tenant's window cannot be opened at all, or "".

    Every tab of the window runs one program: the `switchyard-pane-window` that
    ships beside this tenant's configured pane launcher. Konsole runs a tab's
    `Command` directly, and when it cannot start one it falls back to the
    profile's shell -- silently, with no error anywhere. Live mefp opened four
    tabs and every one of them was an ordinary shell instead of the role's CLI
    (SYRD-233 live UAT).

    The bridge handoff has always checked this program before letting the
    desktop half run it; the path root takes to open the window itself did not.
    The same check, for the same reason: it is a program another account is
    about to run in every tab.
    """
    from scripts import team_launcher as launcher

    program = launcher.pane_window_program(launcher.switchyard_pane_launcher_for(config))
    if not program.exists() or not os.access(program, os.X_OK):
        return (
            f"the program each tab runs, {program}, is not there. This release's pane window "
            "ships beside the tenant's pane launcher, and Konsole falls back to a plain shell "
            "when it cannot start a tab's command"
        )
    if config.pane_launcher is None:
        # A checkout running its own panes: the program is this tree's, owned by
        # whoever cloned it. `_verify_pane_launcher_path` draws the line in the
        # same place -- a configured launcher is a provisioned tenant's, and a
        # provisioned tenant's is root's.
        return ""
    reasons = launcher.untrusted_root_executable_reasons(program, owner_uid=0, runner=runner)
    if reasons:
        return f"the program each tab runs, {program}, is not pinned to root: {reasons[0]}"
    return ""


def validated_presentation_handoff(
    payload: Any, *, project: str, runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run
) -> tuple[dict[str, Any], str]:
    """The handoff, or why it is not one. Checked by both sides independently.

    The bridge checks it as root before it writes it anywhere, and the caller
    checks it again before it acts on it: a value that has been through another
    account is not trusted for having arrived (SYRD-90).
    """
    from scripts import team_launcher as launcher

    if not isinstance(payload, dict):
        return {}, "the presentation handoff is not an object"
    if str(payload.get("schema") or "") != PRESENTATION_HANDOFF_SCHEMA:
        return {}, "the presentation handoff does not carry this schema"
    # Absent means the shape that predates this field, so an older owner half
    # is still understood rather than refused.
    layout = str(payload.get("layout") or LAYOUT_MODE_SEPARATE)
    if layout not in {LAYOUT_MODE_SEPARATE, LAYOUT_MODE_VIEWER}:
        return {}, f"the presentation handoff names an unknown layout {layout!r}"
    if str(payload.get("project") or "") != project:
        return {}, f"the presentation handoff names {payload.get('project')!r} rather than {project}"
    slot_count = payload.get("slot_count")
    if not isinstance(slot_count, int) or isinstance(slot_count, bool):
        return {}, "the presentation handoff slot count is not an integer"
    if not 1 <= slot_count <= launcher.MAX_VISIBLE_PANES_PER_WINDOW:
        return {}, f"the presentation handoff slot count {slot_count} is out of range"
    raw_program = str(payload.get("pane_program") or "")
    program = Path(raw_program)
    if not raw_program or not program.is_absolute():
        return {}, "the presentation handoff pane program is not an absolute path"
    # The one thing in here that names something to execute, so the whole path
    # to it has to be root's: this is a program the desktop account will run in
    # every tab of its own window (SYRD-62, SYRD-90).
    reasons = launcher.untrusted_root_executable_reasons(program, owner_uid=0, runner=runner)
    if reasons:
        return {}, f"the presentation handoff pane program is not pinned to root: {reasons[0]}"
    titles = payload.get("slot_titles")
    if not isinstance(titles, list):
        return {}, "the presentation handoff slot titles are not a list"
    if len(titles) != slot_count:
        return {}, f"the presentation handoff carries {len(titles)} slot titles for {slot_count} slots"
    for title in titles:
        problem = presentation_title_problem(title)
        if problem:
            return {}, f"the presentation handoff is refused: {problem}"
    window_title = payload.get("window_title", "")
    if not isinstance(window_title, str):
        return {}, "the presentation handoff window title is not a string"
    if window_title:
        problem = presentation_title_problem(window_title)
        if problem:
            return {}, f"the presentation handoff is refused: {problem}"
    return {
        "layout": layout,
        "schema": PRESENTATION_HANDOFF_SCHEMA,
        "project": project,
        "slot_count": slot_count,
        "pane_program": raw_program,
        "slot_titles": [str(title) for title in titles],
        "window_title": window_title,
    }, ""


def _tenant_has_desktop_access(project: str, *, caller: str = "") -> bool | None:
    """Does this tenant have a window -- or can this account not tell?

    Three answers, not two. The obvious source is the tenant's configuration,
    and most of the time this account cannot read it: it lives under the
    owner's home, and not being able to look in there is the boundary working
    rather than a fault. Measured on this host, exactly one of four registered
    tenants was readable from this account.

    So the caller's own desktop state directory for the project is consulted
    too -- that is this account's, it is where this project's window has been
    staged before, and its existence means this caller has had a window for
    this tenant. When neither says anything, the answer is None: unknown, and
    an unknown must not be reported as a fault (SYRD-211 live UAT).
    """
    from scripts import team_launcher as launcher

    try:
        entry = launcher._usable_switchyard_entry_for_project(project, config_dir=None, registry_dir=None)[0]
    except Exception:
        entry = None
    if entry is not None:
        try:
            raw = json.loads(Path(entry.config_path).read_text(encoding="utf-8"))
        except (OSError, ValueError, AttributeError, TypeError):
            raw = None
        if isinstance(raw, dict):
            return isinstance(raw.get("desktop_access"), dict)
    if caller:
        try:
            if launcher.desktop_state_dir(project, caller).is_dir():
                return True
        except OSError:
            pass
    return None


def complete_desktop_presentation(
    project: str,
    *,
    caller: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    process_launcher: Callable[..., Any] | None = None,
    print_func: Callable[[str], None] = print,
) -> int:
    """Open this project's window here, from what the bridge handed back.

    The owner account has the sessions and no screen; this account has the
    screen and may not read the tenant's configuration. So the bridge reports
    two checkable facts and this process does the rest in its own home and its
    own session: it stages the layout where it already has permission to, and
    starts the terminal as itself, with no privilege to drop and no password to
    ask for. Absent handoff means there was nothing to open (SYRD-90).
    """
    from scripts import team_launcher as launcher

    from scripts import presentation_controller

    handoff_path = presentation_handoff_path(project, caller)
    try:
        raw = handoff_path.read_text(encoding="utf-8")
    except OSError:
        # "Nothing to open" is only true for a tenant that has no window. For
        # one with a desktop policy this means the owner half never handed its
        # window back, and returning zero here reports a launch that opened
        # nothing as a success -- which is what live Zorin UAT was given: every
        # worker attached, then the shell back, no window and no complaint
        # (SYRD-211 live UAT).
        if launcher._tenant_has_desktop_access(project, caller=caller) is True:
            print_func(
                f"switchyard: {project}'s panes are running, but no presentation window was "
                "handed back to this session, so none was opened. The tenant is up; its window "
                f"is not. Run `switchyard {project}` again, and report this if it repeats"
            )
            return 1
        return 0
    try:
        handoff_path.unlink()
    except OSError:
        pass
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = None
    handoff, problem = validated_presentation_handoff(payload, project=project, runner=runner)
    if problem:
        # Checked again here. It has been through another account, and arriving
        # is not the same as being trustworthy.
        print_func(f"switchyard: refusing {project}'s presentation handoff: {problem}")
        return 1
    grant = launcher._tenant_control_grant(project)
    owner = str(grant.get("owner") or "").strip()
    if not owner:
        print_func(f"switchyard: {project} has no recorded owner; not opening a window")
        return 1
    terminal = launcher.available_presentation_terminal()
    if terminal is None:
        # Said here rather than discovered as exit 127 from inside `env`. Live
        # Zorin got `env: 'konsole': No such file or directory` and a window
        # that never appeared (SYRD-211 live UAT).
        print_func(missing_terminal_refusal(project))
        return 1
    if handoff.get("layout") == LAYOUT_MODE_VIEWER:
        # One tiled session, so one terminal running one command: there is no
        # per-tab layout to write, and therefore nothing here that needs
        # Konsole in particular.
        attach = presentation_controller.display_attach_args_for(
            project,
            presentation_controller.VIEWER_ATTACH_TARGET,
            owner=owner,
            gui_user=caller,
        )
        title = handoff["window_title"] or _registered_project_name(project) or project
        args = terminal_launch_args(
            attach, terminal=terminal, gui_user=caller, window_title=title
        )
        return launch_presentation_terminal(
            args,
            project=project,
            terminal=terminal,
            process_launcher=process_launcher,
            runner=runner,
            print_func=print_func,
        )
    layout = presentation_controller.presentation_layout_payload(
        project,
        slot_count=handoff["slot_count"],
        owner=owner,
        gui_user=caller,
        pane_program=Path(handoff["pane_program"]),
        slot_titles=handoff["slot_titles"],
        window_title=handoff["window_title"] or _registered_project_name(project) or project,
        layout_mode=handoff.get("layout", LAYOUT_MODE_SEPARATE),
    )
    output = launcher.desktop_state_dir(project, caller) / f"{project}-presentation-layout.json"
    refusal = launcher.write_desktop_layout(output, layout, gui_user=caller, runner=runner)
    if refusal:
        print_func(f"switchyard: cannot open {project}'s presentation window: {refusal}")
        return 1
    return launcher.launch_konsole_window(
        output,
        project=project,
        window_title=handoff["window_title"] or _registered_project_name(project) or project,
        gui_user=None,
        runner=runner,
        process_launcher=process_launcher,
    )


def _registered_project_name(project: str) -> str:
    """This project's display name, from the root-owned registry it is listed in."""
    from scripts import team_launcher as launcher

    try:
        entry = launcher._resolve_switchyard_project(project)
    except SystemExit:
        return ""
    return (getattr(entry, "name", "") or "").strip()
