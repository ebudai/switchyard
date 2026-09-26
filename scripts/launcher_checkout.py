"""Keeping a launcher checkout current with the ref it is meant to track.

A launcher run from a git checkout, rather than an installed release, can fall
behind the branch it follows. This module covers:
- **Probing** the checkout (`probe_launcher_checkout`): its head, whether the
  wanted commit exists locally, and how far ahead or behind it is.
- **Keeping it current** at launch (`ensure_launcher_checkout_current`): a
  fast-forward when it is clean and only behind, otherwise a clear refusal,
  unless `SWITCHYARD_ALLOW_STALE_LAUNCHER` says to carry on.
- **Deploying it on request** (`deploy_launcher_checkout`).
- **The staleness warning** an upgrade gives for an artifact-source checkout.
- **The git argv builders** for all of the above.

Every builder here runs only through the owner-correct chokepoint
`run_owner_correct_git`, via `_launcher_checkout_runner`.
`tests/team_launcher_git_ownership_lint_test.py` scans this module too.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-292). `team_launcher`
imports this module at its top and still exports every name callers read there.
This module never imports `team_launcher` at its top. The chokepoint
(`run_owner_correct_git`, `GitOwnerRule`, `_path_owner_user`), `_repo_root`,
`worktree_ref` and the other launcher facilities are read from
`scripts.team_launcher` when a function runs, so the suites' patches there
still reach them. Installing a shared release is a separate thing, and stays
in the launcher.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

if TYPE_CHECKING:
    from scripts.team_launcher import GitOwnerRule, ProjectConfig


ALLOW_STALE_LAUNCHER_ENV = "TEAM_LAUNCHER_ALLOW_STALE"


LEGACY_ALLOW_STALE_LAUNCHER_ENV = "PGU_TEAM_LAUNCHER_ALLOW_STALE"


@dataclass(frozen=True)
class LauncherCheckoutProbe:
    ahead: int = 0
    behind: int = 0
    behind_exact: bool = True
    error: str = ""


def git_fetch_launcher_ref_args(config: ProjectConfig, launcher_repo: Path) -> list[str]:
    return ["git", "-C", str(launcher_repo), "fetch", config.worktree_remote, config.worktree_branch]


def git_launcher_checkout_check_args(launcher_repo: Path) -> list[str]:
    return ["git", "-C", str(launcher_repo), "rev-parse", "--is-inside-work-tree"]


def git_launcher_head_args(launcher_repo: Path) -> list[str]:
    return ["git", "-C", str(launcher_repo), "rev-parse", "HEAD"]


def git_launcher_ls_remote_ref_args(config: ProjectConfig, launcher_repo: Path) -> list[str]:
    return ["git", "-C", str(launcher_repo), "ls-remote", "--exit-code", config.worktree_remote, config.worktree_branch]


def git_launcher_commit_exists_args(launcher_repo: Path, commit: str) -> list[str]:
    return ["git", "-C", str(launcher_repo), "cat-file", "-e", f"{commit}^{{commit}}"]


def git_launcher_ahead_behind_args(
    config: ProjectConfig,
    launcher_repo: Path,
    compare_ref: str | None = None,
) -> list[str]:
    from scripts import team_launcher as launcher

    return [
        "git",
        "-C",
        str(launcher_repo),
        "rev-list",
        "--left-right",
        "--count",
        f"HEAD...{compare_ref or launcher.worktree_ref(config)}",
    ]


def git_checkout_launcher_branch_args(config: ProjectConfig, launcher_repo: Path) -> list[str]:
    return ["git", "-C", str(launcher_repo), "checkout", "--force", config.worktree_branch]


def git_fast_forward_launcher_ref_args(config: ProjectConfig, launcher_repo: Path) -> list[str]:
    from scripts import team_launcher as launcher

    return ["git", "-C", str(launcher_repo), "merge", "--ff-only", launcher.worktree_ref(config)]


def git_launcher_current_branch_args(launcher_repo: Path) -> list[str]:
    return ["git", "-C", str(launcher_repo), "symbolic-ref", "--quiet", "--short", "HEAD"]


def git_launcher_status_porcelain_args(launcher_repo: Path) -> list[str]:
    return ["git", "-C", str(launcher_repo), "status", "--porcelain", "--untracked-files=no"]


def git_launcher_head_short_args(launcher_repo: Path) -> list[str]:
    return ["git", "-C", str(launcher_repo), "rev-parse", "--short", "HEAD"]


def git_clean_launcher_checkout_args(launcher_repo: Path) -> list[str]:
    return ["git", "-C", str(launcher_repo), "clean", "-fdx"]


def _owner_correct_git_runner(
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    owner_rules: Sequence[GitOwnerRule] = (),
) -> Callable[..., subprocess.CompletedProcess[Any]]:
    from scripts import team_launcher as launcher

    def wrapped(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if args[:1] == ["git"]:
            return launcher.run_owner_correct_git(args, runner=runner, owner_rules=owner_rules, **kwargs)
        return runner(args, **kwargs)

    return wrapped


def _launcher_checkout_runner(
    repo: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> tuple[Callable[..., subprocess.CompletedProcess[Any]] | None, str]:
    from scripts import team_launcher as launcher

    owner_user, error = launcher._path_owner_user(repo)
    if not owner_user:
        return None, error or "owner is unknown"
    return _owner_correct_git_runner(runner=runner, owner_rules=[launcher.GitOwnerRule(repo, owner_user)]), ""


def _parse_ahead_behind(output: str) -> tuple[int, int] | None:
    parts = output.strip().split()
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def _format_behind_count(count: int, *, exact: bool = True) -> str:
    if exact:
        return f"{count} commit(s)"
    return f"at least {count} commit(s)"


def probe_launcher_checkout(
    config: ProjectConfig,
    *,
    launcher_repo: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> LauncherCheckoutProbe:
    from scripts import team_launcher as launcher

    repo = launcher_repo or launcher._repo_root()
    check_proc = launcher.run_owner_correct_git(
        git_launcher_checkout_check_args(repo),
        runner=runner,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if check_proc.returncode != 0:
        return LauncherCheckoutProbe(error=f"launcher path is not a git checkout: {repo}")
    head_proc = launcher.run_owner_correct_git(
        git_launcher_head_args(repo),
        runner=runner,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if head_proc.returncode != 0:
        return LauncherCheckoutProbe(
            error=(
                f"failed to read launcher checkout HEAD in {repo}: "
                f"{launcher._proc_failure_reason(head_proc, f'exit {head_proc.returncode}')}"
            )
        )
    local_head = str(head_proc.stdout or "").strip()
    remote_proc = launcher.run_owner_correct_git(
        git_launcher_ls_remote_ref_args(config, repo),
        runner=runner,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if remote_proc.returncode != 0:
        return LauncherCheckoutProbe(
            error=(
                f"failed to inspect launcher deploy ref {launcher.worktree_ref(config)} without fetching in {repo}: "
                f"{launcher._proc_failure_reason(remote_proc, f'exit {remote_proc.returncode}')}"
            )
        )
    remote_head = launcher._parse_ls_remote_head(str(remote_proc.stdout or ""))
    if not remote_head:
        return LauncherCheckoutProbe(
            error=f"could not parse launcher deploy ref {launcher.worktree_ref(config)} from ls-remote output"
        )
    if local_head == remote_head:
        return LauncherCheckoutProbe(ahead=0, behind=0)
    exists_proc = launcher.run_owner_correct_git(
        git_launcher_commit_exists_args(repo, remote_head),
        runner=runner,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if exists_proc.returncode != 0:
        # The remote tip is not in the local object database. Avoid fetching in
        # the hot-path status probe; the deploy path will fetch before merging.
        return LauncherCheckoutProbe(ahead=0, behind=1, behind_exact=False)
    count_proc = launcher.run_owner_correct_git(
        git_launcher_ahead_behind_args(config, repo, remote_head),
        runner=runner,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if count_proc.returncode != 0:
        return LauncherCheckoutProbe(
            error=(
                f"failed to compare launcher checkout {repo} with {launcher.worktree_ref(config)}: "
                f"{launcher._proc_failure_reason(count_proc, f'exit {count_proc.returncode}')}"
            )
        )
    counts = _parse_ahead_behind(str(count_proc.stdout or ""))
    if counts is None:
        return LauncherCheckoutProbe(
            error=(
                f"could not parse launcher ahead/behind count for {repo}: "
                f"{str(count_proc.stdout or '').strip()!r}"
            )
        )
    ahead, behind = counts
    return LauncherCheckoutProbe(ahead=ahead, behind=behind)


def probe_checkout_against_worktree_ref(
    config: ProjectConfig,
    repo: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> LauncherCheckoutProbe:
    from scripts import team_launcher as launcher

    check_proc = launcher.run_owner_correct_git(
        git_launcher_checkout_check_args(repo),
        runner=runner,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if check_proc.returncode != 0:
        return LauncherCheckoutProbe(error=f"path is not a git checkout: {repo}")
    head_proc = launcher.run_owner_correct_git(
        git_launcher_head_args(repo),
        runner=runner,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if head_proc.returncode != 0:
        return LauncherCheckoutProbe(
            error=(
                f"failed to read HEAD in {repo}: "
                f"{launcher._proc_failure_reason(head_proc, f'exit {head_proc.returncode}')}"
            )
        )
    local_head = str(head_proc.stdout or "").strip()
    remote_proc = launcher.run_owner_correct_git(
        git_launcher_ls_remote_ref_args(config, repo),
        runner=runner,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if remote_proc.returncode != 0:
        return LauncherCheckoutProbe(
            error=(
                f"failed to inspect {launcher.worktree_ref(config)} without fetching in {repo}: "
                f"{launcher._proc_failure_reason(remote_proc, f'exit {remote_proc.returncode}')}"
            )
        )
    remote_head = launcher._parse_ls_remote_head(str(remote_proc.stdout or ""))
    if not remote_head:
        return LauncherCheckoutProbe(error=f"could not parse {launcher.worktree_ref(config)} from ls-remote output")
    if local_head == remote_head:
        return LauncherCheckoutProbe(ahead=0, behind=0)
    exists_proc = launcher.run_owner_correct_git(
        git_launcher_commit_exists_args(repo, remote_head),
        runner=runner,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if exists_proc.returncode != 0:
        return LauncherCheckoutProbe(ahead=0, behind=1, behind_exact=False)
    count_proc = launcher.run_owner_correct_git(
        git_launcher_ahead_behind_args(config, repo, remote_head),
        runner=runner,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if count_proc.returncode != 0:
        return LauncherCheckoutProbe(
            error=(
                f"failed to compare {repo} with {launcher.worktree_ref(config)}: "
                f"{launcher._proc_failure_reason(count_proc, f'exit {count_proc.returncode}')}"
            )
        )
    counts = _parse_ahead_behind(str(count_proc.stdout or ""))
    if counts is None:
        return LauncherCheckoutProbe(error=f"could not parse ahead/behind count for {repo}")
    ahead, behind = counts
    return LauncherCheckoutProbe(ahead=ahead, behind=behind)


def launcher_checkout_status(
    config: ProjectConfig,
    *,
    launcher_repo: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> tuple[int, int]:
    from scripts import team_launcher as launcher

    repo = launcher_repo or launcher._repo_root()
    probe = probe_launcher_checkout(config, launcher_repo=repo, runner=runner)
    if probe.error:
        raise SystemExit(
            f"team-launcher: cannot determine launcher checkout freshness for {repo}: {probe.error}"
        )
    return probe.ahead, probe.behind


def _short_head(repo: Path, *, runner: Callable[..., subprocess.CompletedProcess[Any]]) -> str:
    from scripts import team_launcher as launcher

    proc = launcher.run_owner_correct_git(
        git_launcher_head_short_args(repo),
        runner=runner,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if proc.returncode != 0:
        return "unknown"
    return str(proc.stdout or "").strip() or "unknown"


def _auto_fast_forward_launcher_checkout(
    config: ProjectConfig,
    repo: Path,
    *,
    behind: int,
    behind_exact: bool,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> LauncherCheckoutProbe:
    from scripts import team_launcher as launcher

    branch_proc = launcher.run_owner_correct_git(
        git_launcher_current_branch_args(repo),
        runner=runner,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    branch = str(branch_proc.stdout or "").strip()
    if branch_proc.returncode != 0 or branch != config.worktree_branch:
        reason = branch or "detached HEAD"
        raise SystemExit(
            f"team-launcher: refusing to launch from stale checkout {repo}; "
            f"it is {_format_behind_count(behind, exact=behind_exact)} behind {launcher.worktree_ref(config)}, "
            "and automatic fast-forward "
            f"requires branch {config.worktree_branch!r} but found {reason!r}. "
            f"Run `scripts/team-launcher {config.project} deploy-launcher --launcher-repo {repo}` "
            "during an approved restart window, or rerun with --allow-stale-launcher in an emergency."
        )
    status_proc = launcher.run_owner_correct_git(
        git_launcher_status_porcelain_args(repo),
        runner=runner,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if status_proc.returncode != 0:
        raise SystemExit(
            f"team-launcher: refusing to launch from stale checkout {repo}; "
            f"could not inspect local changes before automatic fast-forward: "
            f"{launcher._proc_failure_reason(status_proc, f'exit {status_proc.returncode}')}"
        )
    if str(status_proc.stdout or "").strip():
        raise SystemExit(
            f"team-launcher: refusing to launch from stale checkout {repo}; "
            "local changes are present, so automatic fast-forward is not safe. "
            f"Run `scripts/team-launcher {config.project} deploy-launcher --launcher-repo {repo}` "
            "during an approved restart window, or rerun with --allow-stale-launcher in an emergency."
        )
    before = _short_head(repo, runner=runner)
    fetch_proc = launcher.run_owner_correct_git(git_fetch_launcher_ref_args(config, repo), runner=runner)
    if fetch_proc.returncode != 0:
        raise SystemExit(
            f"team-launcher: refusing to launch from stale checkout {repo}; "
            f"automatic fetch of {launcher.worktree_ref(config)} failed: "
            f"{launcher._proc_failure_reason(fetch_proc, f'exit {fetch_proc.returncode}')}. "
            f"Run `scripts/team-launcher {config.project} deploy-launcher --launcher-repo {repo}` "
            "during an approved restart window, or rerun with --allow-stale-launcher in an emergency."
        )
    merge_proc = launcher.run_owner_correct_git(git_fast_forward_launcher_ref_args(config, repo), runner=runner)
    if merge_proc.returncode != 0:
        raise SystemExit(
            f"team-launcher: refusing to launch from stale checkout {repo}; "
            f"automatic fast-forward to {launcher.worktree_ref(config)} failed: "
            f"{launcher._proc_failure_reason(merge_proc, f'exit {merge_proc.returncode}')}. "
            f"Run `scripts/team-launcher {config.project} deploy-launcher --launcher-repo {repo}` "
            "during an approved restart window, or rerun with --allow-stale-launcher in an emergency."
        )
    after = _short_head(repo, runner=runner)
    probe = probe_launcher_checkout(config, launcher_repo=repo, runner=runner)
    if probe.error:
        print(
            f"warning: team-launcher: auto-fast-forwarded launcher checkout {repo} "
            f"from {before} to {after}, but freshness is now undeterminable: {probe.error}; continuing",
            file=sys.stderr,
        )
        return probe
    if probe.behind:
        raise SystemExit(
            f"team-launcher: launcher checkout {repo} is still {probe.behind} commit(s) "
            f"behind {launcher.worktree_ref(config)} after automatic fast-forward"
        )
    print(
        f"team-launcher: auto-fast-forwarded launcher checkout {repo} from {before} to {after}; "
        f"was {_format_behind_count(behind, exact=behind_exact)} behind {launcher.worktree_ref(config)}",
        file=sys.stderr,
    )
    return probe


def ensure_launcher_checkout_current(
    config: ProjectConfig,
    *,
    launcher_repo: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    auto_deploy: bool = False,
    allow_stale: bool = False,
) -> None:
    from scripts import team_launcher as launcher

    repo = launcher_repo or launcher._repo_root()
    release = launcher.shared_switchyard_release_for_path(repo)
    if release is not None:
        return
    allow_stale = allow_stale or launcher._env_truthy_any(ALLOW_STALE_LAUNCHER_ENV, LEGACY_ALLOW_STALE_LAUNCHER_ENV)
    checkout_runner, owner_error = _launcher_checkout_runner(repo, runner=runner)
    if checkout_runner is None:
        print(
            f"warning: team-launcher: cannot determine launcher checkout owner for {repo}: "
            f"{owner_error}; skipping freshness probe",
            file=sys.stderr,
        )
        return
    probe = probe_launcher_checkout(config, launcher_repo=repo, runner=checkout_runner)
    if probe.error:
        print(
            f"warning: team-launcher: cannot determine launcher checkout freshness for {repo}: "
            f"{probe.error}; continuing",
            file=sys.stderr,
        )
        return
    if probe.behind:
        if allow_stale:
            print(
                f"warning: team-launcher: OVERRIDE proceeding with stale launcher checkout {repo}; "
                f"it is {_format_behind_count(probe.behind, exact=probe.behind_exact)} "
                f"behind {launcher.worktree_ref(config)}",
                file=sys.stderr,
            )
            return
        if auto_deploy:
            post_deploy_probe = _auto_fast_forward_launcher_checkout(
                config,
                repo,
                behind=probe.behind,
                behind_exact=probe.behind_exact,
                runner=checkout_runner,
            )
            if post_deploy_probe.error or post_deploy_probe.behind:
                return
            probe = post_deploy_probe
        else:
            raise SystemExit(
                f"team-launcher: refusing to launch from stale checkout {repo}; "
                f"it is {_format_behind_count(probe.behind, exact=probe.behind_exact)} "
                f"behind {launcher.worktree_ref(config)}. "
                f"Run `scripts/team-launcher {config.project} deploy-launcher --launcher-repo {repo}` "
                "during an approved restart window, rerun with --allow-stale-launcher in an emergency, "
                "or unset --no-launcher-self-deploy for automatic fast-forward."
            )
    if probe.ahead:
        print(
            f"warning: launcher checkout {repo} is {probe.ahead} commit(s) ahead of {launcher.worktree_ref(config)}",
            file=sys.stderr,
        )


def deploy_launcher_checkout(
    config: ProjectConfig,
    *,
    launcher_repo: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    clean: bool = False,
) -> int:
    from scripts import team_launcher as launcher

    repo = launcher_repo or launcher._repo_root()
    checkout_runner, owner_error = _launcher_checkout_runner(repo, runner=runner)
    if checkout_runner is None:
        print(
            f"team-launcher: cannot determine launcher checkout owner for {repo}: "
            f"{owner_error}; not deploying launcher checkout",
            file=sys.stderr,
        )
        return 1
    check_proc = launcher.run_owner_correct_git(
        git_launcher_checkout_check_args(repo),
        runner=checkout_runner,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if check_proc.returncode != 0:
        print(f"team-launcher: launcher path is not a git checkout: {repo}", file=sys.stderr)
        return int(check_proc.returncode)
    fetch_proc = launcher.run_owner_correct_git(git_fetch_launcher_ref_args(config, repo), runner=checkout_runner)
    if fetch_proc.returncode != 0:
        print(
            f"team-launcher: failed to fetch {launcher.worktree_ref(config)} in {repo}: "
            f"{launcher._proc_failure_reason(fetch_proc, f'exit {fetch_proc.returncode}')}",
            file=sys.stderr,
        )
        return int(fetch_proc.returncode)
    checkout_proc = launcher.run_owner_correct_git(git_checkout_launcher_branch_args(config, repo), runner=checkout_runner)
    if checkout_proc.returncode != 0:
        print(
            f"team-launcher: failed to checkout launcher branch {config.worktree_branch} in {repo}: "
            f"{launcher._proc_failure_reason(checkout_proc, f'exit {checkout_proc.returncode}')}",
            file=sys.stderr,
        )
        return int(checkout_proc.returncode)
    merge_proc = launcher.run_owner_correct_git(git_fast_forward_launcher_ref_args(config, repo), runner=checkout_runner)
    if merge_proc.returncode != 0:
        print(
            f"team-launcher: failed to fast-forward launcher checkout {repo} to {launcher.worktree_ref(config)}: "
            f"{launcher._proc_failure_reason(merge_proc, f'exit {merge_proc.returncode}')}",
            file=sys.stderr,
        )
        return int(merge_proc.returncode)
    if clean:
        clean_proc = launcher.run_owner_correct_git(git_clean_launcher_checkout_args(repo), runner=checkout_runner)
        if clean_proc.returncode != 0:
            print(
                f"team-launcher: failed to clean launcher checkout {repo}: "
                f"{launcher._proc_failure_reason(clean_proc, f'exit {clean_proc.returncode}')}",
                file=sys.stderr,
            )
            return int(clean_proc.returncode)
    ahead, behind = launcher_checkout_status(config, launcher_repo=repo, runner=checkout_runner)
    if behind:
        print(
            f"team-launcher: launcher checkout {repo} is still {behind} commit(s) behind {launcher.worktree_ref(config)}",
            file=sys.stderr,
        )
        return 1
    print(f"team-launcher: launcher checkout {repo} is current at {launcher.worktree_ref(config)}")
    return 0


def warn_if_artifact_source_checkout_is_stale(
    config: ProjectConfig,
    *,
    source_repo: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    print_func: Callable[[str], None],
) -> None:
    from scripts import team_launcher as launcher

    if not source_repo.exists():
        return
    shared_release = launcher.shared_switchyard_release_for_path(source_repo)
    if shared_release is not None:
        if shared_release.marker_commit:
            print_func(
                f"switchyard: artifact source is shared release {shared_release.root} "
                f"at {shared_release.marker_commit}"
            )
        elif shared_release.marker_error:
            print_func(
                f"warning: switchyard: cannot determine artifact source shared release "
                f"for {shared_release.root}: {shared_release.marker_error}"
            )
        else:
            print_func(
                f"warning: switchyard: cannot determine artifact source shared release "
                f"for {shared_release.root}: missing release marker"
            )
        return
    probe = probe_checkout_against_worktree_ref(config, source_repo, runner=runner)
    if probe.error:
        print_func(f"warning: switchyard: cannot determine artifact source checkout freshness for {source_repo}: {probe.error}")
        return
    if probe.behind:
        print_func(
            f"warning: switchyard: artifact source checkout {source_repo} is "
            f"{_format_behind_count(probe.behind, exact=probe.behind_exact)} behind {launcher.worktree_ref(config)}; "
            "generated files will reflect this checkout, not the remote tip"
        )
    if probe.ahead:
        print_func(
            f"warning: switchyard: artifact source checkout {source_repo} is "
            f"{probe.ahead} commit(s) ahead of {launcher.worktree_ref(config)}; generated files may include unmerged changes"
        )
