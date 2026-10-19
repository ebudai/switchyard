"""A project's worktrees and the control repository they are cut from.

A tenant's roles work in git worktrees of one control repository, owned by the
project account. This module covers:
- **The control repository.** Its state (missing, empty, occupied, unreadable,
  ready), creating or updating it, and repairing its ownership when a path in
  it is not the owner's.
- **Role worktrees and the shared checkout.** Creating, checking and resetting
  them, and cleaning them.
- **Refresh warnings.** Before the shared checkout is refreshed, the tracked
  edits and untracked files it would lose are named.
- **Refusing a role worktree's refresh** (SYRD-555). A stopped role's worktree
  is refreshed only when that loses nothing: no commit outside the ref, no
  tracked change, no untracked file. Otherwise it is kept exactly as it is,
  said so, and its role starts on it. A refresh moves HEAD, never a branch,
  and never deletes ignored files.
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

import os
import pwd
import shlex
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import GitOwnerRule, ProjectConfig, RoleConfig


@dataclass(frozen=True)
class WorktreeProvisionResult:
    failed_roles: dict[str, str]
    #: Role worktrees whose refresh was refused because it would have lost
    #: work, each with why and the remedy (SYRD-555). The tree is untouched
    #: and its role still starts on it, so these are not failures.
    kept_roles: dict[str, str] = field(default_factory=dict)

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


def git_role_worktree_reset_args(config: ProjectConfig, role: RoleConfig, *, discard: bool = False) -> list[str]:
    """The refresh: HEAD detached at the ref. Never `reset --hard`, which moves the
    checked-out branch and takes its commits with it (otto, 2026-10-04). Only an
    explicit discard forces past tracked changes (SYRD-555)."""
    from scripts import team_launcher as launcher

    return ["git", "-C", role.workdir, "checkout", "--detach", *(["--force"] if discard else []),
            launcher.worktree_ref(config)]


def git_clean_role_worktree_args(role: RoleConfig) -> list[str]:
    """Untracked files only, and only for an explicit discard: never `-x`, so ignored
    local configuration (`local.properties`, `.env`) and build caches stay (SYRD-555)."""
    return ["git", "-C", role.workdir, "clean", "-fd"]


def git_role_worktree_branch_args(role: RoleConfig) -> list[str]:
    return ["git", "-C", role.workdir, "symbolic-ref", "--quiet", "--short", "HEAD"]


def git_role_worktree_unmerged_count_args(config: ProjectConfig, role: RoleConfig, *, detached: bool) -> list[str]:
    """Commits a refresh would take out of reach: a branch's commits not in the ref, or
    a detached HEAD's commits on no branch, local or remote-tracking, and not in the ref."""
    from scripts import team_launcher as launcher

    ref = launcher.worktree_ref(config)
    if detached:
        return ["git", "-C", role.workdir, "rev-list", "--count", "HEAD", "--not", ref, "--branches", "--remotes"]
    return ["git", "-C", role.workdir, "rev-list", "--count", f"{ref}..HEAD"]


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
    as_root = os.geteuid() == 0
    # Specific role roots precede the broad worktree base.  A migrated config
    # can therefore validate each worktree through its declared role account,
    # while missing paths still inherit the project owner rule used during
    # ordinary shared-account provisioning (SYRD-66).
    rules = [
        launcher.GitOwnerRule(Path(role.workdir), launcher.role_run_as_user(config, role))
        for role in config.roles
        if launcher.role_run_as_user(config, role)
        and config.repository is not None
        # As root, the paths as written: `resolve()` would compare where links lead (SYRD-630).
        and (str(Path(role.workdir)) != str(config.repository) if as_root
             else launcher._normalized_path(Path(role.workdir)) != launcher._normalized_path(config.repository))
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


#: The launch flag that discards a named role's uncommitted work, printed in every
#: refusal it would lift (SYRD-555).
DISCARD_WORKTREE_CHANGES_FLAG = "--discard-worktree-changes"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _listed(paths: list[str], limit: int = 5) -> str:
    shown = ", ".join(paths[:limit])
    return shown + (f" and {len(paths) - limit} more" if len(paths) > limit else "")


def role_worktree_refresh_refusal(
    config: ProjectConfig,
    role: RoleConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    discard: bool = False,
) -> tuple[str, str]:
    """Whether refreshing a stopped role's worktree would lose work (SYRD-555).

    ("", "") to refresh; ("kept", why) to leave the worktree exactly as it is
    and start the role on it; ("failed", why) when the worktree could not be
    read, which refuses too. `discard` is the operator's explicit
    `--discard-worktree-changes` for this role: it lifts the refusal for
    tracked changes, untracked files and a branch ahead of the ref (the branch
    keeps its commits, since the refresh only moves HEAD), never for commits a
    detached HEAD holds alone.
    """
    from scripts import team_launcher as launcher

    read = {"runner": runner, "owner_rules": _config_git_owner_rules(config),
            "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True}
    ref = launcher.worktree_ref(config)
    status_proc = launcher.run_owner_correct_git(git_role_worktree_status_porcelain_args(role), **read)
    if status_proc.returncode != 0:
        return "failed", launcher._proc_failure_reason(status_proc, f"status failed with exit {status_proc.returncode}")
    branch_proc = launcher.run_owner_correct_git(git_role_worktree_branch_args(role), **read)
    branch = str(branch_proc.stdout or "").strip()
    if branch_proc.returncode not in (0, 1) or (branch_proc.returncode == 0 and not branch):
        return "failed", launcher._proc_failure_reason(branch_proc, "could not read which branch HEAD is on")
    detached = branch_proc.returncode == 1
    count_proc = launcher.run_owner_correct_git(git_role_worktree_unmerged_count_args(config, role, detached=detached), **read)
    count_text = str(count_proc.stdout or "").strip()
    if count_proc.returncode != 0 or not count_text.isdigit():
        return "failed", launcher._proc_failure_reason(count_proc, f"could not count commits not in {ref}")
    unmerged = int(count_text)
    output = str(status_proc.stdout or "")
    tracked = _tracked_dirty_paths_from_status(output)
    untracked = [line[3:].strip() for line in output.splitlines() if line.startswith("?? ")]

    held: list[str] = []
    if unmerged and detached:
        return "kept", (
            f"detached HEAD has {_plural(unmerged, 'commit')} on no branch and not in {ref}; "
            f"name it first (`git -C {role.workdir} branch <name>`), since no flag discards commits"
        )
    if unmerged:
        held.append(f"branch {branch} has {_plural(unmerged, 'commit')} not in {ref}")
    if tracked:
        held.append(f"{_plural(len(tracked), 'tracked change')}: {_listed(tracked)}")
    if untracked:
        held.append(f"{_plural(len(untracked), 'untracked path')}: {_listed(untracked)}")
    if not held or discard:
        return "", ""
    return "kept", (
        "; ".join(held) + f". To refresh it to {ref}, merge or move that work, or run "
        f"`switchyard start {config.project} {DISCARD_WORKTREE_CHANGES_FLAG} {role.role}` to discard its "
        "tracked changes and untracked files (ignored files stay, and its branch keeps its commits)"
    )


def report_kept_worktrees(
    config: ProjectConfig,
    kept_roles: dict[str, str],
    *,
    print_func: Callable[[str], None] = print,
) -> None:
    """The launch's last word on worktrees it would not refresh, so it is not lost in the scroll."""
    if not kept_roles:
        return
    print_func(f"team-launcher: {_plural(len(kept_roles), 'role worktree')} of {config.project} "
               "kept as they were, not refreshed, so no work was lost:")
    for role, why in sorted(kept_roles.items()):
        print_func(f"team-launcher:   {role}: {why}")


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


def control_repository_state(path: Path, *, owner_user: str = "") -> str:
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

    As root for `owner_user` it looks by descriptor from the owner's home, never
    by path, and a link above the store is refused (SYRD-613).
    """
    from scripts import team_launcher as launcher

    if owner_user and launcher.root_writes_for(owner_user):
        return _control_repository_state_as_root(path, owner_user)
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


def _control_repository_state_as_root(path: Path, owner_user: str) -> str:
    """`control_repository_state` for root: each question asked of the owner's tree by descriptor (SYRD-613)."""
    from scripts import owner_writes

    info = owner_writes.owner_entry_status(owner_user, path)
    if info is None:
        return CONTROL_REPOSITORY_MISSING
    if not stat.S_ISDIR(info.st_mode):
        return CONTROL_REPOSITORY_OCCUPIED
    kinds = [owner_writes.owner_entry_status(owner_user, path / name) for name in ("HEAD", "objects", "refs")]
    if all(kind is not None for kind in kinds) and stat.S_ISREG(kinds[0].st_mode) and all(
            stat.S_ISDIR(kind.st_mode) for kind in kinds[1:]):
        return CONTROL_REPOSITORY_READY
    names = owner_writes.owner_directory_names(owner_user, path)
    return CONTROL_REPOSITORY_OCCUPIED if names else CONTROL_REPOSITORY_EMPTY


def _git_config_value(raw: str) -> str:
    """One value as git writes it in a config file: quotes, escapes, a trailing comment."""
    out: list[str] = []
    quoted = False
    index = 0
    while index < len(raw):
        char = raw[index]
        if char == "\\" and index + 1 < len(raw):
            out.append({"n": "\n", "t": "\t", "b": "\b"}.get(raw[index + 1], raw[index + 1]))
            index += 2
            continue
        if char == '"':
            quoted = not quoted
        elif char in "#;" and not quoted:
            break
        else:
            out.append(char)
        index += 1
    return "".join(out).strip()


def _control_remote_url(control: Path, remote: str, *, owner_user: str = "") -> str:
    """The URL a bare repository records for `remote`, read from its config file, or "".

    Read from the file rather than by running git, as `control_repository_state`
    reads the store: nothing runs against a store that may be somebody else's,
    and a store this cannot read is no evidence either way. As root for
    `owner_user` it is read by descriptor from the owner's home: a link at the
    file or above it is refused, so nothing root alone can read is reported
    back as the store's URL (SYRD-613).
    """
    from scripts import owner_writes

    try:
        raw = (owner_writes.read_owner_bytes(owner_user, control / "config") if owner_user
               else (control / "config").read_bytes())
    except OSError:
        return ""
    lines = (raw or b"").decode("utf-8", errors="replace").splitlines()
    header = f'[remote "{remote}"]'
    inside = False
    for line in lines:
        text = line.strip()
        if text.startswith("["):
            inside = text.replace("\t", " ") == header
            continue
        if not inside or "=" not in text:
            continue
        key, _, value = text.partition("=")
        if key.strip().lower() == "url":
            return _git_config_value(value)
    return ""


def _same_local_repository(recorded: str, repository: Path, *, base: Path, by_name: bool = False) -> bool:
    """Whether a remote URL git recorded names this local repository; `by_name`, as written, looking nothing up."""
    url = recorded[len("file://"):] if recorded.startswith("file://") else recorded
    if "://" in url or (":" in url.split("/", 1)[0] and not url.startswith("/")):
        return False
    path = Path(url).expanduser()
    if not path.is_absolute():
        path = base / path
    if by_name:
        return os.path.normpath(path) == os.path.normpath(repository)
    return path.resolve(strict=False) == repository.expanduser().resolve(strict=False)


def control_repository_origin_mismatch(config: ProjectConfig) -> str:
    """Why an existing commit store belongs to another checkout, or "".

    `switchyard teardown` keeps the owner's home by default, and with it the
    store a previous project under this slug cloned from ITS checkout. A
    `switchyard new` for the slug names the same store path, saw a ready
    repository, skipped the clone and fetched from the old checkout: on otto
    that checkout had been moved, and the launch said only `fetch failed with
    exit 128` (SYRD-545). Where the old checkout still existed it would have
    handed every role the old project's history instead.

    Only a recorded URL naming another place is evidence. No URL, or a store
    this cannot read, is left to the fetch as before, which says what is wrong
    in git's words.
    """
    from scripts import team_launcher as launcher

    if config.control_repository is None or config.repository is None:
        return ""
    as_root = bool(config.run_as_user) and launcher.root_writes_for(config.run_as_user)
    owner = config.run_as_user if as_root else ""
    recorded = _control_remote_url(config.control_repository, config.worktree_remote, owner_user=owner)
    if not recorded or _same_local_repository(recorded, config.repository, base=config.control_repository,
                                              by_name=as_root):
        return ""
    control = config.control_repository
    worktrees = f" and the role worktrees under {config.worktree_base}" if config.worktree_base is not None else ""
    return (
        f"{control} is another checkout's commit store: its {config.worktree_remote} is {recorded}, "
        f"but this project's repository is {config.repository}. It is probably left from an earlier "
        "project under this slug (`switchyard teardown` keeps the owner's home), so nothing was "
        f"fetched into it. Move it aside{worktrees} -- for example "
        f"`mv {shlex.quote(str(control))} {shlex.quote(str(control) + '.legacy')}` -- and launch again; "
        "or, if it really should follow this repository, "
        f"`git -C {shlex.quote(str(control))} remote set-url {shlex.quote(config.worktree_remote)} "
        f"{shlex.quote(str(config.repository))}`."
    )


def role_worktree_foreign_store(config: ProjectConfig, role: RoleConfig) -> str:
    """Why an existing role worktree is not one of this commit store's, or "".

    A linked worktree's `.git` is a file naming its entry in the store it was
    added from, `<store>/worktrees/<name>`. One that names another store, or an
    entry this store no longer has -- the store was replaced, the worktree was
    left -- is not this project's to reset (SYRD-545). A `.git` that cannot be
    read is no evidence, and the ordinary check below still runs.
    """
    from scripts import team_launcher as launcher

    if config.control_repository is None:
        return ""
    if launcher.root_writes_for(config.run_as_user):
        return _role_worktree_foreign_store_as_root(config, role)
    dot_git = Path(role.workdir) / ".git"
    try:
        if dot_git.is_dir():
            return (
                f"{role.workdir} is a repository of its own, not a worktree of this project's commit store "
                f"{config.control_repository}; nothing here adopts or resets it. Move it aside and launch "
                "again, and the launch creates the worktree."
            )
        if not dot_git.is_file():
            return ""
        text = dot_git.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not text.startswith("gitdir:"):
        return ""
    gitdir = Path(text[len("gitdir:"):].strip()).expanduser()
    if not gitdir.is_absolute():
        gitdir = Path(role.workdir) / gitdir
    gitdir = gitdir.resolve(strict=False)
    store = config.control_repository.expanduser().resolve(strict=False)
    if gitdir.parent == store / "worktrees" and gitdir.is_dir():
        return ""
    owner = gitdir.parent.parent if gitdir.parent.name == "worktrees" else gitdir
    detail = (
        f"is a worktree of {owner}, not of this project's commit store {config.control_repository}"
        if owner != store
        else f"was added to a commit store at {store} that has since been replaced: it names {gitdir}, "
        "which this store does not have"
    )
    return (
        f"{role.workdir} {detail}; nothing here adopts or resets another store's worktree. Move it aside "
        "and launch again, and the launch creates it from this project's store."
    )


def _role_worktree_foreign_store_as_root(config: ProjectConfig, role: RoleConfig) -> str:
    """`role_worktree_foreign_store` as root for the owner (SYRD-630): `.git` looked at and read by descriptor from
    the owner's root -- a link there or above it refused, never followed -- and the `gitdir:` it names compared as
    written, never resolved. A refusal raises `SystemExit`."""
    from scripts import owner_writes
    from scripts.owner_git import _lexical

    owner = config.run_as_user
    dot_git = Path(role.workdir) / ".git"
    status = owner_writes.owner_entry_status(owner, dot_git)
    if status is None:
        return ""
    if stat.S_ISLNK(status.st_mode):
        return (
            f"{dot_git} is a link; nothing here follows, adopts or resets it. Move {role.workdir} aside and launch "
            "again, and the launch creates the worktree."
        )
    if stat.S_ISDIR(status.st_mode):
        return (
            f"{role.workdir} is a repository of its own, not a worktree of this project's commit store "
            f"{config.control_repository}; nothing here adopts or resets it. Move it aside and launch "
            "again, and the launch creates the worktree."
        )
    raw = owner_writes.read_owner_bytes(owner, dot_git) if stat.S_ISREG(status.st_mode) else None
    text = (raw or b"").decode("utf-8", "replace").strip()
    if not text.startswith("gitdir:"):
        return ""
    named = text[len("gitdir:"):].strip()
    gitdir = PurePosixPath(named if named.startswith("/") else f"{str(role.workdir).rstrip('/')}/{named}")
    store = PurePosixPath(str(config.control_repository))
    if _lexical(gitdir) and _lexical(store) and gitdir.parent == store / "worktrees":
        entry = owner_writes.owner_entry_status(owner, Path(str(gitdir)))
        if entry is not None and stat.S_ISDIR(entry.st_mode):
            return ""
    named_store = gitdir.parent.parent if gitdir.parent.name == "worktrees" else gitdir
    detail = (
        f"is a worktree of {named_store}, not of this project's commit store {config.control_repository}"
        if named_store != store
        else f"was added to a commit store at {store} that has since been replaced: it names {gitdir}, "
        "which this store does not have"
    )
    return (
        f"{role.workdir} {detail}; nothing here adopts or resets another store's worktree. Move it aside "
        "and launch again, and the launch creates it from this project's store."
    )


def _role_worktree_present_as_root(config: ProjectConfig, role_path: Path) -> bool:
    """Whether a role's worktree is there, by descriptor from the owner's root; a link there or above it refused."""
    from scripts import owner_writes

    status = owner_writes.owner_entry_status(config.run_as_user, role_path)
    if status is not None and stat.S_ISLNK(status.st_mode):
        raise SystemExit(f"switchyard: refusing {role_path} for {config.run_as_user}: it is a link, never followed")
    return status is not None


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
    try:
        state = control_repository_state(config.control_repository, owner_user=config.run_as_user)
        # Before anything writes to it or fetches into it (SYRD-545).
        foreign = control_repository_origin_mismatch(config) if state == CONTROL_REPOSITORY_READY else ""
    except SystemExit as refusal:  # as root, a link at or above the store: refused, never looked through
        return WorktreeProvisionResult({role.role: str(refusal) for role in config.roles})
    if state == CONTROL_REPOSITORY_OCCUPIED:
        reason = (
            f"{config.control_repository} exists, is not a Git repository, and is not empty. "
            "Nothing here will delete it or write over it: move it aside, or point this "
            "project's commit store at another path, and start again."
        )
        return WorktreeProvisionResult({role.role: reason for role in config.roles})
    if foreign:
        return WorktreeProvisionResult({role.role: foreign for role in config.roles})
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
    if launcher.root_writes_for(config.run_as_user):
        return _repair_control_repository_as_root(config)
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


def _repair_control_repository_as_root(config: ProjectConfig) -> WorktreeProvisionResult:
    """Root's repair for the owner, by descriptor from the owner's home (SYRD-613).

    The checks above look by path, and `chown`/`chown -R` by path followed a
    link swapped in at the store, or at a directory above it, after they
    looked. Here nothing is looked at by path: the store must name, as written,
    a directory under the owner's managed root, and `repair_owner_tree` opens
    everything from the home down, refuses a link or a mount, and changes
    owners on what it opened.
    """
    from scripts import owner_writes, tenant_paths

    control = config.control_repository

    def refused(why: object) -> WorktreeProvisionResult:
        message = f"refusing to repair control repository ownership for {control}: {why}"
        return WorktreeProvisionResult({role.role: message for role in config.roles})

    try:
        pwd.getpwnam(config.run_as_user)
        home = owner_writes.owner_home(config.run_as_user)
    except (KeyError, ValueError):
        return refused(f"target user {config.run_as_user!r} does not exist")
    managed = f"{home.rstrip('/')}/.local/state/switchyard/projects"
    text = str(control)
    parent = str(PurePosixPath(text).parent)
    if text != str(PurePosixPath(text)) or "/../" in f"{text}/" or not parent.startswith(f"{managed}/"):
        return refused(f"{parent} is not a switchyard-managed control directory under {managed}")
    try:
        owner_writes.repair_owner_tree(config.run_as_user, control)
    except (tenant_paths.Refused, OSError, SystemExit) as refusal:
        return refused(refusal)
    return WorktreeProvisionResult({})


def ensure_control_role_worktrees(
    config: ProjectConfig,
    *,
    refresh: bool,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    discard: frozenset[str] = frozenset(),
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
    kept: dict[str, str] = {}
    as_root = launcher.root_writes_for(config.run_as_user)
    for role in config.roles:
        role_path = Path(role.workdir)
        try:
            present = _role_worktree_present_as_root(config, role_path) if as_root else role_path.exists()
            foreign = role_worktree_foreign_store(config, role) if present else ""
        except SystemExit as refusal:
            failed[role.role] = str(refusal)
            continue
        if not present:
            add_proc = launcher.run_owner_correct_git(
                git_control_worktree_add_args(config, role),
                runner=runner,
                owner_rules=_config_git_owner_rules(config),
            )
            if add_proc.returncode != 0:
                failed[role.role] = launcher._proc_failure_reason(add_proc, f"worktree add failed with exit {add_proc.returncode}")
            continue
        if foreign:
            failed[role.role] = foreign
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
        outcome, why = role_worktree_refresh_refusal(config, role, runner=runner, discard=role.role in discard)
        if outcome == "failed":
            failed[role.role] = why
            continue
        if outcome == "kept":
            kept[role.role] = why
            print(f"team-launcher: not refreshing {role.role}'s worktree {role.workdir}: {why}", file=sys.stderr)
            continue
        reset_proc = launcher.run_owner_correct_git(
            git_role_worktree_reset_args(config, role, discard=role.role in discard),
            runner=runner,
            owner_rules=_config_git_owner_rules(config),
        )
        if reset_proc.returncode != 0:
            failed[role.role] = launcher._proc_failure_reason(reset_proc, f"refresh failed with exit {reset_proc.returncode}")
            continue
        if role.role not in discard:
            continue
        clean_proc = launcher.run_owner_correct_git(
            git_clean_role_worktree_args(role),
            runner=runner,
            owner_rules=_config_git_owner_rules(config),
        )
        if clean_proc.returncode != 0:
            failed[role.role] = launcher._proc_failure_reason(clean_proc, f"clean failed with exit {clean_proc.returncode}")
    return WorktreeProvisionResult(failed, kept)


def ensure_project_worktrees(
    config: ProjectConfig,
    *,
    refresh: bool,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    discard: frozenset[str] = frozenset(),
) -> WorktreeProvisionResult:
    from scripts import team_launcher as launcher

    if config.control_repository is not None:
        return ensure_control_role_worktrees(config, refresh=refresh, runner=runner, discard=discard)
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

def _prepare_project_worktrees_for_launch(
    config: ProjectConfig,
    *,
    running_roles: list[RoleConfig],
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    discard: frozenset[str] = frozenset(),
) -> WorktreeProvisionResult:
    from scripts import team_launcher as launcher

    if not running_roles:
        return launcher.ensure_project_worktrees(config, refresh=True, runner=runner, discard=discard)
    running_role_names = {role.role for role in running_roles}
    if config.control_repository is not None:
        stopped_roles = [role for role in config.roles if role.role not in running_role_names]
        if not stopped_roles:
            return launcher.WorktreeProvisionResult({})
        result = launcher.ensure_project_worktrees(
            replace(config, roles=stopped_roles), refresh=True, runner=runner, discard=discard
        )
    else:
        result = launcher.ensure_project_worktrees(config, refresh=False, runner=runner)
    return launcher.WorktreeProvisionResult(
        {role: reason for role, reason in result.failed_roles.items() if role not in running_role_names},
        result.kept_roles,
    )
