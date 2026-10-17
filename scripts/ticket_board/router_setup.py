"""Turning the optional Router role on and off in a tenant's declared document (SYRD-599).

`with_router` adds, to the tenant's own document:

* the `router` role, of kind `router`: active, holding `router_assign` and
  nothing else, owning no stage, detached (`slot: null`) with a pane target so
  the board can tell it about work, and Router's own onboarding prompt. Its
  provider, model and effort are the launcher's, like any role's: copied from
  `template_role` until `switchyard set-role-runtime` chooses a cheaper one,
  under the same project account, so no credential moves between owners;
* `router_return`, its one move: Implementation to `analysis`, owner-scoped,
  with a reason, nothing else changed;
* `implementer_kick_back`, unless the document already declares it: every
  Implementation owner's way to hand a misrouted ticket back to the Director's
  triage, with a reason. It goes to the Director, never back through Router,
  and the implementer chooses nothing about where it goes next.

`without_router` deactivates the role and removes `router_return`; the role
stays declared so its recorded decisions keep a name, and
`implementer_kick_back` stays, being an ordinary implementer move. The board
refuses either while Router still holds tickets (it would orphan them).

Both results are validated like any document -- by workflow_config and, on
apply, by the board -- so nothing reaches the board that `apply` would refuse.
"""

from __future__ import annotations

import copy
from typing import Any

from scripts.ticket_board.router_role import (
    ASSIGN, KICK_BACK, RETURN, ROUTER_KIND, ROUTER_ROLE, TRIAGE_STAGE, implementation_stage, router_role)
from scripts.ticket_board.workflow_config import DIRECTOR_ROLE, RUNTIMES, validate

#: Router's remit, stored as its onboarding prompt. It names the two operations
#: and nothing else: every other judgement stays the Director's, and the User
#: decided Router is not even to suggest one, so the prompt does not mention them
#: (SYRD-599).
ROUTER_PROMPT = """You are Router. Your only job is to choose which implementer owns a ticket the Director has already admitted to Implementation, or to hand it back to the Director when that choice is not yours to make.

When the board tells you a ticket is assigned to you, read it once:

    ticket-board-read ticket <ID> --json

Its `routing` field gives the `revision` you are classifying and the `assignable` owners. Choose from those using the ticket's content, origin, the components and paths it names, and what each owner is known to own. Then make exactly one write:

    ticket-board-write router-assign <ID> --assignee <owner> --revision <revision>
    ticket-board-write router-return <ID> --reason "<one or two sentences>" --revision <revision>

Return it to the Director when you are not confident, the ownership signals conflict, the work spans projects, a role it asks for is missing or inactive, or its first deliverable is not implementation (a spec, a decision, an investigation). A justified return costs far less than a wrong assignment.

Then stop and wait for the next notification. Do not re-read tickets, poll the board, or watch panes. Choosing an owner is your whole job: use no other board operation, and leave everything else about the ticket to the Director. If a write is refused, read the refusal: a stale revision means read the ticket once more; anything else means return the ticket to the Director with that refusal as the reason."""


def _transition(source: str, destination: str, action: str, label: str, actors: list[str]) -> dict[str, Any]:
    return {
        "from": source, "to": destination, "action": action, "label": label, "actors": actors,
        "primitive": "move", "owner_scoped": True, "require_commit": False, "require_reason": True,
        "clear_signoffs": [], "allow_no_code": False, "relays_decision_of": None,
    }


def with_router(document: dict[str, Any], *, runtime: str, template_role: str | None = None) -> dict[str, Any]:
    """`document` with an active Router, validated. Raises ValueError when it cannot be added safely."""
    cfg = validate(copy.deepcopy(document))
    project = str(cfg.get("project") or "").strip()
    if runtime not in RUNTIMES:
        raise ValueError(f"router: runtime must be one of {', '.join(sorted(RUNTIMES))}, not {runtime!r}")
    if not project:
        raise ValueError("router: this workflow names no project, so Router's pane target cannot be named")
    existing = router_role(cfg)
    if existing is not None and existing["active"]:
        raise ValueError("router: this workflow already has an active router; nothing was changed")
    if any(r["name"] == ROUTER_ROLE and r["kind"] != ROUTER_KIND for r in cfg["roles"]):
        raise ValueError("router: this workflow already has a role named router that is not a router; nothing was changed")
    impl = implementation_stage(cfg)
    stages = {s["name"]: s for s in cfg["stages"]}
    triage = stages.get(TRIAGE_STAGE)
    if impl is None or triage is None or triage["owners"] != [DIRECTOR_ROLE]:
        raise ValueError(f"router: Router hands work back to {TRIAGE_STAGE}, which this workflow must declare "
                         f"as the {DIRECTOR_ROLE}'s alone; nothing was changed")
    roles = {r["name"]: r for r in cfg["roles"]}
    template = template_role or impl["owners"][0]
    if template not in roles or template == ROUTER_ROLE:
        raise ValueError(f"router: template role {template!r} is not a role of this workflow")
    router = {
        "name": ROUTER_ROLE, "label": "Router", "kind": ROUTER_KIND, "active": True, "capabilities": [ASSIGN],
        "runtime": runtime, "target": f"{project}-{ROUTER_ROLE}:0.0", "slot": None, "serial": False,
        "template_role": template, "onboarding_prompt": ROUTER_PROMPT, "presentation_label": "Router",
    }
    if existing is not None:  # a router turned off earlier: turn the same role back on
        cfg["roles"] = [router if r["name"] == ROUTER_ROLE else r for r in cfg["roles"]]
    else:
        cfg["roles"].append(router)
    if any(t["action"] == RETURN for t in cfg["transitions"]):
        raise ValueError("router: this workflow already declares router_return; nothing was changed")
    cfg["transitions"].append(_transition(impl["name"], TRIAGE_STAGE, RETURN, "Return to triage", [ROUTER_ROLE]))
    kick = [t for t in cfg["transitions"] if t["action"] == KICK_BACK]
    if not kick:
        cfg["transitions"].append(_transition(impl["name"], TRIAGE_STAGE, KICK_BACK, "Not mine: back to triage",
                                              list(impl["owners"])))
    elif not any(t["from"] == impl["name"] and t["to"] == TRIAGE_STAGE and t["owner_scoped"] and t["require_reason"]
                 and sorted(t["actors"]) == sorted(impl["owners"]) for t in kick):
        raise ValueError(f"router: this workflow's {KICK_BACK} does not let every Implementation owner hand work "
                         f"back to {TRIAGE_STAGE} with a reason; fix it by hand, then enable Router")
    return validate(cfg)


def without_router(document: dict[str, Any]) -> dict[str, Any]:
    """`document` with Router inactive and its move removed, validated."""
    cfg = validate(copy.deepcopy(document))
    existing = router_role(cfg)
    if existing is None or not existing["active"]:
        raise ValueError("router: this workflow has no active router; nothing was changed")
    existing["active"] = False
    existing.pop("slot", None)
    cfg["transitions"] = [t for t in cfg["transitions"] if t["action"] != RETURN]
    return validate(cfg)
