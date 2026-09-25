#!/usr/bin/env python3
"""SYRD-264: a highlighted card says when its owner's notice did not arrive.

The highlight means "this is the owner's current work" -- MEFP-1's card had it
while its only notice was dead-lettered, and the User read the highlight as
"Ops has been notified". The card now carries one line when the notice has not
been delivered, and nothing once it has.

The function is executed, not grepped: its source is taken from the page the
board actually serves and run under node with a minimal DOM. (The browser
suites need Playwright's own browser build, which this environment lacks; this
runs the same code without one.)
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML  # noqa: E402

CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def function_source(name: str) -> str:
    """One top-level `function name(...) { ... }` from the served page, braces balanced."""
    start = HTML.index(f"function {name}(")
    depth = 0
    for index in range(HTML.index("{", start), len(HTML)):
        if HTML[index] == "{":
            depth += 1
        elif HTML[index] == "}":
            depth -= 1
            if depth == 0:
                return HTML[start:index + 1]
    raise AssertionError(f"unbalanced function {name}")


def render(tickets: list[dict]) -> list[dict | None]:
    program = """
const document = { createElement: (tag) => ({ tag, className: '', textContent: '', title: '' }) };
const roleLabel = (role) => ({ ops: 'Ops', main: 'Main' }[role] || role);
%s
const tickets = %s;
const out = tickets.map((ticket) => {
  const line = activeWorkDeliveryLine(ticket);
  return line ? { className: line.className, text: line.textContent, title: line.title } : null;
});
process.stdout.write(JSON.stringify({ out }));
""" % (function_source("activeWorkDeliveryLine"), json.dumps(tickets))
    proc = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["out"]


def hints(tickets: list[dict]) -> list[str]:
    """What the same tickets' cards carry on hover (renderCard's card.title)."""
    program = """
const roleLabel = (role) => ({ ops: 'Ops', main: 'Main', director: 'Director' }[role] || role);
%s
process.stdout.write(JSON.stringify(%s.map((ticket) => activeWorkDeliveryHint(ticket))));
""" % (function_source("activeWorkDeliveryHint"), json.dumps(tickets))
    proc = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def main() -> int:
    if not shutil.which("node"):
        raise SystemExit("active_work_delivery_frontend_test: node is required")
    check("const deliveryLine = activeWorkDeliveryLine(ticket);" in HTML, "renderCard uses it")
    check(".card-delivery-failed" in HTML, "and it is styled")

    base = {"active_work_highlight": True, "active_work_owner_role": "ops", "assignee": "ops"}
    # SYRD-268 + SYRD-266: an unconfirmed notice adds no persistent card line
    # -- the highlight is the cue -- and its diagnosis is on hover. It is
    # never shown as delivered.
    unconfirmed_tickets = [
        {**base, "active_work_owner_role": "director", "assignee": "director", "active_work_delivery": {
            "state": "unconfirmed", "at": "2026-09-24T21:51:49-04:00", "reason": "no_submission_witnessed"}},
        {**base, "active_work_delivery": {
            "state": "unconfirmed", "at": "2026-09-24T21:52:00-04:00", "reason": "no_hook_state"}},
        {**base, "active_work_highlight": False, "active_work_delivery": {"state": "unconfirmed", "reason": "x"}},
    ]
    check(render(unconfirmed_tickets) == [None, None, None], "an unconfirmed notice adds no card line")
    witnessed_not, no_state, unhighlighted_hint = hints(unconfirmed_tickets)
    check(witnessed_not == "Sent to Director, not confirmed received · no_submission_witnessed · 2026-09-24T21:51:49-04:00",
          f"MEFP-1's card says on hover that the notice was sent and not seen to arrive: {witnessed_not!r}")
    check(no_state == "Sent to Ops, not confirmed received · no_hook_state · 2026-09-24T21:52:00-04:00",
          f"with the listener's reason when there was nothing to read: {no_state!r}")
    check(unhighlighted_hint == "", "a ticket that is not current work carries no hint")
    check("const hint = activeWorkDeliveryHint(ticket);" in HTML and "card.title = hint;" in HTML,
          "renderCard puts it on the highlighted card's hover")
    check(".card-delivery-unconfirmed" not in HTML, "and there is no unconfirmed card-line style")
    failed, pending, none, delivered, unhighlighted, legacy = render([
        {**base, "active_work_delivery": {
            "state": "failed", "reason": "tmux_target_missing", "at": "2026-09-24T19:25:13-04:00", "attempts": 1}},
        {**base, "active_work_delivery": {
            "state": "pending", "attempts": 3, "reason": "runtime_assignment_unresolved",
            "next_attempt_at": "2026-09-24T19:30:00-04:00"}},
        {**base, "active_work_delivery": {"state": "none"}},
        {**base, "active_work_delivery": {"state": "delivered", "at": "2026-09-24T19:26:00-04:00"}},
        {**base, "active_work_highlight": False, "active_work_delivery": {"state": "failed", "reason": "x"}},
        # A board from before this change sends no delivery field at all.
        {**base},
    ])

    check(failed and failed["text"] == "Not delivered to Ops: tmux_target_missing",
          f"MEFP-1's card says the notice failed and why: {failed}")
    check(failed["className"] == "card-delivery card-delivery-failed", f"{failed}")
    check("2026-09-24T19:25:13-04:00" in failed["title"], f"with when on hover: {failed}")
    check(pending and pending["text"] == "Not yet delivered to Ops (attempt 3)", f"pending says so: {pending}")
    check("runtime_assignment_unresolved" in pending["title"], f"with the last error on hover: {pending}")
    check(none and none["text"] == "No notice recorded for Ops", f"no notice says so: {none}")
    check(delivered is None, "a delivered notice adds nothing to the card")
    check(unhighlighted is None, "a ticket that is not current work adds nothing")
    check(legacy is None, "and an older board's payload renders as before")

    print(f"active_work_delivery_frontend_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
