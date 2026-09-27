"""Resuming a partly provisioned project: its checkout, its registration, and the command.

- `plan_with_tenant_checkout` records where a tenant's checkout is, from where
  its verified configuration is -- structural rather than declared (SYRD-156).
- `_finish_provision_after_packet` registers the verified configuration,
  installs recovered desktop access, starts the roles through the ordinary
  launcher path and reports only when readiness is read back, each phase
  skipped when already done (SYRD-155, SYRD-158).
- `switchyard_resume_provision_command` rebuilds root's artifacts from root's
  own record, checked against the kernel and a verified release, hands back the
  operator packet, and continues past it (SYRD-147, SYRD-155).

Moved out of `scripts/team_launcher.py` unchanged (SYRD-366). The launcher
imports this module at its top and re-exports every name, so `switchyard_main`,
`runtime_artifact_refresh` and every suite that reaches them through the
launcher reach the same objects. Every launcher facility these use, and every
name defined here that another definition here reads, is read from
`team_launcher` when it runs, as it was -- the launcher's own `__file__`
included, so the default launcher script is still found beside the launcher
that is running. The registration timeout default is the
`provider_runtime_state` object the launcher imports; the standard-library
names are this module's own imports, the same objects. This module never
imports `team_launcher` at its top.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

from scripts.provider_runtime_state import RUNTIME_REGISTRATION_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from scripts.packet_completion import PacketCompletion
    from scripts.pane_liveness_checks import PaneLiveness
    from scripts.provider_runtime_state import RuntimeRegistrationWait
    from scripts.session_records import LaunchSessionRecordStatus
    from scripts.team_launcher import ProjectBoardProvision, ProjectConfig


def plan_with_tenant_checkout(
    plan: ProjectBoardProvision, *, config_path: Path | None
) -> ProjectBoardProvision:
    """Record where this tenant's checkout is, from where its configuration is.

    Structural rather than declared: the generated configuration lives at
    `<checkout>/.switchyard/provision/<slug>.json`, so the checkout is the
    directory that contains it. A path read out of the configuration's own
    fields would be a path the account every role runs as can choose, and this
    one decides which directories root re-modes and re-owns (SYRD-156).
    """
    from scripts import team_launcher as launcher

    if config_path is None:
        return plan
    checkout = launcher._project_dir_from_generated_config_path(config_path)
    if checkout is None:
        return plan
    recorded = str(checkout)
    if plan.project_repository == recorded:
        return plan
    return replace(plan, project_repository=recorded)


def _finish_provision_after_packet(
    slug: str,
    plan: "ProjectBoardProvision",
    *,
    registry_dir: Path | None,
    config_dir: Path | None,
    config_path: Path | None,
    completion: PacketCompletion,
    source_release: Path | None = None,
    desktop_approval_path: Path | None = None,
    desktop_installer: Callable[..., ProjectConfig] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    launcher_script: Path | None = None,
    start_roles: bool = True,
    process_commands: Sequence[str] | None = None,
    pane_liveness_states: "Sequence[PaneLiveness] | None" = None,
    session_statuses: Sequence[LaunchSessionRecordStatus] | None = None,
    registration: RuntimeRegistrationWait | None = None,
    runtime_wait_seconds: float = RUNTIME_REGISTRATION_TIMEOUT_SECONDS,
    print_func: Callable[[str], None] = print,
) -> int:
    """Register the project and start its roles, and say what is still missing.

    The phases run in this order because each depends on the last, and each is
    skipped when the world already shows it done: a project registered by an
    interrupted earlier run is not registered again, and roles already running
    are attached to rather than started twice. That is what makes running this
    command again the supported retry rather than a second recovery.
    """
    from scripts import team_launcher as launcher

    owner_uid = launcher.uid_for_user(plan.owner_user)
    verified, config, problems = launcher.verified_tenant_config(
        plan, slug, explicit=config_path, owner_uid=owner_uid
    )
    if config is None or verified is None:
        for objection in problems:
            print_func(f"switchyard: {objection}")
        print_func(
            f"switchyard: refusing to register {slug} from a configuration root has not "
            f"verified. Nothing was changed. If its checkout is not where it was generated, "
            f"name the configuration: `sudo switchyard resume-provision {slug} --config <path>`."
        )
        return 1
    launcher.record_tenant_config_path(slug, verified)

    registry_path = (registry_dir or launcher.switchyard_registry_dir()) / f"{slug}.json"
    if registry_path.exists():
        entry = launcher._load_json(registry_path)
        registered_at = str((entry or {}).get("config_path") or "")
        if registered_at != str(verified):
            print_func(
                f"switchyard: {slug} is already registered at {registry_path}, pointing at "
                f"{registered_at!r} rather than the configuration root verified ({verified}). "
                "Which of those is this project is not this command's to decide; nothing was "
                "changed."
            )
            return 1
        print_func(f"switchyard: {slug} is already registered at {registry_path}")
    else:
        try:
            registry_path = launcher._register_switchyard_project(
                verified, config_dir=config_dir, registry_dir=registry_dir
            )
        except SystemExit as exc:
            print_func(f"switchyard: {exc}")
            return 1
        print_func(f"switchyard: registered {slug} at {registry_path} from {verified}")

    if start_roles:
        # Before the launch, because the launch only ever verifies. The
        # interrupted `switchyard new` never reached the install, so a recovery
        # that went straight to launching asked the tenant to prove access that
        # had never been granted (SYRD-158).
        configured, ready = launcher.install_recovered_desktop_access(
            plan,
            config,
            verified,
            source_release=source_release,
            approval_path=desktop_approval_path,
            installer=desktop_installer,
            runner=runner,
            print_func=print_func,
        )
        if not ready or configured is None:
            print_func(
                f"switchyard: {slug} is registered, but its roles were not started: the "
                "desktop access they need is not installed. Address what is named above and "
                f"run `sudo switchyard resume-provision {slug}` again -- the phases already "
                "done are not repeated."
            )
            return 1
        config = configured
        launched = launcher.launch_project(
            config,
            config_path=verified,
            mode="start",
            script_path=launcher_script or Path(launcher.__file__).resolve().with_name(launcher.TEAM_LAUNCHER_NAME),
            runner=runner,
            layout_output=launcher._owner_state_layout_output_path(slug, owner_home=Path(plan.owner_home)),
            assign_layout_owner=True,
            print_func=print_func,
        )
        if launched != 0:
            print_func(
                f"switchyard: {slug} is registered, but starting its roles did not succeed. "
                f"Fix what the launcher named above and run `sudo switchyard resume-provision "
                f"{slug}` again -- the phases already done are not repeated."
            )
            return 1

    remaining = launcher.recovery_readiness_problems(
        plan,
        config,
        verified,
        registry_path=registry_path,
        runner=runner,
        process_commands=process_commands,
        pane_liveness_states=pane_liveness_states,
        session_statuses=session_statuses,
        completion=completion,
        registration=registration,
        runtime_wait_seconds=runtime_wait_seconds,
        print_func=print_func,
    )
    if remaining:
        for objection in remaining:
            print_func(f"switchyard: {slug} is not finished: {objection}")
        print_func(
            f"switchyard: run `sudo switchyard resume-provision {slug}` again once that is "
            "addressed -- resuming is the supported retry, and it continues from wherever "
            "this stopped."
        )
        return 1
    print_func(
        f"switchyard: {slug} is registered at {registry_path}, its board and listener are "
        f"running, and all {len(config.roles)} configured role(s) have live sessions "
        "registered with the board."
    )
    return 0


def switchyard_resume_provision_command(
    slug: str,
    *,
    source_repo: Path | None = None,
    registry_dir: Path | None = None,
    config_dir: Path | None = None,
    config_path: Path | None = None,
    enable_owner_linger: bool = True,
    euid_getter: Callable[[], int] = os.geteuid,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    launcher_script: Path | None = None,
    start_roles: bool = True,
    completion_reader: Callable[["ProjectBoardProvision"], PacketCompletion] | None = None,
    desktop_approval_path: Path | None = None,
    desktop_installer: Callable[..., ProjectConfig] | None = None,
    process_commands: Sequence[str] | None = None,
    pane_liveness_states: "Sequence[PaneLiveness] | None" = None,
    session_statuses: Sequence[LaunchSessionRecordStatus] | None = None,
    registration: RuntimeRegistrationWait | None = None,
    runtime_wait_seconds: float = RUNTIME_REGISTRATION_TIMEOUT_SECONDS,
    print_func: Callable[[str], None] = print,
) -> int:
    """Rebuild a partly provisioned project's root artifacts so it can finish.

    A `switchyard new` that fails before it registers the project leaves a real
    installation nobody can name: the account, its repository, its credentials,
    its desktop policy, its journal and its exported release all exist, and
    every supported command answers `unknown project`. The recovery cannot be
    to run the packet that failed -- it was rendered by the release that had
    the defect -- nor to register a tenant nobody has checked.

    So this reads root's own record, checks it against the kernel, rebuilds
    every artifact root installs from the release named here, and hands back
    the ordinary operator packet to run. It changes nothing the tenant owns and
    it is re-runnable: the phases themselves are idempotent and the packet is
    regenerated rather than patched (SYRD-147).

    Running that packet is not the end of a `switchyard new`, though, and a
    recovery that stopped there left a project with a live board that no
    ordinary command could name and no role sessions at all. So this continues
    past the packet: it reads whether the packet actually finished, registers
    the generated configuration once root has checked it against its own
    record, and starts the configured roles through the ordinary launcher
    path. It reports success only when all of that is true, so an interrupted
    recovery is finished by running this again (SYRD-155).
    """
    from scripts import team_launcher as launcher

    slug = launcher._validate_project_slug(slug)
    registry = (registry_dir or launcher.switchyard_registry_dir()) / f"{slug}.json"
    baseline = launcher.privileged_baseline_plan_path(slug)
    if launcher.partial_provision_record(slug) is None:
        if registry.exists():
            print_func(
                f"switchyard: {slug} is registered at {registry} and root holds no provisioning "
                f"record to resume from; use `switchyard upgrade {slug}` instead. Nothing was "
                "changed."
            )
            return 1
        print_func(
            f"switchyard: root holds no provisioning record for {slug} at {baseline}, so there "
            "is nothing to resume. A project that was never provisioned is started with "
            "`sudo switchyard new`."
        )
        return 1
    # A registry entry no longer ends this. A recovery that registered the
    # project and was then interrupted before its roles started is exactly the
    # state this command has to be able to continue from, and refusing it here
    # would leave running the remaining phases to nobody (SYRD-155).
    already_registered = registry.exists()
    # Before anything that can only be evaluated as root. Every check below
    # walks a path that must belong to root, and asking an unprivileged caller
    # to read those answers produces a refusal about uids rather than the one
    # thing they need to do differently.
    if euid_getter() != 0:
        print_func(
            f"switchyard: resuming {slug} reads root's own provisioning record and rewrites "
            f"root's artifacts. Run: sudo switchyard resume-provision {slug}"
            + (f" --source-repo {source_repo}" if source_repo is not None else "")
        )
        return 1
    document, problem = launcher.read_plan_no_follow(baseline, require_root_owned=True)
    if document is None:
        print_func(f"switchyard: {problem}")
        print_func(
            f"switchyard: root holds no usable provisioning record for {slug}, so there is "
            "nothing to resume from. Nothing was changed."
        )
        return 1
    recorded_project = str(document.data.get("project") or "").strip()
    if recorded_project != slug:
        print_func(
            f"switchyard: {baseline} records project {recorded_project!r}, not {slug!r}. "
            "Which one it belongs to is not this command's to decide; nothing was changed."
        )
        return 1

    identity = launcher.trusted_owner_identity(slug)
    if not identity.trusted:
        for objection in identity.problems:
            print_func(f"switchyard: {objection}")
        print_func(
            f"switchyard: refusing to resume {slug}: root cannot establish whose installation "
            "this is. Nothing was changed."
        )
        return 1

    selected, release_problem = launcher._resume_source_release(source_repo)
    if release_problem:
        print_func(f"switchyard: {release_problem}")
        print_func(f"switchyard: refusing to resume {slug} from an unverified release. Nothing was changed.")
        return 1

    plan, divergence = launcher._resume_plan_from_record(document, identity, source_repo=selected)
    if divergence:
        for objection in divergence:
            print_func(f"switchyard: {objection}")
        print_func(
            f"switchyard: refusing to resume {slug}: rebuilding it would change what root "
            "installs. Nothing was changed."
        )
        return 1

    # Where this tenant's checkout is, before anything is rendered from this
    # plan: the packet closes that tree, and a packet rendered without it
    # confines nothing. Taken from the location of the generated configuration
    # root has verified, so it is structural rather than declared. A tenant
    # whose configuration cannot be verified yet simply has no checkout
    # recorded, and the packet says so (SYRD-156).
    verified_config, _verified, _config_problems = launcher.verified_tenant_config(
        plan, slug, explicit=config_path, owner_uid=launcher.uid_for_user(plan.owner_user)
    )
    if verified_config is not None:
        plan = launcher.plan_with_tenant_checkout(plan, config_path=verified_config)

    recorded_release = str(document.data.get("source_repo") or "").strip()
    installed = baseline.parent
    if already_registered:
        # Rebuilding what root installs for a project that is already running is
        # `switchyard upgrade`, and doing it here would make a retry of the
        # remaining phases into a silent artifact change.
        print_func(
            f"switchyard: {slug} is registered at {registry}; its root-owned artifacts are left "
            f"as they are (`switchyard upgrade {slug}` refreshes them)."
        )
        # Leaving the artifacts alone is not leaving the directory open. What is
        # in them is unchanged; who can read them is not a rebuild (SYRD-176).
        for repair in launcher.ensure_privileged_provision_dir(installed):
            print_func(f"switchyard: {repair}")
    else:
        rendered = launcher.render_privileged_artifacts(plan, enable_owner_linger=enable_owner_linger)
        installed = launcher.install_privileged_artifacts(plan, rendered)
        if recorded_release and recorded_release != str(selected):
            print_func(
                f"switchyard: {slug} was provisioned from {recorded_release}; its artifacts are "
                f"rebuilt from {selected}"
            )
        print_func(f"switchyard: regenerated {slug}'s root-owned artifacts in {installed}")

    completion = (
        completion_reader(plan)
        if completion_reader is not None
        else launcher.privileged_packet_completion(plan, runner=runner)
    )
    if not completion.done:
        for objection in completion.problems:
            print_func(f"switchyard: {slug} has not finished its privileged packet: {objection}")
        print_func(
            f"switchyard: run {installed}/operator-commands.sh through the ordinary operator "
            "path to finish those phases. Every phase in it is re-runnable, so the work already "
            "done is left alone."
        )
        print_func(
            f"switchyard: then run `sudo switchyard resume-provision {slug}` again -- it "
            "continues from there, registers the project and starts its roles."
        )
        return 1

    return launcher._finish_provision_after_packet(
        slug,
        plan,
        registry_dir=registry_dir,
        config_dir=config_dir,
        config_path=config_path,
        completion=completion,
        source_release=selected,
        desktop_approval_path=desktop_approval_path,
        desktop_installer=desktop_installer,
        runner=runner,
        launcher_script=launcher_script,
        start_roles=start_roles,
        process_commands=process_commands,
        pane_liveness_states=pane_liveness_states,
        session_statuses=session_statuses,
        registration=registration,
        runtime_wait_seconds=runtime_wait_seconds,
        print_func=print_func,
    )
