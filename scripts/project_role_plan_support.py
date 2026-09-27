"""What regenerating a provisioned tenant's plan reads, and how an updated board unit is applied.

The shared support of `switchyard add-role` (`scripts/project_role_add.py`) and
`switchyard set-vcs-close-role` (`scripts/project_vcs_close_role.py`):

- **Role lists.** `_configured_implementer_roles` and `_configured_audit_roles`
  take the roles the recorded plan lists -- normalised, each once, in order --
  or, without a recorded list, those the configuration declares, and add the
  role being introduced when it is not already there.
- **Recorded plan data and its provenance.** `_loaded_plan_field` reads a
  recorded value, falling back when it is missing, `None` or empty (never for
  `False`, `0` or an empty list). `_owner_home_from_plan_data` and
  `_commit_git_dir_from_plan_data` fall back to what the host says, computed
  before the recorded value is consulted. `_regenerated_control_user` never
  reads the controller from the tenant's document: it comes from the installed
  root-owned grant.
- **Applying the unit.** `_install_and_restart_board_unit` installs the updated
  board unit, reloads systemd and restarts the board, stopping at the first
  step that fails.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-357). The launcher
imports this module at its top and re-exports every name; both commands read
them through the launcher when they run, as they did. Every launcher facility
these use -- the reserved role names, the role de-duplicator, the current user,
the installed controller and invoking human, the owner's home, the commit cache
location -- and every name defined here that another definition here calls is
read from `team_launcher` when it runs, as it was. This module never imports
`team_launcher` at its top.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectBoardProvision, ProjectConfig


def _configured_implementer_roles(
    config: ProjectConfig,
    *,
    plan_data: dict[str, Any] | None = None,
    extra_role: str | None = None,
) -> tuple[str, ...]:
    from scripts import team_launcher as launcher

    roles: list[str] = []
    raw_plan_roles = (plan_data or {}).get("implementer_roles")
    if isinstance(raw_plan_roles, list):
        source_roles = [str(role).strip().lower() for role in raw_plan_roles if str(role).strip()]
    else:
        source_roles = [
            role.role
            for role in config.roles
            if role.role not in launcher.NEW_PROJECT_RESERVED_ROLE_NAMES
        ]
    for role in source_roles:
        if role not in roles:
            roles.append(role)
    if extra_role and extra_role not in roles:
        roles.append(extra_role)
    return tuple(roles)


def _configured_audit_roles(
    config: ProjectConfig,
    *,
    plan_data: dict[str, Any] | None = None,
    extra_role: str | None = None,
) -> tuple[str, ...]:
    from scripts import team_launcher as launcher

    raw_plan_roles = (plan_data or {}).get("audit_roles")
    if isinstance(raw_plan_roles, list):
        roles = list(launcher._dedupe_role_names(tuple(str(role).strip().lower() for role in raw_plan_roles if str(role).strip())))
    else:
        roles = ["audit"] if any(role.role == "audit" for role in config.roles) else []
    if extra_role and extra_role not in roles:
        roles.append(extra_role)
    return tuple(roles)


def _regenerated_control_user(config: ProjectConfig, plan_data: dict[str, Any]) -> str:
    """Regenerated, never read from plan.json.

    The controller decides who may cross into the owner account, so taking it
    from a document the tenant can write would let the tenant name whoever it
    liked. It comes from the installed root-owned grant instead, which is also
    why re-rendering a tenant's artifacts reproduces the grant it already has.
    """
    from scripts import team_launcher as launcher

    owner = str(
        launcher._loaded_plan_field(plan_data, "owner_user", config.run_as_user or launcher.current_user_name())
    )
    return launcher.resolve_control_user(config.project, invoking_user=launcher.invoking_human(), owner_user=owner)


def _loaded_plan_field(plan_data: dict[str, Any], key: str, default: Any) -> Any:
    return plan_data[key] if key in plan_data and plan_data[key] not in (None, "") else default


def _owner_home_from_plan_data(config: ProjectConfig, plan_data: dict[str, Any]) -> Path:
    from scripts import team_launcher as launcher

    return Path(str(launcher._loaded_plan_field(
        plan_data,
        "owner_home",
        launcher._owner_home_for_auth(config.run_as_user or launcher.current_user_name()),
    )))


def _commit_git_dir_from_plan_data(config: ProjectConfig, plan_data: dict[str, Any]) -> str:
    from scripts import team_launcher as launcher

    return str(launcher._loaded_plan_field(
        plan_data,
        "commit_git_dir",
        launcher.commit_git_dir_env_for_project(
            project=config.project,
            owner_home=launcher._owner_home_from_plan_data(config, plan_data),
        ),
    ))


def _install_and_restart_board_unit(
    plan: ProjectBoardProvision,
    *,
    board_unit_path: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    install = runner(["sudo", "install", "-m", "0644", str(board_unit_path), f"/etc/systemd/system/{plan.board_unit}"])
    if install.returncode != 0:
        raise SystemExit(f"team-launcher: failed to install updated board unit {plan.board_unit}")
    daemon_reload = runner(["sudo", "systemctl", "daemon-reload"])
    if daemon_reload.returncode != 0:
        raise SystemExit("team-launcher: failed to reload systemd after updating the board unit")
    restart = runner(["sudo", "systemctl", "restart", plan.board_unit])
    if restart.returncode != 0:
        raise SystemExit(f"team-launcher: failed to restart {plan.board_unit}")
