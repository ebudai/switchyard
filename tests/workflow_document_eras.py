#!/usr/bin/env python3
"""The shipped workflow document, as boards of an earlier era carried it.

A migration re-validates the stored document with the validator it shipped
with, so a suite that applies an old migration on top of the current schema has
to hand it a document of that migration's era. The pairing does not occur on a
real board -- one carrying a document with relayed decisions has already applied
the migrations that understand them -- but it is exactly what these suites
construct on purpose, to prove what an upgrade does.

Relayed decisions arrived in pgu952 (SYRD-214) and pgu953 (SYRD-217), and a
relayed *approval* in particular is refused outright by every validator before
pgu953: the director sign-off floor had no relayed case to except, and refusing
it was right for what those validators knew.

Until SYRD-629 (pgu1002) the User's way out of User Review was `user_kick_back`,
a return straight to implementation, which `relay_user_kick_back` mirrored. The
shipped document now sends the User's message to the Director's own decision
stage instead. Every earlier era carried the return, and a board of another
shape may still, so the relay and withdrawal suites run on that shape.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANONICAL_PATH = ROOT / "examples" / "workflows" / "inspection.json"


#: The two transitions out of User Review that the shipped document carried before SYRD-629.
USER_RETURN = [
    {"from": "user_review", "to": "in_progress", "action": "user_kick_back", "label": "User kick back",
     "actors": ["user"], "primitive": "return", "owner_scoped": True, "require_commit": False, "require_reason": True,
     "clear_signoffs": ["audit_signoff", "user_signoff"], "allow_no_code": False},
    {"from": "user_review", "to": "in_progress", "action": "relay_user_kick_back",
     "label": "Record the User's rejection (relayed)", "actors": ["director"], "primitive": "return",
     "owner_scoped": False, "require_commit": False, "require_reason": True,
     "clear_signoffs": ["audit_signoff", "user_signoff"], "allow_no_code": False, "relays_decision_of": "user"},
]


def before_user_feedback(document: dict | None = None) -> dict:
    """The shipped document before SYRD-629: no decision stage, and the User returns work to implementation."""
    source = json.loads(CANONICAL_PATH.read_text()) if document is None else document
    era = json.loads(json.dumps(source))
    if not any(s["name"] == "user_feedback" for s in era["stages"]):
        return era
    era["stages"] = [s for s in era["stages"] if s["name"] != "user_feedback"]
    era["transitions"] = [tr for tr in era["transitions"]
                          if tr["from"] != "user_feedback" and tr["to"] != "user_feedback"]
    at = max((i for i, tr in enumerate(era["transitions"]) if tr["from"] == "user_review"),
             default=len(era["transitions"]) - 1)
    era["transitions"][at + 1:at + 1] = json.loads(json.dumps(USER_RETURN))
    (era.get("migrations") or {}).pop("user_feedback", None)
    return era


def before_relaying(document: dict | None = None) -> dict:
    """The shipped document with every relayed decision removed."""
    source = json.loads(CANONICAL_PATH.read_text()) if document is None else document
    era = json.loads(json.dumps(source))
    era["transitions"] = [
        dict(tr) for tr in era["transitions"] if not tr.get("relays_decision_of")
    ]
    for tr in era["transitions"]:
        tr.pop("relays_decision_of", None)
    era.pop("migrations", None)
    return era
