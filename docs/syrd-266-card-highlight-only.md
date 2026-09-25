# SYRD-266: the highlight is the card's cue; the notice status is on hover and in the ticket

## Ask

After SYRD-264 the User found the notice status line on a highlighted card
redundant with the highlight. They asked for:
- no extra persistent status line or indicator on the ordinary card;
- the delivery state and trace kept for diagnosis;
- useful failure information kept in a drill-down or tooltip;
- no silent claim of delivery;
- both pending and failed checked;
- no change to delivery behaviour.

## Before

`activeWorkDeliveryLine` (SYRD-264) added a coloured line under a
highlighted card's assignee for three states:
- failed: "Not delivered to Ops: tmux_target_missing";
- pending: "Not yet delivered to Ops (attempt 3)";
- none: "No notice recorded for Ops".

SYRD-268 had already kept its own state, `unconfirmed`, off the card and on
the card's hover.

## Change

- **The card.** The line and its `.card-delivery*` styles are removed. A
  highlighted card carries the highlight, nothing more.
- **Hover.** `activeWorkDeliveryHint` gives every state that has not arrived
  its diagnosis:

  | state | hover text |
  |---|---|
  | failed | "Not delivered to Ops: <reason> · <when>" |
  | pending | "Not yet delivered to Ops (attempt N) · <last error> · next try <when>" |
  | unconfirmed | "Sent to Director, not confirmed received · <reason> · <when>" (unchanged) |
  | none | "No notice recorded for Ops" |

  `renderCard` sets it as the highlighted card's `title`.
- **Ticket view.** Hover does not exist on a phone, so the ticket view (the
  drill-down) shows the same text as "Notice: …" under Created/Updated.
- **Delivered.** A delivered notice produces no text anywhere, so nothing is
  claimed either way.
- **Not current work.** A ticket that is not the owner's current work shows
  nothing.
- **The API is unchanged.** `active_work_delivery` and the notification
  trace are unchanged, and no listener or backend code is touched.

## Evidence

- **`tests/active_work_delivery_frontend_test.py`: 25 checks.** It runs the
  **served** `renderCard` and `activeWorkDeliveryHint` under node with a
  minimal DOM. Playwright's browser build is not available here.
  - For failed, pending, unconfirmed, none and delivered, the card keeps its
    highlight. It really renders its id, title and assignee, so the
    remaining checks are not vacuous. Nothing on it mentions delivery,
    notices or receipt.
  - The card's hover carries the exact text for each state, and nothing
    when delivered.
  - An unhighlighted ticket and an older board's payload carry nothing.
  - The hint says nothing for a ticket that is not current work, because the
    ticket view asks it directly.
  - The ticket view renders it. This one is a source check: `renderDetail`
    is too large to run under a stub DOM.
- **Mutation: 8/8 killed.** Covered: a status line put back on the card;
  failed losing its hint; delivered claiming delivery; the hint on a ticket
  that is not current work; the hover never set; the ticket view never
  showing it; pending losing its next try; unconfirmed reading as "no
  notice". The first run's "put back" mutant died of a JavaScript error, not
  the assertion. It was re-aimed at the line's old position, where it dies
  on "says nothing about its notice on the card itself". "Hint on a ticket
  that is not current work" survived that first run because `renderCard`
  guards it. The direct hint check was added for the ticket view, and it is
  killed.
- **Sweep.** 93 suites that load the served page, its scripts or the tickets
  API were run serially under `env -i`, on this branch and on `origin/main`
  (`87f0456`).
  - Exit codes are identical.
  - The 21 red on both trees end on the same final line.
  - The one of those with `test_` functions matches case by case.
  - Most of the 21 stop at the missing-Playwright banner before rendering
    anything, on both trees. No browser suite refers to the removed line or
    its styles.
