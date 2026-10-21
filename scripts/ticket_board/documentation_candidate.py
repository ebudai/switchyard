"""What the board checks before a documentation decision moves a ticket (SYRD-624).

The documentation stage's decisions are declared transitions, so the database
moves the ticket, clears sign-offs and records the comment. What it cannot see
is git. This runs first, from `extension_operations.before_transition`, for
every decision on the documentation stage whoever owns it, and refuses before
anything changes:

* `documentation_sign_off` carries a documentation commit and the ref it was
  published at. The ticket must have a submitted candidate; the commit must be
  a different one, known to the board's trusted commit cache, whose **sole
  parent** is that candidate; and it must be published at exactly
  `documentation_gate.candidate_ref(<actor>, <ticket>, <candidate>)`, which the
  cache must resolve to it. The caller names the ref only so that a mismatch
  is said rather than guessed. The decision's comment then records the old and
  new SHAs, the ref and the paths the commit changes, so the record says what
  replaced what without anyone re-deriving it. An optional `text` is kept as a
  note after that record. The commit must change something -- an empty one
  would be `documentation_no_change` without its reason -- and every one of
  those paths must be a documentation file
  (`documentation_gate.documentation_surface`), added, edited or removed
  as an ordinary non-executable file: a commit that changes anything else --
  code, help text in code, tests, configuration, a link -- is refused, so a
  Librarian's edit can never carry implementation into the candidate.
* `documentation_no_change` carries a reason and no commit: the candidate is
  exactly the one submitted. The comment says so, with the reason.
* `documentation_kick_back` carries its findings as the reason, like any return.

Every reason is bounded, so a decision's record stays a record.
"""

from __future__ import annotations

from typing import Any

from . import commit_cache
from .documentation_gate import DOCUMENTATION_STAGE, KICK_BACK, NO_CHANGE, SIGN_OFF, candidate_ref, outside_documentation

#: The longest reason, findings or note a decision may carry.
REASON_MAX_CHARS = 4000
#: How many changed paths the sign-off record lists before it says how many more.
LISTED_PATHS = 40


def _bounded(payload: dict[str, Any], key: str, what: str) -> str:
    value = payload.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{what} must be text")
    if len(value.strip()) > REASON_MAX_CHARS:
        raise ValueError(f"{what} is limited to {REASON_MAX_CHARS} characters")
    return value.strip()


def _published_at(app: Any, ref: str) -> str:
    """What the trusted cache says `ref` is published at, after one refresh if it does not know yet."""
    published = app.published_ref_commit(ref)
    if not published and commit_cache.refresh_commit_repos(app.commit_git_dirs):
        published = app.published_ref_commit(ref)
    return published


def before_transition(app: Any, ticket_id: str, action: str, payload: dict[str, Any], *, caller_role: str) -> None:
    """Refuse a documentation decision the candidate's history does not support; compose its record."""
    if action not in (SIGN_OFF, NO_CHANGE, KICK_BACK):
        if "ref" in payload:
            raise ValueError(f"{action} takes no ref; only {SIGN_OFF} names one")
        return
    ticket = app.get_ticket(ticket_id)
    if ticket.get("state") != DOCUMENTATION_STAGE:
        return  # the database refuses it from anywhere else, in its own words
    old = str(ticket.get("commit_hash") or "").strip().lower()
    if action == KICK_BACK:
        _bounded(payload, "reason", "the findings")
        if "ref" in payload or payload.get("commit_hash"):
            raise ValueError(f"{KICK_BACK} returns the ticket with findings; it takes no commit or ref")
        return
    if action == NO_CHANGE:
        reason = _bounded(payload, "reason", "the reason") or _bounded(payload, "text", "the reason")
        if "ref" in payload or payload.get("commit_hash"):
            raise ValueError(f"{NO_CHANGE} leaves the candidate exactly as submitted; it takes no commit or ref. "
                             f"To change documentation, publish a commit and use {SIGN_OFF}")
        if reason:
            payload.pop("text", None)
            payload["reason"] = (f"No documentation change: the candidate stays {old or 'without a commit'}, "
                                 f"as submitted.\n\n{reason}")
        return
    note = _bounded(payload, "text", "the note")
    if payload.get("reason"):
        raise ValueError(f"{SIGN_OFF} records its commit, not a reason; put any note in text, or use {NO_CHANGE}")
    ref = str(payload.pop("ref", "") or "").strip()
    raw = str(payload.get("commit_hash") or "").strip()
    if not old:
        raise ValueError(f"{ticket_id} has no submitted candidate commit for a documentation commit to sit on; "
                         f"use {NO_CHANGE} with a reason, or {KICK_BACK} with findings")
    if not raw:
        raise ValueError(f"{SIGN_OFF} needs the documentation commit; without one, use {NO_CHANGE} with a reason")
    new = app._validate_commit_hash(raw).lower()
    if new == old:
        raise ValueError(f"{new[:12]} is the submitted candidate itself, so nothing was edited: use {NO_CHANGE} "
                         "with a reason")
    expected = candidate_ref(caller_role, ticket_id, old)
    if ref != expected:
        raise ValueError(f"a documentation commit for {ticket_id} is published at {expected}, not "
                         f"{ref or 'an unnamed ref'}; publish it with `switchyard-publish-candidate {ticket_id} "
                         f"--commit {new[:12]}`, which chooses that ref")
    published = _published_at(app, expected)
    if published != new:
        raise ValueError(f"{expected} is {'at ' + published[:12] if published else 'not published'} in the board's "
                         f"trusted commit cache, not at {new[:12]}; publish the commit there, then sign off again")
    parents = commit_cache.commit_parents(app.commit_git_dirs, new)
    if parents != [old]:
        shown = ", ".join(p[:12] for p in parents or []) or "none the board can read"
        raise ValueError(f"{new[:12]}'s parents are {shown}; a documentation commit has exactly one parent, the "
                         f"submitted candidate {old[:12]}. Commit on top of the candidate without rebasing or merging, "
                         f"or kick the ticket back with {KICK_BACK}")
    entries = commit_cache.changed_entries(app.commit_git_dirs, old, new)
    if entries is None:
        raise ValueError(f"the board cannot read what {new[:12]} changes against {old[:12]}, so it cannot tell that "
                         "it changes documentation only; nothing was moved")
    if not entries:
        raise ValueError(f"{new[:12]} changes nothing against the candidate {old[:12]}: an empty commit is not a "
                         f"documentation edit. When nothing a person reads needs to change, use {NO_CHANGE} with "
                         "a reason")
    outside = outside_documentation(entries)
    if outside:
        listed = "; ".join(outside[:LISTED_PATHS]) + (f"; and {len(outside) - LISTED_PATHS} more"
                                                      if len(outside) > LISTED_PATHS else "")
        raise ValueError(f"{new[:12]} changes more than documentation: {listed}. A documentation commit changes "
                         "documents only (Markdown, reStructuredText or AsciiDoc outside test data, and text or "
                         "images under docs/), as ordinary files; code, help text in code, tests and configuration "
                         f"are the implementer's -- kick the ticket back with {KICK_BACK}")
    paths = [path for _old, _new, _status, path in entries]
    listed = ", ".join(paths[:LISTED_PATHS]) + (f", and {len(paths) - LISTED_PATHS} more" if len(paths) > LISTED_PATHS else "")
    payload["commit_hash"] = new
    payload["text"] = (f"Documentation commit {new} replaces candidate {old} (its sole parent), published at "
                       f"{expected}. It changes {len(paths)} path{'' if len(paths) == 1 else 's'}: "
                       f"{listed or 'none'}. Every later review's sign-off is cleared; review starts again on "
                       f"{new[:12]}." + (f"\n\n{note}" if note else ""))
