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
  --state in_progress \
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
  "state": "open",
  "blocked_by": ["PGU-23", "PGU-25"],
  "implementation": "",
  "audit_prompt": "",
  "audit_signoff": false,
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
- `state`: `open`, `in_progress`, `director_review`, `audit`, `eric_review`, `done`

Notes:

- `eric_review` is reserved for tickets with `needs_eric_signoff: true`
- `blocked_by` is a list of ticket IDs the ticket is waiting on; unresolved blockers are any entries whose referenced ticket is not yet `done`
- `implementation` is the director-authored implementation package/spec for the implementer during `in_progress`
- `audit_prompt` is the director-authored handoff text for the audit phase
- `screenshots` is the canonical attachment list; `screenshot` is retained as the first attachment for back-compat with older tools and tickets
- Screenshots referenced by tickets may live in either `/tmp/pgu-frames` or `/home/agent/.claude/pgu-tickets-assets`
- The frame picker reads `/tmp/pgu-frames` as a transient inbox; once attached to a ticket, each selected image is copied into `/home/agent/.claude/pgu-tickets-assets`
- Clipboard-pasted screenshots are normalized to PNG and staged under `/home/agent/.claude/pgu-tickets-assets` before ticket attachment
- The detail view renders attachments as a gallery; hover an image to remove it from the ticket
- For new shell-created tickets, prefer `scripts/directorctl ticket-create` over hand-writing `PGU-N.json`; it uses the same atomic filename reservation as the web app create path, so concurrent creators cannot collide on the same ticket ID
- Human shell editing is fine; if a JSON file is invalid, the UI shows a read error banner
