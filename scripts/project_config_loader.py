"""Loading and validating a project's generated configuration.

`load_project_config` reads a project's config -- through the no-follow tenant
reader when root reads it, so a planted symlink or a file that is not JSON is
refused rather than followed (SYRD-228) -- and returns its `ProjectConfig`,
refusing, word for word and in a fixed order: a project that does not match; no
roles; an inline upstream report token; a worktree base or control repository
without the repository they need; empty worktree remote or branch; roles that
are not objects; duplicate or misplaced layout slots; duplicate non-shared role
workdirs; and a control repository outside its boundary.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-450). The launcher
imports this module and re-exports the name; every launcher caller and every
module that reads it through the launcher still reaches the launcher's name.
Everything it reads when it runs -- the slug and prefix validators, the path
helpers, the default config directory and board defaults, the no-follow reader
and the JSON reader, the role parser, the worker-pool parser, the board
environment, the session directory default, the control-repository boundary
check and the config type itself -- is read through the launcher, so a suite
that rebinds one there still intercepts it. This module imports `team_launcher`
only inside the function, when it runs.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig, RoleConfig


def load_project_config(
    project: str,
    config_path: Path | None = None,
    *,
    document: Mapping[str, Any] | None = None,
) -> ProjectConfig:
    from scripts import team_launcher as launcher

    project_slug = launcher._validate_project_slug(project)
    path = config_path or launcher.DEFAULT_CONFIG_DIR / f"{project_slug}.json"
    if document is None and os.geteuid() == 0:
        # Root reads a document in a directory the tenant owns, and the upgrade
        # re-reads it at every phase. `Path.read_text` follows whatever is
        # there, so one planted symlink sent root to read a file of the
        # tenant's choosing -- and a file that was not JSON came back as a
        # traceback rather than a refusal (SYRD-228). Unprivileged callers read
        # their own file as before.
        document, problem = launcher.read_tenant_document_no_follow(
            path, what=f"{project_slug}'s generated configuration"
        )
        if problem:
            raise SystemExit(f"switchyard: {problem}. Nothing was changed.")
    config = dict(document) if document is not None else launcher._load_json(path)
    config_project = launcher._validate_project_slug(str(config.get("project") or project_slug))
    if config_project != project_slug:
        raise SystemExit(f"config project {config_project!r} does not match requested project {project_slug!r}")
    project_name = str(config.get("project_name") or config.get("name") or config_project).strip() or config_project
    roles_raw = config.get("roles")
    if not isinstance(roles_raw, list) or not roles_raw:
        raise SystemExit(f"{path} must define a non-empty roles list")
    base = path.parent
    layout = launcher._expand_path(str(config.get("layout") or f"{config_project}-konsole-layout.json"), base=base)
    run_as_user = str(config.get("run_as_user") or "").strip()
    session_dir = launcher._expand_path(str(config.get("session_dir") or str(launcher.default_session_dir_for_user(run_as_user))), base=base)
    ticket_prefix = launcher.validate_ticket_prefix(str(config.get("ticket_prefix") or config_project))
    board_url = str(config.get("board_url") or launcher._default_board_url(config_project)).strip()
    board_socket = str(config.get("board_socket") or launcher._default_board_socket(config_project)).strip()
    upstream_report_url = str(config.get("upstream_report_url") or config.get("report_board_url") or "").strip()
    inline_report_token_keys = [
        key for key in ("upstream_report_token", "tenant_report_token") if str(config.get(key) or "").strip()
    ]
    if inline_report_token_keys:
        raise SystemExit(
            f"{path} stores an upstream report token value in {', '.join(inline_report_token_keys)}; "
            "use upstream_report_token_file instead"
        )
    upstream_report_token_file = str(
        config.get("upstream_report_token_file") or config.get("tenant_report_token_file") or ""
    ).strip()
    pane_launcher = None
    pane_launcher_raw = config.get("pane_launcher")
    if pane_launcher_raw is not None:
        pane_launcher = launcher._expand_path(str(pane_launcher_raw), base=base)
    repository = None
    repository_raw = config.get("repository")
    if repository_raw is not None:
        repository = launcher._expand_path(str(repository_raw), base=base)
    control_repository = None
    control_repository_raw = config.get("control_repository")
    if control_repository_raw is not None:
        control_repository = launcher._expand_path(str(control_repository_raw), base=base)
    worktree_base = None
    worktree_base_raw = config.get("worktree_base")
    if worktree_base_raw is not None:
        worktree_base = launcher._expand_path(str(worktree_base_raw), base=base)
    if repository is None and worktree_base is not None:
        raise SystemExit(f"{path} must not define worktree_base without repository")
    if control_repository is not None and repository is None:
        raise SystemExit(f"{path} must not define control_repository without repository")
    if control_repository is not None and worktree_base is None:
        raise SystemExit(f"{path} must define worktree_base when control_repository is set")
    worktree_remote = str(config.get("worktree_remote") or "origin").strip()
    worktree_branch = str(config.get("worktree_branch") or "main").strip()
    if not worktree_remote or not worktree_branch:
        raise SystemExit(f"{path} worktree_remote and worktree_branch must be non-empty")
    roles: list[RoleConfig] = []
    for raw in roles_raw:
        if not isinstance(raw, dict):
            continue
        default_workdir = repository
        if control_repository is not None and raw.get("workdir") is None and worktree_base is not None:
            role_name = str(raw.get("role") or "").strip()
            if role_name:
                default_workdir = worktree_base / role_name
        roles.append(launcher._role_from_json(config_project, raw, base=base, default_workdir=default_workdir))
    if len(roles) != len(roles_raw):
        raise SystemExit(f"{path} roles must all be JSON objects")
    slots = [role.slot for role in roles if role.slot is not None]
    if len(set(slots)) != len(slots):
        raise SystemExit(f"{path} contains duplicate role slot assignments")
    detached_with_slots = [role.role for role in roles if role.detached and role.slot is not None]
    if detached_with_slots:
        raise SystemExit(f"{path} detached roles must not define layout slots: {', '.join(detached_with_slots)}")
    visible_without_slots = [role.role for role in roles if not role.detached and role.slot is None]
    if visible_without_slots:
        raise SystemExit(f"{path} visible roles must define layout slots: {', '.join(visible_without_slots)}")
    if repository is not None:
        repo_path = launcher._normalized_path(repository)
    else:
        repo_path = ""
    seen_workdirs: dict[str, str] = {}
    duplicate_non_shared: list[str] = []
    for role in roles:
        normalized_workdir = launcher._normalized_path(Path(role.workdir))
        previous_role = seen_workdirs.setdefault(normalized_workdir, role.role)
        if previous_role != role.role and normalized_workdir != repo_path:
            duplicate_non_shared.append(f"{previous_role}/{role.role}:{normalized_workdir}")
    if duplicate_non_shared:
        raise SystemExit(f"{path} contains duplicate non-shared role workdir assignments: {', '.join(duplicate_non_shared)}")
    parsed_config = launcher.ProjectConfig(
        project=config_project,
        project_name=project_name,
        ticket_prefix=ticket_prefix,
        layout=layout,
        session_dir=session_dir,
        board_url=board_url,
        board_socket=board_socket,
        upstream_report_url=upstream_report_url,
        upstream_report_token_file=upstream_report_token_file,
        run_as_user=run_as_user,
        pane_launcher=pane_launcher,
        repository=repository,
        control_repository=control_repository,
        worktree_base=worktree_base,
        worktree_remote=worktree_remote,
        worktree_branch=worktree_branch,
        roles=roles,
        desktop_access=config.get("desktop_access"),
        role_state_isolation=bool(config.get("role_state_isolation", False)),
        worker_pool=launcher.parse_worker_pool(config.get("worker_pool"), path=path),
    )
    boundary_error = launcher._control_repository_boundary_error(parsed_config, require_existing_user=False)
    if boundary_error is not None:
        raise SystemExit(f"{path} control_repository {boundary_error}")
    return replace(parsed_config, roles=launcher._with_project_board_env(parsed_config, roles))
