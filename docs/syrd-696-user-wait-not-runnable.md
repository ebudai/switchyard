# SYRD-696: a ticket waiting on the User is not runnable work

## What happened

SYRD-691 sat in Implementation, owned by Ops, waiting on the User's approval of an install packet. The board stall alert
(SYRD-637) sent the Director a digest twice on 2026-10-27:

> Board stall: runnable work has made no durable progress for 60 minutes ...

| episode | wait recorded (UTC) | clock started (UTC) | digest (UTC) |
|---|---|---|---|
| 7 | 10:03:40 (Ops' dependency request) | 14:03:56 | 15:04 |
| 8 | 15:08:39 (the progress event "SYRD-691 waits on user") | 19:08:45 | 20:08 |

Each clock started at the first listener pass after its wait turned four hours old, 16 s and 6 s after. Each digest
followed one live threshold (3600 s) later.

## Why

`board_stall_runnable` excluded a waiting ticket only while `ticket_awaiting_role_is_active`, which lapses four hours
after the wait was recorded. That window belongs to a wait on a pane role:
- its handoff tells the awaited role at 0, 5 and 15 minutes, and the Director at 30 (SYRD-148);
- the owner's idle nudges resume after it (PGU-453).

A wait on the User has no such window. It sends nothing (SYRD-549), and nothing on the board makes it due. When the window
lapsed, the ticket counted as its owner's runnable work, though the owner can do nothing until the User answers.

The per-ticket role stall (SYRD-595/675) already excludes any recorded wait, of any age. Only the board stall alert let a
User wait lapse.

## The change

`board_stall_runnable` (migration `pgu1018_syrd696_user_wait_not_runnable.sql`; `schema.sql` ends with the same
function) excludes a ticket whose wait names a role of the User's kind for as long as the wait stands:

```
not counted: SYRD-691: waiting on user, the User, until the wait is cleared
```

This is the same rule the board already applies to work in the User's own stage ("its next action is the User's"). It is
by kind, never by name: the workflow's `user` identity, or any other role declared with `kind: user`.

**Unchanged:**
- a wait on a pane role is excluded while its handoff's window is open, and counts again after it, its due point;
- every other exclusion: Draft, Triage, Backlog and terminal stages, holds, parking, blockers, snoozes, serial slots and
  review lanes, the Director's and the User's stages, and tickets with their own stall episode;
- every counted stage: Implementation, Documentation, Inspection, Audit and Publication;
- the progress record, the episode and its one digest.

**Clearing the wait.** The ticket counts again from the clearing. `board_stall_dependency_progress` already records
"dependency cleared" as progress, so the clock starts there. The wait's own "waits on user" event stays in the record, and
the ticket's hand-over time is untouched.

**How such a wait reaches a board:**
- the Director's `await-user`, in a stage the workflow's `user_waits` lists (SYRD-662);
- a wait recorded before SYRD-662, which an upgrade keeps (`user_wait_status` reports it; the board moves nothing).

SYRD-691's wait is the second kind: the live board runs d24a15cf, which predates SYRD-662.

## Tests

`tests/board_stall_user_wait_test.py` runs main first, from a git archive, then this tree. Each run uses production-built
boards with the evaluator called as the listener calls it, at simulated times (`board_stall_test`'s `Stall`).

**SYRD-691's wait, and the Director's await-user in Audit:**
- On main, both are excluded while young, count from four hours on, and half an hour later one digest names them.
- Here, neither counts at 0, 239, 241, 271 or 600 minutes, the status says why, and no episode opens.

**Cleared:**
- the ticket counts again on a clock started by the clearing;
- exactly one digest follows half an hour later;
- its hand-over time is the one the digest gives;
- the wait's progress event is kept.

**Mixed board:**
- the User's wait, a pane-role wait and plain runnable work sit together;
- the pane-role wait is excluded inside its window and counts after it;
- the one digest names the runnable work and the pane-role wait, never the User's.

**A second role of the User's kind**, under its own name, is the User all the same.

**Main's board upgraded** by this tree's migrations behaves the same.

`ticket-board-read stall` prints why the ticket is not counted, and `schema.sql` mirrors the migration byte for byte.
