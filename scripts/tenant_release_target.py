"""Which release a tenant runs, which one it should, and where that comes from.

- `switchyard_bare_repo` is the source cache `SWITCHYARD_BARE_REPO` names, and
  refuses one that is not an absolute path; `explicit_source_caches` are the
  absolute caches an operator named in `--commit-git-dir`, in their order.
- `_parse_ls_remote_head`, `_deploy_ref_remote_branch` and the
  `git_deploy_ref_*_args` builders read a deploy ref and the git argv that
  resolves it. `_resolve_deploy_ref_readonly` resolves it in a checkout through
  the owner-correct git chokepoint, and `_resolve_deploy_ref_from_bare_repo` in
  a bare source cache; neither fetches nor writes.
- `tenant_release_status` observes a tenant's current release and resolves its
  target -- from an installed release's own marker first, then only from an
  explicitly named cache, or from a source checkout -- and
  `installed_release_deploy_target` answers for that marker, refusing rather
  than falling back (SYRD-61, SYRD-100).

Moved out of `scripts/team_launcher.py` unchanged (SYRD-361). The launcher
imports this module at its top and re-exports every name, so the release
upgrade and deploy paths and the modules that read them through the launcher
-- `launcher_checkout`, `project_status` -- reach the same objects. Every
launcher facility these use, every name defined here that another definition
here reads, and the `TenantReleaseStatus` they build are read from
`team_launcher` when they run, as they were. The publication boundary's
`root_controlled_problems` is still imported inside the function that uses it.
The default deploy ref comes from the `release_refs` leaf, the object the
launcher imports; the standard-library names are this module's own imports,
the same objects. This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from scripts.release_refs import DEFAULT_TENANT_RELEASE_DEPLOY_REF

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig, TenantReleaseStatus


def switchyard_bare_repo() -> Path | None:
    selected = os.environ.get("SWITCHYARD_BARE_REPO", "").strip()
    if not selected:
        return None
    cache = Path(selected).expanduser()
    if not cache.is_absolute():
        raise SystemExit("switchyard: SWITCHYARD_BARE_REPO must be an absolute source-cache path")
    return cache


def _parse_ls_remote_head(output: str) -> str | None:
    for line in output.splitlines():
        parts = line.strip().split()
        if parts:
            return parts[0]
    return None


def _deploy_ref_remote_branch(deploy_ref: str) -> tuple[str, str] | None:
    if deploy_ref.startswith("refs/") or "/" not in deploy_ref:
        return None
    remote, branch = deploy_ref.split("/", 1)
    if not remote or not branch:
        return None
    return remote, branch


def git_deploy_ref_ls_remote_args(source_repo: Path, deploy_ref: str) -> list[str]:
    from scripts import team_launcher as launcher

    remote_branch = launcher._deploy_ref_remote_branch(deploy_ref)
    if remote_branch is None:
        raise ValueError(f"deploy ref does not name a remote branch: {deploy_ref}")
    remote, branch = remote_branch
    return ["git", "-C", str(source_repo), "ls-remote", remote, f"refs/heads/{branch}"]


def git_deploy_ref_rev_parse_args(source_repo: Path, deploy_ref: str) -> list[str]:
    return ["git", "-C", str(source_repo), "rev-parse", "--verify", f"{deploy_ref}^{{commit}}"]


def _resolve_deploy_ref_readonly(
    source_repo: Path,
    deploy_ref: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> tuple[str, str]:
    from scripts import team_launcher as launcher

    if launcher._deploy_ref_remote_branch(deploy_ref) is not None:
        remote_proc = launcher.run_owner_correct_git(
            launcher.git_deploy_ref_ls_remote_args(source_repo, deploy_ref),
            runner=runner,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if remote_proc.returncode == 0:
            remote_sha = launcher._parse_ls_remote_head(str(remote_proc.stdout or ""))
            if remote_sha:
                return remote_sha, ""
        else:
            return "", (
                f"`{' '.join(launcher.git_deploy_ref_ls_remote_args(source_repo, deploy_ref))}` failed: "
                + launcher._proc_failure_reason(remote_proc, f"git ls-remote exited {remote_proc.returncode}")
            )
    rev_parse_proc = launcher.run_owner_correct_git(
        launcher.git_deploy_ref_rev_parse_args(source_repo, deploy_ref),
        runner=runner,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if rev_parse_proc.returncode != 0:
        return "", (
            f"`{' '.join(launcher.git_deploy_ref_rev_parse_args(source_repo, deploy_ref))}` failed: "
            + launcher._proc_failure_reason(rev_parse_proc, f"git rev-parse exited {rev_parse_proc.returncode}")
        )
    return str(rev_parse_proc.stdout or "").strip(), ""


def _resolve_deploy_ref_from_bare_repo(
    bare_repo: Path,
    deploy_ref: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> tuple[str, str]:
    from scripts import team_launcher as launcher

    remote_branch = launcher._deploy_ref_remote_branch(deploy_ref)
    refs = (
        (f"refs/remotes/{remote_branch[0]}/{remote_branch[1]}",)
        if remote_branch is not None
        else (deploy_ref,)
    )
    errors: list[str] = []
    for ref in refs:
        proc = runner(
            ["git", f"--git-dir={bare_repo}", "rev-parse", "--verify", f"{ref}^{{commit}}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if proc.returncode == 0:
            return str(proc.stdout or "").strip(), ""
        errors.append(f"{ref}: " + launcher._proc_failure_reason(proc, f"git rev-parse exited {proc.returncode}"))
    return "", "; ".join(errors)


def explicit_source_caches(commit_git_dir: str | None) -> tuple[Path, ...]:
    """The source caches an operator named, in the order they named them.

    An installed shared release carries no history of its own, so the commit to
    deploy has to come out of a repository somebody chose. Until now the only
    way to choose one was `SWITCHYARD_BARE_REPO`, and an environment variable
    cannot survive the operator handoff: the accounts phase hands the upgrade
    back through `sudo`, which scrubs it. `--commit-git-dir` is the same
    selection carried as an argument -- it is already the pinned list of
    repositories this tenant verifies commits against -- so a pinned release
    stays pinned across the handoff (SYRD-61).

    Only absolute paths: a cache resolved against whatever directory root
    happened to be in is exactly the ambient guessing this replaces.
    """
    return tuple(
        candidate
        for item in (commit_git_dir or "").split(os.pathsep)
        if item.strip()
        for candidate in (Path(item.strip()).expanduser(),)
        if candidate.is_absolute()
    )


def tenant_release_status(
    config: ProjectConfig,
    *,
    config_path: Path | None = None,
    source_repo: Path | None = None,
    commit_git_dir: str | None = None,
    deploy_ref: str = DEFAULT_TENANT_RELEASE_DEPLOY_REF,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> TenantReleaseStatus | None:
    from scripts import team_launcher as launcher

    board_root = launcher._tenant_board_root_from_config_or_plan(config, config_path)
    if board_root is None:
        return None
    plan_data = launcher._plan_data_from_config(config, config_path) if config_path is not None else {}
    owner_user = str(plan_data.get("owner_user") or config.run_as_user or launcher.current_user_name()).strip()
    if not owner_user:
        raise SystemExit(f"switchyard: cannot determine tenant owner for {config.project}")
    raw_owner_home = str(plan_data.get("owner_home") or "").strip()
    if raw_owner_home:
        owner_home = Path(raw_owner_home).expanduser()
    elif board_root.name == f"{config.project}-ticketboard-live":
        owner_home = board_root.parent
    else:
        raise SystemExit(
            f"switchyard: provision plan for {config.project} is missing owner_home and it cannot be "
            f"derived from board_root {board_root}"
        )
    if not owner_home.is_absolute():
        raise SystemExit(f"switchyard: tenant owner_home must be absolute: {owner_home}")
    provisioned_system_unit = None
    if config_path is not None:
        # Prefer root's own copy. The provision directory belongs to the tenant,
        # so installing from it would let the tenant choose what root runs; the
        # mirror under /etc/switchyard is written and owned by root and is the
        # source the printed install commands must name (SYRD-39).
        candidates = [
            launcher.privileged_provision_dir(config.project, root=launcher.switchyard_privileged_provision_root())
            / f"{config.project}-ticket-board.service",
            (config_path.parent / f"{config.project}-ticket-board.service").resolve(strict=False),
        ]
        for candidate in candidates:
            required_units = (
                candidate,
                candidate.parent / f"{config.project}-ticket-board-canary.service",
                candidate.parent / f"{config.project}-ticket-board-notify-listener.service",
            )
            if all(path.is_file() for path in required_units):
                provisioned_system_unit = candidate
                break
    selected_commit_git_dir = (
        commit_git_dir.strip()
        if commit_git_dir is not None
        else str(plan_data.get("commit_git_dir") or "").strip()
    )
    resolved_source_repo = (source_repo or launcher._repo_root()).expanduser().resolve(strict=False)
    current_release, current_sha = launcher._current_tenant_release(board_root)
    clone_source_repo: Path | None = None
    if launcher.shared_switchyard_release_for_path(resolved_source_repo) is not None:
        # Root's own marker first, when the operator named this release and its
        # exact commit. The release is the content; asking the publication cache
        # whether it has heard of a commit that has deliberately not been
        # published yet is a question with only one answer (SYRD-100 review).
        marked_sha, marker_refusal = launcher.installed_release_deploy_target(
            resolved_source_repo, deploy_ref
        )
        if marked_sha or marker_refusal:
            return launcher.TenantReleaseStatus(
                board_root=board_root,
                owner_user=owner_user,
                owner_home=owner_home,
                provisioned_system_unit=provisioned_system_unit,
                # Unchanged. The tenant's ordinary provenance cache is what a
                # commit hash is verified against and it stays exactly what it
                # was; nothing here seeds it, and the bootstrap repository never
                # becomes it.
                commit_git_dir=selected_commit_git_dir,
                current_release=current_release,
                current_sha=current_sha,
                target_sha=marked_sha,
                deploy_ref=deploy_ref,
                source_repo=resolved_source_repo,
                resolve_error=marker_refusal,
                # None: the release tree is deployed directly, with no archive
                # step, because it is already the materialized commit.
                clone_source_repo=None,
                board_port=str(plan_data.get("port") or "").strip(),
                board_socket=str(plan_data.get("socket_path") or "").strip(),
            )
        # The argument, never the tenant's plan. The plan's commit_git_dir is a
        # tenant-writable document and it stays what it has always been -- the
        # list of repositories a commit hash is verified against. Choosing which
        # tree root archives and deploys is a different decision, and only an
        # explicit argument or root's own record of one makes it (SYRD-61).
        caches = list(launcher.explicit_source_caches(commit_git_dir))
        environment_cache = launcher.switchyard_bare_repo()
        if environment_cache is not None:
            caches.append(environment_cache)
        target_sha = ""
        resolve_error = (
            "installed shared releases require an explicit source cache in --commit-git-dir "
            "or SWITCHYARD_BARE_REPO, or an explicit --source-repo checkout"
        )
        failures: list[str] = []
        for cache in caches:
            candidate = cache.resolve(strict=False)
            target_sha, cache_error = launcher._resolve_deploy_ref_from_bare_repo(candidate, deploy_ref, runner=runner)
            if target_sha:
                clone_source_repo = candidate
                resolve_error = ""
                break
            failures.append(f"{candidate}: {cache_error}")
        else:
            if failures:
                resolve_error = "; ".join(failures)
    else:
        target_sha, resolve_error = launcher._resolve_deploy_ref_readonly(resolved_source_repo, deploy_ref, runner=runner)
    return launcher.TenantReleaseStatus(
        board_root=board_root,
        owner_user=owner_user,
        owner_home=owner_home,
        provisioned_system_unit=provisioned_system_unit,
        commit_git_dir=selected_commit_git_dir,
        current_release=current_release,
        current_sha=current_sha,
        target_sha=target_sha,
        deploy_ref=deploy_ref,
        source_repo=resolved_source_repo,
        resolve_error=resolve_error,
        clone_source_repo=clone_source_repo,
        # From the tenant's own plan, which is where the unit's --port and
        # --unix-socket come from, so the deploy probes the board it deployed.
        board_port=str(plan_data.get("port") or "").strip(),
        board_socket=str(plan_data.get("socket_path") or "").strip(),
    )


def installed_release_deploy_target(
    source_repo: Path, deploy_ref: str, *, install_root: Path | None = None
) -> tuple[str, str]:
    """The commit an explicitly named installed release deploys, or why not.

    An operator who has just bootstrapped a release names it and its exact SHA:
    `--source-repo /opt/switchyard/releases/<sha> --deploy-ref <sha>`. The
    release is root-owned, immutable, and carries root's own marker saying which
    commit it is. That marker is the deploy target.

    It used to be resolved instead through the tenant's ordinary publication
    cache, which is circular for the case the bootstrap exists to serve: the
    whole point of bootstrapping from a bundle is that the commit has not been
    published yet, so the cache does not have it and never will until it is. The
    live upgrade refused with `cannot produce a safe release update` for a
    release root had already materialized and activated (SYRD-100 review).

    Returns (commit, ""), or ("", reason). A reason is a refusal: the caller must
    not fall back to anything. Only an exact 40-character SHA is answered here --
    a symbolic ref is somebody asking to resolve a name, which this cannot do and
    must not guess at -- and it must be the SHA this release says it is.
    """
    from scripts import team_launcher as launcher

    wanted = (deploy_ref or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", wanted):
        return "", ""
    release = launcher.shared_switchyard_release_for_path(source_repo, install_root=install_root)
    if release is None:
        return "", ""
    from scripts.ticket_board.publication_boundary import root_controlled_problems

    # The same documented seam the rest of the trusted-release path uses: the
    # walk starts at "/" on a host, and moves only when the shared install root
    # has been overridden, because a fixture cannot own "/" (SYRD-97).
    #
    # `expect_uid` is root's, stated rather than defaulted. The default is the
    # caller's own uid, which is right where the caller IS root writing its own
    # artifacts -- what that helper was built for -- and exactly wrong here. The
    # question is whether ROOT controls this release, and an unprivileged caller
    # asking it was told that a root-owned path is not root-controlled because
    # root owns it. `finish-upgrade` is unprivileged by design and is the command
    # that most needs this answer, so the branch could never fire where it was
    # needed most (SYRD-100 review).
    overridden_root = os.environ.get("SWITCHYARD_SHARED_INSTALL_ROOT", "").strip()
    problems = root_controlled_problems(
        str(source_repo),
        expect_uid=os.getuid() if overridden_root else 0,
        base=overridden_root or "/",
    )
    if problems:
        return "", (
            f"{source_repo} is named as an installed release but is not root-controlled: "
            + "; ".join(problems)
        )
    marked = (release.marker_commit or "").strip().lower()
    if not marked:
        return "", (
            f"{source_repo} carries no release marker, so there is nothing to say which commit it "
            "is; it is not deployed from"
        )
    if marked != wanted:
        return "", (
            f"{source_repo} is the installed release for {marked}, but this deploy is pinned at "
            f"{wanted}. Nothing was deployed: deploying one release while naming another is how a "
            "board ends up running code nobody selected."
        )
    return marked, ""
