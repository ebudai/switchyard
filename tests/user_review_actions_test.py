#!/usr/bin/env python3
"""Regression test: user_review tickets expose explicit sign-off and kickback actions."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert "function ticketIsUserReview(ticket)" in HTML
    assert "const commentWho = document.createElement('select');" in HTML
    assert "commentWho.setAttribute('aria-label', 'Comment author');" in HTML
    assert "state.callerRoles = payload.caller_roles || [];" in HTML
    assert "const commentAuthorRoles = state.callerRoles.length" in HTML
    assert "['director', 'main', 'app', 'ops', 'perf', 'audit', 'inspector', 'research', 'user'];" in HTML
    assert "commentAuthorRoles.forEach((role) => {" in HTML
    assert "buildOption(commentWho, role, roleLabel(role));" in HTML
    assert "commentWho.value = ticketIsUserReview(ticket) ? 'user' : 'director';" in HTML
    assert "commentWho.type = 'text';" not in HTML
    assert "commentWho.placeholder = 'who';" not in HTML
    assert "userBannerSignoffButton.type = 'button';" in HTML
    assert "userBannerSignoffButton.textContent = 'Sign Off';" in HTML
    assert "const patch = { user_signoff: true };" in HTML
    assert "const patch = { user_signoff: true, state: 'done' };" not in HTML
    assert "setCreateStatus(`Signed off ✓ ${ticketId} moved to Final Sign-Off.`);" in HTML
    assert "toggleControl('UAT sign-off', ticket.user_signoff" in HTML
    assert "toggleControl('Needs audit', ticket.needs_audit !== false" in HTML
    assert "Ticket does not require audit sign-off." in HTML
    assert "toggleControl('Requires UAT', ticket.needs_user_signoff" in HTML
    assert "disabled: !ticket.needs_user_signoff" in HTML
    assert "Ticket does not require UAT sign-off." in HTML
    assert "if (ticket.needs_user_signoff) {\n        toggles.appendChild(toggleControl('UAT sign-off'" not in HTML
    assert "toggleControl('UAT sign-off', ticket.needs_user_signoff" not in HTML
    assert "toggleControl('Needs UAT', ticket.needs_user_signoff" not in HTML
    assert "if (signoffRecorded) {" in HTML
    assert "signedOffState.textContent = 'Signed Off ✓';" in HTML
    assert "setCreateStatus(error.message, true);" in HTML
    assert "throw new Error('comment requires an author');" in HTML
    assert "throw new Error('comment requires non-empty text');" in HTML
    assert "if (!trimmedText && !nextState) {" in HTML
    assert "setCreateStatus(nextState ? `Moved ${ticketId} to ${stateLabel(nextState)}.`" in HTML
    assert "kickbackButton.textContent = 'Return for Rework';" in HTML
    assert "ticket.state === 'audit'" in HTML
    assert "kickbackButton.textContent = 'Comment + Return to Implementation';" in HTML
    assert "await submitComment(ticket.id, 'inspector', commentText.value, 'in_progress');" in HTML
    assert "kickbackButton.textContent = 'Kick Back -> Implementation';" not in HTML
    assert "await submitComment(ticket.id, commentWho.value, commentText.value, 'analysis');" in HTML
    assert "await updateTicketAction(ticketId, 'user_reopen', { reason: actionReason(patch) }, normalizedCaller);" in HTML
    assert "nextState === 'analysis' &&\n          patch.comment &&" not in HTML
    assert "await submitComment(ticket.id, commentWho.value, commentText.value, 'in_progress');" in HTML
    print("user_review_actions_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
