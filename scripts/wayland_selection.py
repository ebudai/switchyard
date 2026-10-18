"""Which Wayland display a presentation window opens on (SYRD-621).

A compositor's socket name is not stable across logins: after a host outage
the desktop owner's KWin came back on `wayland-1`, while the presentation
launch still forced `wayland-0`, and Konsole aborted with an empty log. The
name was never what made a display right; the owner, its runtime and a live
socket are. So, in order:

1. An explicit Switchyard override (`HOST_WAYLAND_DISPLAY`, then
   `TEAM_LAUNCHER_WAYLAND_DISPLAY`, and their legacy names) comes first, and
   is held to the same proof as every other choice; one that fails it is
   refused by name, never replaced.
2. An unprivileged caller -- already inside a desktop session -- has its own
   `WAYLAND_DISPLAY` used, when that names a live socket the GUI account owns
   in the GUI account's runtime directory. A root crossing never reads it: the
   caller's GUI variables are not the desktop account's to trust.
3. Otherwise the GUI account's only live Wayland socket, the bounded rule
   SYRD-592 gave desktop access (`desktop_access.wayland_sockets`). None, or
   several, is refused by name: Switchyard does not choose between displays.

`launch_hint` is what a failed launch adds to its report: the display it was
given and the live sockets that runtime has.

This module imports `team_launcher` only inside the functions, when they run.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Sequence

#: Where each account's runtime directory lives, as systemd-logind makes it.
#: Read when a function runs, so a test can point it at a fixture.
RUN_USER_ROOT = Path("/run/user")
OVERRIDE_HINT = "TEAM_LAUNCHER_WAYLAND_DISPLAY"


def runtime_dir(uid: int) -> Path:
    return RUN_USER_ROOT / str(uid)


def socket_problem(path: Path, *, uid: int) -> str:
    """Why `path` is not a Wayland socket the account `uid` may be shown on, or "" when it is."""
    runtime = runtime_dir(uid)
    if path.parent != runtime or not path.name.startswith("wayland-"):
        return f"{path} is not a Wayland socket in {runtime}"
    try:
        directory = runtime.lstat()
    except OSError:
        return f"{runtime} does not exist"
    if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != uid:
        return f"{runtime} is not uid {uid}'s runtime directory"
    try:
        info = path.lstat()
    except FileNotFoundError:
        return f"{path} is not live"
    except OSError as exc:
        return f"{path} cannot be inspected ({exc.strerror})"
    if stat.S_ISLNK(info.st_mode):
        return f"{path} is a symbolic link"
    if not stat.S_ISSOCK(info.st_mode):
        return f"{path} is not a socket"
    if info.st_uid != uid:
        return f"{path} belongs to uid {info.st_uid}, not uid {uid}"
    return ""


#: The names looked at one by one when the runtime directory can be passed
#: through but not listed -- the shape a desktop-access grant gives a project's
#: account on its human's runtime (SYRD-592).
PROBED_NAMES = tuple(f"wayland-{n}" for n in range(10))


def live_sockets(uid: int) -> list[Path]:
    """The account's live Wayland sockets: SYRD-592's rule, and only those the account owns."""
    from scripts.desktop_access import wayland_sockets

    runtime = runtime_dir(uid)
    if os.access(runtime, os.R_OK | os.X_OK):
        try:
            candidates = wayland_sockets(runtime)
        except OSError:
            candidates = []
    else:
        candidates = [runtime / name for name in PROBED_NAMES]
    return [path for path in candidates if not socket_problem(path, uid=uid)]


def select_wayland_display(gui_user: str, *, privileged: bool) -> tuple[str | None, str]:
    """The display to open on, as an absolute socket path, or None and why not.

    The environment is read through the launcher's `_env_first`, as the launch
    prefix always read it.
    """
    from scripts import team_launcher as launcher

    uid = launcher.uid_for_user(gui_user) if gui_user else None
    if uid is None:
        return None, f"team-launcher: no Wayland display: {gui_user or 'no desktop account'} is not a local account"
    runtime = runtime_dir(uid)
    # An override keeps its precedence, and is held to the same proof as any
    # other choice: in this account's runtime, a live socket it owns, not a
    # link. One that fails is refused by name rather than replaced -- the
    # operator chose it, and a window on some other display is not what they
    # asked for (SYRD-621 audit).
    for names in ((launcher.HOST_WAYLAND_ENV, launcher.LEGACY_HOST_WAYLAND_ENV),
                  (launcher.GUI_WAYLAND_ENV, launcher.LEGACY_GUI_WAYLAND_ENV)):
        explicit = launcher._env_first(*names)
        if explicit:
            path = Path(explicit) if explicit.startswith("/") else runtime / explicit
            problem = socket_problem(path, uid=uid)
            if problem:
                named = next(name for name in names if launcher._env_first(name))
                return None, (
                    f"team-launcher: {named}={explicit} is not a display {gui_user} may be shown on: {problem}. "
                    f"Unset it, or name a live Wayland socket in {runtime}."
                )
            return str(path), ""
    stale = ""
    own = "" if privileged else launcher._env_first("WAYLAND_DISPLAY")
    if own:
        path = Path(own) if own.startswith("/") else runtime / own
        problem = socket_problem(path, uid=uid)
        if not problem:
            return str(path), ""
        stale = f" This session's WAYLAND_DISPLAY={own} was not used: {problem}."
    live = live_sockets(uid)
    if len(live) == 1:
        return str(live[0]), ""
    if not live:
        return None, (
            f"team-launcher: no live Wayland socket for {gui_user} in {runtime}.{stale} Log into that "
            f"account's desktop session, or name its display with {OVERRIDE_HINT}=<wayland-N>."
        )
    return None, (
        f"team-launcher: {gui_user} has several live Wayland sockets in {runtime} "
        f"({', '.join(p.name for p in live)}), and Switchyard will not choose between them.{stale} Run "
        f"switchyard from the desktop session whose WAYLAND_DISPLAY names one, or set {OVERRIDE_HINT}=<wayland-N>."
    )


def launch_hint(args: Sequence[str]) -> str:
    """What a failed launch was given, and what was live: "" when it carried no Wayland display."""
    display = next((str(a).split("=", 1)[1] for a in args if str(a).startswith("WAYLAND_DISPLAY=")), "")
    if not display:
        return ""
    runtime = Path(display).parent
    try:
        uid = runtime.lstat().st_uid
    except OSError:
        return f"it was started on Wayland display {display}, in a runtime directory that does not exist"
    live = [p.name for p in live_sockets(uid)] if runtime == runtime_dir(uid) else []
    available = ", ".join(live) if live else "none"
    return (
        f"it was started on Wayland display {display}; live Wayland sockets there: {available}. "
        f"If the desktop runs on another, run switchyard from that session or set {OVERRIDE_HINT}=<wayland-N>"
    )
