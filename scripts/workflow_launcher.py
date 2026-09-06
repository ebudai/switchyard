"""Declarative role projection; workflow authorization remains in the board."""

from __future__ import annotations
import copy
import json
from pathlib import Path
from typing import Any

from scripts.ticket_board.workflow_config import validate


def project_roles(raw: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    cfg = validate(document, project=raw["project"])
    result = copy.deepcopy(raw)
    prior = {r["role"]: r for r in raw.get("roles", [])}
    retired = copy.deepcopy(raw.get("retired_workflow_roles", {}))
    prior = {**retired, **prior}
    projected = []
    for spec in cfg["roles"]:
        name = spec["name"]
        if not spec["active"] or spec.get("runtime") is None:
            if name in prior:
                retired[name] = prior[name]
            continue
        template = prior.get(name) or prior.get(spec.get("template_role", "")) or {}
        role = copy.deepcopy(template)
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
        projected.append(role)
        retired.pop(name, None)
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
        files[plan_path] = json.dumps(plan, indent=2, sort_keys=True) + "\n"
    artifact_path = config_path.parent.parent / f'{projected["project"]}.project.json'
    if artifact_path.exists():
        artifact = json.loads(artifact_path.read_text())
        artifact["workflow"] = document
        files[artifact_path] = json.dumps(artifact, indent=2, sort_keys=True) + "\n"
    return files


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
