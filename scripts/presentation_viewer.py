"""Construct the tmux viewer and reconcile its observer sizing.

Viewer panes attach to display slots with exact targets. A viewer pane yields
size to a real window on the same slot, or sizes the slot when it is the only
view. Client and pane probes supply that live evidence.
"""

from __future__ import annotations

import shlex
import subprocess
from typing import Any, Callable, Mapping

from scripts import team_launcher
from scripts.presentation_display_session import (
    _recovery_hook_index,
    _session_exists,
    display_session_name,
)
from scripts.presentation_runtime_assignments import _exact_tmux_target

# The viewer aggregates display slots that other clients may already be showing
# at their own size, so its clients never contribute to slot geometry.  tmux
# still sizes a slot from such a client when it is that slot's only one.
VIEWER_OBSERVER_CLIENT_FLAGS = "ignore-size"


def _session_pane_ttys(
    session: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> set[str]:
    """Terminals owned by a presentation session's own panes."""
    proc = runner(
        [
            "tmux", "list-panes", "-s", "-t", _exact_tmux_target(session),
            "-F", "#{pane_tty}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if proc.returncode != 0:
        return set()
    return {line.strip() for line in str(getattr(proc, "stdout", "") or "").splitlines() if line.strip()}


def _exact_target_args(args: list[str]) -> list[str]:
    """The shared viewer command, with its `-t` target made exact."""
    exact = list(args)
    index = exact.index("-t") + 1
    exact[index] = _exact_tmux_target(exact[index])
    return exact


def _clients_by_tty(
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> dict[str, str]:
    """Every attached client on this server, by terminal, with its session."""
    return {tty: session for tty, (session, _flags) in _client_table(runner=runner).items()}


def _client_table(
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> dict[str, tuple[str, str]]:
    """Every attached client: terminal -> (session, flags)."""
    proc = runner(
        ["tmux", "list-clients", "-F", "#{client_tty}\t#{client_session}\t#{client_flags}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if proc.returncode != 0:
        return {}
    clients: dict[str, tuple[str, str]] = {}
    for line in str(proc.stdout or "").splitlines():
        tty, _, rest = line.partition("\t")
        session, _, flags = rest.partition("\t")
        if tty and session:
            clients[tty] = (session, flags)
    return clients


def _viewer_observer_attach(session: str, *, sizing: bool = False) -> str:
    """Viewer pane command that attaches to a display slot as an observer.

    `sizing` says whether this pane is what sizes the slot. SYRD-27 meant
    `ignore-size` to stop a viewer shrinking a slot that a separate window is
    showing, "while tmux still sizes a slot from the viewer pane when that pane
    is the slot's only client". tmux does not do the second half: it ignores a
    flagged client whenever ANY unflagged client is attached anywhere on the
    server -- the person's own window, every slot's proxy -- so in the viewer
    layout nothing ever sized a slot. Measured on tmux 3.2a, 3.4 and 3.7c alike:
    the viewer maximized to 240x70 and each slot stayed 80x24 inside an 80x34
    pane, the dotted space test12 showed (SYRD-221 UAT). So the viewer decides
    it itself, per slot: a pane that is the slot's only view attaches without
    the flag, and the slot and its worker follow it.

    tmux runs a pane command through `default-shell`, so the exact-target `=`
    prefix has to reach tmux quoted: a zsh default-shell would otherwise read
    `=<session>` as an equals expansion and the pane would die instead of
    attaching.
    """
    attach = shlex.join(
        [
            "env", "TMUX=", "tmux", "attach",
            "-f", f"!{VIEWER_OBSERVER_CLIENT_FLAGS}" if sizing else VIEWER_OBSERVER_CLIENT_FLAGS,
            "-t", _exact_tmux_target(session),
        ]
    )
    return shlex.join(["sh", "-lc", attach])


def _viewer_frame_commands(viewer: str) -> tuple[list[str], ...]:
    return (
        # Each viewer pane already shows the worker's own status line, so the
        # frame carries slot labels on the pane borders instead of adding a
        # second status bar of its own.
        ["tmux", "set-option", "-t", _exact_tmux_target(f"{viewer}:"), "status", "off"],
        [
            "tmux", "set-window-option", "-t", _exact_tmux_target(f"{viewer}:0"),
            "pane-border-status", "top",
        ],
        [
            "tmux", "set-window-option", "-t", _exact_tmux_target(f"{viewer}:0"),
            "pane-border-format", " slot #{@switchyard_slot}: #{@switchyard_role} ",
        ],
    )


def _reconcile_viewer_observers(
    config: team_launcher.ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    """Let each viewer pane size its slot -- unless a real window shows it too.

    SYRD-27's rule, applied per slot rather than hoped for from tmux: an
    observer never resizes what a real window is showing, and a pane that is a
    slot's only view is what sizes it. A separate window on the slot turns the
    viewer's client for it into `ignore-size`; with none, the flag is cleared
    and the slot and its worker follow the pane (SYRD-221 UAT, test12).

    A viewer pane whose client has not attached yet -- or has gone -- is left
    to the flag it attached with.
    """
    reconcile_viewer_observer_flags(
        team_launcher.viewer_session_for_project(config.project), runner=runner
    )


def reconcile_viewer_observer_flags(
    viewer: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    """The per-slot decision itself, from nothing but the viewer's name.

    Separate so that tmux can run it too: the viewer's `client-attached` and
    `client-detached` hooks call it through `switchyard-viewer-layout
    --observers`, because a window opened on a slot after the viewer was built
    must be yielded to at once -- not at the next reconcile, by which time the
    viewer has resized it (SYRD-27's real-client test, SYRD-221).
    """
    own = _session_pane_ttys(viewer, runner=runner)
    if not own:
        return
    clients = _client_table(runner=runner)
    shown_elsewhere = {session for tty, (session, _f) in clients.items() if tty not in own}
    for tty in sorted(own):
        if tty not in clients:
            continue
        session, current = clients[tty]
        ignore = session in shown_elsewhere
        # Touched only when the flag must change. Every refresh makes tmux
        # recalculate sizes, and a slot resized while its recovery message is
        # being drawn reflows that message off the top of its own screen --
        # which is what a refresh on every client event did (SYRD-221).
        if ignore == (VIEWER_OBSERVER_CLIENT_FLAGS in current.split(",")):
            continue
        flags = VIEWER_OBSERVER_CLIENT_FLAGS if ignore else f"!{VIEWER_OBSERVER_CLIENT_FLAGS}"
        # A viewer pane may have detached between listing and refresh.
        runner(
            ["tmux", "refresh-client", "-f", flags, "-t", tty],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _launch_viewer(
    config: team_launcher.ProjectConfig,
    state: Mapping[str, Any],
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    viewer = team_launcher.viewer_session_for_project(config.project)
    if _session_exists(viewer, runner=runner):
        proc = runner(["tmux", "kill-session", "-t", _exact_tmux_target(viewer)])
        if proc.returncode != 0:
            raise SystemExit(f"switchyard: could not replace viewer {viewer}")
    sessions = [display_session_name(config.project, slot) for slot in range(state["slot_count"])]
    # Which slots a real window already shows, decided before any pane attaches:
    # a client that has not finished attaching when the viewer is reconciled
    # keeps the flag it attached with, so that flag has to be right.
    shown_elsewhere = set(_clients_by_tty(runner=runner).values())
    first, *rest = sessions
    attach = _viewer_observer_attach(first, sizing=first not in shown_elsewhere)
    proc = runner([
        "tmux", "new-session", "-d", "-x", str(team_launcher.DEFAULT_VIEWER_COLUMNS),
        "-y", str(team_launcher.DEFAULT_VIEWER_ROWS), "-s", viewer, attach,
    ])
    if proc.returncode != 0:
        raise SystemExit(f"switchyard: could not create viewer {viewer}")
    # Before the splits, or tmux 3.2a builds them in an 80x23 window and the
    # fourth fails for want of rows (SYRD-221 UAT, test11).
    proc = runner(_exact_target_args(team_launcher.tmux_viewer_pin_size_args(viewer)))
    if proc.returncode != 0:
        raise SystemExit(f"switchyard: could not size viewer {viewer}")
    for session in rest:
        proc = runner([
            "tmux", "split-window", "-t", _exact_tmux_target(f"{viewer}:0"),
            _viewer_observer_attach(session, sizing=session not in shown_elsewhere),
        ])
        if proc.returncode != 0:
            raise SystemExit(f"switchyard: could not populate viewer {viewer}")
    commands = (
        # Not `tiled`: tmux grows rows before columns, so five slots come out
        # two columns by three rows -- narrow panes and the shape of an empty
        # sixth cell. The written-out layout puts the row across the top and
        # the remainder across the full width below it (SYRD-216).
        [
            "tmux",
            "select-layout",
            "-t",
            _exact_tmux_target(f"{viewer}:0"),
            team_launcher.viewer_layout_string(
                len(sessions),
                width=team_launcher.DEFAULT_VIEWER_COLUMNS,
                height=team_launcher.DEFAULT_VIEWER_ROWS,
            ),
        ],
        *_viewer_frame_commands(viewer),
    )
    for args in commands:
        proc = runner(args)
        if proc.returncode != 0:
            raise SystemExit(f"switchyard: could not configure viewer {viewer}")
    # And keep it matching the window's shape, not just its first shape: tmux
    # scales a layout on resize and never re-derives it (SYRD-216). A tmux too
    # old for the hook says so and keeps its viewer (SYRD-221 UAT).
    if team_launcher.install_viewer_relayout_hook(viewer, runner=runner) != 0:
        raise SystemExit(f"switchyard: could not configure viewer {viewer}")
    # Built: from here its size is the window's that shows it (SYRD-216).
    proc = runner(_exact_target_args(team_launcher.tmux_viewer_unpin_size_args(viewer)))
    if proc.returncode != 0:
        raise SystemExit(f"switchyard: could not release viewer {viewer} to its window")
    # And whenever a window opens or closes on a slot, re-decide which pane
    # sizes it -- both hooks exist on tmux 3.2a (SYRD-221 UAT).
    for event in ("client-attached", "client-detached"):
        proc = runner(team_launcher.tmux_viewer_observer_hook_args(
            viewer, event, index=_recovery_hook_index(config.project, -1)
        ))
        if proc.returncode != 0:
            raise SystemExit(f"switchyard: could not configure viewer {viewer}")
    _reconcile_viewer_observers(config, runner=runner)
    for slot in range(state["slot_count"]):
        role = state["slots"][str(slot)] or "hidden"
        for option, value in (("@switchyard_slot", str(slot)), ("@switchyard_role", role)):
            proc = runner(
                ["tmux", "set-option", "-p", "-t", _exact_tmux_target(f"{viewer}:0.{slot}"), option, value]
            )
            if proc.returncode != 0:
                raise SystemExit(f"switchyard: could not label viewer slot {slot}")
