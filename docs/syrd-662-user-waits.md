# SYRD-662: a wait on the User is the Director's decision, where the workflow lets one stand

## What happened

SYRD-222 was in Implementation, owned by Ops. Ops judged, correctly, that only
the User could perform its live Zorin acceptance. It ran
`request-dependency --role user`, and the board accepted it and moved nothing:

- the ticket stayed in `in_progress`, owned by `ops`;
- `awaiting_role` became `user`;
- Ops' serial slot stayed taken;
- the ticket never reached User Review, where the User looks.

The User found the stall by hand.

## Why

SYRD-549 let any role holding `await_role` wait on the declared User. That was
for a designer in Draft waiting on the User's answers. `set_awaiting_role`
checked who may wait and where a wait may stand, but not that the User is
reached through User Review. So a wait on the User from Implementation said
"the User is next" while the work had finished no stage and passed no gate.

Measured on main fac6b09 (`tests/user_wait_gate_test.py`, run from a git
archive):

- Ops' `request-dependency --role user` and `await-role --role user` are
  accepted, from Implementation.
- So is the database function called alone, and a direct UPDATE by the
  database's owner.
- Audit may say an unaudited ticket waits on the User.
- A workflow with no User Review stage records a wait on the User all the same.
- The Director's `force_move` and `director_edit` put a ticket in User Review
  without its Audit sign-off: a ticket still in Audit, and a returned ticket
  whose sign-off was cleared. They also skip a Librarian's documentation
  sign-off and the Publisher's publication.

## The rule

### Nobody's dependency names the User

`set_awaiting_role` refuses the User for every caller, the Director included,
before anything is written. That covers `request-dependency`, `await-role` and
the function itself. `request-dependency` writes its comment first, in the
same transaction, so the refusal rolls the comment back too. The refusal says
what to do instead:

> a dependency cannot name the User: SYRD-222 is in Implementation, and work
> does not wait on the User from there. Finish Implementation through its
> declared transition, with the User's UAT steps on the ticket: it reaches the
> User in User review once every gate it owes has passed. Work only a person
> outside the board can do is the Director's operator:<name> blocker

On a workflow with no User Review stage, it says so instead. To the Director,
in a stage that admits a wait on the User, it names `await-user`. A legacy
board, with no declared workflow, never admitted the User, and still refuses
it as before ("invalid awaiting_role: user").

### The one wait on the User: `await-user`

`ticket-board-write await-user <id> --reason "..." --request "<what the User is
asked to do or decide>"` calls `ticket_board.await_user`. It is admitted only
when all of these hold:

- **The caller is the control role.** It is found by the capabilities that
  define it, as SYRD-78 finds it. The API's admission asks for those
  capabilities too. A board UI confirmation acts as the User, so it is refused
  too.
- **The ticket's stage is listed in the workflow's optional `user_waits`.**
  No stage is listed unless a document says so. `workflow_config` refuses a
  document that lists:
  - the Implementation stage, which is finished through its transitions;
  - a User Review stage, where the User already is;
  - a terminal or unowned stage, or one not declared;
  - a stage twice, or any stage when the document has no active User.

  `workflow_user_wait_stage` ignores such an entry anyway, if a stored
  document had one.
- **The reason and the request are not blank.**
- **The ticket has no unresolved blocker** (SYRD-148).

The reason and the request become one comment, and the wait is recorded in the
same transaction. Like any wait on the User, it sends nothing (SYRD-549). The
request is kept in `ticket_user_wait_requests` for the wait it was made with.

### Every writer

A trigger on `ticket_notification_state` refuses any write that newly names
the User, unless `await_user` is recording that wait in this transaction. That
covers:

- a direct UPDATE by the database's owner, as a migration or a hand edit would
  run it;
- any helper written later.

A wait already standing, which a write leaves unchanged, is left alone.
Ordinary activity on the ticket therefore still works.

The mark `await_user` leaves is a row in `ticket_user_wait_requests`, stamped
with its transaction id. No role has any privilege on that table, so the board
service cannot forge the mark. A session setting would not do: the service
could set one.

A held review's hand-off never names the User. User Review declares
`notify: none`, so the hand-off goes to the Director (SYRD-22).

### Into User Review, by any path

A trigger on `tickets` refuses a state change into a User Review stage while
the ticket lacks any sign-off it owes. Owed are the sign-offs of the reviews on
the way from Implementation to User Review:

- **On the way** means work can reach the stage from Implementation without
  passing User Review, and can go from it into User Review. Each step is an
  approval, a move or a gate skip.
- **The way may pass any stage that holds live work.** That includes review
  stages and system stages, such as a Director's hold before User Review or
  the Publisher's stages.
- **The way never passes a parking stage**, the shape deferral parks work in:
  no owner, tells nobody. The board's own test, `declared_parking_stage`,
  decides this.
- **The walk back stops at Implementation.**

So these owe nothing:
- a review reached only through parking;
- a review before Implementation;
- a review that sends work back into Implementation;
- a review only after User Review, such as one the User's feedback goes
  through.

A stage's sign-off is owed when its gate is on, or when it has no gate. Where
the Publisher is declared, its stage on the way is owed as a published attempt
for the commit the ticket carries.

Audit found the first version wrong in two ways, on b23aa7e:
- it walked back through review stages only, so a Director's system hold
  before User Review hid Audit's sign-off;
- it owed every review that leads into User Review, including one that only
  follows User Review.

This holds for every path:
- the declared transitions;
- `force_move`;
- `director_edit`.

A sign-off follows its commit: a return clears it, and a submission with
another commit clears it (SYRD-271). So Audit's sign-off is for this commit. A
ticket with `needs_audit=false` skips Audit and owes no Audit sign-off.

The UAT-steps guard (SYRD-629) is unchanged and still runs after this one.

### The prompt

The unresolved-turn choices no longer offer "`request-dependency` ... or on
the User". They offer `await-user` only to an owner it would admit: the
Director, in a listed stage.

## What the User and the Director see

The ticket's JSON carries `user_wait`, which is null unless the ticket waits on
a role of kind `user`. It holds:

- the role and since when;
- the Director's request, its reason, and who made it;
- `valid`;
- for an invalid wait, the problem and the repair.

The read client prints it, and the board card shows it in any stage:

- a valid request reads **Waiting on the User**: "The Director asks: ...";
- an invalid wait reads **Invalid wait on the User**, with its problem and
  repair.

## Waits already standing

The upgrade moves and clears nothing. A wait on the User is not valid in these
cases:

- **It has no `await-user` record**: "it was recorded without the Director's
  await-user, the only way work waits on the User". Every such wait recorded
  before this upgrade is one.
- **Its stage no longer admits one**: "<stage> does not let the Director wait
  on the User".

An invalid wait puts the ticket in the Director's queue
(`ticket-board-read director`). The migration also sends the Director one
notice per invalid wait (payload kind `invalid_user_wait`), naming the problem
and the repair.

The repair is ordinary:

- the Director runs `clear-awaiting-role <id>`, which the Director could
  always do; or
- the owner finishes the stage through its declared transition and every gate
  after it. A stage change clears the wait as it always has.

The live syrd board had no standing wait when this was written. SYRD-222 is
in User Review.

## Changed suites

Three earlier suites recorded waits on the User. Each is changed only where it
names the User:

- **`declared_wait_admission_test` (SYRD-549).** The designer's wait on the
  User in Draft, and the database's, are now asserted refused, with nothing
  recorded. The clearing and stale-escalation cases wait on pane roles
  instead: a wait on App, and the designer's wait on the Director. The
  designer's prompt no longer says "or on the User".
- **`recorded_dependency_unresolved_turn_test` (SYRD-619).** Its waits now
  name pane roles. A recorded wait answers the prompt whoever it names, which
  is what that suite tests. SYRD-609's own case, a polkit dialog only the User
  could answer, is now operator work: an `operator:` blocker.
- **`operator_person_wait_test` (SYRD-451).** Its "a recorded wait lapses, a
  blocker does not" case waits on Main.

`ticket_board_operation_role_policy_test` pins the operation table. It now
counts `await_user`.

`codex_capacity_retry_test` compares the turn-end prompt with an older
release's. It now drops "or on the User" from the older text before comparing.

## Tests

`tests/user_wait_gate_test.py` runs on boards built the production way
(companion roles, `schema.sql`, the real migrate runner, `rbac.sql`):

- the shipped inspection workflow;
- the same without User Review;
- the same with a Librarian's documentation gate and the Publisher;
- that, with a Director's hold before User Review and gated branches off the
  way to it;
- the same declaring `user_waits: ["analysis"]`.

Writes go through the HTTP API as each role, through the real
`ticket-board-write`, or through the service's own connection where the
database alone is the question. The suite runs on main, from a git archive,
and on this tree. Main's run reproduces every gap listed above. On this tree:

- **The SYRD-222 shape:** a no-code UAT ticket in Implementation, owned by Ops.
  - The wait on the User is refused by every route: the API, the CLI,
    `await-role`, the function alone, the database's owner, the Director, and
    the Director's `await-user`.
  - The ticket, its wait, its comments, its notifications and the reservations
    are unchanged.
  - The ticket then takes the declared no-code submission, Audit's sign-off and
    the Director's acceptance into User Review. There it has its UAT steps and
    is in the User's queue, and Ops' slot is free.
- **An audited code ticket:**
  - there is no wait on the User from Audit;
  - neither `force_move` nor `director_edit` puts it in User Review without
    Audit's sign-off;
  - after the sign-off, the declared path reaches User Review;
  - after a return, the ticket owes Audit again.
- **Documentation and Publisher:**
  - User Review needs the documentation sign-off;
  - it needs the publication of the ticket's commit, and a publication of
    another commit does not count.
- **A Director's hold before User Review (Audit's topology).** This board has
  the shipped workflow with the Librarian and the Publisher, plus:
  - a system hold (`pre_user_hold`), reached from Implementation only by a
    gate skip;
  - a parking stage that Audit and an early review can park work in;
  - a gated review before Implementation;
  - a gated rework review that approves back into Implementation;
  - a gated review only after User Review.

  On it:
  - without Audit's sign-off, `force_move`, `director_edit` and the hold's
    declared release are refused, and nothing moves;
  - with Audit's sign-off but without the documentation sign-off, or without
    the publication, entry is still refused;
  - with all of them, all three ways in are admitted, and the gates off the
    way are not owed.
- **No User Review:** the refusal says so.
- **A person outside the board:** an `operator:` blocker is set and released as
  before, and moves nothing.
- **`await-user`:**
  - the User is refused, at the API and in the database, and so is Ops calling
    the database function directly;
  - a blank reason is refused;
  - in a listed stage, the Director records the wait with its comment, nothing
    is sent, and the read client shows it;
  - from Implementation it is refused, whatever `user_waits` says;
  - the Director's prompt offers it there;
  - a forged session setting is refused, and so is a forged mark (permission
    denied);
  - a withdrawn permission reports the standing wait as not valid and moves
    nothing;
  - the validator's refusals, and the database ignoring an entry a stored
    document should not have.
- **A historical wait:** a board built on main has one, recorded by Ops through
  `request-dependency`, and is upgraded to this tree.
  - The wait stays and is reported not valid, with its repair.
  - It is in the Director's queue, the Director is told once, and the real
    listener delivers that notice once.
  - Ordinary activity leaves the wait standing.
  - Clearing it moves nothing.
- **The board card:** the served `alertStackForTicket`, run under node over the
  real `user_wait` payloads.
- **Parity:** the twelve functions have the same body hash, owner and grants
  three ways: on a fresh `schema.sql` board, on a migrated board, and on a
  board upgraded from main.
