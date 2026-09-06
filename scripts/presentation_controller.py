"""Runtime presentation slots for persistent Switchyard role sessions.

RoleConfig remains the authoritative worker registry.  This module persists only
which role a stable display slot should present and changes proxy panes without
restarting or modifying worker sessions.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import shlex
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from scripts import team_launcher

PRESENTATION_SCHEMA = "switchyard.presentation.v1"
PRESENTATION_HISTORY_LIMIT = 100
DIRECTOR_ROLE = "director"


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
        expected_session = f"{config.project}-{role.role}"
        expected_target = f"{expected_session}:0.0"
        if role.tmux_session != expected_session or role.target != expected_target:
            raise SystemExit(
                f"switchyard: refusing foreign presentation target for {role.role}: expected {expected_target}, "
                f"configured {role.target}"
            )


def _require_director(config: team_launcher.ProjectConfig, environ: Mapping[str, str]) -> str:
    actor = (environ.get("TICKET_BOARD_CALLER_ROLE") or environ.get("PGU_TICKET_BOARD_CALLER_ROLE") or "").strip().lower()
    if actor != DIRECTOR_ROLE:
        raise SystemExit("switchyard: runtime presentation changes require TICKET_BOARD_CALLER_ROLE=director")
    selected_project = (environ.get("TICKET_BOARD_PROJECT") or "").strip()
    if selected_project and selected_project != config.project:
        raise SystemExit(
            f"switchyard: refusing cross-project presentation change from {selected_project!r} to {config.project!r}"
        )
    return actor


def _tmux_runner(
    config: team_launcher.ProjectConfig,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> Callable[..., subprocess.CompletedProcess[Any]]:
    if config.run_as_user and team_launcher.current_user_name() != config.run_as_user:
        return team_launcher._owner_process_runner(owner_user=config.run_as_user, runner=runner)
    return runner


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
    live = _session_exists(role.tmux_session, runner=runner)
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


def _proxy_command(config: team_launcher.ProjectConfig, role_name: str | None, role_status: Mapping[str, Any]) -> tuple[str, str]:
    if role_name is None:
        message = f"{config.project}: display slot hidden"
        return str(Path.home()), _status_command(message)
    role = _role_by_name(config, role_name)
    if role is not None and role_status.get("live"):
        recovery = f"switchyard present {config.project} recover {role_name}"
        message = f"{config.project}: {role_name} disconnected; use `{recovery}`"
        attach = shlex.join(
            [
                "env", "TMUX=", "tmux", "attach", "-f", "!no-detach-on-destroy",
                "-t", _exact_tmux_target(role.tmux_session),
            ]
        )
        script = f"{attach}; printf '%s\\n' {shlex.quote(message)}; exec sleep 2147483647"
        return role.workdir, shlex.join(["sh", "-lc", script])
    state = role_status.get("state", "unavailable")
    recovery = " (resume available)" if role_status.get("resumable") else ""
    message = f"{config.project}: {role_name} is {state}{recovery}; use `switchyard present {config.project} recover {role_name}`"
    return role.workdir if role is not None else str(Path.home()), _status_command(message)


def _status_command(message: str) -> str:
    script = f"printf '%s\\n' {shlex.quote(message)}; exec sleep 2147483647"
    return shlex.join(["sh", "-lc", script])


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
            f"`switchyard present {config.project} recover {role_name}`"
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


def _configure_display_session(
    config: team_launcher.ProjectConfig,
    slot: int,
    role_name: str | None,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    session = display_session_name(config.project, slot)
    status = _role_status(config, role_name, runner=runner) if role_name else {"state": "hidden", "live": False}
    role = _role_by_name(config, role_name or "")
    if role is not None and status.get("live"):
        detach_proc = runner(
            [
                "tmux", "set-option", "-t", _exact_tmux_target(f"{role.tmux_session}:"),
                "detach-on-destroy", "on",
            ]
        )
        if detach_proc.returncode != 0:
            raise RuntimeError(f"tmux could not configure recovery for {role_name} (exit {detach_proc.returncode})")
    workdir, command = _proxy_command(config, role_name, status)
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
        ["tmux", "set-window-option", "-t", _exact_tmux_target(f"{session}:0"), "remain-on-exit", "on"],
        ["tmux", "set-option", "-t", _exact_tmux_target(f"{session}:"), "status", "on"],
        [
            "tmux", "set-option", "-t", _exact_tmux_target(f"{session}:"),
            "status-left", f" slot {slot}: {label} ",
        ],
        ["tmux", "set-option", "-t", _exact_tmux_target(f"{session}:"), "set-titles", "on"],
        [
            "tmux", "set-option", "-t", _exact_tmux_target(f"{session}:"),
            "set-titles-string", f"{config.project} slot {slot}: {label}",
        ],
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


def _apply_mapping(
    config: team_launcher.ProjectConfig,
    mapping: Mapping[str, str | None],
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    for slot in range(len(mapping)):
        _configure_display_session(config, slot, mapping[str(slot)], runner=runner)
    viewer = team_launcher.viewer_session_for_project(config.project)
    if _session_exists(viewer, runner=runner):
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


def presentation_report(
    config: team_launcher.ProjectConfig,
    *,
    config_path: Path,
    state_path: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> dict[str, Any]:
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
    return {
        "schema": PRESENTATION_SCHEMA,
        "project": config.project,
        "revision": state["revision"],
        "active_layout": state["active_layout"],
        "focused_slot": state["focused_slot"],
        "state_path": str(state_path),
        "slots": slots,
        "hidden_roles": hidden_roles,
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
    first, *rest = sessions
    attach = shlex.join(["env", "TMUX=", "tmux", "attach", "-t", _exact_tmux_target(first)])
    proc = runner([
        "tmux", "new-session", "-d", "-x", str(team_launcher.DEFAULT_VIEWER_COLUMNS),
        "-y", str(team_launcher.DEFAULT_VIEWER_ROWS), "-s", viewer, attach,
    ])
    if proc.returncode != 0:
        raise SystemExit(f"switchyard: could not create viewer {viewer}")
    for session in rest:
        proc = runner([
            "tmux", "split-window", "-t", _exact_tmux_target(f"{viewer}:0"),
            shlex.join(["env", "TMUX=", "tmux", "attach", "-t", _exact_tmux_target(session)]),
        ])
        if proc.returncode != 0:
            raise SystemExit(f"switchyard: could not populate viewer {viewer}")
    commands = (
        ["tmux", "select-layout", "-t", _exact_tmux_target(f"{viewer}:0"), "tiled"],
        ["tmux", "set-option", "-t", _exact_tmux_target(f"{viewer}:"), "status", "on"],
        [
            "tmux", "set-window-option", "-t", _exact_tmux_target(f"{viewer}:0"),
            "pane-border-status", "top",
        ],
        [
            "tmux", "set-window-option", "-t", _exact_tmux_target(f"{viewer}:0"),
            "pane-border-format", " slot #{@switchyard_slot}: #{@switchyard_role} ",
        ],
    )
    for args in commands:
        proc = runner(args)
        if proc.returncode != 0:
            raise SystemExit(f"switchyard: could not configure viewer {viewer}")
    for slot in range(state["slot_count"]):
        role = state["slots"][str(slot)] or "hidden"
        for option, value in (("@switchyard_slot", str(slot)), ("@switchyard_role", role)):
            proc = runner(
                ["tmux", "set-option", "-p", "-t", _exact_tmux_target(f"{viewer}:0.{slot}"), option, value]
            )
            if proc.returncode != 0:
                raise SystemExit(f"switchyard: could not label viewer slot {slot}")


def _launch_separate(
    config: team_launcher.ProjectConfig,
    state: Mapping[str, Any],
    *,
    config_path: Path,
    output_path: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    process_launcher: Callable[..., Any] | None,
) -> None:
    layout = team_launcher._new_project_layout_payload(state["slot_count"])
    leaves = team_launcher._layout_leaves(layout)
    for slot, leaf in enumerate(leaves):
        session = display_session_name(config.project, slot)
        args = ["env", "TMUX=", "tmux", "attach", "-t", _exact_tmux_target(session)]
        if config.run_as_user and team_launcher.current_user_name() != config.run_as_user:
            args = ["sudo", "-u", config.run_as_user, "-H", *args]
        leaf["Command"] = shlex.join(args)
        leaf["WorkingDirectory"] = str(Path.home())
        leaf["Title"] = f"{config.project} slot {slot}"
    output = output_path or team_launcher.default_layout_output_path(config, config_path=config_path).with_name(
        f"{config.project}-presentation-layout.json"
    )
    team_launcher._write_private_json_atomic(output, layout)
    team_launcher.ensure_owner_file(config, output, runner=runner)
    result = team_launcher.launch_konsole_window(
        output,
        project=config.project,
        window_title=team_launcher.project_window_title(config),
        gui_user=config.run_as_user or None,
        runner=runner,
        process_launcher=process_launcher,
    )
    if result != 0:
        raise SystemExit(f"switchyard: could not launch separate presentation window (exit {result})")


def launch_presentation(
    config: team_launcher.ProjectConfig,
    *,
    config_path: Path,
    layout: str,
    state_path: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    process_launcher: Callable[..., Any] | None = None,
) -> int:
    """Restore saved slots during ordinary project launch without changing workers."""
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
        _launch_separate(
            config,
            state,
            config_path=config_path,
            output_path=state_path.with_name(f"{config.project}-presentation-layout.json"),
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
) -> dict[str, Any]:
    _validate_role_namespace(config)
    actor = _require_director(config, environ)
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


def print_presentation_report(report: Mapping[str, Any], *, json_output: bool, print_func: Callable[[str], None] = print) -> None:
    if json_output:
        print_func(json.dumps(report, indent=2, sort_keys=True))
        return
    print_func(f"{report['project']} presentation revision {report['revision']} ({report['active_layout']})")
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
