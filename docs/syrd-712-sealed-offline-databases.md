# SYRD-712: a sealed offline database does not block the database-auth hardening

## What happened

SYRD-707 could not preview the cluster's hardening (SYRD-706). The planner refuses every board database that no root record accounts for, because its listener and anyone else reaching it would be cut off. SYRD-705 left one such database: `pgu_syrd705_failed_20261030205539`, its preserved evidence. The cluster refuses every connection to it: `pg_database.datallowconn` is false, and even the superuser gets `FATAL: database ... is not currently accepting connections`.

Reopening it to dump or drop it would break SYRD-705's preservation boundary, and would put a live authentication change ahead of the hardening.

## The change

`scripts/database_auth_transition.py`:

- **The planner reads each database's `datallowconn`**, from `postgres`'s own catalogue, in one query (`DATABASES`).
  - A database no root record accounts for is still a refusal while it accepts connections.
  - One the cluster refuses every connection to is **sealed offline**: it is named in the plan (`Plan.sealed`) and in its digest, and granted no rule. The hardened `pg_hba` and `pg_ident` are exactly what they would be without it.
  - Registered databases, and `postgres`, are planned as before.
- **The preview reports it first**, before the tenants and the diffs, in a `SEALED OFFLINE` section. That section says the database accepts no connection, is granted nothing, and is never connected to, dumped, dropped, renamed or reopened.
- **The apply** recomputes the plan, so any drift is a different digest and a refusal before anything is written:
  - the sealed database reopened;
  - the sealed database gone;
  - any other database appearing, sealed or not.

  After the probes it reads the sealed databases' state again. One that is gone, or now accepts connections, fails the apply, and the files and jobs come back. Root's backup records the sealed set.
- **The way back (`restore`)**, run directly or by a failed apply, reads their state again and reports any that is no longer sealed.

Nothing the transition does names a sealed database as a connection target or changes its flags. Every read of its state is a query, as `postgres`, against the `postgres` database's catalogue.

**Unchanged:**
- a connectable database without root's records is a hard refusal;
- the root records themselves; `test6_ticket_board`'s question is answered by the ordinary privileged preview, not by a separate reader.

## Tests

`tests/sealed_offline_database_test.py`, against `database_auth_transition_test`'s host. That host decides each probe by the files last reloaded, gives each database its own `datallowconn`, and records any connection to a sealed database and any `ALTER`/`DROP DATABASE`, rename, `datallowconn` change or dump. It runs main first, from a git archive, then this tree:

**On main:**
- the sealed evidence database is refused like a live one;
- the apply refuses.

**Here:**
- it is sealed, not a refusal;
- the digest differs from the same host without it;
- the hardened files are byte-identical to that host's;
- the preview names it before anything else;
- apply succeeds, with its state read again after the probes;
- root's backup records it;
- restore reads its state again and leaves it sealed;
- the accepted digest stops holding when, between preview and apply, it reopens, another sealed database appears, or it is dropped; a connectable database appearing is refused;
- reopened during the apply: the apply fails, says so, and restores everything;
- dropped during the apply: the apply fails with "gone" and restores;
- no violation in any scenario.

`database_auth_transition_test`'s host gained `datallowconn`, violation recording, and reopen/drop during the reload. Its own 19 checks are unchanged.
