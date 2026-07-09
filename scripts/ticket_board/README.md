# PGU Ticket Board

Lightweight local ticket board for Eric and the director.

Launch:

```bash
python3 scripts/ticket-board.py --host 127.0.0.1 --port 8770
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
  "screenshot": "/tmp/pgu-frames/frame_0.png",
  "assignee": "main",
  "state": "open",
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

- `assignee`: `main`, `app`, `perf`, `ops`, `audit`, `unassigned`
- `state`: `open`, `in_progress`, `audit`, `eric_review`, `done`

Notes:

- `eric_review` is reserved for tickets with `needs_eric_signoff: true`
- Screenshots referenced by tickets may live in either `/tmp/pgu-frames` or `/home/agent/.claude/pgu-tickets-assets`
- The frame picker reads `/tmp/pgu-frames` as a transient inbox; once attached to a ticket, the image is copied into `/home/agent/.claude/pgu-tickets-assets`
- Clipboard-pasted screenshots are normalized to PNG and staged under `/home/agent/.claude/pgu-tickets-assets` before ticket attachment
- Human shell editing is fine; if a JSON file is invalid, the UI shows a read error banner
