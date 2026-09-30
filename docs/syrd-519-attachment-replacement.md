# SYRD-519: replacing a ticket's screenshots works when it already has some

## What happened

While working on SYRD-518, Main found that `ticket_board.edit_fields` raised
the following whenever a ticket that already had attachments was given a new
screenshot list:
```
duplicate key value violates unique constraint "ticket_attachments_pkey"
Key (ticket_id, position)=(<id>, 0) already exists
```

## Reproduced on main a13b5c1

The boards were built the production way: companion roles, `schema.sql`, the
real `ticket-board-migrate`, and `rbac.sql`. One board was legacy and one had
a declared workflow. Edits went through the real `TicketBoardApp.update_ticket`.

| ticket | legacy | declared |
|---|---|---|
| empty, given `[a, b]` | ok | ok |
| populated, given a new list | **pkey violation** | **pkey violation** |
| populated, given exactly its own list | **pkey violation** | **pkey violation** |
| populated, cleared to `[]` | ok | ok |

In every failure the earlier rows were untouched, because the transaction
rolled back.

## Cause

The copy of `edit_fields` that production runs is the one in the newest
migration, `pgu770_restrict_commit_hash_edit_fields.sql`; the `schema.sql`
copy is identical. Its replacement step was one statement:
```
WITH existing AS (SELECT …), deleted AS (DELETE …), requested AS (…)
INSERT … SELECT … FROM requested LEFT JOIN existing …
```
PostgreSQL runs an unreferenced data-modifying CTE (`deleted`) to completion
after the main query. So the `INSERT` still met the old
`(ticket_id, position)` rows. An empty ticket had nothing to collide with,
and `[]` inserted nothing, which is why only those cases worked.

## The change

New migration `pgu967_syrd519_replace_attachments.sql`, plus the same edit in
`schema.sql`, which keeps the two copies identical. `edit_fields` is otherwise
exactly pgu770's. The replacement is now three statements:
1. read the existing `path → metadata` into a variable;
2. `DELETE` the ticket's attachments;
3. `INSERT` the requested list, each path keeping its metadata.

**Still true:**
- **Atomicity.** The three statements are one function call inside the
  caller's transaction. If a later step of the update is refused, or the
  insert itself fails, the delete is undone too; the tests show both.
- **Role checks.** `require_actor` and the declared caller-role check run
  first, as before.
- **Ordering and metadata.**
  - Positions are contiguous from 0 in request order.
  - Only position 0 is primary.
  - `source_field` stays `screenshots`.
  - A kept attachment carries its metadata (such as a crop) to its new
    position; a new one starts with `{}`.
  - The ticket's `screenshot` is the new first.

**Unchanged:** the Python path, materialization, and both board kinds.

## Not changed here: SYRD-520

This fix is SQL only. Files materialized before a refused update are still
left in the asset directory. That was measured with the same builder: after
a refused update, the database kept its one row, and the asset directory went
from 1 file to 2, on both boards. A screenshot dropped by a replacement also
stays on disk. That file cleanup is SYRD-520's, with its own evidence.

## Evidence

- **`tests/attachment_replacement_test.py`.** It runs every case on both
  boards. The code and SQL from a13b5c1 run in a child process to reproduce
  the collisions, and this tree shows the fix.
- **Mutation, with `tests/bounded_run.py mutate` on the migration: 8 of 8
  killed.** Each kill was confirmed from its assertion line. The mutants
  cover:
  - the old sibling-CTE form;
  - no delete;
  - metadata not carried, or read after the delete;
  - every attachment primary;
  - positions starting at 1;
  - an empty migration;
  - the ticket's `screenshot` not refreshed.
- **Sweep against a13b5c1.** 74 suites were run under `env -i`: every suite
  naming screenshots, `edit_fields`, attachments or migrations, minus eight
  that drive the launcher, ACLs or real home paths. 48 pass on both trees and
  26 fail on both.
  - **Separable cases.** The three failing suites that have them fail the
    same cases on both trees.
  - **Browser suites.** Most of the rest could not start a browser: this
    host's Playwright wants build 1243 and has 1234 cached. I launched them
    through a wrapper that points `launch()` at the cached headless shell,
    the same way on both trees. Then 17 pass on both, including
    `attachment_crop`, `attachment_lightbox`, `file_attach_frontend` and
    `director_edit`.
  - **The remaining six fail at the same point on both trees.**
    `ticket_board_ui_audit_test` stops on the SYRD-285 blocker rule.
  - **`ticket_board_write_api_test`'s screenshot cases.** They sit behind a
    step that already fails on both trees. With only that step skipped, they
    print `ok` on both.
  - **`ticket_board_park_blocked_postgres_test`.** Its "it applies last"
    case was already red, and now also lists this migration.
- **Rebased onto 9acae3e (SYRD-476).**
  - **Migration number.** That commit took pgu965, and the Director keeps
    pgu966 for Main's SYRD-514, so this migration is pgu967.
  - **Composition.** A fresh `schema.sql` board and a production-migrated
    board install byte-identical `edit_fields` (the fixed form), and both
    keep SYRD-476's `serial_reservations()`. The runner applies pgu965 and
    then pgu967.
  - **Re-checked on the rebased tree.** This suite passes both under `env -i`
    and in the pane, and all 8 mutants are still killed. SYRD-476's
    `serial_reservation_status_test`, `lifecycle_reservation_test` and
    `publication_upgrade_history_test` pass on both 9acae3e and this tree.
  - **Re-sweep of the 74 suites against 9acae3e.** 45 pass on both trees
    and 29 fail identically on both. Three suites that had passed on a13b5c1
    (`external_blocker_test`, `operator_wait_test` and
    `signoff_follows_commit_test`) now fail on 9acae3e itself, with
    `function ticket_board.serial_reservations() does not exist` from
    SYRD-476's `rbac.sql` grant. That is not from this change; it is
    reported on the ticket.
