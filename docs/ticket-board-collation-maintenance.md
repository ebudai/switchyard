# Ticket Board Collation Maintenance

PostgreSQL warns when a database was created with an older operating-system
collation version than the one currently provided by libc. For the ticket board,
this matters because btree indexes cover text-heavy lookup paths such as ticket
ids, states, assignees, and comments.

Run the repair only during a coordinated maintenance window. `REINDEX DATABASE`
takes locks.

Preflight observed for PGU-600 on 2026-08-22:

```text
pgu      recorded 2.43, actual 2.44
postgres recorded 2.43, actual 2.44
pgu ticket count: 574
```

Dry-run the exact SQL:

```bash
scripts/ticket-board-refresh-collation-version --plan-only
```

Execute during the window:

```bash
sudo -u postgres scripts/ticket-board-refresh-collation-version --execute --expected-ticket-count 574
```

Afterwards, a fresh `psql` connection to both `postgres` and `pgu` should no
longer print the collation-version warning, and the `pgu` ticket count should
still match the preflight count.
