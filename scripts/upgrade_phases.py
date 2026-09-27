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
- **U5, identity and accounts** (`_upgrade_identities_and_accounts`, SYRD-347):
  the owner's GitHub identity kept or reported, pending identities and the
  artifacts verdict, then the role accounts -- the migration published or
  named -- and the identities transaction. A migration root cannot vouch for
  answers 1, a failed transaction its own code; going on returns a frozen
  `UpgradeIdentitiesDone` with `config` and `release_report_config`, reloaded
  after a real transaction and otherwise the caller's own.

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
    from scripts.role_identity_cutover import RoleAccountCutover
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


@dataclass(frozen=True)
class UpgradeIdentitiesDone:
    """What U5 hands the rest of `upgrade_project_command` when the upgrade goes
    on, in the order U5 assigns them. A refusal gets its code instead."""

    config: ProjectConfig
    release_report_config: ProjectConfig


def _upgrade_identities_and_accounts(
    config: ProjectConfig,
    *,
    commit_git_dir: str | None,
    config_path: Path,
    cutover: RoleAccountCutover,
    deploy_ref: str | None,
    desktop_choice: Any,
    dry_run: bool,
    effective_source_repo: Path,
    print_func: Callable[[str], None],
    publication_detail: str,
    publish_remote: str,
    release_report_config: ProjectConfig,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    source_repo: Path | None,
    tooling_root: Path | None,
    trusted_release_root: Path | None,
) -> UpgradeIdentitiesDone | int:
    """U5 of `upgrade_project_command`, unchanged: the owner's GitHub identity,
    pending identities and the artifacts verdict, then the role accounts and the
    identities transaction. `config` and `release_report_config` come in as the
    caller's current values and go back out, changed only where the upgrade
    always changed them: after a real identities transaction."""
    from scripts import team_launcher as launcher

    # The owner's GitHub identity, on every upgrade as well as at provisioning:
    # the account this found had a key and no configuration selecting it, and an
    # existing tenant never re-runs the operator script. Idempotent, and it
    # reads no private material (SYRD-74).
    owner_home_for_identity = launcher._tenant_owner_home(config, config_path)
    owner_for_identity = config.run_as_user or launcher.current_user_name()
    from scripts.ticket_board.project_provision import (
        owner_github_identity_commands,
        owner_github_key_path,
        resolve_owner_github_identity,
    )

    # Which key this tenant publishes with, before anything is rendered from it.
    # The renderer defaults to `id_ed25519` when it is not told, and being not
    # told is how a live upgrade generated that key, pointed the managed block
    # at it, and left the tenant unable to push with the deploy key it had been
    # using for weeks (SYRD-100).
    plan_data = launcher._plan_data_from_config(config, config_path)
    # Only a tenant that publishes to GitHub has a GitHub identity to manage.
    # mefp publishes to a local bare repository, and this ran anyway: it warned
    # that no GitHub key was selected and sent the operator to
    # set-owner-identity, which then wrote a github.com block onto a local-only
    # tenant and reported an authentication failure against a forge it never
    # uses (SYRD-229). The remote is root's, never the tenant's git config.
    from scripts.ticket_board.project_provision import publication_uses_github
    from scripts.ticket_board.publication_boundary import resolve_pinned_remote

    effective_remote, _remote_problem = resolve_pinned_remote(
        config.project,
        registration_root=launcher.switchyard_privileged_provision_root(),
        declared_remote=publish_remote,
    )
    github_applies = publication_uses_github(
        effective_remote,
        recorded_host_alias=str(plan_data.get("owner_github_host_alias") or ""),
    )
    selected_identity = resolve_owner_github_identity(
        str(owner_home_for_identity),
        recorded_key_name=str(plan_data.get("owner_github_key_name") or ""),
        recorded_host_alias=str(plan_data.get("owner_github_host_alias") or ""),
    )
    if github_applies is False:
        print_func(
            f"switchyard: {config.project} publishes to {effective_remote}, not GitHub, so no "
            "owner GitHub identity is selected, configured or checked"
        )
        if str(plan_data.get("owner_github_key_name") or "") or str(
            plan_data.get("owner_github_host_alias") or ""
        ):
            print_func(
                f"switchyard: {config.project}'s plan still records a GitHub identity it does not "
                f"use; clear it with `sudo switchyard set-owner-identity {config.project} --clear` "
                "(try --dry-run first)"
            )
    elif not selected_identity.resolved:
        # Nothing is rendered, nothing is generated, and the managed block is
        # left exactly as it is. Choosing among the owner's keys, or making a
        # new one beside them, is the substitution this must not perform.
        for problem in selected_identity.problems:
            print_func(f"warning: switchyard: {problem}")
        print_func(
            f"warning: switchyard: {config.project}'s owner GitHub identity was left untouched, "
            "so publication continues with whatever is already configured."
        )
    elif dry_run:
        print_func(
            f"switchyard: would keep {owner_for_identity}'s GitHub identity on "
            f"{owner_github_key_path(str(owner_home_for_identity), key_name=selected_identity.key_name)}, "
            f"from {selected_identity.source}"
        )
    if github_applies is not False and selected_identity.resolved and not dry_run and os.geteuid() == 0:
        identity_script = "set -eu\n" + "\n".join(
            owner_github_identity_commands(
                owner_for_identity,
                str(owner_home_for_identity),
                key_name=selected_identity.key_name,
                host=selected_identity.host,
                host_alias=selected_identity.host_alias,
                comment=f"{owner_for_identity} switchyard {config.project}",
            )
        )
        applied = runner(
            ["sh", "-c", identity_script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        if getattr(applied, "returncode", 1) != 0:
            print_func(
                f"switchyard: could not provision {owner_for_identity}'s GitHub identity "
                f"(exit {applied.returncode}): "
                f"{(str(getattr(applied, 'stderr', '') or '').strip() or 'no output')[:300]}"
            )
    if github_applies is not False and selected_identity.resolved and not dry_run:
        # Read back against the same key the block selects. Checking the default
        # while the block names another is a readiness answer about a key nobody
        # publishes with.
        identity = launcher.github_identity_status(
            owner_for_identity,
            owner_home_for_identity,
            key_name=selected_identity.key_name,
            host=selected_identity.host,
            runner=runner,
        )
        remedy = launcher.github_identity_remedy(identity, project=config.project)
        if remedy:
            print_func(remedy)
        else:
            print_func(
                f"switchyard: {owner_for_identity} can publish to GitHub as its own identity "
                f"({selected_identity.key_name}, from {selected_identity.source})"
            )
    if not dry_run:
        # Preparation needs the accounts before the active configuration names
        # them, and it must not read that list from the tenant (SYRD-45).
        launcher.write_pending_identities(config)
    # The phase's own verdict, recorded once and last. Recording the publication
    # failure and then unconditionally recording "done" over it left the journal
    # claiming a phase completed cleanly when part of it had not run at all
    # (SYRD-97 review).
    launcher.record_upgrade_phase(
        config,
        config_path=config_path,
        phase="artifacts",
        state="incomplete" if publication_detail else "done",
        detail=publication_detail,
        dry_run=dry_run,
    )

    accounts_ready = launcher._role_accounts_ready(config)
    if not accounts_ready:
        migration_path: Path | None = None
        migration_problems: list[str] = []
        if dry_run:
            migration_path = launcher.trusted_role_account_migration_path(config)
        elif os.geteuid() == 0:
            migration_path, migration_problems = launcher.publish_role_account_migration(
                config,
                config_path=config_path,
                resume_source=launcher.read_upgrade_source(config),
                runner=runner,
                print_func=print_func,
            )
            if migration_path is None:
                # Naming it is telling an operator to run it as root, so root
                # publishing one it cannot vouch for stops here rather than
                # handing it over (SYRD-62).
                for problem in migration_problems:
                    print_func(f"switchyard: {problem}")
                print_func(
                    f"switchyard: {config.project}'s roles still share the project account, and its "
                    "role-account migration is not an artifact root can vouch for, so it is not "
                    "being handed to an operator to run."
                )
                launcher.record_upgrade_phase(
                    config, config_path=config_path, phase="accounts", state="blocked",
                    detail="; ".join(migration_problems),
                )
                return 1
        else:
            # Unprivileged: this run regenerates the tenant's own artifacts and
            # reports. Publishing root's copy is root's, so it names the one
            # that is already published or the command that publishes it, and
            # writes nothing itself (SYRD-62).
            migration_path, migration_problems = launcher.role_account_migration_instruction(
                config, runner=runner
            )
        if migration_path is not None:
            next_step = f"run {migration_path} (safe to re-run)"
        else:
            next_step = (
                f"run `sudo switchyard upgrade {config.project}`, which publishes the role-account "
                "migration where only root can write it"
                + (f": {'; '.join(migration_problems)}" if migration_problems else "")
            )
        print_func(
            f"switchyard: {config.project}'s roles still share the project account. An operator must "
            f"{next_step}, then rerun `switchyard upgrade {config.project}`, "
            "which runs whichever phase is next in order."
        )
        launcher.record_upgrade_phase(
            config, config_path=config_path, phase="accounts", state="pending", dry_run=dry_run
        )
    else:
        launcher.record_upgrade_phase(
            config, config_path=config_path, phase="accounts", state="done", dry_run=dry_run
        )
        if not cutover.is_complete:
            # The whole transaction: workers, trees, configuration, installed
            # units, services, and a real write from each role. Nothing about
            # the director gates it -- the director's own write comes after,
            # from an identity this creates (SYRD-45).
            cutover_result = launcher.cutover_role_identities_command(
                config,
                config_path=config_path,
                dry_run=dry_run,
                runner=runner,
                # The verified release, not the checkout. The transaction
                # restarts every role against the staged bundle and checks that
                # bundle against this source, and the bundle now comes out of
                # the release rather than out of a tree the project account can
                # write. Pinning it here is the same repair, one layer up: the
                # check and the thing being checked have to name one release
                # (SYRD-97 review).
                source_repo=(
                    trusted_release_root
                    if trusted_release_root is not None
                    else (effective_source_repo if source_repo is not None else None)
                ),
                commit_git_dir=commit_git_dir,
                deploy_ref=deploy_ref,
                tooling_dir=launcher._staged_tooling_dir(config, tooling_root),
                print_func=print_func,
            )
            if not dry_run:
                config = launcher.load_project_config(config.project, config_path)
                release_report_config = config
                cutover = launcher.role_account_cutover(config, runner=runner)
            if cutover_result != 0:
                # The transaction has already said why it stopped and what the
                # rollback brought back, so the only thing left is to stop here
                # and say so. Everything after this point -- the director phase,
                # the release, the "the remaining step is the director's" line --
                # describes an upgrade still moving forward. Reporting those
                # after a rolled-back identities phase is what let a retry read
                # as progress, and left the failure detectable only by an
                # unrelated assertion about the board build much later
                # (SYRD-64).
                print_func(
                    f"switchyard: stopping: {config.project}'s identities phase did not complete, "
                    "so no later phase ran and none is being reported. What the transaction said "
                    "above -- what it refused to start, or what the rollback brought back -- is "
                    "the state this tenant is in."
                )
                for line in launcher.upgrade_phase_report(
                    config,
                    config_path=config_path,
                    cutover=cutover,
                    journal=launcher.read_upgrade_journal(config, config_path=config_path, trusted=True),
                    desktop_policy=desktop_choice,
                    dry_run=dry_run,
                ):
                    print_func(line)
                return cutover_result
        else:
            launcher.record_upgrade_phase(
                config, config_path=config_path, phase="identities", state="done", dry_run=dry_run
            )
    return UpgradeIdentitiesDone(
        config=config,
        release_report_config=release_report_config,
    )
