# SYRD-549: a declared workflow's roles can record the waits it gives them

## What otto reported

Release 2951c5c9, OTTO-9:
1. **The ticket:** a Draft ticket owned by the declared `designer`. The
   designer asked the User five questions and ended its turn, waiting on the
   answers.
2. **The prompt:** the turn-end prompt said "`request-dependency` if you need
   another role", and offered a blocker and "continue work".
3. **The request:** the designer ran
   `request-dependency OTTO-9 --role user`, and nothing was recorded.
4. **The escalation:** at grace expiry the Director was told the turn was
   unresolved. By then the User had answered, so the Director acted on a
   stale escalation.

The report named three refusals in `ticket_board.set_awaiting_role`:
1. a fixed actor list, applied even under a declared workflow;
2. an awaited role must have a pane target, which the User does not;
3. Draft is not a stage a wait may be recorded in.

## Reproduced

`tests/declared_wait_admission_test.py` builds a board the production way and
gives it an otto-shaped declared workflow:
- **`designer`** owns Draft and may release a draft;
- **`uiux`** is a custom-named implementer holding `await_role`;
- **`scribe`** is a custom-named implementer that does not hold it;
- **`user`** is the declared User: kind `user`, active, with no pane.

Every write goes through the real `ticket-board-write`, as that role. On main
d546181, in a child process from a git archive:
- **Refusal 2 is real.** The designer's `request-dependency --role user` is
  refused "invalid awaiting_role: user". Nothing is recorded, not even the
  reason, because the comment and the wait are one transaction.
- **Refusal 3 is real.** In Draft, even a wait on the Director is refused
  "active ticket not found for awaiting_role".
- **Refusal 1 does not reproduce.** `uiux` records a wait on App, and it is
  handed over. Under a declared workflow `require_actor` admits by the
  caller's own `await_role` capability and ignores the list it is passed.
  That list applies only to a board with no declared workflow. `scribe` is
  refused by the API and by the database alone. The same scenario, run
  against otto's release 2951c5c9 from a git archive, gives the same answer.
- **The prompt offers moves the owner cannot make:**
  - a blocker, which only the Director may set;
  - "continue work", which no API or CLI route reaches
    (`grant_turn_continuation` has no caller);
  - `request-dependency`, to a role that does not hold `await_role`.
- **Otto's sequence:** the designer's wait on the User is refused, so the
  Director is escalated at grace expiry.

## The change

Migration `pgu989_syrd549_declared_waits.sql`, with the same text in
`schema.sql`. No signature changes, so no grant moves. The new helpers are
revoked from PUBLIC, as `rbac.sql` does for every function.

**Who a wait may name** is one rule, `wait_target_kind(role)`:
- **`pane`:** a declared active role with a pane target. It is handed the
  wait on the usual schedule.
- **`external`:** the declared User, meaning an active role of kind `user`.
  The wait is recorded and, like any wait, mutes the owner's reminders and
  unresolved-turn prompts for its four hours. Nothing is sent. A notice to a
  role with no pane is requeued forever (`role_runtime_unassigned`). And
  telling the Director that the User has not answered is the non-decision
  SYRD-273 ended. The User gets no process authority: it is the target of
  the wait, not an actor in it.
- **Not awaitable:** anything else, refused "invalid awaiting_role" as
  before.
- **A legacy board** keeps its fixed list, without the User.

**Where a wait may be recorded:** `workflow_wait_stage` now admits a declared
draft-kind stage that some role owns. Backlog, a draft stage nobody owns,
holds no work to wait on and is still refused. Legacy stages are unchanged.

**Who may record a wait** is unchanged:
- **On a declared board:** the caller's own `await_role` capability,
  checked by the API's handler and again by the database's `require_actor`.
- **`request-dependency`:** also needs `add_comment`, as SYRD-194 composed
  it.

A comment in `set_awaiting_role` now says the fixed list is the legacy
board's.

**What the prompt offers** is computed by `unresolved_turn_choices(state,
assignee, owner)`, and only from what the moves themselves check:
- **transition:** a declared transition out of the stage that names the
  owner (`transition_allows_actor`), and for an owner-scoped one, the owner
  is the assignee;
- **`request-dependency`:** the owner holds `add_comment` and `await_role`,
  and the stage admits a wait. The text adds "or on the User" where a User
  can be awaited;
- **a blocker:** the owner holds `set_blockers`;
- **"Continue work":** never offered, because nothing can reach it.
- **No move at all:** the owner is told so and asked to say in a comment
  what the ticket needs.

`unresolved_turn_repair_options` writes the sentence that names the moves.
The guard still writes the sentences before and after it, which readers of
the prompt and an existing wording test match on. The payload's `choices`
lists the same moves. A legacy board keeps
its rules:
- the owner's move;
- a wait for any owner but the User, in a wait stage;
- blockers only for the Director.

**Ending a wait** is unchanged:
- the owner clears it (`clear-awaiting-role`, which needs that capability);
- a stage or assignee change clears it;
- it lapses after four hours.

A comment from the User does not clear it, as no comment by an awaited role does (PGU-915).

## Tests

`tests/declared_wait_admission_test.py`, 22 checks.

**The board and harness:**
- the real notify listener, driven one loop iteration at a time;
- the real `PaneActivityGate` over real hook files;
- each pane role registered the production way, with a runtime assignment
  naming a live process;
- a second database with no declared workflow, for the legacy cases.

**Main d546181:** refusals 2 and 3; refusal 1 not reproduced; the prompt's
impossible moves; and otto's stale escalation.

**This tree:**
- **Draft, the User:** the designer records, in Draft, that it waits on the
  User, with its reason. Nothing is handed to anyone, and nothing is ever
  addressed to the User. The database alone admits a User wait too.
- **Draft, a pane role:** a wait on the Director is handed over on the usual
  schedule.
- **A custom implementer:** awaits App, which is handed the wait.
- **Refusals:**
  - `scribe`, without `await_role`, by the API and by the database alone;
  - an undeclared caller;
  - a role awaiting itself;
  - an inactive role awaited;
  - a wait in Backlog.
- **Clearing:** the owner clears its wait on the User. A role without the
  capability cannot.
- **Legacy:** a board with no declared workflow answers exactly as main.
  That covers a wait on the Director and its schedule, the User refused, the
  User unable to wait, and clearing.
- **The prompts:**
  - the designer's names its transition and `request-dependency ... or on
    the User`, and no blocker or "continue";
  - the scribe's names only its transition;
  - asked directly, the choices for four owner/stage pairs match the rule,
    including an owner with none, which is told so rather than offered a move.
- **The stale escalation:** a User wait recorded within the grace keeps the
  Director from being escalated. Once the wait is cleared, the next
  unresolved turn prompts the owner again.
- **Parity and upgrade:** `schema.sql` and `rbac.sql` install the same seven
  functions, owners and grants as the migrations. A board built on main takes
  the migration in place and ends up identical.

## Evidence

- **Mutation:** thirteen mutants, run through `tests/bounded_run.py mutate`,
  all killed. Each kill was read on its failing assertion:
  - **"The User cannot be awaited" (refusal 2):** the designer's request is
    refused.
  - **"Draft admits no wait" (refusal 3):** the same.
  - **"The fixed list decides on a declared board" (refusal 1, put back):**
    the designer is refused.
  - **"An unowned draft stage admits a wait":** the Backlog check.
  - **"The User is handed waits like a pane":** a schedule is queued to
    `user`.
  - **"A User wait gets the handoff schedule":** the same.
  - **"A role without await_role is admitted":** the database alone admits
    `scribe`.
  - **"Legacy may await the User":** legacy no longer matches main.
  - **"Every owner is offered a blocker":** the choices check.
  - **"Every owner is offered request-dependency":** the same.
  - **"A transition is offered whoever its actors are":** the same.
  - **"The guard's prompt keeps the fixed list":** the designer's prompt.
  - **"schema.sql: Draft admits no wait":** the parity check.
- **The sweep:** 130 related suites, run serially on this tree and on main
  d546181, with provider stubs that refuse to start. They are the suites
  touching awaiting roles, dependencies, the unresolved-turn guard, the wait
  stage, `schema.sql`, `rbac.sql` or migrations. Full output was compared,
  with temporary paths, times, ports and hashes normalised.
  - **Exit codes:** identical on all 130.
  - **The same reds:** 14 suites are red on both trees. 11 have identical
    output. The other 3 differ only where a migration listing now names
    pgu989.
  - **`ticket_board_durable_awaiting_role_test`:** red on both trees in the
    same way. Its fresh-board checks pass on both. It then fails in its
    upgrade section, which applies today's `rbac.sql` to a schema pinned at
    an old commit.
  - **`ticket_board_ordinary_pass_escalation_test`:** went red on the first
    sweep. It requires the guard's own text to say "The Director has NOT
    been told". So that sentence stays in the guard, and only the sentence
    naming the moves is computed. That test is unedited and passes.
  - **Main moved:** from 0d9e16d to d546181 (SYRD-578, SYRD-481) during the
    work. Neither commit touches the board's SQL or the listener. The test,
    its child run of main, all thirteen mutants and this sweep were run on
    d546181.
