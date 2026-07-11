# PORT-NOTES — TypeScript port of the ticket-board backend

## Milestone 3 (real-time events + SO_PEERCRED evaluation)

M3 scope per the brief: `GET /events` push, the SO_PEERCRED Unix-socket PID
path (PGU-212), and porteraudit's M2 carryovers (`deno.json`'s stale task
entrypoint, date/normalization parity gaps).

### `GET /events` — what Python actually does, and what I built

Despite "LISTEN-NOTIFY" being in the M3 brief's framing, **Python's `/events`
is not Postgres LISTEN/NOTIFY at all.** `TicketBoardEventHub` is in-process
pub/sub: every write handler calls `events.notify_change(store_signature())`
immediately after its own write (so changes made through the Python server
show up with ~0 latency), backstopped by a 1-second poll thread that
re-fetches `store_signature()` to catch changes from *outside* the process
(the JSON files being hand-edited, historically, or another writer). Each
SSE connection gets its own maxsize-1 "latest version" slot — concurrent
version bumps coalesce, which is safe because every event just tells the
client to re-`GET /api/board` in full; there's no diff payload to miss.

I considered a genuine Postgres LISTEN on the existing
`ticket_board_state_transition` channel (schema.sql already `pg_notify`s on
it via `notify_ticket_state_transition`) instead of polling — postgres.js's
`sql.listen()` would have been the more idiomatic Deno-native choice. I
didn't use it: that channel only fires on ticket **state** changes. `edit_fields`,
`add_comment`, and `set_blockers` writes don't touch `state` and wouldn't
notify anything, yet Python's board DOES pick those up (its signature
includes comment/blocker/attachment *counts*, not just state). Matching
that would mean adding broader `pg_notify` calls to the triggers, which is
out of scope (schema/functions are read-only for this port). So `src/events.ts`
reproduces Python's actual mechanism faithfully: immediate in-process notify
after every write handler in `server.ts`, backstopped by a 1s poll — not
because LISTEN/NOTIFY is unavailable, but because it's a narrower signal
than what's actually being ported.

**The "critical" constraint** (a persistent poll connection must not come
from/exhaust the request-handling pool) is real regardless of polling vs.
LISTEN: a 1-per-second query holding the pool's only slot(s) would starve
concurrent HTTP requests. `src/db.ts` now exposes `connectForEventPolling()`
— a separate `postgres()` client with `max: 1`, used *only* by `EventHub`'s
backstop loop; `main.ts` wires it up distinctly from the request-serving
`sql`. Verified live: created one scratch ticket while an `/events` curl
connection was open — the version bump arrived within ~500ms (the immediate
post-write notify), not after a 1s poll tick or the 15s keepalive window;
confirmed the keepalive (`: keepalive\n\n`, exact byte match to Python's) at
the 15s mark with no writes; confirmed clean disconnect handling via
`req.signal`'s `abort` event (empirically verified with a standalone probe:
`signal.aborted` stays `false` for the stream's full lifetime and only
flips at the real client disconnect, despite Deno logging a "legacy abort
behavior" deprecation notice — the *current* behavior is correct for a
streaming response body; `--unstable-no-legacy-abort` is a future-proofing
option worth revisiting if Deno changes the default before this ships for
real).

### SO_PEERCRED / PGU-212 Unix-socket peer auth — descoped, documented per director decision

**Finding (this is the interesting engineering question, not a failure):**
Deno 2's Unix-socket API (`Deno.listen({transport: "unix"})` /
`Deno.UnixConn`) exposes **zero file-descriptor access** — no `.rid`, no
internal handle, nothing, on stable or with `--unstable-net`. Confirmed
empirically: `Object.keys(conn)` is `{}`, `conn.rid` is `undefined`, and the
`Conn` prototype's only members are `read`/`write`/`close`/`closeWrite`/
`readable`/`writable`/`ref`/`unref`/`remoteAddr`/`localAddr` — no path to a
raw fd. `getsockopt(fd, SOL_SOCKET, SO_PEERCRED, ...)` (what `server.py`'s
`peer_credentials()` calls, via Python's own socket object's fd) fundamentally
needs that fd. There is no small shim for this in Deno's own networking
stack.

Options evaluated:
- **(A) Full FFI raw-socket rewrite.** `Deno.dlopen("libc.so.6", ...)` does
  work (verified: `socket()` returned a real fd via FFI) and could
  implement the *entire* Unix-socket accept loop via raw libc calls
  (`socket`/`bind`/`listen`/`accept`/`getsockopt`/`recv`/`send`), bypassing
  `Deno.listen`/`Deno.serve` entirely. This gets true SO_PEERCRED parity but
  means hand-rolling the whole connection lifecycle *and* an HTTP/1.1
  parser by hand — a full alternate server implementation, not "a small FFI
  shim." Real engineering cost for a path that, per the director, is
  dormant even in production.
- **(B) Native sidecar helper.** A tiny compiled helper (any language with
  libc access) that accepts on the socket, reads `SO_PEERCRED` itself, and
  hands the connection + credentials to the Deno process somehow (e.g. via
  `SCM_RIGHTS` fd-passing over a second socket, or by having the sidecar
  proxy bytes while attaching a header). Avoids FFI in the TS codebase but
  introduces a second running process and an IPC protocol between it and
  Deno — a real design in its own right, not evaluated further since (C)
  is sufficient given the director's finding below.
- **(C) Stay on TCP + `X-PGU-Caller-Role` header, no Unix socket in the TS
  port.** What's implemented.

**Director's call: (C), and it's not a compromise.** The live
`pgu-ticket-board.service` unit doesn't pass `--unix-socket` — the Python
board today serves TCP + header only; SO_PEERCRED is infrastructure that
exists in `server.py` but is dormant in the actual running deployment. So
(C) is full parity with what's *actually live*, not a reduced-scope
fallback. (A)/(B) remain documented above as real options if PID-based
local auth becomes an active requirement later (e.g. if `--unix-socket` is
ever turned on in production).

### Parity fixes (porteraudit carryovers from M2)

- `deno.json`'s `start`/`check` tasks referenced `src/main.ts`; `main.ts`
  has always lived at the project root (`ticket-board-ts/main.ts`). `deno
  task check` failed outright (`Cannot find module`) — this was never
  caught because M1/M2 testing always ran `deno run ... main.ts` directly,
  never `deno task`. Fixed; added the `--allow-write=/var/run/postgresql`
  flag to the `start` task too (needed for the Postgres Unix-socket
  connection, previously only passed manually on the command line).
- `refreshed_at` (board snapshot) and `screenshots[].modified` were both
  using JS's `Date.toISOString()`, which matches neither of Python's two
  *different* timestamp formats: `iso_now()` is UTC, second-precision,
  `+00:00`-suffixed (`refreshed_at`); `format_timestamp()` converts to the
  **server's local timezone** (`.astimezone()` with no argument) and uses a
  totally different, non-ISO, space-separated layout with no offset
  (`screenshots[].modified`). `toISOString()` is UTC-with-milliseconds-and-
  `"Z"` — matches neither. New `src/time.ts` (`isoNow()`,
  `formatLocalTimestamp()`) reproduces both exactly, including the
  local-vs-UTC distinction, which is easy to lose sight of since both
  fields look superficially like "just format a timestamp."
- Ticket-id case normalization: `app.py` does `.strip().upper()` on ticket
  ids inside nearly every method (`route_ticket`, `get_ticket`,
  `merge_tickets`, ...), so a lowercase/mixed-case id in a request still
  resolves. The TS port didn't normalize anywhere before M3 — a
  `pgu-235`-style id would 400 with "ticket not found" instead of resolving
  like Python does. Fixed at the single HTTP dispatch point in `server.ts`
  (ticket id extracted from the URL) plus `merge`'s `target_id` (the one
  other ticket-id-shaped field that arrives via a request body rather than
  the URL path).

### M3 test hygiene

M2 testing left roughly a dozen new ticket ids on the live board (all
ended `done`/`cancelled`, but still real churn on shared ticket-id space).
For M3: exactly **one** scratch ticket was created (to observe `/events`
push on a real write), cancelled immediately after. SO_PEERCRED verification
required no ticket writes at all (pure Deno-API probing); the parity fixes
were verified via `deno check` + direct `curl`/`psql` reads, not new tickets.

---

## Milestone 2 (complete write-action API + caller-role authorization)

M2 ports every remaining `POST /api/tickets/actions/<op>` and
`POST /api/tickets/<id>/actions/<op>` from `server.py`'s
`OPERATION_ALLOWED_ROLES`: `file_bug`, `start_work`, `submit_to_audit`,
`audit_sign_off`, `audit_kick_back`, `eric_sign_off`, `eric_reopen`,
`mark_done`, `defer`, `cancel`, `set_manually_controlled`, `set_blockers`,
`add_comment`, `edit_fields`, `merge` (M1 already had `create_ticket` +
`route`). Out of scope, per the M2 brief: `GET /events` SSE, SO_PEERCRED
socket peer auth (PGU-212), the frontend.

### Auth matrix as exhaustive typed data

`Operation` (`src/auth.ts`) is now the full 17-member string-literal union.
`OPERATION_ALLOWED_ROLES: Record<Operation, readonly CallerRole[]>` means
adding an `Operation` without a matching roles entry is a compile error — the
same device as `legalNextStates()`. `server.ts`'s `TICKET_OPERATION_HANDLERS:
Record<Exclude<Operation, "create_ticket" | "file_bug">, TicketOpHandler>`
extends that guarantee to dispatch: an op with a role entry but no handler
also fails to compile. Between the two, "gate it but forget to wire it" or
"wire it but forget to gate it" both become type errors instead of a 500 or
an unintended-open endpoint discovered in production.

One auth wrinkle beyond the plain role map: `start_work` and
`submit_to_audit` additionally require the caller's role to equal the
ticket's *current assignee* (`server.py`'s `require_operation_allowed`,
mirrored as `requireAssigneeMatch` in `auth.ts`) — an implementer role alone
isn't sufficient, it has to be *your* ticket. Missing this would have let
any implementer role start/submit any other implementer's ticket.

### What M2 surfaced

5. **postgres.js double-encodes a pre-`JSON.stringify`'d parameter in a
   multi-param `.unsafe()` call.** `edit_fields(id, patch jsonb)` kept
   failing with the DB's own `"edit_fields patch must be an object"` even
   though the JSON text was valid — `jsonb_typeof()` on the received value
   was `"string"`, not `"object"`. Root cause: passing
   `[ticketId, JSON.stringify(patch)]` to `.unsafe("...($1, $2::jsonb)", ...)`
   sends the *string* as $2, and postgres.js's parameter encoding for that
   two-param shape re-serializes it, producing a jsonb value that's a
   quoted string scalar containing the JSON text, not a JSON object. Passing
   the plain JS object directly (`[ticketId, patch]`, letting postgres.js's
   own auto-serialization handle the jsonb encoding) fixed it immediately —
   confirmed the correct/incorrect behavior side by side with a 5-line
   repro script before changing the real code. This is a client-library
   semantics gap, not a schema or Python issue, but it's exactly the kind of
   thing that silently breaks the FIRST time a real object payload crosses
   a jsonb boundary in a new stack, and would've been a confusing bug report
   ("edit_fields is broken") without the isolated repro.

6. **`merge()`'s two-statement `manually_controlled` dance is load-bearing,
   not redundant-looking cleanup.** `schema.sql`'s `merge()` does `UPDATE
   ... SET manually_controlled = true WHERE id = source_id` immediately
   followed by a second `UPDATE ... SET state = 'done', ..., 
   manually_controlled = source_ticket.manually_controlled` (the *original*
   pre-merge value, captured before either UPDATE). Read as a single unit
   it looks like the first UPDATE's effect is immediately undone by the
   second and could be deleted. It can't: `enforce_ticket_workflow_update`
   is a BEFORE-UPDATE-FOR-EACH-ROW trigger that short-circuits all
   transition-legality checks whenever `OLD.manually_controlled OR
   NEW.manually_controlled` is true. The first UPDATE flips
   `manually_controlled` true (trivially legal, since `NEW.manually_controlled`
   being true bypasses the trigger for that statement); the second UPDATE
   then sees `OLD.manually_controlled = true` and is *also* bypassed,
   letting `state` jump straight to `'done'` from any prior state (skipping
   the normal "must pass through director_review" rule) while restoring
   `manually_controlled` to whatever it was originally. Two sequential
   statements exploiting per-statement trigger evaluation order — didn't
   change anything in the port (I just call `SELECT ticket_board.merge($1,
   $2)` once), but worth flagging so nobody "simplifies" `merge()` into one
   UPDATE during a future schema change and silently reintroduces the
   transition-legality checks for merge, breaking it for non-director_review
   source tickets.

7. **The DB is consistently the sole content-validator for anything the
   Python HTTP layer doesn't pre-check itself**, which simplified this port
   more than expected: `set_blockers`'s ticket-ID format/self-reference,
   `edit_fields`'s `parent_id` cycle detection, `submit_to_audit`/`mark_done`'s
   commit-hash hex format — none of these needed a client-side reimplementation
   of Python's `_validate_*` helpers, because on the postgres backend those
   Python helpers are *also* not consulted (only the DB `CHECK`s and
   `RAISE EXCEPTION`s are). The one place Python's HTTP layer (not `app.py`)
   *does* pre-validate before the DB gets a say is `edit_fields`'s
   invalid-field-name list (reports every invalid key, not just the DB's
   first one) and the `audit_signoff=true`/`eric_signoff=true` rejection —
   both reproduced client-side in `ticket_store.ts: editFields` for exact
   error-text parity.

### M2 simplifications (not bugs)

- `set_blockers`/`create_ticket`'s `blocked_by` still defers ID-format
  validation to the DB (per M1 note); unchanged in M2.
- `merge`'s Python `merge_tickets()` has a second `actor != "director"`
  check that's dead code by the time it runs (the HTTP layer's
  `OPERATION_ALLOWED_ROLES["merge"] = {"director"}` already guarantees
  `caller == "director"`). Not reproduced client-side since it can never
  fire; noted here rather than silently dropped without explanation.

### M2 parity evidence

Full lifecycle exercised against the live `pgu` schema on scratch port 8771
(scratch tickets created under caller=director, assigned to `research` to
avoid colliding with the real `ops`-assigned in-progress ticket already on
the live board — a good reminder that "point a scratch client at prod" means
prod's actual concurrency invariants apply to you too):

- **Non-eric lifecycle**: `create_ticket` → `edit_fields` (set
  `implementation`) → `route` (ready/research) → `start_work` (research) →
  `submit_to_audit` (research, commit_hash) → `audit_sign_off` (audit) →
  auto-advanced to `director_review` (confirmed) → `mark_done` (director) →
  `done`.
- **Eric-signoff lifecycle**: same, but `edit_fields` also set
  `needs_eric_signoff: true` first; after `audit_sign_off` the ticket
  auto-advanced to `eric_review` (not `director_review`, confirmed) →
  `eric_sign_off` (eric) → auto-advanced to `director_review` → `mark_done`
  → `done`.
- **Kickback/misc**: `add_comment` (attribution to `research` confirmed via
  `current_app_actor()` + `set_config('ticket_board.caller_role', ...)`) →
  `set_manually_controlled` true then false → `edit_fields` → `route` →
  `start_work` → `submit_to_audit` → `audit_kick_back` (audit) → confirmed
  back to `analysis`/`unassigned` → `set_blockers` (set then cleared) →
  `defer` → confirmed `backlog` → `route` back to `analysis` → `cancel`
  (director, reason) → `cancelled`.
- **Merge**: two fresh tickets, `merge(source, target)` (director) →
  confirmed response shape is `{source, target}` (not `{ticket}` — the one
  intentional shape divergence, matching Python's `merge_tickets`) → source
  ended `done`/`commit_exempt`, target got the `"Merged in <source>: ..."`
  comment, target's own state untouched.
- **file_bug**: created with `source_ticket_id` pointing at an already-done
  ticket → confirmed response `parent_id` matches.
- **403 cases**: wrong role for `route` (`"research cannot call route"`) and
  wrong assignee for `start_work` (`"main cannot call start_work for ticket
  assigned to research"`) — both exact-text matches to the Python contract.
- All scratch tickets ended in `done` or `cancelled` (terminal states); none
  left in an active workflow state.

---

# Milestone 1 (read + create_ticket + route slice)

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

1. **Exhaustive `switch` is a strictly stronger guarantee than the former
   Python lookup-object mirror of the workflow graph.** The real enforcement
   is `enforce_ticket_workflow_update()` in `schema.sql` (the trigger). In
   TS, `legalNextStates()` is a `switch` over the literal union with a
   `default: assertNever(state)` — if a 10th state is ever added to
   `TICKET_STATES` without a matching `case`, **this fails to compile**. A
   lookup object could instead silently return an empty transition set for the
   new state, surfacing only at runtime as a generic illegal-transition error
   with no indication the lookup itself was stale.

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
   `origin/main` had already moved to the database-backed `app.py`.
   Compounding that, a stale line in `/tmp/pgu-ticket-board.log` from a
   pre-restart process instance read `backend: json`, which looked like
   corroborating evidence. The live `/api/board` response's own
   `store_backend` field (`"postgres"`) and the running process's actual
   command line were the ground truth. One real finding here, independent of
   the port itself:
   - Always read source from your own worktree off `origin/main`, never the
     shared checkout — it can be behind by an arbitrary number of commits.

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
  `add_comment`, `set_blockers`, `set_manually_controlled` were not ported in
  M1 — out of M1's bounded scope by design. `cancel` was exercised directly
  via `psql` (not through this service) to clean up the M1 scratch ticket.
  **All ported in M2** (see above).
- Attachment *writes* (`screenshots`/`screenshot` fields) still aren't
  populated by any op in M2 either — `edit_fields` accepts them and the DB
  function writes `ticket_attachments` rows, so this now actually works
  end-to-end, but it wasn't part of the M2 test sweep (no image-upload
  concept exists in this scratch service yet; `POST /api/upload` is
  explicitly out of scope, matching the M2 brief's "no frontend" carve-out).

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
