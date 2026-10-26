# SYRD-679: a snooze's deadline notice is about the tickets still snoozed

## What happened

**Batch 50** snoozed SYRD-677, and SYRD-677 reached Done before the deadline. **Batch 51** snoozed SYRD-684, which
reached Done at 03:26 EDT, four minutes before its 03:30 deadline. Each snooze then read `invalidated`, listing the
fields that changed. At each deadline the Director was still sent:

> Reminder snooze batch 51 is due (…). Ready: none. Changed while snoozed: SYRD-684 (… changed; now done, …).

Two places let it through:

- **Writing.** `emit_due_reminder_snoozes` (SYRD-537) wrote the batch's notice whenever the batch had a member,
  ready or not.
- **Sending.** The delivery check passed any `reminder_snooze_due` notice without asking anything ("the ticket it
  hangs on proves nothing"). So a ticket completed after the notice was written did not stop it either.

## When the notice is written

A member is **ready** when it is still the ticket that was snoozed: its snapshot is unchanged. The snapshot covers
the stage, owner, candidate, every sign-off and gate, holds, waits and blockers, so completion always changes it.

At the deadline, every member is released as before, each with its reason. Then:

- **A batch with a ready ticket** gets its one notice. The notice hangs on the first ready ticket and still says what
  changed while snoozed.
- **A batch whose every member changed** gets no notice. It closes (`due_emitted`, no `notification_id`) with
  `close_reason` "due; every ticket changed while snoozed, so no notice". Since it is no longer `snoozed`, no later
  pass looks at it again. The changed tickets' ordinary reminders resumed when they changed.

## When the notice is sent

`ticket_board.reminder_snooze_due_stale_reason(payload)` answers NULL while the notice is still to be sent, or else
why not:

| Reason | When |
|---|---|
| `batch_missing` | The payload names no batch that exists. |
| `batch_mismatch` | The batch has no notice: still open, cleared, or closed with nothing ready. |
| `no_ticket_still_snoozed` | No ticket the batch released as ready (`release_reason = 'due'`) is still the one snoozed and not snoozed again in a later batch. |

The listener asks it through `reminder_snooze.due_stale_reason`, as the delivery check, at the three points every
notice is checked: at claim, before the send, and after a hold.

**What the listener does with a stale notice.** It drops it as any stale notice is dropped:

- it is acknowledged, which removes the queue row, so it is never retried;
- the drop is traced with `snooze_premise: <reason>`.

Removing the row also ends the hold that `ticket_reminders_snoozed` kept on the members while their notice was
queued.

## Holding every ready ticket through the send

The last check happens under the delivery hold (SYRD-631). It holds the notice's ticket row `FOR SHARE`, in a
transaction of its own, from the final read through the send. Any write that could retire the notice locks that row
first, so it waits until the send is over.

**The gap Audit found.** For a deadline notice that row is only the anchor, the first ready ticket. The notice stays
current while *any* ready ticket is unchanged. Suppose a batch has two ready tickets:

1. The anchor is completed before the claim. The other ticket still keeps the notice current.
2. The other ticket is then completed while the notice is being sent.

Nothing held that second ticket's row, so its completion committed mid-send. The Director was told about a batch
whose last ready ticket was already done.

**The fix.** `ticket_board.hold_reminder_snooze_due(payload)` runs in the hold's transaction, after the anchor's row is
held. It holds `FOR SHARE NOWAIT` the row of every ticket the batch released as ready (`release_reason = 'due'`).
Those are exactly the rows `reminder_snooze_due_stale_reason` reads.

- A write to any of them waits for the send. That covers completing a ticket, returning it, and snoozing it again
  (`snooze_reminders` locks each ticket `FOR UPDATE`).
- If one is being written right now, the function returns false at once. The notice is requeued with "board write in
  flight" and waits a pass, as for a busy anchor. The listener never waits on a row.
- Tickets that changed before the deadline, and other batches' tickets, are not held.

The listener passes the payload to `hold_for_delivery`, which asks `reminder_snooze.hold_due_members`. For any other
notice that is a no-op: no query, and the anchor hold as before.

**Two details:**

- A ticket the Director cleared early is unchanged, but it was not released as ready, so it keeps no notice
  current.
- A ticket snoozed again in a later batch is that batch's to report.

**Grants.** Only the listener may ask: `rbac.sql` grants it EXECUTE. `rbac.sql` revokes EXECUTE on every function
from every principal and grants each explicitly, after the migrations, on every deploy, so the migration's own grant
line only matters until then.

## Tests

`tests/reminder_snooze_due_premise_test.py` runs on a production-built board (schema.sql, the real migrations,
rbac.sql, a declared workflow). The real notify listener writes and delivers the notices, and tickets complete
through the board's own `mark_done`. Main runs first, from a git archive.

**Each batch:**

| Batch | What happens |
|---|---|
| `done-before` | Completed before the deadline (SYRD-677, SYRD-684). |
| `kicked-before` | Returned to App before the deadline. |
| `done-after` | Completed after the notice was written. |
| `done-in-send` | Completed between the listener's claim and its pre-send recheck. |
| `snoozed-again` | Snoozed again after the notice was written. |
| `cleared-then-done` | One ticket cleared early; the other completed after the notice was written. |
| `valid` | Nothing changes. |
| `mixed` | One ticket completed; the other unchanged. |
| `last-ready-in-send` | Two ready; the anchor completed before the claim; the sender itself completes the other (Audit's reproduction). |
| `member-busy` | A write holds the second ready ticket's row when the listener comes to send. |

**Main:** every batch's notice reaches the Director, `done-before`'s reading "Ready: none". In
`last-ready-in-send` the last ready ticket completes inside the send, and in `member-busy` the notice is sent while
the write holds a ready ticket.

**This tree:**

- `done-before` and `kicked-before` write no notice, and close saying why.
- The four races are dropped with `no_ticket_still_snoozed` (`done-in-send` at `pre_send_recheck`) and never sent.
- `valid` and `mixed` are sent once.
- `last-ready-in-send` is sent once. The last ready ticket's completion waits for the send, then lands.
- `member-busy` is requeued with "board write in flight" and sent once on the next pass.
- The hold covers a batch's ready tickets only: not a changed ticket, not another batch's ticket.
- An ordinary notice is still held and sent once. A stale ordinary notice is dropped with no snooze premise in its
  trace.
- Two more passes write and send nothing for any batch.
- The batch identity is checked first.
- Only the listener may ask or hold.
- A board built from schema.sql alone has the migration's functions.
