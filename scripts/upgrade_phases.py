"""`upgrade_project_command`'s phases, each a bounded step the upgrade runs in order.

`upgrade_project_command` in `scripts/team_launcher.py` stays the orchestration:
it keeps its name, signature and defaults, and calls each phase here, at the
phase's old position, by the launcher's own name.

- **U4, role tooling: preview and privileged staging** (`_stage_upgrade_tooling`,
  SYRD-346): on a dry run, what staging would do; as root, the untrusted
  migration removed, a verified release resolved, the rollback note, the staged
  tooling and hooks, and the publication boundary removed. Each refusal records
  the artifacts phase blocked and answers 1; going on returns a frozen
  `UpgradeToolingStaged` with the verified release root and the publication
  detail -- each the caller's own value when this phase did not change it.

Every launcher facility a phase uses is read from `scripts/team_launcher.py`
when the phase runs, so a patch there still reaches it. This module never
imports `team_launcher` at its top.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig


@dataclass(frozen=True)
class UpgradeToolingStaged:
    """What U4 hands the rest of `upgrade_project_command` when the upgrade goes
    on, in the order U4 assigns them. A refusal gets its code instead."""

    trusted_release_root: Path | None
    publication_detail: str


def _stage_upgrade_tooling(
    config: ProjectConfig,
    *,
    config_path: Path,
    deploy_ref: str | None,
    deploy_ref_chosen: bool,
    dry_run: bool,
    effective_source_repo: Path,
    print_func: Callable[[str], None],
    publication_detail: str,
    publish_remote: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    tooling_root: Path | None,
    trusted_release_root: Path | None,
) -> UpgradeToolingStaged | int:
    """U4 of `upgrade_project_command`, unchanged: role tooling previewed, or staged
    as root from a verified release. `trusted_release_root` and
    `publication_detail` come in as the caller's current values and go back out,
    changed only where the upgrade always changed them."""
    from scripts import team_launcher as launcher

    # Part of the artifacts phase, so it happens on every upgrade including a
    # resumed one. It used to happen only inside the account-creation script,
    # which an upgrade skips once the accounts exist -- leaving the roles on the
    # previous release's hooks and board clients while the board moved on
    # (SYRD-62).
    if dry_run:
        print_func(
            f"switchyard: would stage {config.project} role tooling in "
            f"{launcher._staged_tooling_dir(config, tooling_root)} from {effective_source_repo}"
        )
        launcher.refresh_role_pane_hooks(
            config, staging_root=tooling_root, dry_run=True, runner=runner, print_func=print_func,
        )
        previewed_release, preview_problems = launcher.resolve_trusted_upgrade_release(
            effective_source_repo, deploy_ref or "", dry_run=True, ref_is_pinned=deploy_ref_chosen, runner=runner
        )
        if previewed_release is not None:
            trusted_release_root = previewed_release.root
        for problem in preview_problems or launcher.remove_tenant_publication_boundary(
            config,
            config_path=config_path,
            dry_run=True,
            runner=runner,
            print_func=print_func,
        ):
            # A preview that hides what it could not work out is not a preview.
            # This is the one place an operator finds out what the real run
            # would take away, and finding out then is the whole point of
            # asking.
            print_func(f"warning: switchyard: {problem}")
    elif os.geteuid() == 0:
        legacy_problems = launcher.remove_untrusted_role_account_migration(
            config, config_path=config_path, print_func=print_func
        )
        if legacy_problems:
            # A resume that leaves it behind is not a successful resume: the
            # path this ticket is about would still be there afterwards, still
            # writable by the control role, still named by every older
            # instruction an operator has (SYRD-62).
            for problem in legacy_problems:
                print_func(f"switchyard: {problem}")
            print_func(
                f"switchyard: stopping before any later phase: {config.project} still has a "
                "role-account migration in a directory its control role can write."
            )
            launcher.record_upgrade_phase(
                config, config_path=config_path, phase="artifacts", state="blocked",
                detail="; ".join(legacy_problems),
            )
            return 1
        trusted_release, release_problems = launcher.resolve_trusted_upgrade_release(
            effective_source_repo, deploy_ref or "", ref_is_pinned=deploy_ref_chosen, runner=runner
        )
        if trusted_release is not None:
            release_problems = launcher.stale_launcher_problems(
                trusted_release,
                source_repo=effective_source_repo,
                project=config.project,
                publish_remote=publish_remote,
            )
            if release_problems:
                trusted_release = None
        if trusted_release is None:
            # Staging is the thing that must not proceed. `switchyard-publish-ref`
            # is copied into a root-owned path that a NOPASSWD rule points root
            # at, so staging it from a checkout every role can write is the
            # escalation. Absent a verified release there is no safe source, and
            # continuing would be worse than stopping (SYRD-97 review).
            for problem in release_problems:
                print_func(f"switchyard: {problem}")
            print_func(
                f"switchyard: stopping before any later phase: {config.project}'s privileged "
                "tooling can only be staged from a verified root-owned release, and none is "
                "available."
            )
            launcher.record_upgrade_phase(
                config, config_path=config_path, phase="artifacts", state="blocked",
                detail="; ".join(release_problems),
            )
            return 1
        trusted_release_root = trusted_release.root
        # Before the first thing is replaced, and only then: a retry after a
        # partial upgrade keeps the note taken when the host was last whole.
        rollback_problems = launcher.record_release_rollback(
            config, release=trusted_release, staging_root=tooling_root, print_func=print_func
        )
        if rollback_problems:
            for problem in rollback_problems:
                print_func(f"switchyard: {problem}")
            print_func(
                f"switchyard: stopping before any later phase: {config.project} would be upgraded "
                "with no recorded way back."
            )
            launcher.record_upgrade_phase(
                config, config_path=config_path, phase="artifacts", state="blocked",
                detail="; ".join(rollback_problems),
            )
            return 1
        staging_problems = launcher.refresh_staged_role_tooling(
            config,
            release_root=trusted_release.root,
            staging_root=tooling_root,
            runner=runner,
            print_func=print_func,
        )
        if staging_problems:
            for problem in staging_problems:
                print_func(f"switchyard: {problem}")
            print_func(
                f"switchyard: stopping before any later phase: {config.project}'s roles would come "
                "back on tooling this release did not stage."
            )
            launcher.record_upgrade_phase(
                config, config_path=config_path, phase="artifacts", state="blocked",
                detail="; ".join(staging_problems),
            )
            return 1
        # After staging, because the hooks it writes point at what staging just
        # put there; and on every upgrade, because an existing tenant otherwise
        # keeps the hook set its account-creation run wrote (SYRD-234).
        hook_problems = launcher.refresh_role_pane_hooks(
            config, staging_root=tooling_root, runner=runner, print_func=print_func,
        )
        if hook_problems:
            for problem in hook_problems:
                print_func(f"switchyard: {problem}")
            print_func(
                f"switchyard: stopping before any later phase: {config.project}'s roles would come "
                "back without the hooks this release stages for them."
            )
            launcher.record_upgrade_phase(
                config, config_path=config_path, phase="artifacts", state="blocked",
                detail="; ".join(hook_problems),
            )
            return 1
        publication_problems = launcher.remove_tenant_publication_boundary(
            config,
            config_path=config_path,
            dry_run=False,
            runner=runner,
            print_func=print_func,
        )
        if publication_problems:
            # Reported, not fatal. What is left behind is a sudo rule to a
            # program that is no longer staged, which grants nothing, so the
            # tenant is not less safe for the removal having been incomplete --
            # but an operator must not be told it is gone when it is not, and
            # the upgrade must not take the board down over it.
            for problem in publication_problems:
                print_func(f"warning: switchyard: {problem}")
            print_func(
                f"warning: switchyard: {config.project}'s publication boundary was NOT fully "
                "removed. The rest of this upgrade continued."
            )
            publication_detail = "publication boundary not removed: " + "; ".join(
                publication_problems
            )
    return UpgradeToolingStaged(
        trusted_release_root=trusted_release_root,
        publication_detail=publication_detail,
    )
