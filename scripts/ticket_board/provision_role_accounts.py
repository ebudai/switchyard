"""The per-role Unix accounts a tenant runs its roles as, and the control rule that lets its director drive them.

Who the role accounts are (`NON_PROCESS_ROLES`, `role_account_name`,
`roles_group_name`, `role_account_table`, `role_accounts_env`,
`role_account_home`); the operator commands that create them and give each one
its runtime (`role_accounts_command`, `role_runtime_command`,
`role_runtime_commands`, `role_account_commands`, `ROLE_ACCOUNT_MIGRATION_SUFFIX`,
`role_account_migration_name`); and the sudoers rule that lets the director
account reach each role's tmux and control interface, with the commands that
install it (`render_role_control_sudoers`, `role_control_sudoers`,
`ROLE_CONTROL_SUDOERS_HEREDOC`, `role_control_sudoers_install_commands`).

Moved out of `scripts/ticket_board/project_provision.py` unchanged (SYRD-470).
`project_provision` imports this module and re-exports every name, so every
module and test that imports them from there, or patches them there, still
reaches the same objects. What they read of `project_provision` -- each other,
`shell_quote`, `SHARED_RELEASE_CURRENT`, the repository group, the role tooling
staging and the tenant control -- is read through it when they run, so a patch
there still reaches them. This module imports `project_provision` only inside
the functions that need it, when they run, with the same fallback for direct
script execution.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Sequence


# Roles that never run a local process: the human uses the browser or the
# authenticated HTTP path, so no Unix account is created for them.
NON_PROCESS_ROLES = frozenset({"user", "unassigned"})


def role_account_name(project: str, role: str) -> str:
    """The Unix account that runs one role of one project."""
    return f"{project}-{role}"


def roles_group_name(project: str) -> str:
    """The group whose members may reach this project's board socket."""
    return f"{project}-roles"


def role_account_table(project: str, caller_roles: Sequence[str]) -> tuple[tuple[str, str], ...]:
    """(role, account) for every role that runs as a local process.

    Derived from the project's declared caller roles, so adding or renaming a
    role in configuration is enough; nothing here knows what a director is.
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    seen: list[tuple[str, str]] = []
    for role in caller_roles:
        name = str(role).strip().lower()
        if not name or name in provision.NON_PROCESS_ROLES:
            continue
        if any(existing == name for existing, _ in seen):
            continue
        seen.append((name, provision.role_account_name(project, name)))
    return tuple(seen)


def role_accounts_env(plan: "ProjectBoardProvision") -> str:
    return ",".join(f"{role}={account}" for role, account in plan.role_accounts)


def role_accounts_command(plan: ProjectBoardProvision) -> str:
    """Create one Unix account per role, plus the group that reaches the socket.

    This is the boundary the board relies on: SO_PEERCRED reports a uid the
    caller cannot choose, so giving each role its own account is what makes the
    role mapping authoritative rather than advisory (SYRD-39).

    Idempotent, and safe to re-run on an existing tenant: accounts and group
    memberships that already exist are left alone, which is what makes this
    double as the migration for a tenant that previously ran every role under
    one account.
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    if not plan.role_accounts or not plan.roles_group:
        return ""
    q_group = provision.shell_quote(plan.roles_group)
    q_owner = provision.shell_quote(plan.owner_user)
    q_service = provision.shell_quote(plan.service_user)
    worktrees = dict(plan.role_worktrees)
    lines = [
        f"if ! getent group {q_group} >/dev/null 2>&1; then",
        f"    sudo groupadd -r {q_group}",
        "fi",
        # The board service must be able to hand the socket to the group, and
        # the tenant owner keeps its existing operational access. This group is
        # the SOCKET and nothing else; repositories are granted to the group
        # below, which the service is deliberately not in (SYRD-157).
        f"sudo gpasswd -a {q_service} {q_group} >/dev/null",
        f"sudo gpasswd -a {q_owner} {q_group} >/dev/null",
    ]
    lines.extend(
        provision.repository_group_commands(
            plan.project, [plan.owner_user, *(account for _role, account in plan.role_accounts)]
        )
    )
    for role, account in plan.role_accounts:
        q_account = provision.shell_quote(account)
        q_home = provision.shell_quote(provision.role_account_home(plan, role))
        lines.extend(
            [
                f"if ! getent passwd {q_account} >/dev/null 2>&1; then",
                f"    sudo useradd -m -d {q_home} -s /bin/bash {q_account}",
                "fi",
                f"sudo gpasswd -a {q_account} {q_group} >/dev/null",
                # 0700 and the role's own group: the roles group exists for
                # the board socket, and must not make one role's home readable
                # to another (SYRD-39).
                f"sudo install -d -m 0700 -o {q_account} -g {q_account} {q_home}",
                f"sudo loginctl enable-linger {q_account} >/dev/null 2>&1 || true",
            ]
        )
    return "\n".join(lines)


def role_runtime_command(plan: ProjectBoardProvision) -> str:
    """Prepare each role's runtime AFTER the board and worktrees exist.

    The pane hooks and board skill are installed from the deployed release, and
    the worktrees are created by the launcher, so none of this can run at the
    same time as account creation (SYRD-39).
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    # The staged bundle first, and for EVERY tenant. It is root-owned, lives at
    # a shared path no tenant account can write, and is what a pane runs:
    # switchyard-display-attach, the tenant control client, the board skill
    # installer, the privileged action boundary. A modern single-owner tenant
    # has no per-role accounts, so this whole function used to return nothing
    # for it -- and its six panes opened onto
    # `sudo: /usr/local/lib/switchyard/<project>/switchyard-display-attach:
    # command not found` while provisioning reported success (SYRD-249).
    #
    # Only the per-account loop below depends on there being role accounts.
    worktrees = dict(plan.role_worktrees)
    lines: list[str] = list(provision.role_tooling_staging_commands(plan.project, provision.SHARED_RELEASE_CURRENT))
    if not plan.role_accounts or not plan.roles_group:
        return "\n".join(lines)
    for role, account in plan.role_accounts:
        lines.extend(
            provision.role_runtime_commands(
                project=plan.project,
                runtime_directory=plan.runtime_directory,
                board_current=plan.board_current,
                roles_group=plan.roles_group,
                account=account,
                home=provision.role_account_home(plan, role),
                worktree=worktrees.get(role, ""),
            )
        )
    return "\n".join(lines)


ROLE_ACCOUNT_MIGRATION_SUFFIX = "-role-accounts.sh"


def role_account_migration_name(project: str) -> str:
    """The operator script that moves a tenant onto per-role accounts."""
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    return f"{project}{provision.ROLE_ACCOUNT_MIGRATION_SUFFIX}"


def role_runtime_commands(
    *,
    project: str,
    runtime_directory: str,
    board_current: str,
    roles_group: str,
    account: str,
    home: str,
    worktree: str,
) -> list[str]:
    """Everything one role needs to run as its own account.

    Creating the account is not enough. A role also has to own the tree it works
    in and the runtime paths the launcher hands it, and it needs its own copy of
    the pane hooks and the board skill in its own home -- otherwise it starts
    under the right uid and cannot write, record state, or use the board
    (SYRD-39).
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    q_account, q_group = provision.shell_quote(account), provision.shell_quote(roles_group)
    q_home = provision.shell_quote(home)
    commands = [
        f"if ! getent passwd {q_account} >/dev/null 2>&1; then",
        f"    sudo useradd -m -d {q_home} -s /bin/bash {q_account}",
        "fi",
        f"sudo gpasswd -a {q_account} {q_group} >/dev/null",
        # 0700 and the role's own group. Membership of the roles group is for
        # reaching the board socket; it must never make one role's home
        # readable to another (SYRD-39).
        f"sudo install -d -m 0700 -o {q_account} -g {q_account} {q_home}",
        f"sudo loginctl enable-linger {q_account} >/dev/null 2>&1 || true",
    ]
    if worktree:
        # Guarded: on a fresh project the launcher has not created it yet, and
        # the rerun after first launch completes the handover.
        q_worktree = provision.shell_quote(worktree)
        commands.append(f"if [ -d {q_worktree} ]; then sudo chown -R {q_account}: {q_worktree}; fi")
    runtime_dirs = [
        f"{home}/.local/bin",
        f"{home}/.local/state/{runtime_directory}/pane-sessions",
        f"{home}/.config",
    ]
    for directory in runtime_dirs:
        commands.append(
            f"sudo install -d -m 0700 -o {q_account} -g {q_account} {provision.shell_quote(directory)}"
        )
    # Staged by root at a shared, world-readable path. A role account is only a
    # member of the roles group; the owner's home stays 0700/0710 and holds the
    # owner's credentials, so a role cannot -- and must not -- traverse it to
    # reach these executables (SYRD-39).
    staging = f"/usr/local/lib/switchyard/{project}"
    hook_source = provision.shell_quote(f"{staging}/ticket-board-pane-idle-hook")
    hook_installer = provision.shell_quote(f"{staging}/ticket-board-install-pane-hooks")
    skill_installer = provision.shell_quote(f"{staging}/switchyard-board-skill")
    hook_bin = provision.shell_quote(f"{home}/.local/bin/ticket-board-pane-idle-hook")
    session_dir = provision.shell_quote(f"{home}/.local/state/{runtime_directory}/pane-sessions")
    commands.extend(
        [
            f"sudo install -m 0755 -o {q_account} -g {q_account} {hook_source} {hook_bin}",
            # Run as the role account so the hook configuration lands in that
            # role's own CLI config and points at that role's own state, not the
            # owner's.
            f"sudo -u {q_account} -H env TICKET_BOARD_PROJECT={provision.shell_quote(project)} "
            f"TICKET_BOARD_PANE_SESSION_DIR={session_dir} "
            f"{hook_installer} install "
            f"--home {q_home} --hook-source {hook_source} --bin-path {hook_bin}",
            f"sudo -u {q_account} -H {skill_installer} install --home {q_home}",
        ]
    )
    return commands


def role_account_home(plan: ProjectBoardProvision, role: str) -> str:
    """Where one role account lives. Its worktree, config and credentials sit
    under here, owned by that account, so one role cannot read another's."""
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    return f"/home/{provision.role_account_name(plan.project, role)}"


def role_account_commands(
    project: str,
    role: str,
    owner_user: str,
    service_user: str,
    *,
    role_accounts: Sequence[tuple[str, str]],
    runtime_directory: str = "",
    board_current: str = "",
    worktree: str = "",
    owner_home: str = "",
    worktree_base: str = "",
    control_repository: str = "",
) -> str:
    """Operator commands to add ONE role's Unix account to an existing project.

    Adding a role after provisioning needs a new account, and the launcher does
    not hold root. This emits the same preparation fresh provisioning does --
    account, group, runtime paths, pane hooks, board skill and ownership of the
    role's worktree -- so a rerun of add-role finds a role that can actually
    write and use the board, not just an account that exists (SYRD-39).

    `role_accounts` is the project's whole role set including the one being
    added, and is required rather than defaulted: the control interface is one
    file installed whole, so rendering it from a partial set would revoke the
    grants of every role left out (SYRD-51).
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    group = provision.roles_group_name(project)
    account = provision.role_account_name(project, role)
    resolved_runtime = runtime_directory or f"{project}-ticket-board"
    resolved_board_current = board_current or f"/home/{owner_user}/{project}-ticketboard-live/current"
    lines = [
        f"if ! getent group {provision.shell_quote(group)} >/dev/null 2>&1; then",
        f"    sudo groupadd -r {provision.shell_quote(group)}",
        "fi",
        f"sudo gpasswd -a {provision.shell_quote(service_user)} {provision.shell_quote(group)} >/dev/null",
        f"sudo gpasswd -a {provision.shell_quote(owner_user)} {provision.shell_quote(group)} >/dev/null",
    ]
    lines.extend(provision.role_tooling_staging_commands(project, provision.SHARED_RELEASE_CURRENT))
    lines.extend(
        provision.role_runtime_commands(
            project=project,
            runtime_directory=resolved_runtime,
            board_current=resolved_board_current,
            roles_group=group,
            account=account,
            home=f"/home/{account}",
            worktree=worktree,
        )
    )
    resolved_owner_home = owner_home or f"/home/{owner_user}"
    if control_repository:
        # A role added later needs the same reachability as one provisioned with
        # the project: owning the leaf is not reaching it (SYRD-49). Through the
        # repository group, which the new account joins here and the board
        # service is not in (SYRD-157).
        lines.extend(
            provision.repository_group_commands(
                project, [owner_user, account, *(a for _role, a in role_accounts)]
            )
        )
        lines.extend(
            provision.role_worktree_access_commands(
                owner_home=resolved_owner_home,
                repository_group=provision.repository_group_name(project),
                worktree_base=worktree_base or str(PurePosixPath(worktree).parent) if worktree else resolved_owner_home,
                control_repository=control_repository,
                retired_groups=(group,),
            )
        )
    control = provision.role_control_sudoers_install_commands(
        project, provision.render_role_control_sudoers(project, owner_user, role_accounts)
    )
    if control:
        lines.append("# Refresh the director control interface so it can drive the new role:")
        lines.extend(control)
    # The tenant control bridge is a separate grant on a separate file, and it is
    # installed after the role control interface exactly as before (SYRD-50).
    lines.extend(
        provision.tenant_control_commands(
            project,
            owner_user,
            provision.resolve_control_user(
                project, invoking_user=provision.invoking_human(), owner_user=owner_user
            ),
        )
    )
    return "\n".join(lines)


def render_role_control_sudoers(
    project: str, owner_user: str, role_accounts: Sequence[tuple[str, str]]
) -> str:
    """Least-privilege control paths once each role has its own tmux server.

    Three grants, each `tmux` only, no root and no other command (SYRD-39):

    * the project owner may run tmux as any role account -- the notify listener
      is the owner's user service and delivers board notifications into role
      panes through directorctl, so without this ordinary notifications cannot
      reach a role at all;
    * the director account may run tmux as any other role account, for
      presentation and control of those sessions;
    * the director account may run tmux as the project owner, because the
      display and viewer sessions stay in the owner's server and the isolated
      director account would otherwise be unable to reach them.

    Takes the role set rather than a provisioning plan so the generated operator
    artifacts can embed the document they install. They used to shell out to
    `switchyard provision <project> --render role-control-sudoers`, which is not
    a command the CLI has, so the setup failed before the rule was installed
    (SYRD-51).
    """
    # SYRD-123: no publication grants. The project account holds the project's
    # GitHub credential again, so publishing a candidate is an ordinary push and
    # there is nothing here for root to do. What used to be granted -- two
    # root-owned programs the account could run under NOPASSWD -- is not
    # rendered any more, and an upgrade removes the rule from tenants that have
    # it. Nothing below grants root; every remaining entry is one tmux.
    publication: list[str] = []
    if not role_accounts:
        return ""
    director_account = next(
        (account for role, account in role_accounts if role == "director"),
        "",
    )
    role_targets = ",".join(account for _role, account in role_accounts)
    lines = [
        f"# {project}: role control interface. Each entry grants one command and",
        "# nothing else, so a holder can drive another account's tmux server, and gains",
        "# no other command and no root.",
        f"{owner_user} ALL=({role_targets}) NOPASSWD: /usr/bin/tmux",
    ]
    if director_account:
        director_targets = ",".join(
            account for _role, account in role_accounts if account != director_account
        )
        if director_targets:
            lines.append(f"{director_account} ALL=({director_targets}) NOPASSWD: /usr/bin/tmux")
        # Display and viewer sessions remain the owner's.
        lines.append(f"{director_account} ALL=({owner_user}) NOPASSWD: /usr/bin/tmux")
    return "\n".join(lines) + "\n"


def role_control_sudoers(plan: ProjectBoardProvision) -> str:
    """The role control interface for a whole provisioning plan."""
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    return provision.render_role_control_sudoers(plan.project, plan.owner_user, plan.role_accounts)


ROLE_CONTROL_SUDOERS_HEREDOC = "SWITCHYARD_ROLE_CONTROL_SUDOERS"


def role_control_sudoers_install_commands(project: str, document: str) -> list[str]:
    """Install a role control interface the artifact carries with it.

    The document is embedded rather than regenerated at run time: an artifact
    that shells out to regenerate it can only be as reliable as the command it
    names, and the one it named does not exist (SYRD-51). Staging is unchanged
    and deliberate -- the bytes go straight to a root-owned file through
    /dev/stdin rather than through a path an unprivileged process could replace,
    `visudo -c` validates it before it can be read as policy, and only a valid
    file is given the name sudo actually loads.

    Returns no commands at all for an empty document: this file is installed
    whole, so writing a partial one would revoke the grants it omits.
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    if not document.strip():
        return []
    target = f"/etc/sudoers.d/49-{project}-role-control"
    marker = provision.ROLE_CONTROL_SUDOERS_HEREDOC
    return [
        f"sudo install -m 0440 -o root -g root /dev/stdin {target}.staged <<'{marker}'",
        document.rstrip("\n"),
        marker,
        f"sudo visudo -c -f {target}.staged",
        f"sudo mv {target}.staged {target}",
    ]
