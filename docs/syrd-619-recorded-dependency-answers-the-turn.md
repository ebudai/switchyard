# SYRD-619: a recorded dependency answers the unresolved-turn prompt

## What happened

On 2026-10-18 SYRD-609 sat in `director_review`, owned by the Director. Its
reviewed integration was waiting on a polkit dialog only the User could
answer. From the live listener log and the ticket (times in UTC):

| time | event |
| --- | --- |
| 03:16:03 | The Director is prompted: SYRD-609 "is still yours and this turn ended without resolving it". |
| 03:16:35 | The Director answers with `request-dependency --role user`. |
| 07:17:01 | Notice 6790: "director ended a turn without resolving SYRD-609 (director_review), and the grace period has passed after being prompted it is still unresolved." Its `prompted_at` is the 03:16:03 prompt. |
| 07:17:50 on | A fresh prompt on each turn end (6792), and its escalation (6793). |
| 11:06:51 | The wait ends: the integration completed. |

07:17:01 is four hours and 26 seconds after the dependency.

## Why

Two things, and either alone produces a false escalation.

1. **The wait lapsed.** `ticket_turn_is_resolved` counted a wait only while
   `ticket_awaiting_role_is_active`, which is false four hours after the wait
   began. That window is the idle nudges' design (PGU-453): nudges resume
   after four hours, and the ticket stays visible in the Director's queue.
   The unresolved-turn guard borrowed the same window. Past it, a recorded
   dependency stopped counting as the owner's answer, although nothing about
   the dependency had changed.
2. **The answered prompt escalated.** The escalation half takes every prompt in
   the current assignment whose grace has run out and asks only whether the
   ticket is resolved *now*. Nothing recorded that a wait came after the
   prompt. So the moment the wait stopped counting, at the lapse, or when it
   is cleared even inside four hours, the prompt it had answered was reported
   as ignored.

## The change

All of it is in migration `pgu1009`, with `schema.sql` carrying the same
objects and `rbac.sql` the grant.

- **`ticket_notification_state.wait_recorded_at`.** This is when the current or
  latest wait was recorded, and it is never cleared.
  - A `BEFORE INSERT OR UPDATE OF awaiting_role, awaiting_since_at` trigger
    stamps it whenever a role is named where none was, or a different role.
    So every writer of a wait is covered.
  - The migration backfills a standing wait from its `awaiting_since_at`.
- **`ticket_turn_end_is_resolved(ticket, now)`.** This is
  `ticket_turn_is_resolved`, or a recorded wait however long it has waited.
  Three places ask it:
  - the unresolved-turn generator's prompting half;
  - its escalation half;
  - the listener's delivery check for `unresolved_turn_repair` and
    `unresolved_turn`.

  So the generator and delivery cannot disagree (SYRD-517).
- **An answered prompt is finished.** A prompt older than `wait_recorded_at`:
  - never escalates, now or after the wait clears;
  - no longer opens an episode (SYRD-538), so the next unresolved turn after
    the wait clears is prompted afresh, even inside the old prompt's grace.

## What governs a waiting ticket instead

The dependency's own path, unchanged:
- **A pane role:** the awaiting-role handoff, which sends three notices to that
  role and then one to the Director at 30 minutes.
- **The User:** nothing is sent. The wait is shown in the Director's attention
  queue (SYRD-549).

Once the wait is cleared, by the owner, the Director, the awaited role acting,
or the ticket moving, ordinary handling resumes. The next unresolved turn
prompts the owner, and a grace after the prompt reaches them the Director is
told once.

## Deliberately unchanged

- **Idle nudges** keep the four-hour window and its documented reason
  (`app.py`'s awaiting_role comment).
- **Stall episodes and their escalation (SYRD-595)** still ask
  `ticket_turn_is_resolved`. The ticket is about the unresolved-turn prompt and
  its grace escalation. Whether a four-hour wait should also silence a stall
  alert is a separate decision.
- **Stage gates** are untouched, and no ticket is silenced without a recorded
  `awaiting_role`.

## Proof

`tests/recorded_dependency_unresolved_turn_test.py` runs on a production-built
board: companion roles, `schema.sql`, the migrate runner, `rbac.sql` and the
inspection workflow. It uses the real notify listener, one loop iteration at a
time, and the real `PaneActivityGate` reading real hook files. The grace is a
few seconds and passes for real. A wait's four hours are not waited out: its
`awaiting_since_at` is moved back four hours and a minute.

On main (`3ab25df`), from a git archive:
- **U** (SYRD-609's shape): past four hours, a `director_review` ticket still
  waiting on the User escalates its answered prompt, and the prompts come back.
- **H:** a prompt held behind a menu when the owner recorded a wait is delivered
  once the wait passes four hours.
- **C:** inside four hours, clearing a wait escalates the prompt it answered, at
  once.

On this tree:
- **U and H:** nothing about an unresolved turn is queued or sent while the wait
  stands. The held prompt is dropped, and ops's handoff schedule is armed as
  before.
- **U, H and C, after the wait clears:** nothing escalates. Then one
  unresolved turn prompts the owner, and the Director is told once, a grace
  after delivery.
- **E:** a wait recorded and cleared inside the prompt's grace; the next
  unresolved turn is prompted.
- **X1:** across an upgrade, the backfill keeps a standing wait as the answer
  to the prompt before it.
- **X2:** a prompt main queued after the lapse, still on the board at the
  upgrade, is neither delivered nor escalated.
- **A fresh install** has the same functions, owner, grants and trigger as a
  migrated board.
