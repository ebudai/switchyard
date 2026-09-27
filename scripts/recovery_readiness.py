"""Whether a recovery may be called finished, and whether a tenant's repository boundary is closed.

- `recovery_readiness_problems` reads back everything a finished recovery
  needs -- the registry entry, the privileged packet, a closed repository
  boundary, a live pane and a registered runtime for every role -- rather than
  inferring any of it (SYRD-155, SYRD-162, SYRD-169, SYRD-175).
- `_acl_entries` reads a path's ACL entries with `getfacl`, and
  `repository_boundary_problems` names every way the tenant's shared worktree
  base and control repository are still open to other accounts (SYRD-175).

Moved out of `scripts/team_launcher.py` unchanged (SYRD-365). The launcher
imports this module at its top and re-exports every name, so resume-provision
and `repository_boundary_repair`, which reads the boundary check through the
launcher, reach the same objects. Every launcher facility these use -- the
packet probe, the liveness checks, the runtime-registration wait, the uid and
registry readers -- and every name defined here that another definition here
reads is read from `team_launcher` when it runs, as it was. The provisioning
path helpers are still imported inside the function that uses them. The
registration timeout default is the `provider_runtime_state` object the
launcher imports; the standard-library names are this module's own imports,
the same objects. This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import grp
import stat
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

from scripts.provider_runtime_state import RUNTIME_REGISTRATION_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from scripts.packet_completion import PacketCompletion
    from scripts.pane_liveness_checks import PaneLiveness
    from scripts.provider_runtime_state import RuntimeRegistrationWait
    from scripts.session_records import LaunchSessionRecordStatus
    from scripts.team_launcher import ProjectBoardProvision, ProjectConfig, RoleConfig


def recovery_readiness_problems(
    plan: "ProjectBoardProvision",
    config: ProjectConfig,
    config_path: Path,
    *,
    registry_path: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    process_commands: Sequence[str] | None = None,
    pane_liveness_states: "Sequence[PaneLiveness] | None" = None,
    session_statuses: Sequence[LaunchSessionRecordStatus] | None = None,
    completion: PacketCompletion | None = None,
    registration: RuntimeRegistrationWait | None = None,
    runtime_wait_roles: Sequence[RoleConfig] | None = None,
    runtime_wait_seconds: float = RUNTIME_REGISTRATION_TIMEOUT_SECONDS,
    print_func: Callable[[str], None] = print,
) -> list[str]:
    """Everything that must be true before a recovery may be called finished.

    Recovery is not "the commands returned zero". It is that the project can
    be named, that its board and listener are up, that every role it declares
    has a session, and that those sessions registered themselves with the
    board. Each of those is read back rather than inferred, because the whole
    reason this ticket exists is a recovery that reported success with none of
    them true.
    """
    from scripts import team_launcher as launcher

    problems: list[str] = []
    entry = launcher._load_json(registry_path) if registry_path.exists() else None
    if not isinstance(entry, dict):
        problems.append(f"{plan.project} is not registered at {registry_path}")
    else:
        if str(entry.get("slug") or "") != plan.project:
            problems.append(
                f"{registry_path} registers slug {entry.get('slug')!r}, not {plan.project!r}"
            )
        registered_at = str(entry.get("config_path") or "")
        if registered_at != str(config_path):
            problems.append(
                f"{registry_path} points at {registered_at!r} rather than the verified "
                f"configuration {config_path}"
            )
    problems.extend(
        (completion or launcher.privileged_packet_completion(plan, runner=runner)).problems
    )
    # A tenant whose repository boundary is open is not a finished recovery, and
    # saying it is would be the last place this could be missed (SYRD-175).
    for objection in launcher.repository_boundary_problems(plan, runner=runner):
        problems.append(
            f"{objection} -- repair it with `pkexec switchyard repair-boundary "
            f"{plan.project} --apply`"
        )
    # Liveness comes from the owner's own tmux server and the board's runtime
    # assignments, not from argv. The marker this used to search for belongs to
    # the env wrapper that started the pane, and a long-running CLI has exec'd
    # past it -- which reported six live panes absent (SYRD-169).
    if pane_liveness_states is not None:
        states = list(pane_liveness_states)
    else:
        tmux_targets, tmux_problem = launcher.owner_tmux_targets(config, runner=runner)
        assignments, assignment_problem = launcher.read_runtime_assignment_details(config)
        if tmux_problem:
            problems.append(tmux_problem)
        if assignment_problem:
            problems.append(assignment_problem)
        owner_uid = launcher.uid_for_user(config.run_as_user)
        states = [
            launcher.pane_liveness(
                config, role,
                tmux_targets=tmux_targets,
                assignments=assignments,
                owner_uid=owner_uid,
            )
            for role in config.roles
        ]
    for state in states:
        if not state.live:
            problems.append(f"{state.role} has no running pane: {state.why}")
    # Registration is the pane's own asynchronous work, and this readiness check
    # runs immediately after the launch that started those panes. Sampling once
    # asked before the answer existed, and reported a healthy project as a failed
    # recovery an operator was told to retry by hand (SYRD-162).
    live = {state.role for state in states if state.live}
    waited = (
        registration
        if registration is not None
        else launcher.await_runtime_registration(
            config,
            runtime_wait_roles or config.roles,
            alive=lambda role: role.role in live,
            timeout_seconds=runtime_wait_seconds,
            print_func=print_func,
        )
    )
    for role in waited.exited:
        problems.append(
            f"{role} has no running session, so it will not register a runtime"
        )
    for role in waited.missing:
        problems.append(
            f"{role} did not register a runtime with the board within "
            f"{runtime_wait_seconds:g}s"
        )
    if waited.problem:
        problems.append(waited.problem)
    if session_statuses is not None:
        # The session records, when a caller supplies them: a second reading of
        # the same registration, from the tenant's own session directory.
        found = {status.role for status in session_statuses if status.found}
        for role in config.roles:
            if role.role not in found and role.role not in waited.missing and role.role not in waited.exited:
                problems.append(
                    f"{role.role} has not registered a runtime session with the board"
                )
    return problems


def _acl_entries(path: Path, *, runner: Callable[..., subprocess.CompletedProcess[Any]]) -> list[str]:
    done = runner(
        ["getfacl", "-cpn", str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
    )
    if getattr(done, "returncode", 1) != 0:
        return []
    return [
        line for line in str(getattr(done, "stdout", "") or "").splitlines()
        if line and not line.startswith("#")
    ]


def repository_boundary_problems(
    plan: "ProjectBoardProvision",
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> list[str]:
    """What is still open on this tenant's repository surfaces.

    Two things reach a tenant's worktrees, and they are independent: the mode
    bits, and a named entry to the socket group the board service must be in.
    Both are asked about here, of the filesystem rather than of a plan, because
    the question is what this host grants right now (SYRD-171, SYRD-175).
    """
    from scripts import team_launcher as launcher

    from scripts.ticket_board.project_provision import (
        roles_group_name,
        tenant_control_repository,
        tenant_worktree_base,
    )

    # The surfaces are derived from the plan, so a plan that does not describe a
    # tenant cannot be read as one that is closed. Readiness asking about a plan
    # like that is told it was not checked rather than told it is fine: silence
    # here is exactly the way an open boundary got past a recovery before
    # (SYRD-175, audit of bdd2c17).
    named = [field for field in ("project", "owner_home", "role_worktrees") if not hasattr(plan, field)]
    if named:
        return [
            f"{getattr(plan, 'project', 'this project')}'s repository boundary was not "
            f"checked: the plan it was asked about names no {', no '.join(named)}"
        ]

    problems: list[str] = []
    base = Path(tenant_worktree_base(plan))
    control = Path(tenant_control_repository(plan))
    socket_group = roles_group_name(plan.project)
    # `getfacl -n` answers in gids, so the group is resolved to one. A group
    # this host does not have grants nothing and is nothing to detect -- which
    # is the same thing the packet's own `getent group` guard concludes.
    try:
        socket_gid: int | None = grp.getgrnam(socket_group).gr_gid
    except KeyError:
        socket_gid = None
    stale = tuple(
        prefix
        for gid in ((socket_gid,) if socket_gid is not None else ())
        for prefix in (f"group:{gid}:", f"default:group:{gid}:")
    )

    if base.is_dir():
        mode = stat.S_IMODE(base.stat().st_mode)
        if mode & 0o007:
            problems.append(f"{base} is mode {mode:04o}, which anybody on this host can enter")
        # What the base will do to the NEXT worktree, not only what it did to
        # the last one. Without a default entry closing `other`, every tree a
        # role pane or an implementer creates here is made with the ordinary
        # umask and is world-readable -- which is how a base repaired at syrd
        # journal 0090 was carrying two open ticket worktrees by 0092
        # (SYRD-181).
        entries = launcher._acl_entries(base, runner=runner)
        inherited = [line for line in entries if line.startswith("default:other::")]
        if not inherited:
            problems.append(
                f"{base} grants no inherited closure, so every worktree created under it "
                "from now on will be world-readable"
            )
        elif any(line.split(":")[-1].strip("-") for line in inherited):
            granted = ", ".join(sorted(line.split(":")[-1] for line in inherited))
            problems.append(
                f"{base} passes {granted} to everything created under it, so new worktrees "
                "are readable beyond the tenant"
            )
        open_children = sorted(
            child.name
            for child in base.iterdir()
            if child.is_dir() and stat.S_IMODE(child.stat().st_mode) & 0o007
        )
        if open_children:
            shown = ", ".join(open_children[:3])
            more = f" and {len(open_children) - 3} more" if len(open_children) > 3 else ""
            problems.append(
                f"{len(open_children)} worktree(s) under {base} are world-readable: {shown}{more}"
            )
        if stale and any(line.startswith(stale) for line in launcher._acl_entries(base, runner=runner)):
            problems.append(
                f"{base} still grants {socket_group}, which is the board socket's group"
            )
    if control.is_dir() and stale:
        if any(line.startswith(stale) for line in launcher._acl_entries(control, runner=runner)):
            problems.append(
                f"{control} still grants {socket_group} -- the group the board service is in"
            )
    return problems
