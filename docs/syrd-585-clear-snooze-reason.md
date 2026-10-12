# SYRD-585: clearing a reminder snooze applies what its preview showed

## What was there

A Director could preview ending a reminder snooze (SYRD-537), but the apply
failed inside the board:

```
column reference "reason" is ambiguous
LINE 2: ..., closed_by = who, closed_at = now_ts, close_reason = reason
CONTEXT: PL/pgSQL function clear_reminder_snooze(bigint,text[],text,boolean) line 57 at SQL statement
```

- **The cause:** `clear_reminder_snooze` held the supplied reason in a
  PL/pgSQL variable named `reason`. `reminder_snooze_batches` also has a
  `reason` column (why the snooze was set). The `UPDATE` that closes the batch
  names `reason`, and PL/pgSQL refuses a name that could be either.
- **When it hit:** that `UPDATE` runs only when a clear releases a batch's last
  snoozed ticket: a whole-batch clear, or naming the last ones. So no batch
  could ever be cleared whole. A clear that left a ticket snoozed applied
  normally.
- **Why tests missed it:** SYRD-537's test cleared one ticket of five, which
  never reaches the close.
- **What it wrote:** nothing. The error rolled back the whole call, including
  the members' release just before it.

## The change

The variable is renamed `clear_reason`, in `schema.sql` and in migration
`pgu993_syrd585_clear_snooze_reason.sql`, which carries the same function
text. The signature is unchanged, so no grant moves. The JSON answer still
calls it `reason`.

Renaming, rather than qualifying the column or deleting the variable, matters.
With the variable deleted, the statement would no longer be ambiguous: `reason`
would silently mean the batch's own column, and a whole-batch clear would
record the snooze's reason as its close reason. A mutant does exactly that, and
the test catches it.

Only `clear_reminder_snooze` had this collision. Every PL/pgSQL function that
touches the snooze tables was screened for a local variable named like one of
their columns. The only other hit is `snooze_reminders`'s `reason`, used in an
`INSERT ... VALUES`, where no column is in scope.

## Tests

`tests/reminder_snooze_clear_test.py`, 26 checks. It uses a board built the
production way (companion roles, `schema.sql`, the real migrate runner,
`rbac.sql`, a declared workflow), and the real write CLI against the real board
server over its Unix socket. There are three batches over eight tickets, and a
ninth as a control.

**Main, from a git archive in a child process:**
- the previews work, and clearing one ticket of three applies;
- the whole-batch clear, clearing the last tickets by name, and clearing a
  batch whose members all changed are each refused with the live error
  (`line 57`), writing nothing;
- a board installed from `schema.sql` alone fails the same way.

**This tree:**
- **Previews:** each lists exactly the tickets it would release and those it
  would leave, and every snooze row and the notification queue stay
  byte-identical.
- **Refusals:** each leaves every snooze row and the queue unchanged:
  - a blank reason, at the CLI and in the database;
  - a ticket not in the batch;
  - one ticket in the batch and one not (the good one is not released);
  - a batch that doesn't exist;
  - App;
  - a batch already cleared.
- **A failed apply:** a fault injected into the batch close undoes the members'
  release with it.
- **A whole batch:** the apply releases exactly what the preview listed. Each
  member records `cleared: <reason>` and the Director, and the batch closes as
  `cleared` with the supplied reason. Those tickets' reminders resume, and the
  other batches' stay snoozed.
- **One ticket of three:** it is released alone and the batch stays open.
  Naming the remaining two releases them and closes the batch with their
  reason.
- **Invalidation is untouched:** a change of stage, owner or candidate ends
  that member's snooze by itself, and the ticket says what changed. A batch
  whose members all changed (SYRD-113's) still clears whole.
- **A fresh board:** one installed from `schema.sql` alone clears a whole batch
  too.

## Evidence

- **Mutation:** ten mutants, run through `tests/bounded_run.py mutate`, all
  killed. Each kill was read on its failing assertion.
  - **The fix itself:** the close names the batch's own `reason` column, once
    in `schema.sql` (caught by the schema-only board, which closed with
    "overnight") and once in the migration; the migration renames nothing.
  - **The clear's own rules:** a named clear releases every member; a refused
    apply goes ahead; a preview writes; the batch closes with tickets still
    snoozed; the release records no reason; a blank reason passes.
  - **Invalidation:** a changed ticket stays snoozed, mutated in SYRD-537's
    migration, which is the definition a migrated board runs.
- **The sweep:** the 125 suites that touch the reminder snooze, `schema.sql`,
  `rbac.sql`, the migrate runner or the migrations. Each was run serially on
  this tree and on main f23e709, with provider stubs that refuse to start.
  - **Exit codes:** identical; 13 are red on both trees.
  - **Output:** the reds' normalised output is identical, apart from three
    migration listings that now name pgu993.
  - **SYRD-537's own suite:** `reminder_snooze_test` passes, including its
    check that a `schema.sql`-only board installs the same functions as a
    migrated one.
