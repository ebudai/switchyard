#!/usr/bin/env python3
"""SYRD-37: the life of one interchangeable worker in a declared pool.

A project declares a pool -- a name, a runtime, a size -- and this module is
everything that happens to it afterwards: expanding it into the tenant's own
workflow document so the board knows each worker as a role, working out what an
operator would have to do to bring one up, starting, observing, retiring and
replacing one, and saying what a bench of N workers does to a review lane that
serves it.

Nothing here names a project, a runtime, a size or a role. Those are the
tenant's declaration; this module only knows that a pool is a set of identities
derived from one rule, and that the board, the panes, the worktrees and the
notification targets all have to agree on that rule.

The document is the durable record. A launcher config is a projection of it
(``scripts/workflow_launcher.project_roles``), so a worker that exists in the
document exists everywhere: as a valid assignee, as an owner of the stages it
works in -- which is what gives it the board's per-role serial reservation and
its own notification target -- and as a pane the launcher can start.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

#: How a worker's identity is spelled, in the one place every consumer reads it
#: from. The board role name, the tmux session, the pane target and the worktree
#: are all derived here rather than rebuilt by each caller, because the failure
#: this prevents -- a notification addressed to a pane that is not the role's --
#: is silent.
MEMBER_SEPARATOR = "-"
MEMBER_RE = re.compile(r"^(?P<pool>[a-z][a-z0-9]*(?:-[a-z0-9]+)*)-(?P<index>[1-9][0-9]*)$")


@dataclass(frozen=True)
class WorkerIdentity:
    """One worker, named the same way everywhere it appears."""

    project: str
    pool: str
    index: int
    worktree_base: Path | None = None

    @property
    def role(self) -> str:
        return f"{self.pool}{MEMBER_SEPARATOR}{self.index}"

    @property
    def label(self) -> str:
        return f"{self.pool[:1].upper()}{self.pool[1:]} {self.index}"

    @property
    def tmux_session(self) -> str:
        return f"{self.project}-{self.role}"

    @property
    def target(self) -> str:
        # `<session>:0.0` is the shape the workflow validator requires and the
        # shape the notify listener sends to. One worker, one pane, one address:
        # this is the whole of "exact notification target".
        return f"{self.tmux_session}:0.0"

    @property
    def workdir(self) -> Path | None:
        return None if self.worktree_base is None else Path(self.worktree_base) / self.role


def member_index(pool_name: str, role: str) -> int | None:
    """Which member of ``pool_name`` this role is, or None if it is not one."""
    matched = MEMBER_RE.match(str(role or "").strip())
    if matched is None or matched.group("pool") != pool_name:
        return None
    return int(matched.group("index"))


def identities(project: str, pool_name: str, indices: Iterable[int], *, worktree_base: Path | None = None):
    return [WorkerIdentity(project, pool_name, index, worktree_base) for index in indices]


# --------------------------------------------------------------------------
# The document: where a worker durably exists
# --------------------------------------------------------------------------


def _roles(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    roles = document.get("roles")
    return list(roles) if isinstance(roles, list) else []


def _stages(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    stages = document.get("stages")
    return list(stages) if isinstance(stages, list) else []


def role_named(document: Mapping[str, Any], name: str) -> dict[str, Any] | None:
    return next((role for role in _roles(document) if role.get("name") == name), None)


def template_role(document: Mapping[str, Any], pool) -> dict[str, Any] | None:
    """The existing role a worker is a copy of.

    A worker is not invented from nothing: it does the job some role in this
    tenant already does, so its capabilities and the stages it owns are taken
    from that role rather than from a table in this file. The match is on the
    pool's declared kind, among active roles, preferring one that already runs
    the pool's runtime -- and never a pool member, or expanding a pool twice
    would make the second expansion a copy of the first.
    """
    candidates = [
        role
        for role in _roles(document)
        if role.get("active") is True
        and role.get("kind") == pool.kind
        and member_index(pool.name, str(role.get("name") or "")) is None
    ]
    if not candidates:
        return None
    same_runtime = [role for role in candidates if role.get("runtime") == pool.runtime]
    return (same_runtime or candidates)[0]


def stages_owned_by(document: Mapping[str, Any], role_name: str) -> list[str]:
    return [
        str(stage.get("name"))
        for stage in _stages(document)
        if role_name in list(stage.get("owners") or [])
    ]


def declared_members(document: Mapping[str, Any], pool) -> list[tuple[int, dict[str, Any]]]:
    """Every role in the document that is a member of this pool, live or retired."""
    found: list[tuple[int, dict[str, Any]]] = []
    for role in _roles(document):
        index = member_index(pool.name, str(role.get("name") or ""))
        if index is not None:
            found.append((index, role))
    return sorted(found, key=lambda item: item[0])


def live_members(document: Mapping[str, Any], pool) -> list[str]:
    return [role["name"] for _index, role in declared_members(document, pool) if role.get("active") is True]


def retired_members(document: Mapping[str, Any], pool) -> list[str]:
    return [role["name"] for _index, role in declared_members(document, pool) if role.get("active") is not True]


def next_member_index(document: Mapping[str, Any], pool) -> int:
    """The next number a worker may take.

    Counted past every member the document has ever held, retired ones
    included. A retired worker's name is never handed to a new one: tickets,
    comments and notification traces name it, and a second worker answering to
    the first one's identity makes every one of those records ambiguous.
    """
    declared = declared_members(document, pool)
    return (max(index for index, _role in declared) + 1) if declared else 1


#: How many roles a project's window can show at once. Read from the launcher,
#: which owns the layout, rather than restated: a pool that took a slot the
#: window does not have would be refused by the document validator instead, and
#: the message would be about a number rather than about the bench.
def _visible_slot_limit() -> int:
    from scripts import team_launcher as launcher

    return launcher.MAX_VISIBLE_PANES_PER_WINDOW


def worker_role_definition(
    identity: WorkerIdentity,
    pool,
    template: Mapping[str, Any],
    *,
    onboarding_prompt: str = "",
    slot: int | None = None,
) -> dict[str, Any]:
    """One worker, as the document declares it."""
    definition: dict[str, Any] = {
        "name": identity.role,
        "label": identity.label,
        "kind": pool.kind,
        "active": True,
        "capabilities": list(template.get("capabilities") or []),
        "runtime": pool.runtime,
        "target": identity.target,
        # On-demand presentation is the absence of a slot: the projection turns
        # a slotless role into a detached session, so a worker runs with no
        # permanent pane and is attached when somebody asks to watch it. A pool
        # declared `attached` still takes no slot here -- there are at most six,
        # and a pool is usually larger -- and says so in the preflight instead
        # of failing validation at apply time.
        "slot": slot,
        # A worker is interchangeable, which is exactly what a cleared session
        # per ticket means (SYRD-135); a pool may turn it off.
        "ephemeral": bool(pool.ephemeral),
        # And it is handed one ticket at a time, in every stage it owns. The
        # board already did this for implementers; declaring it makes it the
        # worker's own property rather than a consequence of its kind.
        "serial": True,
    }
    prompt = (
        onboarding_prompt
        or str(getattr(pool, "onboarding_prompt", "") or "")
        or str(template.get("onboarding_prompt") or "")
    ).strip()
    if prompt:
        definition["onboarding_prompt"] = prompt
    return definition


@dataclass(frozen=True)
class DocumentChange:
    """One edit to the tenant's workflow document, in words an operator can check."""

    subject: str
    detail: str


def expand_pool(
    document: Mapping[str, Any],
    pool,
    *,
    project: str,
    worktree_base: Path | None = None,
    onboarding_prompt: str = "",
    count: int | None = None,
) -> tuple[dict[str, Any], list[DocumentChange]]:
    """Declare this pool's workers in the tenant's own workflow document.

    Idempotent: a worker already declared is left exactly as it is, so running
    this twice is not two pools. Returns the new document and what changed; the
    caller applies it through the supported workflow path, which is what
    journals it and gives it a rollback.
    """
    result = copy.deepcopy(dict(document))
    changes: list[DocumentChange] = []
    template = template_role(result, pool)
    if template is None:
        raise ValueError(
            f"{project} declares no active {pool.kind} role for the pool to be modelled on; "
            "a worker copies an existing role's capabilities and stages rather than inventing them"
        )
    stages = stages_owned_by(result, str(template["name"]))
    if not stages:
        raise ValueError(
            f"{template['name']} owns no stage, so a worker copied from it would own none either "
            "and the board would have nowhere to route its work"
        )
    wanted = pool.size if count is None else count
    existing_live = live_members(result, pool)
    missing = max(0, wanted - len(existing_live))
    start = next_member_index(result, pool)
    # On-demand presentation is the absence of a slot: the projection turns a
    # slotless role into a detached session. An `attached` pool asks for the
    # opposite, and a window has only so many slots -- so the ones that exist
    # are handed out here and a pool that does not fit is refused by name rather
    # than by the document validator complaining about a number.
    free_slots: list[int] = []
    if pool.presentation == "attached":
        taken = {
            role.get("slot") for role in _roles(result)
            if role.get("active") is True and isinstance(role.get("slot"), int)
        }
        free_slots = [slot for slot in range(_visible_slot_limit()) if slot not in taken]
        if len(free_slots) < missing:
            raise ValueError(
                f"{pool.name} asks for {missing} attached worker(s) and {project}'s window has "
                f"{len(free_slots)} free slot(s) of {_visible_slot_limit()}; declare the pool "
                "`on-demand` and attach a worker when somebody asks to watch it"
            )
    added: list[str] = []
    for offset in range(missing):
        identity = WorkerIdentity(project, pool.name, start + offset, worktree_base)
        result["roles"].append(
            worker_role_definition(
                identity, pool, template,
                onboarding_prompt=onboarding_prompt,
                slot=free_slots[offset] if free_slots else None,
            )
        )
        added.append(identity.role)
    if added:
        changes.append(
            DocumentChange(
                "roles",
                f"{len(added)} {pool.runtime} worker(s) declared, copied from {template['name']}: "
                + ", ".join(added),
            )
        )
    for stage in _stages(result):
        if str(stage.get("name")) not in stages:
            continue
        owners = list(stage.get("owners") or [])
        joining = [name for name in live_members(result, pool) if name not in owners]
        if not joining:
            continue
        stage["owners"] = owners + joining
        changes.append(
            DocumentChange(
                f"stage {stage['name']}",
                f"{', '.join(joining)} become owner(s), so the board will route and notify them there",
            )
        )
    # Owning the stage is only half of being able to work in it: a transition
    # names its actors, and a worker that owns `in_progress` but is not an actor
    # of the submit out of it is a worker the board hands tickets it cannot
    # move. The template is again the source -- wherever it may act, so may its
    # copies -- so nothing here names an action.
    for transition in result.get("transitions") or []:
        if not isinstance(transition, dict):
            continue
        actors = list(transition.get("actors") or [])
        if str(template["name"]) not in actors:
            continue
        joining = [name for name in live_members(result, pool) if name not in actors]
        if not joining:
            continue
        transition["actors"] = actors + joining
        changes.append(
            DocumentChange(
                f"transition {transition.get('action')}",
                f"{', '.join(joining)} may take it, the same as {template['name']}",
            )
        )
    result, admitted = serialise_review_lanes(result, pool)
    changes += admitted
    if not changes:
        changes.append(DocumentChange("roles", f"{pool.name} is already declared; nothing to add"))
    return result, changes


def serialise_review_lanes(
    document: Mapping[str, Any], pool
) -> tuple[dict[str, Any], list[DocumentChange]]:
    """Give every review lane this pool feeds a head, an order and a queue.

    Done as part of declaring the pool rather than reported and left, because a
    bench whose reviewers are not serialised is the failure this is for: eight
    tickets addressed to one reviewer, all deliverable, none of them current.
    Only roles the document calls reviewers are touched -- a control or user
    stage deliberately holds several tickets at once -- and the edit is in the
    document, so the ordinary workflow rollback undoes it with everything else.
    """
    result = copy.deepcopy(dict(document))
    changes: list[DocumentChange] = []
    for lane in review_admission(result, pool).ambiguous_lanes:
        for owner in lane.owners:
            role = role_named(result, owner)
            if role is None or role.get("kind") != REVIEWER_KIND or role.get("serial") is True:
                continue
            role["serial"] = True
            changes.append(
                DocumentChange(
                    f"stage {lane.stage}",
                    f"{owner} becomes serial: it is handed the oldest waiting ticket first and "
                    "the rest are deferred rather than delivered all at once",
                )
            )
    return result, changes


def retire_worker(
    document: Mapping[str, Any], pool, member: str
) -> tuple[dict[str, Any], list[DocumentChange]]:
    """Take one worker out of service, keeping its identity forever.

    Retirement is a deactivation, never a deletion. The name stays in the
    document so nothing can be given it again, and so the tickets, comments and
    notification traces that name it keep resolving to the worker that did the
    work.
    """
    result = copy.deepcopy(dict(document))
    role = role_named(result, member)
    if role is None or member_index(pool.name, member) is None:
        raise ValueError(f"{member} is not a member of the {pool.name} pool")
    changes: list[DocumentChange] = []
    if role.get("active") is True:
        role["active"] = False
        role["slot"] = None
        # The runtime and target are dropped together -- the validator requires
        # them to agree -- which is what frees the pane address without freeing
        # the name.
        role["runtime"] = None
        role["target"] = None
        changes.append(
            DocumentChange("roles", f"{member} is retired; its identity is kept and never reissued")
        )
    for stage in _stages(result):
        owners = list(stage.get("owners") or [])
        if member not in owners:
            continue
        stage["owners"] = [owner for owner in owners if owner != member]
        changes.append(
            DocumentChange(f"stage {stage['name']}", f"{member} stops owning it, so no work is routed there again")
        )
    for transition in result.get("transitions") or []:
        if not isinstance(transition, dict):
            continue
        actors = list(transition.get("actors") or [])
        if member not in actors:
            continue
        remaining = [actor for actor in actors if actor != member]
        if not remaining:
            # A transition needs at least one actor. A retirement that emptied
            # one would make the document invalid and the stage inescapable,
            # which is a worse outcome than a retired role keeping a permission
            # it can no longer be given work to use.
            continue
        transition["actors"] = remaining
    if not changes:
        changes.append(DocumentChange("roles", f"{member} was already retired"))
    return result, changes


def replace_worker(
    document: Mapping[str, Any],
    pool,
    member: str,
    *,
    project: str,
    worktree_base: Path | None = None,
    onboarding_prompt: str = "",
) -> tuple[dict[str, Any], list[DocumentChange]]:
    """Retire one worker and declare a fresh one, under a new identity."""
    retired, changes = retire_worker(document, pool, member)
    grown, added = expand_pool(
        retired,
        pool,
        project=project,
        worktree_base=worktree_base,
        onboarding_prompt=onboarding_prompt,
        count=len(live_members(retired, pool)) + 1,
    )
    return grown, changes + added


# --------------------------------------------------------------------------
# Review admission: what a bench does to the lanes downstream of it
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AdmissionLane:
    """One stage the pool's work leaves into, and who absorbs it there."""

    stage: str
    owners: tuple[str, ...]
    serial_owners: tuple[str, ...]

    @property
    def deterministic(self) -> bool:
        """Whether every owner here takes its tickets in a defined order."""
        return bool(self.owners) and len(self.serial_owners) == len(self.owners)


@dataclass(frozen=True)
class ReviewAdmission:
    """What happens to a pool's output once it leaves the pool."""

    pool_size: int
    lanes: tuple[AdmissionLane, ...]

    @property
    def ambiguous_lanes(self) -> tuple[AdmissionLane, ...]:
        return tuple(lane for lane in self.lanes if not lane.deterministic)


#: What makes a stage a review lane: somebody whose job in this document is to
#: review owns it. Read from the role's declared kind rather than from its name,
#: so a tenant that calls its reviewers something else is still covered, and a
#: control or user stage -- which a Director deliberately holds several tickets
#: in at once -- is not mistaken for one.
REVIEWER_KIND = "reviewer"


def _stage_named(document: Mapping[str, Any], name: str) -> dict[str, Any] | None:
    return next((stage for stage in _stages(document) if str(stage.get("name")) == name), None)


def downstream_stages(document: Mapping[str, Any], origins: Iterable[str]) -> list[str]:
    """Every stage this work can reach from ``origins``, in the order it reaches them.

    Walked transitively, because a review lane is rarely one hop away: work
    leaves the implementation stage into the first reviewer and only then into
    the second, and a pool that saturates the second is exactly as stuck as one
    that saturates the first.
    """
    seen = set(origins)
    order: list[str] = []
    frontier = list(origins)
    transitions = [t for t in (document.get("transitions") or []) if isinstance(t, Mapping)]
    while frontier:
        current = frontier.pop(0)
        for transition in transitions:
            if str(transition.get("from")) != current:
                continue
            target = str(transition.get("to") or "")
            if not target or target in seen:
                continue
            stage = _stage_named(document, target)
            if stage is None or stage.get("terminal"):
                continue
            seen.add(target)
            order.append(target)
            frontier.append(target)
    return order


def review_admission(document: Mapping[str, Any], pool) -> ReviewAdmission:
    """Which review lanes this pool's work lands in, and whether they are ordered.

    A pool is a queue with N heads; everything downstream of it is a queue with
    however many heads the document gives it. That arithmetic is the thing an
    operator has to see before running eight workers into one reviewer: not
    whether the reviewer is fast enough, but whether the board can say which
    ticket that reviewer is on. A lane every owner of which is serial has a
    head, an order, and a tail that is deferred rather than dropped. A lane
    where any owner is not has none of those, and review load there is
    ambiguous by construction.
    """
    from scripts.ticket_board.workflow_config import role_is_serial_in

    members = live_members(document, pool)
    kinds = {str(role.get("name")): str(role.get("kind") or "") for role in _roles(document)}
    origins = sorted({stage for member in members for stage in stages_owned_by(document, member)})
    lanes: list[AdmissionLane] = []
    for stage_name in downstream_stages(document, origins):
        stage = _stage_named(document, stage_name)
        owners = tuple(str(owner) for owner in (stage.get("owners") or []))
        if not any(kinds.get(owner) == REVIEWER_KIND for owner in owners):
            continue
        serial = tuple(owner for owner in owners if role_is_serial_in(document, owner, stage_name))
        lanes.append(AdmissionLane(stage_name, owners, serial))
    return ReviewAdmission(pool_size=len(members) or pool.size, lanes=tuple(lanes))


def format_review_admission(admission: ReviewAdmission) -> list[str]:
    lines: list[str] = []
    for lane in admission.lanes:
        who = ", ".join(lane.owners)
        if lane.deterministic:
            lines.append(
                f"  ordered  {lane.stage}: {who} take one ticket at a time, oldest first; "
                f"up to {admission.pool_size} of this pool's tickets wait there and each is deferred, not dropped"
            )
        else:
            unordered = ", ".join(owner for owner in lane.owners if owner not in lane.serial_owners)
            lines.append(
                f"  AMBIGUOUS {lane.stage}: {unordered} can hold several of this pool's "
                f"{admission.pool_size} tickets at once with nothing saying which is current; "
                f"declare `serial` on that role so the lane has a head"
            )
    return lines


# --------------------------------------------------------------------------
# Readiness: what has to be true before a worker is given its first ticket
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkerReadiness:
    """One worker, and everything that has to be true before it is handed work.

    Every field is a fact about a different subsystem, kept apart rather than
    collapsed into a boolean, because "not ready" is useless to an operator and
    "its worktree is missing" is not. `blockers` says, in order, what to fix.
    """

    role: str
    target: str
    declared: bool = False
    routed: tuple[str, ...] = ()
    onboarding: bool = False
    skill: str = "unknown"
    runtime: str = "unknown"
    account: bool = False
    worktree: bool = False
    session: bool = False
    holding: tuple[str, ...] = ()

    @property
    def blockers(self) -> tuple[str, ...]:
        found: list[str] = []
        if not self.declared:
            found.append("the workflow document does not declare it as an active role")
        if not self.routed:
            found.append("it owns no stage, so the board has nowhere to route its work")
        if self.skill != "present":
            found.append(f"the canonical board skill is {self.skill} for its runtime's skill tree")
        if not self.onboarding:
            found.append("it has no role onboarding prompt, so its first session starts uninstructed")
        if self.runtime != "authenticated":
            found.append(f"its runtime reports {self.runtime} for the owner account")
        if not self.account:
            found.append("the Unix account its pane runs as does not exist")
        if not self.worktree:
            found.append("its worktree is missing")
        return tuple(found)

    @property
    def ready(self) -> bool:
        return not self.blockers

    def describe(self) -> str:
        state = "running" if self.session else "stopped"
        held = f", holding {', '.join(self.holding)}" if self.holding else ""
        if self.ready:
            return f"{self.role} ({self.target}): ready, {state}{held}"
        return f"{self.role} ({self.target}): not ready, {state}{held} -- {'; '.join(self.blockers)}"


def worker_readiness(
    config,
    pool,
    *,
    document: Mapping[str, Any] | None,
    board: Mapping[str, Any] | None = None,
    owner_home: Path | None = None,
    members: Sequence[str] | None = None,
    runner=None,
) -> list[WorkerReadiness]:
    """Ask every subsystem about every worker, and change none of them.

    The order matters as much as the answers: skill and onboarding are checked
    before the session, because a worker started without them takes its first
    ticket without knowing how to read the board, and that first turn is not
    recoverable by installing the skill afterwards (SYRD-35, SYRD-36).
    """
    import subprocess

    from scripts import team_launcher as launcher
    from scripts.ticket_board import board_skill

    runner = runner or subprocess.run
    document = dict(document or {})
    names = list(members) if members is not None else live_members(document, pool)
    owner = config.run_as_user or launcher.current_user_name()
    home = Path(owner_home) if owner_home is not None else launcher._owner_home_for_auth(owner)

    # "Is a Switchyard-managed copy of the board skill discoverable by this
    # runtime, under the account the panes run as." Not which commit it came
    # from: a worker reading a slightly older copy of the skill still knows how
    # to read the board, and refusing to start it over that would be stricter
    # than the property this gate exists to hold.
    skill_state = "unknown"
    if names:
        results = {
            result.runtime: result
            for result in board_skill.verify_board_skill(home=home, expected_commit="")
        }
        found = results.get(pool.runtime)
        skill_state = found.action if found is not None else "not a runtime the skill installs into"

    runtime_state = launcher._cli_auth_status(
        pool.runtime, owner_user=owner, owner_home=home, runner=runner
    ) if names else "unknown"

    queues = {}
    if board is not None:
        from scripts.ticket_board.read_client import queue_payload

        queues = queue_payload(dict(board), None)

    readiness: list[WorkerReadiness] = []
    for name in names:
        role = role_named(document, name) or {}
        identity = WorkerIdentity(config.project, pool.name, member_index(pool.name, name) or 0, config.worktree_base)
        configured = next((candidate for candidate in config.roles if candidate.role == name), None)
        workdir = Path(configured.workdir) if configured is not None else identity.workdir
        pane_user = launcher.role_run_as_user(config, configured) if configured is not None else owner
        session_alive = False
        if configured is not None:
            role_runner = launcher.role_process_runner_for(config, configured, runner=runner)
            probe = role_runner(
                launcher.tmux_has_session_args(configured),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            session_alive = probe.returncode == 0
        readiness.append(
            WorkerReadiness(
                role=name,
                target=str(role.get("target") or identity.target),
                declared=role.get("active") is True,
                routed=tuple(stages_owned_by(document, name)),
                onboarding=bool(str(role.get("onboarding_prompt") or "").strip() or role.get("onboarding")),
                skill=skill_state,
                runtime=runtime_state,
                account=bool(pane_user) and launcher.local_account_exists(pane_user),
                worktree=workdir is not None and Path(workdir).is_dir(),
                session=session_alive,
                holding=tuple(str(ticket.get("id") or "") for ticket in queues.get(name, [])),
            )
        )
    return readiness


# --------------------------------------------------------------------------
# The upgrade: an ordered plan, each step with the way back out of it
# --------------------------------------------------------------------------


#: How a privileged step is run so that its output, exit status and operator are
#: kept where root keeps them, instead of in somebody's scrollback (SYRD-128).
ROLLOUT_WRAPPER = "switchyard-record-rollout"


#: Which step clears which preflight blocker, by the subject the preflight
#: named. A blocker with no entry here is one no step addresses, and is reported
#: as a blocker on the plan too -- fail closed, so a subject added to the
#: preflight later cannot quietly read as "the plan handles it" (SYRD-37).
BLOCKER_CLEARED_BY = {
    "runtime": "provider first run",
    "board": "board registration",
    "board skill": "board skill",
    # The holding destination is part of the document the apply writes -- but
    # only if the operator put it there, which is why the plan names it on that
    # step rather than promising it.
    "queue": "workflow apply",
}


@dataclass(frozen=True)
class PlanStep:
    """One thing that has to happen, in the order it has to happen in."""

    name: str
    detail: str
    command: tuple[str, ...] = ()
    rollback: str = ""
    privileged: bool = False
    #: Something the operator has to resolve before the plan can run at all, as
    #: opposed to a step the plan performs. Kept in the same list and in the
    #: same order, because an operator reading a plan needs to see the blocker
    #: where it actually stops them.
    blocking: bool = False

    def journalled(self, project: str, *, label: str = "", target_commit: str = "") -> tuple[str, ...]:
        if not self.command or not self.privileged:
            return self.command
        prefix = [ROLLOUT_WRAPPER, project, "--label", label or self.name]
        if target_commit:
            prefix += ["--target-commit", target_commit]
        return tuple(prefix + ["--"] + list(self.command))


def upgrade_plan(
    config,
    pool,
    *,
    document: Mapping[str, Any] | None,
    readiness: Sequence[WorkerReadiness] = (),
    blockers: Sequence[tuple[str, str]] = (),
    config_path: Path | None = None,
    workflow_path: Path | None = None,
) -> list[PlanStep]:
    """Everything between a declared pool and a working one, in order.

    Two shapes, chosen by what the tenant's board already is rather than by a
    switch: a board running a declared workflow gains its workers in one
    reviewed document apply, which is also what serialises the review lane and
    what the rollback journal reverses. A board still running the built-in
    workflow gains them one at a time through `add-role`, which is the supported
    registration path for a tenant with no document -- and cannot serialise a
    review lane, because that is a property of a document it does not have.
    That difference is stated as a step rather than hidden, because it is the
    difference between a bench whose review queue has a head and one whose does
    not.
    """
    project = config.project
    steps: list[PlanStep] = []
    members = live_members(dict(document or {}), pool) if document else []
    declared = bool(document)
    config_arg = str(config_path) if config_path is not None else f"<{project} launcher config>"
    workflow_arg = str(workflow_path) if workflow_path is not None else f"<reviewed {project} workflow document>"

    steps.append(
        PlanStep(
            "preflight",
            "read every check again and change nothing; the plan below assumes it reports no blocker",
            command=("switchyard", "worker-pool", project),
            rollback="nothing to undo; it writes nothing",
        )
    )
    if declared:
        steps.append(
            PlanStep(
                "workflow dry run",
                f"validate the expanded document against the running board without writing it",
                command=("ticket-board-workflow", "apply", "--document", workflow_arg,
                         "--config", config_arg, "--dry-run"),
                rollback="nothing to undo; --dry-run writes nothing",
            )
        )
        steps.append(
            PlanStep(
                "workflow apply",
                f"declare {len(members) or pool.size} worker(s) and serialise the review lane(s) they feed, "
                "in one reviewed document; the board write and the launcher projection move together",
                command=("ticket-board-workflow", "apply", "--document", workflow_arg, "--config", config_arg),
                rollback=(
                    "ticket-board-workflow rollback --journal <the journal this step printed> "
                    f"--config {config_arg}"
                ),
            )
        )
    else:
        steps.append(
            PlanStep(
                "board registration",
                f"{project}'s board runs the built-in workflow, so each worker is registered "
                "through the supported add-role path, one privileged run per worker",
                command=("switchyard", "add-role", project, f"<{pool.name}-N>",
                         "--cli", pool.runtime, "--detached"),
                rollback=(
                    "each add-role run is in the rollout journal; a worker is withdrawn by "
                    "removing its role from the launcher config and re-running "
                    f"`switchyard upgrade {project}`"
                ),
                privileged=True,
            )
        )
        steps.append(
            PlanStep(
                "review admission",
                "a board with no declared workflow cannot serialise its review lane: the reviewer "
                f"will be assigned up to {pool.size} of this pool's tickets at once with nothing "
                "saying which is current. Migrating this tenant to a declared workflow is what "
                "fixes that, and it is a separate reviewed change",
                rollback="",
                blocking=True,
            )
        )

    steps.append(
        PlanStep(
            "board skill",
            f"install the canonical board skill into the owner account's {pool.runtime} skill tree, "
            "before any worker takes a ticket",
            command=("switchyard", "board-skill", "install"),
            rollback="the installed copy carries a Switchyard provenance footer and can be deleted",
        )
    )
    steps.append(
        PlanStep(
            "provider first run",
            f"the ordinary project start, which collects {pool.runtime}'s first run once for the "
            "owner account before any pane opens; every worker in the pool inherits it, so this "
            "is one sign-in and not one per worker (SYRD-191)",
            command=("switchyard", project),
            rollback="nothing to undo; a credential is the account's, not the pool's",
        )
    )
    if declared:
        steps.append(
            PlanStep(
                "worker preparation",
                "prepare one worker's worktree, hooks and folder trust through the established "
                "per-role path, which refuses rather than starting a worker whose runtime is not "
                "signed in",
                command=("ticket-board-workflow", "prepare-role", "--config", config_arg,
                         "--role", f"<{pool.name}-N>"),
                rollback=f"`switchyard teardown {project} --dry-run` lists what a withdrawal would remove",
            )
        )
    steps.append(
        PlanStep(
            "start",
            "bring workers up on demand; a pool with on-demand presentation holds no pane until "
            "somebody asks to watch one",
            command=("switchyard", "worker-pool", project, "start", f"<{pool.name}-N>"),
            rollback=f"switchyard worker-pool {project} stop <{pool.name}-N>",
        )
    )
    # Every preflight blocker is accounted for here, one way or the other: a
    # blocker one of these steps clears is named on that step, so an operator
    # can see the plan answers it; a blocker no step clears is a blocker on the
    # plan as well, because running the sequence would not make the pool work.
    # Silence about either would be the plan and the preflight disagreeing.
    names = {step.name for step in steps}
    for subject, detail in blockers:
        clearing = BLOCKER_CLEARED_BY.get(subject)
        if clearing == "board registration" and "workflow apply" in names:
            clearing = "workflow apply"
        if clearing in names:
            steps = [
                replace(step, detail=f"{step.detail}. Clears: {detail}")
                if step.name == clearing else step
                for step in steps
            ]
            continue
        steps.append(PlanStep(f"blocker: {subject}", detail, rollback="", blocking=True))
    for state in readiness:
        for blocker in state.blockers:
            steps.append(PlanStep(f"blocker: {state.role}", blocker, rollback="", blocking=True))
    return steps


def format_plan(project: str, steps: Sequence[PlanStep], *, target_commit: str = "") -> list[str]:
    blocking = [step for step in steps if step.blocking]
    lines = [
        f"switchyard: worker pool plan for {project}: "
        f"{len(steps) - len(blocking)} step(s), {len(blocking)} blocker(s)"
    ]
    order = 0
    for step in steps:
        if step.blocking:
            lines.append(f"  BLOCKER  {step.name}: {step.detail}")
            continue
        order += 1
        lines.append(f"  {order:>2}. {step.name}: {step.detail}")
        command = step.journalled(project, target_commit=target_commit)
        if command:
            lines.append(f"      run:      {' '.join(command)}")
        if step.rollback:
            lines.append(f"      undo:     {step.rollback}")
    lines.append(
        "switchyard: this printed a plan. Nothing above has been run, and no role, account, "
        "worktree, board registration or session was created."
    )
    return lines


# --------------------------------------------------------------------------
# Running one worker: start, stop, restart, and the gate in front of all three
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkerAction:
    role: str
    action: str
    detail: str

    def describe(self) -> str:
        return f"switchyard: {self.role} {self.action}: {self.detail}"


def start_worker(
    config,
    pool,
    member: str,
    *,
    readiness: Sequence[WorkerReadiness],
    config_path: Path,
    script_path: Path | None = None,
    pane_state_dir: Path | None = None,
    force: bool = False,
    runner=None,
) -> WorkerAction:
    """Bring one worker up, refusing while anything it needs is missing.

    The refusal is the point. A worker started before the board skill and its
    onboarding prompt exist takes its first ticket without knowing how to read
    the board, and no amount of installing them afterwards gets that turn back
    (SYRD-35, SYRD-36). `force` exists for an operator who has a reason and
    says so; it does not exist so the caller can skip the check quietly.
    """
    import subprocess

    from scripts import team_launcher as launcher

    runner = runner or subprocess.run
    state = next((candidate for candidate in readiness if candidate.role == member), None)
    if state is None:
        raise ValueError(f"{member} is not a live member of the {pool.name} pool")
    if state.blockers and not force:
        return WorkerAction(member, "not started", "; ".join(state.blockers))
    if state.session:
        return WorkerAction(member, "already running", f"its session is live at {state.target}")
    role = launcher._role_by_name(config, member)
    pane_user = launcher.role_run_as_user(config, role)
    args = launcher.pane_command_args(
        config.project,
        role,
        config_path=config_path,
        mode="attach-or-start",
        script_path=script_path or config.pane_launcher or Path(launcher.__file__).with_name(launcher.TEAM_LAUNCHER_NAME),
        pane_state_dir=pane_state_dir or launcher.default_pane_state_dir_for_user(pane_user, project=config.project),
        skip_launcher_check=True,
        no_attach=True,
        run_as_user=pane_user,
    )
    result = runner(args)
    if result.returncode != 0:
        return WorkerAction(member, "failed to start", f"the pane command exited {result.returncode}")
    forced = " (forced past its blockers)" if state.blockers else ""
    return WorkerAction(member, "started", f"session {role.tmux_session}{forced}")


def stop_worker(config, member: str, *, runner=None) -> WorkerAction:
    """End one worker's session, leaving its identity and its work where they are.

    Stopping is not retiring: the role stays declared, the board keeps routing
    to it, and anything it holds is still its. That separation is what makes
    restart and replacement mean different things.
    """
    import subprocess

    from scripts import team_launcher as launcher

    runner = runner or subprocess.run
    role = launcher._role_by_name(config, member)
    role_runner = launcher.role_process_runner_for(config, role, runner=runner)
    probe = role_runner(
        launcher.tmux_has_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    if probe.returncode != 0:
        return WorkerAction(member, "already stopped", "it has no live session")
    result = role_runner(
        launcher.tmux_kill_session_args(role), stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    if result.returncode != 0:
        return WorkerAction(member, "failed to stop", f"tmux kill-session exited {result.returncode}")
    return WorkerAction(member, "stopped", f"session {role.tmux_session} is gone; its identity is not")


def restart_worker(config, pool, member: str, **kwargs) -> list[WorkerAction]:
    """Stop and start one worker, which is how a stuck runtime is recovered.

    Deliberately two recorded actions rather than one: a restart whose stop
    succeeded and whose start did not is a stopped worker, and saying
    "restarted" over that would be the report disagreeing with the machine.
    """
    runner = kwargs.get("runner")
    stopped = stop_worker(config, member, runner=runner)
    started = start_worker(config, pool, member, **kwargs)
    return [stopped, started]
