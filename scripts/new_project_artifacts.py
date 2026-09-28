"""The launcher config and layout template a new project is generated with.

- `write_new_project_launcher_artifacts` writes, into the output directory,
  the layout template for the visible roles and then the launcher config;
  takes group and other write off the config; re-lays the template out from
  the slots a declared workflow projects (and records the workflow in the
  design artifact); and folds a validated desktop policy into the config.
  Roles beyond the visible-pane cap are detached, and said so.
- `_new_project_launcher_config_payload` builds that config: each role once,
  in order (`_dedupe_role_defs`), with its CLI, its chosen model and effort
  when chosen, its slot or detachment, its worktree and pane session, the
  board, session directory, control repository and pane launcher, the
  upstream report settings, and the workflow's projection.
- `_new_project_role_env` is each role's environment: the pinned
  `directorctl`, the designer's design and onboarding paths, and the
  director's onboarding seed (`_director_seed_project_dir` finds its project).

Moved out of `scripts/team_launcher.py` unchanged (SYRD-419), in their original
order. The launcher imports this module and re-exports all six names;
`new_project_command` still reaches the writer through the launcher.
Everything the six read -- each other included, and the visible-pane cap, the
default role CLIs, the layout payload, the JSON reader and atomic writer, the
session, worktree and `.switchyard` directories, the onboarding seed and file
name and the shared pane launcher -- is read through the launcher at call
time, so a suite that rebinds one there still intercepts it. The defaults
Python binds at definition -- the default implementer roles and `print` --
are the same objects the launcher bound; the plan type is imported under
TYPE_CHECKING. This module imports `team_launcher` only inside the functions,
when they run.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from scripts.ticket_board.project_provision import DEFAULT_PROJECT_IMPLEMENTER_ROLES

if TYPE_CHECKING:
    from scripts.ticket_board.project_provision import ProjectBoardProvision


def _new_project_control_repository(project: str, owner_user: str) -> Path:
    return Path("/home") / owner_user / ".local" / "state" / "switchyard" / "projects" / project / "control.git"


def _new_project_launcher_config_payload(
    plan: ProjectBoardProvision,
    *,
    repository: Path,
    project_name: str | None = None,
    artifact_path: Path | None = None,
    design_document: Path | None = None,
    director_onboarding: Path | None = None,
    implementer_roles: Sequence[str] = DEFAULT_PROJECT_IMPLEMENTER_ROLES,
    role_clis: Sequence[tuple[str, str]] | None = None,
    #: What each role was chosen to run on, when it was chosen rather than
    #: defaulted. An absent model is left out of the payload rather than written
    #: as an empty string: the launcher reads a missing `model` as "the
    #: runtime's own default", and an empty one would be a value (SYRD-115).
    role_models: Mapping[str, str] | None = None,
    role_efforts: Mapping[str, str] | None = None,
    include_designer: bool = True,
    include_audit: bool = True,
    audit_roles: Sequence[str] | None = None,
    remote: str = "origin",
    default_branch: str = "main",
    worktree_policy: str = "shared",
    upstream_report_url: str = "",
    upstream_report_token_file: str = "",
) -> dict[str, Any]:
    from scripts import team_launcher as launcher

    layout_name = f"{plan.project}-konsole-layout.json"
    role_defs = launcher._dedupe_role_defs(
        role_clis
        or launcher._default_role_cli_pairs(
            implementer_roles,
            include_designer=include_designer,
            include_audit=include_audit,
            audit_roles=audit_roles,
        )
    )
    worktree_base = launcher._new_project_worktree_base(plan.project, plan.owner_user)
    control_repository = launcher._new_project_control_repository(plan.project, plan.owner_user)
    roles = []
    for index, (role, cli) in enumerate(role_defs):
        role_payload: dict[str, Any] = {
            "cli": [cli],
            "env": launcher._new_project_role_env(
                role,
                plan,
                project_name=project_name,
                artifact_path=artifact_path,
                design_document=design_document,
                director_onboarding=director_onboarding,
            ),
            "live_commands": [cli],
            "role": role,
            "target": f"{plan.project}-{role}:0.0",
            "tmux_session": f"{plan.project}-{role}",
            "workdir": str(worktree_base / role),
            "yolo": True,
        }
        chosen_model = str((role_models or {}).get(role, "")).strip()
        if chosen_model:
            role_payload["model"] = chosen_model
        chosen_effort = str((role_efforts or {}).get(role, "")).strip()
        if chosen_effort:
            role_payload["effort"] = chosen_effort
        if index < launcher.MAX_VISIBLE_PANES_PER_WINDOW:
            role_payload["slot"] = index
        else:
            role_payload["detached"] = True
        roles.append(role_payload)
    payload = {
        "project": plan.project,
        **({"project_name": project_name} if project_name else {}),
        "ticket_prefix": plan.ticket_prefix,
        "layout": layout_name,
        "repository": str(repository),
        "control_repository": str(control_repository),
        "run_as_user": plan.owner_user,
        "worktree_branch": default_branch,
        "worktree_remote": remote,
        "worktree_base": str(worktree_base),
        "board_url": f"http://127.0.0.1:{plan.port}",
        "board_socket": plan.socket_path,
        "session_dir": launcher._new_project_session_dir(plan.project, plan.owner_user),
        "role_state_isolation": True,
        "pane_launcher": str(launcher.switchyard_shared_pane_launcher()),
        "presentation": {
            "slot_count": min(len(role_defs), launcher.MAX_VISIBLE_PANES_PER_WINDOW),
            "layouts": {
                "default": {
                    str(index): role
                    for index, (role, _cli) in enumerate(role_defs[:launcher.MAX_VISIBLE_PANES_PER_WINDOW])
                }
            },
        },
        "roles": roles,
    }
    if upstream_report_url:
        payload["upstream_report_url"] = upstream_report_url
    if upstream_report_token_file:
        payload["upstream_report_token_file"] = upstream_report_token_file
    if plan.workflow is not None:
        from scripts.workflow_launcher import project_roles
        payload = project_roles(payload, plan.workflow)
    return payload


def _dedupe_role_defs(role_defs: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for role, cli in role_defs:
        if role in seen:
            continue
        seen.add(role)
        result.append((role, cli))
    return result


def _new_project_role_env(
    role: str,
    plan: ProjectBoardProvision,
    *,
    project_name: str | None = None,
    artifact_path: Path | None = None,
    design_document: Path | None = None,
    director_onboarding: Path | None = None,
) -> dict[str, str]:
    from scripts import team_launcher as launcher

    env: dict[str, str] = {
        "TICKET_BOARD_DIRECTORCTL": f"{plan.board_current}/scripts/directorctl",
    }
    if role == "designer" and design_document is not None:
        project_dir = design_document.parent
        env.update(
            {
                "SWITCHYARD_PROJECT_SLUG": plan.project,
                "SWITCHYARD_PROJECT_NAME": project_name or plan.project,
                "SWITCHYARD_PROJECT_ARTIFACT": str(artifact_path or (launcher._switchyard_dir(project_dir) / f"{plan.project}.project.json")),
                "SWITCHYARD_PROJECT_DESIGN": str(design_document),
                "SWITCHYARD_DESIGNER_ONBOARDING": str(
                    launcher._switchyard_dir(project_dir) / launcher.SWITCHYARD_DESIGN_ONBOARDING_FILE_NAME
                ),
            }
        )
    if role == "director" and director_onboarding is not None and design_document is not None:
        env.update(
            {
                "SWITCHYARD_DIRECTOR_ONBOARDING": str(director_onboarding),
                "SWITCHYARD_PROJECT_DESIGN": str(design_document),
            }
        )
    if role == "director":
        # Generic role data, read by the hook exactly as any other role's prompt is.
        # Seeded here because a project with no declarative workflow document has no
        # document to carry it, and the director's remit must not depend on one: the
        # hook no longer has a director-only branch to fall back to. A declarative
        # project carries the same field in its document, and the projection governs it
        # there, so this value is replaced rather than layered on top.
        seed_root = launcher._director_seed_project_dir(design_document, director_onboarding)
        if seed_root is not None:
            env.setdefault(
                "TICKET_BOARD_ROLE_ONBOARDING_PROMPT",
                launcher.director_onboarding_seed_text(seed_root),
            )
    return env


def _director_seed_project_dir(
    design_document: Path | None, director_onboarding: Path | None
) -> Path | None:
    from scripts import team_launcher as launcher

    if design_document is not None:
        return design_document.parent
    if director_onboarding is not None:
        parent = director_onboarding.parent
        return parent.parent if parent.name == launcher.SWITCHYARD_PROJECT_DIR_NAME else parent
    return None


def write_new_project_launcher_artifacts(
    plan: ProjectBoardProvision,
    output_dir: Path,
    *,
    repository: Path,
    project_name: str | None = None,
    artifact_path: Path | None = None,
    design_document: Path | None = None,
    director_onboarding: Path | None = None,
    implementer_roles: Sequence[str] = DEFAULT_PROJECT_IMPLEMENTER_ROLES,
    role_clis: Sequence[tuple[str, str]] | None = None,
    role_models: Mapping[str, str] | None = None,
    role_efforts: Mapping[str, str] | None = None,
    include_designer: bool = True,
    include_audit: bool = True,
    audit_roles: Sequence[str] | None = None,
    remote: str = "origin",
    default_branch: str = "main",
    worktree_policy: str = "shared",
    upstream_report_url: str = "",
    upstream_report_token_file: str = "",
    print_func: Callable[[str], None] = print,
) -> Path:
    from scripts import team_launcher as launcher

    role_defs = launcher._dedupe_role_defs(
        role_clis
        or launcher._default_role_cli_pairs(
            implementer_roles,
            include_designer=include_designer,
            include_audit=include_audit,
            audit_roles=audit_roles,
        )
    )
    visible_role_count = min(len(role_defs), launcher.MAX_VISIBLE_PANES_PER_WINDOW)
    detached_roles = [role for role, _cli in role_defs[launcher.MAX_VISIBLE_PANES_PER_WINDOW:]]
    if detached_roles:
        print_func(
            f"team-launcher: auto-detached roles beyond the {launcher.MAX_VISIBLE_PANES_PER_WINDOW}-pane window cap: "
            f"{', '.join(detached_roles)}; use attach-role to surface one later or detach another role first"
        )
    config_path = output_dir / f"{plan.project}.json"
    layout_path = output_dir / f"{plan.project}-konsole-layout.json"
    layout_path.write_text(
        json.dumps(launcher._new_project_layout_payload(visible_role_count), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    config_path.write_text(
        json.dumps(
            launcher._new_project_launcher_config_payload(
                plan,
                repository=repository,
                project_name=project_name,
                artifact_path=artifact_path,
                design_document=design_document,
                director_onboarding=director_onboarding,
                implementer_roles=implementer_roles,
                role_clis=role_defs,
                role_models=role_models,
                role_efforts=role_efforts,
                include_designer=include_designer,
                include_audit=include_audit,
                audit_roles=audit_roles,
                remote=remote,
                default_branch=default_branch,
                worktree_policy=worktree_policy,
                upstream_report_url=upstream_report_url,
                upstream_report_token_file=upstream_report_token_file,
            ),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    # Not group-writable, because the configuration decides which account every
    # role runs as and which board they talk to, and root refuses to register a
    # document anybody in its group could rewrite. Some were written 0660, and
    # on the tenant this was written for that made the registered configuration
    # unadoptable by the ordinary command (SYRD-167).
    config_path.chmod(config_path.stat().st_mode & ~(stat.S_IWGRP | stat.S_IWOTH))
    if plan.workflow:
        projected = launcher._load_json(config_path)
        visible_role_count = max(1, 1 + max((role.get("slot", -1) for role in projected["roles"]), default=-1))
        launcher._write_json_atomic(layout_path, launcher._new_project_layout_payload(visible_role_count))
        if artifact_path and artifact_path.exists():
            artifact_data = launcher._load_json(artifact_path)
            artifact_data["workflow"] = plan.workflow
            launcher._write_json_atomic(artifact_path, artifact_data)
    policy_file = output_dir / "desktop-policy.json"
    if policy_file.exists():
        from scripts.desktop_access import validate_policy
        payload = launcher._load_json(config_path)
        payload["desktop_access"] = validate_policy(launcher._load_json(policy_file), project=plan.project, tenant=plan.owner_user)
        launcher._write_json_atomic(config_path, payload)
    return config_path
