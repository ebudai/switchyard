"""A project's worktrees and the control repository they are cut from.

A tenant's roles work in git worktrees of one control repository, owned by the
project account. This module covers:
- **The control repository.** Its state (missing, empty, occupied, unreadable,
  ready), creating or updating it, and repairing its ownership when a path in
  it is not the owner's.
- **Role worktrees and the shared checkout.** Creating, checking and resetting
  them, and cleaning them.
- **Refresh warnings.** Before either kind of tree is refreshed, the tracked
  edits and untracked files it would lose are named.
- **Fetching** a project's worktree ref.
- **The git argv builders** for all of the above.

Every builder here is executed only through the owner-correct chokepoint
`run_owner_correct_git`. `tests/team_launcher_git_ownership_lint_test.py` scans
this module as well as the launcher.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-291). `team_launcher`
imports this module at its top and still exports every name callers read there.
This module never imports `team_launcher` at its top. Launcher facilities
(`run_owner_correct_git`, `GitOwnerRule`, `worktree_ref`, `current_user_name`,
`role_run_as_user`, `_path_owner_label`, `_path_owner_ids`, ...) are read from
`scripts.team_launcher` when a function runs, so the suites' patches there
still reach them. The same goes for `_control_repository_owner_home`: it is
defined here, but the suites patch it on the launcher, so its callers here
reach it through the launcher.
"""

from __future__ import annotations

import pwd
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import GitOwnerRule, ProjectConfig, RoleConfig


@dataclass(frozen=True)
class WorktreeProvisionResult:
    failed_roles: dict[str, str]

    @property
    def ok(self) -> bool:
        return not self.failed_roles


def control_repository_refspec(config: ProjectConfig) -> str:
    return f"+refs/heads/*:refs/remotes/{config.worktree_remote}/*"


def mkdir_p_args(path: Path) -> list[str]:
    return ["mkdir", "-p", str(path)]


def git_fetch_worktree_ref_args(config: ProjectConfig) -> list[str]:
    if config.repository is None:
        raise SystemExit("project config does not define repository")
    return ["git", "-C", str(config.repository), "fetch", config.worktree_remote, config.worktree_branch]


def git_clone_control_repository_args(config: ProjectConfig) -> list[str]:
    if config.repository is None:
        raise SystemExit("project config does not define repository")
    if config.control_repository is None:
        raise SystemExit("project config does not define control_repository")
    return ["git", "clone", "--bare", str(config.repository), str(config.control_repository)]


def git_control_remote_rename_args(config: ProjectConfig) -> list[str]:
    if config.control_repository is None:
        raise SystemExit("project config does not define control_repository")
    return ["git", "-C", str(config.control_repository), "remote", "rename", "origin", config.worktree_remote]


def git_control_fetch_refspec_args(config: ProjectConfig) -> list[str]:
    if config.control_repository is None:
        raise SystemExit("project config does not define control_repository")
    return [
        "git",
        "-C",
        str(config.control_repository),
        "config",
        "--replace-all",
        f"remote.{config.worktree_remote}.fetch",
        control_repository_refspec(config),
    ]


def git_fetch_control_ref_args(config: ProjectConfig) -> list[str]:
    if config.control_repository is None:
        raise SystemExit("project config does not define control_repository")
    return ["git", "-C", str(config.control_repository), "fetch", config.worktree_remote, config.worktree_branch]


def git_control_worktree_add_args(config: ProjectConfig, role: RoleConfig) -> list[str]:
    from scripts import team_launcher as launcher

    if config.control_repository is None:
        raise SystemExit("project config does not define control_repository")
    return [
        "git",
        "--git-dir",
        str(config.control_repository),
        "worktree",
        "add",
        "--detach",
        role.workdir,
        launcher.worktree_ref(config),
    ]


def git_shared_checkout_check_args(config: ProjectConfig) -> list[str]:
    if config.repository is None:
        raise SystemExit("project config does not define repository")
    return ["git", "-C", str(config.repository), "rev-parse", "--is-inside-work-tree"]


def git_checkout_shared_ref_args(config: ProjectConfig) -> list[str]:
    from scripts import team_launcher as launcher

    if config.repository is None:
        raise SystemExit("project config does not define repository")
    return ["git", "-C", str(config.repository), "checkout", "--detach", "--force", launcher.worktree_ref(config)]


def git_shared_checkout_status_porcelain_args(config: ProjectConfig) -> list[str]:
    if config.repository is None:
        raise SystemExit("project config does not define repository")
    return ["git", "-C", str(config.repository), "status", "--porcelain"]


def git_role_worktree_check_args(role: RoleConfig) -> list[str]:
    return ["git", "-C", role.workdir, "rev-parse", "--is-inside-work-tree"]


def git_role_worktree_status_porcelain_args(role: RoleConfig) -> list[str]:
    return ["git", "-C", role.workdir, "status", "--porcelain"]


def git_role_worktree_reset_args(config: ProjectConfig, role: RoleConfig) -> list[str]:
    from scripts import team_launcher as launcher

    return ["git", "-C", role.workdir, "reset", "--hard", launcher.worktree_ref(config)]


def git_clean_role_worktree_args(role: RoleConfig) -> list[str]:
    return ["git", "-C", role.workdir, "clean", "-fdx"]


def git_clean_role_worktree_dry_run_args(role: RoleConfig) -> list[str]:
    return ["git", "-C", role.workdir, "clean", "-fd", "--dry-run"]


def git_clean_shared_checkout_args(config: ProjectConfig) -> list[str]:
    if config.repository is None:
        raise SystemExit("project config does not define repository")
    return ["git", "-C", str(config.repository), "clean", "-fdx"]


def git_clean_shared_checkout_dry_run_args(config: ProjectConfig) -> list[str]:
    if config.repository is None:
        raise SystemExit("project config does not define repository")
    return ["git", "-C", str(config.repository), "clean", "-fd", "--dry-run"]


def _config_git_owner_rules(config: ProjectConfig) -> list[GitOwnerRule]:
    from scripts import team_launcher as launcher

    owner_user = config.run_as_user or launcher.current_user_name()
    if not owner_user:
        return []
    # Specific role roots precede the broad worktree base.  A migrated config
    # can therefore validate each worktree through its declared role account,
    # while missing paths still inherit the project owner rule used during
    # ordinary shared-account provisioning (SYRD-66).
    rules = [
        launcher.GitOwnerRule(Path(role.workdir), launcher.role_run_as_user(config, role))
        for role in config.roles
        if launcher.role_run_as_user(config, role)
        and config.repository is not None
        and launcher._normalized_path(Path(role.workdir)) != launcher._normalized_path(config.repository)
    ]
    roots: list[Path] = []
    if config.repository is not None:
        roots.append(config.repository)
    roots.extend(launcher._control_repository_owned_roots(config))
    rules.extend(launcher.GitOwnerRule(root, owner_user) for root in roots)
    return rules


def _tracked_dirty_paths_from_status(output: str) -> list[str]:
    paths: list[str] = []
    for raw_line in output.splitlines():
        if not raw_line:
            continue
        code = raw_line[:2]
        if code in {"??", "!!"}:
            continue
        path = raw_line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path:
            paths.append(path)
    return paths


def _clean_paths_from_dry_run(output: str) -> list[str]:
    paths: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        prefix = "Would remove "
        if not line.startswith(prefix):
            continue
        path = line.removeprefix(prefix).strip()
        if path:
            paths.append(path)
    return paths


def warn_before_shared_checkout_refresh(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> WorktreeProvisionResult | None:
    from scripts import team_launcher as launcher

    status_proc = launcher.run_owner_correct_git(
        git_shared_checkout_status_porcelain_args(config),
        runner=runner,
        owner_rules=_config_git_owner_rules(config),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if status_proc.returncode != 0:
        reason = launcher._proc_failure_reason(status_proc, f"status failed with exit {status_proc.returncode}")
        return WorktreeProvisionResult({role.role: reason for role in config.roles})
    clean_proc = launcher.run_owner_correct_git(
        git_clean_shared_checkout_dry_run_args(config),
        runner=runner,
        owner_rules=_config_git_owner_rules(config),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if clean_proc.returncode != 0:
        reason = launcher._proc_failure_reason(clean_proc, f"clean dry-run failed with exit {clean_proc.returncode}")
        return WorktreeProvisionResult({role.role: reason for role in config.roles})

    tracked_paths = _tracked_dirty_paths_from_status(str(status_proc.stdout or ""))
    clean_paths = _clean_paths_from_dry_run(str(clean_proc.stdout or ""))
    if not tracked_paths and not clean_paths:
        return None

    print(
        f"warning: team-launcher will reset managed project checkout {config.repository} to {launcher.worktree_ref(config)}",
        file=sys.stderr,
    )
    if tracked_paths:
        print("warning: tracked changes will be discarded:", file=sys.stderr)
        for path in tracked_paths:
            print(f"warning:   {path}", file=sys.stderr)
    if clean_paths:
        print("warning: untracked files will be removed:", file=sys.stderr)
        for path in clean_paths:
            print(f"warning:   {path}", file=sys.stderr)
    return None


def warn_before_role_worktree_refresh(
    config: ProjectConfig,
    role: RoleConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> WorktreeProvisionResult | None:
    from scripts import team_launcher as launcher

    status_proc = launcher.run_owner_correct_git(
        git_role_worktree_status_porcelain_args(role),
        runner=runner,
        owner_rules=_config_git_owner_rules(config),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if status_proc.returncode != 0:
        reason = launcher._proc_failure_reason(status_proc, f"status failed with exit {status_proc.returncode}")
        return WorktreeProvisionResult({role.role: reason})
    clean_proc = launcher.run_owner_correct_git(
        git_clean_role_worktree_dry_run_args(role),
        runner=runner,
        owner_rules=_config_git_owner_rules(config),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if clean_proc.returncode != 0:
        reason = launcher._proc_failure_reason(clean_proc, f"clean dry-run failed with exit {clean_proc.returncode}")
        return WorktreeProvisionResult({role.role: reason})

    tracked_paths = _tracked_dirty_paths_from_status(str(status_proc.stdout or ""))
    clean_paths = _clean_paths_from_dry_run(str(clean_proc.stdout or ""))
    if not tracked_paths and not clean_paths:
        return None

    print(
        f"warning: team-launcher will reset managed role worktree {role.workdir} "
        f"for {role.role} to {launcher.worktree_ref(config)}",
        file=sys.stderr,
    )
    if tracked_paths:
        print("warning: tracked changes will be discarded:", file=sys.stderr)
        for path in tracked_paths:
            print(f"warning:   {path}", file=sys.stderr)
    if clean_paths:
        print("warning: untracked files will be removed:", file=sys.stderr)
        for path in clean_paths:
            print(f"warning:   {path}", file=sys.stderr)
    return None


#: What is at the control repository path. Existence was taken for
#: initialisation, and that stopped being true the moment provisioning started
#: creating this directory ahead of time (SYRD-161).
CONTROL_REPOSITORY_MISSING = "missing"


CONTROL_REPOSITORY_READY = "repository"


CONTROL_REPOSITORY_EMPTY = "empty"


CONTROL_REPOSITORY_OCCUPIED = "occupied"


#: Not an answer, and deliberately not "occupied": a caller who cannot look
#: inside the path has no evidence about what is there, and refusing on that
#: would turn "I cannot see" into "somebody's data is here". It is handled like
#: an absent path, which is what the check that existed before this did with an
#: unreadable one, and git then says what is actually wrong.
CONTROL_REPOSITORY_UNREADABLE = "unreadable"


def control_repository_state(path: Path) -> str:
    """Tell a repository, a placeholder and somebody else's data apart.

    `switchyard new` and the operator packet now create the commit store as an
    owner-owned directory before anything clones into it, so that it exists to
    be confined and granted on. A check for mere existence then read that empty
    directory as an initialised bare repository, skipped the clone, and ran
    `git config` against it: `fatal: not in a git directory`, exit 128, and a
    resumed provision that could not start its roles.

    The three answers are different actions. A repository is used. An empty
    directory is the placeholder provisioning left and is cloned into. Anything
    else is somebody's data at a path this project was pointed at, and nothing
    here will delete or write over it.
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        return CONTROL_REPOSITORY_MISSING
    except OSError:
        return CONTROL_REPOSITORY_UNREADABLE
    if not stat.S_ISDIR(info.st_mode):
        return CONTROL_REPOSITORY_OCCUPIED
    # Git's own test for a repository directory, asked of the filesystem rather
    # than by running git: a bare repository has these three.
    if (path / "HEAD").is_file() and (path / "objects").is_dir() and (path / "refs").is_dir():
        return CONTROL_REPOSITORY_READY
    try:
        occupied = any(path.iterdir())
    except OSError:
        return CONTROL_REPOSITORY_UNREADABLE
    return CONTROL_REPOSITORY_OCCUPIED if occupied else CONTROL_REPOSITORY_EMPTY


def ensure_control_repository(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> WorktreeProvisionResult:
    from scripts import team_launcher as launcher

    if config.control_repository is None:
        return WorktreeProvisionResult({})
    repair_result = repair_control_repository_ownership(config, runner=runner)
    if not repair_result.ok:
        return repair_result
    mkdir_proc = runner(mkdir_p_args(config.control_repository.parent))
    if mkdir_proc.returncode != 0:
        reason = launcher._proc_failure_reason(mkdir_proc, f"mkdir failed with exit {mkdir_proc.returncode}")
        return WorktreeProvisionResult({role.role: reason for role in config.roles})
    state = control_repository_state(config.control_repository)
    if state == CONTROL_REPOSITORY_OCCUPIED:
        reason = (
            f"{config.control_repository} exists, is not a Git repository, and is not empty. "
            "Nothing here will delete it or write over it: move it aside, or point this "
            "project's commit store at another path, and start again."
        )
        return WorktreeProvisionResult({role.role: reason for role in config.roles})
    if state in (
        CONTROL_REPOSITORY_MISSING,
        CONTROL_REPOSITORY_EMPTY,
        CONTROL_REPOSITORY_UNREADABLE,
    ):
        clone_proc = launcher.run_owner_correct_git(
            git_clone_control_repository_args(config),
            runner=runner,
            owner_rules=_config_git_owner_rules(config),
        )
        if clone_proc.returncode != 0:
            reason = launcher._proc_failure_reason(clone_proc, f"clone failed with exit {clone_proc.returncode}")
            return WorktreeProvisionResult({role.role: reason for role in config.roles})
        if config.worktree_remote != "origin":
            rename_proc = launcher.run_owner_correct_git(
                git_control_remote_rename_args(config),
                runner=runner,
                owner_rules=_config_git_owner_rules(config),
            )
            if rename_proc.returncode != 0:
                reason = launcher._proc_failure_reason(rename_proc, f"remote rename failed with exit {rename_proc.returncode}")
                return WorktreeProvisionResult({role.role: reason for role in config.roles})
    refspec_proc = launcher.run_owner_correct_git(
        git_control_fetch_refspec_args(config),
        runner=runner,
        owner_rules=_config_git_owner_rules(config),
    )
    if refspec_proc.returncode != 0:
        reason = launcher._proc_failure_reason(refspec_proc, f"fetch refspec config failed with exit {refspec_proc.returncode}")
        return WorktreeProvisionResult({role.role: reason for role in config.roles})
    fetch_proc = launcher.run_owner_correct_git(
        git_fetch_control_ref_args(config),
        runner=runner,
        owner_rules=_config_git_owner_rules(config),
    )
    if fetch_proc.returncode != 0:
        reason = launcher._proc_failure_reason(fetch_proc, f"fetch failed with exit {fetch_proc.returncode}")
        return WorktreeProvisionResult({role.role: reason for role in config.roles})
    return WorktreeProvisionResult({})


def chown_control_repository_args(config: ProjectConfig, path: Path | None = None, *, recursive: bool = True) -> list[str]:
    if config.control_repository is None or not config.run_as_user:
        raise ValueError("control repository ownership repair requires control_repository and run_as_user")
    args = ["chown"]
    if recursive:
        args.append("-R")
    args.extend([f"{config.run_as_user}:{config.run_as_user}", str(path or config.control_repository)])
    return args


def _control_repository_managed_root(config: ProjectConfig) -> Path | None:
    from scripts import team_launcher as launcher

    owner_home = launcher._control_repository_owner_home(config)
    if owner_home is None:
        return None
    return (owner_home / ".local" / "state" / "switchyard" / "projects").resolve(strict=False)


def _control_repository_owner_home(config: ProjectConfig) -> Path | None:
    if config.control_repository is None or not config.run_as_user:
        return None
    try:
        expected = pwd.getpwnam(config.run_as_user)
    except KeyError:
        return None
    return Path(expected.pw_dir).resolve(strict=False)


def _control_repository_boundary_error(config: ProjectConfig, *, require_existing_user: bool) -> str | None:
    if config.control_repository is None:
        return None
    managed_root = _control_repository_managed_root(config)
    if managed_root is None:
        if not require_existing_user and config.run_as_user:
            # Load-time validation accepts pre-account-creation configs from `new`.
            # The same prefix check still bounds the eventual repair path.
            owner_home = (Path("/home") / config.run_as_user).resolve(strict=False)
            managed_root = (owner_home / ".local" / "state" / "switchyard" / "projects").resolve(strict=False)
        else:
            return f"target user {config.run_as_user!r} does not exist"
    parent = config.control_repository.expanduser().resolve(strict=False).parent
    if parent == managed_root or not parent.is_relative_to(managed_root):
        return f"{parent} is not a switchyard-managed control directory under {managed_root}"
    return None


def _control_repository_owner_mismatch_path(config: ProjectConfig) -> Path | None:
    from scripts import team_launcher as launcher

    if config.control_repository is None or not config.run_as_user:
        return None
    try:
        expected = pwd.getpwnam(config.run_as_user)
    except KeyError:
        return config.control_repository.parent if config.control_repository.parent.exists() else None
    control_path = config.control_repository
    if not control_path.exists():
        return None
    stack = [control_path]
    while stack:
        path = stack.pop()
        owner_ids = launcher._path_owner_ids(path)
        if owner_ids is None:
            return path
        if owner_ids != (expected.pw_uid, expected.pw_gid):
            return path
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            try:
                children = list(path.iterdir())
            except OSError:
                return path
            stack.extend(children)
    return None


def _control_repository_repair_paths(config: ProjectConfig) -> tuple[list[tuple[Path, bool]], Path | None]:
    from scripts import team_launcher as launcher

    if config.control_repository is None or not config.run_as_user:
        return [], None
    boundary_error = _control_repository_boundary_error(config, require_existing_user=True)
    if boundary_error is not None and not config.control_repository.exists() and not config.control_repository.parent.exists():
        return [], None
    try:
        expected = pwd.getpwnam(config.run_as_user)
    except KeyError:
        return [], config.control_repository.parent if config.control_repository.parent.exists() else None

    parent = config.control_repository.parent
    start = config.control_repository if config.control_repository.exists() else parent
    owner_home = launcher._control_repository_owner_home(config)
    if owner_home is None:
        return [], config.control_repository.parent if config.control_repository.parent.exists() else None
    repair_paths: list[tuple[Path, bool]] = []
    current = start
    while True:
        resolved = current.expanduser().resolve(strict=False)
        if not (resolved == owner_home or resolved.is_relative_to(owner_home)):
            return [], current
        owner_ids = launcher._path_owner_ids(current)
        if owner_ids is not None:
            if owner_ids == (expected.pw_uid, expected.pw_gid):
                break
            repair_paths.append((current, current == config.control_repository))
        if resolved == owner_home:
            break
        current = current.parent

    leaf_mismatch = _control_repository_owner_mismatch_path(config)
    if leaf_mismatch is not None and all(path != config.control_repository for path, _recursive in repair_paths):
        repair_paths.append((config.control_repository, True))
    repair_paths.reverse()
    return repair_paths, repair_paths[0][0] if repair_paths else leaf_mismatch


def repair_control_repository_ownership(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> WorktreeProvisionResult:
    from scripts import team_launcher as launcher

    if config.control_repository is None or not config.run_as_user:
        return WorktreeProvisionResult({})
    repair_paths, mismatch = _control_repository_repair_paths(config)
    if mismatch is None:
        return WorktreeProvisionResult({})
    boundary_error = _control_repository_boundary_error(config, require_existing_user=True)
    if boundary_error is not None:
        message = f"refusing to repair control repository ownership for {config.control_repository}: {boundary_error}"
        return WorktreeProvisionResult({role.role: message for role in config.roles})
    actual_owner = launcher._path_owner_label(mismatch)
    for path, recursive in repair_paths:
        chown_proc = runner(chown_control_repository_args(config, path, recursive=recursive))
        if chown_proc.returncode != 0:
            reason = launcher._proc_failure_reason(chown_proc, f"chown failed with exit {chown_proc.returncode}")
            message = (
                f"failed to repair control repository ownership for {config.control_repository}: {reason}; "
                f"{mismatch} is owned by {actual_owner}, expected {config.run_as_user}:{config.run_as_user}"
            )
            return WorktreeProvisionResult({role.role: message for role in config.roles})
    return WorktreeProvisionResult({})


def ensure_control_role_worktrees(
    config: ProjectConfig,
    *,
    refresh: bool,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> WorktreeProvisionResult:
    from scripts import team_launcher as launcher

    control_result = ensure_control_repository(config, runner=runner)
    if not control_result.ok or not refresh:
        return control_result
    if config.worktree_base is None:
        return WorktreeProvisionResult({role.role: "project config does not define worktree_base" for role in config.roles})
    mkdir_proc = runner(mkdir_p_args(config.worktree_base))
    if mkdir_proc.returncode != 0:
        reason = launcher._proc_failure_reason(mkdir_proc, f"mkdir failed with exit {mkdir_proc.returncode}")
        return WorktreeProvisionResult({role.role: reason for role in config.roles})

    failed: dict[str, str] = {}
    for role in config.roles:
        role_path = Path(role.workdir)
        if not role_path.exists():
            add_proc = launcher.run_owner_correct_git(
                git_control_worktree_add_args(config, role),
                runner=runner,
                owner_rules=_config_git_owner_rules(config),
            )
            if add_proc.returncode != 0:
                failed[role.role] = launcher._proc_failure_reason(add_proc, f"worktree add failed with exit {add_proc.returncode}")
            continue
        check_proc = launcher.run_owner_correct_git(
            git_role_worktree_check_args(role),
            runner=runner,
            owner_rules=_config_git_owner_rules(config),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if check_proc.returncode != 0:
            failed[role.role] = launcher._proc_failure_reason(check_proc, f"worktree check failed with exit {check_proc.returncode}")
            continue
        warning_result = warn_before_role_worktree_refresh(config, role, runner=runner)
        if warning_result is not None:
            failed.update(warning_result.failed_roles)
            continue
        reset_proc = launcher.run_owner_correct_git(
            git_role_worktree_reset_args(config, role),
            runner=runner,
            owner_rules=_config_git_owner_rules(config),
        )
        if reset_proc.returncode != 0:
            failed[role.role] = launcher._proc_failure_reason(reset_proc, f"reset failed with exit {reset_proc.returncode}")
            continue
        clean_proc = launcher.run_owner_correct_git(
            git_clean_role_worktree_args(role),
            runner=runner,
            owner_rules=_config_git_owner_rules(config),
        )
        if clean_proc.returncode != 0:
            failed[role.role] = launcher._proc_failure_reason(clean_proc, f"clean failed with exit {clean_proc.returncode}")
    return WorktreeProvisionResult(failed)


def ensure_project_worktrees(
    config: ProjectConfig,
    *,
    refresh: bool,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> WorktreeProvisionResult:
    from scripts import team_launcher as launcher

    if config.control_repository is not None:
        return ensure_control_role_worktrees(config, refresh=refresh, runner=runner)
    if config.repository is None:
        return WorktreeProvisionResult({})
    fetch_proc = launcher.run_owner_correct_git(
        git_fetch_worktree_ref_args(config),
        runner=runner,
        owner_rules=_config_git_owner_rules(config),
    )
    if fetch_proc.returncode != 0:
        reason = launcher._proc_failure_reason(fetch_proc, f"fetch failed with exit {fetch_proc.returncode}")
        return WorktreeProvisionResult({role.role: reason for role in config.roles})
    if not refresh:
        return WorktreeProvisionResult({})
    check_proc = launcher.run_owner_correct_git(
        git_shared_checkout_check_args(config),
        runner=runner,
        owner_rules=_config_git_owner_rules(config),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if check_proc.returncode != 0:
        reason = launcher._proc_failure_reason(check_proc, f"repository check failed with exit {check_proc.returncode}")
        return WorktreeProvisionResult({role.role: reason for role in config.roles})
    warning_result = warn_before_shared_checkout_refresh(config, runner=runner)
    if warning_result is not None:
        return warning_result
    checkout_proc = launcher.run_owner_correct_git(
        git_checkout_shared_ref_args(config),
        runner=runner,
        owner_rules=_config_git_owner_rules(config),
    )
    if checkout_proc.returncode != 0:
        reason = launcher._proc_failure_reason(checkout_proc, f"checkout failed with exit {checkout_proc.returncode}")
        return WorktreeProvisionResult({role.role: reason for role in config.roles})
    clean_proc = launcher.run_owner_correct_git(
        git_clean_shared_checkout_args(config),
        runner=runner,
        owner_rules=_config_git_owner_rules(config),
    )
    if clean_proc.returncode != 0:
        reason = launcher._proc_failure_reason(clean_proc, f"clean failed with exit {clean_proc.returncode}")
        return WorktreeProvisionResult({role.role: reason for role in config.roles})
    return WorktreeProvisionResult({})


def fetch_project_worktree_ref(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    from scripts import team_launcher as launcher

    if config.control_repository is not None:
        result = ensure_control_repository(config, runner=runner)
        if result.ok:
            return 0
        print(
            f"warning: failed to fetch {launcher.worktree_ref(config)} for reload: "
            f"{next(iter(result.failed_roles.values()), 'unknown error')}",
            file=sys.stderr,
        )
        return 1
    if config.repository is None:
        return 0
    fetch_proc = launcher.run_owner_correct_git(
        git_fetch_worktree_ref_args(config),
        runner=runner,
        owner_rules=_config_git_owner_rules(config),
    )
    if fetch_proc.returncode != 0:
        print(
            f"warning: failed to fetch {launcher.worktree_ref(config)} for reload: "
            f"{launcher._proc_failure_reason(fetch_proc, f'exit {fetch_proc.returncode}')}",
            file=sys.stderr,
        )
    return int(fetch_proc.returncode)
