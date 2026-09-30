# SYRD-517: a held ticket is not told its turn ended unresolved

## What happened

MEFP-114 was deliberately held (`manually_controlled=true`) in
director_review, awaiting an operator's export. Its Director kept receiving
"MEFP-114 is still yours and this turn ended without resolving it", and then
the grace escalation "director ended a turn without resolving MEFP-114 …
the grace period has passed". The Director's pane capture showed a delivery
after a 11:18 turn.

## Measured on current main (`f9d4f10`)

The board was built the production way: companion roles, `schema.sql`, the
real `ticket-board-migrate`, `rbac.sql`, a declared workflow. It used the
real `notify_unresolved_turn_end` generator and the real notify listener.

| path | held ticket |
|---|---|
| generation (owner prompt and grace escalation) | **skipped**, correctly: both candidate queries require `NOT ticket_turn_is_resolved(...)`, which counts a hold as resolved |
| delivery of a prompt or escalation queued **before** the hold | **delivered after the hold**: the defect |
| the next unresolved turn after the hold is lifted | prompted and escalated as usual |

**Why this repeats.** The listener's pending-delivery check
(`notification_eligibility._notification_is_current`) drops idle reminders,
nudges, triage and escalations for a held, parked or blocked ticket. It had
**no rule for `unresolved_turn_repair` or `unresolved_turn`**, so a row
queued before the hold kept its right to be delivered. While the Director's
pane is busy, such a row is requeued, and it surfaces at the next idle, turn
after turn.

**Current source versus installed release.** The generator's hold check
(pgu948, SYRD-194) is also in MEFP's older 49abeb4. So MEFP-114's prompts
are the delivery defect, which is present on current main, not an
old-release generator.

**SYRD-513/514 are a different mechanism.** There the owner is an unheld
worker actively working, and the questions are review-queue capacity and
activity detection. Nothing here changes them.

## The change

- **Delivery asks the question generation asks.** For the two unresolved-turn
  kinds, the listener calls `ticket_board.ticket_turn_is_resolved(ticket,
  now)`, the predicate the generator used before enqueueing. It covers a
  hold, an unresolved blocker, an active awaiting-role wait, no current
  owner and a terminal stage, so generation and delivery cannot disagree.
- **Stale rows are dropped.** A resolved turn's row is removed, not left to
  surface later.
- **Nothing real is lost.** Once the hold is lifted, the next unresolved turn
  is prompted and escalated as before.
- **`rbac.sql` grants the listener `ticket_turn_is_resolved`.** Every deploy
  reapplies `rbac.sql` after the migrations, and the function has existed
  since pgu948, so no migration is needed. Without the grant the listener is
  refused loudly ("permission denied"). That is deliberate: a silent
  fallback would hide the misconfiguration.
- **Other kinds are untouched.** Transitions, handoffs, idle reminders and
  escalations keep their existing rules.
- **No hold, gate or MEFP state changes.**

## Evidence

- **`tests/held_turn_end_reminder_test.py`: 9 checks.** Both scenarios run on
  production-built boards with the real generator and listener.
  - **Before:** f9d4f10's code and SQL (from `git archive`) run in a child
    process. The held ticket is not generated for, but the prompt and
    escalation queued before the hold are delivered after it: reproduced.
  - **This tree:**
    - the held ticket is neither generated for nor delivered anything;
    - an unheld ticket is prompted and escalated, so the probe arrives;
    - the rows queued before the hold are not delivered, and are dropped;
    - after unhold, the next turn is prompted once and escalated once after
      grace.
  - The test clears the pane's board identity (`TICKET_BOARD_*`) for itself
    and its child. It passes under `env -i` and in the pane environment. Its
    first pane-env run failed on exactly this, and without the clearing a
    listener that delivered nothing would have passed "nothing delivered
    after the hold".
- **Mutation: 4/4 killed.** Covered: delivery ignoring turn resolution; only
  the owner prompt checked; the listener not granted the question
  ("permission denied"); the condition inverted.
- **Sweep.** 72 suites touching the notify listener, eligibility,
  unresolved turns, idle nudges or RBAC were run serially under `env -i`,
  against `origin/main` (`f9d4f10`).
  - Pass/fail is identical for all 72.
  - The suites red on both trees match case by case (216 cases, failure
    lines included).
  - `notification_eligibility_boundary_test` (SYRD-272) pins the class's
    private methods. The first version added a helper method and turned it
    red; the query is now inline, and it passes at 56 cases.
