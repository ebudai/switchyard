# SYRD-850: the plan gate on the board page -- each ticket's deliverable, a deliverable filter and a plan view

SYRD-843 made the plan gate: an approved three-level plan (milestones, deliverables, tickets), each ticket scoped to
a deliverable or marked unplanned with a reason, and `ticket_board.plan_status()` reporting it all. The board page
showed none of it. This adds the page's half, on a board whose declared workflow runs the gate (the `plan` policy).
Every other board shows nothing new.

## The service

- **`GET /api/plan`** (`scripts/ticket_board/plan_view.py`, registered in `extension_operations.READ_ROUTES`) answers
  `{"plan": plan_status()}`:
  - the policy;
  - the approved commit, its plan file and line count, the plan ticket that approved it and when;
  - each milestone, in the plan's order, with `detailed`, `replan_due` and its deliverables, each with its tickets
    counted by stage;
  - the unplanned tickets counted by stage.

  It answers `{"plan": null}` on a board without the gate: one that never ran it, one older than SYRD-843, and one
  whose gate was turned off and still holds the records. It is read-only, and the service's grant to
  `plan_status()` is SYRD-843's.
- **`plan_scope` in the ticket JSON,** in the ticket read and the board read alike:
  `{"deliverable": "D2.1", "unplanned_reason": null}`, or `{"deliverable": null, "unplanned_reason": "..."}`, as the
  Director set it with `set-deliverable`. It is null when neither is set (a plan ticket, a ticket not scoped yet) and
  on any board without the gate. The read query adds the column only while the declared workflow has a `plan`
  policy, so a board without the gate never reads `ticket_deliverables`. The service reads that table through
  rbac.sql's schema-wide SELECT, which every deploy re-applies.

No migration: everything read here is SYRD-843's (pgu1052).

## The page (`scripts/ticket_board/frontend_script_plan.py`)

- **The ticket view** has one more line among its details. It reads "Deliverable: D2.1 Reader, of milestone M2
  (Ingest)", in the same words as the board's comment when the Director sets one, or "Unplanned: " and the
  Director's reason. A ticket with neither has no line.
- **The filter,** "Deliverable", sits beside the Show switches. It offers "All tickets", each deliverable under its
  milestone, and "Unplanned". It keeps every column to the chosen tickets, and the column counts follow.
- **The plan view** ("Plan") is a dialog in the ticket view's style. It shows, in order:
  - the approved plan file, its length, the commit and the plan ticket that approved it, or that none is approved
    yet;
  - each milestone in the plan's order, marked "Not detailed yet" when it is not, and "Re-plan due" when every
    ticket of it is Done or cancelled (SYRD-843's notice);
  - under each milestone, its deliverables, each with its tickets counted by stage, in the board's stage order and
    with the stages' own labels ("Analysis 1 · Implementation 1"), or "no tickets";
  - the unplanned tickets, counted apart.

  A deliverable, or "Unplanned", is a button: it closes the view and filters the board to those tickets. Escape, the
  close button or a click outside closes it.
- The page asks `/api/plan` on load, every 15 seconds, and whenever the plan view opens. While the answer is null,
  the controls keep `hidden`, the filter lets every ticket through, and nothing else changes.

## Not changed

- **The CLI.** `ticket-board-read ticket --json` carries `plan_scope` like any reader of the ticket JSON, but its
  text view has no new line.
- **Cards.** A card stays as it was. The deliverable is in the ticket view, as the board keeps per-ticket detail off
  the card (SYRD-266).

## Evidence

`tests/plan_board_view_browser_test.py`, in Chromium at desktop size and at a phone's.
- **The plan-gated board** is built as `tests/plan_gate_test.py` builds it: main's tree from before the plan gate,
  upgraded with this tree's migrations and grants, the real server and listener. The plan is approved by a plan
  ticket taken through Audit, DAT, User Review and Final Sign-Off, and closed on its integration commit.
- **Main first**, from a git archive of the SYRD-843 candidate: the gate works there, but there is no `/api/plan`
  (404), no `plan_scope`, and none of the page's plan elements.
- **The candidate:**
  - `/api/plan` equals `plan_status()`, and each ticket's `plan_scope` is right in both reads;
  - the filter's options, and the cards each option leaves;
  - the ticket view's line for a deliverable, for an unplanned ticket, and none for a ticket with neither;
  - the plan view's approved line, its milestones and badges, its counts, the unplanned apart, nothing wider than
    the window, a deliverable opening the filtered board, and Escape;
  - no page errors.
- **Without the gate:** the same board after the gate is turned off, with its records still in place, and a board
  that never ran it. On both, `{"plan": null}`, every `plan_scope` null, the controls hidden, no plan line and every
  ticket shown.
