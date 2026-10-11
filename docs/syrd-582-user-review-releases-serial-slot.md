# SYRD-582: User review releases the implementer, and queued work is not delivered early

## What happened on Otto

Release f92a3d4d, the designer's queue, 2026-10-10/11:

- **OTTO-134: delivered while queued, then refused.** The Director routed it
  to the designer while OTTO-128 waited in `user_review` with the User. The
  board queued it (position 2, behind OTTO-128). Then the Director added a
  routing comment. `add_comment` raises an owner notice ("changed by
  director: new comment") for the ticket's current owner, and neither that
  notice nor the listener's delivery check asked whether the ticket was
  waiting. The designer was handed OTTO-134 and did the work. The board then
  refused the submission until the Director closed OTTO-128.
- **OTTO-133: queued and never delivered.** Routed the same way while
  OTTO-134 waited with the User. Its routing comment probably went in before
  the route, while the Director still owned the ticket, so no owner notice
  was raised. The designer sat idle until the User noticed.

The same route behaved differently because of the order of comment and route.
Underneath both: under the default `review` policy, `ticket_current_reserved_ticket`
let **every** review stage hold its author, including a review only the User
can move.

## The model now

The Director's triage settled the release point: a worker's reservation ends
before human User Review (SYRD-478/539).

- **A human's review releases the author.**
  - When a ticket enters a review stage assigned to a role of kind `user`,
    the trigger `record_user_review_release` writes a row to the new table
    `ticket_board.user_review_releases`.
  - Under the default `review` policy on a Director-dispatched board,
    `ticket_current_reserved_ticket` does not count a released ticket. The
    next ticket in the implementer's queue therefore becomes active, and
    `settle_serial_focus` hands it over once, in the same transaction (the
    SYRD-568 activation notice).
  - The trigger is named to run before the settle trigger, which reads it.
- **The release lasts.** It is recorded rather than read from the current
  assignee, so:
  - the User's sign-off into Director review does not take the slot back
    from the ticket the implementer took next;
  - nor does a Director's re-audit out of User review.
- **Rework re-holds the author.** Re-entering implementation removes the row.
  - A User kick-back therefore waits in the author's queue behind their new
    active ticket. It is not handed over and cannot be submitted.
  - It becomes active again by the ordinary queue rules.
  - Once resubmitted, it holds its author in Audit as before.
- **Unchanged:**
  - every review stage not assigned to a `user` role, such as Audit, Director
    acceptance and Director review;
  - the `lifecycle` policy (SYRD-276), which keeps the author through User
    review because its tenant chose that;
  - pull boards (SYRD-539), which keep their declared `release_after`;
  - legacy boards;
  - ordering, atomic claims, manual control, blockers and same-ticket gates.

  The row is written whatever the policy, and the policy decides what it means.
  So switching a board between policies applies at once, as SYRD-276 does.

## A waiting ticket is never delivered

- **At the source.** `notify_ticket_owner_in_place_change` covers a comment,
  an edit, a blocker change and a reassignment. It now raises no notice for a
  ticket queued behind its implementer's active work: `ticket_serial_waiting`,
  and no unresolved blocker of its own. The activation that later hands the
  ticket over points the implementer at the whole ticket, every comment
  included.
- **Blocked tickets are not covered.** A ticket held back by its own blocker
  still tells its owner what changed, as on main. It cannot be submitted, and
  the change may be what frees it. The case that matters most is blocking the
  ticket an implementer is working on: they must hear it.
- **At delivery.** The listener's `_notification_is_current` now drops, as
  stale, a notice addressed to the owner of an unblocked ticket that is
  queued in that owner's `serial_queue()`. The listener may read
  `serial_queue()`. It asks only in an implementation stage, and only when the
  board has the function. This catches rows queued by an older release: Otto
  has them now.

## The migration: pgu985

- Creates the table, the trigger and the new copies of the two functions.
- Records every ticket currently in a review stage assigned to a `user` role.
- Settles each implementer's slot **with** its handoff. A ticket that has been
  waiting behind one of them is handed over once. On Otto that is OTTO-133.
- Does not record a ticket already past the User's review in a later stage.
  No flag says reliably that it got there: Otto's `route director_review ->
  in_progress` and `user_reopen` keep `user_signoff`. Such a ticket holds as it
  did until it is finished.
- Moves no ticket.
- `rbac.sql` grants SELECT on the table to the board and the listener.
- `schema.sql` carries every final copy.

## Tests

`tests/user_review_serial_release_test.py` builds each board the production
way: schema.sql, the companion roles, the real `ticket-board-migrate` and
rbac.sql. It runs the real HTTP server and the real notify listener with a
recording sender. The workflow is the shipped document with Otto's queue
settings and Otto's Director re-audit route.

- **Main replayed** (a child on a `git archive` of main): both Otto cases
  reproduce.
  - A ticket routed while the active one waits with the User stays queued and
    silent.
  - The Director's routing comment delivers a queued ticket, whose submission
    is then refused.
- **This tree, Otto's OTTO-133 sequence on `app`:**
  - B routed while A is in Director acceptance waits silently, and a comment
    on it raises no notice at all.
  - A reaching the User hands B over once, across a listener restart.
  - The User's kick-back of A waits behind B and cannot be submitted, while B
    in Audit still holds `app`.
  - B reaching the User hands A back once, and A resubmitted holds `app` in
    Audit again.
- **OTTO-134 exactly, on `ops`:**
  - Route, then two Director comments, while E is with the User: F is handed
    over once, as active work.
  - The User's sign-off of E leaves F holding `ops` in Director review.
  - F's submission is accepted.
- **Blocking:** blocking `main`'s active ticket still tells `main`, once.
- **Policies:** a lifecycle board and a pull board releasing after User
  review behave exactly as on main.
- **Upgrade:** main's board in Otto's state is migrated. X is with the User,
  Y is waiting, W is waiting with an unsent comment notice, and V is in Audit
  for `ops`.
  - Y is handed over once.
  - W's old notice is dropped as stale.
  - V still holds `ops`.
  - Y can be submitted.
- **Copy parity and grants, run last:** a fresh schema.sql board has the same
  functions, trigger and grants.

The neighbour sweeps, the mutation results and the remaining exact figures are
in the ticket's evidence comment.
