"""The director's half of a tenant upgrade: `switchyard finish-upgrade`, run unprivileged.

`finish_upgrade_command` refuses root, reports the release the privileged
phases were pinned to (or, unpinned and unnamed, the release root staged for
the roles), checks that the calling account is the one the board authorizes
for the control role, and then -- or, on a dry run, only previews -- installs a
handed-off workflow, migrates the director's onboarding and records the
observed state, and reports the release: withheld while the per-role accounts
or the owner's release root are not ready, and blocked releases said and
answered 1.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-352). The launcher
imports this module at its top and re-exports the command, so `switchyard
finish-upgrade` dispatches to the same object. Every launcher facility the
command uses -- the pin resolver and staged-release fallback, the control-role
and account checks, `_finish_upgrade_preview`, the workflow, onboarding,
journal, cutover and release helpers -- is read from `team_launcher` when it
runs, because suites patch them there. `os` and `subprocess` are this module's
own imports, the same module objects. This module never imports
`team_launcher` at its top.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig


def finish_upgrade_command(
    config: ProjectConfig,
    *,
    config_path: Path,
    dry_run: bool = False,
    source_repo: Path | None = None,
    commit_git_dir: str | None = None,
    deploy_ref: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> int:
    """The director-owned phase of a tenant upgrade, run unprivileged.

    The board decides authority from the peer's uid, so a write only the
    director may make has to come from the director's own process. Root
    reports this phase; the director performs it (SYRD-45).
    """
    from scripts import team_launcher as launcher

    if os.geteuid() == 0:
        print_func(
            "switchyard: finish-upgrade makes a director-authority board write and must not run as "
            "root. Run it from the director's own session."
        )
        return 1
    # The director's phase is another handoff: it runs later, from a different
    # session, with none of the outer command's arguments. It reports the same
    # release the privileged phases were pinned to rather than resolving one of
    # its own (SYRD-61).
    named = source_repo is not None or deploy_ref is not None
    source_repo, commit_git_dir, deploy_ref, pinned = launcher.resolve_pinned_upgrade_source(
        config, source_repo=source_repo, commit_git_dir=commit_git_dir, deploy_ref=deploy_ref
    )
    if pinned:
        print_func(f"switchyard: reporting the release {config.project} was pinned to: {pinned}")
    elif not named:
        # Root's record is in a directory only root can read, so from here it
        # is usually not there at all. Never `origin/main` in its place without
        # saying so: the operator pinned and deployed a release, and resolving a
        # branch nobody asked for is what failed live (SYRD-255).
        unavailable = launcher.upgrade_source_unavailable_reason(config)
        release, commit, staged_problem = launcher.director_readable_pinned_release(config.project)
        if release is not None:
            source_repo, deploy_ref = release, commit
            print_func(
                f"switchyard: {unavailable}; reporting the release root staged for "
                f"{config.project}'s roles instead: {commit} ({release})"
            )
        else:
            print_func(
                f"switchyard: no pinned release is readable for {config.project}: {unavailable}, "
                f"and {staged_problem}. Resolving {deploy_ref} instead; pass --deploy-ref <commit> "
                "--source-repo <installed release> to report a specific release."
            )
    # Bound to the configured account, not to anything the caller says about
    # itself. The board decides the same question from the peer uid; this is so
    # the wrong account gets an answer instead of a rejected write (SYRD-49).
    director, control_reason = launcher.control_role_name(config, config_path=config_path)
    caller = launcher.current_user_name()
    owner = (config.run_as_user or "").strip()
    if not director:
        print_func(
            f"switchyard: cannot establish which role controls {config.project}: {control_reason}. "
            "Refusing rather than guessing which account may make this write."
        )
        return 1
    if director:
        role = launcher._role_by_name(config, director)
        account = (role.run_as_user if role is not None else "") or ""
        if account and caller not in {account, owner}:
            print_func(
                f"switchyard: finish-upgrade makes {config.project}'s director-authority board "
                f"write, which the board authorizes for {account}. This process is {caller}."
            )
            return 1
    if dry_run:
        return launcher._finish_upgrade_preview(
            config,
            config_path=config_path,
            director=director,
            source_repo=source_repo,
            commit_git_dir=commit_git_dir,
            deploy_ref=deploy_ref,
            runner=runner,
            print_func=print_func,
        )
    # A legacy board runs no workflow at all, so there is nothing for the
    # onboarding migration below to migrate. Root may have handed over the one
    # it reviewed; installing it is this command's job, because only the
    # director's process can make that write (SYRD-253).
    installed = launcher.install_handed_off_workflow(
        config, config_path=config_path, caller_role=director, print_func=print_func
    )
    if installed is False:
        return 1
    if installed:
        config = launcher.load_project_config(config.project, config_path)
    launcher.migrate_declarative_director_onboarding(config, config_path=config_path, print_func=print_func)
    config = launcher.load_project_config(config.project, config_path)
    # Completion is what the board and the projection carry, not what the
    # migration call returned: `False` is also what a board too old to accept
    # the document returns, and recording that as done is what would release
    # the activation early (SYRD-45).
    state, reason = launcher.director_onboarding_state(config, config_path=config_path)
    launcher.record_upgrade_phase(
        config,
        config_path=config_path,
        phase="director",
        state={"done": "done", "not required": "not required"}.get(state, "pending"),
        detail=(reason or f"completed by {launcher.current_user_name()}"),
    )
    if state not in {"done", "not required"}:
        print_func(
            f"switchyard: {config.project}'s director migration has not landed: {reason}. The "
            "release activation stays withheld."
        )
        return 1
    cutover = launcher.role_account_cutover(config, runner=runner)
    if not cutover.is_complete:
        print_func(
            f"switchyard: {config.project}'s per-role accounts are not in place yet, so the release "
            "deploy instruction is still withheld."
        )
        return 0
    # Never runs as root, so it cannot repair a legacy release root -- but it
    # must not print a deploy sequence the owner cannot run (SYRD-231).
    root_problems = launcher.owner_release_root_problems(config)
    if root_problems:
        for problem in root_problems:
            print_func(f"switchyard: {problem}")
        print_func(
            f"switchyard: withholding {config.project}'s release deploy sequence until that is "
            "repaired. Nothing was deployed, and no listener needs stopping."
        )
        return 1
    release_status = launcher.report_tenant_release_upgrade(
        config,
        config_path=config_path,
        source_repo=(source_repo or launcher._repo_root()).expanduser().resolve(strict=False),
        commit_git_dir=commit_git_dir,
        deploy_ref=deploy_ref,
        runner=runner,
        print_func=print_func,
    )
    # An observation, not a closure. This command cannot write root's journal --
    # it refuses to run as root by design -- so the phase it used to record went
    # only into the tenant copy, and that copy became the sole record claiming a
    # deployment had finished. It now says what it saw and says who can close
    # the phase (SYRD-117).
    launcher.record_release_phase_from_status(config, config_path=config_path, status=release_status)
    for line in launcher.director_release_divergence_report(config, config_path=config_path):
        print_func(line)
    blocked = launcher.release_update_blocked(release_status)
    if blocked:
        # The same rule as the privileged phase, which this command is the other
        # half of. Saying the release cannot be produced and exiting 0 is what
        # let a wrapper report the upgrade complete over a board that had not
        # moved; applying it to one of the two commands fixed half of that
        # (SYRD-100 review).
        print_func(
            f"switchyard: {config.project}'s release phase did not complete: {blocked}. "
            "Nothing after it is claimed."
        )
        if installed:
            # Partial, and said so: the workflow write above landed and stays.
            print_func(
                f"switchyard: {config.project}'s handed-off workflow is installed and stays "
                "installed; only the release phase is outstanding."
            )
        return 1
    return 0
