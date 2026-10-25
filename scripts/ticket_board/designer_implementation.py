"""Letting a project's designer also own Implementation work (SYRD-678).

pie runs its design review through the ordinary loop: the designer is assigned
the ticket in Implementation, submits it to Audit with a commit, Audit's
kick-back returns it to the designer (SYRD-673: the producer of the revision),
and Audit's sign-off moves it on. `with_designer_implementation` makes that
change to the tenant's own document:

* the designer keeps its kind, `draft`, so it stays the designer: it owns Draft,
  releases drafts, gets the designer's onboarding and pane, and is presented as
  the designer. It declares `additional_kinds: ["implementer"]`;
* it joins the Implementation stage's owners, and every move out of that stage
  its current owners all take (submission with or without a commit,
  Inspection, the kick-back to triage), and every move into it they all take
  (an implementer's `start_work`). A pull claim is left alone: a claimant is
  ephemeral and the designer is not;
* with `routing_flag`, it declares that gate flag (off by default) and gives the
  designer `routing_scope: {"flag": <it>}`: Router gives the designer only
  tickets the Director has flagged, and the Director may still assign it
  anything.

The result is validated like any document, so nothing reaches the board that
`apply` would refuse.
"""

from __future__ import annotations

import copy
from typing import Any

from scripts.ticket_board.role_kinds import IMPLEMENTER, has_kind
from scripts.ticket_board.workflow_config import NAME, validate

DESIGNER_ROLE = "designer"


def with_designer_implementation(document: dict[str, Any], *, routing_flag: str | None = None) -> dict[str, Any]:
    """`document` with the designer also an Implementation owner. Raises ValueError, changing nothing, when it cannot."""
    cfg = validate(copy.deepcopy(document))
    designer = next((r for r in cfg["roles"] if r["name"] == DESIGNER_ROLE), None)
    if designer is None or designer["kind"] != "draft" or not designer["active"]:
        raise ValueError("designer implementation: this workflow has no active designer of kind draft; provision the "
                         "project with its designer first")
    if not designer.get("target"):
        raise ValueError("designer implementation: the designer has no pane, so the board could not tell it about "
                         "Implementation work; give it a pane first")
    impl = [s for s in cfg["stages"] if s["kind"] == "implementation"]
    if len(impl) != 1:
        raise ValueError("designer implementation: cannot tell which stage is Implementation; edit the document by hand")
    stage = impl[0]
    if has_kind(designer, IMPLEMENTER) and DESIGNER_ROLE in stage["owners"]:
        raise ValueError("designer implementation: the designer already owns Implementation; nothing was changed")
    owners = set(stage["owners"])
    designer["additional_kinds"] = [IMPLEMENTER]
    stage["owners"] = [*stage["owners"], DESIGNER_ROLE]
    ready = (cfg.get("scheduling") or {}).get("ready_stage")
    for tr in cfg["transitions"]:
        touches = tr["from"] == stage["name"] or tr["to"] == stage["name"]
        claim = tr["from"] == ready and tr["to"] == stage["name"]
        if touches and not claim and owners <= set(tr["actors"]) and DESIGNER_ROLE not in tr["actors"]:
            tr["actors"] = [*tr["actors"], DESIGNER_ROLE]
    if routing_flag is not None:
        if not NAME.fullmatch(routing_flag):
            raise ValueError(f"designer implementation: {routing_flag!r} is not a flag name")
        flag = cfg["flags"].setdefault(routing_flag, {"kind": "gate", "default": False})
        if flag["kind"] != "gate":
            raise ValueError(f"designer implementation: {routing_flag} is a {flag['kind']} flag, not a gate the Director sets")
        designer["routing_scope"] = {"flag": routing_flag}
    return validate(cfg)
