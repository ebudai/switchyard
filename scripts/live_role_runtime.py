"""Live CLI and model detection, and the stale-provider-runtime drop.

`live_cli_for_role` and `live_model_for_role` say which provider CLI, and which
model, a role's pane is actually running, read from the pane's process tree
(`process_tree_argvs`, `_model_from_argv`); the reload uses them to bring a
role's configuration in line with what is live.

`roles_with_stale_provider_runtime` finds running roles whose runtime started
against older provider state than the account now carries, and
`_drop_roles_with_stale_provider_runtime` ends those sessions, through each
role's own runner, so the launch starts them again.

The process snapshot, the pane pid, the command names, the role runner, the
tmux kill argv, the provider-state generations and the session record are read
from `scripts/team_launcher.py` when a function runs, as are the calls between
the functions here.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-330). `team_launcher`
imports this module at its top and still exports every name callers read there.
This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig, RoleConfig


def process_tree_argvs(pane_pid: int) -> list[list[str]]:
    from scripts import team_launcher as launcher

    if pane_pid <= 0:
        return []
    _parents_by_pid, children_by_parent, _process_names, argv_by_pid = launcher._process_snapshot()
    stack = [pane_pid]
    seen: set[int] = set()
    records: list[list[str]] = []
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        argv = argv_by_pid.get(pid, [])
        if argv:
            records.append(list(argv))
        stack.extend(children_by_parent.get(pid, []))
    return records


def _model_from_argv(argv: Sequence[str], *, model_arg: str) -> str:
    if not model_arg:
        return ""
    for index, token in enumerate(argv):
        if token == model_arg and index + 1 < len(argv):
            return str(argv[index + 1]).strip()
        if token.startswith(f"{model_arg}="):
            return token.split("=", 1)[1].strip()
    return ""


def live_cli_for_role(
    role: RoleConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> list[str]:
    from scripts import team_launcher as launcher

    pane_pid = launcher.pane_pid_for_role(role, runner=runner)
    if pane_pid <= 0:
        return []
    matches = sorted(name for name in launcher.process_tree_command_names(pane_pid) if name in launcher.KNOWN_LIVE_CLI_NAMES)
    if len(matches) == 1:
        return [matches[0]]
    configured_cli = launcher._command_name(role.cli[0])
    if configured_cli in matches:
        return [configured_cli]
    return []


def live_model_for_role(
    role: RoleConfig,
    *,
    session_dir: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> str:
    from scripts import team_launcher as launcher

    pane_pid = launcher.pane_pid_for_role(role, runner=runner)
    if pane_pid > 0:
        for argv in launcher.process_tree_argvs(pane_pid):
            model = launcher._model_from_argv(argv, model_arg=role.model_arg)
            if model:
                return model
    return launcher._session_payload_model_for_role(role, session_dir)


def roles_with_stale_provider_runtime(
    config: ProjectConfig,
    running_roles: Sequence[RoleConfig],
    *,
    owner_home: Path,
) -> list[RoleConfig]:
    """Running roles whose runtime predates the provider state it needs.

    A process reads its provider state once, when it starts. Anything recorded
    afterwards -- a login, an account's first run, a worktree's trust -- is
    invisible to it, and it goes on showing whatever it was showing. Live on the
    testing tenant, five sessions from the previous day were presented after two
    logins and a completed setup, every one of them still on a first-run screen.

    A role with no record at all counts as stale: nothing says its runtime was
    started against the state that exists now, and one restart settles it.
    """
    from scripts import team_launcher as launcher

    stale: list[RoleConfig] = []
    for role in running_roles:
        cli = launcher._role_cli_name(role)
        if not cli:
            continue
        # A store this process cannot read or write answers nothing, and a
        # restart over it would end a live pane and leave the same question
        # for the next launch (SYRD-233).
        if launcher.provider_state_store_problem(config, role):
            continue
        if launcher.recorded_provider_state_generation(config, role) != launcher.provider_state_generation(
            cli, owner_home=owner_home
        ):
            stale.append(role)
    return stale


def _drop_roles_with_stale_provider_runtime(
    config: ProjectConfig,
    running_roles: Sequence[RoleConfig],
    *,
    owner_home: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    print_func: Callable[[str], None] = print,
) -> list[RoleConfig]:
    """Roles that may be presented as they are, rather than started again.

    A process reads its provider state once, when it starts, so a login, an
    account's first run or a worktree's trust recorded afterwards never reaches
    a runtime that was already up. Live on the testing tenant: two logins and a
    completed setup, and five sessions from the previous day presented as they
    were, each still on a first-run screen.

    Staleness is decided by comparing each role's recorded generation with the
    one the account carries now, so it does not depend on this run having
    performed a login -- the failing case had none -- and a token refresh, which
    changes no decision, changes no generation and restarts nobody. A role with
    no record is stale by definition: nothing says what its runtime was started
    against, and one restart settles it (SYRD-191).
    """
    from scripts import team_launcher as launcher

    # Said once, before anything is ended: these roles are left exactly as they
    # are, and the reason is a repair rather than a restart.
    for role, problem in launcher.unreadable_provider_state_roles(config, running_roles):
        print_func(
            f"team-launcher: leaving {role.role} running: {problem}. Its runtime is not "
            f"checked against the account's provider state, and restarting it would settle "
            f"nothing. Run `sudo switchyard upgrade {config.project}` to give the tenant "
            "account its own role state back."
        )
    stale = launcher.roles_with_stale_provider_runtime(config, running_roles, owner_home=owner_home)
    if not stale:
        return list(running_roles), set()
    wanted = {role.role for role in stale}
    keep: list[RoleConfig] = []
    restarted: list[str] = []
    unreconciled: set[str] = set()
    for role in running_roles:
        if role.role not in wanted:
            keep.append(role)
            continue
        role_runner = launcher.role_process_runner_for(config, role, runner=runner)
        result = role_runner(
            launcher.tmux_kill_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        if getattr(result, "returncode", 1) != 0:
            # It could not be ended, so it will not be started either: say so
            # rather than reporting a restart that did not happen, and leave the
            # record alone so the next launch tries again.
            print_func(
                f"team-launcher: {role.role} is running against older "
                f"{launcher._role_cli_name(role)} state and its session could not be ended; it will keep "
                "showing whatever it was showing until it is restarted"
            )
            keep.append(role)
            unreconciled.add(role.role)
            continue
        # The record is NOT updated here: it is written after the launch, for
        # roles that actually came up. A restart that ends a session and then
        # fails to start one must leave the role stale.
        restarted.append(role.role)
    if restarted:
        print_func(
            "team-launcher: restarting "
            + ", ".join(sorted(restarted))
            + ": their runtimes started against older provider state than this account now has"
        )
    return keep, unreconciled

def sync_reload_config_to_live_sessions(
    config: ProjectConfig,
    *,
    config_path: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> ProjectConfig:
    from scripts import team_launcher as launcher

    raw_config = launcher._load_json(config_path)
    raw_roles = raw_config.get("roles")
    if not isinstance(raw_roles, list):
        return config
    role_by_name = {role.role: role for role in config.roles}
    updated_roles: list[RoleConfig] = []
    changed = False
    for raw_role in raw_roles:
        if not isinstance(raw_role, dict):
            continue
        role_name = str(raw_role.get("role") or "").strip()
        role = role_by_name.get(role_name)
        if role is None:
            continue
        # A role's live session is in that role's own tmux server, so reload has
        # to inspect it there or it sees nothing and rewrites the config from a
        # blank reading (SYRD-39).
        role_runner = launcher.role_process_runner_for(config, role, runner=runner)
        if role_runner(launcher.tmux_has_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
            updated_roles.append(role)
            continue
        live_model = launcher.live_model_for_role(role, session_dir=launcher.role_session_dir(config, role), runner=role_runner)
        if not live_model:
            updated_roles.append(role)
            continue
        live_cli = launcher.live_cli_for_role(role, runner=role_runner)
        next_role = role
        if live_cli and live_cli != role.cli:
            raw_role["cli"] = live_cli
            if not set(live_cli) <= set(role.live_commands):
                raw_role["live_commands"] = live_cli
                next_role = replace(next_role, cli=live_cli, live_commands=live_cli)
            else:
                next_role = replace(next_role, cli=live_cli)
            changed = True
        if live_model != role.model:
            raw_role["model"] = live_model
            next_role = replace(next_role, model=live_model)
            changed = True
        updated_roles.append(next_role)
    if not changed:
        return config
    launcher._write_json_atomic(config_path, raw_config)
    return replace(config, roles=updated_roles)
