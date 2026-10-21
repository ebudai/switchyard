# SYRD-631: a held notice is asked again before it is sent

## What happened

On 2026-10-18, SYRD-614 was assigned to Ops and queued behind Ops's active
work (SYRD-613). SYRD-613's bounded background comparison was holding Ops's
pane.

At 14:17:56 the Director blocked SYRD-614 on SYRD-630. The board queued
notification 6896 for Ops, kind `ticket_update`: "SYRD-614 … was changed by
director: blockers -- re-read it before continuing."

The listener requeued it behind the busy pane about twenty times. At 15:23:24 it
delivered it, once the pane was free. The board meanwhile, correctly, showed
SYRD-614 as inactive, with active-work delivery state `none` and `waiting_on`
SYRD-630. The listener log
(`~/.local/state/syrd-ticket-board-notify-listener.log`) holds the whole
sequence. Notification 6896 was the only notice held for SYRD-614.

Ops was told to continue a ticket that was neither its active work nor workable.

## Why it got through

Two gaps, one of which was this incident.

1. **Blocked queued work kept its owner notices.** `_notification_is_current`
   has a rule from SYRD-582: an owner notice about a ticket queued behind the
   owner's active work is not the owner's work yet, so it is dropped. The
   ticket's activation later hands it over once, with every change. That rule
   was deliberately skipped for blocked tickets, "whose owner still hears what
   changed". SYRD-614 was both queued and blocked, so its notice was kept as
   current at every claim, held only by the pane, and then sent.

2. **Only `awaiting_role` was rechecked before the send.** The listener asks
   whether a notice is current when it claims it. It then probes the pane,
   possibly twice, with a recheck delay and background-child sampling, and asks
   again just before sending only for `awaiting_role` handoffs (SYRD-99). A
   blocker, reassignment, terminal move or new serial order landing after the
   claim did not stop the send.

## The change

- **The serial rule now covers blocked work that waits behind another active
  ticket, unless the owner was handed it.** In a declared implementation stage,
  an owner notice about a ticket that is not the owner's active serial work
  (`serial_queue().active` false) is dropped. That now includes a blocked
  ticket, when two things hold:
  - **The owner has a different active ticket** (`serial_queue().active_ticket`
    is set and is not this ticket). An owner whose only work this is, blocked
    and nothing else in hand, is still told, as SYRD-148's operator wait
    requires.
  - **The owner was not handed this ticket in this visit to the stage.**
    "Handed" means a `transition` send to that role is traced since the ticket
    entered its current state, the same evidence the board shows as delivered
    active work. A worker already on the ticket still hears that it was
    blocked, even after its next ticket is activated. A hand-over in an earlier
    visit does not count, and neither does a comment.

  A first version dropped every blocked ticket's owner notice unless it had been
  handed over. The VM comparison showed why that was too wide:
  operator_wait_test's Ops, whose only, seeded ticket was blocked, was no
  longer told its wait was recorded.

- **Every notice is rechecked immediately before the send.**
  `NotificationEligibility.drop_if_stale` asks `_notification_is_current` and,
  when the notice is stale, traces `drop` (busy reason `stale_notification`)
  and `listener_ack`, then acks and forgets it.
  - The listener calls it at claim, unchanged, and again just before the send
    with phase `pre_send_recheck`, for every kind. Before, only `awaiting_role`
    was rechecked there.
  - The pane is never touched for a dropped notice.
  - Moving the record into one helper keeps `notify_listener.py` within the
    review size (1250 → 1248).

- **The last read and the send are one step** (Audit's kick-back of 63e8630).
  Rechecking just before the send still left a window. The background-work
  check, the session clear, rendering the message and the send itself all came
  after the read, and a reassignment committed in that window still reached the
  retired role. Audit reproduced it with PGU-680.

  The listener now holds the notice's ticket row, `FOR SHARE NOWAIT`, through
  `ticket_board.hold_for_delivery` (migration pgu1005, SECURITY DEFINER,
  executable only by the listener), from its last read through the send:
  - **On a connection of its own.** The hold opens its own short transaction;
    the listener's main connection stays in autocommit. Steps on the send path
    catch a failed statement and go on (a context continuation does), and they
    could not do that inside a transaction.
  - **After every step of the listener's own that locks the ticket.** The order
    is: the recheck, the background-work check and the session clear; then the
    hold; then a second recheck under it (phase `held_recheck`), the yield check
    and the send. The session clear's context-continuation decision locks the
    ticket `FOR UPDATE`, and taking the hold before it made the listener wait on
    itself. Nothing the send calls locks the row. While the hold is open, the
    main connection has `lock_timeout` 10s, so any future self-conflict errors
    instead of stopping delivery.
  - **Every retiring write locks that row first.** A reassignment, a terminal
    move or a blocker (`set_blockers` locks the row `FOR UPDATE` and touches it)
    locks it before changing anything, and a role's serial slot moves only when
    its holder's own row changes. So a write arriving during the hold waits
    until the send is over.
  - **A write already holding the row wins.** `NOWAIT` makes the hold fail at
    once, and the notice is requeued ("board write in flight") for the next
    pass. The listener never waits on the row.
  - **A write made after the read is let in first.** Just before the send, the
    listener asks whether a write is waiting on the hold's transaction
    (`pg_locks`). If one is, it releases the hold and requeues; the write
    commits, and the next pass drops the now-stale notice. A write that begins
    waiting after that last look lands after a send that was true when made.

  Dead ends:
  - **Advisory keys taken in a trigger** deadlocked: the writer holds the
    ticket row before its trigger runs, and the listener's own trace and queue
    writes need a key-share lock on that row.
  - **One transaction on the listener's own connection** broke every step that
    recovers from a failed statement.
  - **Locking all of the role's open tickets** was needless: releasing another
    ticket's hold does not move the serial slot (`serial_focus` is sticky).

## Tests

The window, as Audit reproduced it, is in the same suite. A reassignment, a
cancellation or a blocker is started right after the recheck under the hold
finds the hand-over current, and before the sender. A reassignment that commits
between the first recheck and the hold is caught by the recheck under it. Each write waits,
and App is not handed the ticket; the write then lands, and the hand-over is
dropped as stale and recorded. On 63e8630 this fails with Audit's result,
"PGU-680 -- PGU-680 entered Implementation", sent to App.

Two more cases cover the hold itself. A write that starts during the send waits
for it and lands after. A write already holding the row makes the hand-over
wait one pass, without blocking the listener, and it then goes once.

`tests/held_notice_revalidation_test.py` uses a production-built board, the real
server and listener, a pane gate that can be busy, and one that runs an action
the moment a pane is probed:

- **The incident.** B is queued behind Ops's active A and blocked while Ops's
  pane is busy. When the pane frees, Ops is told nothing about B, the notice is
  gone from the queue, and the drop is recorded. Serial focus stays on A, and
  the next notice about A still reaches Ops.
- **Already working on it.** Main is handed D. When D is blocked, Main still
  hears it.
- **The literal sequence.** A hand-over held behind a busy pane, then blocked,
  then released, is dropped and recorded.
- **Races at the final submission boundary.** The hand-over is claimed as
  current, then a reassignment, a cancellation or a new blocker lands while the
  pane is probed. Each is dropped at `pre_send_recheck`, recorded, and nothing
  more about the ticket waits.
- **Blocker resolution.** A ticket blocked before it was routed is not handed
  over; once its blocker is resolved, it is handed over exactly once, as the
  active work.
- **What counts as handed over.** A ticket handed over in an earlier visit and
  queued again, then blocked, is not announced. Nor is one whose hand-over was
  dead-lettered and which only ever received a comment, once another ticket
  becomes active.
- **Nothing else in hand.** An owner whose only work is a blocked ticket still
  hears about it.

`tests/blocked_pane_alert_test.py` (SYRD-633) had a "live shape" that was this
incident: App's "blockers" update about PGU-624, queued behind PGU-625 and then
blocked, was sent, and the pane alert named PGU-625's notice, which waited
behind the pane that send had made busy. That update is now dropped, and the
check says so. So PGU-625's repair prompt goes out before the permission prompt
blocks the pane. The alert is still told once and delivered: it is said on
PGU-624, the blocked ticket App owns, with nothing waiting.

The test fails on main with exactly the incident's message: "PGU-614 … was
changed by director: blockers -- re-read it before continuing."
