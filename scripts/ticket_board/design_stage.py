"""An optional declared design stage: the designer owns specification work, and the Director reviews it (SYRD-567).

A provisioned designer is a draft role: it files and releases drafts, and its
only transition is `release_draft`. Design work then runs outside the board's
gates, under a Director hold. `with_design_stage` adds, to a tenant's own
declared document:

* `design` (kind `system`), owned by the designer, which is assigned on
  entry and, when it runs in a pane, notified and reminded while idle like any
  owner. A provisioned designer may have no pane (it runs as a plain CLI); the
  board cannot reach such a role, so the stage then declares explicit silence
  and `designer_unreachable` says so;
* `design_review` (kind `review`), owned by the Director;
* `start_design`, the Director's route into `design` from the draft stage or
  from triage;
* `submit_design`, the designer's owner-scoped hand-off to review;
* `return_design`, the Director's `return` back to the designer, with a reason.
  Like Audit's kick-back into implementation it is allowed while the ticket is
  blocked (see `workflow_config._review_hands_back`);
* `accept_design`, the Director's move of the reviewed spec into triage, where
  it is routed to implementation as usual;
* a defer from both stages to the parking stage, under the name the document
  already uses for it.

`release_draft` is unchanged: a draft that needs no design still goes straight
to triage. The stages it reads -- draft, triage, parking -- are found by the
shape the document gives them, not by name, and anything ambiguous is refused
rather than guessed. The result is validated like any document, so nothing
reaches the board that `apply` would not accept.
"""

from __future__ import annotations

import copy
from typing import Any

from scripts.ticket_board.workflow_config import DIRECTOR_ROLE, parking_stage_names, validate

DESIGNER_ROLE = "designer"
DESIGN_STAGE = "design"
DESIGN_REVIEW_STAGE = "design_review"


def _transition(source: str, destination: str, action: str, label: str, actors: list[str], *,
                primitive: str = "move", owner_scoped: bool = False, require_reason: bool = False) -> dict[str, Any]:
    return {
        "from": source, "to": destination, "action": action, "label": label, "actors": actors,
        "primitive": primitive, "owner_scoped": owner_scoped, "require_commit": False,
        "require_reason": require_reason, "clear_signoffs": [], "allow_no_code": False,
    }


def _stage(name: str, label: str, owner: str, kind: str, *, notify: str = "assignee") -> dict[str, Any]:
    return {
        "name": name, "label": label, "owners": [owner], "kind": kind, "gate": None, "skip_to": None,
        "signoff": None, "terminal": False, "notify": {"kind": notify, "role": None},
    }


def designer_unreachable(document: dict[str, Any]) -> bool:
    """Whether the designer has no pane target, so the board can neither notify nor remind it."""
    return not any(role["name"] == DESIGNER_ROLE and role.get("target") for role in document["roles"])


def _only(found: set[str], what: str) -> str:
    if len(found) != 1:
        raise ValueError(f"design stage: cannot tell {what} ({', '.join(sorted(found)) or 'none found'}); "
                         "add the stages by hand and apply the document instead")
    return next(iter(found))


def with_design_stage(document: dict[str, Any]) -> dict[str, Any]:
    """`document` with the design stage added, validated. Raises ValueError when it cannot be added safely."""
    cfg = validate(copy.deepcopy(document))
    stages = {stage["name"]: stage for stage in cfg["stages"]}
    taken = sorted({DESIGN_STAGE, DESIGN_REVIEW_STAGE} & set(stages))
    if taken:
        raise ValueError(f"design stage: this workflow already has {', '.join(taken)}; nothing was changed")
    designer = next((role for role in cfg["roles"] if role["name"] == DESIGNER_ROLE), None)
    if designer is None or not designer["active"]:
        raise ValueError("design stage: the designer is not an active role in this workflow, so nobody could own "
                         "design work; provision the project with its designer first")
    parking = parking_stage_names(cfg)
    queue = (cfg.get("queue") or {}).get("stage")
    park = queue if queue in parking else _only(parking, "which stage deferred work waits in")
    draft = _only({name for name, stage in stages.items() if stage["kind"] == "draft" and name not in parking},
                  "which stage holds drafts")
    director_moves = [tr for tr in cfg["transitions"] if DIRECTOR_ROLE in tr["actors"] and tr["primitive"] == "move"]
    triage = _only({tr["to"] for tr in director_moves
                    if tr["from"] == draft and tr["to"] not in parking and not stages[tr["to"]]["terminal"]},
                   "where a released draft goes")
    defer = _only({tr["action"] for tr in director_moves if tr["from"] in (draft, triage) and tr["to"] == park},
                  "what this workflow calls deferring")
    defer_label = next(tr["label"] for tr in director_moves if tr["action"] == defer and tr["to"] == park)
    position = next(index for index, stage in enumerate(cfg["stages"]) if stage["name"] == draft) + 1
    cfg["stages"][position:position] = [
        _stage(DESIGN_STAGE, "Design", DESIGNER_ROLE, "system",
               notify="none" if designer_unreachable(cfg) else "assignee"),
        _stage(DESIGN_REVIEW_STAGE, "Design review", DIRECTOR_ROLE, "review"),
    ]
    director = [DIRECTOR_ROLE]
    cfg["transitions"] += [
        _transition(draft, DESIGN_STAGE, "start_design", "Start design", director),
        _transition(triage, DESIGN_STAGE, "start_design", "Start design", director),
        _transition(DESIGN_STAGE, DESIGN_REVIEW_STAGE, "submit_design", "Submit design", [DESIGNER_ROLE],
                    owner_scoped=True),
        _transition(DESIGN_REVIEW_STAGE, DESIGN_STAGE, "return_design", "Return design", director,
                    primitive="return", require_reason=True),
        _transition(DESIGN_REVIEW_STAGE, triage, "accept_design", "Accept design", director),
        _transition(DESIGN_STAGE, park, defer, defer_label, director),
        _transition(DESIGN_REVIEW_STAGE, park, defer, defer_label, director),
    ]
    return validate(cfg)
