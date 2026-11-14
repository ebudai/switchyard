# SYRD-772: a blocked ticket holds no implementer's slot, in any stage

## What happened
On 2026-11-14, SYRD-768 (Main's) entered DAT, the Director's acceptance stage. It is a review stage, and under the
default `review` reservation policy a review stage holds its author's serial slot. The Director then recorded
`operator:eric` on it while waiting for a deploy for UAT.

SYRD-769 and SYRD-770 sat in Main's queue, assigned and in Implementation, and Main was told nothing until SYRD-768 moved
on to User Review. App's slot sat the same way behind SYRD-741.

SYRD-568 (and SYRD-726 after it) said a ticket with an unresolved blocker holds nothing.
`ticket_current_reserved_ticket` applied that only to implementation-stage tickets.

## The rule now (pgu1042)
**A ticket with an unresolved blocker holds nothing, in every stage that would hold its implementer.** That covers
implementation, review (Inspection, Audit, the Director's acceptance, a Documentation stage), and a lifecycle board's
unfinished system stages. Any kind of blocker counts: another ticket, another board's ticket (`project:PREFIX-N`), or a
person (`operator:name`).

This is SYRD-568's rule applied wherever a ticket can hold, not a new special case for external blockers. The ticket is
waiting on someone other than its implementer, so the implementer is free meanwhile.

**Past implementation, the release lasts until rework.** This uses SYRD-582's pattern for a ticket with the User. A
ticket past implementation that is blocked is recorded in `ticket_board.blocked_slot_releases`:
- **When:** when the blocker is recorded, or when the ticket moves past implementation while blocked.
- **While recorded:** it holds nothing in the stages after it, blocked or not. So once it is unblocked it never takes
  the slot back from the work handed over meanwhile.
- **When it ends:** re-entering implementation removes the record. The ticket then queues behind the active ticket,
  under the usual rules.
- **Unaffected:** work past implementation that was never blocked holds exactly as before, ahead of implementation work.

`ticket_current_reserved_ticket` gains one condition: a non-implementation ticket with a release record holds nothing.
The order of the candidates is unchanged:
- work past implementation first;
- then the active ticket;
- then the lowest ticket number.

Implementation-stage blockers work as SYRD-568 made them.

Otherwise nothing changes: User review still releases (SYRD-582), and a pull board keeps its own release point
(SYRD-539).

## The hand-over
Releasing the slot is the hand-over `settle_serial_focus` already makes. The `ticket_blockers` trigger settles the slot
after the release is recorded, so the next ticket becomes active, and its owner is sent one notice: "it was waiting in
your queue and is now your active ticket", deduplicated as before.

The notices dropped as stale while the ticket was queued do not matter. The hand-over is its own notice, raised by the
release, whichever event releases it: a blocker, User Review, Done, or a reassignment.

## The upgrade
pgu1042 does two things on an existing board:
- records every ticket already blocked past implementation;
- settles every implementer's slot once, with announcements.

On a board stuck the way syrd was, the queued ticket becomes active and its owner is told once. Releasing the old
ticket's blocker afterwards does not take the slot back.

## Tests
`tests/blocked_ticket_slot_release_test.py` runs on a production-built board, with the shipped workflow, the real server
and the real notify listener. Its cases:
- **An `operator:` blocker on a DAT ticket:** the next ticket is handed over once. The blocker's release takes nothing
  back. Sent back, the ticket queues behind the active one, and once active and resubmitted it holds its author in
  Audit again.
- **Other blockers:** a ticket blocker on an Audit ticket, and a `project:` blocker on a DAT ticket.
- **A ticket moved on while blocked:** it is blocked in implementation and moved past it by the Director.
- **A lifecycle board:** a system-stage ticket that is blocked.
- **main's code (the before-run):** it leaves the queued ticket unannounced.
- **The upgrade:** a stuck board is upgraded.
- **Parity:** the functions, triggers and grants match between `schema.sql` and the migrations.
