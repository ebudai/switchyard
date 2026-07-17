# PGU-504 Board UI Functional Audit

Date: 2026-07-17

## Harness

Added `tests/ticket_board_ui_harness.py` as reusable Playwright support for board UI tests:

- launches a real `TicketBoardServer` on localhost with the production HTML/JS
- uses an in-memory ticket store so CI does not mutate the live board database
- supports uploads, thumbnails, image resolution, ticket creation, edits, comments, blocker edits, merge, cancellation, UAT sign-off, and serial-focus queue behavior
- exposes API-backed assertions through `/api/board` and `/api/tickets/<id>`
- loads Playwright from the repo venv or `/tmp/pgu-playwright-venv`

Added `tests/ticket_board_ui_audit_test.py` as the first broad functional sweep.

## Covered Controls

The Playwright audit clicks real UI controls and verifies persisted state through the board API:

- create-ticket form: title, body, assignee, Needs UAT, Needs inspection, Regression, numbered feedback file attachment, feedback missing-number error, pasted image attachment
- visibility filters: Show Deferred, Show Done, Show Cancelled
- detail modal: title edit, parent ticket edit, implementation save, audit notes save, commit hash save
- detail toggles: Requires UAT, Needs inspection, Regression
- comments: author/default path, urgent checkbox, Add Comment
- blockers: picker, Add Blocker immediate persistence, Remove Blocker, blocked card rendering
- attachments: detail file upload, target-set display, lightbox open/close, remove attachment
- workflow actions: Advance to Implementation, Cancel Ticket, UAT Sign Off
- serial-focus behavior: second app ticket is deferred while app is busy, then auto-promotes when the active app ticket completes
- mobile behavior: collapsible New Ticket/columns and no document-level horizontal overflow at 390px width

## Live Board Check

Eric's specific PGU-502/PGU-503 blocker attempt was checked against the live board at `http://127.0.0.1:8770/`.

Current live state:

- `PGU-502` is `done`
- `PGU-503` is `in_progress`, assigned to `ops`

Result: `PGU-502` does not appear in the `PGU-503` blocker picker. That matches current product rules: terminal tickets (`done`, `cancelled`) are intentionally not selectable as blockers. The harness separately verifies the current PGU-505 non-terminal blocker flow by adding `PGU-502` with the `Add Blocker` button, asserting immediate persistence/rendering, then removing it with `Remove Blocker PGU-502`.

The focused blocker browser test also covers the multi-blocker removal edge case: with two blockers present and the Blocked Reason textarea locally cleared but unsaved, removing one blocker preserves the persisted blocked reason for the remaining blocker and does not raise a page error.

The reusable in-memory upload path mirrors the production PGU-502/PGU-503 attachment filename validation. The browser audit now checks that an unnumbered `Feedback` upload surfaces `feedback upload set requires a feedback number`, then verifies a numbered feedback upload preserves a sanitized original filename, de-duplicates a repeated filename with `-2`, and strips a traversal-style filename down to the sanitized basename under the `feedback-007-phone-proof__...` prefix.

The blocker-card assertion now uses a retry-safe browser render check after the API has persisted the blocker update. The flake exposed a stale-load rendering race: an older `/api/board` response could finish after a newer load and overwrite the card DOM with stale blocker state. The frontend now tracks a `loadSequence` and discards superseded `loadBoard()` responses, and the test waits for the clicked `updateTicket()` promise before checking the rendered blocker card.

## Findings Filed

- `PGU-507` - Board detail draft restore can clear saved field values after live reload
- `PGU-508` - Board UI lacks a manual-control toggle despite `set_manually_controlled` action
- `PGU-509` - Board UI has no ticket search control

## Validation

Commands run:

```bash
python3 -m py_compile tests/ticket_board_ui_harness.py tests/ticket_board_ui_audit_test.py
python3 tests/blocked_by_frontend_test.py
python3 tests/blocked_by_frontend_browser_test.py
python3 tests/ticket_board_ui_audit_test.py
for i in $(seq 1 15); do python3 tests/ticket_board_ui_audit_test.py || exit 1; done
```

Result: compile passed, the focused PGU-505 blocker tests passed, the single audit run passed, and the audit test passed 15/15 consecutive runs after merging current `origin/main`.
