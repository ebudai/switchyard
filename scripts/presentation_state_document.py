"""Persist and project the presentation document for display slots."""

from __future__ import annotations

import copy
import fcntl
import json
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from scripts import team_launcher

PRESENTATION_SCHEMA = "switchyard.presentation.v1"
PRESENTATION_HISTORY_LIMIT = 100


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
    elif projected_count < state["slot_count"]:
        # And back down. A count that only grew kept MEFP's window at the six
        # slots its example-derived layout once needed after its roles were
        # put back on 0-3: `switchyard start` opened six panes, two of them
        # inert (SYRD-262). Shrink to what is still in use -- never below what
        # the configuration projects, and never past a slot a named layout or
        # a non-default active mapping still shows somebody in.
        needed = projected_count
        occupied = [
            mapping for name, mapping in state["layouts"].items() if name != "default"
        ]
        if state["active_layout"] != "default":
            occupied.append(state["slots"])
        for mapping in occupied:
            for slot, role in mapping.items():
                if role is not None:
                    needed = max(needed, int(slot) + 1)
        if needed < state["slot_count"]:
            for slot in range(needed, state["slot_count"]):
                state["slots"].pop(str(slot), None)
                for mapping in state["layouts"].values():
                    mapping.pop(str(slot), None)
            state["slot_count"] = needed
            if state["focused_slot"] >= needed:
                state["focused_slot"] = 0
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


def _write_state(
    config: team_launcher.ProjectConfig,
    state_path: Path,
    state: dict[str, Any],
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    team_launcher._write_private_json_atomic(state_path, state)
    team_launcher.ensure_owner_file(config, state_path, runner=runner)
