# Oracle as a remote ticket-board backend (SYRD-642)

A board can keep its tickets in a remote Oracle database instead of the local
PostgreSQL cluster. This is the first, deliberately limited slice: the
**Oracle core** (direction C). It is runnable end to end:
* the board server, the local write socket, the pane clients and notification
  delivery all work against Oracle;
* PostgreSQL boards are unchanged.

What the core does not do yet is listed under [Parity boundary](#parity-boundary).
Each gap is an ordered child ticket of SYRD-642. Every later slice moves one
domain behind a backend-neutral boundary for both backends (direction B).

The PostgreSQL coupling this starts from is inventoried in
`docs/syrd-642-postgresql-inventory.md`.

## Shape

```
pane clients ── Unix socket (peer uid / registered pane process) ──┐
browser ─────── HTTP (127.0.0.1) ─────────────────────────────────┤
                                                                   ▼
                    TicketBoardServer  (unchanged: authority, tokens, routes)
                                                                   │
                       TICKET_BOARD_BACKEND=oracle ─► OracleBoardApp
                                                                   │  workflow_engine.decide()
                                                                   ▼
                        remote Oracle (thin TCP / TCPS), runtime account: rows only
                                                                   ▲
                     oracle_listener ── polls notification_queue ──┘── directorctl ─► role pane
```

| Module | What it is |
| --- | --- |
| `scripts/ticket_board/workflow_engine.py` | The declared-transition rules in Python, read from the same workflow document, using the Python mirrors `workflow_config` already shares with the SQL. This is the first piece of direction B. |
| `scripts/ticket_board/oracle_config.py` | Backend selection, endpoint configuration, credentials files and connecting. |
| `scripts/ticket_board/oracle_schema.py` | The board's Oracle schema as ordered migrations, applied step by step and recorded, and the runtime grants. |
| `scripts/ticket_board/oracle_app.py` | `OracleBoardApp`: the board operations the core supports, each in one transaction. |
| `scripts/ticket_board/oracle_listener.py` | Notification delivery: the durable queue, polled. |
| `scripts/ticket_board/oracle_lifecycle.py`, `scripts/ticket-board-oracle` | `configure`, `check`, `status`, `migrate`, `teardown`. |

## Choosing the backend

| Variable | Meaning |
| --- | --- |
| `TICKET_BOARD_BACKEND` | `oracle` selects Oracle. Unset (or `postgres`) is PostgreSQL, as before. Any other value is refused. |
| `TICKET_BOARD_ORACLE_CONFIG` | The absolute path of the board's Oracle configuration file. |

The board server (`scripts/ticket-board.py`) and the listener entry point
(`scripts/ticket-board-notify-listener`) both read these. With Oracle selected:
* `--database` and every `PG*` setting are ignored;
* no PostgreSQL connection is opened;
* PostgreSQL's driver is never imported by the server, its reads and writes, or
  queue delivery.

The listener's pane activity gate is the one exception: it still reaches its
helpers through `notify_listener`, which imports the `psycopg` library (a
declared board requirement) but never connects. Removing that is part of the
listener slice.

The core needs `python-oracledb`. It runs in thin mode, so no Oracle client
libraries are installed on the host.

## Installing a host for Oracle

`./install` asks first, before planning or installing anything, which databases
this host's ticket boards use: PostgreSQL, Oracle or both. The details are in
`docs/fresh-machine-install.md`. In short:
* **Oracle alone** installs only `python-oracledb`, into the shared venv
  `/opt/switchyard/venv`.
  * No PostgreSQL server, client or psycopg is installed.
  * No cluster is created.
  * Nothing of `postgresql.service` is enabled, started or probed.
  * No local socket is needed.
* **Answering without a terminal:** `--databases postgresql|oracle|both`, or
  `SWITCHYARD_INSTALL_DATABASES`.
* **With neither a terminal nor an answer,** a host keeps what it recorded
  (`/etc/switchyard/databases.json`), or gets PostgreSQL if it has never
  recorded a choice.
* **`sudo ./install --add-database postgresql`** adds PostgreSQL later without
  reinstalling Switchyard.
* **`switchyard new`** creates the board on whichever database the host
  supports; see [Creating an Oracle project](#creating-an-oracle-project). A
  PostgreSQL board on a host without PostgreSQL is refused before anything is
  asked, naming that command.
* **`ticket-board-oracle`** runs itself under the shared venv when the system
  Python lacks `python-oracledb`.

The Oracle Database Free container in `tests/oracle/` is test infrastructure.
No production path names or uses it.

## Configuration

The configuration file holds the endpoint and nothing secret:

```json
{
  "host": "10.20.30.40",
  "port": 1522,
  "sid": "ORCL",
  "protocol": "tcps",
  "wallet_location": "/etc/switchyard/oracle/wallet",
  "wallet_password_file": "/etc/switchyard/oracle/wallet.password",
  "ssl_server_dn_match": true,
  "schema": "BOARD_OWNER",
  "runtime_credentials_file": "/etc/switchyard/oracle/runtime.credentials",
  "bootstrap_credentials_file": "/etc/switchyard/oracle/bootstrap.credentials"
}
```

* **`host`, `port`.** `host` is an IPv4 or IPv6 address or a host name. Nothing
  assumes the database is local.
* **Exactly one of `sid` or `service_name`.** Both, or neither, is refused.
  * A SID goes to the driver's SID field: `(CONNECT_DATA=(SID=…))`.
  * A service name goes to its service-name field.
  * Neither is ever rewritten as the other, or looked up as a TNS alias. A
    database known only by SID is reached by SID. A service name given as a SID
    is refused by the listener (DPY-6003, like ORA-12505), not quietly used.
* **`protocol`** is `tcp` (the default) or `tcps`. Wallet and certificate fields
  are accepted with `tcps` only.
* **`schema`** owns the board's tables. Every connection sets `CURRENT_SCHEMA`
  to it and the session time zone to UTC.
* **Unknown fields are refused,** and so is a `dsn`. A `user` or `password` in
  this file is refused with the reason: those belong in the credentials files.

**Credentials files.** Each holds `{"user": "...", "password": "..."}` for one
account:

| Account | Used by | Needs |
| --- | --- | --- |
| runtime | the board server and the listener | `CREATE SESSION`, plus the table grants `migrate` gives it (`oracle_schema.RUNTIME_GRANTS`): rows only, never DDL, and `DELETE` only on the queue and blockers. |
| bootstrap (optional) | `ticket-board-oracle migrate` | It is the schema owner, with `CREATE SESSION`, `CREATE TABLE` and `CREATE SEQUENCE`. It must not be the runtime account. |

A credentials file, and the wallet password file, is read at connect time only,
and only when it is:
* a regular file, not a link;
* readable and writable by its owner alone (`mode & 0o077 == 0`);
* owned by the account reading it.
Anything else is refused, naming the file and never its contents. A host that
only runs the board needs no bootstrap file.

**No secret is shown.** Passwords and user names are never in the configuration,
in a command line, in the environment, or in any output. `repr`, `status`,
`check`, logs and errors name the endpoint and the credentials files only. The
schema name is shown, because it is part of the endpoint configuration.

**Connecting is bounded:**
* each attempt times out after 10 seconds;
* a failed connect is retried twice, a second apart;
* a statement is never retried, so a write is never applied twice;
* the board server borrows sessions from a pool of at most 10, waiting at most
  10 seconds for one. Each session is pinged as it is borrowed, so one an
  outage broke is replaced rather than failing the next request;
* the listener reconnects after a lost connection;
* a board therefore recovers by itself once the database is reachable again;
* a changed password takes effect when the board restarts.
During an outage a write fails with the driver's own message, and nothing is
half-written.

## Operating

```bash
ticket-board-oracle configure --output /etc/switchyard/oracle/board.json
ticket-board-oracle check     --config /etc/switchyard/oracle/board.json
ticket-board-oracle migrate   --config /etc/switchyard/oracle/board.json --project syrd --ticket-prefix SYRD
ticket-board-oracle status    --config /etc/switchyard/oracle/board.json [--json]
ticket-board-oracle teardown  --config /etc/switchyard/oracle/board.json [--execute]
```

* **`configure`** writes the configuration (mode 0600, in a 0700 directory)
  from its flags.
  * In a terminal it asks for what is missing. It asks whether the database is
    identified by a **service name or a SID**, and only then for the value.
    Passwords are read at a no-echo prompt, and the credentials files are
    written 0600.
  * Without a terminal it refuses rather than guess: for example
    `--sid or --service-name is required`.
  * `--sid` and `--service-name` together are refused.
  * An existing file is replaced only with `--force`.
* **`check`** only reads. In order, it reports:
  1. TCP reachability, before any login (`cannot reach host:port: …`);
  2. the transport: plaintext to another host fails unless `--allow-plaintext`
     is given;
  3. for each configured account:
     * the login, with diagnostics for an unknown SID, an unknown service
       (each with a hint to the other identifier), refused credentials naming
       the file, and a locked account;
     * whether it is over-privileged, which would let a stolen board credential
       do more;
     * any missing grants;
     * pending migrations.

  The exit status is 0 only when every finding passes.
* **`status`** only reads. It shows the configuration (without secrets), the
  migrations applied and pending, the ticket count, the queue depth and the
  oldest due notice, and the number of registered roles.
* **`migrate`** runs as the bootstrap account and applies pending steps. See
  [Schema and migrations](#schema-and-migrations).
* **`teardown`** lists, and with `--execute` removes, this board's local files:
  the configuration and the credentials files.
  * It connects to nothing.
  * It never stops, deletes or administers the database, the schema, its rows
    or its accounts. That is the DBA's.
  * It leaves a wallet in place, because Switchyard did not create it.
  * A path that is a link, or not a regular file owned by this account, is
    refused and left in place.

**Running a board on Oracle:** `switchyard new` does it. The next section
says how.

## Creating an Oracle project

**Choosing the database.** `switchyard new` decides the project's board
database first, before any other question:
* `SWITCHYARD_NEW_DATABASE=postgresql|oracle` decides it;
* on a host installed for both, a terminal is asked
  "1) PostgreSQL on this host  2) Oracle, remote";
* with nobody to ask, the answer is PostgreSQL, as `switchyard new` always did;
* on a host installed for one, that one is used.

A database the host was not installed for is refused, naming the
`./install --add-database` command that adds it.

**For Oracle, before anything else is asked or created:**
1. **A declared workflow is required** (`--workflow-config`, or `--from` an
   artifact that declares one). The Oracle core runs declared workflows only.
2. **The connection is collected:**
   * from `SWITCHYARD_NEW_ORACLE_CONFIG`: a configuration made with
     `ticket-board-oracle configure`, naming bootstrap credentials;
   * or, in a terminal, by asking for it: host, port, service name or SID
     (asked, never guessed), tcps and wallet, schema, and both accounts'
     user names. Passwords are read with echo off.
   * `SWITCHYARD_ORACLE_ALLOW_PLAINTEXT=1` accepts plaintext tcp to another
     host.
3. **It is staged** in a root-only directory under `/etc/switchyard/oracle`.
4. **It is checked** with `ticket-board-oracle check --before-migrate`, in the
   shared venv:
   * reachability, transport, both logins;
   * the owner's privileges;
   * a runtime account holding nothing but a session.

   The grants and tables that `migrate` will make are reported, not failed. A
   failed check stops `switchyard new`, and nothing collected is kept.

**Root's record.** When the plan's artifacts are written, the staging becomes
root's record of the tenant, in `/etc/switchyard/oracle/<project>/` (root,
0755). The plan also records `database_backend: oracle`.

| File | Owner, mode | Used by |
|---|---|---|
| `endpoint.json` | root 0644 | the endpoint as collected |
| `runtime.credentials`, `bootstrap.credentials` | root 0600 | root: check and migrate |
| `bootstrap.json` | root 0644 | root's configuration, with both root credential files |
| `board.json`, `board-runtime.credentials` | root 0644; the board service account 0600 | the board server |
| `listener.json`, `listener-runtime.credentials` | root 0644; the tenant owner 0600 | the notification listener |
| `owner.json`, `owner-bootstrap.credentials` | root 0644; the tenant owner 0600 (optional) | the tenant's own board deploy, which migrates (SYRD-645) |
| `allow-plaintext` | root 0644, optional | the operator's choice above |

Each reader has its own copy of the runtime credential, because a credentials
file is read only by the account that owns it. The bootstrap credential is
root's alone.

**Provisioning:**
* **Units.** The board, listener and canary units are the PostgreSQL ones
  with their database lines replaced, and only those:
  * `TICKET_BOARD_BACKEND=oracle` and `TICKET_BOARD_ORACLE_CONFIG` set to
    that reader's configuration;
  * no `PG*` settings;
  * no database URL;
  * no ordering on `postgresql.service`.

  A template line the rewrite expects but cannot find stops the rendering.
* **The root packet.**
  * It runs no peer authentication, database SQL, `schema.sql`,
    `ticket-board-migrate`, workflow seed or `rbac.sql`.
  * It installs the two runtime-credential copies (0600, each owned by its
    reader).
  * As root, from the release, it then runs `ticket-board-oracle` check
    `--before-migrate`, `migrate` and `apply-workflow`. A second
    `apply-workflow` of the same document changes nothing.
* **Deploy and upgrade.** `ticket-board-service.sh` recognises root's record:
  * the canary runs on the record's configuration;
  * the migration step runs `ticket-board-oracle migrate`. `deploy-release`
    and `deploy-restart` run the board deploy as the tenant owner, which
    migrates with its own copies (`owner.json`); run as root, the deploy
    migrates with root's `bootstrap.json` (SYRD-645). This is the authority a
    PostgreSQL tenant's owner has over its own board database;
  * the PostgreSQL role step is skipped.
* **Keeping the schema owner's credential from the tenant** (SYRD-645). After
  creation an operator may remove the owner's copy:
  `sudo rm /etc/switchyard/oracle/<project>/owner-bootstrap.credentials`. The
  tenant's own deploys then migrate nothing. They read the schema with the
  owner's runtime copy, and refuse a release whose migration steps are not yet
  applied before that release goes live, printing the command root runs to
  apply them (with root's `bootstrap.json`); then deploy again. Removing root's
  `bootstrap.credentials` too keeps no schema-owner credential on the host at
  all; root's deploys then name the command the schema owner runs, with their
  own credentials.
* **Re-rendering.** An upgrade or resume that re-renders the artifacts keeps
  the tenant on Oracle, because root's record exists.

**Status.** `switchyard status` reports an Oracle tenant from root's records,
as any other, and never asks PostgreSQL. It adds one line per Oracle tenant
(and `board_databases` with `--json`), read-only and bounded, through
`ticket-board-oracle status` (SYRD-645):

```
switchyard: demo board database: oracle tcp://db.example:1521 SID ORCL: schema up to date; 3 tickets; 1 queued notices
```

It reads the database as the asking account may: root with its own copy, the
tenant owner with the listener's. Anyone else is told where the database is
and who can see its state. Pending migration steps and an unreachable database
are said on that line. The installed CLI is not on `PATH`; for the full picture,
as root:

```
sudo /opt/switchyard/current/scripts/ticket-board-oracle status --config /etc/switchyard/oracle/<project>/bootstrap.json
sudo /opt/switchyard/current/scripts/ticket-board-oracle check  --config /etc/switchyard/oracle/<project>/bootstrap.json
```

**Teardown.** `switchyard teardown` runs `ticket-board-oracle teardown
--execute` on each configuration in the record (the listener's, the board's,
root's), which removes it and its credentials files and connects to nothing.
It then removes the record `/etc/switchyard/oracle/<project>` (local files
only). The remote database, its schema and its rows are kept, and no ticket
count is needed, because nothing remote is dropped. As root, that teardown
removes a reader's credentials copy only from a directory that only root can
write.

**Not yet on an Oracle tenant:**
* adding a role, and setting the VCS close role, are refused, changing
  nothing (SYRD-645);
* role panes on a headless host fail the same way for every tenant
  (SYRD-661).

**Proven** in a fresh Arch VM (`./install --databases both`), with one release:
* the real `switchyard new` created a PostgreSQL tenant and an Oracle tenant
  (against the test service, by service name);
* both boards and listeners ran, the Oracle one migrated with its workflow
  applied;
* `switchyard status` reported both;
* `switchyard teardown` removed both: the PostgreSQL database was dropped, and
  the Oracle schema (12 tables, `board_meta`, workflow revision 1) and its
  accounts were kept, unchanged.

## Authority

A remote database does not authenticate callers and gains no say in who a
caller is.
* **Local write authority is unchanged.** It is the Unix socket's peer
  credentials, with either the uid → role map or a registered live pane
  process. The board server decides it before the database is consulted.
  `ProcessRoleAuthority` reads the runtime registrations from the Oracle board
  (`runtime_assignment_for_process`) exactly as it reads them from PostgreSQL.
* **A client claiming another role is refused by the server:** for example
  "this pane is registered as director, not audit".

The database network peer gets no board-operation authority. One limit is worth
stating plainly. Anyone holding the **runtime credential** can change the
board's rows directly, bypassing every rule, just as a PostgreSQL service login
can. So:
* keep its file readable only by the account the board service runs as;
* use `tcps` to any host but this one (`check` fails plaintext elsewhere). For
  a tenant, the wallet should hold only the certificate that verifies the
  server (its CA). That is public, so one copy readable by root, the board
  service account and the owner serves every reader. A wallet holding a client
  key (mutual TLS) would need a copy per reader, which tenant creation does not
  make: it is not supported;
* keep the runtime account to rows only (`check` fails one that holds more).

The bootstrap credential can change the schema itself. It belongs only where
migrations are run.

## Behaviour of the core

**Supported**, through the unchanged HTTP and socket routes and clients:
* creating tickets;
* reading tickets, the board and the board's change signature;
* comments;
* every declared transition (`workflow-action` and the named actions such as
  `route`, `submit-to-audit` and `audit-kick-back`);
* the Director's gates (`set-workflow-flags`);
* reading and applying the declared workflow (`configure_workflow`);
* runtime registration, for process authority;
* the pull claim (`claim-next`) on a pull-scheduled workflow.

**Consistency:**
* **A transition** is decided by `workflow_engine.decide()` with the ticket row
  locked (`SELECT … FOR UPDATE`). Its update, its comment and its notice are
  committed together.
* **The pull claim:**
  * locks the claimant's row in `implementer_locks` first. This stands in for
    PostgreSQL's per-implementer advisory lock, which a rows-only account
    cannot take;
  * then takes the oldest admitted ready ticket with
    `FOR UPDATE SKIP LOCKED`, fetching exactly one row.

  Twelve simultaneous claims by three workers take exactly three tickets, one
  each.
* **Refusals** answer as PostgreSQL's do: 403 when the actor may not take the
  action, 400 when a rule refuses it.

**Notifications:**
* A transition notifies its new owner, and a comment notifies the ticket's
  owner. Neither notifies the role that made the change, as PostgreSQL's queue
  trigger rules.
* Each notice is written to `notification_queue` in the change's own
  transaction. A dedupe key collapses a repeat into one refreshed notice.
* `oracle_listener` polls: at once after a delivery, every 5 seconds when idle,
  and a reconnect every 5 seconds after a failure. Each pass:
  1. claims the oldest due notice (`SKIP LOCKED`, one row);
  2. finds the target role's pane from its runtime registration, only while
     that registered process is still alive;
  3. waits while the pane is busy, using the PostgreSQL listener's
     `PaneActivityGate`;
  4. sends it through the same `directorctl` sender;
  5. deletes it on success.
* A notice is never lost. One that cannot be sent is put back with its reason.
  A claim held by a listener that died is taken again after 120 seconds.

### Schema and migrations

The schema is `oracle_schema.MIGRATIONS`, ordered, starting at `ora0001_core`.
Oracle commits each DDL statement on its own, so a migration cannot be one
transaction. Instead:
* **Each step is recorded** in `board_schema_migrations` as it completes.
* **Each step has a probe,** so a step whose DDL ran but was not recorded (a
  crash between the two) is recognised and recorded, never run twice.
* **A failing step stops the run.** It is reported with Oracle's own error and
  the count of steps in place. Nothing is rolled back or dropped, and the next
  `migrate` resumes at that step.
* **Concurrent runs are serialised** by a lock row held on a second connection.
* **Grants follow what exists:** an object a later migration adds is granted
  when that migration has run.

A schema left by any earlier set of migrations upgrades to the latest, and a
second run applies nothing.

## Parity boundary

What a PostgreSQL board does that this core does not, and what an Oracle board
does instead. Each is refused plainly ("… is not in the Oracle core yet" or
"… not yet supported on this backend; nothing was changed") **except where
marked *not applied*.** A *not applied* rule is absent: the Oracle board
behaves as if it were switched off, and it does not say so.

| Area | On an Oracle board now | Slice |
| --- | --- | --- |
| Blockers, dependencies, waits (`set-blockers`, `request-dependency`, `await-role`, external blockers) | refused; a ticket cannot be created blocked | SYRD-646 |
| **Serial focus**: one active ticket per implementer, the rest queued behind it | ***not applied***: an implementer may hold several active tickets | SYRD-647 |
| Pull-scheduled routing into implementation (queue diversion) | refused; `claim-next` works | SYRD-647 |
| UAT instructions; entering a user-review stage | refused, so user review is not entered | SYRD-648 |
| Relayed decisions (`relays_decision_of`); approvals on a Director-held ticket; `--target-assignee` | refused | SYRD-648 |
| Reminders, idle nudges, turn-end and permission-prompt checks, activity holds, delivery proof, Director escalations, dismissing notifications | ***not applied*** (the listener delivers queued notices only); dismissals refused | SYRD-649 |
| Edit fields, reassign, merge, drafts, attachments and screenshots, commit-exemption requests, stalled-ticket recovery, publication requests | refused | SYRD-650 |
| Size review; documentation gate | size review ***not applied***; a documentation-gate decision is refused, never taken unchecked | SYRD-651 |
| Publisher, Router, board stall alert, privileged jobs, context continuation, cross-board sources | ***not applied*** / refused | SYRD-651 |
| Adding a role or the VCS close role on an Oracle tenant | refused; nothing is changed | not yet routed |
| Waits on the User (`user_waits`), execution leases, provider cohorts and quota holds, report follow-ups, `set-audit-return` | refused, reads included (501); a workflow declaring `user_waits` or `scheduling.cohorts` is refused rather than applied unenforced | not yet routed |
| Audit's kick-back to the producer of the audited revision | ***not applied***: it returns to its declared stage and that stage's last implementer | not yet routed |

Every board read the core lacks answers HTTP 501 with that plain refusal and is
never attempted: `/api/reservations` (so `switchyard worker-pool status` reads
each slot as not reported, not as free), `/api/context-recovery/<role>` (a
launch resumes its recorded session or starts fresh, as with any board it
cannot ask), `/api/reminder-snoozes`, `/api/board-stall`, `/api/cohorts`,
`/api/provider-holds`, `/api/execution-leases`, `/api/image/…` and
`/api/thumb/…`. The list is `extension_operations.ORACLE_ABSENT_READS`.

## Proof

Two suites run against a real Oracle Database Free 23ai. When the test service
or python-oracledb is missing, each says it is **skipped** and why; neither
ever passes without having run.

**`tests/oracle_board_test.py`:**
* **Parity.** A production-built PostgreSQL board and an Oracle board, both
  behind the real board server, run the same scenario over HTTP: create, route,
  comment, submit with a real commit, kick-back, a refused approval, resubmit,
  approval through the gate walk, and done. Each step's answer and ticket
  match.
* **Differential walks.** Seeded random walks send each board the same
  requests: advertised actions and arbitrary ones, by allowed actors and by
  anyone, with random reasons, commits, targets, assignees and gate changes.
  Every step's success, HTTP status and resulting ticket (state, owner, commit,
  sign-offs, gates, comments, offered actions) must match. Six walks run by
  default (`ORACLE_WALKS` sets more). Thirty were run once, 1,800 steps, all
  matching.
* **The queue and the listener:**
  * every change queues a notice in its transaction;
  * a busy pane is not typed into;
  * a dead listener's claim times out, and the notice is delivered rather than
    lost;
  * a notice reaches only the role's live registered pane.
* **Concurrent claims:** twelve simultaneous claims, one ticket per worker.
* **Outage and restart.** A local TCP relay is cut and restored:
  * during the cut, a write fails plainly and leaves nothing;
  * afterwards the board writes again and the listener reconnects and delivers;
  * a restarted listener delivers once;
  * a restarted board reads the same ticket.
* **Migrations:**
  * upgrades from each earlier schema;
  * a crash between a step's DDL and its record;
  * a failing step reported honestly and resumed;
  * the runtime grants are the least each table needs.
* **Secrets and authority:**
  * no user name or password appears in a repr, a description or an error;
  * loose permissions, inline secrets, a `dsn`, and both identifiers at once
    are each refused;
  * a registered live pane is its role, while an unregistered process of the
    same account, or a reused pid, is not.
* **The shipped entry point.** `ticket-board.py` serves an Oracle board.
  `ticket-board-write` and the write client write through its Unix socket with
  real peer credentials, and a client claiming another role is refused by the
  server. The socket answers the launcher's runtime-assignments read.
* **The boundary.** Every operation the core lacks, each board operation module
  among them, answers "… is not in the Oracle core yet" and changes nothing. So
  does a workflow declaring `user_waits` or `scheduling.cohorts`. Every read
  the server routes, taken from its own source, is requested over HTTP: each
  is answered, none drops the connection, and each the core lacks is a 501
  refusal.
* **No PostgreSQL:**
  * the server, its reads and queue delivery run with PostgreSQL's driver
    unimportable and no `PG*` settings;
  * the listener's production sender and activity gate are built with every
    PostgreSQL connect refused.

**`tests/oracle_lifecycle_test.py`:**
* `ticket-board-oracle` driven as an operator drives it;
* **an IPv4 address, a non-default port and a SID** (a relay in front of the
  container database, which is known by SID):
  * `configure` stores the SID as a SID;
  * the driver's descriptor carries `(SID=FREE)` and no service name;
  * `migrate`, `check` and a board all work over it;
* the diagnostics:
  * an unreachable endpoint;
  * a service name given as a SID (DPY-6003);
  * an unknown service (DPY-6001);
  * refused credentials (ORA-01017);
  * an over-privileged runtime account and a revoked grant, which `migrate`
    restores;
* `check` and `status` issue only `SELECT`s;
* `configure` in a real terminal asks service-name-or-SID, re-asks on a wrong
  answer, and reads the password with echo off;
* `teardown` connects to nothing, removes only the board's own files, and
  leaves a linked path alone;
* **TCPS with a wallet** (SYRD-645), against the test service's TCPS listener:
  * `configure` keeps the protocol, the wallet and the server certificate's DN;
  * `migrate`, `check` and a board all work over it, and the database reports
    the session as `tcps`;
  * refused, each by its own cause:
    * another server DN (DPY-6002);
    * a wallet holding a certificate with the right DN but another key
      (certificate verification fails);
    * plaintext to the TCPS port (DPY-4011);
    * TLS to the plaintext port (the handshake breaks off);
  * where the test service has no TCPS listener, the suite says the TCPS case
    was skipped;
* across every command, no password and no runtime account name was printed.

**The test service** is disposable and used by these suites only:
`tests/oracle/oracle-free-test-service.sh`, run once as root after review. It
runs a digest-pinned `gvenzl/oracle-free` container bound to `127.0.0.1:1521`,
with its data in a labelled volume, and `teardown` removes both. Its two
passwords reach the container as files (SYRD-645):
* a directory only the database's uid can read, mounted read-only;
* `ORACLE_PASSWORD_FILE` and `APP_USER_PASSWORD_FILE`, read only while the
  database is created;
* the files are deleted then, and the container is started once more, so no
  process keeps them.

No password is in the container's environment or spec, or on a command line.
A container made by an earlier version is refused by `start`.
`tests/oracle/oracle_test_credentials.py init` generates its owner-only
credentials. Each test creates its own owner and runtime accounts through
`SYSTEM` (common `C##` accounts for the by-SID case) and drops them afterwards.

`oracle-free-test-service.sh tcps` (SYRD-645) adds a TCPS listener on
`127.0.0.1:2484`. A container created before SYRD-645 does not publish that
port; `tcps` says so and changes nothing, and `teardown` then `start` recreates
it, with new data.

The image has no Java, so `tcps` borrows Oracle's own Free image, pinned by
digest. It runs as a throwaway container with no network and only the data
volume mounted, and it:
* makes a test CA and a server certificate signed by it
  (`CN=syrd645-tcps-test`). Strict TLS clients such as Python 3.13+ reject a
  bare self-signed certificate, so the CA is a proper X.509 v3 CA;
* imports them into an auto-login wallet, and deletes the CA's key.

The owner receives the CA certificate alone, as
`~/.config/switchyard-oracle-test/tcps-wallet/ewallet.pem` (0600).

## Ordered child slices

Each slice is an active child ticket of SYRD-642, created in analysis for the Director to route, in this order:

1. **SYRD-645: The rest of an Oracle tenant's lifecycle.** It adds:
   * the board database in `switchyard status`;
   * upgrades on a host that keeps no schema-owner credential;
   * teardown through `ticket-board-oracle teardown`;
   * creation on an Oracle-only host;
   * TCPS, proven against the test service.

   Adding a role and the VCS close role on an Oracle tenant still refuse, and
   are not yet routed to a slice.
2. **SYRD-646: Blockers, dependencies and waits on Oracle.**
3. **SYRD-647: Serial focus and queue diversion on Oracle.** One active ticket per
   implementer, queued routing into implementation, and pull-scheduled
   destinations.
4. **SYRD-648: UAT instructions, user review, relays and Director holds on Oracle.**
5. **SYRD-649: Oracle listener parity.** Reminders, nudges, turn-end and prompt checks,
   activity holds, delivery proof and escalations, plus an activity gate that
   does not import `notify_listener`.
6. **SYRD-650: The remaining write verbs on Oracle.** Edit fields, reassign, merge,
   drafts, attachments, commit exemption, recovery and publication requests.
7. **SYRD-651: Size review, documentation gate, Publisher, Router, board stall and
   privileged jobs on Oracle,** each ported or moved behind the boundary.
8. **SYRD-652: Direction B, first domain:** one transition engine for both backends.
   PostgreSQL decides declared transitions through `workflow_engine`, proven
   by the differential walks, and the duplicated rules are retired.
