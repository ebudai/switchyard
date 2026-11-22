"""The research stage as a choice made when a project is created (SYRD-842).

Like the documentation gate (documentation_onboarding), it is optional and off by default, so `switchyard new`
offers it as one plainly labelled menu choice next to Router and the documentation gate:

* **Asked** after them, with three answers: off; one of the project's implementers does the research, which
  creates no role and no session; or a new Researcher, whose runtime, model and effort are chosen through the same
  guided path as every other role's, inside the role plan's owner-catalog guard.
* **Recorded** in the project design artifact as `project.research`: `{"stage": "role", "role": <implementer>}`
  or `{"stage": "researcher", "runtime", "model", "effort"}`. `{"stage": "off"}`, or no field at all -- every
  artifact written before it existed -- is off. Preview, apply and a retry from the artifact all read this record.
* **Applied** by `new_project_command`, after Router and the documentation gate, on the plan's own declared
  document or the one its rows describe, with `research_stage.with_research_stage` added. A Researcher takes the
  window's next free display slot, and runs in the background only when all six are taken; its launcher entry is
  given its own model and effort rather than its template's.

An upgrade never reads the design artifact; `switchyard research-stage <project> enable` is how an existing project
chooses it. Inside the guided role plan the choice travels as one `RoleSelection`: the Researcher as its own role,
or `STAGE_MARKER` with the implementer's name in place of a runtime. `split_role_plan` takes it out again before
any role is created.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, Callable, Sequence

from scripts.ticket_board.research_stage import RESEARCHER_ROLE

#: The role plan's stand-in for "an existing implementer researches": never a role.
STAGE_MARKER = "research-stage"
STAGES = ("off", "role", "researcher")
RESEARCHER_KEYS = ("runtime", "model", "effort")
_RECORD_KEYS = frozenset({"stage", "role", *RESEARCHER_KEYS})
#: The runtime the menu offers first for a Researcher: reading, measuring and writing up.
RESEARCHER_DEFAULT_CLI = "claude"


def research_field(implementers: Sequence[str]) -> Any:
    from scripts import team_launcher as launcher

    return launcher.Field(
        name="research",
        kind=launcher.KIND_SINGLE,
        title="Research stage (optional)",
        description=(
            "Lets the Director run research spikes on the board: a ticket with a question, a timebox and what a "
            "usable answer looks like, a finding whose evidence Audit reviews, and the finding handed back to the "
            "Director. A Researcher runs as one more model session, which is an additional cost."
        ),
        choices=(
            launcher.Choice("off", "off", "no research stage"),
            *(launcher.Choice(f"role:{name}", name, f"research done by {name}; no extra session or cost")
              for name in implementers),
            launcher.Choice("researcher", "researcher", "research done by a Researcher, as one more model session"),
        ),
        default="off",
    )


def prompt_research(
    implementers: Sequence[str],
    *,
    runner: Callable[..., Any] | None,
    owner_args: Sequence[str],
    unverified_because: str,
    input_func: Callable[[str], str],
    print_func: Callable[[str], None],
) -> Any:
    """The stage's selection for the role plan, or None for off. Called inside the owner-catalog guard."""
    from scripts import team_launcher as launcher

    try:
        answer = launcher.terminal_select.select_one(research_field(implementers), input_func=input_func,
                                                     print_func=print_func)
    except launcher.terminal_select.Cancelled:
        raise SystemExit("switchyard: too many invalid answers for the research stage") from None
    if isinstance(answer, str) and answer.startswith("role:"):
        return launcher.RoleSelection(STAGE_MARKER, answer.split(":", 1)[1])
    if answer != "researcher":
        return None
    if RESEARCHER_ROLE in implementers:
        raise SystemExit(f"switchyard: an implementer is named {RESEARCHER_ROLE!r}, which the Researcher needs; "
                         "rename it, or let that implementer do the research")
    return launcher._prompt_role_runtime_plan(
        RESEARCHER_ROLE, default_cli=RESEARCHER_DEFAULT_CLI, configured=launcher.RoleSelection(RESEARCHER_ROLE, ""),
        runner=runner, owner_args=owner_args,
        unverified_because=unverified_because, input_func=input_func, print_func=print_func,
    )


def split_role_plan(role_plan: Sequence[Any]) -> tuple[tuple[Any, ...], tuple[tuple[str, str], ...]]:
    """The roles to create, and the stage's record, out of one guided plan."""
    roles = tuple(entry for entry in role_plan if entry.role not in (STAGE_MARKER, RESEARCHER_ROLE))
    chosen = next((entry for entry in role_plan if entry.role in (STAGE_MARKER, RESEARCHER_ROLE)), None)
    if chosen is None:
        return roles, ()
    if chosen.role == STAGE_MARKER:
        return roles, (("stage", "role"), ("role", chosen.cli))
    values = {"runtime": chosen.cli, "model": chosen.model, "effort": chosen.effort}
    return roles, (("stage", "researcher"), *((key, values[key]) for key in RESEARCHER_KEYS if values[key]))


def stage(research: Sequence[tuple[str, str]]) -> str:
    return dict(research).get("stage", "off")


def with_researcher_cli(role_clis: Sequence[tuple[str, str]],
                        research: Sequence[tuple[str, str]]) -> Sequence[tuple[str, str]]:
    """The role/CLI pairs with a Researcher's runtime among them, for the checks every runtime goes through."""
    if stage(research) != "researcher":
        return role_clis
    return (*role_clis, (RESEARCHER_ROLE, dict(research)["runtime"]))


def settled(role_clis: Sequence[tuple[str, str]], research: Sequence[tuple[str, str]], *,
            print_func: Callable[[str], None] = print) -> tuple[Sequence[tuple[str, str]], tuple[tuple[str, str], ...]]:
    """Split the CLI gate's answer back into the roles to create and the stage's record (as the Librarian's is)."""
    from scripts import team_launcher as launcher

    if stage(research) != "researcher":
        return role_clis, tuple(research)
    roles = tuple(pair for pair in role_clis if pair[0] != RESEARCHER_ROLE)
    runtime = next(cli for role, cli in role_clis if role == RESEARCHER_ROLE)
    chosen = dict(research)
    if runtime == chosen["runtime"]:
        return roles, tuple(research)
    effort = chosen.get("effort", "") if launcher.runtime_catalog.runtime_takes_effort(runtime) else ""
    print_func(f"switchyard: the Researcher now runs on {runtime} instead of {chosen['runtime']}; its model is "
               f"{runtime}'s default until `switchyard set-role-runtime <project> {RESEARCHER_ROLE}` chooses one")
    return roles, (("stage", "researcher"), *((k, v) for k, v in (("runtime", runtime), ("effort", effort)) if v))


def artifact_record(research: Sequence[tuple[str, str]]) -> dict[str, str] | None:
    """`project.research` as the design artifact holds it; None is off, written as no field at all."""
    return dict(research) if stage(research) != "off" else None


def record_research_choice(artifact_path: Path, research: Sequence[tuple[str, str]], *, owner_user: str = "") -> None:
    """Write a settled research choice back into an existing design artifact, if it changed."""
    from scripts.router_onboarding import _read, _rewrite

    artifact = _read(artifact_path, owner_user)
    project = artifact.get("project") or {}
    wanted = artifact_record(research)
    if project.get("research") == wanted or (wanted is None and stage(
            tuple((project.get("research") or {}).items())) == "off"):
        return
    if wanted is None:
        project.pop("research", None)
    else:
        project["research"] = wanted
    _rewrite(artifact_path, artifact, owner_user=owner_user)


def researcher_slot(role_clis: Sequence[tuple[str, str]], documentation: Sequence[tuple[str, str]] = ()) -> int | None:
    """The display slot a Researcher takes: the next one after the roles and a Librarian, or None when all six are
    taken."""
    from scripts.ticket_board.documentation_gate import VISIBLE_SLOTS

    roles = len(dict.fromkeys(role for role, _cli in role_clis))
    roles += 1 if dict(documentation).get("gate") == "librarian" else 0
    return roles if roles < VISIBLE_SLOTS else None


def summary_line(research: Sequence[tuple[str, str]], *, slot: int | None = None, project: str = "<project>") -> str:
    """What `switchyard new` says about the research stage before it creates anything."""
    chosen = dict(research)
    if stage(research) == "off":
        return "switchyard: Research stage: off -- no research stage; no Researcher session or cost"
    if stage(research) == "role":
        return (f"switchyard: Research stage: on, research done by {chosen['role']} -- the Director can run research "
                "tickets; no Researcher session or cost")
    parts = [chosen["runtime"], *([chosen["model"]] if chosen.get("model") else []),
             *([f"effort {chosen['effort']}"] if chosen.get("effort") else [])]
    where = (f"it has a pane of its own, display slot {slot}" if slot is not None else
             f"all six display slots are taken, so it runs in the background (`switchyard attach {project} "
             f"{RESEARCHER_ROLE}` shows it)")
    return (f"switchyard: Research stage: on, research done by a Researcher ({' -> '.join(parts)}) -- one more model "
            f"session; {where}")


def artifact_research(raw: object, *, path: Path, role_names: Sequence[str],
                      implementers: Sequence[str]) -> tuple[tuple[str, str], ...]:
    """`project.research` out of a design artifact. Absent, null or `{"stage": "off"}` is off."""
    from scripts import team_launcher as launcher

    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise SystemExit(f"{path} field 'project.research' must be a JSON object such as {{\"stage\": \"off\"}}")
    unknown = sorted(set(raw) - _RECORD_KEYS)
    if unknown:
        raise SystemExit(f"{path} field 'project.research' contains unknown key {unknown[0]!r}")
    for key in _RECORD_KEYS:
        if key in raw and not isinstance(raw[key], str):
            raise SystemExit(f"{path} field 'project.research.{key}' must be a JSON string")
    chosen = raw.get("stage")
    if chosen not in STAGES:
        raise SystemExit(f"{path} field 'project.research.stage' must be one of {', '.join(STAGES)}")
    if chosen == "off":
        if set(raw) - {"stage"}:
            raise SystemExit(f"{path} gives an off research stage a role or runtime; remove them")
        return ()
    if chosen == "role":
        if set(raw) - {"stage", "role"} or raw.get("role") not in implementers:
            raise SystemExit(f"{path} field 'project.research.role' names the implementer that researches, one of "
                             f"{', '.join(implementers) or 'none'}; a runtime, model or effort is a Researcher's")
        return (("stage", "role"), ("role", raw["role"]))
    if "role" in raw:
        raise SystemExit(f"{path} gives the Researcher a role; it is its own role, named {RESEARCHER_ROLE}")
    runtime = launcher._validate_new_project_cli(str(raw.get("runtime") or ""),
                                                 context=f"{path} project.research.runtime")
    if RESEARCHER_ROLE in role_names:
        raise SystemExit(f"{path} names a role {RESEARCHER_ROLE!r}, which the Researcher needs; rename it or let an "
                         "implementer research")
    values = {"runtime": runtime, "model": str(raw.get("model") or "").strip(),
              "effort": str(raw.get("effort") or "").strip()}
    if values["effort"] and not launcher.runtime_catalog.runtime_takes_effort(runtime):
        raise SystemExit(f"{path} gives the Researcher an effort, which {runtime} does not take")
    return (("stage", "researcher"), *((key, values[key]) for key in RESEARCHER_KEYS if values[key]))


def plan_with_research_stage(
    plan: Any,
    *,
    research: Sequence[tuple[str, str]],
    role_clis: Sequence[tuple[str, str]],
    repository: Path,
    print_func: Callable[[str], None] = print,
) -> Any:
    """`plan` declaring the research stage: its own workflow (Router's and the gate's included), or its rows'."""
    from scripts.router_onboarding import plan_document
    from scripts.ticket_board.documentation_gate import free_visible_slot
    from scripts.ticket_board.research_stage import with_research_stage
    from scripts.ticket_board.workflow_config import validate

    chosen = dict(research)
    document = plan.workflow if plan.workflow is not None else plan_document(
        plan, role_clis=role_clis, repository=repository, label="Research stage", print_func=print_func)
    researcher = ({"runtime": chosen["runtime"], "template_role": plan.implementer_roles[0],
                   "slot": free_visible_slot(document)} if stage(research) == "researcher" else None)
    if researcher is not None:
        print_func(f"switchyard: the Researcher takes display slot {researcher['slot']}"
                   if researcher["slot"] is not None
                   else "switchyard: all six display slots are taken, so the Researcher runs in the background")
    try:
        document = with_research_stage(document, role=chosen.get("role") if stage(research) == "role" else None,
                                       researcher=researcher)
    except ValueError as exc:
        raise SystemExit(f"switchyard: {exc}") from None
    return dataclasses.replace(plan, workflow=validate(document, project=plan.project))


def record_researcher_runtime(config_path: Path, *, research: Sequence[tuple[str, str]], owner_user: str = "") -> None:
    """A Researcher's own model and effort on its projected launcher entry, not its template's."""
    from scripts.router_onboarding import _read, _rewrite

    if stage(research) != "researcher":
        return
    chosen = dict(research)
    payload = _read(config_path, owner_user)
    entry = next((role for role in payload.get("roles", []) if role.get("role") == RESEARCHER_ROLE), None)
    if entry is None:
        raise SystemExit(f"switchyard: {config_path} has no projected {RESEARCHER_ROLE} role; nothing was started")
    for key in ("model", "effort"):
        entry.pop(key, None)
        if chosen.get(key):
            entry[key] = chosen[key]
    _rewrite(config_path, payload, owner_user=owner_user)
