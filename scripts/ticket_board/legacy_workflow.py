"""The declared form of a tenant that predates declared workflows.

A tenant provisioned with the `default-project` seed has its stages,
transitions and roles as TABLE ROWS -- `render_workflow_sql` emits INSERTs for
it and never calls `apply_declared_workflow` -- so `/api/workflow` answers null,
its Director keeps provisioning-scaffold onboarding, and there is no document
anywhere for `adopt-workflow` to record or `migrate-workflow` to install. Live
UAT on mefp found exactly that: the tenant plan declares no workflow (SYRD-240).

This builds that missing document, and it is careful about which half of it is
read and which half is decided.

**Read, never chosen: authority.** Every stage's owners, gate, skip target,
sign-off and terminality, and every transition's endpoints, actors and owner
scoping, come from `project_workflow_stages` / `project_workflow_transitions` --
the same generator that wrote the tenant's rows. A test projects the document
back through that generator and requires identical rows, so the document
describes the board that exists rather than one somebody preferred.

**Decided once, in a table, reviewably: declarative-only fields.** A declared
transition carries `primitive`, `require_commit`, `require_reason` and
`clear_signoffs`, and the legacy seed records none of them. `ACTION_SEMANTICS`
names each seeded action's values and where they came from:

* ``shipped`` -- identical to the one meaning that action already has in the
  shipped declared document. A test asserts the equality, so these are not
  invented; they are reused.
* ``legacy-sql`` -- read from the legacy SQL function that implements the
  action today, with any difference from that behaviour written down in
  ``differs``.

An action the seed produces and the table does not name is a refusal, not a
default. That is what stops a future seed action from arriving with a guessed
primitive.

Stage and role presentation fields (`kind`, `notify`, labels, capabilities)
come from the shipped document by name, for the same reason: they exist there
already, and a stage the shipped document has never described is refused.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .workflow_config import LEGACY_ASSIGNEE_SCOPED_OPERATIONS
except ImportError:  # pragma: no cover - direct execution
    from workflow_config import LEGACY_ASSIGNEE_SCOPED_OPERATIONS

#: How the one entry in this module that is not derived from the tenant is
#: identified in the document's `migrations` markers.
MIGRATION_MARKER = "legacy_workflow_declared"
#: The only seed this module composes a declared form for. Another seed has
#: different rows and different legacy functions behind them, and composing
#: for it would be exactly the guessing this module exists to avoid.
LEGACY_SEED = "default-project"

#: The automatic skip past a gated stage. The seed carries it as a transition
#: with no actors, but a declared workflow expresses it as the stage's own
#: `skip_to`, which the seed ALSO carries as `gate_skip_to`. Turning it into a
#: transition as well would declare the same move twice.
AUTOMATIC_ACTIONS = frozenset({"entry_gate_skip"})


@dataclass(frozen=True)
class ActionSemantics:
    """The declarative-only half of one seeded action."""

    primitive: str
    require_commit: bool
    require_reason: bool
    clear_signoffs: tuple[str, ...]
    label: str
    #: ``shipped`` or ``legacy-sql``.
    source: str
    #: How this differs from what the tenant does today, if it does at all.
    differs: str = ""


#: Every action the `default-project` seed produces, and what it means when
#: declared. See the module docstring for what each ``source`` promises.
ACTION_SEMANTICS: dict[str, ActionSemantics] = {
    # -- reused from the shipped declared document, asserted equal by a test --
    "route": ActionSemantics("move", False, False, (), "Route", "shipped"),
    "release_draft": ActionSemantics("move", False, False, (), "Release draft", "shipped"),
    "submit_to_audit": ActionSemantics("move", True, False, (), "Submit to audit", "shipped"),
    "submit_to_audit_without_commit": ActionSemantics(
        "move", False, True, (), "Complete without code", "shipped"
    ),
    "audit_sign_off": ActionSemantics("approve", False, False, (), "Audit sign-off", "shipped"),
    "audit_kick_back": ActionSemantics(
        "return", False, True, ("audit_signoff", "user_signoff"), "Audit kick-back", "shipped"
    ),
    "director_dat_sign_off": ActionSemantics("move", False, False, (), "DAT sign-off", "shipped"),
    "director_dat_kick_back": ActionSemantics(
        "return", False, True, ("audit_signoff", "user_signoff"), "DAT kick-back", "shipped"
    ),
    "user_sign_off": ActionSemantics("approve", False, False, (), "User sign-off", "shipped"),
    "mark_done": ActionSemantics("move", True, False, (), "Mark done", "shipped"),
    "cancel": ActionSemantics(
        "move", False, False, ("inspector_signoff", "audit_signoff", "user_signoff"),
        "Cancel", "shipped",
    ),
    # -- read from the legacy SQL function that implements each today --------
    # ticket_board.start_work: sets in_progress; no reason, no commit, no
    # sign-off change. Declared exactly.
    "start_work": ActionSemantics("move", False, False, (), "Start work", "legacy-sql"),
    # ticket_board.implementer_kick_back: raises on an empty reason, returns to
    # analysis/director, KEEPS commit_hash, and is allowed while blocked (the
    # legacy forward-promotion list does not include in_progress -> analysis).
    #
    # It is a `move`, and not by choice: the validator requires every `return`
    # to target the implementation stage, and this one goes BACK to triage. I
    # first mapped it to `return` and the validator refused the document, which
    # is the right answer -- a return is "back to the implementer", and this is
    # the implementer handing work back to the Director.
    #
    # As a `move` it keeps the commit and still requires its reason, exactly as
    # legacy does. The one thing that changes is the blocker: a declared move to
    # a non-parking stage is refused while an unresolved blocker stands, where
    # legacy allowed it.
    "implementer_kick_back": ActionSemantics(
        "move", False, True, (), "Kick back to triage", "legacy-sql",
        differs=(
            "refused while an unresolved blocker stands, which legacy allowed; a "
            "blocked ticket can still be parked in backlog by the Director instead"
        ),
    ),
    # ticket_board.user_reopen: reason optional, returns to analysis/director,
    # and leaves `done`. The validator requires `reopen` to leave a terminal
    # stage, so there is no alternative primitive.
    #
    # No `differs`. I first recorded "clears commit_hash, which the legacy
    # function keeps", having read the function on its own -- and the function
    # does not touch commit_hash. But a legacy trigger clears it on the way out
    # anyway, and the differential test showed both boards ending with an empty
    # hash. Declaring a difference that does not happen would have told an
    # operator their adoption changes something it does not.
    "user_reopen": ActionSemantics("reopen", False, False, (), "Reopen", "legacy-sql"),
}


#: Seed rows the legacy function for that action never performs.
#:
#: The legacy transition table does two jobs: it names who may call an action
#: from a stage (the actor check), and it lists destinations. The action
#: FUNCTIONS then hard-code where the ticket actually goes, and for a few rows
#: that is not the row's `to_stage`. Declaring such a row would give the tenant
#: a capability it has never had, so it is excluded -- by name, with the line of
#: SQL that shows it is unreachable, rather than silently dropped.
UNREACHABLE_LEGACY_ROWS: dict[tuple[str, str, str], str] = {
    ("dat", "analysis", "director_dat_kick_back"): (
        "ticket_board.director_dat_kick_back sets state = 'in_progress' unconditionally; "
        "this row only feeds the actor check"
    ),
}


class LegacyWorkflowRefused(Exception):
    """This tenant's workflow cannot be declared without inventing something."""


@dataclass(frozen=True)
class DeclaredLegacyWorkflow:
    """The document, and an honest account of everything that is not a read."""

    document: dict[str, Any]
    #: Behaviour that changes when this document is installed, one line each.
    differences: tuple[str, ...]
    #: Stages and transitions added rather than derived, one line each.
    additions: tuple[str, ...]
    #: Seed rows deliberately not declared, each with the evidence why.
    excluded: tuple[str, ...] = ()


def _canonical(canonical: Mapping[str, Any], project: str) -> dict[str, Any]:
    """The shipped document, re-projected onto this tenant's name."""
    raw = copy.deepcopy(dict(canonical))
    source = str(raw.get("project") or "")
    if source:
        raw = json.loads(json.dumps(raw).replace(f'"{source}-', f'"{project}-'))
    raw["project"] = project
    return raw


def compose_legacy_workflow(
    plan: Any,
    *,
    canonical: Mapping[str, Any],
    stage_seeds: Sequence[Any],
    transition_seeds: Sequence[Any],
    semantics: Mapping[str, ActionSemantics] = ACTION_SEMANTICS,
) -> DeclaredLegacyWorkflow:
    """Build the declared document for a tenant seeded with `default-project`.

    `stage_seeds` and `transition_seeds` are what `project_workflow_stages` /
    `project_workflow_transitions` produce for this tenant's plan -- passed in
    rather than computed here so the caller decides which plan they describe,
    and so a test can hand in exactly the rows a live board holds.
    """
    project = str(plan.project)
    shipped = _canonical(canonical, project)
    shipped_stages = {s["name"]: s for s in shipped["stages"]}
    differences: list[str] = []
    additions: list[str] = []

    # -- stages: authority from the seed, presentation from the shipped doc --
    stages: list[dict[str, Any]] = []
    for seed in stage_seeds:
        base = shipped_stages.get(seed.name)
        if base is None:
            raise LegacyWorkflowRefused(
                f"stage {seed.name!r} has no description in the shipped workflow, so its "
                "kind and notification would have to be invented"
            )
        stage = copy.deepcopy(base)
        stage.update({
            "name": seed.name,
            "label": seed.display_label,
            "owners": list(seed.owner_roles),
            "gate": seed.entry_gate_field,
            "skip_to": seed.gate_skip_to,
            "signoff": seed.exit_signoff_field,
            "terminal": bool(seed.is_terminal),
        })
        stages.append(stage)
    seeded_stage_names = {s["name"] for s in stages}
    terminal_stage_names = {s["name"] for s in stages if s["terminal"]}

    # -- transitions: endpoints and actors from the seed, meaning from the table
    transitions: list[dict[str, Any]] = []
    excluded: list[str] = []
    for seed in transition_seeds:
        if seed.action_name in AUTOMATIC_ACTIONS:
            continue
        unreachable = UNREACHABLE_LEGACY_ROWS.get(
            (seed.from_stage, seed.to_stage, seed.action_name)
        )
        if unreachable:
            excluded.append(
                f"{seed.action_name} {seed.from_stage} -> {seed.to_stage}: {unreachable}"
            )
            continue
        meaning = semantics.get(seed.action_name)
        if meaning is None:
            raise LegacyWorkflowRefused(
                f"action {seed.action_name!r} ({seed.from_stage} -> {seed.to_stage}) has no "
                "declared meaning; add it to ACTION_SEMANTICS from the code that implements "
                "it rather than letting a primitive be guessed"
            )
        primitive = meaning.primitive
        # The validator admits exactly one way out of a terminal stage: an
        # explicit reopen. Legacy routes out of `done` and `cancelled` with the
        # generic `route`, so those rows are declared as reopen -- a rule taken
        # from the validator's own definition, applied per occurrence and
        # recorded, never a primitive picked for the look of it.
        #
        # Not reported as a behavioural difference, because it is not one. I
        # first recorded "reopen clears commit_hash" here; the differential test
        # showed legacy clears it too on the way out of a terminal stage, and
        # that both boards end identically. The primitive's NAME changes; what
        # happens to the ticket does not.
        if seed.from_stage in terminal_stage_names and primitive != "reopen":
            primitive = "reopen"
        # The legacy board's authority is split. The row carries `owner_scoped`,
        # and the SERVER separately refuses these operations to anyone but the
        # assignee -- so for them the row understates the restriction, and a
        # document built from the row alone would let an implementer act on a
        # ticket that is not theirs. The differential test found this: legacy
        # refused `main cannot call start_work for ticket assigned to director`
        # and the first declared document allowed it.
        owner_scoped = bool(seed.owner_scoped) or seed.action_name in LEGACY_ASSIGNEE_SCOPED_OPERATIONS
        transitions.append({
            "from": seed.from_stage,
            "to": seed.to_stage,
            "action": seed.action_name,
            "label": meaning.label,
            "actors": list(seed.allowed_roles),
            "primitive": primitive,
            "owner_scoped": owner_scoped,
            "require_commit": meaning.require_commit,
            "require_reason": meaning.require_reason,
            "clear_signoffs": list(meaning.clear_signoffs),
            "allow_no_code": seed.action_name == "submit_to_audit_without_commit",
        })
    # -- one declared destination per gated move ---------------------------
    #
    # Legacy sign-offs do not choose a destination: `audit_sign_off` sets the
    # flag and a trigger lands the ticket by gate. So the seed lists BOTH
    # places it can end up -- `audit -> dat` and `audit -> director_review` --
    # and a declared executor handed both refuses the sign-off as ambiguous.
    # The differential test found exactly that: as first composed, mefp's Audit
    # could not sign off at all.
    #
    # The declared form of "lands by gate" is ONE transition to the gated stage,
    # whose own `gate` and `skip_to` (read from the seed, like every other
    # authority field) carry the ticket onward when the gate is off. So where a
    # destination is another destination's `skip_to`, it is that skip -- not a
    # second transition -- and it is excluded with that as the evidence.
    skip_targets = {s["name"]: s.get("skip_to") for s in stages}
    by_origin: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for tr in transitions:
        by_origin.setdefault((tr["from"], tr["action"]), []).append(tr)
    redundant: list[dict[str, Any]] = []
    for (origin, action), group in by_origin.items():
        if len(group) < 2:
            continue
        destinations = {tr["to"] for tr in group}
        for tr in group:
            carried_by = [d for d in destinations if d != tr["to"] and skip_targets.get(d) == tr["to"]]
            if carried_by:
                redundant.append(tr)
                excluded.append(
                    f"{action} {origin} -> {tr['to']}: reached by the {carried_by[0]} stage's own "
                    f"gate skip (skip_to {tr['to']}); declaring it as well would make the "
                    f"{action} ambiguous"
                )
    transitions = [tr for tr in transitions if tr not in redundant]

    for name in sorted({t["action"] for t in transitions}):
        if semantics[name].differs:
            differences.append(f"{name}: {semantics[name].differs}")
    # Not a property of any one action: where a kick-back lands when nobody
    # is on record as the implementer. Legacy
    # `ticket_kickback_target_assignee` tries the recorded implementer, then
    # the current assignee, and finally returns the literal 'ops'. A declared
    # return falls back to the implementation stage's first owner instead. The
    # two agree whenever an implementer is recorded -- which is every ticket
    # that reached review through implementation -- and the differential test
    # shows exactly that; they diverge only for one that did not.
    returns = sorted({t["action"] for t in transitions if t["primitive"] == "return"})
    if returns:
        differences.append(
            f"{', '.join(returns)}: with no implementer on record, returns to the "
            "implementation stage's first owner, where legacy returned the hard-coded "
            "'ops'; identical whenever an implementer is recorded"
        )

    # -- a parking stage, which a declared workflow must have -----------------
    #
    # The `default-project` seed has no parking stage, and the validator refuses
    # a document without one: a Director with only `cancel` has to destroy a
    # ticket to clear a queue (SYRD-92). Adding it changes no existing stage,
    # role, ticket or authority -- it is a new stage and new transitions into
    # and out of it -- so it is reported as an addition rather than hidden.
    parking = [
        s for s in stages
        if not s["terminal"] and not s["owners"] and s.get("notify", {}).get("kind") == "none"
    ]
    if not parking:
        backlog = shipped_stages.get("backlog")
        if backlog is None:
            raise LegacyWorkflowRefused(
                "this tenant has no parking stage and the shipped workflow describes none "
                "to add"
            )
        stages.insert(1 if stages and stages[0]["name"] == "draft" else 0, copy.deepcopy(backlog))
        additions.append("stage backlog: somewhere deferred work can wait (required, SYRD-92)")
        director = _control_role(plan)
        defer = next(t for t in shipped["transitions"] if t["action"] == "defer")
        route_out = next(
            t for t in shipped["transitions"]
            if t["from"] == "backlog" and t["action"] == "route"
        )
        for stage in stages:
            if stage["name"] == "backlog" or stage["terminal"]:
                continue
            transitions.append({
                **copy.deepcopy(defer), "from": stage["name"], "to": "backlog",
                "actors": [director],
            })
        revive_to = route_out["to"] if route_out["to"] in seeded_stage_names else "analysis"
        transitions.append({
            **copy.deepcopy(route_out), "from": "backlog", "to": revive_to,
            "actors": [director],
        })
        additions.append(
            f"transitions defer -> backlog from every active stage, and route backlog -> "
            f"{revive_to}, both {director}-only"
        )

    # -- roles and flags: only what the stages and transitions refer to -------
    referenced = {actor for t in transitions for actor in t["actors"]}
    referenced |= {owner for s in stages for owner in s["owners"]}
    referenced |= set(getattr(plan, "roles_in_use", ()) or ())
    roles = [copy.deepcopy(r) for r in shipped["roles"] if r["name"] in referenced | {"unassigned"}]
    # Whether a role is ACTIVE is authority, so it is read from the tenant: a
    # role that owns one of its stages or acts in one of its transitions is a
    # role this tenant runs. The shipped document marks `designer` inactive --
    # it is one tenant's choice -- and copying that made every default-project
    # tenant WITH a designer unrepresentable, since the seed gives it `draft`
    # and the validator will not let an inactive role own a stage. mefp has no
    # designer, which is why the first version passed for it and nothing else.
    for role in roles:
        if role["name"] in referenced:
            role["active"] = True
    missing_roles = sorted(referenced - {r["name"] for r in roles})
    if missing_roles:
        raise LegacyWorkflowRefused(
            f"roles {missing_roles} act in this tenant's workflow but have no description in "
            "the shipped workflow, so their kind and capabilities would have to be invented"
        )
    used_flags = {s.get("gate") for s in stages} | {s.get("signoff") for s in stages}
    used_flags |= {flag for t in transitions for flag in t["clear_signoffs"]}
    flags = {
        name: spec for name, spec in shipped.get("flags", {}).items() if name in used_flags
    }

    document = {
        "schema": shipped["schema"],
        "project": project,
        "roles": roles,
        "stages": stages,
        "transitions": transitions,
        "flags": flags,
        # Empty, deliberately. `reassign` is not a description of the workflow:
        # `apply_declared_workflow` treats it as an instruction to rewrite the
        # assignee of every live ticket in the named stage. Copying it from the
        # shipped document -- which I first did -- would have reassigned
        # tenant tickets as a side effect of declaring their workflow, which is
        # precisely a change to tickets by guesswork. The database refused it.
        "reassign": {},
        "queue": {"stage": "backlog", "assignee": "unassigned"},
        "remove_stages": [],
        "migrations": {MIGRATION_MARKER: True},
    }
    return DeclaredLegacyWorkflow(
        document=document,
        differences=tuple(differences),
        additions=tuple(additions),
        excluded=tuple(excluded),
    )


def _control_role(plan: Any) -> str:
    """The role that parks and revives work. Named, never assumed."""
    for name in ("director",):
        if name in (getattr(plan, "roles_in_use", ()) or ("director",)):
            return name
    raise LegacyWorkflowRefused("this tenant names no director to park and revive work")


def load_canonical(root: Path) -> dict[str, Any]:
    return json.loads((root / "examples" / "workflows" / "inspection.json").read_text(encoding="utf-8"))
