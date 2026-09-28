"""`team-launcher design`: ask for a project's design and record it as an artifact.

`design_project_command` asks for (or takes as arguments) everything a project
design records -- title and summary, code location, remote and branch,
worktree policy, owner, ticket prefix, push policy, audit and implementer
roles, the default gates and the owner's capability grants -- then writes the
design document and the project artifact that `switchyard new --from` consumes.
`_default_project_artifact_path` and `_default_project_design_document_path`
name the two files when no path is given, and `_comma_list` splits the
supplementary-groups answer.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-423), in their original
order. The launcher imports this module and re-exports all four names; its
`main` still dispatches the `design` verb to the launcher's name. Everything the
four read -- each other included, and the launcher's prompts, validators,
defaults, artifact type and payload, markdown renderer and atomic writer, and
the prompt schema, terminal selector, owner and prefix validators and default
implementer roles it imports -- is read through the launcher at call time, so a
suite that rebinds one there still intercepts it. The only definition-time
defaults are the `input` and `print` builtins. This module imports
`team_launcher` only inside the command, when it runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Sequence


def _comma_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _default_project_artifact_path(project: str, output_dir: Path) -> Path:
    return output_dir / f"{project}.project.json"


def _default_project_design_document_path(project: str, output_dir: Path) -> Path:
    return output_dir / f"{project}-design.md"


def design_project_command(
    project: str,
    *,
    output_dir: Path | None = None,
    artifact_path: Path | None = None,
    design_document: Path | None = None,
    project_name: str | None = None,
    design_title: str | None = None,
    design_body: str | None = None,
    repository: Path | None = None,
    remote: str | None = None,
    default_branch: str | None = None,
    worktree_policy: str | None = None,
    owner_user: str | None = None,
    ticket_prefix: str | None = None,
    implementer_roles: Sequence[str] | None = None,
    push_policy: str | None = None,
    audit_signoff: bool | None = None,
    audit_roles: Sequence[str] | None = None,
    needs_inspection: bool | None = None,
    needs_user_signoff: bool | None = None,
    board_service_traversal: bool | None = None,
    supplementary_groups: Sequence[str] | None = None,
    linger: bool | None = None,
    owner_shell: str | None = None,
    input_func: Callable[[str], str] = input,
    print_func: Callable[[str], None] = print,
) -> int:
    from scripts import team_launcher as launcher

    project_slug = launcher._validate_project_slug(project)
    base_dir = (output_dir or Path.cwd()).expanduser().resolve(strict=False)
    target_artifact = (artifact_path or launcher._default_project_artifact_path(project_slug, base_dir)).expanduser().resolve(strict=False)
    target_design_document = (
        design_document or launcher._default_project_design_document_path(project_slug, target_artifact.parent)
    ).expanduser().resolve(strict=False)

    title = design_title if design_title is not None else launcher._prompt_text("Design document title", default=f"{project_slug} design", input_func=input_func)
    resolved_project_name = (project_name or title or project_slug).strip()
    body = design_body if design_body is not None else launcher._prompt_text("Design summary", input_func=input_func)
    repo = repository or Path(launcher._prompt_text("Code location", input_func=input_func))
    resolved_remote = remote or launcher._prompt_text("Remote", default="origin", input_func=input_func)
    resolved_branch = default_branch or launcher._prompt_text("Default branch", default="main", input_func=input_func)
    # A declared set, so it is shown as one. Push policy and owner shell are
    # left as text on purpose: neither has a vocabulary anywhere in this
    # codebase, and a selector whose options somebody invented to fill the list
    # out is worse than a text field -- it looks authoritative (SYRD-115).
    resolved_policy = worktree_policy or launcher.terminal_select.select_one(
        launcher.Field(
            name="worktree_policy",
            kind=launcher.KIND_SINGLE,
            title="Worktree policy",
            choices=(
                launcher.Choice("shared", "shared", "every role works in one checkout"),
                launcher.Choice("isolated", "isolated", "a worktree per role"),
            ),
            default="shared",
        ),
        input_func=input_func,
        print_func=print_func,
    )
    if resolved_policy not in launcher.WORKTREE_POLICIES:
        raise SystemExit(f"worktree policy must be one of {sorted(launcher.WORKTREE_POLICIES)}")
    resolved_owner = launcher._owner_user_verbatim(
        owner_user if owner_user is not None else launcher._prompt_text("Owner user", default=launcher._default_new_project_owner(project_slug), input_func=input_func)
    )
    resolved_prefix = launcher.validate_ticket_prefix(
        ticket_prefix or launcher._prompt_text("Ticket prefix", default=project_slug.upper(), input_func=input_func)
    )
    resolved_push_policy = push_policy or launcher._prompt_text("Push policy", default="director-main-only", input_func=input_func)
    resolved_audit_roles = launcher._dedupe_role_names(
        tuple(launcher._validate_new_project_audit_role(role) for role in (audit_roles or ("audit",)))
    )
    resolved_implementer_roles = tuple(implementer_roles or launcher.DEFAULT_PROJECT_IMPLEMENTER_ROLES)
    role_overlap = set(resolved_implementer_roles) & set(resolved_audit_roles)
    if role_overlap:
        raise SystemExit(f"roles cannot be both implementers and auditors: {', '.join(sorted(role_overlap))}")
    gates = {
        "audit_signoff": audit_signoff
        if audit_signoff is not None
        else launcher._prompt_bool("Require audit signoff gate", default=launcher.PROJECT_DESIGN_DEFAULT_GATES["audit_signoff"], input_func=input_func),
        "needs_inspection": needs_inspection
        if needs_inspection is not None
        else launcher._prompt_bool("Enable inspection gate by default", default=launcher.PROJECT_DESIGN_DEFAULT_GATES["needs_inspection"], input_func=input_func),
        "needs_user_signoff": needs_user_signoff
        if needs_user_signoff is not None
        else launcher._prompt_bool("Enable user signoff gate by default", default=launcher.PROJECT_DESIGN_DEFAULT_GATES["needs_user_signoff"], input_func=input_func),
    }
    grants = {
        "board_service_traversal": board_service_traversal
        if board_service_traversal is not None
        else launcher._prompt_bool("Grant board-service traversal into the owner home", default=True, input_func=input_func),
        "supplementary_groups": list(supplementary_groups)
        if supplementary_groups is not None
        else launcher._comma_list(launcher._prompt_text("Supplementary groups (comma-separated, blank for none)", input_func=input_func)),
        "linger": linger
        if linger is not None
        else launcher._prompt_bool("Enable linger for the owner user", default=True, input_func=input_func),
        "shell": owner_shell or launcher._prompt_text("Owner shell", default="fish", input_func=input_func),
    }
    artifact = launcher.ProjectDesignArtifact(
        project=project_slug,
        project_name=resolved_project_name,
        ticket_prefix=resolved_prefix,
        owner_user=resolved_owner,
        repository=repo.expanduser().resolve(strict=False),
        remote=resolved_remote,
        default_branch=resolved_branch,
        worktree_policy=resolved_policy,
        design_document=target_design_document,
        implementer_roles=resolved_implementer_roles,
        audit_roles=resolved_audit_roles,
        role_clis=launcher._default_role_cli_pairs(
            resolved_implementer_roles,
            include_designer=True,
            include_audit=True,
            audit_roles=resolved_audit_roles,
        ),
        include_designer=True,
        include_audit=bool(resolved_audit_roles),
        push_policy=resolved_push_policy,
        gates=gates,
        capability_grants=grants,
    )
    target_design_document.parent.mkdir(parents=True, exist_ok=True)
    target_design_document.write_text(launcher._project_design_markdown(project_slug, title=title, body=body), encoding="utf-8")
    launcher._write_json_atomic(target_artifact, launcher.project_design_artifact_payload(artifact))
    print_func(f"team-launcher: wrote design document {target_design_document}")
    print_func(f"team-launcher: wrote project artifact {target_artifact}")
    return 0
