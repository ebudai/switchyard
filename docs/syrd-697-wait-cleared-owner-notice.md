# SYRD-697: a cleared wait tells the ticket's owner

## What happened

On pie, release bf564449, PIE-17 sat in Implementation, owned by Ops. All times are 2026-10-28, EDT.

| time | what |
|---|---|
| 18:22:51 | Ops asks Audit for a review (`request-dependency`), and its pane goes idle. |
| 18:24:24 | Audit reviews, writes "Clearing the wait.", and clears it. |
| 18:24 to 19:2x | Nothing reaches Ops' pane. |
| 18:52:52 | The stall record's last progress is "PIE-17 awaiting_role notice delivered to director". Nothing appeared in the Director's pane. |
| about 19:20 | The Director comments. That comment reaches Ops, and Ops starts within a minute. |

The ticket also read `active_work_delivery: unconfirmed, sent_to_retired_runtime`, from a send a day earlier.

## Why

Each of these is reproduced on main by the regression.

**Clearing a wait told nobody.**
- `clear_awaiting_role` cleared the wait and touched the ticket, and did nothing else.
- `add_comment` tells the owner only when the comment is urgent or comes from the Director or the User. So Audit's review did not reach Ops either, and the Director's comment did.
- The ticket was Ops' work again, and Ops was idle at its prompt with no way of knowing.

**A dropped notice was counted as a delivery.**
- `enqueue_awaiting_role_handoff` queues a wait's notices at 0, 5 and 15 minutes to the awaited role, and at 30 minutes to the Director. 18:22:51 + 30 min = 18:52:51.
- By then the wait was gone, and the listener drops such a notice as stale. That is why nothing reached the Director's pane. `drop_if_stale` removes the dropped notice with `ack_notification`, which writes an `ack` trace.
- The board stall alert (SYRD-637) counts every `ack` of a transition, awaiting_role or publication notice as "notice delivered", which is durable progress. That restarted PIE-17's stall clock, with nothing delivered.

**`sent_to_retired_runtime` never ends.**
- The read model computes it on every read. It means the last hand-over sent for this visit to the stage reached an Ops provider whose registration has since been replaced, and no send has reached the live one.
- SYRD-589's redelivery covers only a provider that has begun no turn, and only a ticket with no wait, so nothing ever ended it.
- Meanwhile Ops had been working the ticket: its review request is a comment it wrote.

## The change

**A cleared wait tells the owner** (migration `pgu1019_syrd697_wait_cleared_owner_notice.sql`; `schema.sql` ends with the same functions).
- When `clear_awaiting_role` clears a wait and the caller is not the ticket's owner, the owner gets the in-place change notice that a blocker change gives:

  ```
  PIE-17 -- <title> (in in_progress, that you own) was changed by audit: the wait on audit was cleared -- re-read it before continuing.
  ```

- An owner clearing its own wait is told nothing, and neither is anyone when a ticket with no wait is cleared.
- A held review's completion wait is not the owner's either (SYRD-22, SYRD-180). The owner of a Director-held ticket approved it; the approval is deferred until the hold is released, and the board set the wait on whoever takes the ticket next. Clearing it tells the owner nothing. "Approved" is either of:
  - a deferred approval is pending (`pending_held_review`), which is how a declared workflow records it, whatever the stage's sign-off flag is called;
  - on a legacy board, the stage's own sign-off column (Inspection, Audit, User review).
- The notice goes through `notify_ticket_owner_in_place_change`, so its existing rules hold. In particular, a ticket queued behind its implementer's active work is not announced (SYRD-582).

**Only a notice the listener sent is a delivery.** `board_stall_delivery_progress` counts an `ack` as "notice delivered" only when the same notification has a `send` or `send_unconfirmed` trace. Three kinds of ack have no send, and none of them counts:
- a stale drop;
- an unknown target role;
- a provider quota hold.

**An owner's own write confirms its hand-over.**
- An unconfirmed send is now reported as `delivered` once the owner has commented on the ticket after it. The report keeps the send's time and adds `confirmed_by: owner_comment` and `confirmed_at`, the owner's first such comment.
- Ops' review request is such a comment: `request-dependency` writes it as Ops.
- A comment from anyone else, or an owner's comment from before the send, confirms nothing, and the ticket stays `unconfirmed` as before.
- The board card shows no hint for a delivered notice, so the stale "Sent to ops, not confirmed received" goes away.

**Unchanged:**
- comments by roles still notify only when urgent;
- the wait's own handoff schedule, and the listener's stale-drop rule;
- every other delivery state;
- SYRD-589's redelivery.

## Not covered

PIE-17's stall episode 6 recorded Ops' evidence as "no registered provider process" while its pane was running. That comes from pie's runtime registration, which this host cannot read, so it is left for a ticket with that evidence.

## Tests

`tests/wait_cleared_owner_notice_test.py` runs main first, from a git archive. It then runs this tree twice: on a fresh board, and on main's board upgraded by this tree's migrations. Each board is production-built (router_role_board_test's), with the real HTTP server and the real listener; only the panes are stood in for.

**PIE-17, replayed:**
1. Ops is handed the ticket.
2. Its provider is replaced.
3. Ops asks Audit for a review, and Audit is told.
4. Audit comments and clears the wait.
5. The wait's later notices fall due.
6. The Director comments.

On main, nothing reaches Ops after the clear. The three dropped notices are recorded as deliveries, and the ticket reads `sent_to_retired_runtime` to the end. Here:
- Ops is told once, by whom and what;
- the dropped notices are not progress, while the hand-over and the review request still are;
- the delivery reads as confirmed by Ops' own request.

**Also covered:**
- the Director clearing another role's wait tells its owner;
- an owner clearing its own wait is told nothing;
- clearing a ticket with no wait tells nobody;
- a reviewer's comment, or the owner's comment from before the hand-over, confirms nothing;
- the Director's comment reaches the owner, as before;
- a held review: approved by its owner in Audit, or in an Inspection stage whose sign-off is a declared flag of its own, clearing its wait tells the owner nothing; held but not yet approved, the owner is told;
- `schema.sql`'s last definition of each function is the migration's, byte for byte.

`tests/board_stall_user_wait_test.py` (SYRD-696) required `schema.sql` to end with pgu1018's function, which any later migration breaks. It now requires that function's last definition to be pgu1018's, as this ticket's own test does.

`tests/ticket_board_durable_awaiting_role_test.py` (SYRD-15) asserted that nothing at all was sent after the Director cleared Research's wait on the Director. That is PIE-17's case, so it now expects Research's notice "the wait on director was cleared", and still checks that none of the wait's own queued steps is delivered.

`tests/ticket_board_held_review_handoff_test.py` covers the legacy held review: on the first version of this change, the Director clearing a held Audit review's wait told Audit "re-read it before continuing".
