# SYRD-275: the Director's narrated override never reached a live board

## What happened

MEFP's Director was recovering MEFP-5, which had been approved on a stale
Audit sign-off (SYRD-274). The declared `route` from director_review to audit
is not declared, and the documented last resort, `override-move`, was refused:

```
role director cannot call force_move   (require_actor, inside force_move)
```

The role holds every control capability (merge, set_blockers,
set_manually_controlled). `schema.sql` admits the control role, as SYRD-78
intended.

## Cause: SYRD-78 fixed schema.sql and shipped no migration

SYRD-78 (`7848339`) did two things in `schema.sql`:
- it taught `ticket_board.require_actor` that `force_move` / `override_move`
  belong to the role holding every control capability;
- it added `control_override_actions()`.

It shipped **no migration**. That matters because of how boards are built:

- **Provisioning** loads `schema.sql`, then runs `ticket-board-migrate`, then
  `rbac.sql`. `schema.sql` records only itself in `schema_migrations`, so the
  runner then applies **every** `pgu*` migration in order.
- **Upgrades** apply every migration not yet recorded.

Either way, **the newest *migration* copy of a function is what production
runs**, never `schema.sql`'s. For `require_actor` that copy is pgu921's, which
admits an action only as a declared capability. No document may declare
`force_move`, since that would be declaring its own bypass, so every live
board refused the Director. The suites load `schema.sql` alone, so they
tested a `require_actor` that no board had. SYRD-78's own test passed for
exactly that reason.

(The other half of SYRD-78, the `force_move` escape in
`enforce_declared_ticket_update`, did reach production, carried by later
migrations that redefine that function whole.)

## Reproduced

`tests/control_override_reaches_boards_test.py` builds boards the way
provisioning does:
- the provisioning companion's roles;
- `schema.sql`;
- the real `ticket-board-migrate` runner over the migrations directory;
- `rbac.sql`;
- a declared workflow.

It drives `override-move` through the real write client and HTTP server. On
the tree as it was before this change, checked out with `git archive`, the
Director is refused with the live error.

## The change

`pgu963_syrd275_control_override_reaches_boards.sql` installs `schema.sql`'s
`require_actor` and `control_override_actions()`. `schema.sql` held two
different `require_actor` bodies; both places now hold the running one, so
the drift guard compares what runs.

Nothing else changes:
- only the control role is admitted, and every other role is refused by the
  server **and** by the database before the operation's own checks run;
- `force_move` still may not move a sign-off;
- the undeclared `route` from director_review to audit is still refused;
- `force_move`'s own checks (state, assignee) still run.

## Guard: what the suites test is what production runs

The same test builds a `schema.sql`-only board and a provisioned one. It
compares **every** `ticket_board` function by `pg_get_functiondef`, ignoring
comments and whitespace. Two exceptions remain, named in the test with
reasons; anything else that diverges fails.

- **`submit_to_audit_without_commit` (reported, not changed).** Production
  runs pgu913's body, which authorizes a no-commit submission like any
  `submit_to_audit`, so any implementer may waive the commit on another's
  ticket. `schema.sql`'s, which the suites test, is owner-scoped. Tightening
  it changes who may waive a commit on every live board; that belongs to its
  own ticket and decision.
- **`remember_ticket_implementer_assignee()` (harmless).** pgu450 installs
  it; `schema.sql` dropped it; nothing calls it.

The file-level audit also flagged other functions (`register_role_runtime`,
`reset_notification_backoff_for_idle_roles`, `notify_unblocked_dependents`,
`ticket_kickback_target_assignee`). On real databases they differ only in
whitespace or comments.

## MEFP-5

MEFP-5 is not touched here. Its team asked Audit for a review separately.
Once this migration is deployed to MEFP, `override-move MEFP-5 --state audit
--assignee audit` is the sanctioned recovery. It moves the ticket without
granting or clearing any sign-off, so Audit's review and the Director's close
still follow.

## Evidence

- **`tests/control_override_reaches_boards_test.py`: 22 checks.**
  - **Before:** on the pre-change tree, the real runner applies the pgu
    migrations and the Director's `override-move` is refused with the live
    error. Nothing moves.
  - **After:** the runner applies pgu963. The undeclared `route` is still
    refused. Ops, Audit and Main are refused by the server, and Ops by the
    database directly, with nothing moving. The Director's override lands
    MEFP-5's shape in audit/audit, and an invalid state is still refused.
  - **Parity:** `require_actor` and `control_override_actions` are identical
    tested and in production. Every other function matches, except the two
    named ones.
- **Mutation: 3/3 killed.**
  - The migration without `require_actor` (SYRD-78's exact miss) reproduces
    the live error.
  - `force_move` dropped from `control_override_actions`.
  - The control-capability test replaced with "any active role": the
    database lets Ops override.
  - Not mutated: "any control capability" versus "all of them". The workflow
    validator never lets a non-control role hold one, so no valid document
    can tell the two apart.
- **Sweep.** 222 suites touching blockers, the listener, awaiting-role,
  `force_move` / `override_move` / `require_actor`, the migration runner,
  schema, RBAC, sign-offs, workflows or the skills were run serially under
  `env -i`, against `origin/main` (`a8a3e06`). Pass/fail is identical for all
  222. The 57 red on both trees end on the same line, apart from temp paths
  and the migration-order checks already red on main, which now also list
  pgu963.
