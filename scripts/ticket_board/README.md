# PGU Ticket Board

Lightweight local ticket board for Eric and the director.

Launch:

```bash
python3 scripts/ticket-board.py --host 127.0.0.1 --port 8770
```

Shared shell-side ticket creation:

```bash
scripts/directorctl ticket-create \
  --assignee app \
  --state ready \
  --blocked-by "PGU-23, PGU-25" \
  --title "Board: add director as assignee" \
  --body "Allow director-owned action tickets in the assignee enum." \
  --comment-who director \
  --comment-text "Speced + routed to app."
```

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
  "needs_eric_signoff": true,
  "eric_signoff": false,
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

- `assignee`: `main`, `app`, `perf`, `ops`, `audit`, `agent`, `director`, `unassigned`
- `state`: `backlog`, `analysis`, `ready`, `in_progress`, `audit`, `eric_review`, `director_review`, `done`, `cancelled`

Notes:

- `eric_review` is reserved for tickets with `needs_eric_signoff: true`
- `backlog` is for deferred or parked tickets; `analysis` is for active triage/spec work before a ticket is revived into `ready`
- `blocked_by` is a list of ticket IDs the ticket is waiting on; unresolved blockers are any entries whose referenced ticket is not yet `done`
- `implementation` is the director-authored implementation package/spec that must be present before a ticket can enter `ready` or `in_progress`
- `ready` means specced, assigned, and queued for the assignee; `in_progress` means actively being worked
- `audit_prompt` is retained for reference text but is optional in the default workflow
- `commit_hash` stores the verified git commit associated with the ticket when it moves to `done`; it must be on `main`
- `commit_exempt` is an explicit override for non-code/process tickets that should be allowed into `done` without a commit
- `cancelled` is a separate terminal state for abandoned/superseded/won't-do tickets; cancelling requires a non-empty comment reason but no commit/audit sign-off
- `cancelled` is a separate terminal state for abandoned/superseded/won't-do tickets; cancelling requires a non-empty comment reason but no commit/audit sign-off
- `screenshots` is the canonical attachment list; `screenshot` is retained as the first attachment for back-compat with older tools and tickets
- Screenshots referenced by tickets may live in either `/tmp/pgu-frames` or `/home/agent/.claude/pgu-tickets-assets`
- The frame picker reads `/tmp/pgu-frames` as a transient inbox; once attached to a ticket, each selected image is copied into `/home/agent/.claude/pgu-tickets-assets`
- Clipboard-pasted screenshots are normalized to PNG and staged under `/home/agent/.claude/pgu-tickets-assets` before ticket attachment
- The detail view renders attachments as a gallery; hover an image to remove it from the ticket
- New transitions into `done` require either a verified `commit_hash` or `commit_exempt: true`
- `analysis -> in_progress` is not a valid default path; tickets move `analysis -> ready -> in_progress`
- Any state can move to `backlog` to defer work; backlog tickets can be revived to `analysis` or `ready`
- The default review path is `in_progress -> audit -> eric_review -> director_review -> done`, or `in_progress -> audit -> director_review -> done` when Eric sign-off is not required
- Only one unlinked ticket per assignee may be in `in_progress` at a time; linked tickets in the same `parent_id` cluster count as one unit of work, and `ready` is the per-role queue
- For new shell-created tickets, prefer `scripts/directorctl ticket-create` over hand-writing `PGU-N.json`; it uses the same atomic filename reservation as the web app create path, so concurrent creators cannot collide on the same ticket ID
- For direct JSON edits/writes, use `scripts.ticket_store_io.atomic_write_json(...)` or an equivalent temp-file plus `os.replace(...)` flow; the watchdog now watches the store with inotify, so partial in-place writes are treated as malformed until they are replaced by a complete file
- Human shell editing is fine; if a JSON file is invalid, the UI shows a read error banner

PostgreSQL migration foundation:

- `scripts/ticket_board/schema.sql` is the additive PGU-189 schema for a future database-backed board
- The schema is a lossless import target for the current JSON store: normalized ticket, blocker, comment, and attachment tables plus `tickets.source_json` to preserve exact imported JSON
- `scripts/ticket_board/import_json_to_postgres.py --database "$DATABASE_URL" --apply-schema` imports the JSON store into that schema, is safe to rerun, and verifies DB parity against the source JSON after import
- Transition enforcement and notification triggers are intentionally not part of this schema; those are later migration steps
