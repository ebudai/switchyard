"""The command line a role's agent CLI is started with.

Each supported runtime (Claude, Codex, Hermes, agy) spells the same intents
differently. This module holds those per-CLI adapter tables:
- unattended/approval flags (`YOLO_ARGS_BY_CLI`);
- startup arguments;
- how a reasoning effort is passed (`EFFORT_STYLE_BY_CLI`). For Codex that is
  the TOML override `-c model_reasoning_effort="<level>"`;
- the model flag and resume conventions `_role_from_json` defaults a role to.

It also builds a role's arguments from those tables, and the whole pane
command (`cli_command_for_role`): environment, PATH, resume and the CLI's own
arguments.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-290). `team_launcher`
imports this module at its top and still exports every name callers read there.
This module never imports `team_launcher` at its top. Launcher facilities
(`_command_name`, `role_runtime_binding`, `session_id_for_role`,
`hermes_home_for_role`, the env/PATH helpers) are read from
`scripts.team_launcher` when a function runs, so patches there still reach them.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.team_launcher import RoleConfig


YOLO_ARGS_BY_CLI = {
    "agy": ["--dangerously-skip-permissions"],
    "claude": ["--dangerously-skip-permissions"],
    "codex": ["--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust"],
    "hermes": ["--yolo"],
}


STARTUP_ARGS_BY_CLI = {
    "hermes": ["--accept-hooks", "--pass-session-id"],
}


EFFORT_STYLE_BY_CLI = {
    "agy": None,
    "claude": "flag",
    "codex": "config",
    "hermes": "reasoning",
}


DEFAULT_MODEL_ARG_BY_CLI = {
    "hermes": "-m",
}


DEFAULT_RESUME_MODE_BY_CLI = {
    "agy": "flag",
    "claude": "flag",
    "codex": "subcommand",
    "hermes": "flag",
}


DEFAULT_RESUME_FLAG_BY_CLI = {
    "agy": "--conversation",
    "claude": "--resume",
    "hermes": "--resume",
}


DEFAULT_RESUME_SUBCOMMAND_BY_CLI = {
    "codex": "resume",
}


def yolo_args_for_role(role: RoleConfig) -> list[str]:
    from scripts import team_launcher as launcher

    if not role.yolo:
        return []
    cli_name = launcher._command_name(role.cli[0])
    flags = YOLO_ARGS_BY_CLI.get(cli_name)
    if flags is None:
        raise SystemExit(f"role {role.role} uses unsupported yolo cli {cli_name!r}")
    return [flag for flag in flags if flag not in role.extra_args]


def startup_args_for_role(role: RoleConfig) -> list[str]:
    from scripts import team_launcher as launcher

    cli_name = launcher._command_name(role.cli[0])
    flags = STARTUP_ARGS_BY_CLI.get(cli_name, [])
    return [flag for flag in flags if flag not in role.extra_args]


def effort_args_for_role(role: RoleConfig) -> list[str]:
    from scripts import team_launcher as launcher

    if not role.effort:
        return []
    cli_name = launcher._command_name(role.cli[0])
    style = EFFORT_STYLE_BY_CLI.get(cli_name)
    if style == "flag":
        return ["--effort", role.effort]
    if style == "config":
        # Codex's setting is `model_reasoning_effort`; its `-c` value is TOML,
        # so the level is a quoted string. The bare `reasoning_effort` this
        # used to emit is not a setting: Codex 0.156.1 answers
        # "session-flags: `reasoning_effort` is ignored." and runs at its
        # default, measured from its own session header (SYRD-277). A role's
        # extra_args come after this and Codex applies the last `-c` of a key,
        # so an explicit override there still wins.
        return ["-c", f'model_reasoning_effort="{role.effort}"']
    if style == "reasoning":
        return ["--reasoning", role.effort]
    if style is None and cli_name in EFFORT_STYLE_BY_CLI:
        return []
    raise SystemExit(f"role {role.role} uses unsupported effort cli {cli_name!r}")


def _resume_args_for_role(role: RoleConfig, session_id: str) -> list[str]:
    if not session_id:
        return []
    if role.resume_mode == "flag":
        return [role.resume_flag, session_id]
    if role.resume_mode == "subcommand":
        return [role.resume_subcommand, session_id]
    raise SystemExit(f"role {role.role} uses unsupported resume_mode {role.resume_mode!r}")


def hermes_env_for_role(role: RoleConfig, *, session_dir: Path) -> dict[str, str]:
    from scripts import team_launcher as launcher

    if not launcher._uses_hermes(role):
        return {}
    return {"HERMES_HOME": str(launcher.hermes_home_for_role(role, session_dir=session_dir))}


def cli_command_for_role(
    role: RoleConfig,
    *,
    session_dir: Path,
    pane_state_dir: Path | None = None,
    resume: bool = False,
    bin_user: str = "",
) -> list[str]:
    from scripts import team_launcher as launcher

    session_id = launcher.session_id_for_role(role, session_dir) if resume else ""
    if session_id and launcher._uses_fresh_session_per_ticket(role):
        session_id = ""
    command = [*role.cli, *_resume_args_for_role(role, session_id)]
    if role.model:
        command.extend([role.model_arg, role.model])
    command.extend(effort_args_for_role(role))
    command.extend(yolo_args_for_role(role))
    command.extend(startup_args_for_role(role))
    command.extend(role.extra_args)
    env = {
        **role.env,
        "TICKET_BOARD_PANE_TARGET": role.target,
        "TICKET_BOARD_PANE_SESSION_DIR": str(session_dir.expanduser()),
    }
    env.update(hermes_env_for_role(role, session_dir=session_dir))
    if pane_state_dir is not None:
        env["TICKET_BOARD_PANE_STATE_DIR"] = str(pane_state_dir.expanduser())
    if session_id:
        env["TICKET_BOARD_PANE_SESSION_ID"] = session_id
    if role.target.startswith("pgu-"):
        env.setdefault("PGU_PANE_TARGET", role.target)
        env.setdefault("PGU_TICKET_BOARD_PANE_SESSION_DIR", str(session_dir.expanduser()))
        if pane_state_dir is not None:
            env.setdefault("PGU_TICKET_BOARD_PANE_STATE_DIR", str(pane_state_dir.expanduser()))
        if session_id:
            env.setdefault("PGU_PANE_SESSION_ID", session_id)
    pane_path_dirs = launcher.default_user_bin_dirs(bin_user)
    # A role that runs as its own account cannot traverse the owner's home, so
    # the board clients it needs are the staged, root-owned copies (SYRD-45).
    staged_project = str(env.get("TICKET_BOARD_PROJECT") or "").strip()
    if staged_project and bin_user:
        pane_path_dirs.insert(0, f"/usr/local/lib/switchyard/{staged_project}")
    configured_directorctl = env.get("TICKET_BOARD_DIRECTORCTL", "").strip()
    runtime_registrar = "ticket-board-register-runtime"
    if configured_directorctl:
        directorctl_path = Path(configured_directorctl).expanduser()
        if not directorctl_path.is_absolute():
            raise SystemExit(
                f"team-launcher: TICKET_BOARD_DIRECTORCTL must be absolute for {role.role}: "
                f"{configured_directorctl}"
            )
        pane_path_dirs.insert(0, str(directorctl_path.parent))
        runtime_registrar = str(directorctl_path.parent / "ticket-board-register-runtime")
    env["PATH"] = launcher._prepend_paths(env.get("PATH") or launcher.default_pane_base_path(bin_user), pane_path_dirs)
    env_prefix = ["env", *launcher._env_unset_prefix((*launcher.PANE_TARGET_ENV_KEYS, *role.unset_env)), *launcher._env_prefix(env)]
    socket_path = str(role.env.get("TICKET_BOARD_SOCKET") or "").strip()
    if socket_path and str(role.env.get("TICKET_BOARD_PROCESS_AUTHORITY") or "") == "1":
        runtime, target = launcher.role_runtime_binding(role)
        return [
            *env_prefix,
            runtime_registrar,
            "--socket", socket_path,
            "--role", role.role,
            "--runtime", runtime,
            "--target", target,
            "--worktree", role.workdir,
            "--session-dir", str(session_dir.expanduser()),
            "--",
            *command,
        ]
    return [*env_prefix, *command]
