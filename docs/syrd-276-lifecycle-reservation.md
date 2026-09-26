# SYRD-276: keeping an implementer reserved for a ticket's whole life

## The request

MEFP requires one ticket per bug, with `needs_user_signoff`. The originating
implementer must stay reserved through Audit, DAT, User UAT (assigned to the
User) and the Director's close, so that no kickback can leave one worker with
two active tickets. MEFP's report quoted two `schema.sql` lines that release
the reservation in `user_review` with the User. It did not verify the live
functions, because the owner account has no database login.

## What MEFP actually runs (measured, read-only for MEFP)

These boards were built the way provisioning builds one: the companion roles,
`schema.sql`, the real `ticket-board-migrate`, `rbac.sql`. They used **MEFP's
live workflow document** (revision 5, fetched by `GET /api/workflow`), with
only the pane targets renamed and the dropped `inspection` stage declared as
removed.

- **The quoted lines are not what a declared board runs.**
  - `ticket_reserved_implementer` (schema.sql:903) is used only by
    `release_implementer_and_activate_next`, which returns at once on a
    declared board.
  - schema.sql:935 is an early copy of `ticket_current_reserved_ticket` that
    a later copy supersedes; production runs pgu921's copy.
  - On a declared board that one function decides everything: an
    implementation stage holds by its assignee, and **every review-kind
    stage** holds by `last_implementer_assignee`.
- **So MEFP already retains A through User UAT.** Through audit, dat and
  user_review/user, Ops stays reserved by A, and routing B to Ops queues it
  in backlog behind A. The shipped canonical document behaves the same way.
- **The real gap is a kickback out of review.** MEFP's only User kickback is
  `user_reopen`, from user_review to analysis with a `reopen`. analysis is a
  `system` stage, so the reservation is **released**. The serial-focus wakeup
  then tells the Director "B can be routed to ops now: A no longer holds
  their serial focus". When A comes back, Ops has two active tickets, which
  is exactly the case MEFP wants to prevent. Audit's kickback is a `return`
  to in_progress, which keeps A with Ops.
- **For the Director:** SYRD's own board is declared (revision 30) and also
  holds the reservation through user_review. The "release while User UAT
  waits" behaviour exists only in the legacy (undeclared) branch, which no
  declared board runs. SYRD is not changed here.

## The change: a per-tenant `reservation` policy

A declared document may say `"reservation": "lifecycle"`.

- **Absent, or `"review"`:** today's behaviour, exactly. Every existing
  tenant is unchanged, and no stored document is rewritten, because the
  validator leaves an absent key absent.
- **`"lifecycle"`:** a ticket keeps holding its implementer
  (`last_implementer_assignee`) in every **non-terminal `system` stage** too,
  chiefly analysis after a reopen. That lasts until the ticket is **done or
  cancelled**, is **parked** (a defer), or is under manual control.
  - Draft-kind stages never hold.
  - Parking is decided by the `parked` flag, not by stage kind, because a
    parking stage is defined by its shape and a tenant may declare one
    `system`.

**Reservation is computed, not stored.** Switching the policy applies at
once to tickets already in flight, with no data migration. For example, a
ticket already in analysis after a reopen is reserved again the moment the
document opts in.

**What changes:**
- `ticket_current_reserved_ticket` is the one function everything asks: the
  workflow executor's serial-focus redirect, route queueing and the wakeups.
- The policy value is validated by `workflow_config.validate` and by
  `validate_declared_workflow` ("invalid reservation policy").
- The migration is `pgu964_syrd276_lifecycle_reservation.sql`.

**What doesn't:** gates, sign-offs, transitions and manual control. The
policy only decides what counts as holding a slot.

**A schema.sql trap, for the record.** `schema.sql` carries an early copy of
`ticket_current_reserved_ticket` (line ~914), created before
`declared_workflow()` exists. A SQL-language body is validated when it is
created, so that copy cannot mention the later function. It is left as it
was, and the running copy is the later one. SYRD-275's parity guard confirms
that the fresh and provisioned boards run the same function.

## For MEFP

Once this is deployed, the MEFP Director adds `"reservation": "lifecycle"`
to MEFP's workflow document and applies it through the normal workflow
update.
- **Effect:** a worker's bug holds them until it is done, cancelled or
  parked, and a second bug routed to them waits in backlog behind it.
- **Tickets in flight:** they pick the policy up at once.
- **Kickbacks:** after a User reopen, the Director routes the bug back to the
  same worker; an Audit kickback does so by itself.

## Evidence

- **`tests/lifecycle_reservation_test.py`: 32 checks.** Every board is built
  the production way. The workflow is the shipped document plus MEFP's
  `user_reopen` row.
  - **Before** (the tree before this change, via `git archive`): A holds Ops
    through User UAT, and B queues behind it. Then the User's reopen releases
    Ops, and the Director is told B can be routed to Ops.
  - **Default policy:**
    - the reopen still releases, as before;
    - the stored document is not rewritten;
    - switching the live board to `lifecycle` while A sits in analysis
      reserves Ops for A at once.
  - **Lifecycle policy:**
    - A holds Ops through UAT, and B queues;
    - after the reopen A still holds, nobody is told B can go, and B stays
      queued behind A;
    - the Director routes A back to Ops, and an Audit kickback returns it to
      Ops, still holding;
    - A still holds in the Director's final review;
    - only `mark_done` frees Ops, and only then is the Director told B can
      go;
    - parking frees the worker, including from a `system`-kind parking
      stage.
  - **Validation:** the API validator and the database both refuse an
    unknown policy, and both accept `lifecycle`.
- **`control_override_reaches_boards_test`** (the SYRD-275 parity guard):
  22 checks. A schema.sql-only board and a provisioned board run the same new
  function.
- **Mutation: 7/7 killed.** Covered:
  - in the reservation function: the policy ignored; lifecycle for every
    tenant; terminal stages still holding; parked tickets still holding;
  - validation: the database or the API accepting any policy;
  - the migration shipping no reservation function.

  "Parked still holds" survived until the system-kind parking case was added.
- **Sweep.** 233 suites touching reservation, serial focus, queueing, the
  workflow validator, blockers, the listener, the runner, the schema, RBAC,
  sign-offs or the skills were run serially under `env -i`, against
  `origin/main` (`65dbe04`). Pass/fail is identical for all 233. The suites
  red on both trees end on the same line, apart from temp paths and the
  migration-order checks already red on main.
