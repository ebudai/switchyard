"""Runtime presentation slots for persistent Switchyard role sessions.

The launcher projection supplies the desired role list. For project-account
runtimes, PostgreSQL's live assignment supplies the actual worker target. This
module persists only which role a stable display slot should present and changes
proxy panes without restarting or modifying worker sessions.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import shlex
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence
from urllib import error as urllib_error
from urllib import request as urllib_request

from scripts import team_launcher

PRESENTATION_SCHEMA = "switchyard.presentation.v1"
PRESENTATION_HISTORY_LIMIT = 100
DIRECTOR_ROLE = "director"
# The viewer aggregates display slots that other clients may already be showing
# at their own size, so its clients never contribute to slot geometry.  tmux
# still sizes a slot from such a client when it is that slot's only one.
VIEWER_OBSERVER_CLIENT_FLAGS = "ignore-size"
#: How long a freshly launched window is given to attach before we call the
#: launch unproven. A desktop terminal that is going to appear does so in well
#: under this; one that never appears must not be reported as a success.
WINDOW_ATTACH_TIMEOUT_SECONDS = 10.0
WINDOW_ATTACH_POLL_SECONDS = 0.25
#: The key table a display slot's clients are put in: one that has no bindings,
#: so every key falls through to the pane instead of reaching tmux. Naming a
#: table that was never created is deliberate and sufficient -- a lookup that
#: finds nothing is what makes the client incapable of a tmux command.
DISPLAY_KEY_TABLE = "switchyard-display"


def runtime_assignment_config(
    config: team_launcher.ProjectConfig,
    *,
    opener: Callable[[str], Any] | None = None,
    wait_seconds: float = 0.0,
    poll_seconds: float = team_launcher.RUNTIME_REGISTRATION_POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    print_func: Callable[[str], None] | None = None,
) -> team_launcher.ProjectConfig:
    """Resolve every shared-account worker from the board's current assignment.

    The launcher projection remains the desired role list, but it is not live
    routing evidence.  A replacement pane may use a recovery target, so display
    attachment must consume the same atomic row as notifications and write
    authority instead of reconstructing a conventional tmux name (SYRD-69).

    `wait_seconds` is for the one caller that has just started the panes it is
    asking about. Registration is the pane's own asynchronous work, so a launch
    that sampled once refused its own startup: testing journal 0032 stopped on
    a single role that registered seconds later. Waiting is bounded and
    announced, and every refusal below -- a foreign target, a runtime that does
    not match the projection -- still happens on the first reading, because
    those are not races (SYRD-162).
    """
    if not config.role_state_isolation:
        return config
    deadline = monotonic() + max(0.0, wait_seconds)
    announced = False
    while True:
        resolved, missing = _resolved_runtime_assignments(config, opener=opener)
        if not missing:
            return team_launcher.replace(config, roles=resolved)
        if monotonic() >= deadline:
            raise SystemExit(
                "switchyard: no live runtime assignment for configured role(s): "
                + ", ".join(sorted(missing))
            )
        if not announced:
            announced = True
            if print_func is not None:
                print_func(
                    f"switchyard: waiting up to {wait_seconds:g}s for "
                    + ", ".join(sorted(missing))
                    + " to register a runtime with the board"
                )
        sleep(poll_seconds)


def _resolved_runtime_assignments(
    config: team_launcher.ProjectConfig,
    *,
    opener: Callable[[str], Any] | None = None,
) -> tuple[list[team_launcher.RoleConfig], list[str]]:
    """One reading: the roles resolved from it, and the ones it does not name."""
    url = f"{config.board_url.rstrip('/')}/api/runtime-assignments"
    open_url = opener or (lambda target: urllib_request.urlopen(target, timeout=3))
    try:
        with open_url(url) as response:
            payload = json.load(response)
    except (OSError, ValueError, urllib_error.URLError) as exc:
        raise SystemExit(
            f"switchyard: cannot resolve {config.project} runtime assignments from {url}: {exc}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("project") != config.project:
        raise SystemExit("switchyard: runtime assignment response belongs to another project")
    if payload.get("authority_mode") != "process":
        raise SystemExit("switchyard: running board still uses legacy uid authority")
    assignments = payload.get("assignments")
    if not isinstance(assignments, dict):
        raise SystemExit("switchyard: runtime assignment response has no assignments object")
    resolved: list[team_launcher.RoleConfig] = []
    missing: list[str] = []
    for role in config.roles:
        assignment = assignments.get(role.role)
        if not isinstance(assignment, dict):
            if role.detached:
                resolved.append(role)
            else:
                missing.append(role.role)
            continue
        target = str(assignment.get("actual_target") or "").strip()
        runtime = str(assignment.get("runtime") or "").strip()
        if not target.split(":", 1)[0].startswith(f"{config.project}-"):
            raise SystemExit(
                f"switchyard: refusing foreign runtime assignment for {role.role}: {target!r}"
            )
        if not role.cli or team_launcher._command_name(role.cli[0]) != runtime:
            raise SystemExit(
                f"switchyard: runtime assignment for {role.role} is {runtime!r}, "
                "which differs from the launcher projection"
            )
        resolved.append(
            team_launcher.replace(
                role, target=target, tmux_session=target.split(":", 1)[0]
            )
        )
    return resolved, missing


def display_session_name(project: str, slot: int) -> str:
    return f"{project}-display-{slot}"


def _exact_tmux_target(target: str) -> str:
    """Disable tmux prefix matching for a named session, window, or pane."""
    return target if target.startswith("=") else f"={target}"


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


def presentation_state_path(config: team_launcher.ProjectConfig, *, config_path: Path) -> Path:
    return team_launcher.default_layout_output_path(config, config_path=config_path).parent / "presentation.json"


def _configured_presentation(config: team_launcher.ProjectConfig, config_path: Path) -> dict[str, Any]:
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    value = raw.get("presentation", {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SystemExit(f"switchyard: {config_path} presentation must be a JSON object")
    return value


def default_presentation_document(
    config: team_launcher.ProjectConfig,
    *,
    config_path: Path,
) -> dict[str, Any]:
    configured = _configured_presentation(config, config_path)
    default_mapping = {
        str(role.slot): role.role
        for role in config.roles
        if not role.detached and role.slot is not None
    }
    required_count = max((int(slot) for slot in default_mapping), default=-1) + 1
    configured_count = configured.get("slot_count")
    if configured_count is None:
        configured_count = required_count
    if isinstance(configured_count, bool) or not isinstance(configured_count, int):
        raise SystemExit("switchyard: presentation.slot_count must be an integer")
    slot_count = max(configured_count, required_count)
    if slot_count < 1 or slot_count > team_launcher.MAX_VISIBLE_PANES_PER_WINDOW:
        raise SystemExit(
            "switchyard: presentation.slot_count must be between 1 and "
            f"{team_launcher.MAX_VISIBLE_PANES_PER_WINDOW}"
        )
    layouts: dict[str, dict[str, str | None]] = {"default": default_mapping}
    configured_layouts = configured.get("layouts", {})
    if configured_layouts:
        if not isinstance(configured_layouts, dict):
            raise SystemExit("switchyard: presentation.layouts must be a JSON object")
        for name, mapping in configured_layouts.items():
            if not isinstance(name, str) or not name.strip() or not isinstance(mapping, dict):
                raise SystemExit("switchyard: each presentation layout must be a named JSON object")
            if name == "default":
                # The current RoleConfig projection owns the default.  A
                # workflow change must not leave a stale parallel role list in
                # presentation metadata.
                continue
            layouts[name] = _validated_mapping(mapping, config=config, slot_count=slot_count, allow_removed=False)
    default_slots = _complete_mapping(layouts["default"], slot_count)
    layouts["default"] = default_slots
    return {
        "schema": PRESENTATION_SCHEMA,
        "project": config.project,
        "revision": 0,
        "slot_count": slot_count,
        "slots": copy.deepcopy(default_slots),
        "focused_slot": 0,
        "active_layout": "default",
        "layouts": layouts,
        "history": [],
    }


def _complete_mapping(mapping: Mapping[str, str | None], slot_count: int) -> dict[str, str | None]:
    return {str(slot): mapping.get(str(slot)) for slot in range(slot_count)}


def _validated_mapping(
    mapping: Mapping[Any, Any],
    *,
    config: team_launcher.ProjectConfig,
    slot_count: int,
    allow_removed: bool,
) -> dict[str, str | None]:
    known_roles = {role.role for role in config.roles}
    result: dict[str, str | None] = {}
    seen_roles: set[str] = set()
    for raw_slot, raw_role in mapping.items():
        try:
            slot = int(raw_slot)
        except (TypeError, ValueError) as exc:
            raise SystemExit(f"switchyard: invalid presentation slot {raw_slot!r}") from exc
        if str(slot) != str(raw_slot) and not isinstance(raw_slot, int):
            raise SystemExit(f"switchyard: invalid presentation slot {raw_slot!r}")
        if slot < 0 or slot >= slot_count:
            raise SystemExit(f"switchyard: presentation slot {slot} is outside 0..{slot_count - 1}")
        if raw_role is None:
            role = None
        elif not isinstance(raw_role, str) or not raw_role.strip():
            raise SystemExit(f"switchyard: presentation slot {slot} role must be a non-empty string or null")
        else:
            role = raw_role.strip()
            if role not in known_roles and not allow_removed:
                raise SystemExit(f"switchyard: presentation layout references unknown role {role!r}")
            if role in seen_roles:
                raise SystemExit(f"switchyard: presentation role {role!r} appears in more than one slot")
            seen_roles.add(role)
        result[str(slot)] = role
    return _complete_mapping(result, slot_count)


def validate_presentation_document(
    value: Mapping[str, Any],
    *,
    config: team_launcher.ProjectConfig,
) -> dict[str, Any]:
    if value.get("schema") != PRESENTATION_SCHEMA:
        raise SystemExit("switchyard: unsupported presentation state schema")
    if value.get("project") != config.project:
        raise SystemExit(
            f"switchyard: presentation state project {value.get('project')!r} does not match {config.project!r}"
        )
    revision = value.get("revision")
    slot_count = value.get("slot_count")
    focused_slot = value.get("focused_slot")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise SystemExit("switchyard: presentation revision must be a non-negative integer")
    if isinstance(slot_count, bool) or not isinstance(slot_count, int) or not 1 <= slot_count <= team_launcher.MAX_VISIBLE_PANES_PER_WINDOW:
        raise SystemExit("switchyard: invalid presentation slot_count")
    if isinstance(focused_slot, bool) or not isinstance(focused_slot, int) or not 0 <= focused_slot < slot_count:
        raise SystemExit("switchyard: invalid presentation focused_slot")
    slots = value.get("slots")
    if not isinstance(slots, dict):
        raise SystemExit("switchyard: presentation slots must be a JSON object")
    layouts_value = value.get("layouts")
    if not isinstance(layouts_value, dict) or "default" not in layouts_value:
        raise SystemExit("switchyard: presentation layouts must include default")
    layouts: dict[str, dict[str, str | None]] = {}
    for name, mapping in layouts_value.items():
        if not isinstance(name, str) or not name or not isinstance(mapping, dict):
            raise SystemExit("switchyard: invalid presentation layout")
        layouts[name] = _validated_mapping(mapping, config=config, slot_count=slot_count, allow_removed=True)
    history = value.get("history", [])
    if not isinstance(history, list) or not all(isinstance(item, dict) for item in history):
        raise SystemExit("switchyard: presentation history must be a JSON list")
    active_layout = value.get("active_layout", "default")
    if not isinstance(active_layout, str):
        raise SystemExit("switchyard: invalid active_layout")
    return {
        "schema": PRESENTATION_SCHEMA,
        "project": config.project,
        "revision": revision,
        "slot_count": slot_count,
        "slots": _validated_mapping(slots, config=config, slot_count=slot_count, allow_removed=True),
        "focused_slot": focused_slot,
        "active_layout": active_layout,
        "layouts": layouts,
        "history": history[-PRESENTATION_HISTORY_LIMIT:],
    }


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


def _read_state(path: Path, *, config: team_launcher.ProjectConfig, config_path: Path) -> dict[str, Any]:
    if not path.exists():
        return default_presentation_document(config, config_path=config_path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"switchyard: cannot read presentation state {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"switchyard: presentation state {path} must contain a JSON object")
    state = validate_presentation_document(value, config=config)
    current_defaults = default_presentation_document(config, config_path=config_path)
    projected_count = current_defaults["slot_count"]
    if projected_count > state["slot_count"]:
        for slot in range(state["slot_count"], projected_count):
            state["slots"][str(slot)] = current_defaults["slots"].get(str(slot))
            for mapping in state["layouts"].values():
                mapping[str(slot)] = None
        state["slot_count"] = projected_count
    for name, mapping in current_defaults["layouts"].items():
        state["layouts"][name] = {
            str(slot): mapping.get(str(slot))
            for slot in range(state["slot_count"])
        }
    if state["active_layout"] == "default":
        state["slots"] = copy.deepcopy(state["layouts"]["default"])
    return state


@contextmanager
def _locked_state(
    path: Path,
    *,
    config: team_launcher.ProjectConfig,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    team_launcher.ensure_layout_output_owner(config, path, runner=runner)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as handle:
        team_launcher.ensure_owner_file(config, lock_path, runner=runner)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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
    resumable = bool(team_launcher.session_id_for_role(role, config.session_dir))
    state = "live" if live and not pane_dead else "dead" if live else "missing"
    return {"role": role_name, "state": state, "live": live and not pane_dead, "resumable": resumable}


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


def _write_state(
    config: team_launcher.ProjectConfig,
    state_path: Path,
    state: dict[str, Any],
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    team_launcher._write_private_json_atomic(state_path, state)
    team_launcher.ensure_owner_file(config, state_path, runner=runner)


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
    return actual, "connected"


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


def display_attach_args(
    config: team_launcher.ProjectConfig,
    slot: int,
    *,
    gui_user: str,
) -> list[str]:
    """What one presentation tab runs to display one slot.

    Within one account there is no boundary and the tab attaches directly. When
    the window belongs to a desktop user and the sessions belong to the tenant
    owner, it goes through the per-project display bridge: one root-owned
    program, one NOPASSWD grant, and a slot number as its only input. The
    alternative that shipped was `sudo -u <owner> tmux attach` in every tab,
    which asks a human for their password six times as a window opens, and the
    only way to avoid that without this bridge is a blanket sudo grant on the
    owner account (SYRD-65).
    """
    return display_attach_args_for(
        config.project, slot, owner=config.run_as_user or "", gui_user=gui_user
    )


#: What the caller asks the privileged helper for when the tenant's layout is
#: the single tiled viewer session rather than one display session per slot.
VIEWER_ATTACH_TARGET = "viewer"


def display_attach_args_for(
    project: str, slot: int | str, *, owner: str, gui_user: str
) -> list[str]:
    """The same answer from primitives, for a caller with no tenant config.

    The bridge caller builds its own layout and has never been able to read the
    tenant's configuration; what it knows is the project, its owner from the
    root-owned grant, and itself. One definition, so the tab the desktop half
    opens runs what the owner half would have given it (SYRD-90).
    """
    session = (
        team_launcher.viewer_session_for_project(project)
        if slot == VIEWER_ATTACH_TARGET
        else display_session_name(project, slot)
    )
    direct = ["env", "TMUX=", "tmux", "attach", "-t", _exact_tmux_target(session)]
    owner = (owner or "").strip()
    desktop = (gui_user or team_launcher.current_user_name()).strip()
    if not owner or owner == desktop:
        return direct
    return [
        os.environ.get("SWITCHYARD_SUDO_BIN", "sudo"),
        "-n",
        team_launcher.display_attach_helper_path(project),
        project,
        str(slot),
    ]


def presentation_layout_payload(
    project: str,
    *,
    slot_count: int,
    owner: str,
    gui_user: str,
    pane_program: Path,
    slot_titles: Sequence[str] = (),
    window_title: str = "",
    layout_mode: str = "",
) -> dict[str, Any]:
    """The Konsole layout for one project's presentation window.

    Rendered from primitives so the owner half and the bridge caller produce
    the same document, rather than one of them shipping the other a document to
    write (SYRD-90).

    The titles are passed to the pane wrapper, not written into the layout and
    hoped for: Konsole's layout parser has no key for a split's title, which is
    why `leaf["Title"]` alone left every header reading the fallback
    cwd-and-program text on the desktop handoff path (SYRD-122, SYRD-130).
    """
    titles = list(slot_titles)
    # The viewer layout is ONE tab: the owner built a single tiled tmux session
    # holding every role, so there are no per-slot display sessions to open
    # tabs on, and asking for slot 0..4 of something that does not exist is a
    # window of five errors (SYRD-211 live UAT).
    viewer = layout_mode == team_launcher.LAYOUT_MODE_VIEWER
    layout = team_launcher._new_project_layout_payload(1 if viewer else slot_count)
    for slot, leaf in enumerate(team_launcher._layout_leaves(layout)):
        title = titles[slot] if slot < len(titles) else ""
        leaf["Command"] = team_launcher.inert_pane_command(
            pane_program,
            display_attach_args_for(
                project,
                VIEWER_ATTACH_TARGET if viewer else slot,
                owner=owner,
                gui_user=gui_user,
            ),
            title=title,
            window_title=window_title,
        )
        # The desktop account's own directory, not this process's. Under a
        # privileged invocation `Path.home()` is root's, and the tab recorded
        # `WorkingDirectory=/root` -- a directory the desktop user cannot even
        # enter (SYRD-65).
        leaf["WorkingDirectory"] = team_launcher._gui_home(gui_user) if gui_user else str(Path.home())
        leaf["Title"] = title or f"{project} slot {slot}"
    return layout


def _hand_off_desktop_half(
    config: team_launcher.ProjectConfig,
    state: Mapping[str, Any],
    *,
    gui_user: str,
) -> bool:
    """Report what the bridge caller needs, when this process cannot do it.

    Only when the bridge asked for it: it opens the descriptor and names it in
    the environment it built. Nothing here decides where anything is written --
    this end writes to a pipe it was handed, and root decides what becomes of
    it (SYRD-90).
    """
    raw_fd = os.environ.get(team_launcher.PRESENTATION_HANDOFF_FD_ENV, "").strip()
    if not raw_fd.isdigit():
        return False
    payload = team_launcher.render_presentation_handoff(
        config.project,
        slot_count=int(state["slot_count"]),
        pane_program=team_launcher.pane_window_program(
            team_launcher.switchyard_pane_launcher_for(config)
        ),
        slot_titles=team_launcher.presentation_slot_titles(config, int(state["slot_count"])),
        window_title=team_launcher.project_window_title(config),
    )
    try:
        with os.fdopen(int(raw_fd), "w", encoding="utf-8", closefd=True) as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
    except OSError as exc:
        raise SystemExit(
            f"switchyard: could not hand {config.project}'s presentation window to "
            f"{gui_user or 'the desktop account'}: {exc}"
        ) from exc
    print(
        f"switchyard: {config.project}'s display slots are up; its window opens in "
        f"{gui_user or 'the desktop account'}'s own session."
    )
    return True


def _launch_separate(
    config: team_launcher.ProjectConfig,
    state: Mapping[str, Any],
    *,
    config_path: Path,
    output_path: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    process_launcher: Callable[..., Any] | None,
) -> None:
    # The account that owns the sessions does not necessarily own a screen.
    # Naming it as the GUI user is what sent a launch at the owner's own
    # runtime directory, where there is no compositor listening (SYRD-65).
    gui_user = team_launcher.presentation_gui_user(config)
    layout = presentation_layout_payload(
        config.project,
        slot_count=state["slot_count"],
        owner=config.run_as_user or "",
        gui_user=gui_user,
        pane_program=team_launcher.pane_window_program(
            team_launcher.switchyard_pane_launcher_for(config)
        ),
        slot_titles=team_launcher.presentation_slot_titles(config, state["slot_count"]),
    )
    # Under the control bridge this process is the project owner: it can
    # neither write into the desktop account's state directory nor reach that
    # person's compositor. It does the tenant half, reports the two facts the
    # desktop half needs, and the caller -- who owns both -- does the rest
    # (SYRD-90).
    if _hand_off_desktop_half(config, state, gui_user=gui_user):
        return
    # After the handoff: a caller that reached this process through the
    # tenant-control bridge was admitted by the very grant the tabs need, and
    # opens its own window. This is the path where this process opens it.
    #
    # Before anything opens: a window whose every tab will be refused is worse
    # than no window, because it looks like a presentation and answers nothing.
    # Live mefp opened four tabs that each exited "sudo: a password is
    # required" (SYRD-233). The workers are already up; only the window stops.
    # A tab whose program will not start is a shell, not a pane: Konsole falls
    # back to the profile's shell with no error, so four roles become four
    # ordinary prompts (SYRD-233 live UAT). Checked before the layout is
    # written, for the same reason the bridge handoff checks it.
    program_problem = team_launcher.presentation_pane_program_problem(config, runner=runner)
    if program_problem:
        raise SystemExit(
            f"switchyard: not opening {config.project}'s presentation window: {program_problem}. "
            f"Its roles are running. Run `sudo switchyard upgrade {config.project}` to stage this "
            f"release's pane program, then `switchyard {config.project}` to open the window; "
            f"`switchyard attach {config.project} <role>` reaches any role now."
        )
    if gui_user and gui_user != (config.run_as_user or team_launcher.current_user_name()):
        bridge = team_launcher.display_bridge_launch_problem(config, gui_user=gui_user)
        if bridge:
            raise SystemExit(
                f"switchyard: not opening {config.project}'s presentation window: {bridge}. "
                f"Its roles are running. Run `sudo switchyard upgrade {config.project}` to install "
                f"the display bridge for {gui_user}, then `switchyard {config.project}` to open "
                f"the window; `switchyard attach {config.project} <role>` reaches any role now."
            )
    output = output_path or team_launcher.desktop_presentation_layout_path(
        config, config_path=config_path, gui_user=gui_user
    )
    refusal = team_launcher.write_desktop_layout(
        output,
        layout,
        gui_user=gui_user or team_launcher.current_user_name(),
        runner=runner,
        # Named so a crossing write can be held to this project's own state
        # root under the desktop account and nowhere else (SYRD-233).
        project=config.project,
    )
    if refusal:
        # Named precisely, because the two ways to arrive here need different
        # things said. Through the control bridge this process is the project
        # owner: it has no way into the desktop account's state directory and no
        # way into that person's compositor either, so the answer is the
        # privileged route rather than anything this invocation can retry
        # (SYRD-90).
        through_bridge = bool(os.environ.get(team_launcher.TENANT_CONTROL_CALLER_ENV, "").strip())
        arrived = (
            " This invocation came through the tenant control bridge, which runs as "
            f"{team_launcher.current_user_name()}: only root can hand a layout to another "
            "account."
            if through_bridge
            else ""
        )
        raise SystemExit(
            f"switchyard: refusing to open {config.project}'s presentation window: {refusal}."
            f"{arrived} A terminal handed a layout it cannot read aborts before anything appears; "
            f"run `sudo switchyard {config.project}` from "
            f"{gui_user or 'the desktop account'}'s own session, which can."
        )
    result = team_launcher.launch_konsole_window(
        output,
        project=config.project,
        window_title=team_launcher.project_window_title(config),
        gui_user=gui_user or None,
        runner=runner,
        process_launcher=process_launcher,
    )
    if result != 0:
        raise SystemExit(f"switchyard: could not launch separate presentation window (exit {result})")


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
) -> int:
    """Restore saved slots during ordinary project launch without changing workers."""
    config = runtime_assignment_config(
        config, wait_seconds=assignment_wait_seconds, print_func=print_func
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
) -> dict[str, Any]:
    config = runtime_assignment_config(config)
    _validate_role_namespace(config)
    # Narrow on purpose: the operator path opens for the recovery of the
    # DIRECTOR's slot and nothing else. A disconnected app or ops slot is still
    # the Director's to recover, because the Director is still there to do it.
    operator_recovery = action == "recover" and (role_name or "").strip().lower() == DIRECTOR_ROLE
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
        recovery_runner = _exact_tmux_runner(prepared_owner_runner)
        result = team_launcher.ensure_visible_role_session_for_viewer(
            role,
            mode="attach-or-start",
            session_dir=prepared_config.session_dir,
            pane_state_dir=team_launcher.default_pane_state_dir_for_user(
                prepared_config.run_as_user, project=prepared_config.project
            ),
            bin_user=prepared_config.run_as_user,
            runner=recovery_runner,
        )
        if result != 0:
            raise SystemExit(f"switchyard: recovery failed for {role.role} (exit {result})")
        return _mutate(
            prepared_config, config_path=config_path, state_path=state_path, actor=actor, action=action,
            detail={"role": role.role}, transform=lambda _state: None, runner=prepared_owner_runner,
            file_runner=runner,
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
