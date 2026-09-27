"""Reconnecting the presentation after a cutover, asking whether anyone is looking at it, and handing its window back to the caller.

- **After a cutover:** `reconnect_presentation` re-points the display slots at
  the roles that just came back, and `presentation_is_attached` says whether a
  terminal outside presentation is actually showing it. Both ask
  `scripts/presentation_controller.py`, imported when they run, and neither
  lets a presentation failure end the cutover.
- **Handing the window back:** a launch that crossed the lifecycle bridge
  cannot open a window as the owner account. `hand_presentation_back_to_the_caller`
  writes what the caller needs to open it (`render_presentation_handoff`,
  with each slot's title from `presentation_slot_titles`) to the descriptor
  the bridge names in `PRESENTATION_HANDOFF_FD_ENV`.

The handoff schema, window and pane titles, and the pane program stay in
`scripts/team_launcher.py`. This module reads them there when a function runs.
The suites patch `reconnect_presentation` on the launcher, and its callers --
the cutover among them -- reach all three entry points there.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-316). `team_launcher`
imports this module at its top and still exports every name callers read there.
This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

from scripts.layout_modes import LAYOUT_MODE_SEPARATE

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig



def hand_presentation_back_to_the_caller(
    config: "ProjectConfig",
    *,
    slot_count: int,
    window_title: str = "",
    layout: str = LAYOUT_MODE_SEPARATE,
    slot_titles: Sequence[str] | None = None,
    pane_program: Path | None = None,
    print_func: Callable[[str], None] = print,
) -> bool:
    """Report the window for the bridge caller to open, and say so if it cannot.

    `_hand_off_desktop_half` does this for tenants that run the presentation
    controller, and only those: it is reached through `launch_presentation`,
    which `presentation_enabled` gates on the config carrying a `presentation`
    section. A provisioned tenant carries `desktop_access` and a `layout` and no
    such section, so a bridged launch of one fell through to opening Konsole
    HERE -- as the owner account, which has no screen -- and the caller, finding
    no handoff, opened nothing and returned success.

    That is what live Zorin UAT saw: every worker started or attached, then the
    shell back, no window and no complaint (SYRD-211 live UAT).
    """
    from scripts import team_launcher as launcher

    raw_fd = os.environ.get(PRESENTATION_HANDOFF_FD_ENV, "").strip()
    if not raw_fd.isdigit():
        return False
    payload = render_presentation_handoff(
        config.project,
        slot_count=slot_count,
        pane_program=(
            pane_program
            if pane_program is not None
            else launcher.pane_window_program(launcher.switchyard_pane_launcher_for(config))
        ),
        slot_titles=(
            list(slot_titles)
            if slot_titles is not None
            else presentation_slot_titles(config, slot_count)
        ),
        window_title=window_title or launcher.project_window_title(config),
        layout=layout,
    )
    try:
        with os.fdopen(int(raw_fd), "w", encoding="utf-8", closefd=True) as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
    except OSError as exc:
        raise SystemExit(
            f"switchyard: could not hand {config.project}'s presentation window back to "
            f"the account that asked for it: {exc}"
        )
    print_func(
        f"switchyard: {config.project}'s panes are up; its window opens in the session that "
        "ran this, which owns the screen."
    )
    return True


#: The bridge tells its child which descriptor to answer on. The bridge chooses
#: it, never the caller: the environment the child gets is rebuilt from
#: root-owned data, and this is one more field of it.
PRESENTATION_HANDOFF_FD_ENV = "SWITCHYARD_PRESENTATION_HANDOFF_FD"


def presentation_slot_titles(config: ProjectConfig, slot_count: int) -> list[str]:
    """What each slot in the presentation window calls itself.

    A slot no role occupies keeps the project's own name: the window is still
    that project's, and there is no role to claim it (SYRD-130).
    """
    from scripts import team_launcher as launcher

    titles = [launcher.project_window_title(config)] * max(int(slot_count), 0)
    for role in config.roles:
        if role.detached or role.slot is None:
            continue
        if 0 <= role.slot < len(titles):
            titles[role.slot] = launcher.pane_split_title(config, role)
    return titles


def render_presentation_handoff(
    project: str,
    *,
    slot_count: int,
    pane_program: Path,
    slot_titles: Sequence[str],
    window_title: str = "",
    layout: str = "",
) -> dict[str, Any]:
    """Everything the caller needs to build its own layout, and nothing else.

    Not the layout itself. The account that owns the sessions renders nothing
    the desktop account will run: it reports what it alone knows -- how many
    slots there are, which pinned program a tab runs, and what each slot is
    called -- and the caller builds the layout from its own code. What crosses
    is checkable, and a payload that is not is refused rather than written into
    somebody's home (SYRD-90).

    The titles are here because the desktop half has no other way to learn
    them: it knows the project's slug and nothing about its roles, which is why
    its window opened with the fallback title (SYRD-130).

    The window's own name crosses for the same reason and is checked the same
    way. The desktop half could read the project's registered display name for
    itself, and does for `--qwindowtitle`; but this one is handed to a terminal
    as an escape sequence, so it travels as a field that both sides validate
    rather than as something one side looks up unchecked (SYRD-139).
    """
    from scripts import team_launcher as launcher

    return {
        "schema": launcher.PRESENTATION_HANDOFF_SCHEMA,
        "project": project,
        # Which shape the owner actually built. On a desktop that is not KDE the
        # auto layout resolves to `viewer` -- one tiled tmux session holding
        # every role -- and a caller told only "five slots" would build five
        # tabs for display sessions that do not exist (SYRD-211 live UAT).
        "layout": str(layout or LAYOUT_MODE_SEPARATE),
        "slot_count": int(slot_count),
        "pane_program": str(pane_program),
        "slot_titles": [str(title) for title in slot_titles],
        "window_title": str(window_title),
    }


def reconnect_presentation(
    config: ProjectConfig,
    *,
    config_path: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> list[str]:
    """Point the display slots back at the roles that just came back.

    The slots are long-lived sessions that proxy a worker; when the workers are
    replaced the proxies have to be re-pointed, in place. Re-running the whole
    launch instead is what opens a second six-pane window and stacks two status
    bars, so the mapping is reapplied rather than relaunched (SYRD-45).
    """
    from scripts import presentation_controller

    if not presentation_controller.presentation_enabled(config, config_path=config_path):
        return []
    try:
        presentation_controller.reconnect_display_slots(config, config_path=config_path, runner=runner)
    except Exception as exc:  # noqa: BLE001 - reported, never fatal to the cutover
        return [str(exc)]
    return []


def presentation_is_attached(
    config: ProjectConfig,
    *,
    config_path: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> bool:
    """Whether a terminal outside presentation is actually displaying it.

    Every slot reporting a connected client is not the same thing: in the
    viewer topology those clients are presentation's own panes, so six of them
    can exist with no window on any screen at all. This asks the question the
    tenant cares about -- is somebody looking at it -- and a project with no
    presentation configured is not claimed to be attached (SYRD-65).
    """
    from scripts import presentation_controller

    if not presentation_controller.presentation_enabled(config, config_path=config_path):
        return False
    try:
        return presentation_controller.presentation_window_attached(
            config, config_path=config_path, runner=runner
        )
    except Exception:  # noqa: BLE001 - an unreadable presentation is not an attachment
        return False
