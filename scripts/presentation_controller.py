"""Runtime presentation slots for persistent Switchyard role sessions.

The launcher projection supplies the desired role list. For project-account
runtimes, PostgreSQL's live assignment supplies the actual worker target. This
state records only which role a stable display slot should present. This
controller changes proxy panes without restarting or modifying worker sessions.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Collection, Mapping

from scripts import team_launcher
from scripts.presentation_display_session import (
    DIRECTOR_ROLE,
    DISPLAY_KEY_TABLE,
    _configure_display_session,
    _configure_recovery_hook,
    _proxy_client_flags,
    _proxy_command,
    _proxy_crosses_account,
    _recovery_hook_index,
    _recovery_instruction,
    _role_by_name,
    _role_status,
    _session_exists,
    _status_command,
    _status_script,
    _worker_has_independent_client,
    display_lock_commands,
    display_lock_options,
    display_session_name,
    display_slot_terminal_title_commands,
    worker_attach_argv,
)
from scripts.presentation_runtime_assignments import (
    _exact_tmux_target,
    _resolved_runtime_assignments,
    runtime_assignment_config,
    runtime_divergence_refusal,
)
from scripts.presentation_state_document import (
    PRESENTATION_HISTORY_LIMIT,
    PRESENTATION_SCHEMA,
    _complete_mapping,
    _configured_presentation,
    _locked_state,
    _read_state,
    _validated_mapping,
    _write_state,
    default_presentation_document,
    presentation_state_path,
    validate_presentation_document,
)
from scripts.presentation_viewer import (
    VIEWER_OBSERVER_CLIENT_FLAGS,
    _client_table,
    _clients_by_tty,
    _exact_target_args,
    _launch_viewer,
    _reconcile_viewer_observers,
    _session_pane_ttys,
    _viewer_frame_commands,
    _viewer_observer_attach,
    reconcile_viewer_observer_flags,
)
from scripts.presentation_window_launch import (
    VIEWER_ATTACH_TARGET,
    _hand_off_desktop_half,
    _launch_separate,
    display_attach_args,
    display_attach_args_for,
    presentation_layout_payload,
)
from scripts.presentation_window_visibility import (
    WINDOW_ATTACH_POLL_SECONDS,
    WINDOW_ATTACH_TIMEOUT_SECONDS,
    _detach_headless_presentation,
    _presentation_client_ttys,
    _session_client_ttys,
    _slot_visible,
    await_presentation_window,
    external_presentation_clients,
    unmapped_presentation_message,
)


def _exact_tmux_runner(
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> Callable[..., subprocess.CompletedProcess[Any]]:
    """Keep presentation recovery exact while it reuses legacy launch helpers."""
    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        exact_args = list(args)
        if exact_args and exact_args[0] == "tmux":
            for index, arg in enumerate(exact_args[:-1]):
                if arg == "-t":
                    target = exact_args[index + 1]
                    if (
                        exact_args[1] in {"set-option", "show-options"}
                        and "-p" not in exact_args
                        and ":" not in target
                    ):
                        target = f"{target}:"
                    exact_args[index + 1] = _exact_tmux_target(target)
        return runner(exact_args, **kwargs)

    return run


def _validate_role_namespace(config: team_launcher.ProjectConfig) -> None:
    for role in config.roles:
        target_session = role.target.split(":", 1)[0]
        if (
            not role.tmux_session.startswith(f"{config.project}-")
            or role.tmux_session != target_session
        ):
            raise SystemExit(
                f"switchyard: refusing foreign presentation target for {role.role}: "
                f"configured {role.target}"
            )


#: How an operator recovery is written in the presentation history. Prefixed
#: rather than bare, so it can never be mistaken for a role name.
OPERATOR_ACTOR_PREFIX = "operator:"


def operator_recovery_caller(
    config: team_launcher.ProjectConfig,
    environ: Mapping[str, str],
    *,
    grant_root: Path | None = None,
) -> str:
    """The desktop operator this run was authorized for, or "".

    The one bounded way a runtime presentation change happens without an
    attached Director pane. It is not a second way to be the Director: it
    authorizes exactly one action on exactly one slot, and everything it rests
    on was decided by root before this process started.

    `SWITCHYARD_TENANT_CONTROL_CALLER` is not a role the caller claims. The
    tenant-control bridge is root-owned, reached through one NOPASSWD grant
    naming that program, and it resolves the caller from SUDO_UID -- which the
    kernel sets -- before checking it against the authorized user in this
    tenant's root-owned grant file. This re-reads that same grant and requires
    the two to agree, so a name that does not match the tenant's registered
    operator authorizes nothing.

    What this is NOT is a security boundary against the tenant's own accounts.
    A modern tenant runs every role as the project account, so the account that
    could set this variable is the account that already owns these sessions --
    the same reason `_require_director` is an honesty gate rather than a
    barrier (SYRD-112). The authentication that matters happens at the sudoers
    boundary in the bridge; this is the tenant-side half agreeing with it.
    """
    caller = (environ.get(team_launcher.TENANT_CONTROL_CALLER_ENV) or "").strip()
    if not caller:
        return ""
    # `_tenant_control_grant` already answers {} for a grant that is missing,
    # unreadable, malformed, or that names another tenant -- so an empty
    # `authorized_user` is every one of those cases, and re-checking them here
    # would be unreachable code claiming to be a safeguard.
    grant = team_launcher._tenant_control_grant(config.project, root=grant_root)
    authorized = str(grant.get("authorized_user") or "").strip()
    if not authorized or caller != authorized:
        return ""
    return caller


def _require_director(
    config: team_launcher.ProjectConfig,
    environ: Mapping[str, str],
    *,
    operator_recovery: bool = False,
    grant_root: Path | None = None,
) -> str:
    """Who may change this project's presentation at runtime.

    `operator_recovery` is passed only by the one action that needs it: the
    recovery of a disconnected Director slot. Every other runtime presentation
    change stays Director-only, because every other one is an ordinary
    operation the attached Director can perform -- while this one is the
    action whose precondition is that the Director's slot is gone (SYRD-239).
    """
    actor = (environ.get("TICKET_BOARD_CALLER_ROLE") or environ.get("PGU_TICKET_BOARD_CALLER_ROLE") or "").strip().lower()
    operator = (
        operator_recovery_caller(config, environ, grant_root=grant_root)
        if operator_recovery and actor != DIRECTOR_ROLE
        else ""
    )
    if actor != DIRECTOR_ROLE and not operator:
        if operator_recovery:
            # The catch-22 this replaces: the screen told the operator to run a
            # command gated to the pane that had just disconnected. Name the
            # one they can actually run instead of the variable they were
            # previously left to work out and set by hand.
            raise SystemExit(
                "switchyard: recovering a disconnected Director slot needs either the "
                f"Director's own pane or {config.project}'s registered operator. Run "
                f"`switchyard recover-display {config.project}` from the desktop session "
                "that owns the screen"
            )
        raise SystemExit("switchyard: runtime presentation changes require TICKET_BOARD_CALLER_ROLE=director")
    # Checked for the operator too, and against the project this config is for.
    # The bridge pins the project from root-owned data, so this cannot normally
    # disagree -- and a recovery aimed at another tenant is exactly the thing
    # that must not work if it ever does.
    selected_project = (environ.get("TICKET_BOARD_PROJECT") or "").strip()
    if selected_project and selected_project != config.project:
        raise SystemExit(
            f"switchyard: refusing cross-project presentation change from {selected_project!r} to {config.project!r}"
        )
    if operator:
        # Named for what it is. The presentation history records this string,
        # and recording an operator's recovery as `director` would put one
        # party's action in another's name -- the same false attribution a
        # relayed decision is careful to avoid. A reader of the history can
        # tell the Director's own move from the recovery somebody else had to
        # make because the Director's pane was gone.
        return f"{OPERATOR_ACTOR_PREFIX}{operator}"
    return actor


def _session_owner_map(config: team_launcher.ProjectConfig) -> dict[str, str]:
    """tmux session name -> the account whose tmux server holds it.

    Role sessions live in their own role account's server; display and viewer
    sessions belong to the project owner (SYRD-39).
    """
    owners: dict[str, str] = {}
    for role in config.roles:
        account = team_launcher.role_run_as_user(config, role)
        if account:
            owners[role.tmux_session] = account
    return owners


def _tmux_target_session(args: list[str]) -> str:
    """The session a tmux invocation addresses, if any."""
    for flag in ("-t", "-s"):
        if flag in args:
            index = args.index(flag) + 1
            if index < len(args):
                target = str(args[index]).lstrip("=")
                return target.split(":", 1)[0]
    return ""


def _tmux_runner(
    config: team_launcher.ProjectConfig,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> Callable[..., subprocess.CompletedProcess[Any]]:
    """Route each tmux call to the account that owns the session it addresses.

    With one account per role there is no shared tmux server, so presentation
    reaches a role's session through the generated role-control interface
    (sudo -u <role account> tmux) rather than assuming ambient access. Display
    and viewer sessions stay with the project owner (SYRD-39).
    """
    owners = _session_owner_map(config)
    current_user = team_launcher.current_user_name()

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        account = config.run_as_user
        if args and args[0] == "tmux":
            session = _tmux_target_session(args)
            account = owners.get(session, config.run_as_user)
        if account and current_user != account:
            return team_launcher._owner_process_runner(owner_user=account, runner=runner)(args, **kwargs)
        return runner(args, **kwargs)

    return run


def presentation_window_attached(
    config: team_launcher.ProjectConfig,
    *,
    config_path: Path,
    state_path: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> bool:
    """Whether a terminal window is displaying this project's slots."""
    state_path = state_path or presentation_state_path(config, config_path=config_path)
    owner_runner = _tmux_runner(config, runner)
    slot_count = _slot_count(config, config_path, state_path)
    return bool(external_presentation_clients(config, slot_count, runner=owner_runner))


def _apply_mapping(
    config: team_launcher.ProjectConfig,
    mapping: Mapping[str, str | None],
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    presentation_ttys = _presentation_client_ttys(config, len(mapping), runner=runner)
    for slot in range(len(mapping)):
        _configure_display_session(
            config, slot, mapping[str(slot)], runner=runner, presentation_ttys=presentation_ttys
        )
    viewer = team_launcher.viewer_session_for_project(config.project)
    if _session_exists(viewer, runner=runner):
        for args in _viewer_frame_commands(viewer):
            proc = runner(args)
            if proc.returncode != 0:
                raise RuntimeError(f"tmux could not configure viewer {viewer} (exit {proc.returncode})")
        _reconcile_viewer_observers(config, runner=runner)
        for slot in range(len(mapping)):
            label = mapping[str(slot)] or "hidden"
            for option, value in (("@switchyard_slot", str(slot)), ("@switchyard_role", label)):
                proc = runner(
                    ["tmux", "set-option", "-p", "-t", _exact_tmux_target(f"{viewer}:0.{slot}"), option, value]
                )
                if proc.returncode != 0:
                    raise RuntimeError(f"tmux could not label viewer slot {slot} (exit {proc.returncode})")


def _mutate(
    config: team_launcher.ProjectConfig,
    *,
    config_path: Path,
    state_path: Path,
    actor: str,
    action: str,
    detail: Mapping[str, Any],
    transform: Callable[[dict[str, Any]], None],
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    file_runner: Callable[..., subprocess.CompletedProcess[Any]] | None = None,
) -> dict[str, Any]:
    with _locked_state(state_path, config=config, runner=file_runner or runner):
        state = _read_state(state_path, config=config, config_path=config_path)
        before = copy.deepcopy(state)
        transform(state)
        state["slots"] = _validated_mapping(
            state["slots"], config=config, slot_count=state["slot_count"], allow_removed=True
        )
        state["revision"] += 1
        history = [*state["history"], {
            "revision": state["revision"],
            "actor": actor,
            "action": action,
            "detail": dict(detail),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }]
        state["history"] = history[-PRESENTATION_HISTORY_LIMIT:]
        try:
            _apply_mapping(config, state["slots"], runner=runner)
            _write_state(config, state_path, state, runner=file_runner or runner)
        except Exception as exc:
            try:
                _apply_mapping(config, before["slots"], runner=runner)
            except Exception as rollback_exc:
                raise SystemExit(
                    f"switchyard: presentation update failed ({exc}); rollback also failed ({rollback_exc})"
                ) from exc
            raise SystemExit(f"switchyard: presentation update failed; prior mapping restored: {exc}") from exc
        return state


#: How long a respawned proxy has to become a client of its worker. Its attach
#: is one local tmux client starting; seconds, not minutes.
RECOVERY_ATTACH_TIMEOUT_SECONDS = 5.0
RECOVERY_ATTACH_POLL_SECONDS = 0.1


def _live_display_labels(
    config: team_launcher.ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> dict[int, str | None]:
    """Every live display session of this project, and the role it is showing.

    Read from the sessions themselves -- `<project>-display-<n>` on the owner's
    server and the `@switchyard_role` each one was last configured with -- so
    it answers for the window that is actually open, whatever the stored
    mapping currently derives.
    """
    listing = runner(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    if listing.returncode != 0:
        return {}
    pattern = re.compile(rf"^{re.escape(config.project)}-display-(\d+)$")
    labels: dict[int, str | None] = {}
    for name in str(getattr(listing, "stdout", "") or "").splitlines():
        match = pattern.match(name.strip())
        if not match:
            continue
        slot = int(match.group(1))
        label = runner(
            [
                "tmux", "display-message", "-p", "-t",
                _exact_tmux_target(f"{name.strip()}:0.0"), "#{@switchyard_role}",
            ],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        value = str(getattr(label, "stdout", "") or "").strip() if label.returncode == 0 else ""
        labels[slot] = None if value in {"", "hidden"} else value
    return labels


def _live_role_display_slots(
    config: team_launcher.ProjectConfig,
    role: team_launcher.RoleConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> tuple[dict[int, str | None], list[int]]:
    """The live displays, and which of them show ``role`` -- or a refusal."""
    live = _live_display_labels(config, runner=runner)
    slots = sorted(slot for slot, label in live.items() if label == role.role)
    if not slots:
        showing = ", ".join(
            f"slot {slot}: {label or 'nothing'}" for slot, label in sorted(live.items())
        ) or "none"
        raise SystemExit(
            f"switchyard: no live display of {config.project} is showing {role.role} "
            f"(its display sessions show: {showing}), so there is no {role.role} display to "
            "recover; nothing was changed"
        )
    return live, slots


def _recover_role_display(
    config: team_launcher.ProjectConfig,
    role: team_launcher.RoleConfig,
    *,
    config_path: Path,
    state_path: Path,
    actor: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    file_runner: Callable[..., subprocess.CompletedProcess[Any]] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    timeout: float = RECOVERY_ATTACH_TIMEOUT_SECONDS,
    poll: float = RECOVERY_ATTACH_POLL_SECONDS,
) -> dict[str, Any]:
    """Reattach the slots showing ``role`` -- and nothing else -- then prove it.

    This used to be `_mutate` with a no-op transform, which re-applies the
    WHOLE mapping: every slot's proxy was killed and respawned to recover one.
    On mefp that left Ops -- which had been fine -- on an inert "display slot
    hidden" screen, while the command reported every slot connected from their
    labels (SYRD-239 live UAT).

    Now only the live display sessions SHOWING this role are reconfigured --
    found from the sessions themselves, not from the stored mapping, which can
    name different slots than the open window shows. Every other slot, the
    viewer's layout and the mapping itself are left exactly as they are.
    Success is reported only once each of those slots is proven to be a live
    client of the worker. Otherwise it refuses, and nothing is recorded as a
    recovery that did not happen.
    """
    with _locked_state(state_path, config=config, runner=file_runner or runner):
        state = _read_state(state_path, config=config, config_path=config_path)
        # Which display the operator is looking at is a fact about the LIVE
        # display sessions, not about the stored mapping. While the layout is
        # "default", `_read_state` re-derives the mapping from the current
        # config on every read, so after a role set changes it can name a
        # different slot than the one the open window is showing: on mefp it
        # put the Director in slot 1 and Ops in slot 5 of 6, while the
        # four-pane window still showed display 0-3 as they were opened. The
        # label on each live display session is what it is showing.
        live, slots = _live_role_display_slots(config, role, runner=runner)
        presentation_ttys = _presentation_client_ttys(
            config, max(max(live) + 1, state["slot_count"]), runner=runner
        )
        for slot in slots:
            _configure_display_session(
                config, slot, role.role, runner=runner, presentation_ttys=presentation_ttys
            )
        # Flags only: a viewer client's ignore-size. It respawns nothing.
        _reconcile_viewer_observers(config, runner=runner)
        deadline = monotonic() + max(0.0, timeout)
        pending = list(slots)
        while True:
            pending = [
                slot for slot in pending if not _proxy_attached(config, slot, role, runner=runner)
            ]
            if not pending or monotonic() >= deadline:
                break
            sleep(poll)
        if pending:
            raise SystemExit(
                f"switchyard: {role.role}'s display was respawned but slot(s) "
                f"{', '.join(str(slot) for slot in pending)} did not attach to its worker "
                f"{role.tmux_session} within {timeout:g}s. It was not recovered; no other slot "
                "was touched and nothing was recorded"
            )
        # Attached is not the same as seen. On mefp the Director's WINDOW pane
        # -- the Konsole tab that was a client of its display slot -- had itself
        # exited, so a display proxied perfectly to the worker was on no screen
        # at all. Success means a window is showing it (SYRD-239 live UAT).
        #
        # Only when there IS a window. A presentation no window is attached to
        # at all -- headless, or before anyone opens one -- has nothing to be
        # visible on, and refusing there would only make `present recover`
        # unusable on it; its report already says `window_attached: false`.
        # What made mefp wrong was a window showing three panes while the
        # Director's display was on none of them.
        count = max(max(live) + 1, state["slot_count"])
        own = _presentation_client_ttys(config, count, runner=runner)
        windowed = bool(external_presentation_clients(config, count, runner=runner))
        unseen = [
            slot for slot in slots
            if windowed and not _slot_visible(config, slot, own, runner=runner)
        ]
        if unseen:
            gui_user = team_launcher.presentation_gui_user(config)
            reopen = "; ".join(
                shlex.join(display_attach_args_for(
                    config.project, slot, owner=config.run_as_user or "", gui_user=gui_user,
                ))
                for slot in unseen
            )
            raise SystemExit(
                f"switchyard: {role.role} is attached to its worker again in display slot(s) "
                f"{', '.join(str(slot) for slot in unseen)}, but no window is showing "
                f"{'it' if len(unseen) == 1 else 'them'} -- the window pane for that slot has "
                f"closed. It is not visible, so this is not reported as a recovery, and no other "
                f"slot was touched. To show it, run this in a terminal on the desktop: {reopen}"
            )
        state["revision"] += 1
        history = [*state["history"], {
            "revision": state["revision"],
            "actor": actor,
            "action": "recover",
            "detail": {"role": role.role, "slots": slots},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }]
        state["history"] = history[-PRESENTATION_HISTORY_LIMIT:]
        _write_state(config, state_path, state, runner=file_runner or runner)
        return state


def _actual_proxy_role(
    config: team_launcher.ProjectConfig,
    slot: int,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> tuple[str | None, str]:
    session = display_session_name(config.project, slot)
    if not _session_exists(session, runner=runner):
        return None, "disconnected"
    proc = runner(
        [
            "tmux", "display-message", "-p", "-t", _exact_tmux_target(f"{session}:0.0"),
            "#{@switchyard_role}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if proc.returncode != 0:
        return None, "failed"
    actual = str(getattr(proc, "stdout", "") or "").strip() or None
    # The label is what the slot was last TOLD to show, set right after the
    # respawn whether or not the attach inside it lived. Reporting it as
    # "connected" is how a recovery claimed a Director and an Ops slot were
    # connected while one showed nothing and the other an inert "display slot
    # hidden" screen (SYRD-239 live UAT). So the state is measured.
    if actual is None or actual == "hidden":
        return actual, "hidden"
    role = _role_by_name(config, actual)
    if role is None or not _proxy_attached(config, slot, role, runner=runner):
        return actual, "detached"
    return actual, "connected"


def _proxy_attached(
    config: team_launcher.ProjectConfig,
    slot: int,
    role: team_launcher.RoleConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> bool:
    """Is this display slot's proxy a live client of ``role``'s worker session?

    Proven from both ends rather than from a label: the slot's pane is alive,
    and its terminal is one of the terminals the worker's own session lists as
    a client. The proxy is a nested `tmux attach` running in that pane, so its
    client terminal IS the pane's terminal; nothing else puts that terminal in
    the worker's client list.
    """
    pane = _exact_tmux_target(f"{display_session_name(config.project, slot)}:0.0")
    dead = runner(
        ["tmux", "display-message", "-p", "-t", pane, "#{pane_dead}"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    if dead.returncode != 0 or str(getattr(dead, "stdout", "") or "").strip() != "0":
        return False
    tty = runner(
        ["tmux", "display-message", "-p", "-t", pane, "#{pane_tty}"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    pane_tty = str(getattr(tty, "stdout", "") or "").strip()
    if tty.returncode != 0 or not pane_tty:
        return False
    clients = runner(
        ["tmux", "list-clients", "-t", _exact_tmux_target(role.tmux_session), "-F", "#{client_tty}"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    if clients.returncode != 0:
        return False
    return pane_tty in {line.strip() for line in str(getattr(clients, "stdout", "") or "").splitlines()}


def slots_showing_role(
    config: team_launcher.ProjectConfig,
    *,
    config_path: Path,
    role_name: str,
    state_path: Path | None = None,
) -> tuple[int, ...]:
    """Display slots currently mapped to ``role_name``, in slot order.

    A detached role, or one nobody is showing, yields an empty tuple; callers
    use that to distinguish "nothing to reconnect" from "reconnect failed".
    """
    state_path = state_path or presentation_state_path(config, config_path=config_path)
    state = _read_state(state_path, config=config, config_path=config_path)
    return tuple(
        slot
        for slot in range(state["slot_count"])
        if state["slots"].get(str(slot)) == role_name
    )


def reconnect_role_slots(
    config: team_launcher.ProjectConfig,
    *,
    config_path: Path,
    role_name: str,
    state_path: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> tuple[int, ...]:
    """Re-attach the display slots showing ``role_name`` to its live session.

    A slot's proxy is an attach to a worker session. When that session is
    replaced -- a runtime switch, a crash and restart -- the proxy's own
    session-closed hook parks it on a recovery message, and nothing re-attaches
    it afterwards: the operator is left looking at a blank pane while the
    replacement worker runs perfectly well behind it.

    Call this only once the replacement session is proven live. Reconnecting
    into the gap between kill and start attaches the proxy to nothing, which
    parks it again for the same reason.
    """
    config = runtime_assignment_config(config)
    _validate_role_namespace(config)
    state_path = state_path or presentation_state_path(config, config_path=config_path)
    owner_runner = _tmux_runner(config, runner)
    slots = slots_showing_role(config, config_path=config_path, role_name=role_name, state_path=state_path)
    if not slots:
        return ()
    presentation_ttys = _presentation_client_ttys(config, _slot_count(config, config_path, state_path), runner=owner_runner)
    for slot in slots:
        _configure_display_session(
            config, slot, role_name, runner=owner_runner, presentation_ttys=presentation_ttys
        )
    _reconcile_viewer_observers(config, runner=owner_runner)
    return slots


def _slot_count(config: team_launcher.ProjectConfig, config_path: Path, state_path: Path) -> int:
    return int(_read_state(state_path, config=config, config_path=config_path)["slot_count"])


def presentation_report(
    config: team_launcher.ProjectConfig,
    *,
    config_path: Path,
    state_path: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> dict[str, Any]:
    config = runtime_assignment_config(config)
    _validate_role_namespace(config)
    state_path = state_path or presentation_state_path(config, config_path=config_path)
    owner_runner = _tmux_runner(config, runner)
    state = _read_state(state_path, config=config, config_path=config_path)
    visible_names = {role for role in state["slots"].values() if role}
    slots = []
    for slot in range(state["slot_count"]):
        desired = state["slots"][str(slot)]
        actual, client_state = _actual_proxy_role(config, slot, runner=owner_runner)
        role_status = _role_status(config, desired, runner=owner_runner) if desired else None
        slots.append({
            "slot": slot,
            "desired_role": desired,
            "actual_role": actual,
            "client_state": client_state,
            "focused": slot == state["focused_slot"],
            "worker": role_status,
        })
    hidden_roles = [
        _role_status(config, role.role, runner=owner_runner)
        for role in config.roles
        if role.role not in visible_names
    ]
    # Per-slot `client_state` answers "is this slot proxying its worker", which
    # a report can answer yes to for every slot while the whole presentation is
    # invisible. The window is a separate fact and is reported as one (SYRD-65).
    external = sorted(external_presentation_clients(config, state["slot_count"], runner=owner_runner))
    return {
        "schema": PRESENTATION_SCHEMA,
        "project": config.project,
        "revision": state["revision"],
        "active_layout": state["active_layout"],
        "focused_slot": state["focused_slot"],
        "state_path": str(state_path),
        "slots": slots,
        "hidden_roles": hidden_roles,
        "window_attached": bool(external),
        "external_clients": external,
    }


def presentation_enabled(config: team_launcher.ProjectConfig, *, config_path: Path) -> bool:
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(raw, dict) and (
        isinstance(raw.get("presentation"), dict)
        or presentation_state_path(config, config_path=config_path).exists()
    )


def reconnect_display_slots(
    config: team_launcher.ProjectConfig,
    *,
    config_path: Path,
    state_path: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> None:
    """Re-point the existing display slots at the current worker sessions.

    The slots survive a worker being replaced; what has to change is which
    session each one proxies. This is deliberately not launch_presentation:
    that opens a window, and opening a second one is how a tenant ends up with
    two six-pane windows and two status bars (SYRD-45).
    """
    _validate_role_namespace(config)
    state_path = state_path or presentation_state_path(config, config_path=config_path)
    owner_runner = _tmux_runner(config, runner)
    with _locked_state(state_path, config=config, runner=runner):
        state = _read_state(state_path, config=config, config_path=config_path)
        _apply_mapping(config, state["slots"], runner=owner_runner)
        _write_state(config, state_path, state, runner=runner)


def launch_presentation(
    config: team_launcher.ProjectConfig,
    *,
    config_path: Path,
    layout: str,
    state_path: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    process_launcher: Callable[..., Any] | None = None,
    assignment_wait_seconds: float = 0.0,
    print_func: Callable[[str], None] | None = None,
    unstarted: Collection[str] = (),
) -> int:
    """Restore saved slots during ordinary project launch without changing workers."""
    config = runtime_assignment_config(
        config, wait_seconds=assignment_wait_seconds, print_func=print_func, unstarted=unstarted
    )
    _validate_role_namespace(config)
    state_path = state_path or presentation_state_path(config, config_path=config_path)
    owner_runner = _tmux_runner(config, runner)
    with _locked_state(state_path, config=config, runner=runner):
        state = _read_state(state_path, config=config, config_path=config_path)
        _apply_mapping(config, state["slots"], runner=owner_runner)
        _write_state(config, state_path, state, runner=runner)
    if layout == "viewer":
        _launch_viewer(config, state, runner=owner_runner)
    elif layout == "separate":
        # Deliberately not derived from the presentation state path. That
        # path is the tenant's own project-state directory, 0700 and owned by
        # the project owner, so a layout written beside it is one the desktop
        # account cannot open however it is chowned -- Konsole reported "A
        # problem occurred when loading the Layout" and showed a blank window.
        # Where the layout goes is the one question
        # `desktop_presentation_layout_path` exists to answer, and bootstrap
        # already asks it; this is the same launch (SYRD-90).
        _launch_separate(
            config,
            state,
            config_path=config_path,
            runner=runner,
            process_launcher=process_launcher,
        )
    else:
        raise SystemExit(f"switchyard: unsupported presentation launch layout {layout!r}")
    return 0


def stop_presentation(
    config: team_launcher.ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> int:
    """Stop stable display sessions before their worker sessions are stopped."""
    owner_runner = _tmux_runner(config, runner)
    exit_code = 0
    for slot in range(team_launcher.MAX_VISIBLE_PANES_PER_WINDOW):
        session = display_session_name(config.project, slot)
        if not _session_exists(session, runner=owner_runner):
            continue
        identity = owner_runner(
            [
                "tmux", "display-message", "-p", "-t", _exact_tmux_target(f"{session}:0.0"),
                "#{@switchyard_project}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        if identity.returncode != 0 or str(getattr(identity, "stdout", "") or "").strip() != config.project:
            continue
        hook = f"session-closed[{_recovery_hook_index(config.project, slot)}]"
        hook_proc = owner_runner(["tmux", "set-hook", "-gu", hook])
        if hook_proc.returncode != 0:
            exit_code = exit_code or int(hook_proc.returncode)
        proc = owner_runner(
            ["tmux", "kill-session", "-t", _exact_tmux_target(session)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        if proc.returncode != 0:
            exit_code = exit_code or int(proc.returncode)
            print_func(f"failed to stop display slot {slot}: {session}")
        else:
            print_func(f"stopped display slot {slot}: {session}")
    return exit_code


def presentation_action(
    config: team_launcher.ProjectConfig,
    *,
    config_path: Path,
    action: str,
    role_name: str | None = None,
    slot: int | None = None,
    other_slot: int | None = None,
    layout: str = "default",
    state_path: Path | None = None,
    environ: Mapping[str, str] = os.environ,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    process_launcher: Callable[..., Any] | None = None,
    window_timeout: float = WINDOW_ATTACH_TIMEOUT_SECONDS,
    window_poll: float = WINDOW_ATTACH_POLL_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    proc_root: Path | None = None,
    assignment_opener: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    # Narrow on purpose: the operator path opens for the recovery of the
    # DIRECTOR's slot and nothing else. A disconnected app or ops slot is still
    # the Director's to recover, because the Director is still there to do it.
    operator_recovery = action == "recover" and (role_name or "").strip().lower() == DIRECTOR_ROLE
    try:
        config = runtime_assignment_config(config, opener=assignment_opener)
    except SystemExit as refusal:
        # Said precisely only to a caller who could have acted: anyone else gets
        # the refusal unchanged, and no probe runs on their behalf.
        try:
            _require_director(config, environ, operator_recovery=operator_recovery)
        except SystemExit:
            raise refusal from None
        precise = runtime_divergence_refusal(
            config, opener=assignment_opener, runner=_tmux_runner(config, runner),
            proc_root=proc_root,
        )
        if precise:
            raise SystemExit(precise) from None
        raise
    _validate_role_namespace(config)
    actor = _require_director(config, environ, operator_recovery=operator_recovery)
    owner_runner = _tmux_runner(config, runner)
    state_path = state_path or presentation_state_path(config, config_path=config_path)

    def require_slot(state: Mapping[str, Any], candidate: int | None) -> int:
        if candidate is None or candidate < 0 or candidate >= state["slot_count"]:
            raise SystemExit(f"switchyard: slot must be within 0..{state['slot_count'] - 1}")
        return candidate

    if action == "show":
        if not role_name or _role_by_name(config, role_name) is None:
            raise SystemExit(f"switchyard: unknown role {role_name!r}")

        def transform(state: dict[str, Any]) -> None:
            selected = require_slot(state, slot)
            for key, value in state["slots"].items():
                if value == role_name:
                    state["slots"][key] = None
            state["slots"][str(selected)] = role_name
            state["active_layout"] = "custom"

        return _mutate(config, config_path=config_path, state_path=state_path, actor=actor, action=action,
                       detail={"role": role_name, "slot": slot}, transform=transform, runner=owner_runner,
                       file_runner=runner)
    if action == "hide":
        def transform(state: dict[str, Any]) -> None:
            selected = require_slot(state, slot)
            state["slots"][str(selected)] = None
            state["active_layout"] = "custom"

        return _mutate(config, config_path=config_path, state_path=state_path, actor=actor, action=action,
                       detail={"slot": slot}, transform=transform, runner=owner_runner, file_runner=runner)
    if action == "swap":
        def transform(state: dict[str, Any]) -> None:
            first = require_slot(state, slot)
            second = require_slot(state, other_slot)
            state["slots"][str(first)], state["slots"][str(second)] = (
                state["slots"][str(second)], state["slots"][str(first)]
            )
            state["active_layout"] = "custom"

        return _mutate(config, config_path=config_path, state_path=state_path, actor=actor, action=action,
                       detail={"slot_a": slot, "slot_b": other_slot}, transform=transform, runner=owner_runner,
                       file_runner=runner)
    if action == "restore":
        def transform(state: dict[str, Any]) -> None:
            if layout not in state["layouts"]:
                raise SystemExit(f"switchyard: unknown presentation layout {layout!r}")
            state["slots"] = copy.deepcopy(state["layouts"][layout])
            state["active_layout"] = layout

        return _mutate(config, config_path=config_path, state_path=state_path, actor=actor, action=action,
                       detail={"layout": layout}, transform=transform, runner=owner_runner, file_runner=runner)
    if action == "focus":
        with _locked_state(state_path, config=config, runner=runner):
            state = _read_state(state_path, config=config, config_path=config_path)
            selected = require_slot(state, slot)
            prior = state["focused_slot"]
            viewer = team_launcher.viewer_session_for_project(config.project)
            viewer_exists = _session_exists(viewer, runner=owner_runner)
            if viewer_exists:
                proc = owner_runner(
                    ["tmux", "select-pane", "-t", _exact_tmux_target(f"{viewer}:0.{selected}")]
                )
                if proc.returncode != 0:
                    raise SystemExit(f"switchyard: viewer focus failed; prior focus preserved (exit {proc.returncode})")
            state["focused_slot"] = selected
            state["revision"] += 1
            state["history"] = [*state["history"], {
                "revision": state["revision"], "actor": actor, "action": action,
                "detail": {"slot": selected}, "timestamp": datetime.now(timezone.utc).isoformat(),
            }][-PRESENTATION_HISTORY_LIMIT:]
            try:
                _write_state(config, state_path, state, runner=runner)
            except Exception as exc:
                if viewer_exists:
                    owner_runner(
                        ["tmux", "select-pane", "-t", _exact_tmux_target(f"{viewer}:0.{prior}")]
                    )
                raise SystemExit(f"switchyard: focus state write failed; prior focus restored: {exc}") from exc
            return state
    if action == "bootstrap":
        state = _mutate(
            config, config_path=config_path, state_path=state_path, actor=actor, action=action,
            detail={"layout": layout}, transform=lambda _state: None, runner=owner_runner, file_runner=runner,
        )
        if layout == "viewer":
            _launch_viewer(config, state, runner=owner_runner)
        elif layout == "separate":
            _launch_separate(config, state, config_path=config_path, runner=runner, process_launcher=process_launcher)
        else:
            raise SystemExit("switchyard: bootstrap layout must be viewer or separate")
        # Bootstrap is documented as creating a presentation window, and it is
        # what an operator reaches for when the tenant has gone blank. Returning
        # zero because the nested sessions came up is the answer that sent a
        # recovered-looking project back to a user who could still see nothing;
        # so the window is proved, and an unproved one is a failure with the
        # command that does work (SYRD-65).
        attached = await_presentation_window(
            config, state["slot_count"], runner=owner_runner, timeout=window_timeout,
            poll=window_poll, monotonic=monotonic, sleep=sleep,
        )
        # For the layout that opens a terminal, the terminal itself has to still
        # be there: it is the only evidence that does not go through tmux, and
        # the one that failed here aborted on a layout file it could not read,
        # which no client count would have shown (SYRD-65).
        windowed = layout != team_launcher.LAYOUT_MODE_SEPARATE or bool(
            team_launcher.presentation_window_processes(
                config, config_path=config_path, proc_root=proc_root
            )
        )
        if not attached or not windowed:
            _detach_headless_presentation(config, state["slot_count"], runner=owner_runner)
            raise SystemExit(
                unmapped_presentation_message(
                    config,
                    layout=layout,
                    control_user=team_launcher.resolve_control_user(
                        config.project, owner_user=config.run_as_user or ""
                    ),
                )
            )
        return state
    if action == "recover":
        # Recovery is a new worker launch entry. Reuse the launcher's desktop
        # readiness validation and its prepared per-role environment before a
        # start/resume attempt; presentation must not invent a parallel setup
        # or consent path.
        prepared_config = team_launcher.prepare_project_desktop(config, runner=runner)
        _validate_role_namespace(prepared_config)
        role = _role_by_name(prepared_config, role_name or "")
        if role is None:
            raise SystemExit(f"switchyard: unknown role {role_name!r}")
        prepared_owner_runner = _tmux_runner(prepared_config, runner)
        # Before the worker is touched: a recovery with no display to recover
        # must not start or restart anything on its way to refusing.
        _live_role_display_slots(prepared_config, role, runner=prepared_owner_runner)
        recovery_runner = _exact_tmux_runner(prepared_owner_runner)
        result = team_launcher.ensure_visible_role_session_for_viewer(
            role,
            mode="attach-or-start",
            # The role's own store, as launch and start use: the project-wide
            # one handed Hermes another home (SYRD-563).
            session_dir=team_launcher.role_session_dir(prepared_config, role),
            pane_state_dir=team_launcher.default_pane_state_dir_for_user(
                prepared_config.run_as_user, project=prepared_config.project
            ),
            bin_user=prepared_config.run_as_user,
            runner=recovery_runner,
        )
        if result != 0:
            raise SystemExit(f"switchyard: recovery failed for {role.role} (exit {result})")
        return _recover_role_display(
            prepared_config, role, config_path=config_path, state_path=state_path, actor=actor,
            runner=prepared_owner_runner, file_runner=runner, monotonic=monotonic, sleep=sleep,
        )
    raise SystemExit(f"switchyard: unknown presentation action {action!r}")


def attachable_roles(
    config: team_launcher.ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> list[dict[str, Any]]:
    """Every configured role and whether a terminal could attach to it now.

    Named by role, in configuration order. No slot number and no session name:
    those are what an operator should not have to know (SYRD-76).
    """
    owner_runner = _tmux_runner(config, runner)
    return [_role_status(config, role.role, runner=owner_runner) for role in config.roles]


def print_attachable_roles(
    report: Mapping[str, Any],
    *,
    json_output: bool = False,
    print_func: Callable[[str], None] = print,
) -> None:
    if json_output:
        print_func(json.dumps(report, indent=2, sort_keys=True))
        return
    project = report["project"]
    roles = report["roles"]
    if not roles:
        print_func(f"{project} has no configured roles")
        return
    print_func(f"{project} roles")
    for worker in roles:
        attachable = "attach" if worker["live"] else "-"
        resumable = " resumable" if worker.get("resumable") else ""
        print_func(f"  {worker['role']:<12} {worker['state']}{resumable} [{attachable}]")
    print_func(f"Attach with `switchyard attach {project} <role>`.")


def attach_role_command(
    config: team_launcher.ProjectConfig,
    *,
    role_name: str | None,
    json_output: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    attacher: Callable[[list[str]], int] | None = None,
    print_func: Callable[[str], None] = print,
) -> int:
    """Attach this terminal to a role's live worker, by project and role name.

    The registered assignment decides which session that is, so a replacement
    worker on a recovery target is reached by the same name as the original,
    and a slot number, Unix account or internal session name is never something
    the operator has to supply or see (SYRD-76).
    """
    config = runtime_assignment_config(config)
    _validate_role_namespace(config)
    owner_runner = _tmux_runner(config, runner)
    if not role_name:
        print_attachable_roles(
            {"project": config.project, "roles": attachable_roles(config, runner=runner)},
            json_output=json_output,
            print_func=print_func,
        )
        return 0
    wanted = role_name.strip()
    role = _role_by_name(config, wanted)
    if role is None:
        known = ", ".join(sorted(item.role for item in config.roles)) or "none"
        raise SystemExit(
            f"switchyard: {config.project} has no role {wanted!r}; configured roles: {known}"
        )
    status = _role_status(config, role.role, runner=owner_runner)
    if not status["live"]:
        resume = " Its session is resumable." if status["resumable"] else ""
        raise SystemExit(
            f"switchyard: {config.project} role {role.role} is {status['state']}, so there is "
            f"nothing to attach to.{resume} Use "
            f"`switchyard present {config.project} recover {role.role}` to start it."
        )
    if _proxy_crosses_account(config, role):
        # This caller is not the account the worker runs as. Attaching hands
        # them that session's key tables, so without this they could press
        # prefix-c for a shell as the worker's account -- the privileged parent
        # shell this command exists to avoid. Idempotent, and the same options
        # a display slot already applies to the same session (SYRD-76).
        for lock_args in display_lock_commands(role.tmux_session):
            lock = owner_runner(lock_args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            if lock.returncode != 0:
                detail = str(getattr(lock, "stderr", "") or "").strip()
                raise SystemExit(
                    f"switchyard: could not secure {config.project} role {role.role} before "
                    f"attaching, so it was not attached{': ' + detail if detail else ''}"
                )
    # Sizing follows whoever else is watching: alone, this terminal sizes the
    # worker so the pane fills it; alongside a display slot it must not resize
    # what that slot shows (SYRD-27).
    observer = _worker_has_independent_client(role, presentation_ttys=set(), runner=owner_runner)
    argv = worker_attach_argv(config, role, observer=observer)
    attach = attacher or (lambda command: subprocess.run(command).returncode)
    print_func(f"switchyard: attaching to {config.project} role {role.role}; detach with Ctrl-b d")
    status_code = attach(argv)
    if status_code != 0:
        raise SystemExit(
            f"switchyard: attaching to {config.project} role {role.role} failed (exit {status_code})"
        )
    print_func(f"switchyard: detached from {config.project} role {role.role}")
    return 0


def print_presentation_report(report: Mapping[str, Any], *, json_output: bool, print_func: Callable[[str], None] = print) -> None:
    if json_output:
        print_func(json.dumps(report, indent=2, sort_keys=True))
        return
    print_func(f"{report['project']} presentation revision {report['revision']} ({report['active_layout']})")
    external = report.get("external_clients") or []
    if report.get("window_attached"):
        print_func(f"  window: displayed by {', '.join(external)}")
    else:
        print_func("  window: NOT displayed by any terminal; the slots below have only headless clients")
    for item in report["slots"]:
        role = item["desired_role"] or "hidden"
        worker = item["worker"]
        worker_state = worker["state"] if worker else "hidden"
        actual = item["actual_role"] or "-"
        marker = "*" if item["focused"] else " "
        print_func(
            f"{marker} slot {item['slot']}: {role} worker={worker_state} "
            f"client={item['client_state']} actual={actual}"
        )
    for worker in report["hidden_roles"]:
        recovery = " resumable" if worker["resumable"] else ""
        print_func(f"  hidden: {worker['role']} worker={worker['state']}{recovery}")
