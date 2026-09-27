"""Finding a project's presentation windows among running processes, and closing them.

- **Finding them:** `presentation_window_processes` names the Konsole windows
  open on this project's presentation layout, by pid, matching the layout path
  as a whole argument (`PresentationWindowProcess`);
  `desktop_presentation_windows` does the same from the desktop account's side,
  from the project and the caller alone.
- **Closing them:** `close_presentation_window` and
  `close_desktop_presentation` terminate exactly those pids and report what
  they could not close.
- **Root windows:** `unsafe_root_presentation_windows` and
  `unsafe_presentation_report` are the SYRD-43 check that a window is running
  as root (`UnsafePresentationWindow`).

`presentation_window_processes` is defined TWICE here, as it was in the
launcher: the SYRD-43 scan (`PRESENTATION_PROGRAM_NAMES`, `_proc_cmdline`,
`presentation_layout_markers`) and, after it, the SYRD-193 scan. The later
definition is the one the name binds to and the one every caller reaches;
`unsafe_root_presentation_windows` still calls it with the earlier one's
expectations (SYRD-321 records this). Both copies moved in their original
order so that nothing about which one binds changed.

The process table root, the layout paths, the window title and the command
name stay in `scripts/team_launcher.py` and are read there when a function
runs. The suites patch `close_desktop_presentation` on the launcher, and every
caller reaches the entry points there.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-321). `team_launcher`
imports this module at its top and still exports every name callers read there.
This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import os
import pwd
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Sequence

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig



@dataclass(frozen=True)
class UnsafePresentationWindow:
    """A presentation process running as root, which no role pane may do."""

    pid: int
    uid: int
    user: str
    command: str


PRESENTATION_PROGRAM_NAMES = ("konsole", "yakuake", "xterm", "gnome-terminal", "alacritty", "kitty")


def _proc_cmdline(proc_root: Path, pid: str) -> list[str]:
    try:
        raw = (proc_root / pid / "cmdline").read_bytes()
    except OSError:
        return []
    return [part for part in raw.decode("utf-8", "replace").split("\0") if part]


def presentation_layout_markers(config: ProjectConfig, *, config_path: Path | None = None) -> list[str]:
    """Argument fragments that identify a window as this project's presentation."""
    from scripts import team_launcher as launcher

    markers = [
        f"{config.project}-konsole-layout.json",
        f"{config.project}-presentation-layout.json",
        launcher.project_window_title(config),
    ]
    if config.layout is not None:
        markers.append(str(config.layout))
    if config_path is not None:
        markers.append(str(config_path))
    return [marker for marker in markers if marker]


def unsafe_root_presentation_windows(
    config: ProjectConfig,
    *,
    config_path: Path | None = None,
    proc_root: Path | None = None,
) -> list[UnsafePresentationWindow]:
    """Terminal windows showing this project's panes that are running as root.

    A root terminal is a root shell behind every tab: when the tmux client in a
    tab detaches, the tab falls back to that shell, and anything pasted into it
    runs as root. Such a window can predate the repair, and the tenant cannot
    signal it, so start, status and upgrade have to say it is there rather than
    report the project safely attached (SYRD-43).
    """
    from scripts import team_launcher as launcher

    return [
        window
        for window in launcher.presentation_window_processes(
            config, config_path=config_path, proc_root=proc_root
        )
        if window.uid == 0
    ]


def presentation_window_processes(
    config: ProjectConfig,
    *,
    config_path: Path | None = None,
    proc_root: Path | None = None,
) -> list[UnsafePresentationWindow]:
    """Terminal windows showing this project's panes, whoever owns them.

    A window is a process, and it is the only evidence of one that does not go
    through tmux. Attached clients say a session has a terminal; they do not
    say a terminal is on a screen, and a terminal that aborted on a layout file
    it could not read leaves clients behind for a moment and no window at all
    (SYRD-65). The root subset of this is what `status` refuses to call
    attached (SYRD-43).
    """
    from scripts import team_launcher as launcher

    proc_root = proc_root or launcher.PROC_ROOT
    markers = presentation_layout_markers(config, config_path=config_path)
    found: list[UnsafePresentationWindow] = []
    try:
        entries = sorted(path.name for path in proc_root.iterdir() if path.name.isdigit())
    except OSError:
        return []
    for pid in entries:
        uid = launcher._proc_effective_uid(proc_root, pid)
        if uid is None:
            continue
        argv = _proc_cmdline(proc_root, pid)
        if not argv:
            continue
        # What the process IS, not what its arguments mention. A privileged
        # launch reaches the desktop account through `sudo -u <user> -- env -i
        # ... konsole ...`, and sudo stays as the parent: its argv names konsole
        # and the layout, but it is not a terminal and there is no shell behind
        # any tab of it. Counting it reported a root window that does not exist,
        # while the terminal it started -- the real one, running as the desktop
        # account -- is scanned here on its own merits (SYRD-90).
        if launcher._command_name(argv[0]) not in PRESENTATION_PROGRAM_NAMES:
            continue
        if not any(marker in part for part in argv for marker in markers):
            continue
        try:
            user = pwd.getpwuid(uid).pw_name
        except KeyError:
            user = str(uid)
        found.append(
            UnsafePresentationWindow(pid=int(pid), uid=uid, user=user, command=" ".join(argv))
        )
    return found


def unsafe_presentation_report(
    config: ProjectConfig, windows: Sequence[UnsafePresentationWindow]
) -> str:
    """What is unsafe, and the one command that replaces it without touching workers."""
    lines = [
        f"switchyard: {config.project} is NOT safely attached: "
        f"{len(windows)} presentation window(s) are running as root, so every tab in them "
        "falls back to a root shell when its pane detaches."
    ]
    for window in windows:
        lines.append(f"  pid {window.pid} ({window.user}): {window.command}")
    lines.append(
        f"An operator must run `sudo switchyard replace-window {config.project}`, which replaces "
        "only those windows and leaves every worker session running."
    )
    return "\n".join(lines)


@dataclass(frozen=True)
class PresentationWindowProcess:
    """One Konsole window, and the project whose layout it was opened on."""

    pid: int
    owner_uid: int
    layout: str


def presentation_window_processes(
    config: ProjectConfig,
    *,
    config_path: Path,
    gui_user: str = "",
    proc_root: Path | None = None,
) -> list[PresentationWindowProcess]:
    """The presentation windows open on THIS project's layout, by pid.

    Identified by the absolute layout path in the window's own argv, compared as
    a whole argument rather than searched for as text. A substring match would
    make `atlas` close `atlas-staging`'s window, and the project slug is a
    prefix of every sibling tenant's paths; the window title is worse still,
    because it is the project's display name and two tenants may share one.

    Konsole is started with these arguments and does not exec away from them, so
    unlike a role pane's environment wrapper (SYRD-169) the marker is still
    there when this is asked. The pid is read from /proc rather than from
    anything recorded at launch: a window that was reopened by hand, or survived
    a crash of whatever started it, is still this project's window (SYRD-193).
    """
    from scripts import team_launcher as launcher

    root = Path(proc_root) if proc_root is not None else Path("/proc")
    candidates = {
        str(launcher.desktop_presentation_layout_path(config, config_path=config_path, gui_user=gui_user or "")),
        str(launcher.default_layout_output_path(config, config_path=config_path).with_name(
            f"{config.project}-presentation-layout.json"
        )),
    }
    found: list[PresentationWindowProcess] = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().decode("utf-8", "replace").split("\0")
            owner_uid = entry.stat().st_uid
        except OSError:
            continue
        if not argv or "konsole" not in Path(argv[0] or "").name:
            continue
        # Either spelling of the same argument, and neither by substring: a
        # window opened by hand may use `--layout=PATH` where the launcher uses
        # two arguments, while `PATH.backup` is a different file and a different
        # window. Matching whole values keeps both facts true.
        match = next(
            (
                value
                for value in argv
                if value in candidates or value.partition("=")[2] in candidates
            ),
            "",
        )
        if not match:
            continue
        found.append(PresentationWindowProcess(int(entry.name), owner_uid, match))
    return found


def close_presentation_window(
    config: ProjectConfig,
    *,
    config_path: Path,
    gui_user: str = "",
    proc_root: Path | None = None,
    signaller: Callable[[int, int], None] = os.kill,
    print_func: Callable[[str], None] = print,
) -> list[str]:
    """Close this project's presentation window, and only this project's.

    A terminate, not a kill: Konsole closes its window and its own children on
    SIGTERM, and the sessions inside it are tmux clients whose servers this
    command stops separately. Returns what it could not close.
    """
    from scripts import team_launcher as launcher

    problems: list[str] = []
    windows = launcher.presentation_window_processes(
        config, config_path=config_path, gui_user=gui_user, proc_root=proc_root
    )
    if not windows:
        print_func(f"already closed presentation window: {config.project}")
        return problems
    for window in windows:
        try:
            signaller(window.pid, signal.SIGTERM)
        except ProcessLookupError:
            # It closed between the scan and the signal, which is the outcome
            # this was asking for.
            continue
        except PermissionError:
            problems.append(
                f"the {config.project} presentation window (pid {window.pid}) belongs to uid "
                f"{window.owner_uid} and this process may not close it"
            )
            continue
        except OSError as exc:
            problems.append(f"could not close the {config.project} presentation window: {exc}")
            continue
        print_func(f"closed presentation window: {config.project} (pid {window.pid})")
    return problems


def desktop_presentation_windows(
    project: str,
    *,
    caller: str,
    proc_root: Path | None = None,
) -> list[PresentationWindowProcess]:
    """This project's windows in the account that owns the screen.

    Deliberately derived from the project and the caller alone, with no
    ProjectConfig: the desktop account cannot read the tenant's configuration at
    all, which is the whole reason the bridge exists. `desktop_state_dir` is
    already agreed by construction between the two sides of the handoff
    (SYRD-90), and it is the path Konsole was actually started with, so it is
    the one its argv carries.

    Whole-argument matching, both spellings, exactly as the tenant-side scan
    does: `<path>.backup` is a different file, and the project slug is a prefix
    of every sibling tenant's paths.
    """
    from scripts import team_launcher as launcher

    root = Path(proc_root) if proc_root is not None else Path("/proc")
    wanted = str(launcher.desktop_state_dir(project, caller) / f"{project}-presentation-layout.json")
    found: list[PresentationWindowProcess] = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().decode("utf-8", "replace").split("\0")
            owner_uid = entry.stat().st_uid
        except OSError:
            continue
        if not argv or "konsole" not in Path(argv[0] or "").name:
            continue
        match = next(
            (value for value in argv if value == wanted or value.partition("=")[2] == wanted),
            "",
        )
        if not match:
            continue
        found.append(PresentationWindowProcess(int(entry.name), owner_uid, match))
    return found


def close_desktop_presentation(
    project: str,
    *,
    caller: str,
    proc_root: Path | None = None,
    signaller: Callable[[int, int], None] = os.kill,
    print_func: Callable[[str], None] = print,
) -> int:
    """Close this project's presentation windows, here, where they can be closed.

    The mirror of `complete_desktop_presentation`, and it exists for the same
    reason that does. The owner account has the sessions and no screen; this
    account has the screen. A stop run entirely on the owner side asked a
    tenant-side scan for the tenant's own layout path, found nothing -- the live
    window names THIS account's copy -- and reported "already closed" over a
    window still on screen. Even had it matched, the owner may not signal a
    process belonging to this account, so the close has to happen here (SYRD-202).

    EVERY matching window, not the first: the UAT that found this left two on
    the same layout, and a stop that closes one of them is the same bug again.
    """
    from scripts import team_launcher as launcher

    windows = launcher.desktop_presentation_windows(project, caller=caller, proc_root=proc_root)
    if not windows:
        return 0
    problems: list[str] = []
    for window in windows:
        try:
            signaller(window.pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except OSError as exc:
            problems.append(f"could not close {project}'s presentation window {window.pid}: {exc}")
            continue
        print_func(f"switchyard: closed {project}'s presentation window (pid {window.pid})")
    for problem in problems:
        print_func(f"switchyard: {problem}")
    return 1 if problems else 0
