"""`switchyard_new_command`'s phases, each a bounded step the command runs in order.

`switchyard_new_command` in `scripts/team_launcher.py` stays the orchestration:
it keeps its name, signature and defaults, and calls each phase here, at the
phase's old position, by the launcher's own name.

- **P0, choices and confirmation** (`_resolve_new_project_choices`, SYRD-369):
  the one-statement runner normalization, then the project, slug, owner, shell,
  path and roles -- from the design artifact, or asked for -- the agy
  credential source, the desktop policy decided and validated, the
  registration checked, an existing owner confirmed, the new project
  confirmed, the root gate, the guided or supplied role plan, and the artifact,
  design and onboarding paths with the progress stages. Every refusal is the
  command's own `SystemExit`, in the command's order, and nothing is created
  before the root gate. Going on returns a frozen `NewProjectChoices`: the
  twenty-one values the rest of the command reads -- `runner` the plain runner
  every later step uses, `first_run_runner` exactly what the caller passed.
- **P1, host and agent CLI checks** (`_check_new_project_preflight`, SYRD-370):
  the stage begun, the project path checked before anything is created, the
  source checkout and the design artifact's branch resolved, the provisioning
  plan built and prechecked with the plain runner, the agy credential source
  validated when there is one, and -- last, before the first mutation -- every
  role's agent CLI required for the new owner. Every refusal is the command's
  own, in the command's order. Going on returns a frozen `NewProjectPreflight`:
  the source checkout, the plan, the worktree branch and the gate's
  `selected_role_clis`, which replaces P0's.

Every launcher facility a phase uses is read from `scripts/team_launcher.py`
when the phase runs, so a patch there still reaches it. This module never
imports `team_launcher` at its top.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

if TYPE_CHECKING:
    from scripts.team_launcher import ProvisioningStages, _NoRunnerInjected
    from scripts.ticket_board.project_provision import ProjectBoardProvision


@dataclass(frozen=True)
class NewProjectChoices:
    """What P0 hands the rest of `switchyard_new_command`, by the command's own
    local names. A refusal raises instead."""

    agy_source_origin: str
    artifact_path: Path
    design_document: Path
    director_onboarding: Path
    first_run_runner: Callable[..., subprocess.CompletedProcess[Any]] | _NoRunnerInjected
    include_audit: bool
    include_designer: bool
    owner_shell: str
    owner_user: str
    project_dir: Path
    resolved_agy_credential_source: str
    resolved_project_name: str
    resolved_slug: str
    runner: Callable[..., subprocess.CompletedProcess[Any]]
    selected_audit_roles: tuple[str, ...]
    selected_desktop_policy: dict
    selected_implementer_roles: tuple[str, ...]
    selected_role_clis: tuple[tuple[str, str], ...]
    selected_role_efforts: dict[str, str]
    selected_role_models: dict[str, str]
    stages: ProvisioningStages


def _resolve_new_project_choices(
    *,
    slug: str | None,
    agent_name: str | None,
    project_name: str | None,
    project_path: Path | None,
    from_artifact: Path | None,
    role_clis: Sequence[tuple[str, str]] | None,
    yes: bool,
    desktop_policy: Path | None,
    headless: bool,
    desktop_gui_user: str | None,
    desktop_approval_settings_path: Path | None,
    allow_existing_owner_user: bool,
    agy_credential_source: str | None,
    no_agy_credential: bool,
    agy_credential_settings_path: Path | None,
    home_base: Path,
    euid_getter: Callable[[], int],
    runner: Callable[..., subprocess.CompletedProcess[Any]] | _NoRunnerInjected,
    config_dir: Path | None,
    registry_dir: Path | None,
    input_func: Callable[[str], str],
    print_func: Callable[[str], None],
) -> NewProjectChoices:
    from scripts import team_launcher as launcher

    # One statement, deliberately. Whether anything was injected is what
    # decides if the setup windows are watched, and a capture-then-resolve
    # pair is one editing accident away from capturing the RESOLVED value and
    # silently unwatching every window again -- which is the defect this
    # ticket spent three candidates not fixing (SYRD-221). Everything below
    # wants the plain runner it always had; only the first-run phase wants to
    # know what the caller actually passed.
    first_run_runner, runner = runner, (
        subprocess.run if isinstance(runner, launcher._NoRunnerInjected) else runner
    )
    if from_artifact is not None:
        artifact = launcher.load_project_design_artifact(from_artifact)
        resolved_slug = artifact.project
        resolved_project_name = artifact.project_name
        owner_user = artifact.owner_user
        owner_shell = str(artifact.capability_grants.get("shell") or launcher.PROJECT_DESIGN_DEFAULT_CAPABILITY_GRANTS["shell"])
        project_dir = launcher._resolve_project_path(project_path or artifact.repository)
        selected_role_clis = artifact.role_clis
        selected_implementer_roles = artifact.implementer_roles
        include_designer = artifact.include_designer
        include_audit = artifact.include_audit
        selected_audit_roles = artifact.audit_roles
    else:
        resolved_project_name = (project_name or launcher._prompt_text("Project name", input_func=input_func)).strip()
        if not resolved_project_name:
            raise SystemExit("switchyard: project name cannot be empty")
        default_slug = launcher._slug_from_project_name(resolved_project_name)
        resolved_slug = launcher._validate_project_slug(slug or launcher._prompt_text("Slug", default=default_slug, input_func=input_func))
        raw_agent = agent_name if agent_name is not None else launcher._prompt_text(
            "Agent user",
            default=launcher._agent_owner_user(resolved_slug),
            input_func=input_func,
        )
        owner_user = launcher._owner_user_verbatim(raw_agent)
        owner_shell = str(launcher.PROJECT_DESIGN_DEFAULT_CAPABILITY_GRANTS["shell"])
        default_project_dir = launcher._project_dir(home_base, owner_user, resolved_project_name)
        project_dir = launcher._resolve_project_path(
            project_path
            or launcher._prompt_text("Project path", default=str(default_project_dir), input_func=input_func)
        )
        selected_role_clis = ()
        selected_implementer_roles = ()
        include_designer = True
        include_audit = True
        selected_audit_roles = ("audit",)
    artifact_agy_source = ""
    if from_artifact is not None:
        artifact_agy_source = str(
            artifact.capability_grants.get("agy_credential_source") or ""
        ).strip()
    resolved_agy_credential_source, agy_source_origin = launcher._resolve_agy_credential_source(
        override=agy_credential_source,
        opt_out=no_agy_credential,
        artifact_value=artifact_agy_source,
        home_base=home_base,
        settings_path=agy_credential_settings_path,
        yes=yes,
        input_func=input_func,
        print_func=print_func,
    )
    selected_desktop_policy, desktop_policy_origin = launcher._resolve_desktop_policy(
        desktop_policy=desktop_policy,
        headless=headless,
        gui_user=desktop_gui_user or "",
        project=resolved_slug,
        tenant=owner_user,
        yes=yes,
        input_func=input_func,
        print_func=print_func,
        settings_path=desktop_approval_settings_path,
    )
    from scripts.desktop_access import validate_policy
    # Validated whichever way it arrived. A generated policy is checked by the
    # same rules as one an operator wrote, so there is one description of what
    # a valid grant is rather than a second, kinder one for our own output.
    selected_desktop_policy = validate_policy(selected_desktop_policy, project=resolved_slug, tenant=owner_user)
    launcher._check_switchyard_registration_available(
        slug=resolved_slug,
        name=resolved_project_name,
        config_dir=config_dir,
        registry_dir=registry_dir,
    )
    launcher._confirm_existing_owner_user(
        owner_user,
        allow_existing_owner_user=allow_existing_owner_user,
        input_func=input_func,
        print_func=print_func,
        agy_credential_source=resolved_agy_credential_source,
    )
    launcher._confirm_switchyard_new(
        slug=resolved_slug,
        owner_user=owner_user,
        project_name=resolved_project_name,
        project_dir=project_dir,
        yes=yes,
        input_func=input_func,
        print_func=print_func,
    )
    if euid_getter() != 0:
        raise SystemExit("switchyard: new requires sudo; re-run as `sudo ./switchyard new`")
    selected_role_models: dict[str, str] = {}
    selected_role_efforts: dict[str, str] = {}
    if from_artifact is None:
        if role_clis is not None:
            chosen_pairs: Sequence[tuple[str, str]] = role_clis
        else:
            # One guided path per role -- runtime, then that runtime's models,
            # then the effort levels it actually renders -- instead of four
            # identifiers to recall and a comma-separated line to compose
            # (SYRD-115).
            role_plan = launcher._prompt_switchyard_role_plan(
                runner=runner,
                owner_user=owner_user,
                owner_home=launcher._owner_home_for_auth(owner_user, fallback=home_base / owner_user),
                input_func=input_func,
                print_func=print_func,
            )
            chosen_pairs = [(entry.role, entry.cli) for entry in role_plan]
            selected_role_models = {
                entry.role: entry.model for entry in role_plan if entry.model
            }
            selected_role_efforts = {
                entry.role: entry.effort for entry in role_plan if entry.effort
            }
            launcher.print_role_plan_review(role_plan, print_func=print_func)
        selected_role_clis = launcher._dedupe_role_cli_pairs(chosen_pairs)
        launcher._require_new_project_roles(selected_role_clis)
        selected_implementer_roles = tuple(
            role for role, _cli in selected_role_clis if role not in launcher.NEW_PROJECT_RESERVED_ROLE_NAMES
        )
        if not selected_implementer_roles:
            raise SystemExit("switchyard: at least one implementer role is required")
        include_designer = any(role == "designer" for role, _cli in selected_role_clis)
        include_audit = any(role == "audit" for role, _cli in selected_role_clis)
        selected_audit_roles = ("audit",) if include_audit else ()

    artifact_path = (from_artifact or (launcher._switchyard_dir(project_dir) / f"{resolved_slug}.project.json")).expanduser().resolve(strict=False)
    design_document = project_dir / launcher.SWITCHYARD_DESIGN_FILE_NAME
    director_onboarding = launcher._switchyard_dir(project_dir) / launcher.SWITCHYARD_DIRECTOR_ONBOARDING_FILE_NAME
    stages = launcher.ProvisioningStages(launcher.NEW_PROJECT_STAGES, print_func=print_func)
    return NewProjectChoices(
        agy_source_origin=agy_source_origin,
        artifact_path=artifact_path,
        design_document=design_document,
        director_onboarding=director_onboarding,
        first_run_runner=first_run_runner,
        include_audit=include_audit,
        include_designer=include_designer,
        owner_shell=owner_shell,
        owner_user=owner_user,
        project_dir=project_dir,
        resolved_agy_credential_source=resolved_agy_credential_source,
        resolved_project_name=resolved_project_name,
        resolved_slug=resolved_slug,
        runner=runner,
        selected_audit_roles=selected_audit_roles,
        selected_desktop_policy=selected_desktop_policy,
        selected_implementer_roles=selected_implementer_roles,
        selected_role_clis=selected_role_clis,
        selected_role_efforts=selected_role_efforts,
        selected_role_models=selected_role_models,
        stages=stages,
    )


@dataclass(frozen=True)
class NewProjectPreflight:
    """What P1 hands the rest of `switchyard_new_command`, by the command's own
    local names -- `selected_role_clis` the gate's answer, which replaces P0's.
    A refusal raises instead."""

    effective_source_repo: Path
    precheck_plan: ProjectBoardProvision
    selected_role_clis: Sequence[tuple[str, str]]
    worktree_branch: str


def _check_new_project_preflight(
    *,
    from_artifact: Path | None,
    source_repo: Path | None,
    commit_git_dir: str | None,
    port: int | None,
    database: str | None,
    yes: bool,
    home_base: Path,
    port_in_use: Callable[[int], bool],
    socket_exists: Callable[[Path], bool],
    config_dir: Path | None,
    registry_dir: Path | None,
    input_func: Callable[[str], str],
    print_func: Callable[[str], None],
    agent_cli_policy: str,
    agent_cli_sources: Sequence[str] | None,
    include_audit: bool,
    include_designer: bool,
    owner_user: str,
    project_dir: Path,
    resolved_agy_credential_source: str,
    resolved_project_name: str,
    resolved_slug: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    selected_audit_roles: tuple[str, ...],
    selected_implementer_roles: tuple[str, ...],
    selected_role_clis: tuple[tuple[str, str], ...],
    stages: ProvisioningStages,
) -> NewProjectPreflight:
    from scripts import team_launcher as launcher

    stages.begin("host and agent CLI checks")
    launcher._precheck_project_path_before_mutating(owner_user, project_dir)
    effective_source_repo = (source_repo or launcher._repo_root()).expanduser().resolve(strict=False)
    precheck_artifact = launcher.load_project_design_artifact(from_artifact, expected_project=resolved_slug) if from_artifact else None
    worktree_branch = precheck_artifact.default_branch if precheck_artifact else "main"
    precheck_plan = launcher.build_plan(
        project=resolved_slug,
        project_name=precheck_artifact.project_name if precheck_artifact else resolved_project_name,
        owner_user=owner_user,
        owner_home=home_base / owner_user,
        port=port,
        database=database,
        source_repo=effective_source_repo,
        commit_git_dir=commit_git_dir,
        ticket_prefix=precheck_artifact.ticket_prefix if precheck_artifact else None,
        implementer_roles=precheck_artifact.implementer_roles if precheck_artifact else selected_implementer_roles,
        include_designer=precheck_artifact.include_designer if precheck_artifact else include_designer,
        include_audit=precheck_artifact.include_audit if precheck_artifact else include_audit,
        audit_roles=precheck_artifact.audit_roles if precheck_artifact else selected_audit_roles,
        board_service_traversal=(
            bool(precheck_artifact.capability_grants.get("board_service_traversal", True))
            if precheck_artifact
            else True
        ),
    )
    launcher.precheck_new_project(
        precheck_plan,
        source_repo=effective_source_repo,
        repository=project_dir,
        runner=runner,
        port_in_use=port_in_use,
        socket_exists=socket_exists,
        config_dir=config_dir,
        registry_dir=registry_dir,
        require_owner_user=False,
        require_repository=False,
    )
    if resolved_agy_credential_source:
        launcher._validate_agy_credential_source(resolved_agy_credential_source, owner_user, home_base)
    # BEFORE the first mutation, and that placement is the fix. Everything below
    # this line creates something: the service user, the owner account, the
    # project directory, the provisioning artifacts. A CLI problem discovered
    # after any of them has already stranded a partly built tenant, which is
    # what "declining installation must never strand a partially provisioned
    # tenant" means in practice (SYRD-210).
    selected_role_clis = launcher.require_agent_clis_for_new_tenant(
        selected_role_clis,
        owner_user=owner_user,
        policy=agent_cli_policy,
        sources=launcher._parse_agent_cli_sources(agent_cli_sources),
        interactive=not yes,
        input_func=input_func,
        print_func=print_func,
    )
    return NewProjectPreflight(
        effective_source_repo=effective_source_repo,
        precheck_plan=precheck_plan,
        selected_role_clis=selected_role_clis,
        worktree_branch=worktree_branch,
    )
