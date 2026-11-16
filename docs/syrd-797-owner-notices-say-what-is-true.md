# SYRD-797: an owner's change notice says something true when it is read

## What happened

Filed by sapi. While ops worked SAPI-69 (2026-11-15), it was repeatedly woken by Director notices whose event was
not in the ticket when it read it. That cost false wakeups and duplicate dependency comments, and it was a risk: an
agent could take "the wait was cleared" for an approval. The board's records and SAPI-69's comments show three
mechanisms.
1. **Clear, then comment.** The Director clears ops's wait and then comments its decision: two writes, seconds
   apart. The clear woke ops at once, and ops read the ticket before the comment existed (17:21:26 against 17:21:27,
   17:26:42 against 17:26:45, and others).
2. **A wake about a wait that stood again.** A clear's wake was queued while ops was busy running a step, and reached
   it after ops had asked again (`request_dependency`). It said a wait was cleared that stood again (17:29:06,
   "awaiting_role: director").
3. **Crossed comments.** The Director's approval (18:06:16) and ops' own comment (18:06:18) crossed. ops, seeing its
   own comment newest, read the "new comment" wake as nothing new.

No notice was sent before its write was committed: each one is enqueued in the transaction of its change.

## What changed

Migration `pgu1045`, with `schema.sql` carrying the same functions, and the listener.
- **The clear waits a moment.** `clear_awaiting_role` holds its owner's wake for 30 seconds. One wake is kept per
  ticket and role (SYRD-159), so the decision's comment, when it follows, replaces it and goes at once. With no
  comment, the clear is told after the moment.
- **No stale "wait cleared".** The listener treats such a notice as current only while no wait stands on the ticket.
  A clear empties the wait, so a wait standing at delivery was raised since; the notice is dropped
  (`stale_notification`).
- **The comment is named.** A comment's notice now says "new comment at <the time the ticket shows for that
  comment>", so its reader finds it even when it is not the newest.

## Tests

`tests/owner_notice_truth_test.py`, on a production-built board with the real server and listener (SYRD-772's rig):
- **Clear, then comment:** the clear alone is held, due in about 30 s. The comment's wake goes at once, once, naming
  the comment's time exactly as the ticket shows it.
- **A bare clear:** not told at once, and told after the moment.
- **Clear, then ask again before delivery:** the "wait cleared" notice is dropped, not delivered.
- **Crossed comments:** with main's own comment newest, the wake still names the Director's approval.
- **A comment while the wait stands** (SAPI-69's real approval flow: the Director comments without clearing): its
  wake reaches main, with the wait still on the Director. Only a "wait cleared" notice is dropped.

Four mutants, all killed: the comment left unnamed, the clear not held, a "wait cleared" notice delivered while the
wait stands, and every change notice dropped while a wait stands.
