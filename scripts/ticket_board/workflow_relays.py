"""The one bounded workflow change that lets a Director relay the User's acceptance (SYRD-535).

pgu953 (SYRD-217) grants `relay_user_sign_off` by migration, once, to the
workflow document a board has when the migration runs. A tenant that declared
its workflow afterwards -- MEFP adopted its legacy workflow later, and is now at
revision 66 -- never received it, and a migration does not run twice. So its
Director could not enter a User's acceptance at all.

`user_acceptance_relay` is that migration's rule, in Python, against a document
the caller holds: the relay copied from the User's own approval in
`user_review` -- destination and sign-off clearing both -- granted to the single
control role, only where the User has no pane, only where the approval does not
end the ticket, and only where nothing already grants it. It adds exactly one
transition and changes nothing else.

A rejection is different, and is not added here. A relay may only return or
approve, and must mirror a move the User itself can make from the same stage
(`workflow_config.validate`). Where the User rejects by `reopen`, there is
nothing a relay may mirror; giving the User a `return` move first is a decision
about the tenant's workflow, not a repair. `rejection_relay_finding` says which
case a document is in.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

#: What pgu953 names the control role by: what it can do, not what it is called.
CONTROL_CAPABILITIES = ("set_manually_controlled", "merge")


def document_digest(document: dict[str, Any]) -> str:
    """A stable digest of a workflow document, for a reviewer to compare before and after."""
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _user_moves(document: dict[str, Any], primitive: str) -> list[dict[str, Any]]:
    return sorted(
        (
            t for t in document.get("transitions") or []
            if isinstance(t, dict) and t.get("from") == "user_review" and t.get("primitive") == primitive
            and "user" in (t.get("actors") or [])
        ),
        key=lambda t: str(t.get("action") or ""),
    )


def user_acceptance_relay(document: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """The relay pgu953 would grant this document, or why it grants none."""
    models = _user_moves(document, "approve")
    if not models:
        return None, "the User has no approval in user_review for a relay to mirror"
    model = models[0]
    controllers = sorted(
        str(r.get("name")) for r in document.get("roles") or []
        if isinstance(r, dict) and r.get("active") and set(CONTROL_CAPABILITIES) <= set(r.get("capabilities") or [])
    )
    if len(controllers) != 1:
        return None, (
            f"{len(controllers)} active roles hold the control capabilities ({', '.join(controllers) or 'none'}); "
            "which of them may enter the User's acceptance is the tenant's decision, not a repair"
        )
    transitions = [t for t in document.get("transitions") or [] if isinstance(t, dict)]
    if any(t.get("relays_decision_of") == "user" and t.get("from") == "user_review" and t.get("primitive") == "approve"
           for t in transitions):
        return None, "the User's acceptance is already relayable here"
    if any(t.get("action") == "relay_user_sign_off" for t in transitions):
        return None, "this workflow already names an action relay_user_sign_off; it is the tenant's own"
    user = next((r for r in document.get("roles") or [] if isinstance(r, dict) and r.get("name") == "user"), None)
    if user is None or user.get("target") is not None:
        return None, "the User has a pane of its own here, and signs off for itself"
    stages = {s.get("name"): s for s in document.get("stages") or [] if isinstance(s, dict)}
    if bool((stages.get(model.get("to")) or {"terminal": True}).get("terminal")):
        return None, "the User's approval ends the ticket, and a relayed approval may not"
    return {
        "from": "user_review",
        "to": model["to"],
        "action": "relay_user_sign_off",
        "label": "Record the User's acceptance (relayed)",
        "actors": [controllers[0]],
        "primitive": "approve",
        "owner_scoped": False,
        "require_commit": True,
        "require_reason": True,
        "clear_signoffs": list(model.get("clear_signoffs") or []),
        "allow_no_code": False,
        "relays_decision_of": "user",
    }, f"mirrors the User's own {model.get('action')} to {model['to']}"


def with_user_acceptance_relay(document: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
    """`document` with the relay added -- a copy, nothing else changed -- or None and the reason."""
    relay, reason = user_acceptance_relay(document)
    if relay is None:
        return None, None, reason
    updated = copy.deepcopy(document)
    updated["transitions"] = [*updated.get("transitions", []), relay]
    return updated, relay, reason


def rejection_relay_finding(document: dict[str, Any]) -> str:
    """Whether the User's rejection can be relayed here, said plainly."""
    if any(t.get("relays_decision_of") == "user" and t.get("primitive") == "return"
           for t in document.get("transitions") or [] if isinstance(t, dict)):
        return "the User's rejection is already relayable here"
    if _user_moves(document, "return"):
        return ("the User rejects by returning the work, so a rejection relay could mirror that move; it is "
                "not part of this change")
    reopen = _user_moves(document, "reopen")
    if reopen:
        return (
            f"the User rejects here by {reopen[0].get('action')} (a reopen to {reopen[0].get('to')}), and a relay may "
            "only return or approve: there is no rejection relay to add. Giving the User a return move first is a "
            "decision about this workflow, not a repair"
        )
    return "the User has no rejection in user_review for a relay to mirror"
