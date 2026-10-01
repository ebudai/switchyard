# SYRD-536: a declared return out of review needs no sign-off; what is advertised is what the board will do

## What happened

MEFP-233 was in `user_review`:
- assigned to the User;
- `audit_signoff=true`, `needs_user_signoff=true`, `user_signoff=false`;
- not held, and with no blockers.

Its `workflow_actions` advertised the Director's `route` → `analysis`
(primitive `move`). After finding a UAT-oracle edge case, the Director ran
`ticket-board-write route MEFP-233 --state analysis`. The board refused it:
`stage signoff required` (`enforce_declared_ticket_update`). That was a
review return, not a relayed rejection and not an approval.

## Cause, on main a8d3be9

`enforce_declared_ticket_update` (pgu961, identical to `schema.sql`) refuses
any move out of a stage that has a sign-off unless:
- the primitive is `return` or `reopen`, or
- the destination is a parking stage, or
- the sign-off is set.

So a Director's `move` back to `analysis` was treated as forward promotion. The
advertisement (`available_transitions`) filters only by actor and owner, so it
offered moves the guard refuses. That included `route` → `audit` and `cancel`
(both still refused for want of the sign-off, as is any non-return exit).

## The rule

A move out of a stage S to a destination D is a **declared return** when all
four hold:
0. **S is a review:** it carries a sign-off. Anywhere else a move is
   promotion or routing, whatever reopen shares its destination (see the
   Audit correction below);
1. **D is where S already sends work back:** some `return` or `reopen`
   transition out of S lands on D;
2. **D is not on S's declared forward path:** the destinations of S's
   approvals and S's gate skip, transitively;
3. **D is not terminal.**

It is read only from the declaration, never from a label or an action name. A
`return` must target the implementation stage (`validate`). A `reopen` has no
destination constraint, which is why rule 2 exists: a reopen declared into the
forward path is not a return.

For MEFP's `user_review`, the User's `user_reopen` lands on `analysis`, so the
Director's `route` → `analysis` is a return. `route` → `audit` is not, because
nothing declares `audit` as a way back from `user_review`, and it still needs
the sign-off.

**A declared return:**
- **needs no sign-off,** and grants, clears and forges none (`user_signoff`
  stays false and `needs_user_signoff` stays true);
- **keeps the candidate:** `commit_hash` and the audit sign-off ride along,
  because a `move` clears neither;
- **is not stopped by a blocker,** since handing work back is not promotion.

**Unchanged:**
- forward moves, `mark_done`, approvals and the User's own actions;
- the actor, owner and configured-transition checks;
- blockers on forward moves;
- SYRD-271's stale-sign-off clearing on a new candidate;
- parking, relays and terminal reopens.

## The change

- **`ticket_board.declared_review_return(cfg, source, destination)`**, a new
  function. `enforce_declared_ticket_update` uses it twice:
  - **the sign-off,** as its own first branch: a non-approval declared return
    does nothing there. SYRD-263's parking clause stays byte-identical, and
    `director_defer_unaccepted_review_test` requires that. My first version put
    the condition inside that clause and broke that test; I narrowed my change
    rather than edit its fixture;
  - **the blocker check.**
- **Migration `pgu969_syrd536_declared_review_return.sql`.** It holds the
  helper, a `REVOKE EXECUTE … FROM PUBLIC` for it, and the guard. The guard is
  pgu961's apart from the two exemptions, and `schema.sql` carries the same
  text.
  - The helper needs no grant: the guard runs inside the definer-rights
    workflow action, like `declared_parking_stage`.
  - SYRD-530's migrate runner already revokes PUBLIC execute after every
    migration, so the migration's own revoke matters only for a migration
    applied outside the runner.
- **`workflow_config.declared_review_return`,** the Python mirror.
  - `advertised_transitions(cfg, ticket)` is `available_transitions` without
    the moves the stage's missing sign-off refuses, by the same rule. The
    ticket reads (`workflow_actions`) use it.
  - Dispatch still uses `available_transitions`, so a refused move gets the
    guard's own truthful refusal, not "unknown action".
  - On an unsigned User Review, `route` → `audit`, a forward move and `cancel`
    are no longer advertised; they were already refused.

## Evidence

- **`tests/declared_review_return_test.py`.** It runs on a production-built
  board with a MEFP-shaped declaration: the User signs off or reopens to
  `analysis`, and the Director routes `user_review` to `analysis` or `audit`,
  plus one Director move forward to `director_review`. It drives the real app.
  Main's code and SQL, run in a child from a git worktree, refuse the
  advertised route with `stage signoff required`, while both routes are
  advertised.
  - **The route back to `analysis`** succeeds. `user_signoff` stays false,
    `needs_user_signoff` stays true, and the audit sign-off and candidate are
    unchanged.
  - **Still refused for the sign-off:** `route` → `audit`, the forward move,
    and `cancel` (refused on main too).
  - **Other refusals:** a non-Director, and an unconfigured route.
  - **Blockers:** a blocked return succeeds, and a blocked forward move is
    refused.
  - **The User's own sign-off** is unchanged.
  - **The advertisement** lists `route` → `analysis` and not the refused moves.
  - **Python and the database** agree on every stage pair. A `reopen` declared
    into the forward path is no return in either.
  - **A fresh `schema.sql` board and a migrated board** install the same guard
    and helper.
  - **Grants:** only the owner may execute the helper, before `rbac.sql` and
    after it. That comes from SYRD-530's migrate runner revoking PUBLIC
    execute, not from pgu969's own revoke, which is equivalent on every
    runner path. The test says so rather than claiming it.

  It passes both under `env -i` and in a role pane: 19 checks, after the correction below.
- **Mutation, with `tests/bounded_run.py mutate`: 8 of 9 killed.** Each kill
  was confirmed from its assertion line. The mutants cover:
  - the sign-off still demanded on a return;
  - a blocker stopping a return;
  - any destination counting as a return;
  - the forward path ignored;
  - `schema.sql` not updated;
  - the advertisement ignoring the sign-off, or dropping declared returns;
  - the mirror ignoring the forward path.

  The survivor drops the migration's own `REVOKE … FROM PUBLIC`. It is
  equivalent: SYRD-530's migrate runner revokes PUBLIC execute after every
  migration.
- **Focused sweep against a8d3be9.** 19 suites naming `workflow_actions`,
  `available_transitions`, the guard, parking, relays or blockers' promotion
  refusal were run under `env -i`, with refusing stubs; none was reached. 15
  pass on both trees and 4 fail on both. `ticket_board_park_blocked_postgres_test`'s
  known "it applies last" failure now lists pgu969 too.

## Audit correction: only a review is a review source

Audit returned 36d35ec. `declared_review_return` never required the source
stage to be a review, and the guard consults it in the general blocker check.

**Audit's case,** reproduced through the real production schema and app:
- `analysis` has no sign-off and an ordinary Director `route` → `in_progress`;
- an otherwise-valid `reopen` `analysis` → `in_progress` is added;
- a ticket in `analysis` carries an unresolved blocker.

The route counted as a return, and started the blocked work.

**The fix.** The helper, and the Python mirror, now return false unless the
source stage carries a sign-off. Both copies stay identical in `schema.sql`
and pgu969.

**The test** reproduces Audit's case on the same board, as a new workflow
revision:
- the blocked forward route is refused ("unresolved blocker prevents forward
  promotion") and the ticket stays in `analysis`;
- the helper answers false in both copies;
- no stage without a sign-off is a review source for any destination, in
  Python or the database.

**Against the rejected candidate,** the same scenario lets the route through
to `in_progress`, and reports five non-review "returns" in each copy.

**Mutation,** with the review-source check removed:
- from the SQL alone, or from Python alone: killed by the Python/SQL agreement
  check;
- from all three copies at once, so that they still agree: killed by Audit's
  assertion itself, which is the defect.

The full plan kills 10 of 11. The survivor is the migration's own PUBLIC
revoke, equivalent under the runner, and it is reported as such, not as a
kill.

**The positive case is unchanged:** MEFP's `user_review` → `analysis`, with no
forged sign-off and the gate, audit and candidate untouched. The suite passes
both under `env -i` and in a role pane, 19 checks, and the focused sweep is
unchanged.

No live MEFP-233 change, workflow write, relay, gate clear or override.
