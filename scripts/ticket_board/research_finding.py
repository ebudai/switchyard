"""What the board checks and records around a research ticket (SYRD-842).

The research stage's moves are declared transitions (research_stage.py), and
the database keeps the rules git cannot affect (pgu1051): a brief before
Research, research-only decisions, the round's timebox and its finding. This
module is the rest:

* `before_transition`, run by the service before a research move, refuses
  before anything changes and composes each decision's record:
  - `submit_finding` carries a findings commit the board's trusted commit cache
    knows, with exactly one parent, that adds or edits exactly the ticket's own
    file under the project's research directory
    (`research_stage.research_path`) as an ordinary file, and nothing else.
    The comment names the commit and the file;
  - `submit_finding_without_commit` carries the finding as its reason and takes
    no commit (the database clears an earlier round's findings commit, so the
    candidate never names a finding the researcher replaced);
  - the hand-back, Audit's kick-back, the Director's return and close carry a
    bounded reason and no commit.
* `perform` is the Director's `set_research_brief`, through the extension route.
* `run`, each listener pass, asks the board to tell the Director once about
  each research round whose timebox passed with no finding.
* `handoff` adds the brief or the finding to a research ticket's transition
  notice, read when the notice is sent.
"""

from __future__ import annotations

from typing import Any

from . import commit_cache
from .research_stage import (CLOSE, HAND_BACK, KICK_BACK, RESEARCH_STAGE, RETURN, SIGN_OFF, SUBMIT, SUBMIT_NO_CODE,
                             outside_finding, research_directory, research_path, timebox_seconds)

OPERATIONS = frozenset({"set_research_brief"})
#: The longest reason a hand-back, kick-back, return or close carries.
REASON_MAX_CHARS = 4000
#: A finding with nothing to keep is its whole reason: question, evidence, answer, confidence, recommendation.
FINDING_MAX_CHARS = 16000
_HANDOFF_KINDS = frozenset({"transition"})
def _bounded(payload: dict[str, Any], key: str, what: str, limit: int = REASON_MAX_CHARS) -> str:
    value = payload.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{what} must be text")
    if len(value.strip()) > limit:
        raise ValueError(f"{what} is limited to {limit} characters")
    return value.strip()


def _known(app: Any, commit: str) -> list[str] | None:
    """The commit's parents in the trusted cache, after one refresh if it does not know the commit yet."""
    parents = commit_cache.commit_parents(app.commit_git_dirs, commit)
    if parents is None and commit_cache.refresh_commit_repos(app.commit_git_dirs):
        parents = commit_cache.commit_parents(app.commit_git_dirs, commit)
    return parents


def before_transition(app: Any, ticket_id: str, action: str, payload: dict[str, Any], *, caller_role: str) -> None:
    """Refuse a research move its finding does not support; compose its record."""
    if action not in (SUBMIT, SUBMIT_NO_CODE, HAND_BACK, SIGN_OFF, KICK_BACK, RETURN, CLOSE):
        return
    ticket = app.get_ticket(ticket_id)
    if action in (SUBMIT, SUBMIT_NO_CODE, HAND_BACK) and ticket.get("state") != RESEARCH_STAGE:
        return  # the database refuses it from anywhere else, in its own words
    if action != SUBMIT and payload.get("commit_hash"):
        raise ValueError(f"{action} takes no commit; only {SUBMIT} carries one, the findings commit")
    if action in (HAND_BACK, KICK_BACK, RETURN, CLOSE):
        _bounded(payload, "reason", "the reason")
        return
    if action == SIGN_OFF:
        return
    if action == SUBMIT_NO_CODE:
        finding = _bounded(payload, "reason", "the finding", FINDING_MAX_CHARS) \
            or _bounded(payload, "text", "the finding", FINDING_MAX_CHARS)
        if finding:
            payload.pop("text", None)
            payload["reason"] = f"Research finding (no commit).\n\n{finding}"
        return
    note = _bounded(payload, "text", "the note")
    if payload.get("reason"):
        raise ValueError(f"{SUBMIT} records its findings commit, not a reason; put a note in text, or use "
                         f"{SUBMIT_NO_CODE} when there is nothing to keep")
    raw = str(payload.get("commit_hash") or "").strip()
    if not raw:
        raise ValueError(f"{SUBMIT} needs the findings commit; with nothing to keep, use {SUBMIT_NO_CODE} with the "
                         "finding as its reason")
    new = app._validate_commit_hash(raw).lower()
    expected = research_path(research_directory(app.workflow_configuration()), ticket_id)
    parents = _known(app, new)
    if parents is None:
        raise ValueError(f"the board's trusted commit cache does not know {new[:12]}; publish the findings commit "
                         f"(switchyard-publish-candidate), then submit it again")
    if len(parents) != 1:
        raise ValueError(f"{new[:12]} has {len(parents)} parents; a findings commit is one ordinary commit, with one "
                         f"parent, that changes {expected}")
    entries = commit_cache.changed_entries(app.commit_git_dirs, parents[0], new)
    if entries is None:
        raise ValueError(f"the board cannot read what {new[:12]} changes, so it cannot tell that it changes only "
                         f"{expected}; nothing was moved")
    if not entries:
        raise ValueError(f"{new[:12]} changes nothing; a findings commit adds or edits {expected}")
    outside = outside_finding(entries, expected)
    if outside:
        raise ValueError(f"{new[:12]} changes more than the findings file: {'; '.join(outside[:20])}. A findings "
                         f"commit adds or edits {expected} and nothing else")
    verb = "adds" if entries[0][2].startswith("A") else "edits"
    payload["commit_hash"] = new
    payload["text"] = f"Research finding: commit {new} {verb} {expected}." + (f"\n\n{note}" if note else "")


def perform(app: Any, operation: str, payload: dict[str, Any], *, caller_role: str,
            ticket_id: str | None = None) -> dict[str, Any]:
    """set_research_brief: the Director's brief for one ticket, which makes it a research ticket."""
    if operation not in OPERATIONS or ticket_id is None:
        raise ValueError(f"{operation} is for one ticket")
    unknown = set(payload) - {"question", "timebox", "usable_answer"}
    if unknown:
        raise ValueError(f"{operation} does not take {', '.join(sorted(unknown))}")
    question, answer = payload.get("question"), payload.get("usable_answer")
    if not isinstance(question, str) or not isinstance(answer, str):
        raise ValueError("a research brief's question and usable answer are text")
    timebox = payload.get("timebox")
    seconds = timebox if type(timebox) is int else timebox_seconds(timebox if isinstance(timebox, str) else "")
    with app._pg_connect() as conn:
        with conn.transaction():
            app._pg_set_caller_role(conn, caller_role)
            row = conn.execute("SELECT ticket_board.set_research_brief(%s, %s, %s, %s) AS result",
                               (ticket_id, question, seconds, answer)).fetchone()
    result = row["result"] if isinstance(row, dict) else row[0]
    return {"ticket_id": ticket_id, "research": result}


def _present(conn: Any, signature: str) -> bool:
    row = conn.execute("SELECT to_regprocedure(%s) IS NOT NULL AS present", (signature,)).fetchone()
    return bool(row["present"] if isinstance(row, dict) else row[0]) if row is not None else False


def run(listener: Any, conn: Any) -> int | None:
    """One listener pass: the board tells the Director about each research round whose timebox passed, once."""
    if not getattr(listener, "workflow", None) or not _present(
            conn, "ticket_board.note_research_timeboxes(timestamptz)"):
        return None
    try:
        row = conn.execute("SELECT ticket_board.note_research_timeboxes() AS told").fetchone()
    except Exception as exc:  # one pass's failure is logged; the next pass asks again from the board's own state
        listener.logger.warning("research timebox pass failed: %s", str(exc).splitlines()[0] if str(exc) else exc)
        return None
    return row["told"] if isinstance(row, dict) else row[0]


def handoff(conn: Any, ticket_id: str, kind: str, message: str) -> str:
    """A research ticket's transition notice as delivered: its brief or its finding said after it."""
    if kind not in _HANDOFF_KINDS:
        return message
    try:
        if not _present(conn, "ticket_board.ticket_research_handoff(text)"):
            return message
        result = conn.execute("SELECT ticket_board.ticket_research_handoff(%s)", (ticket_id,))
        row = result.fetchone() if result is not None and hasattr(result, "fetchone") else None
    except Exception:  # the notice itself is never held back by its research line
        return message
    line = (next(iter(row.values())) if isinstance(row, dict) else row[0]) if row else None
    if isinstance(line, (bytes, bytearray)):  # the listener's connection reads text as bytes
        line = line.decode("utf-8", "replace")
    if not isinstance(line, str) or not line.strip():
        return message
    return f"{message.rstrip()}{' ' if message.rstrip().endswith(('.', '!', '?')) else '. '}{line.strip()}"
