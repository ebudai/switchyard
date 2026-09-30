# SYRD-307: a kick-back's options mean what the declared workflow does

## What happened

On MEFP, a Director ran
`ticket-board-write director-dat-kick-back MEFP-… --reason … --target-assignee luna-2`,
which `--help` advertises. The command was refused with "unknown workflow
action payload field". The reason-only kick-back worked. The same mismatch was
reported again as SYRD-427, SYRD-455 and SYRD-483.

## Measured on main 8e031b7

The board was built the production way: companion roles, `schema.sql`, the
real `ticket-board-migrate`, `rbac.sql`, and a declared workflow. It was
driven through the real `ticket-board-write` CLI and HTTP server.

| command | `--target-assignee` | reason only |
|---|---|---|
| `audit-kick-back` | refused, unknown payload field | returns to the implementer |
| `director-dat-kick-back` | refused, unknown payload field | returns to the implementer |
| `inspector-kick-back` | refused, unknown payload field | **also refused**: the client sends `recommendations`, which the declared schema does not know either |

- **Why the target is refused.** On a declared board, the server hands a
  kick-back to `perform_workflow_action`. That function accepts only `target`,
  `assignee`, `commit_hash`, `text` and `reason`.
- **Legacy boards are different.** The legacy kick-back SQL functions still
  map `target_assignee` to the assignee. Nothing here changes that path.

## The contract, from the declared executor

A kick-back is a `return` primitive. `enforce_declared_ticket_update` sends it
to:
1. the recorded `last_implementer_assignee`, when that is an owner of the
   destination;
2. otherwise the current assignee, when that is an owner;
3. otherwise the destination's first owner.

It ignores any requested assignee. So on a declared board a kick-back cannot
choose its target. Letting the reviewer choose one would give the reviewer new
authority over assignment, which the ticket forbids. Adding
`target_assignee` to the allowlist would have hidden the error while silently
ignoring the request.

## The change

In `TicketBoardApp.perform_workflow_action` (declared path only):
- **`--target-assignee` becomes a confirmation.** When it names exactly where
  the return goes (case and spaces ignored), the kick-back proceeds. When it
  names anyone else, the command is refused **before anything changes**, with
  guidance:
  > director_dat_kick_back returns PGU-35 to its recorded implementer, ops; it
  > cannot send it to main. Kick it back without --target-assignee, then have
  > the Director reassign it
- **Only `return` transitions take a target.** On any other transition it is
  refused ("takes no target assignee").
- **An Inspector's `recommendations` are its reason**, unless a reason or text
  was also given.
- **`--help` for all three commands** now says the option is an optional
  confirmation.

**Unchanged:**
- **The executor:** actor and owner checks, return to the recorded
  implementer, serial reservations, and the clearing of `audit_signoff` and
  `user_signoff` on return (asserted for the DAT kick-back).
- **The reason-only workaround.**
- **Generic payload permissions.** The allowlist is not widened.
- **Legacy boards and the SQL.**

## Evidence

- **`tests/kickback_target_assignee_test.py`.** It covers all three
  kick-backs, each run three ways: confirming target, other target, and
  reason-only. It also covers a non-return action. The code and SQL from
  8e031b7 run in a child process to reproduce the defect, and this tree
  shows the fix. It passes both under `env -i` and in a role pane.
- **Mutation, with `tests/bounded_run.py mutate`: 9 mutants, 8 killed.**
  Each kill was confirmed from its assertion line. The one survivor adds
  `recommendations` to the allowlist. It is equivalent, because the key is
  popped before the check.
- **The middle fallback of the return rule is not exercised.** That is the
  current assignee being an owner of the destination. It mirrors the
  executor, but a reviewer holding a review stage is never an owner of
  `in_progress`, so the test does not reach that branch.
- **Sweep against 8e031b7.** 56 suites were run under `env -i`: every suite
  naming kick-backs, `perform_workflow_action`, the write CLI/client,
  `target_assignee` or `recommendations`, minus six launcher/CLI suites that
  start real sessions and never reach this code. 51 pass on both trees and 5
  fail identically on both. `ticket_board_park_blocked_postgres_test` fails
  the same one case on both trees. The other four stop at the first failure,
  at the same line on both trees.
- **Legacy kick-backs.** `ticket_board_write_api_test` stops before its
  legacy kick-back cases (`exercise_write_api`, including `target_assignee`
  and `recommendations`). Run with only the step that already fails skipped,
  it prints `ok` on both trees.
