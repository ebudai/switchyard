# SYRD-537: a reminder snooze for named tickets, and one notice at its deadline

## The request

MEFP moves user-approved Final Sign-Off into overnight integration batches, so
interactive UAT is not held up for minutes per commit and push. Until then the
board keeps waking the Director, and each ticket's reviewer, about exactly
those tickets. The only existing ways to stop that each cost something:
- **stage `notify.kind=none`** also silences the initial handoff, and does so
  for the whole stage;
- **`manually_controlled`** bypasses workflow gates;
- **a fake blocker** is a false record.

The Director asked for a durable, attributable command to defer the OPTIONAL
reminders for named tickets until a deadline. It must leave stage, owner, gates,
the candidate, blockers, reservations, initial handoffs and other roles alone.
At the deadline it writes one queue-ready notice, created exactly once.

## What a snooze silences, and where

A snooze applies to **member tickets only**. It defers their optional
reminders:

| Kind | Producer (newest copy) | Snoozed by |
|---|---|---|
| `idle_reminder`, and the `escalation` after it | `notify_idle_turn_end_nudges` (pgu948) | the generator, and delivery |
| `nudge`, and its `escalation` | `notify_idle_stall_nudges`, `notify_due_nudges` (pgu934), both subqueries of each | the generator, and delivery |
| `unresolved_turn_repair` (the "still yours" prompt), `unresolved_turn` (grace escalation) | `notify_unresolved_turn_end`, through `ticket_turn_is_resolved` (pgu968) | the generator, and delivery |
| `awaiting_role` steps 2–4 (scheduled when the wait starts) | `enqueue_awaiting_role_handoff` | delivery |

**Never snoozed:**
- `transition` (initial and unblock handoffs);
- `triage`;
- `awaiting_role` step 1 ("New handoff.");
- `ticket_update` and publication notices;
- a permission-prompt `escalation`, which is about a pane, not a ticket, even
  when it hangs on a member ticket;
- anything about a ticket that is not a member.

**Two ends, so that no producer can bypass it:**
1. **Generation.** One predicate,
   `ticket_board.ticket_reminders_snoozed(ticket, now)`, is added as a single
   condition to the four producers' newest copies:
   - `ticket_turn_is_resolved` (pgu968);
   - `notify_idle_turn_end_nudges` (pgu948);
   - `notify_idle_stall_nudges` and `notify_due_nudges` (pgu934).

   Migration pgu970 redefines them verbatim apart from that condition.
   `schema.sql`'s last copies carry the identical text, because the migration
   is generated from them.
2. **Delivery.** At claim, and again at the pre-send recheck, the listener asks
   `reminder_snooze.snoozed_reminder_detail`. An optional row for a snoozed
   ticket is **discarded**, traced `listener_discard` with reason
   `reminder_snoozed`. It is never acked: `ack_notification` records delivery,
   so acking would bump `idle_reminder_count` and arm the next escalation
   (SYRD-32). This catches:
   - rows queued before the snooze;
   - the awaiting-role schedule;
   - any producer that misses the generation check;
   - a snooze set while a reminder is in flight.

## The data (pgu970, and `schema.sql` identical)

- **`reminder_snooze_batches`:**
  - `due_at`;
  - `reason` (non-empty);
  - `created_by`, the caller's role;
  - `state`: `snoozed`, `due_emitted` or `cleared`;
  - `closed_by`, `closed_at`, `close_reason`;
  - `notification_id` of the deadline notice.
- **`reminder_snooze_members`:**
  - `batch_id`, `ticket_id`, `position`;
  - the frozen `snapshot`;
  - `released_at`, `released_by`, `release_reason`.

  A partial unique index allows each ticket in at most one batch that has not
  released it.
- **Membership is the explicit list the Director named,** frozen. No rule adds
  tickets, so future tickets are never muted.

## "Still the ticket that was snoozed": the override policy

The snapshot records the ticket's:
- state and assignee;
- `commit_hash`;
- every sign-off and gate flag (`audit_signoff`, `needs_audit`,
  `needs_inspection`, `inspector_signoff`, `needs_user_signoff`,
  `user_signoff`, `workflow_flags`);
- `manually_controlled` and `parked`;
- its wait (`awaiting_role`, `awaiting_since_at`);
- its unresolved blockers.

The predicate holds only while the ticket still matches its snapshot, and
either:
- **the membership is open and the batch is `snoozed`.** The deadline alone
  does not end it (see the Audit correction below); or
- **the deadline's notice released the member as ready, and that notice is still
  queued and not dead-lettered.** The notice is the first optional word about
  the batch, and ordinary reminders follow it.

So a substantive change ends that member's snooze **at once**, and no writer
has to remember to. Ordinary reminders resume from the next pass, and the
change's own handoff is delivered as usual: a move, a reassignment, a new
candidate, a sign-off or gate, a hold, a new wait, a new or resolved blocker.

**Urgent handoffs are never deferred.** They are not optional kinds, and the
change that causes one ends the snooze anyway.

**Attribution.** The ticket's status reads `invalidated` with the changed
fields straight away. The membership row records the release when it is next
reconciled:
- a new snooze naming the ticket releases it with
  `invalidated: <fields> changed`;
- the deadline releases it with `due; <fields> changed`.

**Reads are pure.** A status GET, `/api/board`, or `/api/reminder-snoozes`
never writes. The suite checks that repeated reads leave the member rows
byte-identical.

## Commands, authority, display

- **Snooze:**
  `ticket-board-write snooze-reminders --ticket ID … --until <ISO 8601 with offset or Z> --reason TEXT [--apply]`
  - Without `--apply` it is a preview: the exact members with their stage and
    owner, the deadline in UTC, any memberships it would release, and every
    refusal.
  - It writes nothing.
  - With `--apply`, any refusal refuses the whole command.
- **Refusals:**
  - an unknown or terminal ticket;
  - a ticket already snoozed, or due and not yet reported;
  - no reason;
  - a deadline that is local time (no offset), in the past, or more than 7
    days away;
  - more than 100 tickets.
- **Clear:**
  `ticket-board-write clear-reminder-snooze --batch N [--ticket ID …] --reason TEXT [--apply]`
  - It previews unless `--apply`.
  - It releases the named members, or the whole batch, with
    `cleared: <reason>` attributed to the caller.
  - Ordinary reminders resume at once.
  - A batch with no open members closes as `cleared`, and writes no deadline
    notice.
- **Authority:** the hold's (`set_manually_controlled`). A snooze silences a
  subset of what holding a ticket silences, for a bounded time.
  - The database checks it in both functions through `require_actor`.
  - The board admits it through `COMPOSED_OPERATION_CAPABILITIES`, the
  `release_external_blocker` pattern. No capability is added to the control
  floor, so existing workflow documents stay valid.
  - On a legacy board only the Director may snooze.
  - App and Ops are refused at the board and by the database. Ops authority is
    not broadened.
- **Grants:**
  - the service may execute the snooze, clear and the two reads;
  - the listener may execute the predicate and the emission;
  - helpers are owner-only.

  They are in pgu970 and in `rbac.sql`. Deploys run `rbac.sql` after the
  migrations (SYRD-530), and it revokes every function grant before
  re-granting.
- **Display:**
  - the ticket JSON carries `reminder_snooze`, null unless the ticket is in an
    open batch;
  - the ticket view shows one line beside "Notice:", one of:
    - "Reminders: snoozed until … (batch N, by director: reason)";
    - "… snooze no longer applies, <fields> changed …";
    - "… snooze due at …, queue-ready notice pending …".

  It is scheduling, never delivery status, and never a card line (SYRD-266).

## The deadline: what is exactly-once, and what is not

**Creating the notice.** Each listener loop pass calls
`emit_due_reminder_snoozes(now)` after the serial-focus wake-ups.
- It selects due `snoozed` batches with `FOR UPDATE SKIP LOCKED`.
- It reconciles each open member: unchanged means ready, otherwise changed, with
  the fields that changed.
- It releases every member.
- It enqueues one `ticket_update` row with payload kind `reminder_snooze_due`,
  addressed to the role that set the batch, with dedupe key
  `reminder-snooze-due:<batch>`. The notice lists the ready tickets and the
  changed ones.
- It sets the batch to `due_emitted`.

All of this is one transaction. The row lock, the state flip and the unique
dedupe key mean:
- concurrent passes create the notice at most once (a second pass skips the
  locked row rather than waiting);
- a restarted listener finds the batch already closed;
- a pass that dies before commit creates nothing, and the next pass creates
  it.

**Which ticket it hangs on.** The queue needs a ticket, so the notice is
attached to the first open member. The eligibility check judges this payload
kind by the batch, not by that ticket's stage or owner.

**Delivering it.** This is the queue's ordinary transport, not exactly-once to
a human:
- a busy pane requeues it (`pane busy`);
- a send is acked once the recipient's own hooks witness a turn (`send`), or
  acked unconfirmed (`send_unconfirmed`) and never retyped;
- a claim that dies before sending expires after 2 minutes and is retried.

**The window this design does not close:** a listener that dies after the send
but before the ack sends again once the claim expires. That is the listener's
existing window for every kind, since there is no pre-send marker. It is stated
here, not claimed away.

**After the deadline,** the ready members stay held until the notice has left
the queue: delivered and acked, including `send_unconfirmed`, or
dead-lettered. Then ordinary reminder policy follows. Members that changed
during the snooze resumed when they changed, and cleared ones when they were
cleared.

**What this costs.** The notice is written by the listener, so a stopped
listener holds the batch past its deadline. But a stopped listener delivers
nothing anyway, and the `pg_cron` backstop nudge asks the same predicate.

**A notice that cannot be delivered** is dead-lettered by the listener's
existing failure path, for example when the role's pane is missing. That
releases its members, so the snooze never silences them for good.

## Audit correction: the notice comes first

Audit returned c22504c. At the deadline:
- `ticket_reminders_snoozed` stopped holding as soon as `due_at` passed;
- `listen_once` ran the producers before the due pass;
- older queued member rows sort ahead of the notice.

So ordinary member reminders could reach the Director before the one
batch-ready notice, which is the very repetition the feature replaces. My own
test encoded it (`past_deadline_rows`).

**Measured on c22504c,** running the corrected test's scenario against that
commit's code and SQL: twelve member reminders were delivered before the notice.
- The producers enqueued member reminders after the deadline.
- The wait's queued steps were not held.
- The ticket showed no pending state.

**Now:**
- The predicate holds until the notice has left the queue, as above.
- `listen_once` runs the due pass first, so the notice is written before any
  producer in the same pass.
- While the notice is pending, the ticket shows `status: "due"` ("snooze due
  at …, queue-ready notice pending").

**Mutation of the correction:** eight mutants, all killed, each read on its
assertion line.
- My suite kills seven:
  - H1: members resume when the notice is written;
  - H2: a dead-lettered notice still holds;
  - H3: the pending ticket state is omitted;
  - H4: the pending state is not called due;
  - Q7b: the deadline alone ends the hold;
  - Q8b and Q9b: the rewritten predicate ignores changes, or releases.
- **H5, the due pass moved back after the producers, survives my suite.** It is
  equivalent there, because the predicate already holds members whatever the
  pass order. It is killed by `idle_nudges_boundary_test`'s loop-order
  assertion, which pins the order the Director asked for.

**Why ordering holds whatever the queue order:** no optional member row can be
generated, or delivered, until the notice row is gone. That covers rows queued
before expiry, multiple members, a busy-pane retry, a listener restart,
concurrent passes, and the `pg_cron` producer.

## Defaults

With no batch, nothing changes. No workflow document changes, and every
existing project behaves as before.

## Decisions taken within scope

1. Authority reuses `set_manually_controlled`, rather than adding a capability.
2. `due_at` is capped at 7 days.
3. `triage` is an initial handoff, so it is never snoozed.
4. The deadline notice is a `ticket_update` row with its own payload kind,
   which avoids rebuilding the queue's kind CHECK constraint.
5. The notice is addressed to the role that set the batch.

## Evidence

### `tests/reminder_snooze_test.py`: 48 checks

The suite builds a production-shaped board: companion roles, `schema.sql`,
the real `ticket-board-migrate`, then `rbac.sql`, with a declared workflow.
It drives:
- every reminder producer, on a disposable clock;
- the real notify listener, including its own `listen_once` loop, with an
  injected pane, gate and turn-start witness;
- the real write CLI against the real board server, over its Unix socket.

It passes under `env -i` and in a role pane.

**Main, 792774a, run in a child from a git archive:**
- the same night reminds about every ordinary member ticket;
- the wait delivers its whole schedule;
- the reminders are acked and counted;
- the CLI has no `snooze-reminders`.

**This tree.**

*Preview and authority:*
- the preview names exactly the members, gives the deadline in UTC, and writes
  nothing;
- the snooze is refused for App and Ops at the board, and for App by the
  database itself;
- it is refused for a local-time or past deadline, and for a ticket already
  snoozed;
- the Director's snooze is attributed, and the ticket JSON says so.

*Overnight:*
- no producer enqueues an optional reminder about a member (read from the
  trace), while the same producers keep reminding about the controls;
- reminders queued before the snooze are discarded, not acked and not counted;
- the wait's handoff is delivered and its steps 2–4 are discarded;
- a permission-prompt escalation hung on a member is delivered as on main;
- status reads leave scheduling untouched.

*Unchanged:*
- the implementer, Ops and Audit handoffs during the night are identical to
  main's;
- the gates refuse the unapproved member exactly as on main.

*Changes and the deadline:*
- a kick-back or a new blocker ends that member's snooze at once, says which
  fields changed, delivers the handoff, and resumes its reminders;
- an early clear previews its scope and resumes only that ticket;
- from the deadline until the notice is delivered, no producer enqueues an
  optional reminder about a ready member. This covers producers run before the
  notice is written, while it is queued, and during a busy-pane retry, read
  from the trace;
- the wait's steps 3–4, queued before the snooze and older than the notice, are
  discarded while it is pending;
- meanwhile the ticket says its snooze is due, with the notice pending;
- in the order delivered after the deadline, the first message about any ready
  member is the batch's notice, and ordinary reminders follow it;
- a dead-lettered notice releases its members;
- two concurrent passes create the notice once, and the second does not wait
  on the lock;
- a busy pane postpones the notice, a restarted listener delivers it once (one
  `send`), and no later pass repeats it;
- a pass that rolls back creates nothing, and the listener's own loop then
  creates and delivers it;
- a notice hung on an Audit-owned ticket still reaches the Director;
- a snooze set between claim and send stops the send at the pre-send recheck.

*Schema, grants and view:*
- a fresh `schema.sql` board installs the same 16 functions and the same
  tables as the migrated board;
- each new function is executable only by its caller;
- the ticket view renders the sentence, and the card does not.

### Mutation

Mutation ran through `tests/bounded_run.py mutate`, and each kill was read on
its assertion line. There were 34 mutants:
- the SQL predicate, the snapshot, each of the five generator insertions, the
  emission (lock and state flip), the snooze and clear functions, and their
  refusals;
- the Python classification, the claim and pre-send discards, discard versus
  ack, the loop call, the due notice's eligibility exemption, the board
  policy, the read column and both older-board probes, and the ticket view
  line;
- the `rbac.sql` grant.

The first round showed four mutants dying only on the schema-parity check
(Q2, Q4, Q6, Q18) and one surviving (P13, the view line built but not
attached). Three checks were added: a generation-level check from the trace,
a re-snooze refusal, and the view attachment. After that, every mutant is
killed by a behavioural assertion.

### Focused sweep: 99 existing suites, this tree vs 792774a

The sweep covered every suite naming a touched function, module, CLI or role
table. They ran serially under `env -i`, with refusing stubs for the model CLIs
and konsole, which no suite reached.
- 86 pass on both trees, and 13 fail on both.
- The only remaining differences are the two known "applies last" lists
  (`ticket_board_park_blocked_postgres_test`,
  `ticket_board_publication_migration_recovery_test`), which now name pgu970.

### Neighbour suites adapted, each to a change this ticket makes on purpose

- **`ticket_board_read_query_boundary_test`:** the SQL byte hashes, re-measured
  for the new column, and the fake connection answers the older-board probe.
- **`idle_nudges_boundary_test`:** the recorded listen loop gains exactly the
  new pass's statement and log line, and the loop order names the pass.
- **`ticket_board_operation_role_policy_test`:** the two new Director
  operations and their composed authority.
- **`ticket_board_schema_test`** and
  **`ticket_board_review_admission_turn_attribution_test`:** parity is against
  the migration that last defined the function, now pgu970. The SYRD-513 clause
  is still required in it.
- **`ticket_board_unresolved_turn_current_round_test`:** its pre-migration
  board takes that era's `rbac.sql` (`rbac_before`, SYRD-526), not today's.

**Code tolerance, not fixture edits.** Boards built from older schemas and
read with this tree's code (eight suites) read `reminder_snooze` as absent:
the read query and the listener's predicate probe `to_regprocedure` first.

The discard helper lives in `reminder_snooze.py`, so the eligibility class
still holds exactly what SYRD-487 moved into it. A payload key was renamed from
`ready` to `queue_ready`, because `ready` matched the schema test's
removed-stage guard.

### Coordination

SYRD-538 (Main) edits the pane hook, `idle_nudges.py`, the listener's requeue
seam and `notify_unresolved_turn_end`. This ticket edits none of those, and
Main's copy keeps calling `ticket_turn_is_resolved`. The one shared file is
`notify_listener.py`, where these changes are line-local: two discard checks,
placed ahead of the stale-notice ack, and one loop call. Whichever lands second
rebases and renumbers its migration.

No live MEFP snooze, acceptance, deployment, or worker or provider action.
