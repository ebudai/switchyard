# SYRD-637: the board stall alert

The per-ticket paths each speak for themselves:
- an owner's unresolved turn is prompted, then escalated (SYRD-595);
- triage tells the Director about intake (SYRD-120);
- a pull policy reports idle capacity (SYRD-539).

None of them says "nothing that could run on this board has moved for half an
hour". This does: once per episode, to the Director alone.

## What counts as runnable now

`ticket_board.board_stall_runnable` reads only the board's own authorities. A
ticket counts when all of these hold:

1. **Its stage is admitted, active work** (`board_stall_stage_counts`). This is
   judged by the declared stage's shape, never its name. A stage does not count
   if it is:
   - terminal (Done, Cancelled);
   - a draft (Draft);
   - a parking stage (Backlog);
   - a Director hold, meaning not a review and owned by the Director alone.
     That covers Triage/Analysis and the Director's other holding stages.

   A pull policy's ready stage counts.
2. **Someone other than the Director or the User is responsible.** That is the
   assignee, when it owns the stage. In the ready stage it is the claimants,
   and only while one of them could claim the ticket (`ticket_ready_for_pull`).
3. **Nothing makes it wait:**
   - not held or parked;
   - no unresolved blocker, whether internal or external (`operator:`, `project:`);
   - no active dependency (`awaiting_role`);
   - no reminder snooze;
   - not queued behind a reservation;
   - not behind its role's current ticket (`finish_current_stage_blocker`: an
     implementer's serial slot, or a reviewer's lane).
4. **It is not in its own stall episode (SYRD-595).** That episode already has
   one recipient: the owner while being prompted, the Director once escalated.

The status lists every other admitted ticket with the reason it is not counted.

## Progress

Triggers record durable progress in `board_progress_events`, append-only, for
tickets whose stage counts before or after the change:
- a state, owner, hold or candidate change: a transition, claim, review
  decision, completion or cancellation;
- a blocker set, removed, resolved or reopened;
- a dependency change;
- a transition, dependency or publication notice delivered (acknowledged);
- a comment by the ticket's own assignee, which is the explicit checkpoint.

Nudges, reminders, alerts, activity touches and retries record nothing. Neither
does what the listener sees of a pane: a busy pane or a running background
child is evidence in the digest, never a reset.

## Episodes and the digest

The listener's pass (`board_stall.run`) reports each role as pull pickup's own
classifier sees it: no registered process, process gone, stopped on a prompt,
working, background work, or idle. It then calls `note_board_stall`, which:

- records when it first saw runnable work, so the clock starts at the later of
  that moment and the last progress;
- after the threshold, opens an episode and enqueues one Director digest in the
  same transaction, anchored on the oldest runnable ticket (dedupe key
  `board_stall:<episode>`). The digest lists each runnable ticket, its state,
  the responsible role, the role's evidence (including an open provider-capacity
  or blocked-pane episode) and how long it has waited, plus the last durable
  event;
- closes the episode when progress is recorded or nothing runnable remains. A
  later stall needs the full threshold again.

**Exactly once.** The evaluator locks its state row and the open episode, at
most one episode is open, and an open episode is never notified again. So
retries, later passes and a restarted listener add nothing.

**Delivery.** The digest is current only while its episode is open, and only
for the Director (`board_stall_notice_is_current`). Implementers are never
notified by this.

**The threshold** is one bounded policy seam, the listener's
`TICKET_BOARD_BOARD_STALL_SECONDS`: 30 minutes by default, clamped to
60–86400 seconds, and recorded on each pass so the status shows the threshold
in force.

## Reading it

`GET /api/board-stall` and `ticket-board-read stall [--json]` show
`board_stall_status`:
- the last evaluation and its runnable set, with evidence;
- every excluded ticket and why;
- when runnable work was first seen, when the clock started and when it would
  fire;
- the last progress event;
- the open and recent episodes.

## Evidence

`tests/board_stall_test.py` runs on production-built boards (schema.sql,
rbac.sql, the real migrations, the real HTTP server and listener), with explicit
`p_now`. It covers:
- the whole episode, and every exclusion;
- every counted stage (Implementation, Documentation with a Librarian,
  Inspection, Audit, Publication) and a review lane;
- a pull queue;
- stopped, gone, blocked and busy roles as evidence;
- that heartbeats and nudges are not progress, and that there is no storm;
- the listener's own pass, both read surfaces, and main's board upgraded by
  pgu1006.
