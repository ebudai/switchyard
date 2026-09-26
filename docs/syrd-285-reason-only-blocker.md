# SYRD-285: a blocked_reason is a note, not a wait

## What happened

MEFP-14 (in_progress/ops) could not be deployed until Switchyard's SYRD-284
was independently audited and published. The MEFP Director called
`set_blockers` through the Python write client with `blocked_by=[]` and a
concrete `blocked_reason`, and was told it succeeded. Ops kept receiving
unresolved-work reminders, asked for Director coordination again, and was
correctly refused `set_blockers`. As a workaround the Director routed
MEFP-14 to analysis/director.

## Diagnosis

- **A reason without a blocker row blocks nothing, by design.** Every wait
  the board honours is a `ticket_blockers` row:
  - the reminder generators and the Director handoffs;
  - the listener's drops;
  - forward promotion;
  - the awaiting-role refusal.

  `blocked_reason` is text that explains a blocker. On its own it suppresses
  nothing, and the board card does not show the ticket as blocked. So the
  reminders Ops received were the board behaving as specified.
- **The trap was the operation.** `set_blockers` with nothing to wait on and
  a reason reported success, so a Director could reasonably believe a wait
  had been recorded. The CLI cannot send that shape, because `--blocked-by`
  is required, but the API and the Python client could.
- **MEFP's installed release (49abeb4) had no supported way to say it.**
  External blockers (`project:PREFIX-N`, SYRD-270) arrived after it; on it,
  `syrd:SYRD-284` is refused ("invalid blocker ticket id"). Current main has
  them.

## The supported wait for MEFP-14 (SYRD-270, no new mechanism)

```
set-blockers MEFP-14 --blocked-by syrd:SYRD-284 --blocked-reason "..."
```

This is Director-only.
- **While it stands:** MEFP-14 stays in_progress/ops. Reminders, stall and
  turn-end nudges and escalations are silent. Ops cannot reopen a Director
  handoff, and submission is refused.
- **What ends it:** nothing that happens on the SYRD board, SYRD-284 closing
  included. Only this:
  ```
  release-external-blocker MEFP-14 --ref syrd:SYRD-284 --reason "..."
  ```
  run by the Director once SYRD-284's audited publication and deployment
  prerequisites are actually satisfied. Ops is then woken once and submits
  through Audit as usual.
- **MEFP needs the upgrade first.** MEFP must run a release that includes
  SYRD-270 before this form exists there.

## The change: close the trap, keep the note

- **The `set_blockers` operation refuses a reason with nothing to wait on.**
  Nothing, or only blank entries, in `blocked_by` alongside a non-empty
  reason is refused with: "a blocked_reason alone is a note, not a wait.
  Name it in blocked_by -- a ticket here (PREFIX-N), work on another board
  (project:PREFIX-N), or a person (operator:<name>)".
- **Clearing is unchanged.** No blockers and no reason is still allowed.
- **The note itself is unchanged.** Editing a ticket's `blocked_reason` as
  context (the ticket editor's field) still works and, being a note,
  suppresses nothing. That is now the deliberate, documented semantic, and
  it is covered by the regression.
- **Authority is unchanged.** Ops still cannot set blockers. No
  manual-control path is involved.
- **The database is untouched.** It is a server-side operation check, so no
  migration is needed.
- **Docs.** The Director skill and the onboarding guide say that a reason
  alone blocks nothing.

## Evidence

- **`tests/reason_only_blocker_test.py`: 20 checks.** Boards are built the
  production way, with the real HTTP server and write client and the real
  notify listener.
  - **Installed release (49abeb4)**, whose schema, migrations and runner come
    from `git archive`:
    - an idle owner is reminded;
    - the installed server's reason-only patch lands;
    - Ops is still reminded;
    - `syrd:SYRD-284` is refused.
  - **This tree:**
    - `set_blockers` with a reason and nothing, or only blanks, to wait on is
      refused with guidance and records nothing;
    - a reason kept as a note on the ticket still works and still suppresses
      nothing.
    - `syrd:SYRD-284`:
      - MEFP-14 stays in_progress/ops, and every reminder generator is
        silent;
      - Ops cannot reopen a Director handoff, submission is refused, and Ops
        still cannot set blockers;
      - the Director's release wakes Ops exactly once through the listener,
        nothing repeats, and nothing moves.
    - Clearing blockers and reason together still works.
- **Mutation: 3/3 killed.** Covered: reason-only accepted again; clearing
  refused too; a blank entry counted as a blocker.
- **Sweep.** 234 suites touching blockers, the listener, the write API and
  client, the runner, schema, RBAC, reservation, sign-offs or the skills
  were run serially under `env -i`, against `origin/main` (`4470716`).
  Pass/fail is identical except `ticket_board_active_role_reminder_postgres_test`,
  which was red once on this branch. It uses neither `set_blockers` nor
  `blocked_reason`, and it then passed 3/3 on both trees: a timing flake.
  The suites red on both trees end on the same line, apart from temp paths.
