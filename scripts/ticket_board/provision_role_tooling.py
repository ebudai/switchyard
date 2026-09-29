"""How provisioning stages the tooling role accounts run, and proves the system units they read.

The shared staging directory under the tenant control root
(`tenant_control_root`, `role_tooling_staging_dir`); the executables staged
into it and retired from it (`ROLE_STAGED_EXECUTABLES`,
`RETIRED_STAGED_EXECUTABLES`), the modules they import
(`entry_point_module_dependencies`), the Git template roles clone with
(`GIT_TEMPLATE_DIR_NAME`, `git_template_files`), the operator commands that
stage all of it (`role_tooling_staging_commands`,
`git_template_staging_commands`) and the check of what was staged
(`staged_role_tooling_problems`); and the proofs that a system unit a role
reads is the one root installed (`readable_system_unit_path`,
`system_unit_proof_commands`, `system_unit_proof_chain`).

Moved out of `scripts/ticket_board/project_provision.py` unchanged (SYRD-467).
`project_provision` imports this module and re-exports every name, so every
module and test that imports them from there, or patches them there, still
reaches the same objects. What they read of `project_provision` -- each other,
`shell_quote`, `TENANT_CONTROL_ROOT`, the release marker and skills names,
`privileged_install` and its own `__file__` (the scripts beside it) -- is read
through it when they run, so a patch there still reaches them. This module
imports `project_provision` only inside the functions that need it, when they
run, with the same fallback for direct script execution.
"""

from __future__ import annotations

import ast
import json
import os
import stat
from pathlib import Path
from typing import Sequence


#: What the publication hop staged, removed by name on every upgrade. Keeping
#: the list is the only way an upgrade can take a program away: staging refreshes
#: what the release carries and cannot know about a name nobody mentions any
#: more (SYRD-123).
RETIRED_STAGED_EXECUTABLES: tuple[str, ...] = (
    "switchyard-publish-ref",
    "switchyard-publish",
    "switchyard-integrate-main",
    "switchyard-integrate",
    "switchyard_publication_authority.py",
)


#: The executables a role account reaches through the shared staging directory.
#: One list, so what is installed and what is scanned for dependencies cannot
#: drift apart.
ROLE_STAGED_EXECUTABLES: tuple[str, ...] = (
    "ticket-board-pane-idle-hook",
    "ticket-board-install-pane-hooks",
    # Answers Claude's own permission prompts for a pane already running in
    # bypass mode. Staged rather than copied into the role's home like the idle
    # hook: a role that could rewrite this could make it answer "allow" for a
    # session that is not in bypass at all (SYRD-234).
    "ticket-board-claude-permission-hook",
    "switchyard-board-skill",
    # SYRD-123: publishing a candidate is a push again. The account every role
    # runs as holds the project's GitHub credential, so this needs no grant, no
    # privileged helper and no Director -- it is a careful wrapper around one
    # `git push`. The old name is staged beside it because panes and habits
    # still reach for it, and says once what changed.
    "switchyard-publish-candidate",
    "switchyard-request-publication",
    "ticket-board-register-runtime",
    # Root-owned and reached only through this tenant's sudo grant.
    "switchyard-tenant-control",
    # The one-slot display bridge a presentation window's tabs run, so a
    # desktop terminal can show the owner's display sessions without six
    # password prompts and without a broad sudo grant (SYRD-65).
    "switchyard-display-attach",
    # The board clients themselves. A role that cannot run these has no
    # normal board access at all: they live under the owner's home, which
    # is 0710 and which no role account may traverse (SYRD-45).
    "ticket-board-write",
    "ticket-board-read",
    "directorctl",
)


def _script_sibling_modules(source_root: Path) -> dict[str, Path]:
    return {path.stem: path for path in source_root.glob("*.py")}


def _imported_names(path: Path) -> set[str]:
    """Top-level module names a Python source file imports, or nothing.

    A file that does not parse as Python -- `directorctl` is a shell script
    with Python embedded in it -- contributes no dependency here rather than
    stopping the render.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, ValueError):
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def entry_point_module_dependencies(
    entry_points: Sequence[str] = ROLE_STAGED_EXECUTABLES,
    *,
    source_root: Path | None = None,
) -> tuple[str, ...]:
    """Sibling modules the staged executables import, transitively.

    Each of these entry points puts its own directory on `sys.path` and imports
    from there, so a module it imports has to be staged beside it or the staged
    copy is not runnable at all. `switchyard-board-skill` imports
    `board_skill_cli`, which was never staged: the wrapper installed fine and
    then died with `ModuleNotFoundError` the first time a role account ran it
    (SYRD-60).

    Discovered rather than listed, so the next entry point that grows a sibling
    import is staged with it instead of failing the same way. The `ticket_board`
    package is staged separately as a whole tree.
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    root = source_root if source_root is not None else Path(provision.__file__).resolve().parents[1]
    modules = provision._script_sibling_modules(root)
    needed: set[str] = set()
    frontier = [root / name for name in entry_points]
    while frontier:
        path = frontier.pop()
        for imported in provision._imported_names(path):
            if imported in modules and imported not in needed:
                needed.add(imported)
                frontier.append(modules[imported])
    return tuple(sorted(needed))


def role_tooling_staging_dir(project: str, *, root: Path | str | None = None) -> str:
    """Where a role account reaches this tenant's root-owned tooling."""
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    return f"{root if root is not None else provision.tenant_control_root()}/{project}"


#: Overridable for the same reason the publish grant root and the sudoers root
#: are: a suite has to be able to exercise the real installation path without
#: writing into /usr/local on the host running it.
TENANT_CONTROL_ROOT_ENV = "SWITCHYARD_TENANT_CONTROL_ROOT"


def tenant_control_root() -> str:
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    import os as _os

    return _os.environ.get(provision.TENANT_CONTROL_ROOT_ENV, "").strip() or provision.TENANT_CONTROL_ROOT


def readable_system_unit_path(project: str, *, root: Path | str | None = None) -> str:
    """Where an unprivileged deployer can read this tenant's reviewed unit.

    The deployer compares the release's production unit against the one systemd
    has loaded, and refuses to continue when they differ, because daemon-reload
    is deliberately outside the board's polkit grant. It runs as the project
    account, and root's own copy of that unit lives in the privileged provision
    directory, which is root-only -- so on SYRD-126 the deployer read the
    candidate as ABSENT and refused a release whose unit was in fact installed,
    correct, and byte-identical.

    The fix is not to open that directory. Root publishes a copy here instead,
    beside the other root-owned bytes a role account is meant to read, and the
    handoff names the copy (SYRD-127). A copy can be compared; the installed
    unit cannot be compared with itself.
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    return f"{provision.role_tooling_staging_dir(project, root=root)}/{project}-ticket-board.service"


def system_unit_proof_commands(
    project: str, source_token: str, *, staging_root: Path | str | None = None
) -> list[str]:
    """Publish the readable copy, or take away a stale one.

    The `else` branch is the half that matters: a tenant with no reviewed unit
    must leave the deployer with no candidate, so it refuses rather than
    comparing the release against a copy of some earlier release's unit. Same
    shape as the role tooling staging for the same reason -- what is published
    is exactly what exists now.

    `source_token` is placed in the script as written, so a caller passes either
    a shell-quoted path or an expansion like `"$system_unit_candidate"` -- the
    operator packet knows the path only at run time.
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    staging = provision.role_tooling_staging_dir(project, root=staging_root)
    target = provision.readable_system_unit_path(project, root=staging_root)
    source = source_token
    return [
        f"sudo install -d -m 0755 -o root -g root {provision.shell_quote(staging)}",
        f"if [ -f {source} ]; then",
        f"    sudo install -m 0444 -o root -g root {source} {provision.shell_quote(target)}",
        "else",
        f"    sudo rm -f {provision.shell_quote(target)}",
        "fi",
    ]


def system_unit_proof_chain(
    project: str, source_token: str, *, staging_root: Path | str | None = None
) -> list[str]:
    """The same publication, as commands that can be `&&`-joined.

    Used where the copy follows an install of the very bytes it copies, so the
    source cannot be absent and the conditional above would only be noise -- and
    where the surrounding command is a single chain, which a multi-line `if`
    cannot be part of.
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    return [
        f"sudo install -d -m 0755 -o root -g root {provision.shell_quote(provision.role_tooling_staging_dir(project, root=staging_root))}",
        f"sudo install -m 0444 -o root -g root {source_token} "
        f"{provision.shell_quote(provision.readable_system_unit_path(project, root=staging_root))}",
    ]


def _release_marker_commit(root: Path) -> tuple[str, str]:
    """The commit a release names, and why it names none. One of the two is empty."""
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    marker = root / provision.RELEASE_MARKER_NAME
    if not marker.is_file():
        return "", f"{marker} does not exist"
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return "", f"{marker} could not be read: {exc}"
    commit = str(payload.get("commit") or "").strip()
    return commit, "" if commit else f"{marker} names no commit"


def staged_role_tooling_problems(
    project: str,
    release_root: str,
    *,
    staging_root: Path | None = None,
    expect_uid: int | None = None,
) -> list[str]:
    """Whether the staged bundle is the one this release would install.

    Staging happened only inside the account-creation script, so a tenant whose
    accounts already existed skipped it and kept the previous release's hooks
    and board clients while its board moved on. The roles then load code from
    one release and talk to a board from another, and the provenance marker
    names a release the staged files did not come from -- so the bundle is
    checked against the selected release before the identity transaction, not
    assumed from having run the script once (SYRD-62).
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    staging = Path(staging_root) if staging_root is not None else Path(provision.role_tooling_staging_dir(project))
    source = Path(release_root)
    # Whoever is entitled to stage this. On a host that is root, because this
    # runs from the privileged phases; naming the reader's own uid rather than a
    # literal 0 is what makes it checkable without a root-owned sandbox, and it
    # is the same identity either way (SYRD-62).
    owner_uid = os.getuid() if expect_uid is None else expect_uid
    problems: list[str] = []

    def _owned(path: Path, what: str, *, executable: bool) -> None:
        try:
            info = path.lstat()
        except OSError:
            problems.append(f"{what} is not staged at {path}")
            return
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            problems.append(f"{path} is not a regular file")
            return
        if info.st_uid != owner_uid:
            problems.append(f"{path} is owned by uid {info.st_uid} rather than by uid {owner_uid}")
        if info.st_mode & 0o022:
            problems.append(f"{path} is group- or world-writable")
        if executable and not info.st_mode & 0o111:
            problems.append(f"{path} is not executable")

    def _absent(path: Path, what: str) -> None:
        """Left behind by a newer release than the one selected."""
        if path.exists() or path.is_symlink():
            problems.append(
                f"{path} is staged but {what} is not in this release; the staged bundle is not "
                "the one this release would install"
            )

    for name in provision.ROLE_STAGED_EXECUTABLES:
        # What the SELECTED release carries, not what the running launcher
        # knows about. Deliberately selecting an older release is what a
        # rollback is, and it must not be blocked by tools that release never
        # had -- while a tool it does not have must not survive in the staged
        # bundle either (SYRD-93 live acceptance).
        if (source / "scripts" / name).is_file():
            _owned(staging / name, name, executable=True)
        else:
            _absent(staging / name, name)
    # Derived from the release being staged, not from whatever this process
    # happens to be running out of: a newer release may import a module the
    # running one does not (SYRD-62).
    for module in provision.entry_point_module_dependencies(source_root=source / "scripts"):
        if (source / "scripts" / f"{module}.py").is_file():
            _owned(staging / f"{module}.py", f"the companion module {module}", executable=False)
    template_files = provision.git_template_files(str(source))
    if Path(template_files[0][0]).is_file():
        for source_file, relative, _mode in template_files:
            _owned(staging / provision.GIT_TEMPLATE_DIR_NAME / relative, f"the Git template's {relative}",
                   executable=True)
    else:
        _absent(staging / provision.GIT_TEMPLATE_DIR_NAME, "the Git template")
    for tree, what in ((staging / "ticket_board", "the ticket_board package"),
                       (staging / provision.SKILLS_DIR_NAME, f"the canonical {provision.SKILLS_DIR_NAME} tree")):
        if not tree.is_dir():
            problems.append(f"{what} is not staged at {tree}")
        elif not any(tree.iterdir()):
            problems.append(f"{tree} is empty")
    source_commit, source_reason = provision._release_marker_commit(source)
    staged_commit, staged_reason = provision._release_marker_commit(staging)
    if source_commit and staged_commit != source_commit:
        problems.append(
            f"the staged bundle names release {staged_commit or staged_reason or 'nothing'} "
            f"and this one is {source_commit}"
        )
    elif not source_commit and staged_commit:
        problems.append(
            f"the staged bundle names release {staged_commit}, which this source does not "
            f"({source_reason})"
        )
    return problems


def role_tooling_staging_commands(
    project: str, release_root: str, *, staging_root: Path | str | None = None
) -> list[str]:
    """Copy the executables roles need out of the owner's home.

    Roles are not in the owner's group and the owner's home is not traversable
    by them, which is correct -- it holds the owner's credentials. These few
    scripts are not secret, so root stages them at a shared path that role
    accounts can execute without being given any access to the owner (SYRD-39).

    They come from the pinned shared release, not from the tenant's deployed
    board: the deployed one is the release being replaced, and staging its
    clients would give the roles the version that predates the repair
    (SYRD-45).
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    staging = provision.role_tooling_staging_dir(project, root=staging_root)
    commands = [f"sudo install -d -m 0755 -o root -g root {provision.shell_quote(staging)}"]
    # The bounded privileged action boundary, installed and repaired from the
    # same selected release as everything else here (SYRD-112). Host-wide
    # rather than per tenant, and re-runnable, so every tenant's staging pass
    # is also a repair of it: a helper whose mode drifted, a policy somebody
    # removed and a release that never installed one are the same fix. It is
    # here rather than only in the operator script because the operator script
    # runs once, at provisioning, and an existing tenant would otherwise keep
    # whatever its original provisioning happened to write -- the same way
    # pane hooks were stranded on SYRD-234.
    boundary_root, boundary_policy_dir = provision.privileged_install.roots_for(staging_root)
    commands.extend(
        provision.privileged_install.install_commands(
            release_root, root=boundary_root, policy_dir=boundary_policy_dir
        )
    )

    def stage(source: str, target: str, mode: str) -> list[str]:
        """Stage what this release has, and take away what it does not.

        The list of what roles need is the running launcher's, and the release
        being staged is whichever one was selected -- so the two disagree
        whenever an older release is deliberately selected, which is exactly
        what a rollback does. Failing the whole staging step then leaves the way
        back blocked at the moment it is needed. Instead each file is staged if
        the release carries it, and removed if it does not, so the staged set is
        always precisely what the selected release provides (SYRD-93 live
        acceptance).
        """
        return [
            f"if [ -f {provision.shell_quote(source)} ]; then",
            f"    sudo install -m {mode} -o root -g root {provision.shell_quote(source)} {provision.shell_quote(target)}",
            "else",
            f"    sudo rm -f {provision.shell_quote(target)}",
            "fi",
        ]

    for name in provision.ROLE_STAGED_EXECUTABLES:
        commands.extend(
            stage(f"{release_root}/scripts/{name}", f"{staging}/{name}", "0755")
        )
    # Taken away, not merely no longer listed. A name dropped from the list
    # above stops being refreshed and stays on disk forever, so the programs the
    # publication hop staged are removed by name: leaving a root-owned publisher
    # staged while saying there is no privileged gate would make the second
    # statement false on every tenant that ever had one (SYRD-123).
    for name in provision.RETIRED_STAGED_EXECUTABLES:
        commands.append(f"sudo rm -f {provision.shell_quote(f'{staging}/{name}')}")
    # The modules those executables import from their own directory. Not
    # executable, but every bit as required: without them the staged copy is a
    # wrapper around an import that fails (SYRD-60).
    for module in provision.entry_point_module_dependencies():
        commands.extend(
            stage(
                f"{release_root}/scripts/{module}.py",
                f"{staging}/{module}.py",
                "0644",
            )
        )
    # The clients import the ticket_board package from their own directory, so
    # the package is staged beside them. Public code, root-owned, world
    # readable: nothing here is the owner's (SYRD-45).
    commands.append(
        f"sudo rm -rf {provision.shell_quote(f'{staging}/ticket_board')}"
    )
    commands.append(
        f"sudo cp -a {provision.shell_quote(f'{release_root}/scripts/ticket_board')} "
        f"{provision.shell_quote(f'{staging}/ticket_board')}"
    )
    commands.append(
        f"sudo chown -R root:root {provision.shell_quote(f'{staging}/ticket_board')}"
    )
    commands.append(
        f"sudo chmod -R a+rX {provision.shell_quote(f'{staging}/ticket_board')}"
    )
    # The canonical skill bodies the installer projects. Without them the staged
    # wrapper resolves its default tree root to a directory that holds no
    # skills, so `install` has nothing to install: the bundle has to be
    # self-contained, not a pointer back at a checkout no role can reach
    # (SYRD-60).
    commands.append(f"sudo rm -rf {provision.shell_quote(f'{staging}/{provision.SKILLS_DIR_NAME}')}")
    commands.append(
        f"sudo cp -a {provision.shell_quote(f'{release_root}/{provision.SKILLS_DIR_NAME}')} "
        f"{provision.shell_quote(f'{staging}/{provision.SKILLS_DIR_NAME}')}"
    )
    commands.append(f"sudo chown -R root:root {provision.shell_quote(f'{staging}/{provision.SKILLS_DIR_NAME}')}")
    commands.append(f"sudo chmod -R a+rX {provision.shell_quote(f'{staging}/{provision.SKILLS_DIR_NAME}')}")
    # The release marker, so a skill installed from the staged tree can name the
    # commit it came from. Without it every projected copy is unprovenanced and
    # `verify` rejects it -- the bundle installs and then cannot be checked
    # (SYRD-60). Guarded: a deployment made from a plain checkout has no marker.
    marker_source = f"{release_root}/{provision.RELEASE_MARKER_NAME}"
    marker_staged = f"{staging}/{provision.RELEASE_MARKER_NAME}"
    commands.append(f"if [ -f {provision.shell_quote(marker_source)} ]; then")
    commands.append(
        f"    sudo install -m 0644 -o root -g root {provision.shell_quote(marker_source)} "
        f"{provision.shell_quote(marker_staged)}"
    )
    commands.append("else")
    # Staging a source that names no commit must not leave the last one's
    # marker behind: every skill installed afterwards would be stamped with a
    # release that does not contain it, which is precisely the false provenance
    # the marker exists to prevent (SYRD-60).
    commands.append(f"    sudo rm -f {provision.shell_quote(marker_staged)}")
    commands.append("fi")
    commands.extend(provision.git_template_staging_commands(release_root, staging))
    return commands


#: A Git template for the repositories roles create. Role panes name it in
#: GIT_TEMPLATE_DIR, so a source checkout a role clones -- and every worktree
#: linked to it -- has the warning-only size policy before its first commit,
#: with no global hooksPath and nothing done to repositories roles did not
#: make (SYRD-257).
GIT_TEMPLATE_DIR_NAME = "git-template"


def git_template_files(release_root: str) -> list[tuple[str, str, str]]:
    """(release source, path under the template, mode) for the staged template."""
    try:
        from scripts.repository_hooks import PRE_COMMIT_HELPERS, TEMPLATE_PRE_COMMIT
    except ImportError:  # pragma: no cover - direct execution beside the module
        from repository_hooks import PRE_COMMIT_HELPERS, TEMPLATE_PRE_COMMIT

    return [
        (f"{release_root}/scripts/{TEMPLATE_PRE_COMMIT}", "hooks/pre-commit", "0755"),
        *(
            (f"{release_root}/scripts/{source}", f"hooks/{name}", "0755")
            for name, source in PRE_COMMIT_HELPERS
        ),
    ]


def git_template_staging_commands(release_root: str, staging: str) -> list[str]:
    """Stage the template whole, or not at all.

    A release that predates it has no template hook; staging its helpers
    alone would give roles a template that copies scripts nothing runs, so the
    template is removed instead -- which is also what a rollback to such a
    release needs.
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    template = f"{staging}/{provision.GIT_TEMPLATE_DIR_NAME}"
    files = provision.git_template_files(release_root)
    commands = [f"sudo rm -rf {provision.shell_quote(template)}"]
    commands.append(f"if [ -f {provision.shell_quote(files[0][0])} ]; then")
    commands.append(
        f"    sudo install -d -m 0755 -o root -g root {provision.shell_quote(template)} "
        f"{provision.shell_quote(f'{template}/hooks')}"
    )
    for source, relative, mode in files:
        commands.append(
            f"    sudo install -m {mode} -o root -g root {provision.shell_quote(source)} "
            f"{provision.shell_quote(f'{template}/{relative}')}"
        )
    commands.append("fi")
    return commands
