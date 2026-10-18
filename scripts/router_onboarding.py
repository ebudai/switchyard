"""Router as a choice made when a project is created (SYRD-599, User UAT kick-back).

Router is optional and costs a model session, so `switchyard new` offers it as
one plainly labelled menu choice, default **off**:

* **Asked** after the implementers are chosen, and only when there are at
  least two of them. With one implementer every ticket already has its owner,
  so the menu is not shown; the operator is told why and Router stays off.
  When turned on, Router's runtime, model and effort are chosen through the
  same guided path as every other role's (`_prompt_role_runtime_plan`), read
  as the owner account under the same guard.
* **Recorded** in the project design artifact as `project.router`:
  `{"enabled": true, "runtime": ..., "model": ..., "effort": ...}`. A design
  written by hand may say `{"enabled": false}`; an artifact without the field,
  which is every one written before it existed, means off. Preview, apply and
  a retry from the artifact (`switchyard new --from`) all read this one
  record, so none of them can ask differently or turn Router on by itself.
* **Applied** by `new_project_command`. A project with Router off is
  provisioned exactly as before: no Router role, no transition, no pane or
  session, no cost. A project with Router on starts with a declared
  workflow: the one its plan describes (composed by `legacy_workflow` the way
  `switchyard migrate-workflow` composes it, or the operator's own
  `--workflow-config`), with Router added by the audited
  `router_setup.with_router`. Root records that document like any declared
  workflow, so `resume-provision` rebuilds the same board. The launcher entry
  is projected from it, detached, and given Router's chosen model and effort
  rather than its template's; the ordinary start then launches it and reports
  its session record with the other roles'.

An upgrade never reads the design artifact, so a release that supports Router
never turns it on for an existing project; `switchyard router-role enable`
remains how an existing project chooses it.
"""

from __future__ import annotations

import dataclasses
import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any, Callable, Sequence

from scripts.ticket_board.router_role import ROUTER_ROLE

#: The runtime the menu offers first. Router's work is short and narrow; the
#: model list that follows is where a low-cost choice is made.
ROUTER_DEFAULT_CLI = "claude"
ROUTER_KEYS = ("runtime", "model", "effort")
_RECORD_KEYS = frozenset({"enabled", *ROUTER_KEYS})


def router_field() -> Any:
    from scripts import team_launcher as launcher

    return launcher.Field(
        name="router",
        kind=launcher.KIND_SINGLE,
        title="Router (optional)",
        description=(
            "Router assigns each ticket the Director admits to Implementation to one of your "
            "implementers, or hands it back. It runs as one more model session, on a low-cost "
            "model you choose next, and that session is an additional cost."
        ),
        choices=(
            launcher.Choice("off", "off", "the Director assigns implementation work; no Router session, no extra cost"),
            launcher.Choice("on", "on", "Router assigns implementation work, as one more low-cost model session"),
        ),
        default="off",
    )


def prompt_router(
    implementers: Sequence[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] | None,
    owner_args: Sequence[str],
    unverified_because: str,
    input_func: Callable[[str], str],
    print_func: Callable[[str], None],
) -> Any:
    """Router's selection, or None for off. Called by the role plan, inside its owner-catalog guard."""
    from scripts import team_launcher as launcher

    if len(implementers) < 2:
        print_func(
            f"switchyard: Router is not offered: with one implementer ({', '.join(implementers)}) every "
            "ticket already has its owner, so Router would add a model session and nothing else. It stays off."
        )
        return None
    try:
        answer = launcher.terminal_select.select_one(router_field(), input_func=input_func, print_func=print_func)
    except launcher.terminal_select.Cancelled:
        raise SystemExit("switchyard: too many invalid answers for Router") from None
    if answer != "on":
        return None
    # Low effort is offered first: Router reads one ticket and makes one choice.
    return launcher._prompt_role_runtime_plan(
        ROUTER_ROLE, default_cli=ROUTER_DEFAULT_CLI, configured=launcher.RoleSelection(ROUTER_ROLE, "", effort="low"),
        runner=runner, owner_args=owner_args,
        unverified_because=unverified_because, input_func=input_func, print_func=print_func,
    )


def router_pairs(selection: Any) -> tuple[tuple[str, str], ...]:
    """A Router selection as the design record holds it: () is off."""
    if selection is None:
        return ()
    values = {"runtime": selection.cli, "model": selection.model, "effort": selection.effort}
    return tuple((key, values[key]) for key in ROUTER_KEYS if values[key])


def split_role_plan(role_plan: Sequence[Any]) -> tuple[tuple[Any, ...], tuple[tuple[str, str], ...]]:
    """The roles to create, and Router's record, out of one guided plan."""
    roles = tuple(entry for entry in role_plan if entry.role != ROUTER_ROLE)
    router = next((entry for entry in role_plan if entry.role == ROUTER_ROLE), None)
    return roles, router_pairs(router)


def with_router_cli(role_clis: Sequence[tuple[str, str]], router: Sequence[tuple[str, str]]) -> Sequence[tuple[str, str]]:
    """The role/CLI pairs with Router's runtime among them, for the CLI checks every runtime goes through."""
    return (*role_clis, (ROUTER_ROLE, dict(router)["runtime"])) if router else role_clis


def settled(role_clis: Sequence[tuple[str, str]], router: Sequence[tuple[str, str]], *,
            print_func: Callable[[str], None] = print) -> tuple[Sequence[tuple[str, str]], tuple[tuple[str, str], ...]]:
    """Split the CLI gate's answer back into the roles to create and Router's record.

    When the operator switched Router's runtime there, its model belonged to the
    old runtime, so it is dropped (the new runtime's default, chosen again with
    `switchyard set-role-runtime`), and so is an effort the new runtime does not take.
    """
    from scripts import team_launcher as launcher

    if not router:
        return role_clis, tuple(router)
    roles = tuple(pair for pair in role_clis if pair[0] != ROUTER_ROLE)
    runtime = next(cli for role, cli in role_clis if role == ROUTER_ROLE)
    chosen = dict(router)
    if runtime == chosen["runtime"]:
        return roles, tuple(router)
    effort = chosen.get("effort", "") if launcher.runtime_catalog.runtime_takes_effort(runtime) else ""
    print_func(f"switchyard: Router now runs on {runtime} instead of {chosen['runtime']}; its model is {runtime}'s "
               "default until `switchyard set-role-runtime <project> router` chooses one")
    return roles, tuple((key, value) for key, value in (("runtime", runtime), ("effort", effort)) if value)


def record_router_choice(artifact_path: Path, router: Sequence[tuple[str, str]], *, owner_user: str = "") -> None:
    """Write a settled Router back into an existing design artifact, if it changed."""
    from scripts import team_launcher as launcher

    artifact = _read(artifact_path, owner_user)
    project = artifact.get("project") or {}
    wanted = {"enabled": True, **dict(router)} if router else None
    if project.get("router") == wanted or (wanted is None and not (project.get("router") or {}).get("enabled")):
        return
    if wanted is None:
        project.pop("router", None)
    else:
        project["router"] = wanted
    _rewrite(artifact_path, artifact, owner_user=owner_user)


def summary_line(router: Sequence[tuple[str, str]]) -> str:
    """What `switchyard new` says about Router before it creates anything."""
    if not router:
        return "switchyard: Router: off -- the Director assigns implementation work; no Router session or cost"
    chosen = dict(router)
    parts = [chosen["runtime"], *([chosen["model"]] if chosen.get("model") else []),
             *([f"effort {chosen['effort']}"] if chosen.get("effort") else [])]
    return (f"switchyard: Router: on ({' -> '.join(parts)}) -- assigns implementation work as one more "
            "model session; it starts detached with the other roles")


def artifact_router(raw: object, *, path: Path, implementer_roles: Sequence[str]) -> tuple[tuple[str, str], ...]:
    """`project.router` out of a design artifact. Absent, null or disabled is off."""
    from scripts import team_launcher as launcher

    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise SystemExit(f"{path} field 'project.router' must be a JSON object such as {{\"enabled\": false}}")
    unknown = sorted(set(raw) - _RECORD_KEYS)
    if unknown:
        raise SystemExit(f"{path} field 'project.router' contains unknown key {unknown[0]!r}")
    enabled = raw.get("enabled")
    if not isinstance(enabled, bool):
        raise SystemExit(f"{path} field 'project.router.enabled' must be a JSON boolean")
    for key in ROUTER_KEYS:
        if key in raw and not isinstance(raw[key], str):
            raise SystemExit(f"{path} field 'project.router.{key}' must be a JSON string")
    if not enabled:
        return ()
    runtime = launcher._validate_new_project_cli(str(raw.get("runtime") or ""), context=f"{path} project.router.runtime")
    if len(implementer_roles) < 2:
        raise SystemExit(
            f"{path} enables Router with one implementer ({', '.join(implementer_roles)}): every ticket already "
            "has its owner, so Router would add a model session and nothing else. Set project.router.enabled to false."
        )
    if ROUTER_ROLE in implementer_roles:
        raise SystemExit(f"{path} names an implementer {ROUTER_ROLE!r}, which Router needs; rename it or leave Router off")
    values = {"runtime": runtime, "model": str(raw.get("model") or "").strip(), "effort": str(raw.get("effort") or "").strip()}
    if values["effort"] and not launcher.runtime_catalog.runtime_takes_effort(runtime):
        raise SystemExit(f"{path} gives Router an effort, which {runtime} does not take")
    return tuple((key, values[key]) for key in ROUTER_KEYS if values[key])


def plan_with_router(
    plan: Any,
    *,
    router: Sequence[tuple[str, str]],
    role_clis: Sequence[tuple[str, str]],
    repository: Path,
    print_func: Callable[[str], None] = print,
) -> Any:
    """`plan` declaring Router: its own workflow, or the one its rows describe, plus `with_router`."""
    from scripts import team_launcher as launcher
    from scripts.ticket_board import legacy_workflow
    from scripts.ticket_board.project_provision import project_workflow_stages, project_workflow_transitions
    from scripts.ticket_board.router_setup import with_router
    from scripts.ticket_board.workflow_config import validate

    chosen = dict(router)
    document = plan.workflow
    if document is None:
        role_defs: list[tuple[str, str]] = []
        for role, cli in role_clis:  # each role once, as the launcher config lists them
            if role not in dict(role_defs):
                role_defs.append((role, cli))
        # Exactly what each pane will register as: the launcher config's runtime,
        # target and slot for these roles, so the board serves them (SYRD-262).
        panes = {
            role: {
                "runtime": launcher._command_name(cli),
                "target": f"{plan.project}-{role}:0.0",
                "slot": index if index < launcher.MAX_VISIBLE_PANES_PER_WINDOW else None,
            }
            for index, (role, cli) in enumerate(role_defs)
        }
        canonical = legacy_workflow.load_canonical(Path(__file__).resolve().parents[1])
        try:
            composed = legacy_workflow.compose_legacy_workflow(
                plan,
                canonical=canonical,
                stage_seeds=project_workflow_stages(plan),
                transition_seeds=project_workflow_transitions(plan),
                panes=panes,
            )
        except legacy_workflow.LegacyWorkflowRefused as exc:
            raise SystemExit(f"switchyard: Router needs a declared workflow, and this project's could not be composed: {exc}") from None
        document = composed.document
        # A fresh board's schema seeds the shipped stage set. The default seed
        # replaces it wholesale; a declared document must name each stage it
        # drops, or the board refuses it as omitting one.
        document["remove_stages"] = sorted(
            {stage["name"] for stage in canonical["stages"]} - {stage["name"] for stage in document["stages"]}
        )
        print_func(f"switchyard: Router: {plan.project} starts with a declared workflow, the one its plan describes plus Router.")
        for difference in composed.differences:
            print_func(f"switchyard:   unlike an undeclared board: {difference}")
        document = launcher.seed_director_onboarding(document, repository)[0]
    template = plan.implementer_roles[0]
    try:
        document = with_router(document, runtime=chosen["runtime"], template_role=template)
    except ValueError as exc:
        raise SystemExit(f"switchyard: {exc}") from None
    return dataclasses.replace(plan, workflow=validate(document, project=plan.project))


def record_router_runtime(config_path: Path, artifact_path: Path | None, *, router: Sequence[tuple[str, str]],
                          owner_user: str = "") -> None:
    """Router's own model and effort on its projected launcher entry, not its template's.

    The declared workflow is not also written into the design artifact, whether
    composed here or given with --workflow-config: the artifact's
    `project.router` is what describes Router, and an artifact carrying a
    workflow is one `switchyard new --from` refuses, so a retry from it -- with
    the same --workflow-config, if one was given -- would fail instead of
    reusing the choice.
    """
    from scripts import team_launcher as launcher

    chosen = dict(router)
    payload = _read(config_path, owner_user)
    entry = next((role for role in payload.get("roles", []) if role.get("role") == ROUTER_ROLE), None)
    if entry is None:
        raise SystemExit(f"switchyard: {config_path} has no projected {ROUTER_ROLE} role; nothing was started")
    for key in ("model", "effort"):
        entry.pop(key, None)
        if chosen.get(key):
            entry[key] = chosen[key]
    _rewrite(config_path, payload, owner_user=owner_user)
    if artifact_path is not None:
        # Read, never first looked at by path: only a read that finds nothing there makes it absent (Audit of 94b20405).
        try:
            artifact = _read(artifact_path, owner_user)
        except FileNotFoundError:
            artifact = None
        if artifact is not None and artifact.pop("workflow", None) is not None:
            _rewrite(artifact_path, artifact, owner_user=owner_user)


def _read(path: Path, owner_user: str) -> dict[str, Any]:
    """A document about to be rewritten: in the owner's tree as root, read by descriptor, never through a link."""
    from scripts import owner_writes
    from scripts import team_launcher as launcher

    return owner_writes.read_owner_json(owner_user, path) if owner_writes.root_writes_in(owner_user, path) \
        else launcher._load_json(path)


def _rewrite(path: Path, payload: dict[str, Any], *, owner_user: str = "") -> None:
    """Replace `path` atomically, keeping the owner and mode it had.

    As root in `owner_user`'s tree, by descriptor from the owner's root, with the
    owner and mode read from a descriptor opened on it there: never a by-path
    `lstat`, `lchown` or `chmod`, each of which a link swapped in -- at the
    name or above it -- would have redirected (SYRD-611).
    """
    from scripts import owner_writes
    from scripts import team_launcher as launcher

    if owner_writes.root_writes_in(owner_user, path):
        owner_writes.rewrite_owner_file(owner_user, path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return
    before = path.lstat()
    launcher._write_json_atomic(path, payload)
    if os.geteuid() == 0:
        os.lchown(path, before.st_uid, before.st_gid)
    os.chmod(path, stat.S_IMODE(before.st_mode))
