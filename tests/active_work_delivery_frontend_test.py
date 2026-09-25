#!/usr/bin/env python3
"""A highlighted card's notice status: on hover and in the ticket, not on the card.

SYRD-264 made a highlighted card say when its owner's notice had not
arrived: MEFP-1 was highlighted as Ops's work while its only notice was
dead-lettered, and the User read the highlight as "Ops has been notified".
SYRD-268 added "sent, not confirmed received". The User then found a status
line beside the highlight redundant (SYRD-266). So the card carries only the
highlight, and why the notice has not arrived -- failed, pending, unconfirmed
or never recorded -- is on the card's hover and in the ticket view. A
delivered notice claims nothing anywhere.

The functions are executed, not grepped: their source is taken from the page
the board actually serves, and `renderCard` itself runs under node with a
minimal DOM. (The browser suites need Playwright's own browser build, which
this environment lacks; this runs the same code without one.)
"""

from __future__ import annotations

import json
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


# A DOM just large enough for renderCard; every piece of text it sets is kept.
FAKE_DOM = """
function element(tag) {
  const el = {
    tag, className: '', textContent: '', title: '', children: [], dataset: {}, style: {}, attributes: {},
    classList: { add: (...names) => { el.className = [el.className, ...names].filter(Boolean).join(' '); } },
    appendChild: (child) => { el.children.push(child); return child; },
    append: (...children) => { children.forEach((child) => el.children.push(child)); },
    addEventListener: () => {},
    setAttribute: (name, value) => { el.attributes[name] = value; },
  };
  return el;
}
const document = { createElement: element };
function texts(el) {
  return [el.textContent, el.title, ...Object.values(el.attributes), ...el.children.flatMap(texts)].filter(Boolean);
}
const state = { selectedId: '' };
const roleLabel = (role) => ({ ops: 'Ops', main: 'Main', director: 'Director' }[role] || role);
const badge = (text, className) => { const el = element('span'); el.className = className || ''; el.textContent = text; return el; };
const manualBlockedSummary = () => '';
const userSignoffSummary = () => '';
const unresolvedBlockedBy = () => [];
const ticketScreenshotEntries = () => [];
const renderAlertStack = () => null;
const openDetail = () => {};
"""


def render_cards(tickets: list[dict]) -> list[dict]:
    """The served renderCard's output: the card's hover, and every text inside it."""
    program = FAKE_DOM + "\n".join(
        function_source(name) for name in ("activeWorkDeliveryHint", "renderCard")
    ) + """
const out = %s.map((ticket) => {
  const card = renderCard(ticket);
  return { title: card.title, className: card.className, texts: card.children.flatMap(texts) };
});
process.stdout.write(JSON.stringify(out));
""" % json.dumps(tickets)
    proc = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def ticket(delivery: dict | None, *, highlight: bool = True, owner: str = "ops") -> dict:
    record = {
        "id": "MEFP-1", "title": "Queue", "state": "in_progress", "assignee": owner,
        "active_work_highlight": highlight, "active_work_owner_role": owner,
        "needs_audit": False, "blocked_by": [], "comments": [],
    }
    if delivery is not None:
        record["active_work_delivery"] = delivery
    return record


def main() -> int:
    if not shutil.which("node"):
        raise SystemExit("active_work_delivery_frontend_test: node is required")

    failed, pending, unconfirmed, none, delivered, unhighlighted, legacy = render_cards([
        ticket({"state": "failed", "reason": "tmux_target_missing", "at": "2026-09-24T19:25:13-04:00", "attempts": 1}),
        ticket({"state": "pending", "attempts": 3, "reason": "runtime_assignment_unresolved",
                "next_attempt_at": "2026-09-24T19:30:00-04:00"}),
        ticket({"state": "unconfirmed", "reason": "no_submission_witnessed", "at": "2026-09-24T21:51:49-04:00"},
               owner="director"),
        ticket({"state": "none"}),
        ticket({"state": "delivered", "at": "2026-09-24T19:26:00-04:00"}),
        ticket({"state": "failed", "reason": "x"}, highlight=False),
        # A board from before SYRD-264 sends no delivery field at all.
        ticket(None),
    ])

    # The card: the highlight, and no status line of any kind.
    for name, card in (("failed", failed), ("pending", pending), ("unconfirmed", unconfirmed),
                       ("none", none), ("delivered", delivered)):
        check("card-active-work" in card["className"], f"a {name} card keeps its highlight: {card['className']}")
        on_card = " | ".join(card["texts"])
        check({"MEFP-1", "Queue"} <= set(card["texts"]) and any(text in card["texts"] for text in ("Ops", "Director")),
              f"the card really rendered its id, title and assignee ({name}): {on_card}")
        check(not any(word in on_card.lower() for word in ("deliver", "notice", "confirmed", "received")),
              f"and says nothing about its notice on the card itself ({name}): {on_card}")
    check(".card-delivery" not in HTML and "activeWorkDeliveryLine" not in HTML,
          "the card status line and its styles are gone from the served page")

    # The hover: why the notice has not arrived, never a claim that it did.
    check(failed["title"] == "Not delivered to Ops: tmux_target_missing · 2026-09-24T19:25:13-04:00",
          f"MEFP-1's shape: failed, why and when, on hover: {failed['title']!r}")
    check(pending["title"] == ("Not yet delivered to Ops (attempt 3) · runtime_assignment_unresolved"
                               " · next try 2026-09-24T19:30:00-04:00"),
          f"pending: attempts, last error and next try: {pending['title']!r}")
    check(unconfirmed["title"] == ("Sent to Director, not confirmed received · no_submission_witnessed"
                                   " · 2026-09-24T21:51:49-04:00"),
          f"unconfirmed, as SYRD-268 left it: {unconfirmed['title']!r}")
    check(none["title"] == "No notice recorded for Ops", f"no notice: {none['title']!r}")
    check(delivered["title"] == "", f"a delivered notice claims nothing: {delivered['title']!r}")
    check(unhighlighted["title"] == "" and "card-active-work" not in unhighlighted["className"],
          "a ticket that is not current work carries no hint")
    check(legacy["title"] == "", "and an older board's payload renders as before")

    # The ticket view asks the hint directly, so the hint itself must say
    # nothing for a ticket that is not the owner's current work.
    program = FAKE_DOM + function_source("activeWorkDeliveryHint") + """
process.stdout.write(JSON.stringify(%s.map((ticket) => activeWorkDeliveryHint(ticket))));
""" % json.dumps([ticket({"state": "failed", "reason": "x"}, highlight=False),
                  ticket({"state": "failed", "reason": "x"})])
    proc = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    not_current, current = json.loads(proc.stdout)
    check(not_current == "" and current == "Not delivered to Ops: x",
          f"the hint speaks only for current work: {not_current!r} / {current!r}")

    # The ticket view: the same diagnosis, where hover cannot reach.
    detail = function_source("renderDetail")
    check("const deliveryHint = activeWorkDeliveryHint(ticket);" in detail
          and "deliveryLine.textContent = `Notice: ${deliveryHint}`;" in detail
          and "meta.appendChild(deliveryLine);" in detail,
          "renderDetail shows the same text in the ticket view")

    print(f"active_work_delivery_frontend_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
