# SYRD-256 — a tenant report must file on a declared-workflow board

## What failed

The MEFP Director's live upstream report got past the report token (SYRD-238)
and then failed with

```
invalid configured caller role: <NULL>
```

and no ticket was created.

The report-only HTTP action authenticates `X-Ticket-Board-Report-Token` and
calls `ticket_board.file_report` with **no caller role**. That is by design: the
report credential is not a role, and must never act as one.

Reproduced exactly on a real cluster, over HTTP, with the real
`configure_workflow` applied: a legacy board files the report (201); the same
board after declaring a workflow refuses it with the live message, from the
same line.

## Two refusals, not one

1. `file_report` called `require_actor(ARRAY[]::text[], 'file_report')`. The
   declared-workflow branch of `require_actor` resolves `current_app_actor()`,
   which refuses a NULL role.
2. `file_report` then rebuilds the ticket's export projection
   (`refresh_ticket_source_json`), which is an `UPDATE`. On a declared board
   that runs `enforce_declared_ticket_update`, which resolves the actor **in
   its `DECLARE` block** — before any of its existing windows (force-move,
   merge) can run — and refuses NULL again.

Fixing only the first exposed the second. That intermediate state is what the
mutation campaign's R3 reproduces.

## The fix

`file_report` alone changes. `require_actor`, `current_app_actor` and both
update triggers are untouched, so no other operation's authority moves.

**Its own authority check.** The database writer must be
`ticket_board_service`; no role is consulted at all. It neither refuses the
report path's missing role nor lends it any role's capabilities. What it may
write is unchanged and fixed by the function: a new ticket in `analysis`,
`unassigned`, audit required, carrying an origin project.

**A non-role actor for the one refresh statement.** For a same-state,
same-owner, no-flag update, `enforce_declared_ticket_update` needs only that
*some* actor is named; it compares that name against nothing else except
`'director'` (for reassignment) and transition actor lists. So `file_report`
names `report:tenant` as `ticket_board.workflow_actor`, immediately before
the refresh, and clears it immediately after — the same scoping the workflow
executor already uses for the same setting.

`report:tenant` can never be a role: `workflow_roles.name` is `CHECK`ed
against `^[a-z][a-z0-9_-]{0,63}$` in the database, and it contains a colon.
It is in no transition's actor list and is not `director`, so it can move,
reassign and flag nothing. Every structural check the trigger makes still
runs.

Rejected alternatives:

- **Set a real role for the refresh.** That is the report token borrowing a
  role's identity — what the ticket forbids.
- **A window inside `enforce_declared_ticket_update`.** Correct, but the actor
  is resolved in `DECLARE`, so it needs restructuring a shared, heavily pinned
  function; `ticket_board_park_blocked_postgres_test` asserts that body
  verbatim.
- **Skip the refresh.** `source_json` is an export projection that nothing in
  Python reads, but every writer keeps it complete. Skipping would leave report
  tickets quietly different from every other ticket.

### Shipping it to an installed board

MEFP's board predates this. `pgu957_syrd256_report_only_file_report.sql`
carries the new function, identical to `schema.sql` character for character,
and sorts after `pgu765`, which installed the old copy. It is idempotent
(`CREATE OR REPLACE` plus a no-op grant) and the signature is unchanged, so
`rbac.sql` and existing grants still apply.

## Evidence

`tests/tenant_report_declared_workflow_test.py`: 39 checks against a real
cluster, over real HTTP, with the workflow declared through the real handler.

- A legacy board files the report (control).
- A declared board files it: `analysis`, `unassigned`, origin project and
  source ref kept, audit required, no parent. The stored projection equals a
  fresh `build_ticket_source_json`, so the refresh really ran.
- The report-only boundary holds on the declared board. These are refused: no
  token, a wrong token, the **write** token, and six protected fields. The
  report token opens nothing else (`create_ticket`, `add_comment`, `route`,
  `configure_workflow`). None of these creates a ticket.
- **The named actor carries no authority.** This is driven as the superuser.
  The service role has no direct `UPDATE` on `tickets`, so trying it as the
  service would be refused by the grant and prove nothing about the trigger.
  Under `report:tenant`, a state change, a reassignment and a flag change are
  each refused by the trigger's own message, and the ticket does not move.
- **It can never be a role.** Inserting it into `workflow_roles` violates the
  CHECK constraint.
- **It does not outlive the refresh.** The setting is empty when
  `file_report` returns.
- **Only the service may call it.** `postgres` has EXECUTE everywhere, so this
  reaches the function body rather than a missing grant.
- **Upgrade path.** On a declared board holding `pgu765`'s copy, the report
  fails with the live message and creates nothing. Applying `pgu957` repairs
  it, and replaying `pgu957` is harmless.
- **Drift.** `pgu957` owns `file_report`, `schema.sql`'s copy is inside it,
  and it applies after the copy it replaces. The function's *code*, with
  comments stripped, calls neither `require_actor` nor `current_app_actor`.
- The suite reads the actor from the shipped function rather than restating
  it, so every guarantee is about the string actually used.

**Mutation: 11/11 killed.** Every SQL mutant is applied to `schema.sql` and
`pgu957` identically, so a kill has to come from behaviour rather than the
drift check. The mutants: restore `require_actor`; drop the service check; name
no actor; leave the actor set; borrow `director`; name a valid role string;
skip the refresh; assign the report; drop required audit; let protected fields
through the handler; ship the old body in the migration only.

R3 (name no actor) was killed first by the marker-extraction assertion. The
behavioural case covers it too: it is exactly the intermediate state above,
where the declared report still failed in the trigger.

## A neighbour's check changed

`tests/permission_prompt_waits_migration_test.py` asserted that `pgu956` is
"the last ordered migration". That holds for one release; any ticket that ships
a board fix then breaks it with nothing wrong. `ticket_board_park_blocked_postgres_test`
has the same check and has been red on main since `pgu955` landed. No narrower
fix exists: repairing an installed board needs an ordered migration, and a new
one must sort after `pgu956`.

The check now states the invariant its own comment gave: *every board function
`pgu956` calls exists before it runs*, either in the floor an upgrade starts
from or in an earlier migration. The dependencies are read from the function's
own code. All three already exist in that floor. Verified falsifiable by
appending a call to a function no earlier migration installs; the check then
names it. `park_blocked` is left alone: it is red on main at that same line,
and it still fails identically.

## Sweep

19 suites, run serially under `env -i` against a clean `origin/main` worktree at
`faafe96`. Pass/fail is identical per suite, except for the neighbour above
(now green) and the new suite.

Three suites are red on both trees at the **identical line**:

- `ticket_board_write_api_test`: line 2191, env-widening RBAC, which needs a
  subprocess this pane cannot run.
- `ticket_board_schema_test`: line 931.
- `ticket_board_park_blocked_postgres_test`: line 177, "applies last".

The write-API suite's legacy report cases run *after* its unrelated failure,
so they never execute in this pane on either tree. `exercise_postgres_backend`
— the half that holds every legacy report-boundary case — was therefore run on
its own on both trees: **ok on both**.

## Not covered here

- `file_report` has always inserted into a stage literally named `analysis`,
  and the declared-workflow validator does not require one. `create_ticket` and
  others share that assumption. It is pre-existing and unchanged here.
  `examples/workflows/inspection.json` has the stage; MEFP's live document was
  not inspected.
- Live acceptance, per the ticket: verify together with SYRD-238's candidate
  from a restarted MEFP Director pane after this board release is deployed.
