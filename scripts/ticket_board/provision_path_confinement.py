"""How provisioning confines a tenant's paths: the containment checks and the confinement commands.

The checks that one path lies within another and that a prefix which merely
coincides is not containment, and the directories between a root and a path.
On top of them, the operator commands that give or take away access along the
owner home, the tenant's source release, its worktrees, the commit store,
repository copies, the retired socket group, a role's worktree and the
director's control repository, with the modes and default ACLs they set
(`TENANT_SOURCE_MODE`, `INHERITED_WORKTREE_CLOSURE`, `REPOSITORY_COPY_MODE`,
`INTERIOR_DIRECTORY_MODE`), and the owned ancestors of a path.

The refusal (`PathContainmentError`) and the normal-form check
(`_refuse_unnormalized`) stay on `project_provision`: the GitHub identity
module shares them, and callers catch the refusal from there.

Moved out of `scripts/ticket_board/project_provision.py` unchanged (SYRD-464).
`project_provision` imports this module and re-exports every name, so every
module and test that imports them from there, or patches them there, still
reaches the same objects. What they read of `project_provision` -- each other,
`shell_quote`, `_refuse_unnormalized` and `PathContainmentError` -- is read
through it when they run, so a patch there still reaches them. This module
imports `project_provision` only inside the functions that need it, when they
run, with the same fallback for direct script execution.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Sequence


def _is_within(root: str, candidate: str) -> bool:
    """Containment by path components, not by string prefix.

    `/home/foobar` starts with `/home/foo`, and a grant computed from that
    coincidence would be written against somebody else's home (SYRD-49).
    """
    try:
        return PurePosixPath(candidate).is_relative_to(PurePosixPath(root))
    except ValueError:
        return False


def _refuse_prefix_coincidence(root: str, candidate: str) -> None:
    """Refuse a path that only looks contained because of a shared prefix.

    A path outside the owner home is an ordinary configuration -- a project
    repository kept elsewhere needs no grant on that home. A path that shares
    the home's string prefix without sharing its components is not: it is
    `/home/foobar` read as if it were inside `/home/foo`, and a privileged
    grant computed from that coincidence would be written against somebody
    else's home. Refuse before emitting the command (SYRD-49).
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    provision._refuse_unnormalized(root, what="the owner home")
    provision._refuse_unnormalized(candidate, what="the granted path")
    if not root or not candidate or provision._is_within(root, candidate):
        return
    if str(candidate).startswith(str(root)):
        raise provision.PathContainmentError(
            f"{candidate} is not inside {root}; refusing to grant access to a "
            "path that only shares its prefix"
        )


def _interior_directories(root: str, leaf: str) -> list[str]:
    """Directories strictly between a root and a leaf inside it.

    A leaf that is not inside the root has no interior, and refuses outright
    when it is inside only by string prefix.
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    provision._refuse_prefix_coincidence(root, leaf)
    if not provision._is_within(root, leaf):
        return []
    relative = PurePosixPath(leaf).relative_to(PurePosixPath(root))
    directories: list[str] = []
    current = PurePosixPath(root)
    for part in relative.parts[:-1]:
        current = current / part
        directories.append(str(current))
    return directories


def owner_home_traversal_commands(owner_home: str, principal: str) -> list[str]:
    """Let one principal walk THROUGH the owner's home without reading it.

    The owner home is 0710 and holds the owner's credentials, so it must stay
    unreadable; but a role that owns a worktree beneath it still cannot reach
    that worktree without traversal. `--x` grants exactly that and nothing else,
    which is the same grant the board service already holds (SYRD-49).
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    provision._refuse_unnormalized(owner_home, what="the owner home")
    return [f"sudo setfacl -m {principal}:--x {provision.shell_quote(owner_home)}"]


#: The tenant's own tree, closed to everything but the tenant. Not 0700: the
#: group is the tenant's own group, which is the tenant, so the extra bit grants
#: nobody anything today and keeps the directory conventional for an owner who
#: later adds a member deliberately.
TENANT_SOURCE_MODE = "0750"


#: What every directory created under a tenant's worktree base inherits. The
#: same access the base itself has -- the tenant reads and writes, the tenant's
#: own group may traverse, and nobody else exists -- installed as a DEFAULT so
#: the kernel applies it to trees nothing in this product creates (SYRD-181).
INHERITED_WORKTREE_CLOSURE = "d:u::rwx,d:g::r-x,d:o::---"


def tenant_source_confinement_commands(
    *, owner_user: str, owner_home: str, checkout: str
) -> list[str]:
    """Close the tenant's source tree to everything that may walk through the home.

    The owner home is 0710 with named `--x` entries, so a principal that must
    reach one thing beneath it can walk past everything else: the board service
    reaching its release, the control role reaching its configuration. Traversal
    was meant to be the entire grant. It was not, because the tree below was
    left world-readable -- `Projects` and the checkout inside it were created
    0755 -- so anything holding traversal could list and read the tenant's
    source. `sudo -u boardsvc test -r /home/<tenant>/Projects/<project>`
    succeeded for exactly that reason, and no ACL was involved: the named entry
    grants `--x` under an `--x` mask and cannot grant read. The mode bits did
    it (SYRD-156).

    `install -d` is why the parent was the worse half. Given a path it creates
    every missing component, but it applies `-m`, `-o` and `-g` only to the
    LAST one; intermediates get the caller's umask, and the caller is root.
    That is how a tenant ends up with `Projects` owned by root at 0755 above a
    checkout owned by the tenant. Every directory is named here rather than
    left to be created on the way past, which also makes this a repair: run
    against a tenant already in that shape it moves the parent back to the
    tenant and closes it.

    A checkout kept outside the owner home is not the tenant tree this is about
    and is not ours to re-mode, so it yields nothing. The argument is named
    `checkout` rather than `source_repo` because the packet passed the RELEASE
    it renders from -- which is outside every home -- and this answered
    truthfully that there was nothing to confine while confining nothing
    (SYRD-156 reopened).
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    provision._refuse_unnormalized(owner_home, what="the owner home")
    provision._refuse_unnormalized(checkout, what="the project checkout")
    provision._refuse_prefix_coincidence(owner_home, checkout)
    directories = provision.owned_ancestor_dirs(owner_home, checkout, include_target=True)
    quoted_owner = provision.shell_quote(owner_user)
    return [
        f"sudo install -d -m {provision.TENANT_SOURCE_MODE} -o {quoted_owner} -g {quoted_owner} "
        f"{provision.shell_quote(directory)}"
        for directory in directories
    ]


#: Mode for a repository copy under a tenant home. Same reasoning as
#: TENANT_SOURCE_MODE: the group is the tenant's own, and what reaches inside
#: reaches by a named entry that says so.
REPOSITORY_COPY_MODE = "0750"


def commit_store_read_commands(
    *, owner_home: str, service_user: str, commit_git_dir: str
) -> list[str]:
    """Let the board service resolve commits, and nothing more than that.

    The board validates a commit hash against a real repository, so the service
    account genuinely needs to READ one -- `TICKET_BOARD_COMMIT_GIT_DIR`. What
    it does not need is write, and what it must not have is every other
    repository the tenant keeps. So the grant is named, read-only, and points at
    the configured store alone: `rX` rather than `rwX`, with a default entry so
    the objects git writes later are readable too, and `--x` on the directories
    above it because a grant on a directory nobody can reach is not a grant.

    A store outside the owner home is not ours to re-mode or traverse-grant, and
    yields nothing here (SYRD-157).
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    provision._refuse_unnormalized(owner_home, what="the owner home")
    provision._refuse_unnormalized(commit_git_dir, what="the commit store")
    provision._refuse_prefix_coincidence(owner_home, commit_git_dir)
    if not provision._is_within(owner_home, commit_git_dir) or commit_git_dir.rstrip("/") == owner_home.rstrip("/"):
        return []
    principal = f"u:{service_user}"
    commands = provision.owner_home_traversal_commands(owner_home, principal)
    for directory in provision._interior_directories(owner_home, commit_git_dir):
        command = f"sudo setfacl -m {principal}:--x {provision.shell_quote(directory)}"
        if command not in commands:
            commands.append(command)
    quoted = provision.shell_quote(commit_git_dir)
    commands.append(f"sudo setfacl -R -m {principal}:rX {quoted}")
    commands.append(f"sudo setfacl -R -m d:{principal}:rX {quoted}")
    return commands


#: Directories between the home and a repository. Not tightened here: they hold
#: things other than repositories, several accounts already reach through them
#: by name, and the repository below is what this is about. They are named only
#: so they exist and belong to the tenant rather than to root.
INTERIOR_DIRECTORY_MODE = "0755"


def repository_copy_confinement_commands(
    *, owner_user: str, owner_home: str, repositories: Sequence[str], mode: str = ""
) -> list[str]:
    """Close a tenant's repository copies to everything but the tenant.

    World-readable is how the board service reached repositories nobody granted
    it: a checkout at 0755 under a home it may traverse is readable by anything
    that may traverse the home. Closing them is what makes the named grant above
    the ONLY way in, which is what lets the same evidence answer both halves --
    the selected store is readable by the service, and the copies beside it are
    not (SYRD-157).

    Directories only, and only inside the owner home; a repository kept
    elsewhere is not this tenant's tree to re-mode.
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    provision._refuse_unnormalized(owner_home, what="the owner home")
    quoted_owner = provision.shell_quote(owner_user)
    resolved_mode = mode or provision.REPOSITORY_COPY_MODE
    commands: list[str] = []

    def add(directory: str, directory_mode: str) -> None:
        command = (
            f"sudo install -d -m {directory_mode} -o {quoted_owner} -g {quoted_owner} "
            f"{provision.shell_quote(directory)}"
        )
        if command not in commands:
            commands.append(command)

    for repository in repositories:
        if not repository:
            continue
        provision._refuse_unnormalized(repository, what="a repository copy")
        provision._refuse_prefix_coincidence(owner_home, repository)
        if not provision._is_within(owner_home, repository):
            continue
        if repository.rstrip("/") == owner_home.rstrip("/"):
            continue
        # The directories above it, at the mode they already have on a
        # provisioned host, but named so they EXIST and belong to the tenant.
        # A grant is made on each of them next, and setfacl on a path that is
        # not there yet fails -- which on a fresh tenant is the whole packet
        # dying three lines before it would have created the repository.
        for interior in provision._interior_directories(owner_home, repository):
            add(interior, provision.INTERIOR_DIRECTORY_MODE)
        add(repository, resolved_mode)
    return commands


def tenant_worktree_confinement_commands(
    *,
    owner_user: str,
    owner_home: str,
    worktree_base: str,
    worktrees: Sequence[str] = (),
) -> list[str]:
    """Close a tenant's worktree base, and every tree inside it, to the world.

    `Projects` was closed by SYRD-156 and `syrd-worktrees` beside it was not,
    which left 163 directories at 0755 holding the same source across every
    role and every historical ticket. Anything with traversal on the base could
    read all of it -- and traversal was exactly what the socket group's named
    entry conveyed.

    The base and each directory between it and the home are named, at 0750
    owned by the tenant, the same way the checkout is. The trees already inside
    are closed by a sweep rather than by name: the plan knows the roles it
    declares, and a base accumulates a worktree per ticket that no plan
    mentions. Closing the base alone would be enough to make them unreachable,
    and closing them too is what makes that true of a base somebody later
    reopens (SYRD-171).
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    provision._refuse_unnormalized(owner_home, what="the owner home")
    provision._refuse_unnormalized(worktree_base, what="the worktree base")
    provision._refuse_prefix_coincidence(owner_home, worktree_base)
    directories = provision.owned_ancestor_dirs(owner_home, worktree_base, include_target=True)
    if not directories:
        return []
    quoted_owner = provision.shell_quote(owner_user)
    commands = [
        f"sudo install -d -m {provision.TENANT_SOURCE_MODE} -o {quoted_owner} -g {quoted_owner} "
        f"{provision.shell_quote(directory)}"
        for directory in directories
    ]
    for worktree in worktrees:
        if not worktree:
            continue
        provision._refuse_unnormalized(worktree, what="a role worktree")
        if not provision._is_within(worktree_base, worktree):
            continue
        command = (
            f"sudo install -d -m {provision.TENANT_SOURCE_MODE} -o {quoted_owner} -g {quoted_owner} "
            f"{provision.shell_quote(worktree)}"
        )
        if command not in commands:
            commands.append(command)
    # Everything already in the base, including the ticket worktrees no plan
    # names. `o-rwx` rather than a mode: this takes the world away and leaves
    # whatever else the tenant has arranged alone.
    # Guarded on the base existing: the line above creates it, so in a whole
    # packet run it always does, and a partial or interrupted run must not die
    # on a directory that has not been made yet.
    commands.append(f"if [ -d {provision.shell_quote(worktree_base)} ]; then")
    commands.append(
        f"    sudo find {provision.shell_quote(worktree_base)} -mindepth 1 -maxdepth 1 -type d "
        "-exec chmod o-rwx {} +"
    )
    # And what the base does to the NEXT tree, not only to the ones already in
    # it. The sweep closes what is there; the next `git worktree add` -- a role
    # pane being created, or an implementer starting a ticket -- makes a
    # directory with the ordinary umask and reopens the boundary. Live on syrd:
    # the base was repaired at journal 0090 and two later ticket worktrees
    # stood at 0755 by 0092.
    #
    # A default ACL is the only thing that holds without the creator
    # cooperating: the kernel intersects it with whatever mode the caller asks
    # for, so anything created here is closed to the world whoever creates it
    # and however. Inside the same guard as the sweep, for the same reason: a
    # partial run must not die on a base that has not been made yet (SYRD-181).
    commands.append(
        f"    sudo setfacl -m {provision.shell_quote(provision.INHERITED_WORKTREE_CLOSURE)} "
        f"{provision.shell_quote(worktree_base)}"
    )
    commands.append("fi")
    return commands


def socket_group_retirement_commands(
    *,
    owner_user: str,
    owner_home: str,
    socket_group: str,
    worktree_base: str,
    control_repository: str,
) -> list[str]:
    """Take the socket group off the surfaces it was never meant to reach.

    The socket group exists so role accounts can talk to the board, and the
    board service must be in it to hand the socket over. A grant to that group
    is therefore a grant to the board service -- which is why SYRD-157 moved
    repositories to a group of their own. What that change did not do is take
    the old grants away from a tenant that already had them: the only caller
    that retires anything is the add-role path, and a shared-account tenant
    never reaches it.

    So on syrd the socket group still held `--x` on the worktree base and
    `rwX` with a default entry on the control repository, verbatim the grant
    the repository group exists to avoid (SYRD-171).

    Removal is by entry -- `setfacl -x` -- so every other entry the tenant has
    is left exactly as it is, and an entry that is already gone removes
    successfully, which is what makes this safe to re-run. The group itself is
    not touched: `boardsvc` stays in it, because the socket is what it is for.
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    provision._refuse_unnormalized(owner_home, what="the owner home")
    group = (socket_group or "").strip()
    if not group or group == owner_user:
        # A plan that records the owner's own name here has no socket group to
        # retire, and removing the owner's group from the owner's tree is not a
        # boundary -- it is a mistake with the same shape.
        return []
    stale = f"g:{group}"
    removals: list[str] = []
    # The base carries the entry itself and its children carried none, so this
    # is not recursive there: a sweep of every tree under it would be a great
    # deal of work to remove entries that are not there. The control repository
    # IS recursive, because its grant came with a default entry and every object
    # git has written since inherited it.
    for surface, recursive in ((worktree_base, False), (control_repository, True)):
        if not surface:
            continue
        provision._refuse_unnormalized(surface, what="a repository surface")
        if not provision._is_within(owner_home, surface):
            continue
        scope = "-R " if recursive else ""
        removals.append(f"    sudo setfacl {scope}-x d:{stale} {provision.shell_quote(surface)}")
        removals.append(f"    sudo setfacl {scope}-x {stale} {provision.shell_quote(surface)}")
    if not removals:
        return []
    # Guarded on the group existing at all. Removing an entry that is already
    # gone succeeds; naming a group this host does not have is an `Invalid
    # argument` and would stop the packet on a tenant that never had one.
    return [
        f"if getent group {provision.shell_quote(group)} >/dev/null 2>&1; then",
        *removals,
        "fi",
    ]


def role_worktree_access_commands(
    *,
    owner_home: str,
    repository_group: str,
    worktree_base: str,
    control_repository: str,
    retired_groups: Sequence[str] = (),
) -> list[str]:
    """Make each role's own worktree and its git metadata reachable.

    Chowning a worktree leaf does not make it reachable: every directory above
    it has to be traversable, and a linked worktree's gitdir lives inside the
    owner's control repository, which the role must also read and write to
    record a commit. Neither grant exposes the owner's credentials, which stay
    0700 and are not named here (SYRD-49).

    The grantee is the REPOSITORY group, which exists for nothing else. It used
    to be the socket group, and that was the defect SYRD-157 exists to fix: the
    board service is a member of the socket group -- it has to be, to hand the
    socket to the roles -- so granting repositories to that group handed the
    service read and write over the tenant's git repository, with a default
    entry so every object created later inherited it too. Two authorities, one
    group, and membership needed for the first silently conferred the second.

    `retired_groups` are grants this function used to make and must now take
    away. Removal is by entry, not by rewriting the ACL, so anything else the
    tenant has is left alone; `setfacl -x` on an entry that is already gone
    succeeds, which is what makes a repair safe to re-run.
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    provision._refuse_unnormalized(owner_home, what="the owner home")
    provision._refuse_unnormalized(worktree_base, what="the worktree base")
    provision._refuse_unnormalized(control_repository, what="the control repository")
    group = f"g:{repository_group}"
    interior = provision._interior_directories(owner_home, worktree_base)
    # Every component between the home and the control repository, so the
    # gitdir a linked worktree points at can be opened at all.
    interior += provision._interior_directories(owner_home, control_repository)
    commands: list[str] = []
    # The home is granted only when something a role must reach is actually
    # beneath it; a worktree base or control repository kept elsewhere needs no
    # access to the owner's home at all.
    if provision._is_within(owner_home, worktree_base) or provision._is_within(owner_home, control_repository):
        commands.extend(provision.owner_home_traversal_commands(owner_home, group))
    for directory in interior + [worktree_base]:
        command = f"sudo setfacl -m {group}:--x {provision.shell_quote(directory)}"
        if command not in commands:
            commands.append(command)
    # The repository itself is shared: a commit made in any role's worktree
    # writes objects, refs and logs here. Default entries so the objects git
    # creates later inherit the same access.
    commands.append(f"sudo setfacl -R -m {group}:rwX {provision.shell_quote(control_repository)}")
    commands.append(f"sudo setfacl -R -m d:{group}:rwX {provision.shell_quote(control_repository)}")
    for retired in retired_groups:
        if not retired or retired == repository_group:
            continue
        stale = f"g:{retired}"
        # Only on the repository, and deliberately not on the traversal entries
        # above it. Traversal conveys no repository authority once the tree
        # itself grants that group nothing and is not world-readable, and a role
        # pane that is RUNNING keeps the supplementary groups it started with --
        # taking traversal away underneath it would break live work to tidy up
        # an entry that is not the one doing harm.
        commands.append(f"sudo setfacl -R -x d:{stale} {provision.shell_quote(control_repository)}")
        commands.append(f"sudo setfacl -R -x {stale} {provision.shell_quote(control_repository)}")
    return commands


def director_control_access_commands(
    *,
    owner_home: str,
    director_account: str,
    provision_dir: str,
    project_dir: str,
) -> list[str]:
    """Let the configured director account use its own unprivileged commands.

    The director's board write is authorized by the board from its peer uid, and
    the command that makes it has to read and update the durable control
    artifacts -- the launcher config, the workflow projection, the plan and the
    upgrade record. Those live in a 0600 file under the 0710 owner home, so the
    director could not read them and the entrypoint escalated to root, which
    that command then correctly refuses. The grant is bound to the configured
    account and reaches only the provisioning directory (SYRD-49).
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    provision._refuse_unnormalized(owner_home, what="the owner home")
    provision._refuse_unnormalized(provision_dir, what="the provisioning directory")
    provision._refuse_unnormalized(project_dir, what="the project directory")
    principal = f"u:{director_account}"
    interior = provision._interior_directories(owner_home, provision_dir)
    commands: list[str] = []
    if provision._is_within(owner_home, provision_dir):
        commands.extend(provision.owner_home_traversal_commands(owner_home, principal))
    for directory in interior:
        command = f"sudo setfacl -m {principal}:--x {provision.shell_quote(directory)}"
        if command not in commands:
            commands.append(command)
    provision._refuse_prefix_coincidence(owner_home, project_dir)
    project_grant = f"sudo setfacl -m {principal}:--x {provision.shell_quote(project_dir)}"
    if project_dir and provision._is_within(owner_home, project_dir) and project_grant not in commands:
        commands.append(project_grant)
    commands.append(f"sudo setfacl -R -m {principal}:rwX {provision.shell_quote(provision_dir)}")
    # Default entries so an atomically written replacement keeps the contract:
    # every projection writes a new file and renames it into place.
    commands.append(f"sudo setfacl -R -m d:{principal}:rwX {provision.shell_quote(provision_dir)}")
    return commands


def owned_ancestor_dirs(owner_home: str, target: str, *, include_target: bool) -> tuple[str, ...]:
    home = PurePosixPath(owner_home)
    path = PurePosixPath(target)
    try:
        path.relative_to(home)
    except ValueError:
        return ()
    end = path if include_target else path.parent
    if end == home:
        return ()
    relative_parts = end.relative_to(home).parts
    return tuple(str(home.joinpath(*relative_parts[:index])) for index in range(1, len(relative_parts) + 1))
