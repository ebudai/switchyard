# SYRD-668: a tenant's grants are the board's own

## What happened

pgu928, which added `director_edit`, ended with a loop. For each role in
`pg_roles` that is not a superuser and has `EXECUTE` on `force_move`, it granted:

- `EXECUTE` on `director_edit`, `signoff_fields` and `role_controls_project`;
- `SELECT, INSERT` on `ticket_field_audit`;
- `USAGE, SELECT` on `ticket_field_audit_id_seq`.

The loop meant to find the board's writer without spelling its name. In a
fresh provision, though, migrations run before `rbac.sql`. Every function still
carries PostgreSQL's default `PUBLIC` `EXECUTE` then, so every non-superuser role
in the cluster passed the test. pgu930, pgu935, pgu938, pgu939 and pgu960 found
their grantee with the same loop.

## Measured on main

Measured with the rendered packet's own database phase (SYRD-654's harness),
for two tenants, one after the other, in one cluster.

- **A host's second tenant differs from its first.** Only `ticket_field_audit`
  and its sequence differ. On the second, every role user (`director`, `user`,
  `ops`, `app`, `audit`, `inspector`, `perf`, `research`, `main`) holds
  `INSERT` on the audit and `USAGE` on its sequence. On the first, those role
  users did not exist yet when the migrations ran: `rbac.sql` creates them.
- **Every tenant, the first included, grants more:**
  - all 16 of PostgreSQL's predefined non-superuser roles hold `SELECT, INSERT`
    on the audit and `SELECT, USAGE` on its sequence, from pgu928;
  - they also hold `SELECT` on `publication_requests`, from pgu930;
  - `ticket_board_listener` holds `INSERT` on the audit.
- **`rbac.sql` never revoked any of it.** Its resets name only the board's own
  roles, and of the tables only the ones it lists.
- **No function grant survives** to anyone outside the board's roles.

Who can use these grants:

- A role user can, because it has `USAGE` on the board's schema. On a second
  tenant, Ops inserts a forged audit row directly.
- The listener can.
- Most predefined roles cannot: they have no `USAGE` on the schema, so a
  `pg_monitor` member is refused at the schema.
- `pg_read_all_data` is the exception. It implicitly uses every schema, so a
  read-only login in that role, such as a backup or monitoring account, inserts
  forged audit rows and draws their ids.

## Who needs what

`rbac.sql`'s header states the design: direct writes are revoked from every
non-owner role, and the writer reaches tables only through definer-rights
functions.

- **`ticket_field_audit`** is written only by `director_edit`, which runs with
  definer rights, in `schema.sql` and in pgu978. So no principal needs direct
  `INSERT`, nor `USAGE` on its sequence. That includes `ticket_board_service`,
  which `rbac.sql` used to grant both. Like every table, the audit is readable
  by the board's principals.
- **`signoff_fields` and `role_controls_project`** are called only inside
  definer-rights functions: `director_edit`, `withdraw_signoff`,
  `recover_stalled_ticket`, `require_publication_control`,
  `record_privileged_job` and `ui_delegation_control_role`. No Python calls them.
  Nobody needs `EXECUTE` on them.
- **`director_edit`** is the writer's: the API calls it, after the handler's
  capability check.
- **The board's database principals** are the nine role users, the service and
  the listener. Nothing in `rbac.sql` or any migration grants to any other role.
  Provisioning refuses any service or listener name but `ticket_board_service`
  and `ticket_board_listener`, so the writer can be named. pgu928's "cannot be
  spelled" did not hold.

## The fix

### `rbac.sql`

`rbac.sql` runs last on every provision and every deploy, so it sets the final
ACL.

- **The audit is reset for the principals before the read grants,** as
  `rbac.sql` already does for the tables it lists. The read grant then gives
  every principal `SELECT` on it, and the sequence gets nothing.
- **The writer gets `director_edit` only.** It no longer gets `signoff_fields`,
  `role_controls_project`, or any write on the audit.
- **It ends with a sweep.** On the schema and every relation and function in
  it, it revokes whatever any role holds that is neither the object's owner nor
  a board principal. This repairs an existing database: its leaked grants go on
  its next deploy. It is idempotent, and it never names a role that might not
  exist.

  The sweep runs last, after every grant `rbac.sql` makes, so its list of
  principals is the final word. At the top, a mistake in that list would have
  been undone by the grants that follow, and no test could see it.

### The migrations

- **pgu928, pgu930 and pgu935** grant `ticket_board_service` by name, when it
  exists, instead of whoever passes a privilege test that `PUBLIC` still
  passes. pgu928 grants only `director_edit`.
- **pgu938, pgu939 and pgu960** already granted the service by name just above
  their loops, so the loops are removed.

An applied migration does not run again, so these changes affect fresh
provisions only. Existing databases are repaired by `rbac.sql`.

## Tests

`tests/field_audit_grants_test.py` runs the packet's database phase as the
packet prints it. It provisions two tenants one after the other in one cluster,
then a tenant built by main's tree, which it upgrades the way a deploy does:
this tree's `ticket-board-migrate`, then its `rbac.sql`. Main runs from a git
archive.

- **Main:**
  - the second tenant has different grants from the first;
  - a role user holds the audit's `INSERT` and sequence `USAGE` on the second;
  - predefined roles hold grants on every tenant;
  - Ops, the listener and a `pg_read_all_data` login each insert an audit row.
- **This tree:**
  - both tenants have the same grants on every object;
  - no role outside the board's holds anything, after the migrations or after
    `rbac.sql`;
  - outside the SYRD-83 objects, every board principal holds exactly what it
    holds on main's first tenant;
  - the migrations grant the writer `director_edit` and no other SYRD-83
    object;
  - the audit is readable by the eleven principals and writable by none, its
    sequence and the two helpers belong to nobody, and `director_edit` is the
    writer's;
  - Ops, the Director, the listener, the `pg_read_all_data` login and the writer
    are all refused, at the table, sequence or function, for direct `INSERT`,
    `nextval` and the helpers, and every role but the writer for
    `director_edit`;
  - the Director's edit through the board's handler still writes its audit row.
- **The existing tenant:**
  - built by main, it carries the leaks, plus pgu928's function grants as it
    once made them and a stray schema grant;
  - upgraded, its grants equal a fresh tenant's, and stay so on a second run;
  - Ops, the listener and the `pg_read_all_data` login are refused.
