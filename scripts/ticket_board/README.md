# PGU Ticket Board

Lightweight local ticket board for Eric and the director.

Launch:

```bash
python3 scripts/ticket-board.py --host 127.0.0.1 --port 8770
```

Defaults:

- Store path: `/home/agent/.claude/pgu-tickets`
- Screenshot directory: `/tmp/pgu-frames`
- Local URL: `http://127.0.0.1:8770/`

Store contract:

- One JSON file per ticket in the store directory, named `PGU-N.json`
- The web app re-reads disk on every request; there is no in-memory ticket cache
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
- Screenshots must point inside `/tmp/pgu-frames`
- Human shell editing is fine; if a JSON file is invalid, the UI shows a read error banner
