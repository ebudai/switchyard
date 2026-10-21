"""The Publisher's place in a declared workflow: a mechanical publication stage after Audit (SYRD-625).

Integration of an approved candidate into the public integration branch used to
be the Director's routine, done by hand in a pane. With the Publisher it is a
workflow stage taken by a system actor that has no model, runtime, pane,
session, onboarding prompt or capability: the board's notify listener runs it
(`publisher`, `publisher_pass`), and the only things it can do are the three
transitions composed here.

`with_publisher` adds, to a tenant's own declared document:

* the `publisher` role: kind `system`, active, with no runtime, pane target,
  slot, prompt, template or capability. provision_role_accounts lists it among
  the roles that are never processes, so no account or pane is made for it;
* the `publication` stage, owned by the Publisher, ungated and silent, placed
  after Audit: every approval out of Audit, and Audit's own gate skip, now land
  here instead of wherever they landed before, so it runs after every required
  code, documentation, inspection and Audit gate whichever of them a ticket
  was subject to;
* the `publication_failed` stage, held by the Director, who is told when work
  lands there;
* the Publisher's three ways out of `publication`, owner-scoped, each with the
  Publisher's own reason:
  - `publication_published` to the stage Audit used to approve into. The board's
    ordinary gate walk continues from there, so a ticket that needs the User's
    sign-off goes on towards User Review and any other goes to the Director's
    Final Sign-Off, exactly as an Audit approval went before;
  - `publication_return`, a return to the recorded implementer that clears
    every sign-off: the candidate or its lineage has to change, so every gate
    is renewed for whatever comes back;
  - `publication_escalate` to `publication_failed`, for anything the Director
    has to fix: a credential, a policy, the publication target, the journal or
    a helper;
* the Director's two ways out of `publication_failed`: `publication_retry`
  back to the Publisher, and `publication_kick_back` to the implementer, which
  clears every sign-off like the Publisher's own return;
* a defer (and any other Director-only exit to parking or a terminal stage)
  from both new stages, under the names the document already uses for the
  stage Audit approved into.

`without_publisher` takes the stages and transitions out again and puts Audit's
approvals back where they went; the Publisher stays declared, inactive, so the
comments and decisions it recorded keep a name. The board refuses it while a
ticket is in either stage.

`validate_publisher` is the shape check workflow_config runs on every document,
so a Publisher cannot be given a pane, a model, a capability, another stage or
another move by editing a document by hand. The database adds the rest:
ticket_board.publisher_finish is the only path to the three transitions, and a
trigger refuses them, and any ticket write in the Publisher's name, from
anywhere else (migration pgu1004).
"""

from __future__ import annotations

import copy
from typing import Any, Callable

PUBLISHER_ROLE = "publisher"
PUBLISHER_KIND = "system"
PUBLICATION_STAGE = "publication"
FAILED_STAGE = "publication_failed"
PUBLISHED = "publication_published"
RETURN = "publication_return"
ESCALATE = "publication_escalate"
RETRY = "publication_retry"
KICK_BACK = "publication_kick_back"
#: The Publisher's moves, and the only ones it has.
PUBLISHER_ACTIONS = (PUBLISHED, RETURN, ESCALATE)
#: The Director's answers to a publication failure.
DIRECTOR_ACTIONS = (RETRY, KICK_BACK)
#: Role fields that would give the Publisher a process, a conversation or a voice.
PROCESS_FIELDS = ("runtime", "target", "slot", "onboarding", "onboarding_prompt", "template_role",
                  "ephemeral", "serial", "presentation_label")
#: The sign-off flag that marks Audit's stage, whatever the tenant calls it.
AUDIT_SIGNOFF = "audit_signoff"

PUBLISHER_DECLARATION = {
    "name": PUBLISHER_ROLE, "label": "Publisher", "kind": PUBLISHER_KIND, "active": True, "capabilities": [],
}


def publication_stage(cfg: dict[str, Any] | None) -> dict[str, Any] | None:
    return next((s for s in (cfg or {}).get("stages", []) if s["name"] == PUBLICATION_STAGE), None)


def publisher_role(cfg: dict[str, Any] | None) -> dict[str, Any] | None:
    return next((r for r in (cfg or {}).get("roles", []) if r["name"] == PUBLISHER_ROLE), None)


def audit_stage(cfg: dict[str, Any]) -> dict[str, Any] | None:
    """The stage whose sign-off is Audit's, or None when there is not exactly one."""
    found = [s for s in cfg["stages"] if s["signoff"] == AUDIT_SIGNOFF]
    return found[0] if len(found) == 1 else None


def continuation(cfg: dict[str, Any] | None) -> str | None:
    """Where successful publication sends work: the stage Audit used to approve into."""
    return next((t["to"] for t in (cfg or {}).get("transitions", []) if t["action"] == PUBLISHED), None)


def _signoffs(cfg: dict[str, Any]) -> list[str]:
    return sorted(name for name, flag in cfg["flags"].items() if flag["kind"] == "signoff")


def _transition(source: str, destination: str, action: str, label: str, actors: list[str], *, primitive: str,
                owner_scoped: bool, require_commit: bool = False, require_reason: bool = False,
                clear_signoffs: list[str] | None = None) -> dict[str, Any]:
    return {
        "from": source, "to": destination, "action": action, "label": label, "actors": actors,
        "primitive": primitive, "owner_scoped": owner_scoped, "require_commit": require_commit,
        "require_reason": require_reason, "clear_signoffs": clear_signoffs or [], "allow_no_code": False,
        "relays_decision_of": None,
    }


def _composed_transitions(cfg: dict[str, Any], impl: str, after: str) -> list[dict[str, Any]]:
    """The Publisher's three moves and the Director's two answers to a failure, exactly."""
    every = _signoffs(cfg)
    return [
        _transition(PUBLICATION_STAGE, after, PUBLISHED, "Published to the integration branch", [PUBLISHER_ROLE],
                    primitive="move", owner_scoped=True, require_commit=True, require_reason=True),
        _transition(PUBLICATION_STAGE, impl, RETURN, "Return: candidate cannot be published as it is",
                    [PUBLISHER_ROLE], primitive="return", owner_scoped=True, require_reason=True,
                    clear_signoffs=every),
        _transition(PUBLICATION_STAGE, FAILED_STAGE, ESCALATE, "Publication failed: Director", [PUBLISHER_ROLE],
                    primitive="move", owner_scoped=True, require_reason=True),
        _transition(FAILED_STAGE, PUBLICATION_STAGE, RETRY, "Retry publication", ["director"], primitive="move",
                    owner_scoped=False),
        _transition(FAILED_STAGE, impl, KICK_BACK, "Return to the implementer", ["director"], primitive="return",
                    owner_scoped=False, require_reason=True, clear_signoffs=every),
    ]


def _stages() -> list[dict[str, Any]]:
    return [
        {"name": PUBLICATION_STAGE, "label": "Publication", "owners": [PUBLISHER_ROLE], "kind": "system",
         "gate": None, "skip_to": None, "signoff": None, "terminal": False, "notify": {"kind": "none", "role": None}},
        {"name": FAILED_STAGE, "label": "Publication failed", "owners": ["director"], "kind": "system",
         "gate": None, "skip_to": None, "signoff": None, "terminal": False,
         "notify": {"kind": "assignee", "role": None}},
    ]


def with_publisher(document: dict[str, Any]) -> dict[str, Any]:
    """`document` with the Publisher between Audit and what Audit approved into. Raises ValueError, changing nothing."""
    from scripts.ticket_board.workflow_config import DIRECTOR_ROLE, parking_stage_names, validate

    cfg = validate(copy.deepcopy(document))
    if publication_stage(cfg) is not None:
        raise ValueError(f"publisher: this workflow already has a {PUBLICATION_STAGE} stage; nothing was changed")
    taken = sorted({PUBLICATION_STAGE, FAILED_STAGE} & {s["name"] for s in cfg["stages"]})
    clashing = sorted(t["action"] for t in cfg["transitions"] if t["action"] in PUBLISHER_ACTIONS + DIRECTOR_ACTIONS)
    if taken or clashing:
        raise ValueError(f"publisher: this workflow already declares {', '.join(taken + clashing)}; nothing was changed")
    audit = audit_stage(cfg)
    if audit is None:
        raise ValueError("publisher: this workflow has no single Audit stage (the stage signed off by audit_signoff) "
                         "for publication to follow")
    approvals = [t for t in cfg["transitions"] if t["from"] == audit["name"] and t["primitive"] == "approve"]
    targets = sorted({t["to"] for t in approvals} | ({audit["skip_to"]} if audit["skip_to"] else set()))
    if not approvals or len(targets) != 1:
        raise ValueError(f"publisher: cannot tell where approved work goes after {audit['name']} "
                         f"({', '.join(targets) or 'no approval'}); add the stage by hand and apply the document")
    after = targets[0]
    impl = next((s["name"] for s in cfg["stages"] if s["kind"] == "implementation"), None)
    stages = {s["name"]: s for s in cfg["stages"]}
    if impl is None or stages[after]["terminal"] or stages[after]["kind"] == "implementation":
        raise ValueError(f"publisher: {after} is not a stage publication can hand work on to")
    for approval in approvals:
        approval["to"] = PUBLICATION_STAGE
    if audit["skip_to"]:
        audit["skip_to"] = PUBLICATION_STAGE
    if publisher_role(cfg) is not None:  # a Publisher turned off earlier: turn the same role back on
        cfg["roles"] = [dict(PUBLISHER_DECLARATION) if r["name"] == PUBLISHER_ROLE else r for r in cfg["roles"]]
    else:
        cfg["roles"].append(dict(PUBLISHER_DECLARATION))
    position = next(i for i, s in enumerate(cfg["stages"]) if s["name"] == audit["name"]) + 1
    cfg["stages"][position:position] = _stages()
    parking = parking_stage_names(cfg)
    exits = [dict(t, **{"from": source}) for source in (PUBLICATION_STAGE, FAILED_STAGE) for t in cfg["transitions"]
             if t["from"] == after and t["actors"] == [DIRECTOR_ROLE] and t["primitive"] == "move"
             and (t["to"] in parking or stages[t["to"]]["terminal"])]
    if not any(t["to"] in parking for t in exits):
        raise ValueError(f"publisher: the Director cannot defer work out of {after}, so the new stages would have no "
                         "way to put work down; add them by hand instead")
    cfg["transitions"] += _composed_transitions(cfg, impl, after) + exits
    removed = [name for name in cfg.get("remove_stages") or [] if name not in (PUBLICATION_STAGE, FAILED_STAGE)]
    cfg["remove_stages"] = removed
    return validate(cfg)


def without_publisher(document: dict[str, Any]) -> dict[str, Any]:
    """`document` without the Publisher's stages: Audit approves where it did before; the role stays, inactive."""
    from scripts.ticket_board.workflow_config import validate

    cfg = validate(copy.deepcopy(document))
    if publication_stage(cfg) is None:
        raise ValueError(f"publisher: this workflow has no {PUBLICATION_STAGE} stage; nothing was changed")
    after = continuation(cfg)
    gone = (PUBLICATION_STAGE, FAILED_STAGE)
    cfg["stages"] = [s for s in cfg["stages"] if s["name"] not in gone]
    cfg["transitions"] = [t for t in cfg["transitions"] if t["from"] not in gone]
    for t in cfg["transitions"]:
        if t["to"] == PUBLICATION_STAGE:
            t["to"] = after
    for s in cfg["stages"]:
        if s["skip_to"] == PUBLICATION_STAGE:
            s["skip_to"] = after
    removed = list(cfg.get("remove_stages") or [])
    cfg["remove_stages"] = removed + [name for name in gone if name not in removed]
    role = publisher_role(cfg)
    if role is not None:
        role["active"] = False
    return validate(cfg)


def validate_publisher(cfg: dict[str, Any], need: Callable[[bool, str], None]) -> None:
    """The Publisher's shape, wherever it is declared: no process, no capability, three moves, one stage."""
    role = publisher_role(cfg)
    if role is not None:
        need(role["kind"] == PUBLISHER_KIND and role["capabilities"] == []
             and not any(role.get(field) not in (None, False) for field in PROCESS_FIELDS),
             "invalid publisher: it is a system actor with no model, runtime, pane, session, prompt or capability")
        need(not any(PUBLISHER_ROLE in s["owners"] and s["name"] != PUBLICATION_STAGE for s in cfg["stages"]),
             "invalid publisher: it owns the publication stage and nothing else")
        need(not any((PUBLISHER_ROLE in t["actors"] or t.get("relays_decision_of") == PUBLISHER_ROLE)
                     and t["action"] not in PUBLISHER_ACTIONS for t in cfg["transitions"]),
             "invalid publisher: its only moves are publication_published, publication_return and "
             "publication_escalate")
    stage = publication_stage(cfg)
    composed = [t for t in cfg["transitions"] if t["action"] in PUBLISHER_ACTIONS + DIRECTOR_ACTIONS]
    stages = {s["name"]: s for s in cfg["stages"]}
    if stage is None:
        need(not composed and FAILED_STAGE not in stages,
             "invalid publisher: its moves and the publication_failed stage need the publication stage")
        need(not (role and role["active"]), "invalid publisher: an active publisher needs its stage")
        return
    need(bool(role and role["active"]), "invalid publisher: the publication stage needs an active publisher role")
    need(stage == _stages()[0] and stages.get(FAILED_STAGE) == _stages()[1],
         "invalid publisher: publication is the publisher's alone, ungated, signed off by nobody and silent; "
         "publication_failed is the director's and tells the director")
    after = continuation(cfg)
    impl = next((s["name"] for s in cfg["stages"] if s["kind"] == "implementation"), "")
    need(after in stages and after not in (PUBLICATION_STAGE, FAILED_STAGE) and not stages[after]["terminal"]
         and stages[after]["kind"] != "implementation",
         "invalid publisher: publication hands work on to a later, non-terminal stage")
    expected = {t["action"]: t for t in _composed_transitions(cfg, impl, after or "")}
    need(sorted(t["action"] for t in composed) == sorted(expected)
         and all(t == expected[t["action"]] for t in composed),
         "invalid publisher: its moves and the director's answers to a failure are exactly as composed -- a "
         "published move carrying the commit, returns that clear every sign-off, and an escalation to the director")
    audit = audit_stage(cfg)
    need(audit is not None and audit["skip_to"] in (None, PUBLICATION_STAGE)
         and all(t["to"] == PUBLICATION_STAGE for t in cfg["transitions"]
                 if t["from"] == audit["name"] and t["primitive"] == "approve"),
         "invalid publisher: every approval out of Audit, and Audit's gate skip, enters publication")
    need(not any(s["skip_to"] == after for s in cfg["stages"])
         and all(t["from"] == PUBLICATION_STAGE or t["actors"] == ["director"] or t["primitive"] in ("return", "reopen")
                 for t in cfg["transitions"] if t["to"] == after),
         f"invalid publisher: nothing but publication and the director's own moves reaches {after}")
