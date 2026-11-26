# SYRD-888: a scope change names the target of its move into Implementation

## What happened

On sapi (release e71574b6, declared workflow revision 43),
`ticket-board-write request-scope-change SAPI-159` was refused:

    unknown or ambiguous workflow action; specify target

The whole call rolled back: no plan ticket was opened, and nothing was recorded
on SAPI-159.

## Why

`request_scope_change` (SYRD-843) opens a plan ticket in analysis. It then picks
the Director's declared move from analysis into a stage of kind
`implementation`, and performs it. It performed that move by its action name
alone.

The executor, `perform_workflow_action_as`, matches a move by its source stage
and action name. It narrows the match by target only when the payload names one,
and refuses any count other than one. sapi declares two Director `route` moves
out of analysis, one to `in_progress` and one to `user_review`. So the bare
name `route` matched two moves, even though the function had already chosen the
one into Implementation.

## The fix

The function now keeps the chosen move's target stage, and passes it to the
executor as `target`, which is the same field `route --state` sends. For a
deterministic choice, the selection is ordered by action and then target.
Nothing else changes: the same move is chosen and the same plan owner is
assigned.

- `migrations/pgu1062_syrd888_scope_change_route_target.sql` replaces the
  function, keeping its signature. `schema.sql` carries the same definition.
- In SYRD-855's `schema_compatibility` table, pgu1062 is compatible for older
  code, with no condition. Older code calls the function the same way. It gets
  the same result, or a scope change where it used to get the refusal.

## Pinned by

`tests/scope_change_route_target_test.py` (17 checks) first checks that schema.sql's
function is the one pgu1062 installs, so fresh and upgraded boards run the same
code. It then runs on a real board,
built two ways: fresh from this tree, and upgraded from the parent tree through
its migrations. The workflow is the board suites' shipped one, plus three
changes:

- the plan gate, owned by a new Designer;
- a second Director `route` out of analysis, to `user_review`;
- that route declared before the one to `in_progress`.

The checks:

- `route` leaves analysis for two stages;
- the scope change is accepted;
- one plan ticket opens, in `in_progress`, for the Designer, flagged
  `plan_change`;
- the waiting ticket is blocked on it;
- a second scope change extends it.

On the parent tree, the same test fails at "the scope change is accepted", with
sapi's error verbatim.

## Not changed

The other in-schema caller that passes an action name, `recover_stalled_ticket`,
chooses the owner's single no-code move from the stage. It would meet the same
refusal only if the workflow declared a second move with that action name out of
the same stage. No shipped composer declares one, so it is left as it is.
