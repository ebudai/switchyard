# PORT-NOTES — Milestone 1 (TypeScript port of the ticket-board backend)

Scope: `GET /api/board`, `POST /api/tickets/actions/create_ticket`,
`POST /api/tickets/<id>/actions/route`, against the live `pgu` Postgres
schema, on scratch port 8771. Postgres-backend path only — the JSON store is
dead code on `origin/main` (empty ticket dir in prod; see below).

## Stack

- **Deno 2** (installed to `~/.deno`, no root needed — `curl -fsSL
  https://deno.land/install.sh | sh`). Built-in TS + strict mode, no build
  step, npm interop for `postgres`/`zod`. Node wasn't justified: nothing here
  needs the Node ecosystem specifically.
- **`postgres` (postgres.js)** over `pg`: TS-native types (no `@types/pg`
  gap), a tagged-template/`.unsafe()` API that maps directly onto the
  `SELECT ticket_board.<fn>(...)` calls the Python backend makes, and
  `sql.begin()` transactions read cleanly. `pg` would be the better call for
  COPY streaming or LISTEN/NOTIFY fan-out; M1 needs neither. Connects over
  the peer-auth Unix socket as `ticket_board_service`, same as the Python
  service's `psycopg.connect`.
- **Zod at the two boundaries**: HTTP request bodies (`CreateTicketBodySchema`,
  `RouteBodySchema`) and Postgres rows (`TicketRowSchema`, since postgres.js
  hands back `jsonb`/`text[]` as untyped `any`). Everything between those two
  edges is plain typed TS.
- **`TicketState` as a string-literal union + exhaustive `switch`**
  (`src/types.ts: legalNextStates`), not a lookup object. See finding #1.

## What the port surfaced

1. **Exhaustive `switch` is a strictly stronger guarantee than Python's
   `LEGAL_STATE_TRANSITIONS` dict, and the codebase currently has the state
   machine encoded THREE times, not two.** The real enforcement is
   `enforce_ticket_workflow_update()` in `schema.sql` (the trigger). Python's
   `app.py` has its own `LEGAL_STATE_TRANSITIONS` dict + `_enforce_legal_state_transition`,
   but that path is **only reachable from the JSON-backend `update_ticket()`
   method** — dead code now that the JSON store is retired in prod (confirmed:
   `store_backend` is `"postgres"` on the live board, JSON ticket dir is
   empty). `route_ticket()` (the postgres path) skips it entirely and lets
   the DB trigger be the sole authority. In TS, `legalNextStates()` is a
   `switch` over the literal union with a `default: assertNever(state)` —
   if a 10th state is ever added to `TICKET_STATES` without a matching
   `case`, **this fails to compile**. Python's `dict.get(state, set())`
   would instead silently return an empty transition set for the new state,
   surfacing only at runtime as a generic "illegal state transition" error
   with no indication the dict itself was the thing that was stale. Concrete
   recommendation: once JSON is confirmed fully retired, delete
   `LEGAL_STATE_TRANSITIONS`/`_enforce_legal_state_transition`/
   `_enforce_workflow_rules` and friends from `app.py` — they're unreachable
   and a second Python-side mirror TS now makes redundant twice over.

2. **`ticket_board.require_actor(p_allowed_roles, p_action)`'s first
   parameter is dead.** Every `SECURITY DEFINER` function (`create_ticket`,
   `route`, `cancel`, `audit_sign_off`, ...) calls
   `require_actor(ARRAY['director', 'eric'], 'create_ticket')`-style, passing
   a role array that reads as "only director/eric may call this." The
   function body never references `p_allowed_roles` — it only checks
   `current_actor_role() = 'ticket_board_service'`, which is true for every
   HTTP request alike (single DB service role). So **all per-operation
   authorization is enforced exclusively at the application layer**
   (`server.py`'s `OPERATION_ALLOWED_ROLES` dict / this port's
   `src/auth.ts`). That's not necessarily wrong — defense-in-depth at the DB
   layer would need per-caller DB roles, which the single-writer model
   deliberately doesn't have — but the `ARRAY[...]` argument is actively
   misleading about where the check lives, and a reviewer skimming
   `schema.sql` would reasonably conclude the DB enforces it. Worth either
   removing the dead parameter or wiring it up for real. TS didn't "catch"
   this via its type system (it's a SQL-level issue) but writing a
   from-scratch `auth.ts` forced tracing every call site to find out where
   the real check lives, which is how this surfaced.

3. **`needs_eric_signoff=true` on plain create silently no-ops in a
   naive port, but errors in Python.** `_pg_create_ticket_record` explicitly
   raises (`"postgres function API does not support initial signoff fields
   yet"`) if `needs_eric_signoff`/`audit_signoff`/`eric_signoff` are truthy,
   because `ticket_board.create_ticket(title, body)` has nowhere to put them
   — there's no DB function support for setting those on initial insert. My
   first pass defined `needs_eric_signoff` on `CreateTicketInput` and never
   read it, which would have silently created a ticket that doesn't need
   Eric's signoff even though the caller asked for one. Fixed to reject with
   the same message Python uses (`src/ticket_store.ts: createTicket`). This
   is exactly the class of "quiet contract gap" a compiler doesn't save you
   from — only tracing the Python source path end-to-end did.

4. **Runtime backend confusion, resolved but worth recording.** Mid-port I
   read `scripts/ticket_board/*` from the *shared* checkout at
   `/home/agent/Projects/pgu`, which was stale (pre-migration, JSON-only) —
   `origin/main` had already moved to the dual-backend `app.py`. Compounding
   that, a stale line in `/tmp/pgu-ticket-board.log` from a pre-restart
   process instance read `backend: json`, which looked like corroborating
   evidence. The live `/api/board` response's own `store_backend` field
   (`"postgres"`) and the running process's actual `ps` cmdline
   (`--store-backend postgres`) were the ground truth. Two real findings
   here, independent of the port itself:
   - Always read source from your own worktree off `origin/main`, never the
     shared checkout — it can be behind by an arbitrary number of commits.
   - **Operational gap**: `~/.config/systemd/user/pgu-ticket-board.service`'s
     `ExecStart` has no `--store-backend` flag — the live process is
     currently `postgres` only because someone started it manually/out of
     band with extra flags the unit file doesn't have. A crash under
     `Restart=on-failure` would restart via the *unit file's* command line
     and silently fall back to the JSON backend default. Worth a ticket to
     get `--store-backend postgres --frames ... --assets ...` into the unit
     file itself.

## Known M1 simplifications (not bugs, just out of bounded scope)

- `create_ticket`'s `blocked_by` accepts raw strings and defers all
  `PGU-N` format/self-reference/dedup validation to the DB's `CHECK`
  constraint on `ticket_blockers.blocker_ticket_id`, rather than
  reproducing Python's `_validate_blocked_by` client-side. A malformed
  blocker ID surfaces as a raw Postgres constraint-violation message
  instead of Python's cleaner `"invalid blocked_by ticket id: ..."` string.
  Fine for M1 (create+route are the tested paths); worth closing before
  `set_blockers`/`edit_fields` are ported for real.
- `screenshot_available` does a real `Deno.stat` per screenshot (parity with
  Python's `Path.is_file()`), but attachment *writes* aren't implemented —
  matches `_pg_create_ticket_record`'s own restriction
  (`"postgres function API does not support attachment writes yet"`).
- `file_bug`, `start_work`, `submit_to_audit`, `audit_sign_off`,
  `eric_sign_off`, `mark_done`, `defer`, `cancel`, `merge`, `edit_fields`,
  `add_comment`, `set_blockers`, `set_manually_controlled` are not ported —
  out of M1's bounded scope by design. `cancel` was exercised directly via
  `psql` (not through this service) to clean up the scratch ticket used for
  parity testing.

## Parity evidence

Ran the scratch service on 8771 against the live `pgu` schema:
- `GET /api/board`: top-level keys and per-ticket keys are byte-for-byte
  identical set to the live Python board's response (`asset_dir, assignees,
  errors, frame_dir, refreshed_at, screenshots, states, store_backend,
  store_path, tickets` / 22 per-ticket fields).
- Missing caller header → `400 missing X-PGU-Caller-Role` (exact match).
- Wrong role for `route` → `403 ops cannot call route` (exact match).
- Created `PGU-227` via `create_ticket` (caller=director) → `201` with full
  ticket shape.
- Routed `PGU-227` to `backlog`/`ops` → `200`, verified via `GET /api/board`.
- Cleaned up: routed back to `analysis` (so `cancel`'s state-machine
  precondition was satisfiable — confirms `legalNextStates` matches the DB
  trigger, since the DB rejected `backlog -> cancelled` exactly as the TS
  union predicts), then `ticket_board.cancel('PGU-227', ...)` via `psql`.
  Final state: `cancelled`, comment recorded.

## Run it

```
cd ticket-board-ts
deno task start   # PORT=8771 by default, PGHOST/PGDATABASE/PGUSER from env
```
