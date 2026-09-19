# Recording the acceptance the User cannot record

SYRD-214 gave the Director a way to relay the User's rejection, and drew the
fence at returns: a relayed approval, it argued, is that person's sign-off
forged, and that is the one thing this must never become.

The argument was half right and the fence was in the wrong place.

The User's *acceptance* is exactly as unreachable as their rejection. `user_sign_off`
belongs to the `user` role and correctly refuses the Director, and the human
User has no pane. So on SYRD-211 the User completed the acceptance steps, said
so in the Director conversation, and the Director honoured it the only way left:
a narrated `override-move` to `director_review`, then the ordinary close. The
ticket shipped with `user_signoff=false` and a paragraph of prose where the
sign-off should have been.

Refusing to write that sign-off protected nobody. It did not stop the decision
being acted on; it moved it into the workflow's own bypass, where the
destination is whatever the controller types, no gate is checked, no commit is
named, and nothing in the record distinguishes the User's verdict from the
Director's. The board declined to write a sign-off it could not verify, and
thereby lost the only structured trace it could have had.

## What replaced the fence

`relays_decision_of` now admits `approve` as well as `return`, under a rule that
is tighter than "never approve" everywhere it matters:

- **A relay must mirror a move the relayed role can itself make out of that
  stage** — same primitive, same destination, same sign-off clearing. A relay is
  that role's own step under another hand, never a route the role does not have.
  Where several of the role's moves match and disagree about their destination,
  the document is refused rather than resolved.
- **A relayed approval is only for a role with no pane of its own.** Every role
  that can be driven on this board has a `target`; the User is the one that does
  not, which is the whole reason its verdict arrives in conversation. A reviewer
  that can sign off for itself must. An installation that gives the User a
  surface stops satisfying this and keeps direct sign-off — which is the correct
  answer there, and is the same condition the migration checks before granting
  anything.
- **A relayed approval names the commit it accepts**, and may not be the move
  that ends a ticket. It hands the work on; closing stays a separate act.
- The SYRD-214 conditions are unchanged: a reason is required, the relayed role
  may not be an actor of its own relay, and a relay is never owner-scoped.

The pane rule is on approvals only. Applied to returns it would retroactively
invalidate documents `pgu952` legitimately produced, and the migration written
to clean those up would fight `pgu952` on every run — each undoing the other,
two revisions at a time, for ever. Returning work is not what this protects.

## The part that is a weakening, said plainly

SYRD-82 established that the director must not be granted sign-off authority:
an `approve` transition writes its source stage's sign-off flag, so listing the
director among its actors hands the controller the power to approve the work it
directs. This ticket narrows that prohibition by exactly the relayed case.

The narrowing is real and should be read as one. What bounds it:

- the director still holds no approval of its own anywhere in the document — a
  relayed approval with its provenance stripped is a plain director sign-off,
  and both validators still refuse it;
- the relay can only do what the User could have done from that stage;
- it is available only because the User has no pane, and disappears if that
  changes;
- and the ticket still stops at Director review, where closing it is a separate,
  commit-bearing act.

What none of that does is make the Director honest. A Director who misreports
what the User said produces a false sign-off, and the board cannot tell. It
could not tell before either: the same false acceptance was reachable by a
narrated override that named no commit, checked no gate, and left no structured
record at all. This trades an undetectable bypass for an undetectable
misstatement that is at least written down, attributed, and bound to a commit.
That is an improvement, not a proof.

## The record

The executor composes the attribution from `relays_decision_of`, and an approval
gets its own wording, because the rejection relay's disclaimer would be false
here:

> director relayed this decision from user, who reported it outside the board.
> It is recorded as user sign-off, decided by user and entered by director, and
> director did not perform the review behind it.

followed by what the Director was told. A direct `user_sign_off` writes no such
comment, so the two are never confusable in the ticket.

## Preconditions the User's own sign-off does not have

A relayed approval is held to more than the role's own would be, deliberately.
Before it is accepted:

- the stage's gate must actually be set (`needs_user_signoff`);
- every other review the ticket is subject to must already have been given — a
  ticket that reached `user_review` without its audit cannot be accepted by
  relay;
- the payload must name the exact candidate the ticket already records, and it
  cannot change it. A commit-exempt ticket has nothing to name, and naming one
  anyway is refused.

The User signing off an unaudited ticket is their own mistake to make, in front
of the work. The same thing relayed is a mistake nobody in the conversation is
placed to catch.

## Installation

`schema.sql`, `examples/workflows/inspection.json` and the three changed
functions cover fresh provisioning.
`scripts/ticket_board/migrations/pgu953_syrd217_relay_user_acceptance.sql`
brings an existing board to the same place: it re-creates `signoff_reset_key`,
`validate_declared_workflow` and `perform_workflow_action_as` with bodies
identical to the fresh schema's, and adds the transition with a revision bump, a
`workflow_revisions` row and the state-transition notify.

It copies its destination and sign-off clearing from the User's own sign-off
rather than restating them, and adds nothing where the User has a pane, where no
user-owned approval leaves `user_review`, where that approval would end the
ticket, where the action name is taken, where the tenant already relays the
User's acceptance under another name, or where more than one active role holds
the control capabilities. `pgu952` is not rewritten: a migration keeps the body
it shipped, or it stops describing what the boards that already ran it received.

## A consequence worth stating: the document is no longer backward-readable

Every migration carries its own copy of `validate_declared_workflow`, and
several re-validate the stored document as part of their repair. Until now the
shipped `examples/workflows/inspection.json` could be handed to any of those
older copies and be accepted -- SYRD-214 squeaked through because a director
*return* was already legal.

A relayed approval is not. Every validator before `pgu953` refuses
`relay_user_sign_off` outright, with `director must not be granted sign-off
authority`, and that refusal is correct for what those validators knew: they
have no relayed case to except. So the current document cannot be loaded by an
older release's validator, and there is no way to express this transition such
that it could be.

This does not affect a real board. Migrations apply in order, once: a board
carrying a document with relayed decisions has already applied the migrations
that understand them, and a board still running `pgu927`'s validator is still
running a `pgu927`-era document. It does affect suites that deliberately
construct that pairing to prove what an upgrade does -- they apply one old
migration on top of the current schema and then hand it today's document.
`tests/workflow_document_eras.py` gives them `before_relaying()`, and seven
suites now seed with it, which is also a more faithful fixture: a tenant about
to run `pgu927` did not have relays.

One of those, `ticket_board_director_capability_floor_test`, replays the entire
tail from `pgu927` onward, so seeding it this way makes it exercise `pgu952` and
`pgu953` granting both relays in sequence during a real replay.

## Verification

`tests/relayed_user_acceptance_test.py` drives the real HTTP handler against a
real cluster, twice: on a board provisioned from `schema.sql` and on one that
reaches the action by migration, since those install separate copies of the same
functions. `tests/schema_function_drift.py` asserts the copies match, resolving
"the migration" as the newest one defining each function — the one an upgraded
board is actually left running.

Each pass checks the exact transition and that it stops there, the sign-off
written, the candidate untouched, all four clauses of the attribution and the
absence of the rejection relay's disclaimer, the commit-exempt case, that direct
`user_sign_off` still works and leaves no relay record, every other role refused
by name, and the refusals: no reason, no commit, the wrong commit, the gate
unset, an earlier review missing, and the wrong stage. The upgrade pass first
reproduces the defect on the pre-change document, where the Director is offered
nothing out of `user_review` that reaches Director review at all.

`tests/relayed_user_rejection_test.py` was updated for the widened rule and
still passes; its approve-shaped refusals now assert what an approval must
carry rather than that approvals are impossible.

Mutation coverage: 32 mutants across the executor's preconditions and record,
both validators' fences, the narrowed floor, the migration's guards, the shipped
document and the drift guard itself. No survivors. Four of them survived the
first pass and each named a real gap rather than a missing assertion — the
`move` primitive was only redundantly blocked, the sign-off comparison was only
order-insensitive by accident of fixture ordering, and two migration guards were
each masked by the other.
