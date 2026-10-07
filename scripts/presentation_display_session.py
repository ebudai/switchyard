"""Construct and configure owner-side display sessions for worker slots.

This owner probes a worker, builds its proxy/status pane, locks the outer and
cross-account tmux transport, labels the slot, and installs its recovery hook.
Callers decide mapping, viewer, live recovery and desktop handoff.
"""

from __future__ import annotations

import hashlib
import shlex
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping

from scripts import team_launcher
from scripts.presentation_runtime_assignments import _exact_tmux_target

DIRECTOR_ROLE = "director"
#: The key table a display slot's clients are put in: one that has no bindings,
#: so every key falls through to the pane instead of reaching tmux. Naming a
#: table that was never created is deliberate and sufficient -- a lookup that
#: finds nothing is what makes the client incapable of a tmux command.
DISPLAY_KEY_TABLE = "switchyard-display"



def display_session_name(project: str, slot: int) -> str:
    return f"{project}-display-{slot}"


def _role_by_name(config: team_launcher.ProjectConfig, role_name: str) -> team_launcher.RoleConfig | None:
    return next((role for role in config.roles if role.role == role_name), None)


def _session_exists(
    session: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> bool:
    return runner(
        ["tmux", "has-session", "-t", _exact_tmux_target(session)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def _role_status(
    config: team_launcher.ProjectConfig,
    role_name: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> dict[str, Any]:
    role = _role_by_name(config, role_name)
    if role is None:
        return {"role": role_name, "state": "removed", "live": False, "resumable": False}
    session_probe = runner(
        ["tmux", "has-session", "-t", _exact_tmux_target(role.tmux_session)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    live = session_probe.returncode == 0
    if not live:
        error = str(getattr(session_probe, "stderr", "") or "").strip()
        missing_markers = (
            "can't find session",
            "no server running",
            "failed to connect to server",
            "no such file or directory",
        )
        if error and not any(marker in error.lower() for marker in missing_markers):
            raise RuntimeError(
                f"tmux could not query {role_name}'s configured server: {error}"
            )
    pane_dead = False
    if live:
        proc = runner(
            ["tmux", "display-message", "-p", "-t", _exact_tmux_target(role.target), "#{pane_dead}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        pane_dead = proc.returncode != 0 or str(getattr(proc, "stdout", "") or "").strip() == "1"
    resumable = bool(team_launcher.session_id_for_role(role, team_launcher.role_session_dir(config, role)))
    state = "live" if live and not pane_dead else "dead" if live else "missing"
    return {"role": role_name, "state": state, "live": live and not pane_dead, "resumable": resumable}


def _worker_has_independent_client(
    role: team_launcher.RoleConfig,
    *,
    presentation_ttys: set[str],
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> bool:
    """True when a client outside presentation already displays this worker."""
    proc = runner(
        [
            "tmux", "list-clients", "-t", _exact_tmux_target(role.tmux_session),
            "-F", "#{client_tty}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if proc.returncode != 0:
        return False
    return any(
        tty.strip() and tty.strip() not in presentation_ttys
        for tty in str(getattr(proc, "stdout", "") or "").splitlines()
    )


def display_lock_options() -> tuple[tuple[str, str], ...]:
    """The session options that make an attached display client input-only.

    Attaching a client to a session hands it that session's key tables, and
    exact-target selection constrains only which session is attached: it says
    nothing about what the client may then do. With the tenant owner's default
    prefix still live, a desktop user given a display slot can press prefix-c
    for a shell in the owner's account, prefix-colon for a tmux command prompt,
    or prefix-s to switch to any other session on that server -- including
    another project's, when two share an owner (SYRD-65 review).

    Three options close it, and all three are needed. Both prefixes go, so the
    prefix table is unreachable. The key table goes too, because the root table
    is still consulted without a prefix and an owner whose tmux.conf carries
    any `bind -n` would keep exactly one of those bindings live; pointing the
    session at a table with no bindings in it leaves nothing to find. What is
    left is a client whose keys all fall through to the pane, which is the
    proxy attached to the worker -- so ordinary typing still reaches the role.
    """
    return (("prefix", "None"), ("prefix2", "None"), ("key-table", DISPLAY_KEY_TABLE))


def display_lock_commands(session: str) -> tuple[list[str], ...]:
    """`tmux` argv that locks one display session's transport."""
    return tuple(
        ["tmux", "set-option", "-t", _exact_tmux_target(f"{session}:"), option, value]
        for option, value in display_lock_options()
    )


def _proxy_client_flags(observer: bool) -> str:
    # A display slot is the worker's only client in the ordinary presentation
    # topology, so it must keep sizing that worker: otherwise the worker stops
    # following the presentation window and leaves unused space in the slot.
    # When the worker is already visible in a client of its own, the slot is a
    # second observer instead and must not resize what that client shows.
    flags = ["!no-detach-on-destroy"]
    if observer:
        flags.insert(0, "ignore-size")
    return ",".join(flags)


def worker_attach_argv(
    config: team_launcher.ProjectConfig,
    role: team_launcher.RoleConfig,
    *,
    observer: bool,
) -> list[str]:
    """The one argv that attaches a terminal to a role's live worker session.

    Display slots and `switchyard attach` share it deliberately. It is the only
    place that decides whether reaching a worker crosses a Unix account, and a
    second copy of that decision is a second thing to get wrong.

    `TMUX=` is cleared because the caller may itself be inside tmux, and tmux
    refuses to nest without it.
    """
    argv = ["env", "TMUX="]
    if _proxy_crosses_account(config, role):
        # Historical migrated tenants keep one tmux server per role.  The
        # project owner crosses that boundary through the preinstalled
        # role-control grant, which permits only tmux as the configured role
        # account: no root shell and no arbitrary program.
        argv.extend(
            [
                "/usr/bin/sudo", "-n", "-u",
                team_launcher.role_run_as_user(config, role),
                "/usr/bin/tmux",
            ]
        )
    else:
        # Shared-account tenants (including legacy PGU and SYRD-69 process
        # authority projects) keep the zero-artifact direct path.
        argv.append("tmux")
    argv.extend(
        ["attach", "-f", _proxy_client_flags(observer), "-t", _exact_tmux_target(role.tmux_session)]
    )
    return argv


def _proxy_crosses_account(
    config: team_launcher.ProjectConfig, role: team_launcher.RoleConfig
) -> bool:
    worker_account = team_launcher.role_run_as_user(config, role)
    owner_account = (config.run_as_user or team_launcher.current_user_name()).strip()
    return bool(worker_account and worker_account != owner_account)


def _proxy_command(
    config: team_launcher.ProjectConfig,
    role_name: str | None,
    role_status: Mapping[str, Any],
    *,
    observer: bool = False,
) -> tuple[str, str]:
    if role_name is None:
        message = f"{config.project}: display slot hidden"
        return str(Path.home()), _status_command(message)
    role = _role_by_name(config, role_name)
    if role is not None and role_status.get("live"):
        recovery = _recovery_instruction(config, role_name)
        message = f"{config.project}: {role_name} disconnected; use `{recovery}`"
        attach = shlex.join(worker_attach_argv(config, role, observer=observer))
        script = f"{attach}; {_status_script(message)}"
        return role.workdir, shlex.join(["sh", "-lc", script])
    state = role_status.get("state", "unavailable")
    recovery = " (resume available)" if role_status.get("resumable") else ""
    message = (
        f"{config.project}: {role_name} is {state}{recovery}; "
        f"use `{_recovery_instruction(config, role_name)}`"
    )
    return role.workdir if role is not None else str(Path.home()), _status_command(message)


def _recovery_instruction(config: team_launcher.ProjectConfig, role_name: str) -> str:
    """The command the person reading this slot can actually run.

    A disconnected DIRECTOR slot is the catch-22 this exists for: the screen
    used to name `switchyard present <project> recover director`, which is
    gated to the Director pane that had just gone away, so the desktop operator
    reading it was told to run the one command they could not (SYRD-239). They
    get the operator entry point instead.

    Every other slot keeps the Director's own command, because for those the
    Director is still attached and it is still their call.
    """
    if (role_name or "").strip().lower() == DIRECTOR_ROLE:
        return f"switchyard recover-display {config.project}"
    return f"switchyard present {config.project} recover {role_name}"


def _status_script(message: str) -> str:
    """A slot's status screen: the message at the top, redrawn on every resize.

    Printed once and left, a message is reflowed by tmux when its slot is
    resized, and tmux keeps the cursor row -- so a slot narrowed after the
    message was drawn pushes its first line into history, and the person reads
    "over app`" with the recovery command scrolled away. Slots follow their
    window now, so they are resized whenever it is (SYRD-221 UAT, test12).
    Trapping WINCH redraws it instead; checked under bash and dash, Ubuntu's
    /bin/sh, with one sleeping child whatever the number of resizes.
    """
    return (
        f"show() {{ printf '\\033[H\\033[2J%s\\n' {shlex.quote(message)}; }}; trap show WINCH; show; "
        "while :; do sleep 2147483647 & pid=$!; wait $pid; kill $pid 2>/dev/null; done"
    )


def _status_command(message: str) -> str:
    return shlex.join(["sh", "-lc", _status_script(message)])


def _recovery_hook_index(project: str, slot: int) -> int:
    digest = hashlib.blake2s(f"{project}:{slot}".encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(digest, "big") & 0x7FFFFFFF


def _configure_recovery_hook(
    config: team_launcher.ProjectConfig,
    slot: int,
    role_name: str | None,
    role_status: Mapping[str, Any],
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    hook = f"session-closed[{_recovery_hook_index(config.project, slot)}]"
    if role_name is None or not role_status.get("live"):
        proc = runner(["tmux", "set-hook", "-gu", hook])
    else:
        role = _role_by_name(config, role_name)
        assert role is not None
        message = (
            f"{config.project}: {role_name} disconnected; use "
            f"`{_recovery_instruction(config, role_name)}`"
        )
        respawn = shlex.join(
            [
                "respawn-pane", "-k", "-t",
                _exact_tmux_target(f"{display_session_name(config.project, slot)}:0.0"),
                "-c", role.workdir, _status_command(message),
            ]
        )
        condition = f"#{{==:#{{hook_session_name}},{role.tmux_session}}}"
        hook_command = shlex.join(["if-shell", "-F", condition, respawn, ""])
        proc = runner(["tmux", "set-hook", "-g", hook, hook_command])
    if proc.returncode != 0:
        raise RuntimeError(f"tmux could not configure recovery hook for slot {slot} (exit {proc.returncode})")


def display_slot_terminal_title_commands(
    config: team_launcher.ProjectConfig, session: str
) -> tuple[list[str], ...]:
    """What a display slot tells the terminal around it to call the window.

    The project, never the slot. A display slot is a frame around one worker,
    and its pane is attached to by the terminal the User is looking at -- so
    whatever this session sends as a terminal title becomes that window's
    caption. It used to send `<project> slot <n>: <role>`, which is how the
    Konsole caption came to read `syrd slot 1: director` as soon as a pane was
    focused: the pane wrapper had already named the window after the project,
    and tmux overwrote it the moment its client attached (SYRD-141).

    Sent rather than silenced. Turning `set-titles` off would leave whatever
    was last set standing, which is right today only because the wrapper set it
    first; sending the project name means every redraw re-asserts it, so a
    program inside the pane that names the terminal is corrected rather than
    obeyed.

    The slot's own label is not lost: it is on `status-left` and in the
    `@switchyard_slot`/`@switchyard_role` pane options, which is where the
    viewer reads it for its pane borders.
    """
    target = _exact_tmux_target(f"{session}:")
    return (
        ["tmux", "set-option", "-t", target, "set-titles", "on"],
        [
            "tmux", "set-option", "-t", target,
            "set-titles-string", team_launcher.project_window_title(config),
        ],
    )


def _configure_display_session(
    config: team_launcher.ProjectConfig,
    slot: int,
    role_name: str | None,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    presentation_ttys: set[str] | None = None,
) -> None:
    session = display_session_name(config.project, slot)
    status = _role_status(config, role_name, runner=runner) if role_name else {"state": "hidden", "live": False}
    role = _role_by_name(config, role_name or "")
    if role is not None and status.get("live"):
        if _proxy_crosses_account(config, role):
            # The desktop-facing display session is already locked, but its
            # pane is a nested client of the role's tmux server.  Lock that
            # transport too before attaching: otherwise the same keystrokes
            # could reach the inner prefix and open a role-account shell or
            # tmux command prompt (SYRD-66).
            for lock_args in display_lock_commands(role.tmux_session):
                lock_proc = runner(lock_args)
                if lock_proc.returncode != 0:
                    raise RuntimeError(
                        f"tmux could not secure the {role_name} proxy transport "
                        f"(exit {lock_proc.returncode})"
                    )
        detach_proc = runner(
            [
                "tmux", "set-option", "-t", _exact_tmux_target(f"{role.tmux_session}:"),
                "detach-on-destroy", "on",
            ]
        )
        if detach_proc.returncode != 0:
            raise RuntimeError(f"tmux could not configure recovery for {role_name} (exit {detach_proc.returncode})")
    observer = bool(
        role is not None
        and status.get("live")
        and _worker_has_independent_client(
            role,
            presentation_ttys=presentation_ttys if presentation_ttys is not None else set(),
            runner=runner,
        )
    )
    workdir, command = _proxy_command(config, role_name, status, observer=observer)
    if _session_exists(session, runner=runner):
        proc = runner(
            ["tmux", "respawn-pane", "-k", "-t", _exact_tmux_target(f"{session}:0.0"), "-c", workdir, command]
        )
    else:
        proc = runner(
            [
                "tmux", "new-session", "-d", "-x", str(team_launcher.DEFAULT_VIEWER_COLUMNS),
                "-y", str(team_launcher.DEFAULT_VIEWER_ROWS), "-s", session, "-c", workdir, command,
            ]
        )
    if proc.returncode != 0:
        raise RuntimeError(f"tmux could not update display slot {slot} (exit {proc.returncode})")
    label = role_name or "hidden"
    commands = (
        # Before anything else about the slot: a client may attach the moment
        # the session exists, and it must never be one that can drive the
        # owner's tmux server (SYRD-65 review).
        *display_lock_commands(session),
        ["tmux", "set-window-option", "-t", _exact_tmux_target(f"{session}:0"), "remain-on-exit", "on"],
        # The slot is a frame around a worker that draws its own status line.
        # Leaving this one on stacks two status bars in every presentation
        # client, so the label lives in the window title and pane options.
        ["tmux", "set-option", "-t", _exact_tmux_target(f"{session}:"), "status", "off"],
        [
            "tmux", "set-option", "-t", _exact_tmux_target(f"{session}:"),
            "status-left", f" slot {slot}: {label} ",
        ],
        *display_slot_terminal_title_commands(config, session),
        [
            "tmux", "set-option", "-p", "-t", _exact_tmux_target(f"{session}:0.0"),
            "@switchyard_project", config.project,
        ],
        [
            "tmux", "set-option", "-p", "-t", _exact_tmux_target(f"{session}:0.0"),
            "@switchyard_slot", str(slot),
        ],
        [
            "tmux", "set-option", "-p", "-t", _exact_tmux_target(f"{session}:0.0"),
            "@switchyard_role", label,
        ],
    )
    for args in commands:
        option_proc = runner(args)
        if option_proc.returncode != 0:
            raise RuntimeError(f"tmux could not label display slot {slot} (exit {option_proc.returncode})")
    _configure_recovery_hook(config, slot, role_name, status, runner=runner)
