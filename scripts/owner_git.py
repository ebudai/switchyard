"""Running git as the account that owns the repository, and a project's own git helpers.

Git refuses to operate in a repository owned by another account. Worse, run
as the wrong account, it leaves files that account then owns. So every git
command Switchyard runs goes through one chokepoint, `run_owner_correct_git`.
It works out which repository a command targets and who owns it (by the
configured `GitOwnerRule`s, else by the path's owner), and runs the command as
that account.

The project helpers build on that chokepoint:
- initialising or requiring a project's repository;
- committing the files `switchyard new` wrote;
- porcelain status;
- a runner bound to a project's owner.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-293). `team_launcher`
imports this module at its top and still exports every name callers read there.
The chokepoint's contract seam stays **on the launcher**. Suites patch
`team_launcher.run_owner_correct_git`, so every caller -- the other git modules
and this module's own helpers -- calls `launcher.run_owner_correct_git` when
it runs, never this module's function directly. The chokepoint's own launcher
dependencies (`current_user_name`, which decides the run-as owner, and
`_proc_failure_reason`) are read from `scripts.team_launcher` when it runs.
So is `_normalized_path`. The launcher defines that name twice, and the later
definition is the one every caller gets, so it stays there and is looked up
through the launcher, exactly as before. This module never imports
`team_launcher` at its top.
"""

from __future__ import annotations

import pwd
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence


def _path_owner_user(path: Path) -> tuple[str, str]:
    try:
        info = path.stat()
    except OSError as exc:
        return "", str(exc)
    try:
        return pwd.getpwuid(info.st_uid).pw_name, ""
    except KeyError:
        return "", f"uid {info.st_uid} has no passwd entry"


@dataclass(frozen=True)
class GitOwnerRule:
    root: Path
    owner_user: str


def _path_is_under(path: Path, root: Path) -> bool:
    from scripts import team_launcher as launcher

    normalized = launcher._normalized_path(path)
    normalized_root = launcher._normalized_path(root)
    return normalized == normalized_root or normalized.is_relative_to(normalized_root)


def _git_target_path_from_args(args: Sequence[str]) -> Path | None:
    if not args or args[0] != "git":
        return None
    for index, arg in enumerate(args):
        if arg == "-C" and index + 1 < len(args):
            return Path(args[index + 1])
        if arg == "--git-dir" and index + 1 < len(args):
            return Path(args[index + 1])
        if arg.startswith("--git-dir="):
            return Path(arg.split("=", 1)[1])
    if len(args) >= 5 and args[1:3] == ["clone", "--bare"]:
        return Path(args[4])
    return None


def _git_owner_for_target(target: Path, owner_rules: Sequence[GitOwnerRule]) -> tuple[str, str]:
    # A configured target must not be redirected after validation.  In
    # particular, do not follow a role-owned worktree symlink into another
    # tenant and then choose that destination's owner (SYRD-66).
    if target.is_symlink():
        return "", f"refusing symlink git target {target}"
    for rule in owner_rules:
        if rule.owner_user and _path_is_under(target, rule.root):
            return rule.owner_user, ""
    if target.exists():
        return _path_owner_user(target)
    return "", f"{target} does not exist and no owner rule matched"


def _git_owner_failure(args: Sequence[str], detail: str) -> subprocess.CompletedProcess[Any]:
    return subprocess.CompletedProcess(list(args), 125, stderr=f"owner-correct git skipped: {detail}")


def run_owner_correct_git(
    args: list[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    owner_rules: Sequence[GitOwnerRule] = (),
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    from scripts import team_launcher as launcher

    target = _git_target_path_from_args(args)
    if target is None:
        return _git_owner_failure(args, "git command does not declare a target path")
    owner_user, error = _git_owner_for_target(target, owner_rules)
    if not owner_user:
        return _git_owner_failure(args, error or f"cannot determine owner for {target}")
    if owner_user == launcher.current_user_name():
        return runner(args, **kwargs)
    return runner(["sudo", "-u", owner_user, *args], **kwargs)


def _git_status_porcelain(repo: Path, *, runner: Callable[..., subprocess.CompletedProcess[Any]]) -> str:
    from scripts import team_launcher as launcher

    try:
        result = launcher.run_owner_correct_git(
            ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"],
            runner=runner,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise SystemExit(f"team-launcher: cannot inspect deploy checkout {repo}: {exc}") from exc
    if result.returncode != 0:
        stderr = str(getattr(result, "stderr", "") or "").strip()
        detail = f": {stderr}" if stderr else ""
        raise SystemExit(f"team-launcher: cannot inspect deploy checkout {repo}{detail}")
    return str(getattr(result, "stdout", "") or "").rstrip("\n")


def _run_owner_git(
    owner_user: str,
    project_dir: Path,
    *git_args: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> subprocess.CompletedProcess[Any]:
    from scripts import team_launcher as launcher

    return launcher.run_owner_correct_git(
        ["git", "-C", str(project_dir), *git_args],
        runner=runner,
        owner_rules=[GitOwnerRule(project_dir, owner_user)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _ensure_project_git_repository(
    *,
    owner_user: str,
    project_dir: Path,
    branch: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> bool:
    from scripts import team_launcher as launcher

    if not (project_dir / ".git").exists():
        init = _run_owner_git(owner_user, project_dir, "init", "-b", branch, runner=runner)
        if init.returncode != 0:
            raise SystemExit(
                f"switchyard: failed to initialize git repository in {project_dir}: "
                f"{launcher._proc_failure_reason(init, f'exit {init.returncode}')}"
            )
        remote = _run_owner_git(owner_user, project_dir, "remote", "get-url", "origin", runner=runner)
        if remote.returncode != 0:
            add_remote = _run_owner_git(owner_user, project_dir, "remote", "add", "origin", str(project_dir), runner=runner)
            if add_remote.returncode != 0:
                raise SystemExit(
                    f"switchyard: failed to add local origin remote for {project_dir}: "
                    f"{launcher._proc_failure_reason(add_remote, f'exit {add_remote.returncode}')}"
                )
        created_repository = True
    else:
        check = _run_owner_git(
            owner_user,
            project_dir,
            "rev-parse",
            "--is-inside-work-tree",
            runner=runner,
        )
        if check.returncode != 0:
            raise SystemExit(
                f"switchyard: project path {project_dir} has unusable git metadata: "
                f"{launcher._proc_failure_reason(check, f'exit {check.returncode}')}"
            )
        created_repository = False

    head = _run_owner_git(owner_user, project_dir, "rev-parse", "--verify", "HEAD", runner=runner)
    if head.returncode == 0:
        return created_repository
    if not created_repository:
        raise SystemExit(
            f"switchyard: project path {project_dir} is already a git repository but has no initial commit; "
            "create one yourself, or remove its .git directory and let switchyard initialize it"
        )

    add = _run_owner_git(owner_user, project_dir, "add", ".", runner=runner)
    if add.returncode != 0:
        raise SystemExit(
            f"switchyard: failed to stage initial project files in {project_dir}: "
            f"{launcher._proc_failure_reason(add, f'exit {add.returncode}')}"
        )
    commit = _run_owner_git(
        owner_user,
        project_dir,
        "-c",
        "user.name=Switchyard",
        "-c",
        "user.email=switchyard@localhost",
        "commit",
        "--allow-empty",
        "-m",
        "Initial Switchyard project",
        runner=runner,
    )
    if commit.returncode != 0:
        raise SystemExit(
            f"switchyard: failed to create initial git commit in {project_dir}: "
            f"{launcher._proc_failure_reason(commit, f'exit {commit.returncode}')}"
        )
    return True


def _require_existing_project_git_repository(
    *,
    owner_user: str,
    project_dir: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    check = _run_owner_git(
        owner_user,
        project_dir,
        "rev-parse",
        "--is-inside-work-tree",
        runner=runner,
    )
    if check.returncode != 0:
        raise SystemExit(
            f"switchyard: --no-git-init was set, but project path {project_dir} is not a git repository; "
            "initialize it yourself before running switchyard new"
        )
    head = _run_owner_git(owner_user, project_dir, "rev-parse", "--verify", "HEAD", runner=runner)
    if head.returncode != 0:
        raise SystemExit(
            f"switchyard: --no-git-init was set, but project path {project_dir} has no initial commit; "
            "create one yourself before running switchyard new"
        )


def _commit_project_git_changes(
    *,
    owner_user: str,
    project_dir: Path,
    message: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    from scripts import team_launcher as launcher

    status = _run_owner_git(owner_user, project_dir, "status", "--porcelain", runner=runner)
    if status.returncode != 0:
        raise SystemExit(
            f"switchyard: failed to inspect git status in {project_dir}: "
            f"{launcher._proc_failure_reason(status, f'exit {status.returncode}')}"
        )
    if not str(getattr(status, "stdout", "") or "").strip():
        return
    add = _run_owner_git(owner_user, project_dir, "add", ".", runner=runner)
    if add.returncode != 0:
        raise SystemExit(
            f"switchyard: failed to stage project files in {project_dir}: "
            f"{launcher._proc_failure_reason(add, f'exit {add.returncode}')}"
        )
    commit = _run_owner_git(
        owner_user,
        project_dir,
        "-c",
        "user.name=Switchyard",
        "-c",
        "user.email=switchyard@localhost",
        "commit",
        "-m",
        message,
        runner=runner,
    )
    if commit.returncode != 0:
        raise SystemExit(
            f"switchyard: failed to commit project files in {project_dir}: "
            f"{launcher._proc_failure_reason(commit, f'exit {commit.returncode}')}"
        )


def _owner_project_git_runner(
    *,
    owner_user: str,
    project_dir: Path,
    owned_roots: Sequence[Path] = (),
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> Callable[..., subprocess.CompletedProcess[Any]]:
    from scripts import team_launcher as launcher

    normalized_roots = [launcher._normalized_path(project_dir), *(launcher._normalized_path(path) for path in owned_roots)]
    owner_rules = [GitOwnerRule(root, owner_user) for root in normalized_roots]

    def is_owned_path(path: Path) -> bool:
        return any(_path_is_under(path, root) for root in normalized_roots)

    def wrapped(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if len(args) >= 3 and args[:2] == ["mkdir", "-p"] and is_owned_path(Path(args[2])):
            return runner(["sudo", "-u", owner_user, *args], **kwargs)
        if len(args) >= 3 and args[:2] == ["test", "-x"] and is_owned_path(Path(args[2])):
            return runner(["sudo", "-u", owner_user, *args], **kwargs)
        if args[:1] == ["git"]:
            return launcher.run_owner_correct_git(args, runner=runner, owner_rules=owner_rules, **kwargs)
        return runner(args, **kwargs)

    return wrapped
