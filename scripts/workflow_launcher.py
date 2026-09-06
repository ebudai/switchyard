"""Declarative role projection; workflow authorization remains in the board."""

from __future__ import annotations
import copy
import json
from pathlib import Path
from typing import Any

from scripts.ticket_board.project_provision import NON_PROCESS_ROLES, role_account_name
from scripts.ticket_board.workflow_config import validate


def project_roles(raw: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    cfg = validate(document, project=raw["project"])
    result = copy.deepcopy(raw)
    prior = {r["role"]: r for r in raw.get("roles", [])}
    retired = copy.deepcopy(raw.get("retired_workflow_roles", {}))
    prior = {**retired, **prior}
    # A project has opted into per-role Unix identities when its existing roles
    # carry accounts. Projection must never opt one in that never asked, and
    # never leave a role in an isolated project without an identity (SYRD-39).
    isolated = any(
        str(existing.get("run_as_user") or "").strip() for existing in prior.values()
    )
    projected = []
    for spec in cfg["roles"]:
        name = spec["name"]
        if not spec["active"] or spec.get("runtime") is None:
            if name in prior:
                retired[name] = prior[name]
            continue
        existing = prior.get(name)
        template = existing or prior.get(spec.get("template_role", "")) or {}
        role = copy.deepcopy(template)
        if existing is None:
            # A template supplies pane shape, not identity. Carrying its Unix
            # account over would run the new role as the template role's
            # account, which the board resolves as the TEMPLATE role: the new
            # process could not act as itself, and could act as the role it was
            # copied from. Identity is never inherited (SYRD-39).
            role.pop("run_as_user", None)
        # Runtime selection is bounded; preserve existing vendor arguments only
        # when the selected runtime matches the trusted local role template.
        if not role.get("cli") or role["cli"][0] != spec["runtime"]:
            role["cli"] = [spec["runtime"]]
        role.update(
            role=name,
            target=spec["target"],
            tmux_session=spec["target"].split(":")[0],
            live_commands=[spec["runtime"]],
        )
        role.pop("slot", None)
        role.pop("detached", None)
        if spec.get("slot") is None:
            role["detached"] = True
        else:
            role["slot"] = spec["slot"]
        if raw.get("worktree_base"):
            role["workdir"] = str(Path(raw["worktree_base"]) / name)
        elif raw.get("repository"):
            role["workdir"] = raw["repository"]
        # SYRD-36 x SYRD-39: a role the workflow document introduces -- whether
        # from nothing or from another role's template -- has no account of its
        # own. Without one it launches as the project owner, and on the board
        # socket its uid is indistinguishable from every other unisolated
        # writer. It gets exactly the account provisioning would have given it,
        # from the same function, so the two can never drift apart.
        if not role.get("run_as_user") and isolated and name not in NON_PROCESS_ROLES:
            role["run_as_user"] = role_account_name(raw["project"], name)
        env = role.setdefault("env", {})
        if name != spec.get("template_role", name):
            for key in [
                "SWITCHYARD_DESIGNER_ONBOARDING",
                "SWITCHYARD_PROJECT_ARTIFACT",
                "SWITCHYARD_PROJECT_DESIGN",
            ]:
                env.pop(key, None)
        env.pop("TICKET_BOARD_ROLE_ONBOARDING", None)
        if spec.get("onboarding"):
            env["TICKET_BOARD_ROLE_ONBOARDING"] = spec["onboarding"]
        # Popped unconditionally so clearing a prompt actually removes it from the
        # projected environment rather than leaving the previous one behind.
        env.pop("TICKET_BOARD_ROLE_ONBOARDING_PROMPT", None)
        if spec.get("onboarding_prompt"):
            env["TICKET_BOARD_ROLE_ONBOARDING_PROMPT"] = spec["onboarding_prompt"]
        # Phase-one bridge (SYRD-36): the hook never reads the workflow document, so the
        # migration marker is projected alongside the prompt. Without it the hook cannot
        # tell a tenant that predates stored onboarding from a director who cleared theirs.
        env.pop("TICKET_BOARD_ROLE_ONBOARDING_MIGRATED", None)
        if cfg.get("migrations", {}).get("director_onboarding"):
            env["TICKET_BOARD_ROLE_ONBOARDING_MIGRATED"] = "1"
        projected.append(role)
        retired.pop(name, None)
    # One account backs exactly one role. The board refuses both roles when a
    # uid backs two, so a projection that produced a collision would take the
    # colliding roles off the board entirely; refuse to write it instead.
    owners: dict[str, str] = {}
    for role in projected:
        account = str(role.get("run_as_user") or "").strip()
        if not account:
            continue
        if account in owners:
            raise ValueError(
                f"projected roles {owners[account]!r} and {role['role']!r} would both run as "
                f"the Unix account {account!r}; each role needs its own identity"
            )
        owners[account] = role["role"]
    active = {r["role"] for r in projected}
    for name, role in prior.items():
        if name not in active:
            retired[name] = role
    result.update(roles=projected, retired_workflow_roles=retired, workflow=cfg)
    return result


def projection_files(config_path: Path, document: dict[str, Any]) -> dict[Path, str]:
    from scripts import team_launcher as launcher

    raw = json.loads(config_path.read_text())
    document = validate(document, project=raw["project"])
    projected = project_roles(raw, document)
    layout = Path(projected["layout"])
    if not layout.is_absolute():
        layout = config_path.parent / layout
    visible = max(
        1, 1 + max((r.get("slot", -1) for r in projected["roles"]), default=-1)
    )
    files = {
        config_path: json.dumps(projected, indent=2, sort_keys=True) + "\n",
        layout: json.dumps(
            launcher._new_project_layout_payload(visible), indent=2, sort_keys=True
        )
        + "\n",
        config_path.parent
        / "workflow.json": json.dumps(validate(document), indent=2, sort_keys=True)
        + "\n",
    }
    plan_path = config_path.parent / "plan.json"
    if plan_path.exists():
        plan = json.loads(plan_path.read_text())
        plan["workflow"] = validate(document)
        files.update(refreshed_role_account_artifacts(plan, projected, provision_dir=config_path.parent))
        files[plan_path] = json.dumps(plan, indent=2, sort_keys=True) + "\n"
    artifact_path = config_path.parent.parent / f'{projected["project"]}.project.json'
    if artifact_path.exists():
        artifact = json.loads(artifact_path.read_text())
        artifact["workflow"] = document
        files[artifact_path] = json.dumps(artifact, indent=2, sort_keys=True) + "\n"
    return files


def refreshed_role_account_artifacts(
    plan: dict[str, Any], projected: dict[str, Any], *, provision_dir: Path
) -> dict[Path, str]:
    """Carry declarative role changes into the plan's role->account table.

    The board resolves authority from that table, and the rollout hands each
    role the tree it works in, so a role added by a workflow document has
    neither until both are refreshed here. `plan` is updated in place; the
    regenerated board unit is returned for the caller to write with the rest of
    the projection. A project that never opted into per-role identities keeps
    an empty table (SYRD-39).
    """
    if not plan.get("role_accounts"):
        return {}
    accounts = [
        (role["role"], str(role["run_as_user"]).strip())
        for role in projected["roles"]
        if str(role.get("run_as_user") or "").strip()
    ]
    # A role the document retires keeps its mapping: its account still exists on
    # the host, and leaving the row means that uid still resolves to the role it
    # actually is, which the board then refuses as inactive. Dropping the row
    # would leave a live account unattributed instead.
    named = {role for role, _account in accounts}
    for entry in plan["role_accounts"]:
        role, account = str(entry[0]), str(entry[1])
        if role not in named:
            accounts.append((role, account))
    plan["role_accounts"] = [list(entry) for entry in accounts]
    if plan.get("role_worktrees"):
        worktrees = {str(entry[0]): str(entry[1]) for entry in plan["role_worktrees"]}
        for role in projected["roles"]:
            if role.get("workdir") and role["role"] in named:
                worktrees[role["role"]] = str(role["workdir"])
        plan["role_worktrees"] = [[role, path] for role, path in sorted(worktrees.items())]
    unit_name = str(plan.get("board_unit") or "").strip()
    if not unit_name:
        return {}
    from dataclasses import replace as _replace
    from scripts.ticket_board.project_provision import ProjectBoardProvision, render_board_unit
    from scripts import team_launcher as launcher

    try:
        current = launcher._project_board_provision_from_json(provision_dir / "plan.json")
    except SystemExit:
        # A plan too old to load as a provision record still gets its table
        # refreshed above; regenerating its unit is the operator's rollout step.
        return {}
    refreshed = _replace(
        current,
        role_accounts=tuple(tuple(entry) for entry in plan["role_accounts"]),
        role_worktrees=tuple(tuple(entry) for entry in plan.get("role_worktrees", ())),
    )
    assert isinstance(refreshed, ProjectBoardProvision)
    return {provision_dir / unit_name: render_board_unit(refreshed)}


def prepare_role(config_path: Path, role_name: str, *, runner=None) -> None:
    """Prepare only the named role through established owner/trust/hook paths."""
    import subprocess
    from dataclasses import replace
    from scripts import team_launcher as launcher

    runner = runner or subprocess.run
    raw = json.loads(config_path.read_text())
    expected = project_roles(raw, raw["workflow"])
    if raw["roles"] != expected["roles"]:
        raise ValueError(
            "launcher roles differ from validated workflow; apply projection before preparation"
        )
    config = launcher.load_project_config(raw["project"], config_path)
    role = launcher._role_by_name(config, role_name)
    narrowed = launcher.prepare_project_desktop(
        replace(config, roles=[role]), runner=runner
    )
    role_runner = runner
    if config.run_as_user and launcher.current_user_name() != config.run_as_user:
        role_runner = launcher._owner_project_git_runner(
            owner_user=config.run_as_user,
            project_dir=config.repository or Path("/"),
            owned_roots=launcher._control_repository_owned_roots(config),
            runner=runner,
        )
    result = launcher.ensure_project_worktrees(
        narrowed, refresh=True, runner=role_runner
    )
    if not result.ok:
        raise RuntimeError(f"role worktree preparation failed: {result.failed_roles}")
    launcher.ensure_generated_project_pane_hooks(
        config,
        config_path=config_path,
        script_path=Path(launcher.__file__).with_name("team-launcher"),
        pane_state_dir=launcher.default_pane_state_dir_for_user(
            config.run_as_user, project=config.project
        ),
        runner=runner,
    )
    report = launcher.run_switchyard_launch_first_run_auth(narrowed, runner=runner)
    launcher.report_first_run_auth_warnings(report)
    if report.has_warnings:
        raise RuntimeError("role authentication/trust needs completion before start")


def assign_projection_owner(config, config_path: Path, *, runner) -> None:
    """Initial privileged provisioning hands generated controls to the tenant."""
    from scripts import team_launcher as launcher

    raw = json.loads(config_path.read_text())
    # Initially legacy projects need writable controls too: their first workflow
    # is applied after startup. Enumerate the projection paths without requiring
    # an existing workflow document or rendering a replacement configuration.
    layout = Path(raw["layout"])
    if not layout.is_absolute():
        layout = config_path.parent / layout
    files = [
        config_path,
        layout,
        config_path.parent / "workflow.json",
        config_path.parent / "plan.json",
        config_path.parent.parent / f'{raw["project"]}.project.json',
    ]
    # This is deliberately non-recursive: service receipts and unrelated host
    # paths are not part of tenant configuration ownership.
    for path in [config_path.parent, *files]:
        if path.exists():
            launcher.ensure_owner_file(config, path, runner=runner)
