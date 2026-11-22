"""The plan gate as a choice made when a project is created (SYRD-843).

Optional and off by default, like the research stage (research_onboarding) and the documentation gate, so `switchyard
new` offers it as one menu choice after them: off, or the role that owns the plan -- the project's designer when it
has one, or one of its implementers. No role or session is created: the owner is a role the project already has.

* **Recorded** in the project design artifact as `project.plan = {"owner": <role>}`; no field, or
  `{"owner": "off"}`, is off. Preview, apply and a retry from the artifact read this record.
* **Applied** by `new_project_command`, after Router, the documentation gate and the research stage, on the plan's
  own declared document or the one its rows describe, with `plan_gate.with_plan_gate` (the default plan file,
  decision log and size limit; `switchyard plan-gate` changes them later).

An upgrade never reads the design artifact; `switchyard plan-gate <project> enable` is how an existing project
chooses it. Inside the guided role plan the choice travels as one `RoleSelection`, `GATE_MARKER` with the owner's
name in place of a runtime, which `split_role_plan` takes out before any role is created.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, Callable, Sequence

GATE_MARKER = "plan-gate"


def plan_field(owners: Sequence[str]) -> Any:
    from scripts import team_launcher as launcher

    return launcher.Field(
        name="plan",
        kind=launcher.KIND_SINGLE,
        title="Plan gate (optional)",
        description=(
            "Gives the project's implementation plan one owner: milestones, deliverables, tickets, changed only "
            "through plan tickets that Audit reviews and the User approves. The Director scopes every ticket from "
            "it and never edits it. No extra session: the owner is a role the project already has."
        ),
        choices=(
            launcher.Choice("off", "off", "no plan gate"),
            *(launcher.Choice(f"owner:{name}", name, f"the plan owned by {name}") for name in owners),
        ),
        default="off",
    )


def prompt_plan(owners: Sequence[str], *, input_func: Callable[[str], str],
                print_func: Callable[[str], None]) -> Any:
    """The gate's selection for the role plan, or None for off."""
    from scripts import team_launcher as launcher

    try:
        answer = launcher.terminal_select.select_one(plan_field(owners), input_func=input_func, print_func=print_func)
    except launcher.terminal_select.Cancelled:
        raise SystemExit("switchyard: too many invalid answers for the plan gate") from None
    if isinstance(answer, str) and answer.startswith("owner:"):
        return launcher.RoleSelection(GATE_MARKER, answer.split(":", 1)[1])
    return None


def split_role_plan(role_plan: Sequence[Any]) -> tuple[tuple[Any, ...], tuple[tuple[str, str], ...]]:
    """The roles to create, and the gate's record, out of one guided plan."""
    roles = tuple(entry for entry in role_plan if entry.role != GATE_MARKER)
    chosen = next((entry for entry in role_plan if entry.role == GATE_MARKER), None)
    return roles, ((("owner", chosen.cli),) if chosen is not None else ())


def owner(plan: Sequence[tuple[str, str]]) -> str:
    return dict(plan).get("owner", "off")


def summary_line(plan: Sequence[tuple[str, str]]) -> str:
    if owner(plan) == "off":
        return "switchyard: Plan gate: off -- no plan owner; the Director keeps any plan by hand"
    return (f"switchyard: Plan gate: on, the plan owned by {owner(plan)} -- changed only through plan tickets Audit "
            "reviews and the User approves; no extra session")


def artifact_plan(raw: object, *, path: Path, owners: Sequence[str]) -> tuple[tuple[str, str], ...]:
    """`project.plan` out of a design artifact. Absent, null or `{"owner": "off"}` is off."""
    if raw is None:
        return ()
    if not isinstance(raw, dict) or set(raw) != {"owner"} or not isinstance(raw["owner"], str):
        raise SystemExit(f'{path} field \'project.plan\' must be {{"owner": <role>}}, or {{"owner": "off"}}')
    if raw["owner"] == "off":
        return ()
    if raw["owner"] not in owners:
        raise SystemExit(f"{path} field 'project.plan.owner' names the role that owns the plan: one of "
                         f"{', '.join(owners) or 'none'}, or off")
    return (("owner", raw["owner"]),)


def plan_with_plan_gate(plan: Any, *, gate: Sequence[tuple[str, str]], role_clis: Sequence[tuple[str, str]],
                        repository: Path, print_func: Callable[[str], None] = print) -> Any:
    """`plan` declaring the plan gate: its own workflow (every other optional stage's included), or its rows'."""
    from scripts.router_onboarding import plan_document
    from scripts.ticket_board.plan_gate import with_plan_gate
    from scripts.ticket_board.workflow_config import validate

    document = plan.workflow if plan.workflow is not None else plan_document(
        plan, role_clis=role_clis, repository=repository, label="Plan gate", print_func=print_func)
    try:
        document = with_plan_gate(document, owner=owner(gate))
    except ValueError as exc:
        raise SystemExit(f"switchyard: {exc}") from None
    return dataclasses.replace(plan, workflow=validate(document, project=plan.project))
