"""`team-launcher upgrade` / `switchyard upgrade`: run a tenant upgrade's privileged phases, in order.

`upgrade_project_command` is the upgrade's orchestration: it pins the source
(U1), recovers the upgrade state (U2), refreshes the generated artifacts (U3),
stages the role tooling (U4), moves identities and accounts (U5) and finishes
the upgrade (U6), handing each phase what the earlier ones settled and
returning the first refusal as it came. The phases themselves live in
`scripts/upgrade_phases.py`.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-424). The launcher
imports this module and re-exports the command; its `main` and `switchyard_main`
still dispatch `upgrade` to the launcher's name. The six phases and their four
continuation types are read through the launcher at call time, so a suite that
rebinds one there still intercepts it. The definition-time defaults are
`subprocess.run` and `print`, the objects the launcher bound; the config type
is imported under TYPE_CHECKING. This module imports `team_launcher` only inside
the command, when it runs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig


def upgrade_project_command(
    config: ProjectConfig,
    *,
    config_path: Path,
    dry_run: bool = False,
    desktop_policy: Path | None = None,
    source_repo: Path | None = None,
    commit_git_dir: str | None = None,
    # `None` means the caller said nothing about the ref, which is not the same
    # as asking for the default one (SYRD-61).
    deploy_ref: str | None = None,
    # Where this tenant's root-owned role tooling is staged. Only a test names
    # it; on a host it is /usr/local/lib/switchyard (SYRD-62).
    tooling_root: Path | None = None,
    # Stated once by an operator and then recorded root-owned. It is not read
    # from the tenant, because every role runs as the account that owns the
    # tenant's git config and could aim the push somewhere else (SYRD-97 review).
    publish_remote: str = "",
    # Stated once by an operator when a tenant gains an upstream board or that
    # board moves; recorded in the tenant's configuration afterwards, so the
    # next upgrade needs no flag and the panes need none ever (SYRD-238).
    upstream_report_url: str = "",
    upstream_report_token_file: str = "",
    registry_dir: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> int:
    """Run the privileged phases of a tenant upgrade, in order, and stop there.

    The phases are ordered because their failure modes are: writing per-role
    accounts into the configuration before those accounts exist leaves roles
    that cannot start, and installing the matching board authority table would
    then stop recognising the panes that are actually running. Each phase is
    journaled so an interrupted upgrade resumes, and the phases root does not
    own -- creating the accounts, and the director's own board write -- are
    reported rather than attempted (SYRD-45).
    """
    from scripts import team_launcher as launcher

    source_pinned = launcher._pin_upgrade_source(
        config,
        commit_git_dir=commit_git_dir,
        deploy_ref=deploy_ref,
        desktop_policy=desktop_policy,
        dry_run=dry_run,
        print_func=print_func,
        publish_remote=publish_remote,
        runner=runner,
        source_repo=source_repo,
        tooling_root=tooling_root,
    )
    if not isinstance(source_pinned, launcher.UpgradeSourcePinned):
        return source_pinned
    desktop_choice = source_pinned.desktop_choice
    deploy_ref_chosen = source_pinned.deploy_ref_chosen
    source_repo = source_pinned.source_repo
    commit_git_dir = source_pinned.commit_git_dir
    deploy_ref = source_pinned.deploy_ref
    state_ready = launcher._recover_upgrade_state(
        config,
        config_path=config_path,
        deploy_ref=deploy_ref,
        desktop_policy=desktop_policy,
        dry_run=dry_run,
        print_func=print_func,
        runner=runner,
        source_repo=source_repo,
    )
    if not isinstance(state_ready, launcher.UpgradeStateReady):
        return state_ready
    source_repo = state_ready.source_repo
    effective_source_repo = state_ready.effective_source_repo
    config = state_ready.config
    cutover = state_ready.cutover

    release_report_config = config
    trusted_release_root: Path | None = None
    publication_detail = ""
    config = launcher._refresh_upgrade_artifacts(
        config,
        commit_git_dir=commit_git_dir,
        config_path=config_path,
        dry_run=dry_run,
        effective_source_repo=effective_source_repo,
        print_func=print_func,
        registry_dir=registry_dir,
        runner=runner,
        source_repo=source_repo,
        upstream_report_token_file=upstream_report_token_file,
        upstream_report_url=upstream_report_url,
    )
    tooling_staged = launcher._stage_upgrade_tooling(
        config,
        config_path=config_path,
        deploy_ref=deploy_ref,
        deploy_ref_chosen=deploy_ref_chosen,
        dry_run=dry_run,
        effective_source_repo=effective_source_repo,
        print_func=print_func,
        publication_detail=publication_detail,
        publish_remote=publish_remote,
        runner=runner,
        tooling_root=tooling_root,
        trusted_release_root=trusted_release_root,
    )
    if not isinstance(tooling_staged, launcher.UpgradeToolingStaged):
        return tooling_staged
    trusted_release_root = tooling_staged.trusted_release_root
    publication_detail = tooling_staged.publication_detail
    identities_done = launcher._upgrade_identities_and_accounts(
        config,
        commit_git_dir=commit_git_dir,
        config_path=config_path,
        cutover=cutover,
        deploy_ref=deploy_ref,
        desktop_choice=desktop_choice,
        dry_run=dry_run,
        effective_source_repo=effective_source_repo,
        print_func=print_func,
        publication_detail=publication_detail,
        publish_remote=publish_remote,
        release_report_config=release_report_config,
        runner=runner,
        source_repo=source_repo,
        tooling_root=tooling_root,
        trusted_release_root=trusted_release_root,
    )
    if not isinstance(identities_done, launcher.UpgradeIdentitiesDone):
        return identities_done
    config = identities_done.config
    release_report_config = identities_done.release_report_config

    return launcher._finish_upgrade(
        config,
        commit_git_dir=commit_git_dir,
        config_path=config_path,
        deploy_ref=deploy_ref,
        desktop_choice=desktop_choice,
        dry_run=dry_run,
        effective_source_repo=effective_source_repo,
        print_func=print_func,
        release_report_config=release_report_config,
        runner=runner,
    )
