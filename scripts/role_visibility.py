"""A role's visibility: whether it occupies a pane slot, and taking it out of one.

- `tmux_detach_clients_args` is the argv that detaches every client from a
  role's tmux session without touching the session itself.
- `_raw_role_for_update` finds the role's entry in the raw launcher config.
- `_write_role_visibility` records a role as detached (no slot) or visible in
  a slot: it rewrites the launcher config atomically, repairs its owner, and
  reloads it.
- `detach_role_from_slot` is `pane detach-role`: it records the role as
  detached, then detaches any live client, leaving the worker session running
  headless.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-406), in their original
order. The launcher imports this module and re-exports all four names; its
`main` and `scripts/role_pane_entry.py` still reach them through the launcher.
Everything they call -- the config reader and writer, the owner repair, the
config loader, the role lookup, the tmux argv, and each other -- is read
through the launcher at call time, so a suite that rebinds one there still
intercepts it. The `subprocess.run` and `print` defaults are bound at
definition, as they were, from this module's own imports: the same objects.
`RoleConfig` and `ProjectConfig` are annotations only. This module imports
`team_launcher` only inside the functions, when they run.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig, RoleConfig


def tmux_detach_clients_args(role: RoleConfig) -> list[str]:
    return ["tmux", "detach-client", "-s", role.tmux_session]


def _raw_role_for_update(raw_roles: Any, role_name: str) -> dict[str, Any]:
    if not isinstance(raw_roles, list):
        raise SystemExit("team-launcher: launcher config roles must be a JSON list")
    for raw_role in raw_roles:
        if isinstance(raw_role, dict) and str(raw_role.get("role") or "").strip() == role_name:
            return raw_role
    raise SystemExit(f"unknown role {role_name!r} in launcher config")


def _write_role_visibility(
    config: ProjectConfig,
    *,
    config_path: Path,
    role: RoleConfig,
    detached: bool,
    slot: int | None,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> ProjectConfig:
    from scripts import team_launcher as launcher

    raw_config = launcher._load_json(config_path)
    raw_role = launcher._raw_role_for_update(raw_config.get("roles"), role.role)
    if detached:
        raw_role["detached"] = True
        raw_role.pop("slot", None)
    else:
        if slot is None:
            raise ValueError("visible role update requires slot")
        raw_role["detached"] = False
        raw_role["slot"] = slot
    try:
        launcher._write_json_atomic(config_path, raw_config)
    except OSError as exc:
        raise SystemExit(f"team-launcher: cannot update launcher config {config_path}: {exc}") from exc
    launcher.ensure_owner_file(config, config_path, runner=runner)
    return launcher.load_project_config(config.project, config_path)


def detach_role_from_slot(
    config: ProjectConfig,
    *,
    config_path: Path,
    role_name: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> int:
    from scripts import team_launcher as launcher

    role = launcher._role_by_name(config, role_name)
    if role.detached:
        print_func(f"team-launcher: role {role.role} is already detached")
        return 0
    previous_slot = role.slot
    launcher._write_role_visibility(
        config,
        config_path=config_path,
        role=role,
        detached=True,
        slot=None,
        runner=runner,
    )
    print_func(f"team-launcher: detached role {role.role} from slot {previous_slot}; tmux session remains headless")
    if runner(launcher.tmux_has_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
        return 0
    detach_proc = runner(launcher.tmux_detach_clients_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if detach_proc.returncode != 0:
        print_func(f"team-launcher: no live tmux client detached for {role.role}; session remains configured headless")
    return 0
