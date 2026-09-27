"""`upgrade_project_command`'s phases, each a bounded step the upgrade runs in order.

`upgrade_project_command` in `scripts/team_launcher.py` stays the orchestration:
it keeps its name, signature and defaults, and calls each phase here, at the
phase's old position, by the launcher's own name.

- **U1, desktop decision and source pinning** (`_pin_upgrade_source`, SYRD-351):
  the desktop policy decided or refused, the source pin resolved -- explicit,
  or recovered and refused when stale -- the non-root pin warning, the
  publication remote recorded, then the explicit pin made durable, the remote
  put back when it cannot be. Three refusals answer 1 and a stale recovered pin
  its own code; going on returns a frozen `UpgradeSourcePinned`.
- **U2, manager, desktop and repatriation** (`_recover_upgrade_state`, SYRD-350):
  the source resolved, the owner's user manager recovered, the desktop
  configured with its presentation and display bridge, interrupted role state
  restored and repatriated, and the cutover read afresh -- a partial one
  reported and, with live roles, reverted. Five refusals answer 1; going on
  returns a frozen `UpgradeStateReady`.
- **U3, generated artifacts and the upstream report** (`_refresh_upgrade_artifacts`,
  SYRD-349): the stale-source warning, the generated layout (reloading the
  configuration when it changed), runtime artifacts, onboarding documents, the
  board skill and repository hooks, then the upstream report link and
  credential and the registered agent CLIs, whose problems are reported and not
  fatal. It answers the configuration the rest of the upgrade uses.
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
- **U6, finish** (`_finish_upgrade`, SYRD-348): the director phase, a fresh
  cutover, the release root and the release deployed and recorded, the director
  instruction, the phase report and what is still outstanding, unsafe windows,
  then the upgrade's own exit code -- 1 when the release is blocked, else 0 --
  which `upgrade_project_command` returns.

Every launcher facility a phase uses is read from `scripts/team_launcher.py`
when the phase runs, so a patch there still reaches it. This module never
imports `team_launcher` at its top.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, replace
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


def _finish_upgrade(
    config: ProjectConfig,
    *,
    commit_git_dir: str | None,
    config_path: Path,
    deploy_ref: str | None,
    desktop_choice: Any,
    dry_run: bool,
    effective_source_repo: Path,
    print_func: Callable[[str], None],
    release_report_config: ProjectConfig,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> int:
    """U6 of `upgrade_project_command`, unchanged: the director phase, the release
    phase and its report, and the upgrade's closing report; it answers the
    upgrade's own exit code."""
    from scripts import team_launcher as launcher

    # Asked after the cutover, because the director makes that write from the
    # identity the cutover gives it.
    director_state, director_reason = launcher.director_onboarding_state(config, config_path=config_path)
    launcher.record_upgrade_phase(
        config, config_path=config_path, phase="director",
        state={"done": "done", "not required": "not required"}.get(director_state, "pending"),
        detail=director_reason, dry_run=dry_run,
    )

    final_cutover = launcher.role_account_cutover(config, runner=runner)
    release_deployed = False
    release_blocked = ""
    release_root_problems: list[str] = []
    if not final_cutover.is_complete:
        print_func(
            f"switchyard: withholding the {config.project} release deploy instruction until its "
            "legacy role state is repatriated to the project account: the release enforces "
            "process-bound authority and must not strand a resumable pane."
        )
    else:
        release_root_problems = (
            launcher.prepare_tenant_release_root(config, dry_run=dry_run, print_func=print_func)
            if os.geteuid() == 0
            else launcher.owner_release_root_problems(config)
        )
    if final_cutover.is_complete and release_root_problems:
        # Before any deploy sequence is printed and before the phase is called
        # ready: live mefp was told `ready`, had its listener stopped, and then
        # the deploy could not create its release (SYRD-231).
        for problem in release_root_problems:
            print_func(f"switchyard: {problem}")
        print_func(
            f"switchyard: withholding {config.project}'s release deploy sequence: its owner "
            "cannot publish a release under the board root yet. Nothing was deployed, and no "
            "listener needs stopping."
        )
        launcher.record_upgrade_phase(
            config, config_path=config_path, phase="release", state="blocked",
            detail="; ".join(release_root_problems), dry_run=dry_run,
        )
        release_blocked = "; ".join(release_root_problems)
    elif final_cutover.is_complete:
        release_status = launcher.report_tenant_release_upgrade(
            release_report_config,
            config_path=config_path,
            source_repo=effective_source_repo,
            commit_git_dir=commit_git_dir,
            deploy_ref=deploy_ref,
            runner=runner,
            print_func=print_func,
        )
        release_deployed = launcher.record_release_phase_from_status(
            config,
            config_path=config_path,
            status=release_status,
            dry_run=dry_run,
        )
        release_blocked = launcher.release_update_blocked(release_status)

    if director_state in {"pending", "unknown"}:
        director_action = (
            f"the director runs `switchyard finish-upgrade {config.project}` from their own "
            f"session ({director_reason or 'outstanding'}); root cannot make that write and "
            "will not pretend to"
        )
        if release_deployed:
            # Saying "after that deploy" here is what sent an operator looking for a
            # deploy the transaction had already made (SYRD-48).
            print_func(
                f"switchyard: {config.project}'s board release is deployed and no further deploy "
                f"is needed. The remaining step is the director's: {director_action}."
            )
        else:
            when = (
                "after that deploy"
                if final_cutover.is_complete
                else "once its roles are on their own accounts and the release is deployed"
            )
            print_func(f"switchyard: {when}, {director_action}.")
    trusted_journal = launcher.read_upgrade_journal(config, config_path=config_path, trusted=True)
    for line in launcher.upgrade_phase_report(
        config,
        config_path=config_path,
        cutover=final_cutover,
        journal=trusted_journal,
        desktop_policy=desktop_choice,
        dry_run=dry_run,
    ):
        print_func(line)
    # What exit 0 means, said before the operator reads it as "done". Preparing
    # artifacts and deploying the board are different things, and an upgrade
    # that returns 0 having only done the first has to say which one it did and
    # name the exact command that does the other (SYRD-117).
    if not dry_run:
        for line in launcher.outstanding_release_phase_report(
            config, config_path=config_path, journal=trusted_journal
        ):
            print_func(line)
    unsafe_windows = launcher.unsafe_root_presentation_windows(config, config_path=config_path)
    if unsafe_windows:
        print_func(launcher.unsafe_presentation_report(config, unsafe_windows))
    if release_blocked:
        # The release this upgrade was asked to deploy could not be. Saying so
        # and exiting 0 is worse than either on its own: every wrapper that reads
        # the status reported the upgrade complete over a board that had not
        # moved, and the operator had to read the transcript to find out
        # otherwise (SYRD-100 review).
        print_func(
            f"switchyard: {config.project}'s release phase did not complete: {release_blocked}. "
            "Nothing after it is claimed."
        )
        return 1
    return 0


def _refresh_upgrade_artifacts(
    config: ProjectConfig,
    *,
    commit_git_dir: str | None,
    config_path: Path,
    dry_run: bool,
    effective_source_repo: Path,
    print_func: Callable[[str], None],
    registry_dir: Path | None,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    source_repo: Path | None,
    upstream_report_token_file: str,
    upstream_report_url: str,
) -> ProjectConfig:
    """U3 of `upgrade_project_command`, unchanged: the generated layout, runtime
    artifacts, onboarding documents, board skill and repository hooks refreshed,
    and the upstream report link, credential and agent CLIs recorded. It answers
    the configuration the rest of the upgrade uses -- reloaded when the layout
    changed, and as the report link recorded it."""
    from scripts import team_launcher as launcher

    launcher.warn_if_artifact_source_checkout_is_stale(
        config, source_repo=effective_source_repo, runner=runner, print_func=print_func,
    )
    result = launcher.upgrade_generated_project_layout(config, config_path=config_path, dry_run=dry_run, runner=runner)
    print_func(result.message)
    if result.changed and not dry_run:
        config = launcher.load_project_config(config.project, config_path)
    runtime_artifacts = launcher.refresh_generated_project_runtime_artifacts(
        config,
        config_path=config_path,
        dry_run=dry_run,
        source_repo=effective_source_repo if source_repo is not None else None,
        commit_git_dir=commit_git_dir,
        runner=runner,
    )
    print_func(runtime_artifacts.message)
    project_dir = launcher._project_dir_from_generated_config_path(config_path)
    if project_dir is not None:
        onboarding_commit_git_dir = commit_git_dir
        if onboarding_commit_git_dir is None:
            plan_commit_git_dir = launcher._plan_data_from_config(config, config_path).get("commit_git_dir")
            if isinstance(plan_commit_git_dir, str) and plan_commit_git_dir.strip():
                onboarding_commit_git_dir = plan_commit_git_dir.strip()
        launcher.upgrade_switchyard_onboarding_docs(
            source_repo=effective_source_repo,
            project_dir=project_dir,
            owner_user=config.run_as_user or launcher.current_user_name(),
            commit_git_dir=onboarding_commit_git_dir,
            dry_run=dry_run,
            runner=runner,
            print_func=print_func,
        )
    launcher.ensure_generated_project_board_skill(
        config,
        config_path=config_path,
        script_path=effective_source_repo / "scripts" / "team-launcher",
        source_repo=effective_source_repo,
        dry_run=dry_run,
        runner=runner,
        print_func=print_func,
    )
    launcher.repair_repository_policy_hooks(
        config_path, source_repo=effective_source_repo, dry_run=dry_run, print_func=print_func,
    )
    # Before the phases, because a tenant that cannot file a report is how this
    # host finds out anything is wrong with it at all (SYRD-238).
    config, report_link_problems = launcher.record_upstream_report_link(
        config,
        config_path=config_path,
        upstream_report_url=upstream_report_url,
        upstream_report_token_file=upstream_report_token_file,
        dry_run=dry_run,
        print_func=print_func,
    )
    report_problems = report_link_problems + launcher.refresh_upstream_report_credential(
        config, dry_run=dry_run, registry_dir=registry_dir, print_func=print_func,
    )
    # A tenant registered before its CLI selection was recorded cannot be
    # offered a promotion at launch without guessing, and guessing is what
    # asked the `test` tenant about a Hermes no role of its uses (SYRD-220).
    report_problems += launcher.refresh_registered_agent_clis(
        config, registry_dir=registry_dir, dry_run=dry_run, print_func=print_func,
    )
    if report_problems:
        for problem in report_problems:
            print_func(f"switchyard: {problem}")
        print_func(
            f"switchyard: {config.project} keeps the report credential it had; nothing else "
            "about this upgrade depends on it."
        )
    return config


@dataclass(frozen=True)
class UpgradeStateReady:
    """What U2 hands the rest of `upgrade_project_command` when the upgrade goes
    on, in the order U2 assigns them. A refusal gets its code instead."""

    source_repo: Path | None
    effective_source_repo: Path
    config: ProjectConfig
    cutover: RoleAccountCutover


def _recover_upgrade_state(
    config: ProjectConfig,
    *,
    config_path: Path,
    deploy_ref: str | None,
    desktop_policy: Path | None,
    dry_run: bool,
    print_func: Callable[[str], None],
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    source_repo: Path | None,
) -> UpgradeStateReady | int:
    """U2 of `upgrade_project_command`, unchanged: the source resolved, the
    owner's user manager recovered, the desktop configured with its presentation
    and bridge, interrupted role state restored and repatriated, and the cutover
    read afresh -- a partial one reported and, with live roles, reverted.
    `source_repo` and `config` go back out as the caller's own unless this phase
    changed them."""
    from scripts import team_launcher as launcher

    # Resolved, so every phase after this one works on the tree the operator was
    # looking at rather than on whatever a moved symlink comes to mean.
    if source_repo is not None:
        source_repo = Path(launcher.resolved_source_selection(source_repo))
    effective_source_repo = (source_repo or launcher._repo_root()).expanduser().resolve(strict=False)

    # Before anything else, because everything else depends on it. The identities
    # transaction stops the roles and then talks to this manager; against a wedged
    # one it would hang there indefinitely, with the roles down. Recovering it is
    # part of the ordered upgrade rather than a command an operator has to know
    # about (SYRD-54).
    manager_state, manager_detail = launcher.owner_user_manager_state(
        config, runner=runner, config_path=config_path
    )
    if manager_state == launcher.MANAGER_WEDGED:
        print_func(
            f"switchyard: {config.run_as_user or launcher.current_user_name()}'s user manager is not "
            f"answering ({manager_detail}); its units, including this tenant's notify listener, "
            "cannot be started or stopped until it is recovered."
        )
        problems = launcher.repair_owner_user_manager(
            config, runner=runner, config_path=config_path, dry_run=dry_run, print_func=print_func
        )
        if problems:
            for problem in problems:
                print_func(f"switchyard: {problem}")
            print_func(
                f"switchyard: stopping before any phase runs: moving {config.project} onto per-role "
                "identities would stop its roles and then wait on that manager."
            )
            return 1

    if desktop_policy is not None or config.desktop_access is not None:
        config = launcher.configure_project_desktop(config, config_path=config_path, policy_path=desktop_policy,
            dry_run=dry_run, helper=effective_source_repo / "scripts/desktop_access.py", runner=runner)
        # Once the desktop account is known, and before any phase can declare the
        # tenant ready to restart: a legacy tenant presenting to a different
        # account needs the presentation section new tenants are born with, or
        # its workers start and its window cannot read its own layout (SYRD-233).
        # A dry run's policy was not persisted, so it is carried in by hand.
        planned = config
        if dry_run and desktop_policy is not None:
            from scripts import desktop_access as _desktop

            raw_policy = (
                {"mode": "headless"} if str(desktop_policy) == "headless" else launcher._load_json(desktop_policy)
            )
            planned = replace(config, desktop_access=_desktop.validate_policy(
                raw_policy, project=config.project, tenant=config.run_as_user or launcher.current_user_name()
            ))
        planned, presentation_ready = launcher.migrate_legacy_presentation(
            planned, config_path=config_path, dry_run=dry_run, print_func=print_func
        )
        if not presentation_ready:
            return 1
        # Every upgrade, not only the one that adds the section: a tenant moved
        # onto the desktop-account window by an earlier run can still lack the
        # bridge its tabs cross, which is the state live mefp was left in
        # (SYRD-233). Asked only when the window will cross accounts.
        pinned = launcher.pinned_presentation_gui_user(planned)
        if pinned and (
            launcher.presentation_controller_enabled(planned, config_path=config_path)
            or launcher.legacy_presentation_migration(planned, config_path=config_path).needed
        ):
            if not launcher.ensure_display_bridge(
                planned, gui_user=pinned, dry_run=dry_run, runner=runner, print_func=print_func,
                root_check=launcher._privileged_upgrade_check_command(config.project, deploy_ref),
            ):
                return 1
        if not dry_run:
            config = planned

    # Every tenant, desktop or not: a role whose own state directory is not the
    # tenant account's cannot record what its runtime was started against, and
    # every launch would then end that role's live pane over an answer it could
    # not keep (SYRD-233).
    if not launcher.restore_interrupted_role_state(
        config, dry_run=dry_run, runner=runner, print_func=print_func
    ):
        return 1

    repatriated, repatriation_problems = launcher.repatriate_role_runtime_state(
        config, config_path=config_path, dry_run=dry_run, runner=runner
    )
    if repatriation_problems:
        print_func(
            f"switchyard: refusing {config.project}'s project-account migration:\n  "
            + "\n  ".join(repatriation_problems)
        )
        print_func(
            "switchyard: no account, worktree, installed unit, or release was changed; "
            "resume after every named role is safely checkpointed"
        )
        return 1
    if repatriated:
        if dry_run:
            print_func(
                f"switchyard: would repatriate {config.project}'s resumable role state and "
                "remove dedicated-account bindings"
            )
        else:
            print_func(
                f"switchyard: repatriated {config.project}'s resumable role state to "
                f"{config.run_as_user or launcher.current_user_name()}; dedicated accounts were left intact"
            )
            config = launcher.load_project_config(config.project, config_path)

    # Detect the partial state before doing anything else, so every later phase
    # reads a configuration that matches the host.
    cutover = launcher.role_account_cutover(config, runner=runner)
    # Probed under both identities: a configuration naming accounts that do not
    # exist makes every probe through them fail, and the live tenant then looks
    # like a fresh one (SYRD-45).
    serving = launcher.running_role_identities(config, runner=runner)
    live_roles = sorted(serving)
    if cutover.is_partial:
        print_func(
            f"switchyard: {config.project} is part-way onto per-role accounts and the two do not "
            "agree:\n  " + "\n  ".join(
                cutover.problems)
        )
        # Reverting is for a tenant with sessions to protect. A freshly
        # provisioned project declares its accounts before the operator creates
        # them and has nothing running, so there is nothing to preserve and
        # nothing to undo -- it simply waits for the accounts (SYRD-45).
        reverted, message = (
            launcher.revert_incomplete_role_account_cutover(config, config_path=config_path, dry_run=dry_run)
            if live_roles
            else (False, "")
        )
        if not live_roles:
            print_func(
                f"switchyard: no {config.project} role session is running, so the configuration is "
                "left as provisioned and waits for the accounts."
            )
        if message:
            print_func(message)
        launcher.record_upgrade_phase(
            config, config_path=config_path, phase="identities", state="reverted",
            detail="; ".join(cutover.missing_accounts), dry_run=dry_run,
        )
        if reverted and not dry_run:
            config = launcher.load_project_config(config.project, config_path)
            cutover = launcher.role_account_cutover(config, runner=runner)
    return UpgradeStateReady(
        source_repo=source_repo,
        effective_source_repo=effective_source_repo,
        config=config,
        cutover=cutover,
    )


@dataclass(frozen=True)
class UpgradeSourcePinned:
    """What U1 hands the rest of `upgrade_project_command` when the upgrade goes
    on, in the order U1 assigns them. A refusal gets its code instead."""

    desktop_choice: dict[str, Any] | None
    deploy_ref_chosen: bool
    source_repo: Path | None
    commit_git_dir: str | None
    deploy_ref: str


def _pin_upgrade_source(
    config: ProjectConfig,
    *,
    commit_git_dir: str | None,
    deploy_ref: str | None,
    desktop_policy: Path | None,
    dry_run: bool,
    print_func: Callable[[str], None],
    publish_remote: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    source_repo: Path | None,
    tooling_root: Path | None,
) -> UpgradeSourcePinned | int:
    """U1 of `upgrade_project_command`, unchanged: the desktop policy decided or
    refused, the source pin resolved -- explicit or recovered, a stale recovered
    pin refused -- the non-root warning, then the publication remote and the
    durable source recorded, the remote put back when the source cannot be kept.
    The pin, cache and ref go back out as this phase resolved them."""
    from scripts import team_launcher as launcher

    # First, before anything is recorded or repaired and before any phase.
    # Every role launch needs a desktop policy, and a legacy tenant can predate
    # them: live on mefp the upgrade ran to completion, reported nothing left to
    # do, and the pane restart it was followed by suspended a working tenant and
    # then refused to start it. Some phases restart roles themselves, so finding
    # out later would strand them down mid-upgrade. Asked here, the answer costs
    # nothing and changes nothing (SYRD-232).
    desktop_choice, desktop_problems = launcher.upgrade_desktop_policy_decision(config, desktop_policy)
    if desktop_problems:
        for problem in desktop_problems:
            print_func(f"switchyard: {problem}")
        print_func(
            f"switchyard: {'this dry run shows the upgrade would ' if dry_run else ''}"
            f"{'stop' if dry_run else 'stopping'} before any phase, so {config.project} is not "
            "declared ready and no pane restart should follow. Nothing was changed: no "
            "artifact, account, release, access grant, board, listener or role session."
        )
        return 1
    # Before the source is used for anything, because everything the later
    # phases deploy is decided by it. An operator who pinned a release on the
    # outer command gets the same release in every phase that follows, whether
    # the next one is reached by this process or by the rerun the accounts phase
    # asks for; an operator who pinned nothing here is filled in from what was
    # pinned last time (SYRD-61).
    pinned_explicitly = (
        source_repo is not None or commit_git_dir is not None or deploy_ref is not None
    )
    # Whether the deploy ref is somebody's choice or a default the resolver
    # supplies. Only a choice may contradict an installed release's marker.
    deploy_ref_chosen = deploy_ref is not None
    # Read back first, then record what this invocation actually ends up using.
    # Doing it the other way round would let an operator who pins one of the
    # three erase the other two, and the phase after theirs would then be the
    # one guessing.
    source_repo, commit_git_dir, deploy_ref, recovered = launcher.resolve_pinned_upgrade_source(
        config, source_repo=source_repo, commit_git_dir=commit_git_dir, deploy_ref=deploy_ref
    )
    deploy_ref_chosen = deploy_ref_chosen or bool(recovered)
    if recovered:
        print_func(
            f"switchyard: {config.project} keeps the release this upgrade was pinned to: {recovered}"
        )
    if recovered and not pinned_explicitly:
        refused = launcher._recovered_pin_behind_host(
            config,
            source_repo=source_repo,
            deploy_ref=deploy_ref,
            dry_run=dry_run,
            tooling_root=tooling_root,
            runner=runner,
            print_func=print_func,
        )
        if refused is not None:
            return refused
    if pinned_explicitly and not dry_run and os.geteuid() != 0:
        print_func(
            f"switchyard: this upgrade is not root, so {config.project}'s pinned release is not "
            "recorded. The generated continuation will say so, and the privileged rerun has to "
            "carry --source-repo, --commit-git-dir and --deploy-ref itself."
        )
    # Where this tenant publishes, recorded before anything else is written:
    # everything after it decides by this remote, and a pin that cannot be kept
    # has to stop the upgrade while nothing has changed yet. A warning printed
    # after the phases, with the run carrying on and exiting 0, is what the
    # first version of this did (SYRD-229 review).
    pin_changed, pin_previous = False, ""
    if publish_remote.strip():
        pin_changed, pin_previous, pin_problems = launcher.record_publication_remote(
            config.project, publish_remote, dry_run=dry_run, print_func=print_func
        )
        if pin_problems:
            for problem in pin_problems:
                print_func(f"switchyard: {problem}")
            print_func(
                f"switchyard: refusing to upgrade {config.project}: its publication remote could "
                "not be recorded, and everything after this decides by it. Nothing was changed."
            )
            return 1
    if pinned_explicitly:
        durability = launcher.record_upgrade_source(
            config,
            source_repo=source_repo,
            commit_git_dir=commit_git_dir,
            deploy_ref=deploy_ref,
            dry_run=dry_run,
        )
        if durability:
            # Before any phase, so nothing is regenerated, no phase is recorded
            # as safely resumable, and above all no continuation is advertised:
            # a handoff that cannot carry the pin is the incident this ticket is
            # about, and accepting the pin anyway would schedule it (SYRD-61).
            for problem in durability:
                print_func(f"switchyard: {problem}")
            if pin_changed:
                # So that "Nothing was changed" below stays true.
                restored = launcher.restore_publication_remote(config.project, pin_previous)
                if restored:
                    print_func(f"switchyard: {restored}")
            print_func(
                f"switchyard: refusing to upgrade {config.project} with a release it cannot keep. "
                "Its accounts phase hands the upgrade back through sudo, which carries neither "
                "arguments nor environment, so a pin that is not durable is one the next phase "
                "would have to guess at. Nothing was changed."
            )
            return 1
    return UpgradeSourcePinned(
        desktop_choice=desktop_choice,
        deploy_ref_chosen=deploy_ref_chosen,
        source_repo=source_repo,
        commit_git_dir=commit_git_dir,
        deploy_ref=deploy_ref,
    )
