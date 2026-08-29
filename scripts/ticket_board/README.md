# PGU Ticket Board

Lightweight local ticket board for User and the director.

Launch:

```bash
python3 scripts/ticket-board.py --host 127.0.0.1 --port 8770
```

Shared shell-side ticket creation:

```bash
/home/agent/bin/directorctl ticket-create \
  --board-url http://127.0.0.1:8770 \
  --assignee app \
  --state in_progress \
  --blocked-by "PGU-23, PGU-25" \
  --title "Board: add director as assignee" \
  --body "Allow director-owned action tickets in the assignee enum." \
  --comment-who director \
  --comment-text "Speced + routed to app."
```

Pane write API:

- Pane callers should use the explicit operation routes rather than patching
  ticket JSON directly:
  - `POST /api/tickets/actions/create_ticket`
  - `POST /api/tickets/actions/file_bug`
  - `POST /api/tickets/<TICKET-ID>/actions/<operation>`
- Browser HTTP operation requests must include the per-process
  `X-Ticket-Board-Write-Token` emitted into the served board page, plus
  `X-Ticket-Board-Caller-Role` with one of `director`, `user`, `main`, `app`,
  `ops`, `audit`, `inspector`, `perf`, or `research`. Without the token, HTTP
  writes are rejected; read-only HTTP requests remain available.
  `X-PGU-Write-Token` and `X-PGU-Caller-Role` are accepted only as legacy
  compatibility aliases.
- Local pane tooling should write through the Unix-domain socket at
  `/run/pgu-ticket-board/ticket-board.sock` when it exists. The write client registers the
  auto-resolved pane role on `/api/register-caller`; the board derives the
  caller role from the socket connection's OS-verified `SO_PEERCRED` PID and
  ignores caller-role headers for socket writes.
- The Unix-socket registration allows one live PID per pane role. Duplicate
  live registrations are rejected and logged; the registration is released when
  the socket connection closes, and stale dead-PID registrations are dropped.
- Ticket-scoped operations are `route`, `start_work`, `submit_to_inspection`,
  `submit_to_audit`,
  `audit_sign_off`, `audit_kick_back`, `director_dat_sign_off`,
  `director_dat_kick_back`, `user_sign_off`, `user_reopen`, `mark_done`,
  `defer`, `cancel`, `set_manually_controlled`, `set_blockers`, `add_comment`,
  `edit_fields`, and `merge`.
- Python and shell tooling should use `scripts.ticket_board.write_client` or
  `scripts/ticket-board-write` instead of editing `PGU-N.json` directly. The
  client resolves its default caller role from `TICKET_BOARD_CALLER_ROLE`, then
  legacy `PGU_TICKET_BOARD_CALLER_ROLE`. If neither is set, writes fail instead
  of inferring a role from tmux or falling back to `director`. The standing pane
  launcher exports this variable for each role, so `pgu-main`, `pgu-app`, and
  `pgu-ops` write as `main`, `app`, and `ops` by default. Pass `--caller-role`
  when deliberately acting as a different board role.
- Tests must point write-client calls at disposable board targets. When the
  write client detects it is running under a test process, it refuses writes to
  the live PGU board URL/socket before opening a connection. Use
  `TICKET_BOARD_ALLOW_PRODUCTION_WRITES_UNDER_TEST=1` only for a deliberate
  production-write test.
- The main/app/ops panes should use their own role for allowed pane operations,
  for example `scripts/ticket-board-write start-work PGU-123`,
  `scripts/ticket-board-write submit-to-inspection PGU-123`,
  `scripts/ticket-board-write submit-to-audit PGU-123 --commit-hash <sha>`,
  `scripts/ticket-board-write file-bug --source-ticket-id PGU-123 ...`, and
  `scripts/ticket-board-write add-comment PGU-123 --text ...`.

Read-only CLI:

- `scripts/ticket-board-read ticket PGU-123` prints one full ticket in a
  human-readable form: state/assignee, key flags, blocker fields, body,
  implementation, and full chronological comments. `scripts/ticket-board-read
  PGU-123` is shorthand for the same command.
- `scripts/ticket-board-read comments PGU-123` prints only the chronological
  comment thread.
- `scripts/ticket-board-read queue [role]` prints active/review work plus
  backlog in serial-focus order, either grouped for all roles or limited to one
  role.
- `scripts/ticket-board-read board [--all]` prints one line per ticket grouped
  by state; terminal tickets are hidden unless `--all` is set.
- `scripts/ticket-board-read blockers PGU-123` prints `blocked_by`, `blockers`,
  and `blocked_reason` for one ticket.
- `scripts/ticket-board-read director` and `scripts/ticket-board-read mine`
  print items needing director attention.
- Add `--json` to any read subcommand for raw JSON output. The read client only
  uses read-only HTTP `GET` endpoints; it does not import the write client,
  register callers, open the write socket, or mutate board state.

Defaults:

- Store path: `/home/agent/.claude/pgu-tickets`
- Persistent asset path: `/home/agent/.claude/pgu-tickets-assets`
- Screenshot directory: `/tmp/pgu-frames`
- Local URL: `http://127.0.0.1:8770/`

Store contract:

- One JSON file per ticket in the store directory, named `PGU-N.json`
- The web app re-reads disk on every request; there is no in-memory ticket cache
- Browser clients subscribe to `GET /events` (SSE) and re-fetch the board on ticket-store changes
- Director-side shell edits are expected and supported

Ticket schema:

```json
{
  "id": "PGU-1",
  "title": "short title",
  "body": "details and notes",
  "screenshot": "/home/agent/.claude/pgu-tickets-assets/PGU-1-1234567890.png",
  "screenshots": [
    "/home/agent/.claude/pgu-tickets-assets/PGU-1-1234567890.png",
    "/home/agent/.claude/pgu-tickets-assets/PGU-1-1234567891.png"
  ],
  "assignee": "main",
  "state": "analysis",
  "blocked_by": ["PGU-23", "PGU-25"],
  "implementation": "",
  "audit_prompt": "",
  "audit_signoff": false,
  "commit_hash": "0123456789abcdef0123456789abcdef01234567",
  "commit_exempt": false,
  "needs_user_signoff": true,
  "user_signoff": false,
  "created": "2026-07-08T12:34:56+00:00",
  "updated": "2026-07-08T12:40:01+00:00",
  "comments": [
    {
      "who": "director",
      "text": "audit this after diffuse tweak",
      "ts": "2026-07-08T12:41:00+00:00"
    }
  ]
}
```

Allowed values:

- `assignee`: `main`, `app`, `perf`, `ops`, `audit`, `agent`, `director`, `research`, `user`, `unassigned`
- `state`: `draft`, `backlog`, `analysis`, `in_progress`, `inspection`, `audit`, `dat`, `user_review`, `director_review`, `done`, `cancelled`

Notes:

- `user_review` is reserved for tickets with `needs_user_signoff: true` and is displayed in the UI as `UAT`
- `dat` is Director Acceptance Testing, a director-owned gate for `needs_user_signoff` tickets after audit and before UAT
- `draft` is an opt-in pre-triage staging state for director/User prep; it stays unassigned and does not notify until released to `analysis`
- `backlog` is for deferred or parked tickets; `analysis` is for active triage/spec work before a ticket is revived into Implementation (`in_progress`)
- `blocked_by` is a list of ticket IDs the ticket is waiting on; `blockers` carries the same IDs with a persistent `resolved` flag, and unresolved blockers prevent forward promotion until the referenced blocker reaches `done` or `cancelled`
- `implementation` is an optional director-authored implementation package/spec; ticket body/comments can carry the spec, and only assignee must be set before entering Implementation (`in_progress`)
- `in_progress` is displayed as Implementation and is limited to one active ticket per implementer; extra implementer work is auto-queued as assigned `backlog` with `parked: false`
- `director_review` is displayed as Final Sign-Off and is the director's final gate before `done`
- `audit_prompt` is retained for reference text but is optional in the default workflow
- `commit_hash` stores the verified git commit associated with the ticket when it moves to `done`; it must be on `main`
- `commit_exempt` is an explicit override for non-code/process tickets that should be allowed into `done` without a commit
- `cancelled` is a separate terminal state for abandoned/superseded/won't-do tickets; cancelling requires a non-empty comment reason but no commit/audit sign-off
- `cancelled` is a separate terminal state for abandoned/superseded/won't-do tickets; cancelling requires a non-empty comment reason but no commit/audit sign-off
- `screenshots` is the canonical attachment list; `screenshot` is retained as the first attachment for back-compat with older tools and tickets
- Screenshots referenced by tickets may live in either `/tmp/pgu-frames` or `/home/agent/.claude/pgu-tickets-assets`
- API/write-client attachment paths may still reference `/tmp/pgu-frames` as a transient inbox; once attached to a ticket, each selected image is copied into `/home/agent/.claude/pgu-tickets-assets`
- Clipboard-pasted screenshots are normalized to PNG and staged under `/home/agent/.claude/pgu-tickets-assets` before ticket attachment
- Web uploads can tag a set before attaching. Target uploads are saved as
  `target__upload_<ns>.png`, attempt uploads as
  `attempt-<NNN>-<label>__upload_<ns>.png`, and feedback/freeform labels as
  `<label>__upload_<ns>.png`; untagged uploads keep the legacy
  `upload_<ns>.png` name.
- The detail view groups image attachments into collapsible sets by filename prefix:
  `target__<name>.png` goes into `Target`,
  `attempt-<NNN>-<label>__<name>.png` goes into `Render Attempt NNN - Label`,
  `<label>__<name>.png` goes into a named set such as `Feedback`,
  and unprefixed legacy images go into `Ungrouped`. The newest numbered render
  attempt is expanded by default; older attempts, target references, and
  ungrouped images start collapsed.
- For visual rework tickets, attach reference images with the `target__` prefix
  and attach each new render batch under the next attempt prefix, for example
  `attempt-003-uat-rework__front.png`. Keep the existing descriptive filename
  after the double underscore so lightbox captions remain readable.
- The detail view renders attachments as grouped galleries; click an image to
  view it inline, and hover an image to remove it from the ticket
- Detail image lightboxes can crop an attached render into a numbered Feedback
  set. Crop outputs are named with the source + `x/y/w/h` rectangle and carry
  structured attachment metadata with `source_path`, `source_name`, and the
  original-pixel crop rectangle.
- New transitions into `done` require either a verified `commit_hash` or `commit_exempt: true`
- `draft -> analysis` must use the `release_draft` operation and is restricted to director/User plus configured draft-owner roles
- `analysis -> in_progress` is the default handoff from triage/spec to Implementation
- Any state can move to `backlog` to defer work; backlog tickets can be revived to `analysis`
- The default UAT-bound review path is `in_progress -> audit -> DAT (internal state: dat) -> UAT (internal state: user_review, assignee=user) -> director_review -> done`; code-only tickets keep `in_progress -> audit -> director_review -> done`
- Implementer focus is reserved from `in_progress` through agent/director review (`inspection`, `audit`, DAT, and director review). UAT tickets assigned to `user` do not hold an implementer reservation. Finishing/cancelling/parking a reserved ticket auto-activates that implementer's oldest unblocked queued backlog ticket
- Director deferral parks the current ticket as `backlog` with `parked: true`; queued implementer backlog (`parked: false`) is intentionally excluded from idle-stall nudges until it becomes active
- When a ticket is legitimately waiting on another pane, use `scripts/ticket-board-write await-role PGU-N --role perf|inspector|audit|...`; active awaiting markers suppress stall nudges, clear when that role acts on the ticket, and expire after the fallback timeout
- Discretionary idle-stall/backstop nudges route to the director as coordination signals; primary transition/work-delivery notifications still route to the responsible pane
- For new shell-created tickets, use `/home/agent/bin/directorctl ticket-create`; it writes through the board action API.
- Do not hand-write `PGU-N.json` files. The retired JSON store is not a board backend.

Ticket board validation:

- `scripts/ticket-board-test-suite` is the standard one-command validation set
  for ticket-board changes. By default it discovers every
  `tests/ticket_board_*_test.py` and `tests/ticket_board_*_test.sh`, runs all
  runnable suites without fail-fast, and prints per-group and per-suite
  PASS/FAIL/SKIP results plus a summary. Use `--group adjacent` for
  board-adjacent tests outside the `ticket_board_*` namespace, or `--group all`
  for both groups.
- The adjacent group is discovered from test file content, not a manifest: any
  non-`ticket_board_*` standalone test that references `TICKET_BOARD_SOCKET`,
  `TICKET_BOARD_URL`, `board_url`, `--board-url`, `ticket-board-write`, or
  `ticket_board.write_client` is included automatically.
- The runner strips live board/socket and pane-hook environment variables before
  launching each suite, so an ambient pane environment cannot redirect tests to
  the production board.
- Environment-dependent skips are reported by suite name with the reason next
  to the result. Use `--fail-on-skip` when the validation environment is
  expected to have every dependency installed.

PostgreSQL board backend:

- `scripts/ticket_board/schema.sql` is the live board schema.
- `scripts/ticket-board-migrate` records migration filenames by `name` in
  `ticket_board.schema_migrations`; new migrations must use the collision-free
  `pgu<num>_slug.sql` naming scheme. The legacy `270-287` numbered filenames
  are preserved as historical names and backfilled as already-applied when a DB
  was provisioned directly from `schema.sql`.
- Migration apply order is deterministic filename order only. If two unapplied
  migrations genuinely depend on each other, rebase/combine them before merge
  rather than expecting the runner to infer dependencies.
- Tickets are stored in normalized ticket, blocker, comment, and attachment tables.
- The PostgreSQL backend requires psycopg 3 at runtime and for
  `tests/ticket_board_postgres_backend_test.py`. On Arch/CachyOS install it
  with `sudo pacman -S python-psycopg`; for isolated test runs, use a venv
  that can see that package, such as
  `python3 -m venv --system-site-packages /tmp/pgu-ticket-board-venv`.
  `scripts/ticket_board/requirements.txt` records the equivalent pip
  dependency for non-system Python environments.
- `scripts/ticket_board/rbac.sql` creates the login roles for each board pane/service role without setting passwords and grants minimal table/column permissions; only `director` can update `tickets.manually_controlled`
- `tickets.manually_controlled` defaults to `false`; PGU-191 transition/notification triggers must no-op when it is `true` so the director can hand-manipulate exceptional tickets
- The PGU-207 trigger layer enforces workflow gates, maintains `ticket_notification_state`, emits `pg_notify('ticket_board_state_transition', ...)` on state transitions, and exposes `notify_due_nudges()` for the 5-minute pg_cron reminder cadence. The notify listener reconciles missed transition notifications on reconnect so listener downtime does not drop implementation/audit/review nudges.
- Transition notifications are not queued back to the same `ticket_board.caller_role` that caused the transition; the trigger still emits a wake notification for listeners to reconcile state.
- Notification delivery is traced in `ticket_board.notification_trace`; use `scripts/ticket_board/dump_notification_trace.py --ticket-id PGU-N` to inspect enqueue/send/drop/ack/failure history for spurious-ping reports. High-volume diagnostic rows such as claim/requeue/gate-defer are pruned after a short retention window.
