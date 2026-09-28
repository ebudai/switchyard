"""`team-launcher new`: provision a project from its design artifact, through the existing phases.

`new_project_command` loads the project's design artifact, resolves the source,
repository, worktree and role choices, prechecks the host, writes the board
plan and launcher artifacts, renders and (with --execute) installs the
privileged artifacts, and records the rollout. `_new_project_artifact_dir`
names the default output directory and `recorded_provisioning_command` wraps
the privileged step in the rollout recorder.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-426), in their original
order. The launcher imports this module and re-exports all three names; its
`main` still dispatches `new` to the launcher's name, and
`scripts/new_project_phases.py` still reads it there. Everything the three read
-- each other included, and the launcher's config and artifact helpers, the
design-artifact loader, the precheck, the plan builder and writers, the
privileged renderer and installer, the desktop and onboarding steps and the
role-account migration -- is read through the launcher at call time, so a suite
that rebinds one there still intercepts it. The definition-time defaults -- the
port and socket probes and `subprocess.run` -- are the objects the launcher
bound: the probes are imported from the module the launcher imports them from.
This module imports `team_launcher` only inside the functions, when they run.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping

from scripts.new_project_precheck import _path_exists, _tcp_port_in_use


def recorded_provisioning_command(project: str, script_name: str) -> str:
    """How an operator runs the provisioning packet so it records itself.

    `script_name` is a path, and callers pass an absolute one: the packet is run
    from a journal, a Polkit transaction or whatever directory the operator was
    in, and none of those is a promise about the cwd (SYRD-149).
    """
    from scripts import team_launcher as launcher

    recorder = launcher._rollout_recorder_path()
    if recorder is None:
        return f"bash {shlex.quote(script_name)}"
    return " ".join(
        [
            "sudo",
            shlex.quote(str(recorder)),
            shlex.quote(project),
            "--label",
            "provisioning",
            "--",
            "bash",
            shlex.quote(script_name),
        ]
    )


def _new_project_artifact_dir(project: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"{project}-team-launcher-new."))


def new_project_command(
    project: str,
    *,
    from_artifact: Path | None = None,
    owner_user: str | None = None,
    owner_home: Path | None = None,
    desktop_policy: Path | None = None,
    port: int | None = None,
    database: str | None = None,
    source_repo: Path | None = None,
    workflow_config: Path | None = None,
    commit_git_dir: str | None = None,
    repository: Path | None = None,
    output_dir: Path | None = None,
    director_onboarding: Path | None = None,
    execute: bool = False,
    dry_run: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    port_in_use: Callable[[int], bool] = _tcp_port_in_use,
    socket_exists: Callable[[Path], bool] = _path_exists,
    require_owner_user: bool | None = None,
    enable_owner_linger: bool = True,
    upstream_report_url: str = "",
    upstream_report_token_file: str = "",
    #: Chosen per role by the selectors, when this came from an interactive
    #: `switchyard new`. A scripted run passes neither and the generated
    #: configuration carries no model or effort, exactly as before (SYRD-115).
    role_models: Mapping[str, str] | None = None,
    role_efforts: Mapping[str, str] | None = None,
    print_func: Callable[[str], None] = print,
) -> int:
    from scripts import team_launcher as launcher

    if execute and dry_run:
        raise SystemExit("team-launcher: --execute and --dry-run are mutually exclusive")
    effective_source_repo = (source_repo or launcher._repo_root()).expanduser().resolve(strict=False)
    design_artifact = launcher.load_project_design_artifact(from_artifact, expected_project=project) if from_artifact else None
    if design_artifact is not None:
        if owner_user is not None or repository is not None:
            raise SystemExit("team-launcher: --from owns owner/repository answers; do not also pass --owner-user or --repository")
        effective_owner = design_artifact.owner_user
        effective_repository = design_artifact.repository.expanduser().resolve(strict=False)
        remote = design_artifact.remote
        default_branch = design_artifact.default_branch
        worktree_policy = design_artifact.worktree_policy
        ticket_prefix = design_artifact.ticket_prefix
        project_name = design_artifact.project_name
        design_document = design_artifact.design_document if design_artifact.include_designer else None
        implementer_roles = design_artifact.implementer_roles
        role_clis = design_artifact.role_clis
        # A checked-in artifact reproduces the whole choice, not just the
        # runtime: an explicit argument still wins, so nothing a caller passes
        # is overridden by the file (SYRD-115).
        role_models = role_models or dict(design_artifact.role_models)
        role_efforts = role_efforts or dict(design_artifact.role_efforts)
        include_designer = design_artifact.include_designer
        include_audit = design_artifact.include_audit
        audit_roles = design_artifact.audit_roles
        board_service_traversal = bool(design_artifact.capability_grants.get("board_service_traversal", True))
    else:
        if repository is None:
            raise SystemExit("team-launcher: new project requires --repository for the project's working checkout")
        effective_owner = (owner_user or launcher._default_new_project_owner(project)).strip()
        effective_repository = repository.expanduser().resolve(strict=False)
        remote = "origin"
        default_branch = "main"
        worktree_policy = "shared"
        ticket_prefix = None
        project_name = project
        design_document = None
        implementer_roles = launcher.DEFAULT_PROJECT_IMPLEMENTER_ROLES
        role_clis = launcher._default_role_cli_pairs(implementer_roles, include_designer=True, include_audit=True)
        include_designer = True
        include_audit = True
        audit_roles = ("audit",)
        board_service_traversal = True
    if worktree_policy not in launcher.WORKTREE_POLICIES:
        raise SystemExit(f"team-launcher: worktree policy must be one of {sorted(launcher.WORKTREE_POLICIES)}")
    plan = launcher.build_plan(
        project=project,
        project_name=project_name,
        workflow=launcher.seed_director_onboarding(
            json.loads(workflow_config.read_text())
            if workflow_config
            else launcher._load_json(from_artifact).get("workflow")
            if from_artifact
            else None,
            effective_repository,
        )[0],
        owner_user=effective_owner,
        owner_home=owner_home,
        # The human at the keyboard, so they can run `switchyard <project>`
        # afterwards without sudo and without becoming the owner account.
        control_user=launcher.resolve_control_user(
            project, invoking_user=launcher.invoking_human(), owner_user=effective_owner
        ),
        port=port,
        database=database,
        source_repo=effective_source_repo,
        # The tenant's own checkout, which is not the release the artifacts are
        # rendered from. Passing the release where this was meant is what made
        # the packet confine nothing (SYRD-156).
        project_repository=effective_repository,
        commit_git_dir=commit_git_dir,
        ticket_prefix=ticket_prefix,
        implementer_roles=implementer_roles,
        board_service_traversal=board_service_traversal,
        include_designer=include_designer,
        include_audit=include_audit,
        audit_roles=audit_roles,
    )
    launcher.precheck_new_project(
        plan,
        source_repo=effective_source_repo,
        repository=effective_repository,
        runner=runner,
        port_in_use=port_in_use,
        socket_exists=socket_exists,
        config_dir=None,
        registry_dir=None,
        require_owner_user=execute if require_owner_user is None else require_owner_user,
    )
    artifact_dir = (output_dir or launcher._new_project_artifact_dir(plan.project)).expanduser().resolve(strict=False)
    if desktop_policy is not None:
        from scripts.desktop_access import validate_policy
        selected = {"mode": "headless"} if str(desktop_policy) == "headless" else launcher._load_json(desktop_policy)
        selected = validate_policy(selected, project=plan.project, tenant=plan.owner_user)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        launcher._write_json_atomic(artifact_dir / "desktop-policy.json", selected)
    # The rollout must hand each role the tree it works in, so record those
    # paths on the plan before the operator artifact is rendered (SYRD-39).
    _role_worktree_base = launcher._new_project_worktree_base(plan.project, plan.owner_user)
    plan = replace(
        plan,
        role_worktrees=tuple(
            (role, str(_role_worktree_base / role)) for role, _account in plan.role_accounts
        ),
    )
    launcher.write_artifacts(plan, artifact_dir, enable_owner_linger=enable_owner_linger)
    if execute and os.geteuid() == 0:
        # The provision directory is handed to the tenant, so root publishes its
        # own copy of everything it later installs or executes -- rendered here
        # from the plan root just computed, never copied back out of the tenant's
        # directory -- and installs from that instead (SYRD-39).
        rendered_privileged = launcher.render_privileged_artifacts(plan, enable_owner_linger=enable_owner_linger)
        try:
            mirrored_privileged = launcher.install_privileged_artifacts(plan, rendered_privileged)
        except OSError as exc:
            mirrored_privileged = None
            print_func(
                f"switchyard: could not stage {plan.project} privileged artifacts for root: {exc}. "
                f"Run `switchyard upgrade {plan.project}` as root before installing its units."
            )
        if mirrored_privileged is not None:
            print_func(
                f"switchyard: root installs {plan.project} units, grants and SQL from {mirrored_privileged}"
            )
    config_path = launcher.write_new_project_launcher_artifacts(
        plan,
        artifact_dir,
        repository=effective_repository,
        project_name=project_name,
        artifact_path=from_artifact,
        design_document=design_document,
        director_onboarding=director_onboarding,
        implementer_roles=implementer_roles,
        role_clis=role_clis,
        role_models=role_models,
        role_efforts=role_efforts,
        include_designer=include_designer,
        include_audit=include_audit,
        audit_roles=audit_roles,
        remote=remote,
        default_branch=default_branch,
        worktree_policy=worktree_policy,
        upstream_report_url=upstream_report_url.strip(),
        upstream_report_token_file=upstream_report_token_file.strip(),
        print_func=print_func,
    )
    commands_path = artifact_dir / "operator-commands.sh"
    # The complete role-isolation handoff -- accounts, ownership, runtime,
    # tooling AND credential seeding -- is written beside the other artifacts, so
    # an operator who follows the printed instruction once has everything. It
    # used to be produced only by a later failed start, which meant doing exactly
    # what provisioning said still left the credential gaps open (SYRD-39).
    try:
        handoff_config = launcher.load_project_config(plan.project, config_path)
    except SystemExit:
        handoff_config = None
    if handoff_config is not None and launcher.role_isolation_gaps(handoff_config):
        handoff_path, handoff_problems = launcher.publish_role_account_migration(
            handoff_config, config_path=config_path, print_func=print_func
        )
        if handoff_path is not None:
            print_func(
                f"team-launcher: roles are not isolated yet; run {handoff_path} as an operator "
                "(safe to re-run) before starting them"
            )
        else:
            print_func(
                "team-launcher: roles are not isolated yet, and the migration script was not "
                "published where root can run it: " + "; ".join(handoff_problems)
            )
    if not execute:
        print_func(f"team-launcher: dry-run for {plan.project}; artifacts in {artifact_dir}")
        print_func(f"team-launcher: launcher config {config_path}")
        print_func("team-launcher: execution plan:")
        print_func("  sudo -v")
        # By absolute path, and with no `cd` in front of it. The packet resolves
        # the artifacts beside it from its own location, so the directory an
        # operator happens to be in is not part of the instruction -- and an
        # instruction that told them to change directory first is what taught
        # everyone the packet needed one (SYRD-149).
        print_func(f"  {launcher.recorded_provisioning_command(plan.project, str(commands_path))}")
        print_func(
            f"team-launcher: that leaves a root-owned record of the run; read it with "
            f"`switchyard rollout-log {plan.project}` (SYRD-128)"
        )
        print_func("")
        print_func(commands_path.read_text(encoding="utf-8").rstrip("\n"))
        return 0
    print_func(f"team-launcher: provisioning {plan.project}; artifacts in {artifact_dir}")
    sudo_result = runner(["sudo", "-v"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if sudo_result.returncode != 0:
        stderr = str(getattr(sudo_result, "stderr", "") or "").strip()
        detail = f": {stderr}" if stderr else ""
        raise SystemExit(f"team-launcher: sudo authentication failed{detail}")
    # Recorded when the recorder is reachable, plain otherwise: provisioning a
    # project must not depend on the journal, but when it can be recorded it
    # should be, because this is the run whose evidence matters most and the
    # one an operator is least likely to still have a terminal for (SYRD-128).
    recorder = launcher._rollout_recorder_path()
    if recorder is not None:
        result = runner(
            ["sudo", str(recorder), plan.project, "--label", "provisioning",
             "--", "bash", str(commands_path)],
        )
        print_func(
            f"team-launcher: the run is recorded; read it with "
            f"`switchyard rollout-log {plan.project}`"
        )
    else:
        result = runner(["bash", str(commands_path)])
    if result.returncode != 0:
        raise SystemExit(f"team-launcher: provisioning failed with exit status {result.returncode}")
    config = launcher.load_project_config(plan.project, config_path)
    if config.desktop_access is not None:
        launcher.configure_project_desktop(config, config_path=config_path,
            helper=effective_source_repo / "scripts/desktop_access.py", runner=runner)
    from scripts.workflow_launcher import assign_projection_owner
    assign_projection_owner(config, config_path, runner=runner)
    print_func(f"team-launcher: provisioned {plan.project}; launcher config {config_path}")
    return 0
