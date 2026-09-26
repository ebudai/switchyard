"""Moving a tenant's roles onto their own accounts: the root script, its trusted copy and the operator's instruction.

A tenant provisioned before per-role accounts runs every role as its owner.
Migrating it gives each role its own account, and only root can do that, so
Switchyard renders a script for root to run rather than doing it itself:
- `render_role_account_migration` renders the script. It guards on each
  account's existence (`account_existence_guard`), then grants each role
  exactly the access it needs:
  - its paths (`role_path_access_commands`);
  - the commit stores it reads (`configured_commit_store_paths`,
    `commit_store_read_commands_for`);
  - its confined repository copy (`repository_copy_confinement_commands_for`);
  - the director's control access (`director_control_access_commands_for`);
  - the named-user ACLs it replaces (`named_acl_users`, `_ACL_NAMED_USER`).
- `publish_role_account_migration` writes the script where only root can put
  it. `trusted_role_account_migration_path` and
  `_privileged_artifact_boundary` name that place and prove who wrote it;
  `remove_untrusted_role_account_migration` removes a copy that fails the
  proof; `_privileged_directory_is_closed` checks its directory.
- `role_account_migration_instruction` tells the operator what to run.
- `upgrade_role_accounts_in_config` is the config half of the upgrade.

Naming role accounts, resolving users and homes, and the privileged provision
root and its modes stay in `scripts/team_launcher.py`. This module reads them
from there when a function runs, so the suites' patches on the launcher reach
it. Cutting the running roles over to their new identities is not here.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-305). `team_launcher`
imports this module at its top and still exports every name callers read there.
This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig


def upgrade_role_accounts_in_config(config_path: Path, *, dry_run: bool = False) -> tuple[bool, str]:
    """Retired compatibility entry point; account expansion is forbidden."""
    return False, "team-launcher: per-role Unix account creation is retired (SYRD-69)"


def role_path_access_commands(config: ProjectConfig) -> list[str]:
    """ACL grants that make each role's own worktree and git metadata reachable."""
    from scripts import team_launcher as launcher

    from scripts.ticket_board.project_provision import (
        PathContainmentError,
        repository_group_name,
        role_worktree_access_commands,
        roles_group_name,
    )

    owner = config.run_as_user or launcher.current_user_name()
    owner_home = str(launcher.home_dir_for_user(owner) or Path("/home") / owner)
    worktree_base = str(
        config.worktree_base
        or (Path(config.roles[0].workdir).parent if config.roles else Path(owner_home))
    )
    control = config.control_repository
    if control is None:
        return []
    try:
        return role_worktree_access_commands(
            owner_home=owner_home,
            repository_group=repository_group_name(config.project),
            worktree_base=worktree_base,
            control_repository=str(control.expanduser()),
            # The grant this used to make, taken back off the repository it was
            # made on. The socket group keeps the socket and loses the git
            # repository it was never meant to carry (SYRD-157).
            retired_groups=(roles_group_name(config.project),),
        )
    except PathContainmentError as exc:
        # A refusal, not a crash: the artifact goes to an operator who runs it
        # as root, so a grant computed from a path that is not where it was
        # said to be must not be written at all (SYRD-49).
        print(
            f"switchyard: no role path access granted for {config.project}: {exc}",
            file=sys.stderr,
        )
        return []


def configured_commit_store_paths(project: str, owner_home: Path) -> list[str]:
    """The repositories this tenant's board actually resolves commits against.

    Read from the installed unit rather than assumed, because the two tenants
    this has to repair disagree: a freshly provisioned one resolves against its
    own `control.git`, and syrd resolves against a separate source cache the
    deploy selected. Granting the default on a host that selected something else
    would leave the board unable to verify a commit and hand the service a
    repository it does not use -- both halves wrong at once. The default is the
    fallback for a tenant whose unit is not installed yet (SYRD-157).
    """
    from scripts.ticket_board.commit_repos import default_commit_git_dirs

    unit = Path("/etc/systemd/system") / f"{project}-ticket-board.service"
    raw = ""
    try:
        for line in unit.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "Environment":
                name, has_value, setting = value.strip().partition("=")
                if has_value and name.strip() == "TICKET_BOARD_COMMIT_GIT_DIR":
                    raw = setting.strip().strip('"')
    except OSError:
        raw = ""
    if raw:
        return [entry for entry in raw.split(os.pathsep) if entry.strip()]
    return [str(path) for path in default_commit_git_dirs(project, owner_home=owner_home)]


def commit_store_read_commands_for(config: ProjectConfig) -> list[str]:
    """Read-only commit resolution for the board service, on the selected store."""
    from scripts import team_launcher as launcher

    from scripts.ticket_board.project_provision import (
        DEFAULT_SERVICE_USER,
        PathContainmentError,
        commit_store_read_commands,
    )

    owner = config.run_as_user or launcher.current_user_name()
    owner_home = Path(str(launcher.home_dir_for_user(owner) or Path("/home") / owner))
    commands: list[str] = []
    for store in configured_commit_store_paths(config.project, owner_home):
        try:
            commands.extend(
                commit_store_read_commands(
                    owner_home=str(owner_home),
                    service_user=DEFAULT_SERVICE_USER,
                    commit_git_dir=store,
                )
            )
        except PathContainmentError as exc:
            print(
                f"switchyard: no commit-store grant for {config.project}: {exc}",
                file=sys.stderr,
            )
    return commands


def repository_copy_confinement_commands_for(config: ProjectConfig) -> list[str]:
    """Close every repository copy under the tenant home that is not already closed."""
    from scripts import team_launcher as launcher

    from scripts.ticket_board.project_provision import (
        PathContainmentError,
        WRITABLE_REPOSITORY_COPY_MODE,
        repository_copy_confinement_commands,
    )

    owner = config.run_as_user or launcher.current_user_name()
    owner_home = Path(str(launcher.home_dir_for_user(owner) or Path("/home") / owner))
    read_only: list[str] = [
        str(config.worktree_base) if config.worktree_base else "",
        *configured_commit_store_paths(config.project, owner_home),
    ]
    control = str(config.control_repository.expanduser()) if config.control_repository else ""
    commands: list[str] = []
    try:
        commands.extend(
            repository_copy_confinement_commands(
                owner_user=owner,
                owner_home=str(owner_home),
                repositories=[path for path in read_only if path and path != control],
            )
        )
        if control:
            # The one the roles WRITE, so it keeps group bits: closing it to
            # 0750 would recompute the ACL mask and clip the grant that makes a
            # role able to record a commit at all.
            commands.extend(
                repository_copy_confinement_commands(
                    owner_user=owner,
                    owner_home=str(owner_home),
                    repositories=[control],
                    mode=WRITABLE_REPOSITORY_COPY_MODE,
                )
            )
    except PathContainmentError as exc:
        print(
            f"switchyard: no repository confinement for {config.project}: {exc}",
            file=sys.stderr,
        )
        return []
    return commands


def director_control_access_commands_for(
    config: ProjectConfig, *, config_path: Path | None = None
) -> list[str]:
    """ACL grants that let the configured director run its own commands."""
    from scripts import team_launcher as launcher

    from scripts.ticket_board.project_provision import (
        PathContainmentError,
        director_control_access_commands,
    )

    director, reason = launcher.control_role_name(config, config_path=config_path)
    if not director:
        print(f"switchyard: no control-role access granted for {config.project}: {reason}", file=sys.stderr)
        return []
    role = launcher._role_by_name(config, director)
    account = (role.run_as_user if role is not None else "") or launcher.role_account_name(
        config.project, director
    )
    accounts = {
        candidate.run_as_user
        for candidate in config.roles
        if candidate.role == director and candidate.run_as_user
    }
    if len(accounts) > 1:
        print(
            f"switchyard: no control-role access granted for {config.project}: {director} resolves "
            f"to more than one account ({', '.join(sorted(accounts))})",
            file=sys.stderr,
        )
        return []
    owner = config.run_as_user or launcher.current_user_name()
    owner_home = str(launcher.home_dir_for_user(owner) or Path("/home") / owner)
    project_dir = launcher._project_dir_from_generated_config_path(config_path) if config_path else None
    if project_dir is None and config.repository is not None:
        project_dir = config.repository
    provision_dir = (
        str(config_path.parent)
        if config_path is not None
        else str((project_dir or Path(owner_home)) / ".switchyard" / "provision")
    )
    try:
        return director_control_access_commands(
            owner_home=owner_home,
            director_account=account,
            provision_dir=provision_dir,
            project_dir=str(project_dir or ""),
        )
    except PathContainmentError as exc:
        print(
            f"switchyard: no control-role access granted for {config.project}: {exc}",
            file=sys.stderr,
        )
        return []


#: The principal an ACL entry names, in any of the forms setfacl accepts.
_ACL_NAMED_USER = re.compile(r"(?:^|[\s,])(?:d:)?u:([A-Za-z0-9_][A-Za-z0-9_.-]*):")


def named_acl_users(commands: Sequence[str]) -> list[str]:
    """The Unix accounts a rendered set of ACL commands names."""
    found: list[str] = []
    for command in commands:
        if "setfacl" not in command:
            continue
        for account in _ACL_NAMED_USER.findall(command):
            if account not in found:
                found.append(account)
    return sorted(found)


def account_existence_guard(accounts: Sequence[str]) -> list[str]:
    """Refuse with the reason rather than with setfacl's diagnostic.

    setfacl rejects a principal the host does not know, and its message --
    `Option -m: Invalid argument near character 3` -- says nothing about which
    account or why. This script creates those accounts itself, so reaching a
    named-user grant without one means the grant was emitted too early; say so
    instead of aborting the migration half-done on a character offset (SYRD-53).
    """
    from scripts.ticket_board.project_provision import shell_quote

    lines: list[str] = []
    for account in accounts:
        quoted = shell_quote(account)
        lines.extend(
            [
                f"if ! getent passwd {quoted} >/dev/null 2>&1; then",
                f"    echo \"{account} does not exist yet; its ACL grant is out of order\" >&2",
                "    exit 1",
                "fi",
            ]
        )
    return lines


def render_role_account_migration(
    config: ProjectConfig,
    *,
    config_path: Path | None = None,
    resume_source: Mapping[str, str] | None = None,
) -> str:
    """Operator commands that move an existing tenant onto per-role accounts.

    Creating the accounts is not enough on its own: a role also has to own the
    working tree and runtime state it uses, or it cannot write them once it
    stops running as the project owner. Every step is guarded or idempotent so
    the artifact is safe to re-run.
    """
    from scripts import team_launcher as launcher

    from scripts.ticket_board.project_provision import (
        DEFAULT_SERVICE_USER,
        render_role_control_sudoers,
        repository_group_commands,
        role_control_sudoers_install_commands,
        role_runtime_commands,
        role_tooling_staging_commands,
        roles_group_name,
        shell_quote,
    )

    owner = config.run_as_user or launcher.current_user_name()
    group = roles_group_name(config.project)
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        f"# Migrate {config.project} onto one Unix account per role (SYRD-39).",
        "# Safe to re-run: every creating step is guarded.",
        f"if ! getent group {shell_quote(group)} >/dev/null 2>&1; then",
        f"    sudo groupadd -r {shell_quote(group)}",
        "fi",
        # The board service is in the socket group and has to be: that is how
        # the socket reaches the roles. It is deliberately NOT in the repository
        # group created next, which is the whole point of having two (SYRD-157).
        f"sudo gpasswd -a {shell_quote(DEFAULT_SERVICE_USER)} {shell_quote(group)} >/dev/null",
        f"sudo gpasswd -a {shell_quote(owner)} {shell_quote(group)} >/dev/null",
    ]
    # Created before the grants below, not with the memberships after them:
    # setfacl refuses a group principal the host does not know, and the grants
    # name this group while the accounts that join it may not exist yet.
    lines.extend(repository_group_commands(config.project, [owner]))
    # Closed BEFORE anything is granted on it, and not the other way round:
    # `chmod` recomputes the ACL mask from the group bits, so confining a
    # repository after granting it would clip the grant that was just made
    # (SYRD-157).
    lines.extend(repository_copy_confinement_commands_for(config))
    # From the pinned shared release, not the tenant's deployed board: the
    # deployed one is the release being replaced (SYRD-45).
    lines.extend(
        launcher.role_tooling_staging_commands(
            config.project, str(launcher.switchyard_shared_install_root() / "current")
        )
    )
    # Owning a worktree is not reaching it: the owner home above it is 0710.
    # These grant traversal and the shared git metadata, and nothing else
    # (SYRD-49).
    # Group principals only, and the group is created above, so these are safe
    # before any account exists. Named-user grants are not, and are emitted
    # after the loop below (SYRD-53).
    lines.extend(role_path_access_commands(config))
    # Close the tenant's own tree before anything else is granted a way through
    # the home. This is the repair half of SYRD-156: an existing tenant has
    # `Projects` root-owned at 0755 above a checkout the tenant owns at 0755,
    # and every principal granted traversal -- the board service included, since
    # it is a member of the roles group added at the top of this artifact --
    # could read the source. Naming each directory moves the parent back to the
    # tenant and closes both; re-running changes nothing.
    if config.repository is not None:
        from scripts.ticket_board.project_provision import tenant_source_confinement_commands

        lines.extend(
            tenant_source_confinement_commands(
                owner_user=owner,
                owner_home=f"/home/{owner}",
                checkout=str(config.repository),
            )
        )
    for role in config.roles:
        # Canonical rather than configured: this artifact is what CREATES the
        # accounts, so it has to be renderable before the configuration names
        # them. Writing the configuration first is what produced a tenant whose
        # roles pointed at accounts that did not exist (SYRD-45).
        account = role.run_as_user or launcher.role_account_name(config.project, role.role)
        if not account or account == owner:
            continue
        lines.extend(
            role_runtime_commands(
                project=config.project,
                runtime_directory=f"{config.project}-ticket-board",
                board_current=f"/home/{owner}/{config.project}-ticketboard-live/current",
                roles_group=group,
                account=account,
                home=f"/home/{account}",
                # Deliberately not the worktree. Handing a tree to the new
                # account while the old one is still working in it takes write
                # access away from a live implementer mid-task; ownership moves
                # inside the cutover, after the workers are stopped (SYRD-45).
                worktree="",
            )
        )
    # Now that the accounts exist, put them in the repository group -- and only
    # them. Membership is what carries git access now, so the list is exactly
    # the accounts that use git: the owner and the roles, never the board
    # service (SYRD-157).
    repository_members = [owner]
    for role in config.roles:
        account = role.run_as_user or launcher.role_account_name(config.project, role.role)
        if account and account != owner and account not in repository_members:
            repository_members.append(account)
    lines.extend(account_existence_guard([a for a in repository_members if a != owner]))
    lines.extend(repository_group_commands(config.project, repository_members))
    # The board service resolves commits against one repository and is granted
    # read on that one alone, by name, with no write anywhere (SYRD-157).
    lines.extend(commit_store_read_commands_for(config))
    # Now the grants that name a role account. The control role is
    # whichever role the workflow gives the control capabilities, so this is
    # derived from the configuration rather than from a role name (SYRD-49), and
    # it is here rather than above because setfacl rejects a principal the host
    # does not know yet (SYRD-53).
    control_grants = director_control_access_commands_for(config, config_path=config_path)
    if control_grants:
        lines.append(
            "# Let the control role reach its own configuration. Its account exists by now:"
        )
        lines.extend(account_existence_guard(named_acl_users(control_grants)))
        lines.extend(control_grants)
    # The document goes in the artifact rather than a command that regenerates it:
    # this named `switchyard provision`, which the CLI does not have, so the setup
    # failed here before the rule was ever installed (SYRD-51).
    control = role_control_sudoers_install_commands(
        config.project,
        render_role_control_sudoers(config.project, owner, launcher.role_control_accounts(config)),
    )
    if control:
        lines.append(
            "# Refresh the director control interface for the current role set:"
        )
        lines.extend(control)
    # Repair reinstalls the lifecycle bridge too, so a tenant provisioned before
    # it existed gains one, and an existing one is rewritten to the same bytes.
    from scripts.ticket_board.project_provision import tenant_control_commands

    lines.extend(
        tenant_control_commands(
            config.project,
            owner,
            launcher.resolve_control_user(
                config.project, invoking_user=launcher.invoking_human(), owner_user=owner
            ),
        )
    )
    lines.append(
        "# Give each role its own private copy of the credentials its CLI needs."
    )
    lines.append(
        "# Idempotent: a role that already has one is left alone; --reseed replaces it."
    )
    lines.append(f"sudo switchyard seed-role-credentials {config.project}")
    lines.append(
        "# The accounts now exist, but the running workers still hold the shared"
    )
    lines.append(
        "# uid. Moving them is root's next phase, not yours: one transaction stops"
    )
    lines.append(
        "# the roles, transfers their worktrees, switches the board release,"
    )
    lines.append(
        "# installs the authority units, restarts and health-checks the board, and"
    )
    lines.append(
        "# brings every role back under its own account. Rerun the upgrade, which"
    )
    lines.append(
        "# runs whichever phase is next in order (SYRD-45)."
    )
    # What that rerun will deploy, named here so an operator can see it before
    # running it. The selection itself does not travel in this file: `sudo`
    # scrubs the environment and this script is written into a directory the
    # tenant owns, so the arguments would be the tenant's to change. Root reads
    # it back from its own record instead (SYRD-61).
    pinned = dict(resume_source or {})
    if any(pinned.get(key) for key in ("source_repo", "commit_git_dir", "deploy_ref")):
        lines.append(
            "# It deploys the release this upgrade was pinned to, which root recorded where"
        )
        lines.append(
            "# only root can write it, so sudo's scrubbed environment cannot lose it:"
        )
        for label, key in (
            ("source ", "source_repo"),
            ("cache  ", "commit_git_dir"),
            ("ref    ", "deploy_ref"),
        ):
            if pinned.get(key):
                lines.append(f"#     {label} {pinned[key]}")
    else:
        lines.append(
            "# No pinned release was recorded for this upgrade. If it cannot resolve the"
        )
        lines.append(
            "# release to deploy, rerun it with --source-repo, --commit-git-dir and"
        )
        lines.append("# --deploy-ref, which it will then record for any later phase.")
    lines.append(f"sudo switchyard upgrade {config.project}")
    lines.append(
        "# That transaction deploys and restarts the board itself, so there is no"
    )
    lines.append(
        "# second restart to make here, and no release left for you to deploy."
    )
    lines.append(
        "# What remains is the director's own board write, which is authorized"
    )
    lines.append(
        "# from the director's uid and cannot be made by root or by you:"
    )
    lines.append(
        f"#     switchyard finish-upgrade {config.project}    # in the director's own session"
    )
    lines.append(
        "# (SYRD-48)."
    )
    return "\n".join(lines) + "\n"


def trusted_role_account_migration_path(config: ProjectConfig) -> Path:
    """Where root publishes the migration script, and where an operator runs it.

    Not beside the launcher configuration. That directory belongs to the tenant
    and the control role is granted `rwX` on it recursively, with a default
    entry so replacements keep the grant -- so the root-owned script an operator
    was told to run as root carried a named `rwx` entry for a role account, in a
    directory that role could empty and refill. The two cannot share a
    directory: the projection has to be writable by the control role and a
    root-run script must not be (SYRD-62).
    """
    from scripts import team_launcher as launcher

    return launcher.privileged_provision_dir(
        config.project, root=launcher.switchyard_privileged_provision_root()
    ) / launcher.role_account_migration_name(config.project)


def _privileged_artifact_boundary() -> Path | None:
    """How far up a published artifact's path root has to answer for.

    On a host, all the way to the filesystem root: `/etc` and `/etc/switchyard`
    are root's and are checked like everything else. `None` says so. When the
    privileged root has been redirected -- which only a test does -- everything
    above the redirection belongs to whoever set that up, so the walk stops
    there and the check still covers every directory this code created
    (SYRD-62).
    """
    from scripts import team_launcher as launcher

    configured = launcher.switchyard_privileged_provision_root()
    if configured == launcher.DEFAULT_PRIVILEGED_PROVISION_ROOT:
        return None
    return configured


def remove_untrusted_role_account_migration(
    config: ProjectConfig, *, config_path: Path, print_func: Callable[[str], None] = print
) -> list[str]:
    """Take the tenant-side copy of the root-run migration script away.

    On every upgrade, not only when an operator step remains. The resume this
    came from has every account already, so the branch that publishes the
    trusted copy is skipped and a file at the known role-writable path would
    simply survive a successful upgrade -- which is the whole finding, still
    sitting there afterwards.

    This is root deleting a file inside a tree the tenant controls, so the path
    is walked component by component with `O_NOFOLLOW` on every one of them,
    from the filesystem root. `O_NOFOLLOW` on the directory alone refuses only
    its own last component: an ancestor several levels up -- `.switchyard`, say
    -- can be replaced with a symlink, and the kernel follows it, so root
    unlinks `<project>-role-accounts.sh` in whatever tree it points at. Any
    ancestor that is a symlink, or that cannot be opened safely, is a refusal
    and nothing is unlinked.

    Nothing here reads the file, parses it or runs it. Existence is asked and
    the unlink is made through the same descriptor, so there is no window
    between the two, and a stale copy that is itself a symlink is removed as the
    link rather than followed (SYRD-62).
    """
    from scripts import team_launcher as launcher

    name = launcher.role_account_migration_name(config.project)
    stale = config_path.with_name(name)
    provision_dir = config_path.parent
    if not provision_dir.is_absolute():
        return [f"refusing to remove {stale}: {provision_dir} is not an absolute path"]
    relative = Path(str(provision_dir).lstrip("/")) / name
    directory, problem = launcher._walk_no_follow(Path(provision_dir.anchor or "/"), relative)
    if directory < 0:
        if problem == "missing":
            # Nothing there to take away.
            return []
        return [f"refusing to remove {stale}: {problem}"]
    try:
        try:
            os.lstat(name, dir_fd=directory)
        except FileNotFoundError:
            return []
        except OSError as exc:
            return [f"could not check the tenant-writable {stale}: {exc}"]
        try:
            os.unlink(name, dir_fd=directory)
        except FileNotFoundError:
            return []
        except OSError as exc:
            return [f"could not remove the tenant-writable {stale}: {exc}"]
    finally:
        os.close(directory)
    print_func(
        f"switchyard: removed {stale}. The migration an operator runs as root is published where "
        "only root can write it, and a copy in this directory is one the control role can rewrite."
    )
    return []


def publish_role_account_migration(
    config: ProjectConfig,
    *,
    config_path: Path,
    resume_source: Mapping[str, str] | None = None,
    # Looked up when called rather than bound here, so a caller that was handed
    # its own answer about being root passes it and everyone else asks the
    # process itself at the time.
    euid_getter: Callable[[], int] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> tuple[Path | None, list[str]]:
    """Publish the migration script somewhere only root could have written it.

    Returns the path an operator may run, or `None` and the reasons it is not
    one. The tenant's old copy is removed rather than left behind: a second file
    of the same name, executable and writable by the control role, is the thing
    this exists to stop somebody running (SYRD-62).
    """
    from scripts import team_launcher as launcher

    if (os.geteuid() if euid_getter is None else euid_getter()) != 0:
        return None, [
            f"only root can publish {config.project}'s role-account migration; run "
            f"`switchyard upgrade {config.project}` as root"
        ]
    name = launcher.role_account_migration_name(config.project)
    body = render_role_account_migration(
        config, config_path=config_path, resume_source=resume_source
    ).encode("utf-8")
    target = launcher.privileged_provision_dir(config.project, root=launcher.switchyard_privileged_provision_root())
    path = target / name
    try:
        launcher.ensure_privileged_provision_dir(target)
        staged = target / f".{name}.new"
        descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.write(descriptor, body)
            os.fchmod(descriptor, launcher.privileged_artifact_mode(name))
            try:
                os.chown(target, 0, 0)
                os.fchown(descriptor, 0, 0)
            except OSError:
                # Attempted, not relied on: what decides whether an operator is
                # sent here is the check below, which reads the ownership and
                # the access control entries back off the published file.
                pass
        finally:
            os.close(descriptor)
        staged.replace(path)
    except OSError as exc:
        return None, [f"could not publish {path}: {exc}"]
    # The tenant copy is not a second way to run this. The artifacts phase has
    # already taken it away on any upgrade; this is the same removal for the
    # callers that reach publishing without one.
    for problem in remove_untrusted_role_account_migration(
        config, config_path=config_path, print_func=print_func
    ):
        print_func(f"switchyard: {problem}. It is not the file to run; {path} is.")
    reasons = launcher.untrusted_root_executable_reasons(
        path, boundary=_privileged_artifact_boundary(), runner=runner
    )
    if reasons:
        return None, reasons
    return path, []


def _privileged_directory_is_closed(directory: Path) -> bool:
    """Whether this is root's directory, closed, that this caller cannot read.

    `Path.exists()` inside it answers False either way, and the difference
    matters: one is something to publish, the other is something published
    (SYRD-176).
    """
    from scripts import team_launcher as launcher

    try:
        info = directory.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == launcher.expected_privileged_uid()
        and not stat.S_IMODE(info.st_mode) & 0o077
        and not os.access(directory, os.R_OK | os.X_OK)
    )


def role_account_migration_instruction(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> tuple[Path | None, list[str]]:
    """The published script an unprivileged caller may point an operator at.

    Naming one is a decision about what root will execute, so it is checked
    rather than assumed present: this caller cannot write the trusted copy and
    must not send anybody to an untrusted one (SYRD-62).
    """
    from scripts import team_launcher as launcher

    path = trusted_role_account_migration_path(config)
    if not path.exists():
        # Root's provisioning directory is root's to read, so an unprivileged
        # caller cannot tell a script that is not there from one it may not
        # look at. Saying "not published" for the second would send an operator
        # to regenerate something that is already waiting for them, so the two
        # are told apart by what can be seen: the directory itself (SYRD-176).
        if os.geteuid() != 0 and _privileged_directory_is_closed(path.parent):
            return None, [
                f"{path.parent} is root's and closed, so this caller cannot tell whether "
                f"{path.name} is published there, and must not name a script it could not "
                "check"
            ]
        return None, [
            f"{path} has not been published yet; run `switchyard upgrade {config.project}` as root"
        ]
    reasons = launcher.untrusted_root_executable_reasons(
        path, boundary=_privileged_artifact_boundary(), runner=runner
    )
    if reasons:
        return None, reasons
    return path, []
