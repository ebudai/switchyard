"""Starting a window in the desktop account's GUI session: crossing to that account, the environment it is given, and the Konsole window itself.

- **Crossing:** `gui_privilege_drop_args` is how a root-invoked launch reaches
  the desktop account (`sudo -u <account> -H --`), and why it refuses to open
  a window as root. `_gui_launch_prefix` puts everything before the terminal
  program together: the crossing, then either `env -i` with exactly
  `GUI_ENVIRONMENT_ALLOWLIST` (`gui_environment_args`, `_gui_runtime_dir`) or,
  for an unprivileged caller, only the Wayland display
  (`normalize_wayland_display`, `GUI_WAYLAND_ENV`, `HOST_WAYLAND_ENV` and their
  legacy names). `gui_program_path` resolves a program an emptied environment
  can still run, and `_refusal_command` says why nothing will be opened.
- **Konsole:** `konsole_launch_args` builds the argv; `launch_konsole_window`
  writes the window-title default beside the layout
  (`write_konsole_config_defaults`, `KONSOLE_DEFAULTS_NAME`,
  `KONSOLE_WINDOW_TITLE_DEFAULTS`, `FALLBACK_XDG_CONFIG_DIRS`), starts the
  terminal in its own session, makes its log readable to the invoking account
  (`_make_konsole_log_readable`) and reports an early exit
  (`_print_konsole_early_exit`).

Account lookups, homes, the default desktop account, the pane search path and
the environment reader stay in `scripts/team_launcher.py` and are read there
when a function runs. The suites patch `launch_konsole_window`,
`_gui_launch_prefix`, `gui_program_path` and `_make_konsole_log_readable` on the
launcher, and every caller -- here, in the launcher, in the presentation
controller and in the desktop half of the hand-off -- reaches them there.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-318). `team_launcher`
imports this module at its top and still exports every name callers read there.
This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import os
import pwd
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

GUI_WAYLAND_ENV = "TEAM_LAUNCHER_WAYLAND_DISPLAY"
LEGACY_GUI_WAYLAND_ENV = "PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY"
HOST_WAYLAND_ENV = "HOST_WAYLAND_DISPLAY"
LEGACY_HOST_WAYLAND_ENV = "PGU_HOST_WAYLAND_DISPLAY"


def normalize_wayland_display(display: str, *, gui_user: str) -> str | None:
    from scripts import team_launcher as launcher

    display = display.strip()
    if display.startswith("/"):
        return display
    user = gui_user.strip()
    uid = launcher.uid_for_user(user) if user else None
    if uid is None:
        return None
    name = display or "wayland-0"
    return f"/run/user/{uid}/{name}"


def _refusal_command(message: str) -> list[str]:
    return ["sh", "-lc", f"printf '%s\\n' {shlex.quote(message)} >&2; exit 1"]


def gui_privilege_drop_args(gui_user: str, *, euid: int | None = None) -> tuple[list[str], str]:
    """How to reach the desktop identity before any GUI process starts.

    A terminal emulator started by a privileged invocation is a root terminal:
    every tab it opens is a root shell, and when the tmux client inside one
    detaches the tab falls back to that shell. Nothing in a role presentation
    window may run as root, so a root-invoked launch crosses to the desktop
    account first. Returns the argv prefix and, when it cannot, why (SYRD-43).
    """
    from scripts import team_launcher as launcher

    effective = os.geteuid() if euid is None else euid
    user = (gui_user or "").strip()
    if effective != 0:
        return [], ""
    if not user or user == "root":
        return [], (
            "team-launcher: refusing to open a presentation window as root. Set "
            f"{launcher.GUI_USER_ENV} to the desktop account, or run switchyard through that "
            "account's sudo so SUDO_USER identifies it."
        )
    if launcher.uid_for_user(user) in (None, 0):
        return [], (
            f"team-launcher: refusing to open a presentation window as root: {user!r} is not "
            "an unprivileged local account."
        )
    # sudo resets the environment, so root's variables and credentials do not
    # cross; everything the GUI needs is named explicitly by the caller.
    return ["sudo", "-u", user, "-H", "--"], ""


#: What Konsole has to be told before a window title can stay put.
#:
#: Konsole's caption is the active split's title unless this is on, in which
#: case it is the window title an escape sequence set -- and every split sets
#: the same one. The setting is an application preference rather than a profile
#: property, so it cannot be passed on the command line and it cannot travel in
#: the layout; it lives in `konsolerc`, which belongs to whoever is running the
#: terminal.
#:
#: So it is supplied as a cascaded default rather than written into anybody's
#: configuration: KConfig reads `$XDG_CONFIG_DIRS` beneath `$XDG_CONFIG_HOME`,
#: so a directory of our own on that path answers for a key the user has never
#: set, and stops answering the moment they set it themselves. Nothing of
#: theirs is edited, and their Konsole windows are unaffected (SYRD-139).
KONSOLE_DEFAULTS_NAME = "konsolerc"
#: `[$i]` is KConfig's immutability marker. Without it this is only a default:
#: a desktop account that has ever set this preference the other way keeps its
#: own answer, and the caption goes back to following whichever split has focus
#: -- which is what a User with that preference set would still have seen
#: (SYRD-141). Marked immutable, the value this launch supplies wins for this
#: process, and only for this process: it is reached through an
#: `XDG_CONFIG_DIRS` entry given to the terminal this launch starts, so nothing
#: of the user's is edited and no other Konsole window is affected.
KONSOLE_WINDOW_TITLE_DEFAULTS = "[KonsoleWindow]\nShowWindowTitleOnTitleBar[$i]=true\n"
FALLBACK_XDG_CONFIG_DIRS = "/etc/xdg"


def write_konsole_config_defaults(directory: Path, *, model: Path | None = None) -> str:
    """Put the cascaded default beside the layout it belongs to.

    Beside it deliberately: the account that can read the layout is the account
    that will read this, so one set of permissions answers for both. The file
    is given the layout's own owner and mode for the same reason -- on the
    crossing path root writes both into somebody else's directory, and a
    default the terminal cannot read is a default that does nothing.

    Returns a refusal rather than raising: a window with a title bar that
    follows focus is still a usable window, and refusing to open one over a
    preference file would be the worse failure.
    """
    target = directory / KONSOLE_DEFAULTS_NAME
    try:
        directory.mkdir(parents=True, exist_ok=True)
        target.write_text(KONSOLE_WINDOW_TITLE_DEFAULTS, encoding="utf-8")
    except OSError as exc:
        return f"cannot write the Konsole window-title default {target}: {exc}"
    if model is None or not model.exists():
        return ""
    try:
        stats = model.stat()
        os.chmod(target, stat.S_IMODE(stats.st_mode))
        if os.geteuid() == 0:
            os.chown(target, stats.st_uid, stats.st_gid)
    except OSError as exc:
        return f"cannot hand the Konsole window-title default {target} to its reader: {exc}"
    return ""


def _gui_launch_prefix(
    *, gui_user: str | None = None, config_dir: Path | None = None
) -> tuple[list[str], str]:
    """Everything before the terminal program: how to cross, and what to carry.

    Shared, because which terminal opens the window is a separate question from
    how to reach the desktop that will show it. Konsole was the only answer to
    the first for a long time, and a non-KDE host does not have it (SYRD-211
    live UAT).
    """
    from scripts import team_launcher as launcher

    user = (gui_user if gui_user is not None else launcher.default_gui_user()).strip()
    privilege_drop, refusal = gui_privilege_drop_args(user)
    if refusal:
        return [], refusal
    wayland_display = None
    host_wayland_display = launcher._env_first(HOST_WAYLAND_ENV, LEGACY_HOST_WAYLAND_ENV)
    if host_wayland_display:
        wayland_display = normalize_wayland_display(host_wayland_display, gui_user=user)
    if not wayland_display:
        wayland_name = launcher._env_first(GUI_WAYLAND_ENV, LEGACY_GUI_WAYLAND_ENV) or "wayland-0"
        wayland_display = normalize_wayland_display(wayland_name, gui_user=user)
    if not wayland_display:
        return [], "team-launcher: no host Wayland display; run from Eric desktop session"
    config_dirs = ""
    if config_dir is not None:
        existing = "" if privilege_drop else str(os.environ.get("XDG_CONFIG_DIRS") or "")
        config_dirs = ":".join(
            part for part in (str(config_dir), existing or FALLBACK_XDG_CONFIG_DIRS) if part
        )
    if privilege_drop:
        # Crossing from root is an environment boundary, so the environment is
        # emptied rather than filtered. sudo's env_reset is the host's policy,
        # not ours: any variable a site's env_keep preserves would otherwise
        # cross into the desktop account, and so would anything -H leaves set.
        # `env -i` discards all of it and the GUI is given exactly the variables
        # it needs, PATH included so an emptied environment can still resolve a
        # program (SYRD-43).
        environment = gui_environment_args(
            user, wayland_display=wayland_display, config_dirs=config_dirs
        )
    else:
        # An unprivileged invocation is already the caller's own session; there
        # is no boundary to cross and its desktop integration is its own.
        environment = [
            "env",
            "QT_QPA_PLATFORM=wayland",
            f"WAYLAND_DISPLAY={wayland_display}",
            *([f"XDG_CONFIG_DIRS={config_dirs}"] if config_dirs else []),
        ]
    return [*privilege_drop, *environment], ""


def konsole_launch_args(
    layout_path: Path,
    *,
    gui_user: str | None = None,
    window_title: str = "",
    config_dir: Path | None = None,
) -> list[str]:
    from scripts import team_launcher as launcher

    prefix, refusal = launcher._gui_launch_prefix(gui_user=gui_user, config_dir=config_dir)
    if refusal:
        return _refusal_command(refusal)
    args = [
        *prefix,
        launcher.gui_program_path("konsole"),
        "--separate",
    ]
    title = window_title.strip()
    if title:
        args.extend(["--qwindowtitle", title])
    args.extend(
        [
            "--layout",
            str(layout_path),
        ]
    )
    return args


# The complete environment a presentation window is given after crossing from
# root. Anything not here does not reach it, including anything a host's sudoers
# env_keep would have preserved (SYRD-43).
GUI_ENVIRONMENT_ALLOWLIST = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "XDG_RUNTIME_DIR",
    "XDG_CONFIG_DIRS",
    "QT_QPA_PLATFORM",
    "WAYLAND_DISPLAY",
)


def gui_program_path(program: str) -> str:
    """An absolute program, so an emptied environment still resolves it."""
    from scripts import team_launcher as launcher

    resolved = shutil.which(program, path=launcher.DEFAULT_PANE_BASE_PATH) or shutil.which(program)
    return resolved or program


def gui_environment_args(
    gui_user: str, *, wayland_display: str, config_dirs: str = ""
) -> list[str]:
    """`env -i` plus exactly the variables the desktop process is allowed."""
    from scripts import team_launcher as launcher

    values = {
        "PATH": launcher.DEFAULT_PANE_BASE_PATH,
        "HOME": launcher._gui_home(gui_user),
        "USER": gui_user,
        "LOGNAME": gui_user,
        "XDG_RUNTIME_DIR": _gui_runtime_dir(gui_user),
        # Not this process's: root's search path is not the desktop account's,
        # so what crosses is the one directory this launch supplies plus the
        # conventional system default.
        "XDG_CONFIG_DIRS": config_dirs,
        "QT_QPA_PLATFORM": "wayland",
        "WAYLAND_DISPLAY": wayland_display,
    }
    return [
        "env",
        "-i",
        *[f"{name}={values[name]}" for name in GUI_ENVIRONMENT_ALLOWLIST if values.get(name)],
    ]


def _gui_runtime_dir(gui_user: str) -> str:
    from scripts import team_launcher as launcher

    uid = launcher.uid_for_user(gui_user)
    return f"/run/user/{uid}" if uid is not None else ""


def launch_konsole_window(
    layout_path: Path,
    *,
    project: str,
    window_title: str = "",
    gui_user: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    process_launcher: Callable[..., Any] | None = None,
) -> int:
    from scripts import team_launcher as launcher

    defaults_refusal = write_konsole_config_defaults(layout_path.parent, model=layout_path)
    if defaults_refusal:
        print(f"team-launcher: {defaults_refusal}", file=sys.stderr)
    args = konsole_launch_args(
        layout_path,
        gui_user=gui_user,
        window_title=window_title or project,
        config_dir=layout_path.parent if not defaults_refusal else None,
    )
    if args[:2] == ["sh", "-lc"]:
        return runner(args).returncode
    launch_process = process_launcher or subprocess.Popen
    try:
        with tempfile.NamedTemporaryFile(
            mode="ab",
            prefix=f"{project}-team-launcher-konsole.",
            suffix=".log",
            delete=False,
        ) as handle:
            log_path = Path(handle.name)
            launcher._make_konsole_log_readable(log_path)
            proc = launch_process(
                args,
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=handle,
                start_new_session=True,
            )
    except OSError as exc:
        print(f"team-launcher: failed to launch Konsole: {exc}", file=sys.stderr)
        return 1
    time.sleep(0.2)
    returncode = proc.poll()
    if returncode is not None:
        _print_konsole_early_exit(returncode, log_path)
        return returncode
    print(
        f"team-launcher: started {project} in background (Konsole pid {proc.pid}; log {log_path})"
    )
    return 0


def _make_konsole_log_readable(log_path: Path, *, environ: dict[str, str] | None = None) -> None:
    from scripts import team_launcher as launcher

    env = environ if environ is not None else os.environ
    sudo_user = str(env.get("SUDO_USER") or "").strip()
    sudo_uid = launcher._int_env(env.get("SUDO_UID"))
    sudo_gid = launcher._int_env(env.get("SUDO_GID"))
    if sudo_user and (sudo_uid is None or sudo_gid is None):
        try:
            user_info = pwd.getpwnam(sudo_user)
        except KeyError:
            user_info = None
        if user_info is not None:
            sudo_uid = user_info.pw_uid if sudo_uid is None else sudo_uid
            sudo_gid = user_info.pw_gid if sudo_gid is None else sudo_gid
    if sudo_uid is not None and sudo_gid is not None:
        try:
            os.chown(log_path, sudo_uid, sudo_gid)
        except OSError:
            pass
    try:
        log_path.chmod(0o644)
    except OSError:
        pass


def _print_konsole_early_exit(returncode: int, log_path: Path) -> None:
    print(f"team-launcher: Konsole exited immediately with status {returncode}; captured output:", file=sys.stderr)
    try:
        captured = log_path.read_bytes()
    except OSError as exc:
        print(f"team-launcher: could not read Konsole log {log_path}: {exc}", file=sys.stderr)
        return
    if not captured:
        print(f"team-launcher: Konsole log {log_path} was empty", file=sys.stderr)
        return
    text = captured.decode("utf-8", errors="replace").rstrip()
    if text:
        print(text, file=sys.stderr)
