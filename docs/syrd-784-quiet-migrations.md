# SYRD-784: a fresh `switchyard new` prints no "already exists, skipping" notices

While creating test13, the User saw hundreds of lines like
`psql:<stdin>:45: NOTICE:  relation "director_rebinds" already exists, skipping`.

## Cause, measured

A new board is built in two psql steps, and both print these notices:

- **The packet applies `schema.sql`.** Its own `IF NOT EXISTS` steps say it 88 times on a fresh database.
- **`ticket-board-migrate` replays every migration over that schema.** On a fresh install that is 194 migrations
  (`schema.sql` records only itself in `ticket_board.schema_migrations`). Each `CREATE … IF NOT EXISTS` finds its
  object already made, and says so. A fresh install printed 327 notices there.

## What changed

The notices are dropped in two places: from the stderr of every psql the migration runner starts (`psql_migrate`),
and from each of the packet's psql steps (`postgres_sql_file_command`). Only notices matching
`NOTICE:  … already exists, skipping` or `NOTICE:  … does not exist, skipping` are dropped. That is PostgreSQL's own
wording for idempotent DDL, and the pattern is the same in both places.

- **Everything else is still shown:** every other notice, every WARNING and every ERROR. `ON_ERROR_STOP` still
  stops a run, and pipefail keeps psql's exit status through the filter.
- **Why not a lower `client_min_messages`:** that would also hide notices that report real conditions. The pg_cron
  installer in `schema.sql` reports "pg_cron … not installed", and two migrations (`pgu1002`, `pgu1020`) report a
  feature not added or a cron job not moved. Those are shown, on fresh installs and upgrades alike.
- **Why not record migrations as already applied:** `schema.sql` is the final shape, but migrations also carry
  backfills, grants and cron moves that it does not. Replaying them over it is how every board is built and tested.
- **The run's record:** `ticket-board-migrate` still names each migration it applies (the record the rollout journal
  keeps and the ordering tests read), and ends with one line: `applied N migration(s); M already recorded`.
- **Upgrades and deploys:** they run the same runner, so they lose only the same idempotent notices.

On a fresh install the runner now prints its per-file lines and the summary, and nothing from PostgreSQL. A host
whose pg_cron is available but not set up still says so.

## Tests

`tests/migrate_quiet_test.py` runs on a throwaway cluster:

- **A fresh board:**
  - `schema.sql` and `rbac.sql` go through the packet's own rendered psql step, and the real `ticket-board-migrate`
    runs over them;
  - no idempotent-DDL notice appears;
  - every applied migration is named, and the summary counts them;
  - the only notices left are the pg_cron installer's, which this cluster triggers;
  - a rerun says "applied 0", quietly.
- **A failing run, from a migrations directory of its own:**
  - a migration's own `RAISE NOTICE` and `RAISE WARNING` are shown;
  - an ERROR stops the run and is printed in full, after the migration it is in;
  - the failed migration is not recorded, and nothing after it runs;
  - the packet's step still fails when psql does.
- **One pattern:** the runner and the packet's step drop the same one.
