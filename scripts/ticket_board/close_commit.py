"""SYRD-864: every move into done records the commit it closes on.

The board's provenance rests on a close naming its commit: `mark_done` takes one, and the declared executor refuses
a close without it ("commit required") -- but only because the move says `require_commit`. A hand-edited document
that dropped it was accepted, applied, and then closed a code ticket (not `commit_exempt`) into done with no commit
recorded: measured on a board built the production way. Nothing else on the path refused it.

So a declared document is refused, when it is applied, if any move into done does not require the commit. Two moves
close without one by design, and stay as they are:
- a no-code completion (`allow_no_code`): its own policy holds it to Implementation, a reason, and no commit;
- the research stage's `close_on_finding`, on a board that declares that stage: a finding with no commit, its move
  pinned exactly by `research_stage.validate_research_stage`.

A ticket with no code still closes by the ordinary `mark_done`: the executor lets a `commit_exempt` ticket through.
The database's `validate_declared_workflow` refuses the same documents (pgu1056).
"""

from __future__ import annotations

from typing import Any, Callable

DONE = "done"
#: The research stage's close of a finding with no commit, and the stage that declares it (research_stage).
FINDING_CLOSE = "close_on_finding"
RESEARCH_STAGE = "research"


def closes_without_commit(cfg: dict[str, Any]) -> list[str]:
    """Each move into done that records no commit and is not one of the two that close without one by design."""
    research = any(stage.get("name") == RESEARCH_STAGE for stage in cfg.get("stages", []))
    return [f"{t['action']} ({t['from']} -> {DONE})" for t in cfg.get("transitions", [])
            if t.get("to") == DONE and not t.get("require_commit") and not t.get("allow_no_code")
            and not (research and t.get("action") == FINDING_CLOSE)]


def validate_close_commit(cfg: dict[str, Any], need: Callable[[bool, str], None]) -> None:
    unrecorded = closes_without_commit(cfg)
    need(not unrecorded, "a move into done must require the commit it closes on (require_commit): "
         + ", ".join(unrecorded))
