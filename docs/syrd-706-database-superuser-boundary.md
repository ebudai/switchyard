# SYRD-706: no tenant account is the cluster superuser

## What was found

From Ops' own account, `psql "postgresql:///<db>?host=/var/run/postgresql&user=postgres"` answered `postgres`, with no password, for every tenant's database. I read the cluster's effective configuration through that same connection, using read-only SELECTs (2026-10-30, PostgreSQL 18.6).

**The hole.** `pg_hba` lines 119–128 were `initdb`'s defaults:
- `local all all trust`;
- `host all all 127.0.0.1/32` and `::1/128 trust`;
- replication `trust`.

Every connection that no earlier rule matched was trusted, as whatever role it named. The earlier rules map only `boardsvc` to `ticket_board_service`, per tenant database. The one for `pgu` (line 130) came after the catch-all, so it was never reached.

**The cause in code.** `scripts/ensure-postgres-cluster` (and the manual remedy in `docs/fresh-machine-install.md`) ran `initdb` with no authentication method. Its default is `trust`.

**What relied on it:**
1. **Every deploy.** `deploy-release` handed migrations and `rbac.sql` to the tenant's own `deploy-restart`. That ran `ticket-board-migrate` and `psql … user=postgres` as the tenant, from the tenant's own release. `ticket-board-service.sh install|deploy|ensure-*` and the listener script's `ensure-*` did the same.
2. **Every tenant's notify listener.** It is a user unit of the tenant's owner, connecting as `ticket_board_listener` with no password and no mapping. Seven were connected.
3. **PGU's pg_cron.** With `cron.host=localhost` and background workers off, its two jobs (`notify_due_nudges` every five minutes, and `prune_notification_trace`) connected as `postgres` over TCP 127.0.0.1.

**What did not rely on it.** Provisioning, `add-role`, `set-vcs-close-role`, `pane_rebind` and the collation refresh already run as root through `sudo -u postgres`.

## The change

**Root does the superuser work** (`scripts/ticket_board/database_admin.py`).
- The release's `ticket-board-migrate` and then its `rbac.sql` run as the cluster's own account: `runuser -u postgres -- env -i PATH=/usr/bin:/bin LANG=C.UTF-8 …`, from `cwd=/`. The URL is the socket with no `user=`, so peer authentication decides.
- They run only from root's trusted exact release of the commit being deployed (`trusted_release_root`), never from the tree the deploy was handed.
- The runner, `rbac.sql`, the migrations directory and every migration must be root's and writable by no one else. A link is refused.
- The database is named by the board unit root renders from its staged provisioning, and must be a plain name.

`deploy_release_in_transaction` does this when it is given the database, then passes `TICKET_BOARD_SKIP_MIGRATIONS=1` to the tenant's `deploy-restart`; every release since 2026-07 honours that flag. Both callers pass the database: `deploy-release` and the identity cutover. That covers deploying a release and rolling back to an older one; an older release's migrations are run by root the same way. A failed migration stops before the RBAC, and the deploy is refused: no tenant deploy runs, the units come back and the listener is restarted. Provisioning was already root's, with the skip flag, and is unchanged.

**The tenant scripts** keep their admin step for hosts that have not moved. When that step fails, the error now names root's `deploy-release` instead of advising an override of `TICKET_BOARD_ADMIN_DATABASE_URL`.

**Each listener gets its own mapping, on its own database** (`ticket-board-boardsvc-setup.sh --apply-peer-auth`):
- `local <db> ticket_board_listener peer map=<project>_ticket_board_listener`;
- `<project>_ticket_board_listener <owner> ticket_board_listener`.

The map is the tenant's own (`project_provision.listener_ident_map`), never shared: a shared map would let one owner reach another tenant's board as its listener. Root and `postgres` are refused as owners. Both callers, `switchyard new` and the operator packet, pass the owner and the map.

**pg_cron connects through the socket** (migration `pgu1020_syrd706_cron_over_the_socket.sql`; `schema.sql` ends with the same lines).
- `cron_over_the_socket` moves each board job's `nodename` to the server's first socket directory (`local_socket_directory`). Over the socket, peer authentication admits the server's own `postgres` account and no other, so no loopback rule is needed.
- Only `schema.sql` schedules the jobs, so a fresh install or a re-applied schema moves them again right after scheduling them.
- The migration does not redefine the `install_*_cron` functions. One of them names `prune_notification_trace`, which no migration installs, and the migration floor (`ticket_board_migration_function_floor_test`) rightly refuses a migration that depends on a function only a fresh install has. Nothing calls those functions on an upgraded board.
- A cluster without pg_cron is left alone.

**New clusters trust nothing.** `ensure-postgres-cluster` and the install doc run `initdb --auth-local=peer --auth-host=scram-sha-256`.

**The host transition**, root's, behind two catalogued `auth_admin` actions (the User authenticates for each; no pane pre-authorizes either):
- `switchyard privileged-action <project> preview-database-auth` runs `switchyard database-auth preview`. It reads the files the cluster names (`SHOW hba_file`, `SHOW ident_file`), the tenants root's provision records name (owner and database, cross-checked against root's registry), every board database the cluster holds, and the pg_cron jobs still on TCP. `database_auth` renders the replacement: `trust` becomes `peer` on the socket and `scram-sha-256` over TCP, each changed line marked; each tenant's listener rule goes before the catch-all, with its ident line. A line it cannot render with certainty (a quoted field, `trust` twice, an unknown type) is a refusal. The preview prints both diffs, the jobs moved, every probe, the backup and the way back, and a digest covering all of it, the current files included. A board database no root record accounts for is a refusal.
- `switchyard privileged-action <project> harden-database-auth digest=<d>` runs `switchyard database-auth apply <d>`, in this order:
  1. It recomputes the plan and refuses unless the digest matches.
  2. It backs up both files and the jobs' hosts under `/var/lib/switchyard/database-auth/<time>/` (0700).
  3. It writes each file atomically, with the owner and mode it had.
  4. It has the cluster read the result (`pg_hba_file_rules`): any trust or any unreadable line puts the files back, and nothing is reloaded.
  5. It moves the jobs and calls `pg_reload_conf()`, never a restart.
  6. It probes as each account:
     - `postgres` connects;
     - each owner is its own listener;
     - each owner is refused as `postgres`, as the board service, on another tenant's board, and as `postgres` over TCP without a password;
     - `boardsvc` still reaches each board.

     A refusal counts only when the server refused by authentication, not when the connection never happened.
  7. Any failure restores the files and the jobs and reloads.
- The way back is `sudo switchyard database-auth restore <backup>`, root only, and only from a directory directly under the backup root.

## Not done here

- **The live transition.** No live cluster change is made by this candidate. The User runs the preview after Audit, accepts its exact text, and applies it by digest.
- **PGU's `pgu` database** has no root record (no provision directory, no registry entry), so today's preview refuses: its listener, connected as `ticket_board_listener`, would be cut off. The order is this candidate, then PGU's cutover (SYRD-705) giving `pgu` its records, then the hardening. `test6_ticket_board` has a provision record but no registry entry. It is accounted for by its provision record, unless that record does not name its owner, in which case the preview names it.
- **Legacy pane-role logins.** `rbac.sql` creates `director`, `user`, `ops`, `app`, `audit`, `inspector`, `perf`, `research` and `main` as `LOGIN` roles. On this host `app`, `audit`, `director`, `eric`, `main`, `ops`, `perf` and `research` also have passwords, set outside this repository. No Switchyard path connects as any of them: there is no `user=` or `PGUSER=` naming them, no `PGPASSWORD` and no `.pgpass` anywhere in `scripts/`, `config/` or `examples/`. After the transition:
  - over the socket, a peer login is possible only for an OS account of the same name, and of these only `eric` (the desktop owner, not a superuser) has one;
  - over TCP, a password login is possible, by SCRAM;
  - no rule trusts them.

  Removing their `LOGIN` and passwords is a follow-up decision for the Director, not part of this emergency cutover.

## Tests

**`tests/database_superuser_boundary_test.py`** runs main first, from a git archive, then this tree:
- **Bootstrap:** the `initdb` that `ensure-postgres-cluster` runs on an empty Arch host (stubbed).
- **Root's database work:** recorded commands, refused on a bad name, an untrusted release, a writable or linked migration, and a failed migration before the RBAC. Also the unit's database name.
- **Peer setup and its callers:** the stubbed-root `--apply-peer-auth` lines, root refused as an owner, and both callers.
- **A real cluster:**
  - while trusting, this account is `postgres` and any tenant's listener;
  - hardened as rendered, the cluster's own reading has no trust and no error;
  - after a reload, the owner is refused as `postgres`, as the board service and on the other tenant's database, and is its own listener;
  - the cluster's own account is still its superuser;
  - refusals are tested for quoted fields, `trust` twice, a `postgres` owner, and unreadable rules.
- **A real cluster with pg_cron preloaded:** the jobs a fresh board schedules connect through the socket. So do main's board's jobs, once upgraded by this migration, and so do the jobs after the tree's schema is applied again.
- `schema.sql`'s last definitions are the migration's, byte for byte.

**`tests/database_auth_transition_test.py`** runs main first (no such actions), then drives the transition against a host whose probes are decided by the files last reloaded, by first-match rules, peer and ident maps:
- the catalogue and operations;
- the exact preview, and its stable digest;
- an apply in order: rules read, jobs moved, one reload, 13 probes;
- the backup, the way back, and restore;
- refusals for a changed file, an unaccounted database, disagreeing records, and an included file that still trusts;
- a probe that connects or is refused for the wrong reason, which restores everything;
- no pg_cron, root only, and backups only.

**Existing suites updated:**
- `catalogued_deploy_release_test`: root's `migrate as postgres` and `rbac as postgres` from the trusted release; the skip flag on the tenant deploy; and a database failure that blocks the deploy.
- `deploy_release_board_readiness_test`;
- `tenant_release_report_boundary_test`, `switchyard_commands_boundary_test`, `switchyard_dispatch_boundary_test`, `switchyard_parsers_boundary_test` and `privileged_front_door_test`: their structural pins gain the new import, command, branch and catalogue entries;
- `ticket_board_project_provision_test`, `provision_operator_packet_boundary_test` and `provision_role_tooling_boundary_test`: the peer-auth command, its seam reads and the staged policy's digest;
- `README.md`'s help block names `database-auth`;
- `temporary_cluster` takes server settings.
