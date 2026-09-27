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
- **P2, project accounts and files** (`_prepare_new_project_accounts`, SYRD-371):
  the stage begun, the board service user and its peer authentication, the
  owner account and project directory, the owner's agent CLIs verified once the
  account exists, the account reported, the agy credential left, refused or
  seeded, then -- fresh or from the design artifact -- the onboarding files,
  the initial artifact, ownership, the onboarding documents and the project's
  git repository, and last the provisioning directory with the desktop policy.
  Every refusal and every partial step is the command's own, in the command's
  order. Going on returns a frozen `NewProjectAccounts` with `provision_dir`.
- **P3, database and board** (`_prepare_new_project_board`, SYRD-372): the
  stage begun, the project provisioned -- its database, board, service and
  generated configuration -- and, when that answers anything but 0, that very
  status returned at once, before anything else runs; `switchyard_new_command`
  returns it unchanged. Otherwise the provisioning artifacts committed, the
  configuration loaded and its desktop prepared, the project registered and the
  first-run worktrees prepared. Going on returns a frozen `NewProjectBoard`
  with `config` and `config_path`.
- **P4, provider sign-in and folder trust** (`_run_new_project_sign_in`,
  SYRD-373): the stage begun, the first-run sign-in and folder trust for the
  owner -- watched when nobody injected a runner -- then three stops, each the
  command's own 1: an owner missing a role's CLI, a provider not signed in, and
  (after the owner confirms a model the list now shows) a model still unknown.
  Otherwise the models reported as not probed, the launch runner built, and a
  launch deferred -- with its role-account handoff published -- while the roles
  are not isolated yet. Going on returns a frozen `NewProjectSignIn` with
  `config` and the report as the confirmation left them, `launch_deferred` and
  `launch_runner`.
- **P5, role panes** (`_launch_new_project_panes`, SYRD-374): the command's
  tail. The stage begun and the staged role tooling ensured -- a problem there
  is reported and the command's 1 returned before any clock or window -- then
  the clocks, the launch (unless it is deferred), a failed launch's own status
  returned, the layout resolved, the presentation announced and the session
  records awaited only for a launch that happened, the sign-in warnings, the
  designer instruction, and the stages finished with 0. The launcher script it
  names is the launcher's own file (`launcher.__file__`), read only when a launch
  actually happens. `switchyard_new_command` returns this phase's answer.

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
    from scripts.team_launcher import FirstRunAuthReport, ProjectConfig, ProvisioningStages, _NoRunnerInjected
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


@dataclass(frozen=True)
class NewProjectAccounts:
    """What P2 hands the rest of `switchyard_new_command`, by the command's own
    local name. A refusal raises instead."""

    provision_dir: Path


def _prepare_new_project_accounts(
    *,
    from_artifact: Path | None,
    output_dir: Path | None,
    home_base: Path,
    git_init: bool,
    print_func: Callable[[str], None],
    agy_source_origin: str,
    artifact_path: Path,
    design_document: Path,
    director_onboarding: Path,
    include_audit: bool,
    include_designer: bool,
    owner_shell: str,
    owner_user: str,
    project_dir: Path,
    resolved_agy_credential_source: str,
    resolved_project_name: str,
    resolved_slug: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    selected_audit_roles: tuple[str, ...],
    selected_desktop_policy: dict,
    selected_implementer_roles: tuple[str, ...],
    selected_role_efforts: dict[str, str],
    selected_role_models: dict[str, str],
    stages: ProvisioningStages,
    effective_source_repo: Path,
    precheck_plan: ProjectBoardProvision,
    selected_role_clis: Sequence[tuple[str, str]],
    worktree_branch: str,
) -> NewProjectAccounts:
    from scripts import team_launcher as launcher

    stages.begin("project accounts and files")
    launcher._ensure_board_service_user(precheck_plan.service_user, runner=runner)
    launcher._ensure_board_service_peer_auth(precheck_plan, source_repo=effective_source_repo, runner=runner)
    owner_result = launcher._ensure_owner_user_and_project_dir(
        owner_user,
        project_dir,
        runner=runner,
        shell=owner_shell,
        owner_home=home_base / owner_user,
    )
    # Only meaningful once the account exists, which is why it is HERE and not
    # beside the precheck. The precheck asks whether this host can serve a
    # tenant; this asks what the tenant it just created actually resolves, and
    # they can differ -- the owner's own PATH is searched first, so an
    # owner-local copy would win at pane launch and nothing else would say so
    # (SYRD-210).
    launcher.verify_agent_clis_for_owner(
        selected_role_clis,
        owner_user=owner_user,
        owner_home=home_base / owner_user,
        runner=runner,
        print_func=print_func,
    )
    if owner_result.created:
        created_shell = owner_result.shell_path or owner_shell
        if owner_shell == launcher.PROJECT_DESIGN_DEFAULT_CAPABILITY_GRANTS["shell"] and Path(created_shell).name != owner_shell:
            print_func(
                f"switchyard: created user {owner_user} with shell {created_shell} "
                f"({owner_shell} unavailable); linger enabled"
            )
        else:
            display_shell = created_shell if "/" in owner_shell else owner_shell
            print_func(f"switchyard: created user {owner_user} with shell {display_shell}; linger enabled")
    else:
        print_func(f"switchyard: using existing user {owner_user} (not modifying)")
    if resolved_agy_credential_source:
        credential_state = launcher._agy_credential_state(owner_user, home_base)
        if credential_state == launcher.AGY_CREDENTIAL_INSTALLED:
            print_func(
                f"switchyard: agy credential already present for {owner_user}; leaving it in place"
            )
        elif credential_state == launcher.AGY_CREDENTIAL_UNUSABLE:
            raise SystemExit(
                f"switchyard: {home_base / owner_user / launcher.AGY_CREDENTIAL_DIR_NAME / launcher.AGY_CREDENTIAL_TOKEN_NAME} "
                f"exists but is not a 0600 regular file owned by {owner_user}, so agy could not "
                "read it; remove it and rerun, or clear capability_grants.agy_credential_source"
            )
        else:
            # Not gated on owner_result.created. Reaching here with a pre-existing owner
            # already required consent to reuse that account, and gating on creation is
            # what made a failed seed unrecoverable: the retry would skip and then let
            # provisioning record a credential source that was never installed.
            launcher._seed_agy_credential_for_owner(
                owner_user=owner_user,
                source_user=resolved_agy_credential_source,
                home_base=home_base,
                runner=runner,
                print_func=print_func,
            )
    if from_artifact is None:
        launcher._write_switchyard_onboarding_files(
            project_name=resolved_project_name,
            slug=resolved_slug,
            owner_user=owner_user,
            project_dir=project_dir,
            artifact_path=artifact_path,
            design_document=design_document,
            director_onboarding=director_onboarding,
            include_designer=include_designer,
        )
        launcher._write_initial_switchyard_project_artifact(
            project_name=resolved_project_name,
            slug=resolved_slug,
            owner_user=owner_user,
            project_dir=project_dir,
            artifact_path=artifact_path,
            design_document=design_document,
            owner_shell=owner_shell,
            implementer_roles=selected_implementer_roles,
            role_clis=selected_role_clis,
            role_models=selected_role_models,
            role_efforts=selected_role_efforts,
            include_designer=include_designer,
            include_audit=include_audit,
            audit_roles=selected_audit_roles,
            agy_credential_source=resolved_agy_credential_source,
            agy_credential_source_origin=agy_source_origin,
        )
        launcher._chown_switchyard_project_files(owner_user=owner_user, project_dir=project_dir, runner=runner)
        if include_designer and design_document.exists():
            launcher._chown_project_file(owner_user=owner_user, path=design_document, runner=runner)
        launcher._install_switchyard_onboarding_docs(
            source_repo=effective_source_repo,
            project_dir=project_dir,
            owner_user=owner_user,
            runner=runner,
            print_func=print_func,
        )
        if git_init:
            created_git_repository = launcher._ensure_project_git_repository(
                owner_user=owner_user,
                project_dir=project_dir,
                branch=worktree_branch,
                runner=runner,
            )
            if created_git_repository:
                print_func(
                    f"switchyard: initialized git repository in {project_dir} "
                    f"on branch {worktree_branch} with an initial commit"
                )
            else:
                print_func(f"switchyard: using existing git repository in {project_dir} without modifying it")
        else:
            print_func(f"switchyard: skipped project git initialization for {project_dir} (--no-git-init)")
            launcher._require_existing_project_git_repository(
                owner_user=owner_user,
                project_dir=project_dir,
                runner=runner,
            )
    else:
        artifact = launcher.load_project_design_artifact(artifact_path)
        launcher._write_switchyard_onboarding_files(
            project_name=resolved_project_name,
            slug=resolved_slug,
            owner_user=owner_user,
            project_dir=project_dir,
            artifact_path=artifact_path,
            design_document=artifact.design_document,
            director_onboarding=director_onboarding,
            include_designer=artifact.include_designer,
        )
        launcher._chown_switchyard_project_files(owner_user=owner_user, project_dir=project_dir, runner=runner)
        launcher._install_switchyard_onboarding_docs(
            source_repo=effective_source_repo,
            project_dir=project_dir,
            owner_user=owner_user,
            runner=runner,
            print_func=print_func,
        )
        if git_init:
            created_git_repository = launcher._ensure_project_git_repository(
                owner_user=owner_user,
                project_dir=project_dir,
                branch=worktree_branch,
                runner=runner,
            )
            if created_git_repository:
                print_func(
                    f"switchyard: initialized git repository in {project_dir} "
                    f"on branch {worktree_branch} with an initial commit"
                )
            else:
                print_func(f"switchyard: using existing git repository in {project_dir} without modifying it")
        else:
            print_func(f"switchyard: skipped project git initialization for {project_dir} (--no-git-init)")
            launcher._require_existing_project_git_repository(
                owner_user=owner_user,
                project_dir=project_dir,
                runner=runner,
            )

    provision_dir = (output_dir or (launcher._switchyard_dir(project_dir) / "provision")).expanduser().resolve(strict=False)
    provision_dir.mkdir(parents=True, exist_ok=True)
    launcher._write_json_atomic(provision_dir / "desktop-policy.json", selected_desktop_policy)
    return NewProjectAccounts(
        provision_dir=provision_dir,
    )


@dataclass(frozen=True)
class NewProjectBoard:
    """What P3 hands the rest of `switchyard_new_command` when provisioning
    succeeded, by the command's own local names. A failed provisioning is
    answered with its own status instead, and a refusal raises."""

    config: ProjectConfig
    config_path: Path


def _prepare_new_project_board(
    *,
    source_repo: Path | None,
    workflow_config: Path | None,
    commit_git_dir: str | None,
    port: int | None,
    database: str | None,
    home_base: Path,
    port_in_use: Callable[[int], bool],
    socket_exists: Callable[[Path], bool],
    registry_dir: Path | None,
    print_func: Callable[[str], None],
    artifact_path: Path,
    director_onboarding: Path,
    owner_user: str,
    project_dir: Path,
    resolved_slug: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    selected_role_efforts: dict[str, str],
    selected_role_models: dict[str, str],
    stages: ProvisioningStages,
    provision_dir: Path,
) -> NewProjectBoard | int:
    from scripts import team_launcher as launcher

    stages.begin("database and board")
    result = launcher.new_project_command(
        resolved_slug,
        from_artifact=artifact_path,
        owner_home=home_base / owner_user,
        source_repo=source_repo,
        workflow_config=workflow_config,
        commit_git_dir=commit_git_dir,
        output_dir=provision_dir,
        director_onboarding=director_onboarding,
        port=port,
        database=database,
        execute=True,
        runner=runner,
        port_in_use=port_in_use,
        socket_exists=socket_exists,
        require_owner_user=False,
        enable_owner_linger=False,
        role_models=selected_role_models,
        role_efforts=selected_role_efforts,
        print_func=print_func,
    )
    if result != 0:
        return result
    launcher._commit_project_git_changes(
        owner_user=owner_user,
        project_dir=project_dir,
        message="Record Switchyard provisioning artifacts",
        runner=runner,
    )
    config_path = provision_dir / f"{resolved_slug}.json"
    config = launcher.load_project_config(resolved_slug, config_path)
    config = launcher.prepare_project_desktop(config, runner=runner)
    launcher._register_switchyard_project(config_path, registry_dir=registry_dir)
    launcher._prepare_first_run_auth_worktrees(config, runner=runner)
    return NewProjectBoard(
        config=config,
        config_path=config_path,
    )


@dataclass(frozen=True)
class NewProjectSignIn:
    """What P4 hands the rest of `switchyard_new_command` when the roles can
    go on to their panes, by the command's own local names -- `config` and
    `first_run_auth_report` as the owner's confirmation left them. A stop is
    answered with the command's own 1 instead, and a refusal raises."""

    config: ProjectConfig
    first_run_auth_report: FirstRunAuthReport
    launch_deferred: bool
    launch_runner: Callable[..., subprocess.CompletedProcess[Any]]


def _run_new_project_sign_in(
    *,
    home_base: Path,
    euid_getter: Callable[[], int],
    input_func: Callable[[str], str],
    print_func: Callable[[str], None],
    interactive: bool | None,
    first_run_runner: Callable[..., subprocess.CompletedProcess[Any]] | _NoRunnerInjected,
    owner_user: str,
    project_dir: Path,
    resolved_slug: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    stages: ProvisioningStages,
    config: ProjectConfig,
    config_path: Path,
) -> NewProjectSignIn | int:
    from scripts import team_launcher as launcher

    # No live model probe here, deliberately. This used to ask every configured
    # role's model to read a file and prove it had, once per role and again when
    # the answer came back without the token -- up to 180 seconds an attempt on
    # the critical path of a first launch, and a launch refused outright when a
    # capable model simply answered without reaching for the tool. On test17
    # that is exactly what happened: Codex answered twice without reading
    # `switchyard-model-probe.txt`, and a tenant whose login and trust were both
    # complete was returned to the shell with no panes (SYRD-246).
    #
    # What stays is what is cheap and certain: the CLI is installed for the
    # owner, and the account is authenticated. Those are the two things that
    # make a pane unusable before it starts. Whether a model can call a tool is
    # the provider's own answer to give, in the pane, in its own words -- and
    # `switchyard validate-models` still asks it on purpose.
    stages.begin("provider sign-in and folder trust", waits_for_you=True)
    first_run_auth_report = launcher.run_first_run_auth_phase(
        config,
        owner_user=owner_user,
        owner_home=launcher._owner_home_for_auth(owner_user, fallback=home_base / owner_user),
        runner=runner,
        foreground_runner=launcher.foreground_runner_for(first_run_runner),
        print_func=print_func,
    )
    if launcher.stop_before_launch_for_missing_owner_clis(first_run_auth_report, print_func=print_func):
        return 1
    if launcher.stop_before_launch_for_unauthenticated_providers(
        first_run_auth_report, print_func=print_func
    ):
        return 1
    # The account exists and has authenticated now, which is the first moment
    # its model list is a real answer. The operator chose before either was
    # true, so they are offered the real list rather than refused for having
    # used the only one available to them (SYRD-250 DAT).
    config, first_run_auth_report = launcher.confirm_unknown_models_with_owner(
        config,
        first_run_auth_report,
        config_path=config_path,
        runner=runner,
        interactive=interactive,
        input_func=input_func,
        print_func=print_func,
    )
    if launcher.stop_before_launch_for_unknown_models(
        first_run_auth_report, project=config.project, print_func=print_func
    ):
        return 1
    launcher.report_models_were_not_probed(config, print_func=print_func)
    launch_runner = launcher._owner_project_git_runner(
        owner_user=owner_user,
        project_dir=project_dir,
        owned_roots=launcher._control_repository_owned_roots(config),
        runner=runner,
    )
    # A newly provisioned project declares per-role accounts that the operator
    # has not created yet, so its roles cannot start with their own identities.
    # Provisioning itself succeeded; the launch is deferred rather than failed,
    # and the artifacts say what to run next (SYRD-39).
    pending_isolation = launcher.role_isolation_gaps(config)
    launch_deferred = bool(pending_isolation)
    if launch_deferred:
        # The complete handoff -- accounts, ownership, runtime, tooling AND
        # credential seeding -- is written here, not left to a later failed
        # start, so following the printed instruction once is enough to make the
        # next start operable (SYRD-39).
        handoff_path, handoff_problems = launcher.publish_role_account_migration(
            config, config_path=config_path, euid_getter=euid_getter, print_func=print_func
        )
        next_step = (
            f"Run {handoff_path} as an operator (safe to re-run), then start it with "
            f"`switchyard {resolved_slug}`."
            if handoff_path is not None
            else (
                "Its role-account migration was not published where root can run it, so there is "
                "nothing to hand you yet: " + "; ".join(handoff_problems)
            )
        )
        print_func(
            f"switchyard: provisioned {resolved_slug}. Its roles are not isolated yet, so they "
            "were not started:\n  " + "\n  ".join(pending_isolation) + "\n" + next_step
        )
    return NewProjectSignIn(
        config=config,
        first_run_auth_report=first_run_auth_report,
        launch_deferred=launch_deferred,
        launch_runner=launch_runner,
    )


def _launch_new_project_panes(
    *,
    home_base: Path,
    pane_state_dir: Path | None,
    session_record_timeout: float,
    session_record_poll: float,
    layout_mode: str,
    layout_environ: dict[str, str] | None,
    konsole_process_launcher: Callable[..., Any] | None,
    print_func: Callable[[str], None],
    include_designer: bool,
    owner_user: str,
    resolved_slug: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    stages: ProvisioningStages,
    config_path: Path,
    config: ProjectConfig,
    first_run_auth_report: FirstRunAuthReport,
    launch_deferred: bool,
    launch_runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> int:
    from scripts import team_launcher as launcher

    # Before any window opens: a pane's first act is to run a program out of
    # the root-owned staged bundle, and a tenant whose staging was skipped
    # opened its tabs onto a command that was not there while provisioning
    # reported success (SYRD-249).
    stages.begin("role panes")
    staging_problems = launcher.ensure_staged_role_tooling(config, runner=runner, print_func=print_func)
    if staging_problems:
        for problem in staging_problems:
            print_func(f"switchyard: {problem}")
        print_func(
            f"switchyard: not opening {resolved_slug}'s windows. Everything else it needs was "
            "created and nothing was removed; the tenant is startable once its tooling is staged."
        )
        return 1
    launch_started_at = launcher.time.time()
    launch_started_ns = launcher.time.time_ns()
    launch_result = 0 if launch_deferred else launcher.launch_project(
        config,
        config_path=config_path,
        mode="start",
        script_path=Path(launcher.__file__).resolve().with_name(launcher.TEAM_LAUNCHER_NAME),
        layout_output=launcher._owner_state_layout_output_path(resolved_slug, owner_home=home_base / owner_user),
        assign_layout_owner=True,
        pane_state_dir=pane_state_dir,
        runner=launch_runner,
        layout_mode=layout_mode,
        layout_environ=layout_environ,
        konsole_process_launcher=konsole_process_launcher,
    )
    if launch_result != 0:
        return launch_result
    # Only a launch that actually happened may be reported as one, and only
    # then are there session records to wait for. Polling a deferred launch
    # would burn the full timeout on panes that were never started (SYRD-39).
    resolved_layout_mode = launcher.resolve_layout_mode(layout_mode, environ=layout_environ, runner=runner)
    if not launch_deferred:
        launcher.announce_new_project_presentation(
            resolved_slug, resolved_layout_mode=resolved_layout_mode, print_func=print_func
        )
    launcher.report_first_run_auth_warnings(first_run_auth_report, print_func=print_func)
    if not launch_deferred:
        launcher.report_launch_session_records(
            config,
            timeout_seconds=session_record_timeout,
            poll_seconds=session_record_poll,
            fallback_changed_since_ns=launch_started_ns,
            pane_state_dir=pane_state_dir or launcher.default_pane_state_dir_for_user(config.run_as_user, project=config.project),
            pane_state_updated_since=launch_started_at,
            print_func=print_func,
        )
    if resolved_layout_mode == launcher.LAYOUT_MODE_VIEWER:
        if include_designer:
            print_func("switchyard: maximize the designer pane during design with Ctrl+a z; press it again to restore")
        else:
            print_func("switchyard: design phase skipped; no designer pane configured")
    else:
        if include_designer:
            print_func("switchyard: maximize the designer pane during design with Konsole Ctrl+Shift+E; restore it when done")
        else:
            print_func("switchyard: design phase skipped; no designer pane configured")
    stages.finish()
    return 0
