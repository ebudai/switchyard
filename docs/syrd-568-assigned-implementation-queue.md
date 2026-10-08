# SYRD-568: assigned Implementation queues, one active ticket per implementer

## What was wrong

On a Director-dispatched declared workflow, the board kept each implementer's
serial slot by refusing to let a second ticket into their implementation stage:

- **The update trigger diverted.** `enforce_declared_ticket_update` sent any
  route, or same-stage reassignment, to a busy implementer into the holding
  destination (`analysis/director` on syrd). `enforce_ticket_workflow_insert`
  did the same for a new ticket.
- **Overrides diverted too.** `force_move`/`override_move` and `director_edit`
  sent such a ticket to `backlog`, through a check written for the legacy stage
  name `in_progress`.
- **Blocked work could not be placed.** It could not be routed to its
  implementer at all ("unresolved blocker prevents forward promotion").
- **Nothing activated.** When the active ticket finished, the board only told
  the Director to route the next ticket again.

On 2026-10-07 the Otto report cohort (SYRD-548/557/558/560/561/563..566) could
not stay in Implementation behind SYRD-550/556/559, so the Director parked it in
manually controlled Analysis to keep its reminders quiet.
`tests/assigned_implementation_queue_test.py` measures each of these on main
(cc4bfbf).

## The model now

An implementer may have any number of tickets assigned in its implementation
stage, and the ordinary route puts them there. **One** of them holds the slot.

**Which ticket holds it.** `ticket_current_reserved_ticket(role)`, the one
function every reader already asks, answers. Only its declared branch changed:

- A ticket with an unresolved blocker holds nothing.
- Work past implementation that holds its author comes first, as it always
  did. That means review, under the default policy, or lifecycle after a
  reopen. Under a lifecycle reopen this keeps the implementer off other work,
  which is the point of that policy.
- Among implementation work, the ticket already active keeps the slot while it
  still qualifies. It is recorded in the new table `ticket_board.serial_focus`,
  which has one row per implementer, so two tickets can never both be active.
  So work routed later, or unblocked later, never takes the slot from work
  under way. Blocking the active ticket hands the slot on. Clearing that
  blocker does not take it back.
- Otherwise the lowest ticket number holds it. That is the queue's stated
  order. To order work otherwise, use `blocked_by`; `parent_id` orders nothing.

**Recording the slot.** `settle_serial_focus(role)` records the slot after
every change that can move it, from AFTER triggers:

- on `tickets` (stage, owner, manual control, parking), named to run after
  every other trigger, so it sees the move's final state;
- on `ticket_blockers`;
- on `workflow_configuration`, so a workflow declared or changed over existing
  tickets records the current holders, with no notice.

When nothing can hold a slot, the record stays. A ticket blocked and then
unblocked with nothing in between is the same work.

**Who announces what.** Each handoff is sent once, by exactly one writer:

- The ticket whose own row moved is told by whatever moved it, as before: a
  route's transition notice, a reassignment's, an unblock's, a released
  blocker's. `settle_serial_focus` cannot see that notice, because it is queued
  after the trigger runs, so it stays out of the way.
- A ticket activated as a side effect is handed over by `settle_serial_focus`,
  in the same transaction, once: "… entered Implementation: it was waiting in
  your queue and is now your active ticket". The row it writes is the record,
  so a listener restart, or a second call, sends nothing more.
  - Example: the active ticket finished, or was blocked, and the next one in
    line takes the slot.
- **Unblocking.** `notify_unblocked_dependents` still announces an unblocked
  ticket that is now active. It does not announce one that is still waiting,
  or one this transaction already handed over. Otherwise unblocking would wake
  every queued ticket.
- **The first record** for an implementer only writes down what already holds:
  nothing changed hands.

**Waiting tickets.** `ticket_serial_waiting(ticket)` is assigned implementation
work that does not hold the slot, whether queued or blocked. Such a ticket:

- gets no transition or reassignment notice;
- gets no reminder. `ticket_serial_focus_reservation_is_current`, which every
  nudge generator and `ticket_turn_is_resolved` already ask, answers true for
  it;
- is not highlighted;
- is not announced to a new session by the pane hook;
- cannot be submitted into review or through any transition carrying a commit.
  Putting it down, routing it back or cancelling it still work.

Manual control is the Director's own hold, not a queue: a manually controlled
ticket holds nothing, and is handed over at once, as on main.

**Blocked work.** It may be placed in its implementer's queue, because routing
into an implementation stage holds and promotes nothing. Submitting it is
still refused.

**The listener's admission** (`finish_current_stage_blocker`) uses the slot in
an implementation stage instead of arrival order. Arrival order held an active
ticket's notice behind an earlier-routed blocked one.

## Pull boards keep their ready queue

A board with a pull policy (SYRD-539) keeps exactly what it had. A route,
reassignment or insert at a busy implementer is still diverted into its ready
stage, pinned to that implementer. Blocked work still cannot be routed into
implementation. Its ready queue is the queue there, and "claiming must not
create duplicate active reservations" holds because a claim refuses a role that
holds a slot. Legacy boards (no declared workflow) are unchanged too.

## Where the order is visible

- **SQL:** `serial_queue()` lists, per implementer, the active ticket, then the
  tickets that can start (lowest number first), then the blocked ones with
  what they wait on.
- **`GET /api/reservations`:** gains `queues`.
- **Ticket JSON:** gains `serial_queue`: `implementer`, `position`, `active`,
  `waiting_on` and `active_ticket`.
- **Ticket view:** reads "Queue: #3 in app's queue, behind SYRD-601, blocked by
  SYRD-602".
- **`ticket-board-read ticket`:** prints the same line.

## Existing queue records

The migration moves no ticket.

- **Existing markers stay.** A ticket the old code diverted keeps its
  `queued_for_assignee`/`queued_behind_ticket` markers. Its reminders stay quiet
  while that reservation holds, the existing wakeup still tells the Director
  when it ends, and the Director's ordinary route then queues it, with no
  override.
- **Readers stay compatible.** The fields are unchanged and every new field is
  optional. The board, the hook and the CLI read a board without
  `serial_queue()` exactly as before.
- **Today's slots are recorded without notices.** The migration records each
  implementer's current slot with notifications off.

## Cost

The waiting check asks for the slot once per Implementation ticket, and the
board snapshot reads the whole queue. The first version re-read the workflow
document for every ticket on the board. `serial_queue()` and the reservation
lookup now read it once.

Measured on a production-built board with 600 tickets, 100 of them in
Implementation (main can only hold 2 there):

| Measurement | main | before the rewrite | now |
|---|---|---|---|
| Board snapshot (`/api/board`, which the pane hook reads with a 2 s timeout) | 0.035 s | 3.4 s | 0.04 s |
| `ticket_turn_is_resolved` over every ticket | 0.024 s | 1.26 s | 0.06 s |
| `notify_due_nudges` | 0.04 s | — | 0.09 s |
| One reservation lookup | 13 ms | — | 6 ms |

## Tests

**`tests/assigned_implementation_queue_test.py`** runs on a production-built
board: schema.sql, the real ticket-board-migrate and rbac.sql, the example
workflow with syrd's holding destination, the real HTTP server, notify
listener, nudge generators and pane hook. It covers the acceptance list:

- **Routing:** three ordinary routes, A→B→C, placed before A is routed.
- **Only A is active:** only A is reported, handed over, highlighted and
  reminded.
- **B and C are handed over once each:** B when A completes (through Audit and
  Director review), then C, across a listener restart.
- **Neither blocked nor waiting work can be submitted.**
- **A parent and child are ordered by their numbers alone.**

It also covers:

- **The queue is visible everywhere:** in SQL, the status endpoint, the CLI and
  the ticket view (through node).
- **The slot moves forward only:** blocking the active ticket hands the slot on
  once, and clearing that blocker does not take it back.
- **The highlight follows the slot,** not arrival order.
- **Overrides and edits:** `force_move`, `override_move` and `director_edit`
  land in place.
- **Manual handoff unchanged:** a manually controlled ticket is handed over as
  on main.
- **Upgrade from main's board:** nothing is moved or sent, and an old diversion
  stays quiet and routes into the queue.
- **Grants and copy parity:** these run last, so parity never stands in for a
  behaviour check under mutation.

**Existing suites adapted.** These encoded the diversion, or placement, that
this ticket removes. Each now asserts the new behaviour in the same place:

- `director_withdrawal`, `serial_reservation_status`,
  `ticket_board_declarative_workflow`, `ticket_board_director_defer_backlog`,
  `ticket_board_director_reassign`, `worker_pool_rehearsal_postgres` and
  `lifecycle_reservation`: a second ticket waits in the implementer's queue.
  Under the default policy, B takes the slot when A's reopen releases it.
- `declared_review_return`: Audit's blocked forward-route case now uses the
  submission route, since placement into implementation is allowed.
- `ticket_board_park_blocked_postgres`: a blocked ticket holds no slot.
- `ticket_board_superseded_queue_notice_postgres`: it first shows the live
  sequence cannot occur at this release. It then makes its three announcements
  on a board as the previous release shipped it, upgrades that board, and runs
  every original delivery, race and control case against the upgraded board.
- `pull_claim_postgres`: its replay of pgu972 now follows pgu972 itself rather
  than the later migrations, which may redefine its functions.

The pull, reminder-suppression, context-restore, operator-wait,
external-blocker, reason-only-blocker and repeat-announcement suites pass
unchanged.

## Mutation

32 mutants, run with `tests/bounded_run.py mutate`.

**How the SQL mutants were run.** Each SQL mutant was applied to the
migration's copy, which is what a production-built board runs. The acceptance
test checks copy parity last; a mutation run sets
`SYRD568_MUTATION_SKIP_COPY_PARITY=1` so a mutant reaches the behaviour checks
and the neighbour suites, instead of stopping on a disagreement it caused.
Neighbour suites that build from `schema.sql` alone saw the same mutants
applied to `schema.sql`'s copy.

**28 are killed by an assertion about behaviour.** Each kill's assertion line
was read. The killing suites were this ticket's test, plus:

- lifecycle_reservation: work in review comes before implementation work;
- pull_claim_postgres: a pull board stops diverting;
- external_blocker: settling announces the moved ticket;
- ticket_board_director_reassign: a dispatch board diverts a reassignment, a
  reassignment notice goes to waiting work, and a workflow write records
  nothing.

**4 survive, all equivalent.**

- **Settling announces a first record.** The migration, and every workflow
  write, record each implementer that holds a slot. After that, a first
  record is only written when a ticket takes a free implementer by its own
  move. That ticket is `p_moved`, which settling never announces anyway.
- **Settling forgets when nothing holds.** After a forget, the next holder is
  either the moved ticket, which is not announced, or a first record, which is
  silent. A kept record only matters when its blocked ticket competes for the
  slot, and any other ticket that takes the slot first replaces the record.
- **Unblocking repeats an activation.** The unblock trigger's same-transaction
  dedupe is a safeguard. Settling never announces a ticket whose own blocker
  rows changed, because it is `p_moved` in the blocker trigger. The settle run
  for the finished blocker happens later, and by then the record has already
  moved, so no activation row ever sits beside an unblock notice.
- **The migration's initial record announces.** Every initial record is a
  first record, which is silent whatever `p_notify` says.

**One neighbour suite fixed.** The declarative workflow suite's browser step
waited with an async `wait_for_function` predicate. Playwright resolves that on
the returned Promise without awaiting it, so the wait returned 0.04 s into a
0.15 s POST and the next read raced the commit. On this branch it failed about
one run in five, and never on main in 8 runs. It now waits for the action's own
response, which is sent after the commit, and passes 10 of 10. Its two earlier
mutant "kills" were that flake.

