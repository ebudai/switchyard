"""The documentation gate as a choice made when a project is created (SYRD-624).

The gate is optional, and a Librarian to decide it costs a model session, so
`switchyard new` offers it as one plainly labelled menu choice, default **off**:

* **Asked** after the implementers (and Router), with three answers: off; a
  Director-owned gate, which creates no role and no session; or a Librarian,
  whose runtime, model and effort are then chosen through the same guided path
  as every other role's, inside the role plan's owner-catalog guard.
* **Recorded** in the project design artifact as `project.documentation`:
  `{"gate": "director"}` or `{"gate": "librarian", "runtime", "model",
  "effort"}`. `{"gate": "off"}`, or no field at all -- every artifact written
  before it existed -- is off. Preview, apply and a retry from the artifact
  (`switchyard new --from`) all read this one record.
* **Applied** by `new_project_command`. Off provisions exactly as before. On
  starts the project with a declared workflow -- its own `--workflow-config`, or
  the one its plan describes (`router_onboarding.plan_document`), after Router's
  if Router is on too -- with `documentation_gate.with_documentation_gate`
  added. A Librarian's launcher entry is projected detached and given its own
  model and effort rather than its template's.

An upgrade never reads the design artifact, so a release that ships the gate
never turns it on for an existing project; `switchyard documentation-gate
<project> enable` is how an existing project chooses it.

Inside the guided role plan the choice travels as one `RoleSelection`: the
Librarian as its own role, or `GATE_MARKER` with the Director as its runtime for
the Director-owned gate. `split_role_plan` takes it out again before any role is
created, so neither is ever a role to provision by that path.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, Callable, Sequence

from scripts.ticket_board.documentation_gate import LIBRARIAN_ROLE

#: The role plan's stand-in for "the Director decides": never a role.
GATE_MARKER = "documentation-gate"
GATES = ("off", "director", "librarian")
LIBRARIAN_KEYS = ("runtime", "model", "effort")
_RECORD_KEYS = frozenset({"gate", *LIBRARIAN_KEYS})
#: The runtime the menu offers first for a Librarian: reading and editing prose.
LIBRARIAN_DEFAULT_CLI = "claude"


def documentation_field() -> Any:
    from scripts import team_launcher as launcher

    return launcher.Field(
        name="documentation",
        kind=launcher.KIND_SINGLE,
        title="Documentation gate (optional)",
        description=(
            "Lets the Director require documentation review of a ticket before Audit: CLI help, guides, "
            "onboarding, project docs and examples are checked and, where needed, corrected in the exact "
            "commit Audit reviews. A Librarian runs as one more model session, which is an additional cost."
        ),
        choices=(
            launcher.Choice("off", "off", "no gate; the Director covers documentation in its own review"),
            launcher.Choice("director", "director", "the gate, decided by the Director; no extra session or cost"),
            launcher.Choice("librarian", "librarian", "the gate, decided by a Librarian, as one more model session"),
        ),
        default="off",
    )


def prompt_documentation(
    implementers: Sequence[str],
    *,
    runner: Callable[..., Any] | None,
    owner_args: Sequence[str],
    unverified_because: str,
    input_func: Callable[[str], str],
    print_func: Callable[[str], None],
) -> Any:
    """The gate's selection for the role plan, or None for off. Called inside the owner-catalog guard."""
    from scripts import team_launcher as launcher

    try:
        answer = launcher.terminal_select.select_one(documentation_field(), input_func=input_func, print_func=print_func)
    except launcher.terminal_select.Cancelled:
        raise SystemExit("switchyard: too many invalid answers for the documentation gate") from None
    if answer == "director":
        return launcher.RoleSelection(GATE_MARKER, "director")
    if answer != "librarian":
        return None
    if LIBRARIAN_ROLE in implementers:
        raise SystemExit(f"switchyard: an implementer is named {LIBRARIAN_ROLE!r}, which the Librarian needs; "
                         "rename it, or choose the Director-owned gate")
    return launcher._prompt_role_runtime_plan(
        LIBRARIAN_ROLE, default_cli=LIBRARIAN_DEFAULT_CLI, configured=launcher.RoleSelection(LIBRARIAN_ROLE, ""),
        runner=runner, owner_args=owner_args,
        unverified_because=unverified_because, input_func=input_func, print_func=print_func,
    )


def split_role_plan(role_plan: Sequence[Any]) -> tuple[tuple[Any, ...], tuple[tuple[str, str], ...]]:
    """The roles to create, and the gate's record, out of one guided plan."""
    roles = tuple(entry for entry in role_plan if entry.role not in (GATE_MARKER, LIBRARIAN_ROLE))
    chosen = next((entry for entry in role_plan if entry.role in (GATE_MARKER, LIBRARIAN_ROLE)), None)
    if chosen is None:
        return roles, ()
    if chosen.role == GATE_MARKER:
        return roles, (("gate", "director"),)
    values = {"runtime": chosen.cli, "model": chosen.model, "effort": chosen.effort}
    return roles, (("gate", "librarian"), *((key, values[key]) for key in LIBRARIAN_KEYS if values[key]))


def gate(documentation: Sequence[tuple[str, str]]) -> str:
    return dict(documentation).get("gate", "off")


def with_librarian_cli(role_clis: Sequence[tuple[str, str]],
                       documentation: Sequence[tuple[str, str]]) -> Sequence[tuple[str, str]]:
    """The role/CLI pairs with a Librarian's runtime among them, for the checks every runtime goes through."""
    if gate(documentation) != "librarian":
        return role_clis
    return (*role_clis, (LIBRARIAN_ROLE, dict(documentation)["runtime"]))


def settled(role_clis: Sequence[tuple[str, str]], documentation: Sequence[tuple[str, str]], *,
            print_func: Callable[[str], None] = print) -> tuple[Sequence[tuple[str, str]], tuple[tuple[str, str], ...]]:
    """Split the CLI gate's answer back into the roles to create and the gate's record.

    When the operator switched the Librarian's runtime there, its model belonged
    to the old runtime, so it is dropped, and so is an effort the new runtime
    does not take (as Router's are, `router_onboarding.settled`).
    """
    from scripts import team_launcher as launcher

    if gate(documentation) != "librarian":
        return role_clis, tuple(documentation)
    roles = tuple(pair for pair in role_clis if pair[0] != LIBRARIAN_ROLE)
    runtime = next(cli for role, cli in role_clis if role == LIBRARIAN_ROLE)
    chosen = dict(documentation)
    if runtime == chosen["runtime"]:
        return roles, tuple(documentation)
    effort = chosen.get("effort", "") if launcher.runtime_catalog.runtime_takes_effort(runtime) else ""
    print_func(f"switchyard: the Librarian now runs on {runtime} instead of {chosen['runtime']}; its model is "
               f"{runtime}'s default until `switchyard set-role-runtime <project> {LIBRARIAN_ROLE}` chooses one")
    return roles, (("gate", "librarian"), *((k, v) for k, v in (("runtime", runtime), ("effort", effort)) if v))


def artifact_record(documentation: Sequence[tuple[str, str]]) -> dict[str, str] | None:
    """`project.documentation` as the design artifact holds it; None is off, written as no field at all."""
    return dict(documentation) if gate(documentation) != "off" else None


def record_documentation_choice(artifact_path: Path, documentation: Sequence[tuple[str, str]], *,
                                owner_user: str = "") -> None:
    """Write a settled gate choice back into an existing design artifact, if it changed."""
    from scripts.router_onboarding import _read, _rewrite

    artifact = _read(artifact_path, owner_user)
    project = artifact.get("project") or {}
    wanted = artifact_record(documentation)
    if project.get("documentation") == wanted or (wanted is None and gate(
            tuple((project.get("documentation") or {}).items())) == "off"):
        return
    if wanted is None:
        project.pop("documentation", None)
    else:
        project["documentation"] = wanted
    _rewrite(artifact_path, artifact, owner_user=owner_user)


def summary_line(documentation: Sequence[tuple[str, str]]) -> str:
    """What `switchyard new` says about the gate before it creates anything."""
    chosen = dict(documentation)
    if gate(documentation) == "off":
        return "switchyard: Documentation gate: off -- no documentation stage; no Librarian session or cost"
    if gate(documentation) == "director":
        return ("switchyard: Documentation gate: on, decided by the Director -- the Director can require "
                "documentation review before Audit; no Librarian session or cost")
    parts = [chosen["runtime"], *([chosen["model"]] if chosen.get("model") else []),
             *([f"effort {chosen['effort']}"] if chosen.get("effort") else [])]
    return (f"switchyard: Documentation gate: on, decided by a Librarian ({' -> '.join(parts)}) -- one more model "
            "session; it starts detached with the other roles")


def artifact_documentation(raw: object, *, path: Path, role_names: Sequence[str]) -> tuple[tuple[str, str], ...]:
    """`project.documentation` out of a design artifact. Absent, null or `{"gate": "off"}` is off."""
    from scripts import team_launcher as launcher

    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise SystemExit(f"{path} field 'project.documentation' must be a JSON object such as {{\"gate\": \"off\"}}")
    unknown = sorted(set(raw) - _RECORD_KEYS)
    if unknown:
        raise SystemExit(f"{path} field 'project.documentation' contains unknown key {unknown[0]!r}")
    for key in _RECORD_KEYS:
        if key in raw and not isinstance(raw[key], str):
            raise SystemExit(f"{path} field 'project.documentation.{key}' must be a JSON string")
    chosen = raw.get("gate")
    if chosen not in GATES:
        raise SystemExit(f"{path} field 'project.documentation.gate' must be one of {', '.join(GATES)}")
    if chosen != "librarian":
        if set(raw) - {"gate"}:
            raise SystemExit(f"{path} gives the documentation gate a runtime, model or effort, which only a "
                             "Librarian takes; set project.documentation.gate to librarian, or remove them")
        return () if chosen == "off" else (("gate", "director"),)
    runtime = launcher._validate_new_project_cli(str(raw.get("runtime") or ""),
                                                 context=f"{path} project.documentation.runtime")
    if LIBRARIAN_ROLE in role_names:
        raise SystemExit(f"{path} names a role {LIBRARIAN_ROLE!r}, which the Librarian needs; rename it or choose "
                         "the Director-owned gate")
    values = {"runtime": runtime, "model": str(raw.get("model") or "").strip(),
              "effort": str(raw.get("effort") or "").strip()}
    if values["effort"] and not launcher.runtime_catalog.runtime_takes_effort(runtime):
        raise SystemExit(f"{path} gives the Librarian an effort, which {runtime} does not take")
    return (("gate", "librarian"), *((key, values[key]) for key in LIBRARIAN_KEYS if values[key]))


def plan_with_documentation_gate(
    plan: Any,
    *,
    documentation: Sequence[tuple[str, str]],
    role_clis: Sequence[tuple[str, str]],
    repository: Path,
    print_func: Callable[[str], None] = print,
) -> Any:
    """`plan` declaring the gate: its own workflow (Router's included), or the one its rows describe."""
    from scripts.router_onboarding import plan_document
    from scripts.ticket_board.documentation_gate import with_documentation_gate
    from scripts.ticket_board.workflow_config import validate

    chosen = dict(documentation)
    document = plan.workflow if plan.workflow is not None else plan_document(
        plan, role_clis=role_clis, repository=repository, label="Documentation gate", print_func=print_func)
    librarian = ({"runtime": chosen["runtime"], "template_role": plan.implementer_roles[0]}
                 if gate(documentation) == "librarian" else None)
    try:
        document = with_documentation_gate(document, librarian=librarian)
    except ValueError as exc:
        raise SystemExit(f"switchyard: {exc}") from None
    return dataclasses.replace(plan, workflow=validate(document, project=plan.project))


def record_librarian_runtime(config_path: Path, artifact_path: Path | None, *,
                             documentation: Sequence[tuple[str, str]], owner_user: str = "") -> None:
    """A Librarian's own model and effort on its projected launcher entry, not its template's.

    As `router_onboarding.record_router_runtime` does for Router, and for the same
    reason the composed workflow is not written into the design artifact.
    """
    from scripts.router_onboarding import _read, _rewrite

    if gate(documentation) != "librarian":
        return
    chosen = dict(documentation)
    payload = _read(config_path, owner_user)
    entry = next((role for role in payload.get("roles", []) if role.get("role") == LIBRARIAN_ROLE), None)
    if entry is None:
        raise SystemExit(f"switchyard: {config_path} has no projected {LIBRARIAN_ROLE} role; nothing was started")
    for key in ("model", "effort"):
        entry.pop(key, None)
        if chosen.get(key):
            entry[key] = chosen[key]
    _rewrite(config_path, payload, owner_user=owner_user)
    if artifact_path is not None:
        try:
            artifact = _read(artifact_path, owner_user)
        except FileNotFoundError:
            artifact = None
        if artifact is not None and artifact.pop("workflow", None) is not None:
            _rewrite(artifact_path, artifact, owner_user=owner_user)
