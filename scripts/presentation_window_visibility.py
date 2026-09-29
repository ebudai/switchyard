"""Prove which presentation clients are visible in a real window.

This owner inspects display and viewer terminals, waits for an external
window, explains failed attachment and clears a headless viewer.
"""

from __future__ import annotations

import subprocess
import time
from typing import Any, Callable

from scripts import team_launcher
from scripts.presentation_display_session import _session_exists, display_session_name
from scripts.presentation_runtime_assignments import _exact_tmux_target
from scripts.presentation_viewer import _session_pane_ttys

#: How long a freshly launched window is given to attach before we call the
#: launch unproven. A desktop terminal that is going to appear does so in well
#: under this; one that never appears must not be reported as a success.
WINDOW_ATTACH_TIMEOUT_SECONDS = 10.0
WINDOW_ATTACH_POLL_SECONDS = 0.25


def _presentation_client_ttys(
    config: team_launcher.ProjectConfig,
    slot_count: int,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> set[str]:
    """Terminals that presentation itself drives, so worker clients can be told apart."""
    ttys: set[str] = set()
    for slot in range(slot_count):
        ttys |= _session_pane_ttys(display_session_name(config.project, slot), runner=runner)
    ttys |= _session_pane_ttys(team_launcher.viewer_session_for_project(config.project), runner=runner)
    return ttys


def _session_client_ttys(
    session: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> set[str]:
    """Terminals attached to one session, whoever they belong to."""
    proc = runner(
        [
            "tmux", "list-clients", "-t", _exact_tmux_target(session),
            "-F", "#{client_tty}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if proc.returncode != 0:
        return set()
    return {line.strip() for line in str(getattr(proc, "stdout", "") or "").splitlines() if line.strip()}


def external_presentation_clients(
    config: team_launcher.ProjectConfig,
    slot_count: int,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> set[str]:
    """Terminals displaying this presentation that presentation does not own.

    Every display slot normally has a client, and in the viewer topology that
    client is a viewer pane -- which is another terminal presentation created
    for itself. Counting those is what let a bootstrap that opened no window at
    all leave six sessions reporting ``session_attached=1`` and report success:
    the clients were real, and every one of them was headless. What proves a
    human can see the project is a client from outside that set: the tab of a
    desktop terminal (SYRD-65).
    """
    own = _presentation_client_ttys(config, slot_count, runner=runner)
    sessions = [display_session_name(config.project, slot) for slot in range(slot_count)]
    sessions.append(team_launcher.viewer_session_for_project(config.project))
    outside: set[str] = set()
    for session in sessions:
        outside |= {tty for tty in _session_client_ttys(session, runner=runner) if tty not in own}
    return outside


def await_presentation_window(
    config: team_launcher.ProjectConfig,
    slot_count: int,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    timeout: float = WINDOW_ATTACH_TIMEOUT_SECONDS,
    poll: float = WINDOW_ATTACH_POLL_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> set[str]:
    """Wait, briefly, for a window to attach; return the terminals that did.

    A desktop terminal is started detached and reaches its tmux client a moment
    later, so the question cannot be asked once and immediately. It also cannot
    be waited on forever: a window that is never going to appear has to become
    an answer rather than a hang.
    """
    deadline = monotonic() + timeout
    while True:
        found = external_presentation_clients(config, slot_count, runner=runner)
        if found or monotonic() >= deadline:
            return found
        sleep(poll)


def unmapped_presentation_message(
    config: team_launcher.ProjectConfig,
    *,
    layout: str,
    control_user: str = "",
) -> str:
    """Why a launch that reported no error still put nothing on a screen.

    Names the one command that does work from where the human actually is. A
    tenant or role account has no desktop session of its own, so a launch made
    from one can create every client in the topology and still be invisible.
    """
    desktop = (control_user or "").strip()
    who = f"{desktop}'s desktop session" if desktop else "the desktop session that owns the screen"
    lines = [
        f"switchyard: {config.project}'s presentation is not on any screen: its display slots "
        "have only presentation's own clients, so nothing here is a window a person can see.",
    ]
    if layout == team_launcher.LAYOUT_MODE_VIEWER:
        lines.append(
            "switchyard: the viewer layout builds the nested sessions but opens no window of its "
            "own; a terminal has to attach to it."
        )
    else:
        lines.append(
            "switchyard: the window was launched and never attached; a tenant or role account has "
            "no desktop session, so its terminal has nowhere to appear."
        )
    lines.append(
        f"switchyard: run `switchyard {config.project}` from an ordinary terminal in {who}, or "
        f"through that account's lifecycle control bridge, which carries the desktop identity."
    )
    return "\n".join(lines)


def _detach_headless_presentation(
    config: team_launcher.ProjectConfig,
    slot_count: int,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    """Take down the clients that were standing in for a window there is not.

    Left in place they are worse than nothing: `present list` reads them as
    connected, so the next person to look sees a healthy presentation and the
    real state -- slots nobody is displaying -- stays hidden.
    """
    viewer = team_launcher.viewer_session_for_project(config.project)
    if _session_exists(viewer, runner=runner):
        runner(
            ["tmux", "kill-session", "-t", _exact_tmux_target(viewer)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _slot_visible(
    config: team_launcher.ProjectConfig,
    slot: int,
    own_ttys: set[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> bool:
    """Is a window a person can see showing this display slot?

    Either a terminal presentation does not own is a client of the slot itself
    (a separate window's tab), or a viewer pane is its client and the viewer
    session has such a terminal (the viewer window). Presentation's own panes
    are clients everywhere and prove nothing on their own (SYRD-65).
    """
    clients = _session_client_ttys(display_session_name(config.project, slot), runner=runner)
    if clients - own_ttys:
        return True
    viewer = team_launcher.viewer_session_for_project(config.project)
    viewer_panes = _session_pane_ttys(viewer, runner=runner)
    return bool(clients & viewer_panes) and bool(
        _session_client_ttys(viewer, runner=runner) - own_ttys
    )
