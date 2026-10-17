# SYRD-602: upstream reports are previewed, then confirmed

## Why
A tenant's panes carry a report token for an upstream board. Before this change,
one `ticket-board-write file-report` call created a ticket there, and MEFP created
SYRD-596, SYRD-597 and SYRD-601 without meaning to.

## The two steps
1. **Preview.** `ticket-board-write file-report --title ... --body ... [--defer]
   [--external-source-ref ...]` writes nothing anywhere. It reads the upstream
   board's public `/api/client-config` and prints:
   - both projects and the caller role;
   - the destination's ticket prefix;
   - the title, requested state, source reference and attachments;
   - a bounded body summary;
   - a digest, and the exact flags to submit it.
2. **Submit.** The same command with `--submit-upstream --expect <digest>`.

## The digest
`report_confirmation.report_digest` is a 24-character SHA-256 prefix, bound to:
- the destination (`project`, `ticket_prefix`);
- the source project;
- the title, body, requested state, source reference and attachments.

Attachments are always none: the report endpoint carries none.

## Where it is checked
**The CLI** refuses:
- a submission whose report or destination no longer matches its digest;
- `--submit-upstream` without `--expect`, and the reverse.

**The upstream board** (`report_confirmation.submit`):
- first checks the token, then the forbidden fields, as before;
- refuses a report with no confirmation, or one that does not match what it
  computes from the payload and its own identity;
- files through `ticket_board.file_confirmed_report` (pgu998). That records each
  confirmation with the ticket it created, so a replay, or simultaneous identical
  submissions, return that ticket and create nothing (`"replayed": true`).

**The preview refuses**, creating nothing, when:
- the report URL is missing, or the credential is missing, unreadable or empty
  (it is read during the preview, and never printed);
- the upstream is unreachable;
- the report board is the project's own board.

The report URL no longer silently falls back to the tenant's own board.

## What does not change
- **Local operations** (create, comment, transition, dependency) never use the
  report URL.
- **The report token's authority** is unchanged. The token never appears in a
  preview, error or log.
- **The board's UI** has no upstream report path; its ticket creation is local.

## Automated reporters
There is no one-call path. No automated reporter exists today. A health or crash
monitor that must report upstream needs its own declared and authorised path on
the receiving board, one an ordinary tenant credential cannot assert. A helper
that fills in the digest for any caller would be the bypass this ticket removes;
Audit's kick-back of 2026-10-17 removed the one the first candidate had.

## Compatibility
- **An older client** against an upgraded board is refused with the two-step
  instructions. That is the point: MEFP's agents run MEFP's release.
- **A new client** against a board older than this change files as before: that
  board does not check or record the confirmation, so it cannot refuse a replay.
