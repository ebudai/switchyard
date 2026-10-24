"""The declared-workflow transition rules, in Python and independent of any database (SYRD-642).

PostgreSQL boards decide a transition inside the database
(`ticket_board.enforce_declared_ticket_update` and `perform_workflow_action_as`).
A board on another backend -- Oracle first -- decides it here, from the same
declared document, and its store only applies the result atomically. The rules
are the PostgreSQL trigger's, read the same way, using the same Python
helpers the board already shares with that SQL (`workflow_config`):

* the transition is the one declared from the ticket's stage for the action
  (and target, when there is more than one), and the actor must be one of its
  actors -- or, for the pull claim, a declared claimant -- and its owner when
  it is owner-scoped;
* a reason when the transition requires one; a commit when it requires one,
  unless the ticket is commit-exempt; a terminal stage is left only by reopen;
* a blocked ticket is not promoted forward: returns, reopens, parking and a
  review handing work back still move;
* `approve` grants the stage's sign-off; leaving a review forward otherwise
  needs it; `clear_signoffs` clears; a return or reopen clears the commit; a
  no-code submission records none and is commit-exempt; leaving
  implementation with a different commit clears every sign-off;
* the gate walk: from the destination, skip every stage whose gate is off, or
  whose sign-off is already given and that has a skip target; a stage entered
  starts with its own sign-off cleared;
* a return goes to the recorded implementer when they own the destination;
  otherwise the assignee is kept when they own it, else the first owner;
* a parking stage holds the work unassigned and parked; leaving it unparks.

What PostgreSQL also does and this does not yet -- UAT instructions (so a
user-review stage is not entered), manual-control holds that defer an
approval, Router and Publisher windows, relays and recoveries -- is refused
here with `Unsupported`, never silently skipped. Serial focus (one active
ticket per implementer, the rest queued) is not applied by this core at all
(docs/syrd-642-oracle-backend.md lists every gap).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .workflow_config import (
    BUILTIN_FLAGS,
    declared_review_return,
    director_hold_stage_names,
    parking_stage_names,
    transition_allows_actor,
)


class TransitionRefused(ValueError):
    """The declared workflow does not allow this transition now (PostgreSQL: a plain exception, HTTP 400)."""


class ActorRefused(PermissionError):
    """This actor may not take this transition (PostgreSQL: SQLSTATE 42501, HTTP 403)."""


class Unsupported(NotImplementedError):
    """A rule the PostgreSQL board applies that this engine does not yet; nothing was changed."""


@dataclass
class TicketState:
    """What a transition reads and writes of one ticket."""

    id: str
    state: str
    assignee: str
    commit_hash: str = ""
    commit_exempt: bool = False
    manually_controlled: bool = False
    parked: bool = False
    flags: dict[str, bool] = field(default_factory=dict)
    blocked: bool = False
    last_implementer: str = ""


@dataclass
class Decision:
    """The ticket after the transition, and what the store must record with it."""

    transition: dict[str, Any]
    ticket: TicketState
    comment: str = ""


def flag(cfg: dict[str, Any], ticket: TicketState, name: str) -> bool:
    if name in ticket.flags:
        return bool(ticket.flags[name])
    return bool((cfg.get("flags", {}).get(name) or {}).get("default", False))


def initial_flags(cfg: dict[str, Any], given: dict[str, Any] | None = None) -> dict[str, bool]:
    """Every declared flag with its default, then what the creator gave; sign-offs always start ungiven."""
    flags = {name: bool(spec.get("default", False)) for name, spec in cfg.get("flags", {}).items()}
    for name, value in (given or {}).items():
        if name in flags and cfg["flags"][name]["kind"] == "gate":
            flags[name] = bool(value)
    return flags


def _stages(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["name"]: s for s in cfg["stages"]}


def find_transition(cfg: dict[str, Any], state: str, action: str, target: str | None = None) -> dict[str, Any]:
    found = [t for t in cfg["transitions"] if t["from"] == state and t["action"] == action
             and (not target or t["to"] == target)]
    if len(found) != 1:
        raise TransitionRefused(f"unknown or ambiguous workflow action {action!r} from {state}; specify target"
                                if found else f"{action} is not declared from {state}")
    return found[0]


def decide(cfg: dict[str, Any], ticket: TicketState, action: str, actor: str,
           payload: dict[str, Any] | None = None) -> Decision:
    """The declared transition `action` by `actor`, applied to `ticket`; raises, changing nothing, when refused."""
    payload = dict(payload or {})
    stages = _stages(cfg)
    tr = find_transition(cfg, ticket.state, action, payload.get("target"))
    if not transition_allows_actor(cfg, tr, actor) or (tr["owner_scoped"] and ticket.assignee != actor):
        raise ActorRefused(f"{actor} cannot perform workflow action {action}")
    if tr.get("relays_decision_of"):
        raise Unsupported(f"{action} relays another role's decision, which this backend does not yet record")
    if ticket.manually_controlled and tr["primitive"] == "approve":
        raise Unsupported(f"{ticket.id} is held by the Director; an approval of a held ticket is not yet supported "
                          "on this backend")
    reason = str(payload.get("text") or payload.get("reason") or "").strip()
    if tr["require_reason"] and not reason:
        raise TransitionRefused("reason required")
    source, primitive = stages[ticket.state], tr["primitive"]
    if source["terminal"] and primitive != "reopen":
        raise TransitionRefused("terminal exit requires reopen")
    parking = parking_stage_names(cfg)
    user_handoff = (source["kind"] == "review" and "user" in source["owners"]
                    and tr["to"] in director_hold_stage_names(cfg))
    if (primitive not in ("return", "reopen") and tr["to"] not in parking and not user_handoff
            and not declared_review_return(cfg, ticket.state, tr["to"]) and ticket.blocked):
        raise TransitionRefused(f"unresolved blocker prevents forward promotion of {ticket.id}")
    new = TicketState(**{**ticket.__dict__, "flags": dict(ticket.flags)})
    if "commit_hash" in payload:
        new.commit_hash = str(payload["commit_hash"] or "").strip()
    if tr["require_commit"] and not new.commit_hash and not new.commit_exempt:
        raise TransitionRefused("commit required")
    signoff = source.get("signoff")
    if primitive not in ("approve", "return", "reopen") and declared_review_return(cfg, ticket.state, tr["to"]):
        pass  # a review handing work back keeps its record (SYRD-536)
    elif primitive == "approve":
        new.flags[signoff] = True
    elif (signoff and primitive not in ("return", "reopen") and tr["to"] not in parking and not user_handoff
          and not flag(cfg, new, signoff)):
        raise TransitionRefused("stage signoff required")
    for name in tr["clear_signoffs"]:
        new.flags[name] = False
    if primitive in ("return", "reopen"):
        new.commit_hash, new.commit_exempt = "", False
    if tr["allow_no_code"]:
        new.commit_hash, new.commit_exempt = "", True
    if source["kind"] == "implementation" and (tr["allow_no_code"] or (new.commit_hash and new.commit_hash != ticket.commit_hash)):
        for name, spec in cfg["flags"].items():
            if spec["kind"] == "signoff":
                new.flags[name] = False
    target, loops = tr["to"], 0
    while True:
        dest = stages[target]
        gate_open = dest["gate"] is None or flag(cfg, new, dest["gate"])
        signed_past = dest["signoff"] is not None and dest["skip_to"] is not None and flag(cfg, new, dest["signoff"])
        if gate_open and not signed_past:
            break
        target, loops = dest["skip_to"], loops + 1
        if loops > len(stages):
            raise TransitionRefused("gate cycle")
    if dest["kind"] == "review" and "user" in dest["owners"] and target != ticket.state:
        raise Unsupported(f"{ticket.id} would enter {target}, which needs the Director's UAT instructions first; "
                          "UAT instructions are not yet supported on this backend")
    if dest["signoff"]:
        new.flags[dest["signoff"]] = False
    owners = list(dest["owners"])
    assignee = str(payload.get("assignee") or new.assignee or "").strip()
    if primitive == "return" and ticket.last_implementer in owners:
        assignee = ticket.last_implementer
    if owners and assignee not in owners:
        assignee = owners[0]
    if dest["kind"] == "implementation" and cfg.get("scheduling") is not None:
        raise Unsupported("a pull-scheduled board's queue diversion is not yet supported on this backend; "
                          "claim admitted work with claim-next")
    new.state, new.assignee = target, assignee
    if target in parking:
        new.assignee, new.parked = "unassigned", True
    elif ticket.state in parking:
        new.parked = False
    if dest["kind"] == "implementation":
        new.last_implementer = new.assignee
    return Decision(transition=tr, ticket=new, comment=reason)


def builtin_columns(ticket: TicketState, cfg: dict[str, Any]) -> dict[str, bool]:
    """The PostgreSQL board's flag columns, read from the flags, so a ticket reads the same on either backend."""
    return {name: flag(cfg, ticket, name) for name in BUILTIN_FLAGS}
