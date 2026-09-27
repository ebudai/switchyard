"""Rebuilding a tenant's plan from what root recorded, and refusing a rebuild that changes it.

- `_validated_role_names` accepts only a list of process role names, recording
  an objection for anything else.
- `REGENERATED_PLAN_FIELDS` are the fields root regenerates rather than reads,
  and `_regenerated_field_divergence` names each one a regenerated plan would
  silently change (SYRD-39, SYRD-52).
- `plan_workflow_from_root` puts root's own recorded workflow on a rebuilt plan
  -- or root's plan record's document, or nothing, or the marker that root
  cannot vouch for a declared one -- and refuses an unusable record (SYRD-165).
- `_resume_source_release` resolves the release a rebuild comes from and refuses
  one that is not a root-controlled Switchyard release, and
  `_resume_plan_from_record` rebuilds a plan from root's record and the
  kernel-checked owner, reporting what it would change.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-360). The launcher
imports this module at its top and re-exports every name, so resume-provision
and the modules that read them through the launcher -- `pane_rebind`,
`workflow_adoption`, `repository_boundary_repair`, `privileged_runtime_plan` --
reach the same objects. Every launcher facility these use, and every name
defined here that another definition here reads, is read from `team_launcher`
when it runs, as it was. The plan builder and the unverified-workflow marker are
still imported inside the functions that use them. The standard-library names
are this module's own imports, the same objects. This module never imports
`team_launcher` at its top.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectBoardProvision


def _validated_role_names(value: Any, field: str, objections: list[str]) -> tuple[str, ...]:
    from scripts import team_launcher as launcher

    if value in (None, ""):
        return ()
    if not isinstance(value, list):
        objections.append(f"{field} is not a list")
        return ()
    names: list[str] = []
    for entry in value:
        name = str(entry).strip().lower()
        if not launcher.ROLE_RE.fullmatch(name) or name in launcher.NON_PROCESS_ROLES:
            objections.append(f"{field} entry {entry!r} is not a role name")
            continue
        names.append(name)
    return tuple(names)


# Fields root regenerates rather than reads. A project provisioned with any of
# them set differently was not built the way root would rebuild it, so
# installing the regenerated artifacts would change what runs -- a board that
# will not start, or, for commit_git_dir, one that resolves commit provenance
# against a different repository without saying so. Divergence is refused, never
# reconciled toward the document: the document is not an authority.
#
# The role tables are deliberately absent. Those are regenerated from validated
# role names and completed by the delta path, and that is also the channel a
# tenant would use to smuggle an account, so ignoring what the document says
# about them is the intended behaviour rather than a divergence to report.
REGENERATED_PLAN_FIELDS: tuple[str, ...] = (
    "owner_user",
    "control_user",
    "owner_home",
    "service_user",
    "service_role",
    "listener_role",
    "database",
    "commit_git_dir",
    "board_root",
    "board_current",
    "asset_dir",
    "frame_dir",
    "board_log",
    "listener_log",
    "socket_path",
    "runtime_directory",
    "board_unit",
    "canary_unit",
    "listener_unit",
    "tmpfiles_name",
    "polkit_name",
    "role_control_sudoers_name",
    # SYRD-50 added this one and did not list it here, so a tenant document
    # could record a different sudoers file name and be silently regenerated
    # rather than refused. Every generated name root installs belongs here
    # (SYRD-52).
    "tenant_control_sudoers_name",
)


def _regenerated_field_divergence(
    plan: ProjectBoardProvision, tenant_data: dict[str, Any], *, skip: Sequence[str] = ()
) -> list[str]:
    """Which recorded values the regenerated plan would silently replace."""
    from scripts import team_launcher as launcher

    diverged: list[str] = []
    for field in launcher.REGENERATED_PLAN_FIELDS:
        if field in skip:
            continue
        recorded = str(tenant_data.get(field) or "").strip()
        regenerated = str(getattr(plan, field, "") or "")
        if recorded and recorded != regenerated:
            diverged.append(f"{field}: provisioned {recorded!r}, regenerated {regenerated!r}")
    return diverged


def plan_workflow_from_root(
    plan: ProjectBoardProvision,
    *,
    root_plan_document: Mapping[str, Any] | None = None,
    declares_workflow: bool = False,
) -> tuple[ProjectBoardProvision, str]:
    """Put root's own recorded workflow on a plan root is rebuilding.

    A rebuilt baseline used to carry no workflow document at all, so a project
    that declares one was regenerated as though it declared none -- and the
    packet then carried the DEFAULT seed. On an established board that seeds
    nothing (SYRD-164), but a fresh or interrupted board with no tickets would
    have been given somebody else's roles, stages and transitions instead of
    its own (SYRD-165).

    Only sources root owns are consulted: its recorded workflow first, and then
    -- for a project provisioned before root kept one -- the document on root's
    own plan record, which regeneration then writes back as a record. A record
    that exists and cannot be used is a refusal rather than a reason to fall
    back, because falling back is what an edit to it would be for.

    When root has neither and the project declares a workflow, that is not
    permission to adopt the tenant's copy and not permission to seed the
    default either: the plan is marked as one root cannot vouch for, and the
    workflow phase of its packet does nothing at all.
    """
    from scripts import team_launcher as launcher

    from scripts.ticket_board.project_provision import UNVERIFIED_DECLARED_WORKFLOW

    document, problem = launcher.recorded_declared_workflow(plan.project)
    if document is not None:
        return replace(plan, workflow=document), ""
    if "holds no recorded workflow" not in problem:
        return plan, problem
    if root_plan_document is not None:
        return replace(plan, workflow=dict(root_plan_document)), ""
    if not declares_workflow:
        # This project declares no workflow; the default seed is its own.
        return plan, ""
    return replace(plan, workflow=None, workflow_seed=UNVERIFIED_DECLARED_WORKFLOW), ""


def _resume_source_release(source_repo: Path | None) -> tuple[Path, str]:
    """The release the rebuilt artifacts come from, and why it may not be used.

    Root-controlled or nothing: these bytes become the units, the SQL and the
    operator packet root installs, so a release a tenant could write is a
    release a tenant could provision itself from.
    """
    from scripts import team_launcher as launcher

    selected = (source_repo or (launcher.switchyard_shared_install_root() / "current")).expanduser()
    try:
        resolved = selected.resolve(strict=True)
    except OSError as exc:
        return selected, f"{selected} is not a release directory on this host ({exc.strerror})"
    if not (resolved / "scripts" / "team_launcher.py").is_file():
        return resolved, f"{resolved} does not look like a Switchyard release"
    walk = launcher.root_controlled_problems_for(str(resolved))
    if walk:
        return resolved, "; ".join(
            [f"{resolved} is not root-controlled, so it is not an audited release"] + walk
        )
    return resolved, ""


def _resume_plan_from_record(
    document: "PlanDocument", identity: "TrustedOwnerIdentity", *, source_repo: Path
) -> tuple[Any, list[str]]:
    """Rebuild the plan from what root recorded, and say what that would change.

    The identity comes from the kernel-checked record rather than from the
    document's own fields, and everything root regenerates is compared against
    what was provisioned: a rebuild that would quietly move an account, a home,
    a board root or a unit name is refused rather than reconciled, which is the
    same rule an upgrade follows.
    """
    from scripts import team_launcher as launcher

    from scripts.ticket_board.project_provision import build_plan

    recorded = document.data
    plan = build_plan(
        project=str(recorded.get("project") or ""),
        project_name=str(recorded.get("project_name") or "") or None,
        owner_user=identity.owner_user,
        owner_home=identity.owner_home,
        port=int(recorded["port"]) if str(recorded.get("port") or "").strip() else None,
        database=str(recorded.get("database") or "") or None,
        service_user=str(recorded.get("service_user") or "") or launcher._default_board_service_user(),
        source_repo=source_repo,
        commit_git_dir=str(recorded.get("commit_git_dir") or "") or None,
        implementer_roles=launcher._validated_role_names(recorded.get("implementer_roles"), "implementer_roles", []) or None,
        ticket_prefix=str(recorded.get("ticket_prefix") or "") or None,
        board_service_traversal=bool(recorded.get("board_service_traversal", True)),
        control_user=str(recorded.get("control_user") or ""),
        audit_roles=launcher._validated_role_names(recorded.get("audit_roles"), "audit_roles", []) or None,
    )
    plan, workflow_problem = launcher.plan_workflow_from_root(
        plan,
        root_plan_document=recorded.get("workflow") or None,
        declares_workflow=recorded.get("workflow") is not None,
    )
    divergence = launcher._regenerated_field_divergence(plan, recorded)
    if workflow_problem:
        divergence = [workflow_problem, *divergence]
    return plan, divergence
