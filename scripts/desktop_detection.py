"""Whose desktop a presentation window belongs to, and which layout that desktop gets.

- **The desktop account:** `presentation_gui_user` answers for one project: a
  recorded Wayland policy's account (`pinned_presentation_gui_user`) wins, then
  the configured account (`GUI_USER_ENV` and its legacy name), `SUDO_USER` and
  the lifecycle bridge's caller -- never root -- then the owner.
  `default_gui_user` is the same question asked without a project.
- **The layout mode:** `detected_invoking_desktop` names the invoking human's
  desktop, from the environment or from `loginctl` through the runner it is
  given (`_desktop_from_loginctl_output`); `resolve_layout_mode` turns
  `auto` into `separate` on KDE (`_desktop_is_kde`) and `viewer` elsewhere.

The environment reader, the current user and the bridge's variable name stay
in `scripts/team_launcher.py` and are read there when a function runs. The
suites patch `detected_invoking_desktop`, `presentation_gui_user` and
`default_gui_user` on the launcher, and every caller -- here, in the launcher
and in the modules already moved out -- reaches them there.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-320). `team_launcher`
imports this module at its top and still exports every name callers read there.
This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING, Any, Callable, Mapping

from scripts.layout_modes import LAYOUT_MODE_AUTO, LAYOUT_MODE_CHOICES, LAYOUT_MODE_SEPARATE, LAYOUT_MODE_VIEWER

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig

GUI_USER_ENV = "TEAM_LAUNCHER_GUI_USER"
LEGACY_GUI_USER_ENV = "PGU_TEAM_LAUNCHER_GUI_USER"


def default_gui_user() -> str:
    from scripts import team_launcher as launcher

    configured = launcher._env_first(GUI_USER_ENV, LEGACY_GUI_USER_ENV)
    if configured:
        return configured
    sudo_user = launcher._env_first("SUDO_USER")
    if sudo_user:
        return sudo_user
    # The bridge's own answer to the same question. It is not a name the caller
    # supplied: the bridge resolves it from SUDO_UID, which the kernel sets,
    # and it names the human whose desktop asked for this project. Trusting it
    # here is what makes a passwordless `switchyard <project>` open a window on
    # that desktop instead of on the owner account's empty session, which is
    # the gap that left recovery available only through sudo (SYRD-65).
    bridged = launcher._env_first(launcher.TENANT_CONTROL_CALLER_ENV)
    if bridged:
        return bridged
    return launcher.current_user_name()


def pinned_presentation_gui_user(config: "ProjectConfig") -> str:
    """The desktop account this tenant's recorded Wayland policy grants, or "".

    The policy is the tenant's own consent record, validated when it was
    installed: it names one account whose compositor these panes may reach. A
    window for this project belongs on that desktop and nowhere else, so where
    a policy names one, it is the answer -- not whoever the environment says
    ran the command (SYRD-233).
    """
    policy = getattr(config, "desktop_access", None)
    if not isinstance(policy, Mapping) or policy.get("mode") != "wayland":
        return ""
    user = str(policy.get("gui_user") or "").strip()
    return "" if user == "root" else user


def presentation_gui_user(config: "ProjectConfig") -> str:
    """The desktop account a presentation window for this project belongs to.

    The owner account owns the sessions; it does not necessarily own a screen.
    A recorded Wayland policy names the account whose screen that is, and wins
    (SYRD-233). Otherwise, when a desktop identity is known -- configured,
    through sudo, or carried by the control bridge -- the window belongs to that
    person's session, and the owner is the fallback for a tenant driven from its
    own desktop (SYRD-65).
    """
    from scripts import team_launcher as launcher

    pinned = pinned_presentation_gui_user(config)
    if pinned:
        return pinned
    for name in (GUI_USER_ENV, LEGACY_GUI_USER_ENV, "SUDO_USER", launcher.TENANT_CONTROL_CALLER_ENV):
        candidate = os.environ.get(name, "").strip()
        # Never root. A presentation window opened as root is a root shell
        # behind every tab, and `sudo -u root` from an already-privileged
        # invocation is not a crossing at all (SYRD-43). An environment that
        # names root is answering a different question than this one.
        if candidate and candidate != "root":
            return candidate
    return config.run_as_user or launcher.current_user_name()


def _desktop_is_kde(desktop: str) -> bool:
    tokens = {
        token.strip().casefold()
        for chunk in desktop.replace(";", ":").split(":")
        for token in [chunk]
        if token.strip()
    }
    return bool(tokens & {"kde", "plasma"})


def _desktop_from_loginctl_output(output: str) -> str:
    values: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "=" in line:
            name, value = line.split("=", 1)
            if name != "Desktop":
                continue
            value = value.strip()
            if value:
                values.append(value)
        else:
            values.append(line)
    return ":".join(values)


def detected_invoking_desktop(
    *,
    environ: dict[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> str:
    from scripts import team_launcher as launcher

    env = environ if environ is not None else os.environ
    desktop = str(env.get("XDG_CURRENT_DESKTOP", "")).strip()
    if desktop:
        return desktop
    if str(env.get("KDE_FULL_SESSION", "")).strip().casefold() in {"1", "true"}:
        return "KDE"
    # Whose desktop the window is for, not which account is executing. Through
    # the control bridge USER is the tenant owner, which has no graphical
    # session, so asking about it answered "no desktop" and chose the viewer
    # layout -- the one layout that opens no window at all (SYRD-65).
    user = str(
        env.get("SUDO_USER") or env.get(launcher.TENANT_CONTROL_CALLER_ENV) or env.get("USER") or ""
    ).strip()
    if not user:
        return ""
    try:
        display_proc = runner(
            ["loginctl", "show-user", user, "-p", "Display", "--value"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, ValueError):
        return ""
    if display_proc.returncode != 0:
        return ""
    session_id = str(display_proc.stdout or "").strip()
    if not session_id:
        return ""
    try:
        session_proc = runner(
            ["loginctl", "show-session", session_id, "-p", "Desktop"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, ValueError):
        return ""
    if session_proc.returncode != 0:
        return ""
    return _desktop_from_loginctl_output(str(session_proc.stdout or ""))


def resolve_layout_mode(
    requested: str,
    *,
    environ: dict[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> str:
    from scripts import team_launcher as launcher

    if requested not in LAYOUT_MODE_CHOICES:
        raise SystemExit(f"unknown layout mode: {requested}")
    if requested != LAYOUT_MODE_AUTO:
        return requested
    desktop = launcher.detected_invoking_desktop(environ=environ, runner=runner)
    if not desktop:
        return LAYOUT_MODE_VIEWER
    return LAYOUT_MODE_SEPARATE if _desktop_is_kde(desktop) else LAYOUT_MODE_VIEWER
