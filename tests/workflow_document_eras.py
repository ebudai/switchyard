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
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANONICAL_PATH = ROOT / "examples" / "workflows" / "inspection.json"


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
