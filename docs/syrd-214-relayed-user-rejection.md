# Recording a User rejection the User cannot record

The User does not operate a board pane. UAT is run and reported in conversation
with the Director, and the board learns the outcome only because the Director
puts it there. For a passed UAT that is fine -- nothing on the board is waiting
on a keystroke the User does not have, because the Director can route the ticket
onward. For a failed one there was no move at all.

`user_kick_back` is `owner_scoped` and belongs to the `user` role, which is
correct: a sign-off decision is the reviewer's, and a role that can be relayed
into approving is a role whose sign-off means nothing. But the document then
offered the Director no way out of `user_review` toward correction. Out of that
stage the Director could defer, cancel, and nothing else. So a failed UAT was
honoured by `override-move` back to `director_review` followed by an ordinary
`director_kick_back` -- the workflow's own bypass, used for a decision the
workflow should simply have had a name for.

That happened three times on SYRD-211. Once is an operator working around a gap;
three times is the gap.

This is a declared-workflow gap only. The hardcoded seed in `schema.sql` gives
the Director `route` out of `user_review` back to `analysis`, `inspection` and
`audit`, so a board without a declared document always had a correction path and
needs nothing from this ticket.

## The action

`relay_user_kick_back` runs `user_review -> in_progress` for the control role.
It is the `return` primitive, it requires a reason, and it clears the same
sign-offs `user_kick_back` clears, so the ticket lands exactly where the User's
own rejection would have put it: with the implementer, with `audit_signoff` and
`user_signoff` cleared, the commit hash dropped, and one transition notification
to the role now holding the work. Nothing about the correction path is special
to it having been relayed.

What is new is the fourth thing the transition carries. `relays_decision_of`
names the role whose decision this is, and the executor composes a preamble onto
the recorded comment from it:

> director relayed this decision from user, who reported it outside the board.
> It is recorded as the decision of user, it is not user sign-off, and director
> did not perform the review behind it.

Composed by the executor rather than left to whoever types the reason. Provenance
that depends on wording is provenance that goes missing the first time somebody
is in a hurry, and the one thing a later reader cannot reconstruct from the
ticket is whether the Director made this call or carried it.

## What a relay may not be

> **Superseded in part by SYRD-217.** The return-only rule below was the fence
> as this ticket shipped it. SYRD-217 found the case it got wrong -- the User's
> acceptance is as unreachable as their rejection, and refusing to record it
> pushed the same decision into a narrated override where nothing was checked
> at all -- and replaced "never approve" with "never anything the relayed role
> could not have done itself, from here". See
> `docs/syrd-217-relayed-user-acceptance.md`. Everything else in this document
> still holds, and `relay_user_kick_back` is unchanged.

A role acting for another role is a narrow and easily-abused thing, so
`relays_decision_of` is fenced rather than merely declared. A transition that
names one must use the `return` primitive, must require a reason, must not be
`owner_scoped`, must not list the relayed role among its own actors, and must
name a role the document actually declares. The first of those is the one that
matters: **a relay can only ever send work back**. There is no shape of this
field that approves, sets a sign-off, or carries a ticket past `user_review` --
not for this action and not for one a tenant writes itself.

The fence is in both validators. `scripts/ticket_board/workflow_config.py` holds
it for documents loaded from the repository, and `validate_declared_workflow`
holds it in the database for documents that arrive any other way. The handler
runs the Python one first, so on the HTTP path the SQL fence is only ever
reached by a writer that has already been stopped -- which is exactly why it is
tested directly rather than through the handler.

`force-move` and `override-move` are unchanged and remain the controller's
bypass for cases the document does not describe. This ticket removes one case
from that list; it does not touch the bypass.

## Installation

`scripts/ticket_board/schema.sql` carries the transition, the two changed
functions, and the field in `examples/workflows/inspection.json`, so a board
provisioned from now on has the action from the start.
`scripts/ticket_board/migrations/pgu952_syrd214_relay_user_rejection.sql` brings
an existing board to the same place: it re-creates the two functions with bodies
identical to the fresh schema's, and adds the transition to the stored document
with a revision bump, a `workflow_revisions` row, and the state-transition
notify that makes open boards pick it up.

The migration copies its destination and its `clear_signoffs` from the User's
own kick-back rather than restating them, so the two cannot disagree. It adds
nothing where the shape it repairs is absent or ambiguous: where no return out of
`user_review` belongs to the `user` role, where the tenant already relays the
User's rejection under a name of its own, where the action name is already taken,
or where more than one active role holds the control capabilities -- because
handing the authority to record a User's rejection to the wrong role is worse
than leaving a tenant to grant it deliberately. Re-running it grants nothing
twice and writes no revision nobody asked for.

## Verification

`tests/relayed_user_rejection_test.py` drives the real HTTP handler against a
real PostgreSQL cluster, twice over: once on a board provisioned from
`schema.sql` and once on a board that predates the action and reaches it by
migration, since those install separate copies of the same two functions. It
asserts the copies are identical, so they cannot drift.

Each pass relays two failed UATs in a row, returning to `user_review` between
them along the real forward path -- `submit_to_audit_without_commit`,
`audit_sign_off`, `director_dat_sign_off` -- with no `override-move` anywhere in
the loop. It checks the destination, the cleared sign-offs, the four clauses of
the attribution preamble, the reason body, that `user_signoff` is never set, that
exactly one notification reaches the implementer, that every other role is
refused by name, and that a reasonless relay moves nothing.

The upgrade pass first reproduces the defect on the pre-change document: the
Director is refused `relay_user_kick_back`, refused the User's own
`user_kick_back`, and offered no transition out of `user_review` that reaches
implementation at all. It then applies the migration, checks what was added
against the User's kick-back it was copied from, re-applies it, and checks the
four tenant shapes it declines.

Mutation coverage: twenty-five mutants across the executor's preamble, both
validators' fences, the migration's guards and the shipped document, with no
survivors, and no survivors either with the drift guard silenced -- so each is
named by what the code does, not only by two files differing.
