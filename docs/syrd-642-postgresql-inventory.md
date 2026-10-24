# SYRD-642 slice 1: the board's PostgreSQL dependencies

This is a read-only inventory of main at `d24a15cf`. Counts are grep-based
(matching lines, not parsed SQL), so treat them as accurate to within a few.
`S` is `scripts/ticket_board/schema.sql` (19,024 lines).

## 1. Logic that lives in the database

### Size

- **Functions:** 343 unique names, about 356 signatures. Of these:
  - 228 are PL/pgSQL and 115 are SQL;
  - 226 are `SECURITY DEFINER`;
  - about 245 pin their `search_path`.
- **Redefinitions:** 32 names are defined more than once, and the last
  definition wins.
- **Overloads:** `create_ticket` has 6, `file_bug` has 4.
- **Tables and views:** 55 tables, 35 indexes, no views.
- **Transaction wrapping:** `S` is wrapped in `BEGIN` from line 13 to line
  10755. The remaining ~8,270 lines (182 functions, 35 tables) are not.

### Triggers

There are 41 triggers: 12 `BEFORE` and 29 `AFTER`. One is statement-level and
the rest are row-level.

- 28 of them are on `tickets`. Their order relies on PostgreSQL firing triggers
  in alphabetical order (`tickets_a_…` through `tickets_zzzzzzz_…`).
- Row replacement: about 74 `NEW.x :=` assignments.
- Recursion guards: `pg_trigger_depth()`.
- Some triggers write back to their own table. For example, the `AFTER UPDATE`
  trigger on `tickets` calls `release_implementer_and_activate_next`, which
  updates `tickets`.

### The largest functions

| Function | Body lines |
|---|---|
| `enforce_declared_ticket_update` | 352 |
| `notify_unresolved_turn_end` | 339 |
| `validate_declared_workflow` | 298 |
| `note_role_stall` | 268 |
| `notify_idle_turn_end_nudges` | 263 |
| `enforce_ticket_workflow_update` | 254 |
| `notify_idle_stall_nudges` | 241 |
| `notify_due_nudges` | 214 |
| `note_provider_capacity` | 180 |
| `edit_fields` | 179 |

### Functions by domain

| Domain | Functions |
|---|---|
| Notifications: queue, nudges, awaiting-role, turn-end | 72 |
| Write verbs | 37 |
| The declared-workflow transition engine | 35 |
| Stalls, episodes, holds, capacity | 31 |
| RBAC, actor resolution, UI delegation, runtime registration | 30 |
| Context continuation | 23 |
| Serial focus and reservations | 21 |
| Publication and Publisher | 21 |
| Pull scheduling | 14 |
| Blockers | 13 |
| Reminders and snooze | 12 |
| Router | 11 |
| Utility | 8 |
| Size review | 8 |
| UAT | 7 |

### PostgreSQL-specific features in use

| Feature | Use in `S` |
|---|---|
| jsonb | 358 lines; `->`/`->>` about 980 times; `?` about 74 times; `?&`, `?|`, `@>`, `<@`; 485 `jsonb_*()` calls; a GIN index; jsonb `CHECK` constraints. `declared_workflow()` is called about 92 times |
| Arrays | `text[]` columns and parameters, `ANY()`, `unnest`, `array_agg`, about 179 `ARRAY[` |
| `ON CONFLICT` | 37 statements (24 `DO UPDATE`), two of them targeting partial indexes |
| Partial unique indexes | 11 |
| `CHECK` constraints | 127, about 20 of them regex (`~`) |
| Locks | `FOR UPDATE` 69, `SKIP LOCKED` 5 (inside data-modifying CTEs with `LIMIT 1`), `FOR SHARE NOWAIT`, advisory transaction locks 6, `LOCK TABLE` 2 |
| Transaction-local settings (`set_config` 55, `current_setting` 65) | Carry context from functions to triggers: `workflow_actor`, `workflow_action`, `router_ticket`, `publisher_ticket`, `merge_source`, `director_edit_target`, `force_move`, … Set externally: `caller_role`, `notification_source_role`, `project`, `ticket_prefix`, `privileged_job_reconciler` |
| `pg_notify` | 5 calls, on channel `ticket_board_state_transition` |
| "Written by my transaction" | `xmin = pg_current_xact_id()` 4 times; `txid_current()` matching in `ui_delegations` |
| Transactional DDL | `apply_declared_workflow` drops and adds constraints, and its dry-run previews depend on rollback |
| Empty string ≠ NULL | 35 `NOT NULL DEFAULT ''` columns, 107 `nullif(…,'')`, about 199 `''` comparisons |
| Other | `format()` 142, `string_agg` 21, `DISTINCT ON`, `LATERAL`, `WITH ORDINALITY`, `RETURNS TABLE`/`SETOF`, `clock_timestamp()`, catalog introspection, optional `pg_cron` |

## 2. Roles, and how the caller is identified

- **`rbac.sql`** creates:
  - 9 pane-role logins, none of which the code ever uses to connect;
  - `ticket_board_service` and `ticket_board_listener`.

  It issues 165 `GRANT EXECUTE` statements: 103 to the service and 71 to the
  listener.
- **`require_actor`** checks only that the session user is
  `ticket_board_service`. It then trusts the `ticket_board.caller_role`
  setting, which any session can set.
- **`require_ticket_board_listener`** checks that the session or role is the
  listener.
- **Connections:**
  - Service: `postgresql:///<db>?host=/var/run/postgresql&user=ticket_board_service`, with peer authentication mapping OS user `boardsvc` to `ticket_board_service` (`ticket-board-boardsvc-setup.sh`).
  - Listener: runs as the project owner with `PGUSER=ticket_board_listener`.
- **Finding: a database login carries board authority on its own.** Anyone who
  can log in as the service can act as any role, the Director included. The
  listener login can claim and ack notifications and run the Publisher's
  functions. Today the only barrier is local peer authentication. A remote
  database turns these credentials into network secrets with that authority,
  so their custody (readable only by the board's service account), TLS, and
  the absence of any other way to reach the board are part of the Oracle
  design (see the contract).

## 3. Migrations

- **Runner:** `scripts/ticket-board-migrate`, bash plus psql.
  - Records applied migrations in `ticket_board.schema_migrations(name, seq, applied_at)`.
  - Applies and records each file in separate psql calls, so the two steps are
    not atomic.
- **Files:** 177 migrations, about 50,000 lines. 89 wrap `BEGIN/COMMIT`; 88 do
  not.
- **Ordering:** `migration_order.py`.
- **Fresh install:** applies `schema.sql`, then replays every migration on top.
- **Rollback:** never reverses a migration.
- **Drift guard:** `tests/schema_function_drift.py` keeps `schema.sql` and the
  migrations in step.

## 4. Python data access

- **psycopg users:**
  - `app.py`: `_pg_connect`, with `dict_row` and transaction-local settings;
  - `notify_listener.py`: `LISTEN`, `conn.notifies()`;
  - `workflow_locking.py`: the psycopg error classes;
  - `dump_notification_trace.py`.
- **Call sites:** 153 `.execute(` calls across 35 modules. 191 of them call 140
  distinct `ticket_board.<fn>()` routines. About 67 read tables directly, and
  11 write tables directly (the workflow seed).
- **Clients:** `read_client` and `write_client` speak HTTP or Unix-socket HTTP
  only and never touch the database.

## 5. Notification wake-up

- **Durable queue:** `ticket_notification_queue`. Rows are written in the same
  transaction as the change that produced them. The listener claims one with
  `FOR UPDATE SKIP LOCKED`, and a claim older than 2 minutes can be claimed
  again. Ack, requeue and dead-letter are listener-only.
- **`pg_notify`:** only a wake hint. The listener waits at most
  `poll_seconds = 5` or until the next attempt is due, so delivery already
  survives a lost `NOTIFY`.
- **Scheduling:** every pass of the listener loop also runs the periodic
  producers: reminders, nudges, pull pickup, publisher, board stall.

## 6. Host lifecycle

| Area | Where |
|---|---|
| Prerequisites | `install-switchyard-prereqs`: postgresql and psycopg packages |
| Cluster | `ensure-postgres-cluster`: `initdb`, `systemctl enable --now postgresql`, `/var/run/postgresql` |
| `switchyard new` checks | `new_project_precheck.py`: the PostgreSQL service and socket; probes with `psql` |
| Provisioning | `provision_board_service.py` (units ordered after `postgresql.service`; database SQL); `provision_operator_packet.py` (`sudo -u postgres psql`); peer-auth setup |
| Deploy | `ticket-board-service.sh`: migrate, then rbac, then ensure roles. The health canary runs against the live database URL |
| Role add, VCS close role, pane rebind | Each runs `sudo -u postgres psql` |
| Teardown | `project_teardown.py`: `DROP DATABASE … WITH (FORCE)`. Roles are cluster-wide and never dropped |
| Backup and restore | None in the repository |

## 7. Tests

- **Fixture:** `tests/temporary_cluster.py` runs `initdb` and `postgres` on a
  private socket. `ticket_board_write_api_test` builds boards on top of it.
- **Suites:** 164 of 706 suites need a real PostgreSQL cluster. 122 apply
  `schema.sql` and 66 run `ticket-board-migrate`.

## 8. Process authority

- **Board side:**
  - The board's Unix socket reads `SO_PEERCRED` and is mode 0660.
  - `ProcessRoleAuthority` requires the project uid, a live pane process
    (pid plus start time), and a row in `role_runtime_assignments`.
  - Over HTTP a write token is required, and the caller is the User.
- **Database side:** this authority is kept only as long as nothing but the
  board process can reach the database as the service. See the finding in §2.

## Features without a direct Oracle equivalent

| PostgreSQL feature | Oracle situation |
|---|---|
| `LISTEN/NOTIFY` | `DBMS_ALERT` or AQ exist; the queue plus polling already works without push |
| Transaction-local `set_config` | Oracle contexts and package state are session-scoped and are not reset at commit |
| Partial unique indexes and `ON CONFLICT` | Function-based unique indexes with `CASE … NULL`, and `MERGE` |
| jsonb operators | `JSON_EXISTS`, `JSON_VALUE`, `JSON_TRANSFORM` |
| Arrays | Collections or JSON |
| `SKIP LOCKED` with `LIMIT` in data-modifying CTEs | Oracle cannot combine `FOR UPDATE` with row limiting. Fetch one row from a `SKIP LOCKED` cursor |
| Advisory transaction locks | `DBMS_LOCK` needs a grant and is not released at commit |
| Trigger order | Needs `FOLLOWS` |
| Triggers writing their own table | Raises ORA-04091 (mutating table) |
| `pg_trigger_depth` and `session_replication_role` | No equivalent |
| `xmin` / `txid_current` | No row-level transaction id |
| Transactional DDL | Oracle DDL commits implicitly |
| `''` and NULL | Oracle stores `''` as NULL |
| Overloading | Packages only |
| `DISTINCT ON`, `clock_timestamp()` vs `now()` | No direct equivalent |
| Host tooling | `psql`, `initdb`, peer `pg_hba`, socket DSNs |

Together these explain the Director's direction C for SYRD-642. A
**board-resident engine** in Python stores through a narrow, portable Oracle
schema (tables, unique keys, row locks, a durable queue), rather than porting
roughly 19,000 lines of PL/pgSQL. Domains then move behind the same boundary
for both backends, one ordered slice at a time.
