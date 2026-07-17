---
name: board
description: How to read from and write to the PGU ticket board (scripts/ticket-board-read / scripts/ticket-board-write) and the workflow conventions every pane follows. Use whenever checking, creating, or advancing a ticket.
---

# Ticket board

The board is the team's coordination engine (`http://127.0.0.1:8770`). Every
pane — regardless of which CLI it runs on — uses the same two CLI-agnostic
tools to read and write it: `scripts/ticket-board-read` and
`scripts/ticket-board-write`. This skill covers tooling usage and workflow
conventions only, not per-role duties (that's a separate doc).

## Reading

Use `scripts/ticket-board-read`, never `add-comment` to "peek" at a ticket —
that posts a real comment and notifies the assignee.

- `scripts/ticket-board-read PGU-123` — full ticket (state/assignee, key
  flags, blockers, body, implementation, full comment thread). Bare `PGU-N` is
  shorthand for `ticket PGU-N`.
- `scripts/ticket-board-read comments PGU-123` — just the comment thread.
- `scripts/ticket-board-read queue [role]` — active/review work + backlog in
  serial-focus order, for one role or all roles.
- `scripts/ticket-board-read board [--all]` — one line per ticket grouped by
  state; `--all` includes done/cancelled (hidden by default).
- `scripts/ticket-board-read blockers PGU-123` — `blocked_by`/`blockers`/
  `blocked_reason` only.
- `scripts/ticket-board-read director` (alias `mine`) — items needing director
  attention.
- Add `--json` to any subcommand for raw JSON. The read client is read-only
  HTTP GET — it never mutates state, registers a caller, or opens the write
  socket.

For direct read-only SQL (e.g. an ad-hoc query the CLI doesn't cover), use the
passwordless read-only role:
`postgresql://ticket_board_service@/pgu?host=/run/postgresql` — verified
read-only (writes get `permission denied for table tickets`).

## Writing

Use `scripts/ticket-board-write <subcommand> ...`, never hand-edit ticket
JSON/rows directly. Full subcommand list: `create-ticket`, `file-bug`,
`route`, `force-move`, `override-move`, `start-work`, `inspector-sign-off`,
`eric-sign-off`, `release-draft`, `defer`, `start-task`, `complete-task`,
`audit-sign-off`, `submit-to-audit`, `submit-to-inspection`,
`request-commit-exempt`, `await-role`, `clear-awaiting-role`,
`audit-kick-back`, `eric-reopen`, `cancel`, `inspector-kick-back`,
`mark-done`, `set-manually-controlled`, `set-blockers`, `add-comment`.

The ones you'll use most:

- `create-ticket --title T --body B [--assignee R --state S
  --needs-inspection --needs-eric-signoff]`
- `route --state <s> --assignee <r>` — the normal way to move/assign a
  ticket. `force-move` is a rare override, not the everyday path.
- `add-comment PGU-123 --text "..."`
- `submit-to-audit PGU-123 --commit-hash <sha>` — hand off your completed
  work; requires a real commit hash resolvable against origin (or the
  ambient repo HEAD if the ticket made no code change — say so explicitly in
  a follow-up comment so it isn't mistaken for a real delivery commit).
- `audit-sign-off` / `inspector-sign-off` / `eric-sign-off`
- `audit-kick-back` / `inspector-kick-back` / `eric-reopen --reason "..."`
- `mark-done --commit-hash <sha>`, `defer`, `cancel --reason "..."`,
  `set-blockers`, `set-manually-controlled`

`--caller-role` defaults to `PGU_TICKET_BOARD_CALLER_ROLE`, then the current
`pgu-*` tmux session name, then `director` — pass it explicitly when acting
outside your pane's own role or when you want to be sure, not just when the
default might be wrong.

## Workflow conventions

Stages, in order: `draft -> backlog -> analysis (triage) -> in_progress
(implementation) -> inspection -> audit -> eric_review (UAT) ->
director_review (final sign-off) -> done`, plus `cancelled` as a terminal
side-state from most stages.

- `analysis` is director-owned triage; don't leave a ticket parked there
  silently — advance it or escalate priority.
- `in_progress` is a shared stage: any of `main/app/ops/perf/research` can own
  it, and an implementer submits its OWN work forward through the pipeline
  (`submit-to-audit`/`submit-to-inspection`) — nobody else does that for you.
- `inspection` only fires if `needs_inspection` is set on the ticket;
  `eric_review` only if `needs_eric_signoff` is set — both are optional gates,
  not always part of the path.
- `audit` and `eric_review`/`director_review` are review/sign-off stages owned
  by `audit`/`director` respectively.
- `set-manually-controlled` mutes automated nudges for a ticket — only use it
  on genuinely held/stalled tickets, never on one you're actively working (it
  also blocks the assignee from routing/submitting, not just the nudges).
- The board auto-notifies assignees on relevant transitions — don't stack a
  manual ping on top of a transition that already notified.
- Merge current `origin/main` before validating anything you're about to
  submit — the shared checkout can be stale.
