# SYRD-270: waiting on another board's work: external blockers

## What happened

MEFP-4 (in_progress/ops) had a finished commit, `ed68f13`, that the MEFP board
could not resolve. The board still verified commits against the historical
`/data/git/stellaris-fixpatch.git`, and Switchyard operator ticket SYRD-269 was
to repoint it. The MEFP board had no way to record that wait:

- `blocked_by` accepted only its own tickets ("blocker ticket not found");
- `await-role` accepts only a role with a pane, and `user` is refused.

So Ops opened a Director dependency. The Director could do nothing with it and
cleared it, and Ops opened it again. MEFP-4 collected four Director comments in
fifteen minutes, none with a new action.

## What the board already had

An unresolved blocker is exactly the wait that was needed.

- **Forward promotion** is refused (`enforce_declared_ticket_update`:
  "unresolved blocker prevents forward promotion"), so the owner cannot submit
  and no gate is skipped.
- **Reminders** are suppressed: due, stall and turn-end nudges and the
  unresolved-turn Director handoff all check blockers inline (pgu934, pgu948).
- **Awaiting-role handoffs** are refused, and an existing wait is cleared
  (SYRD-148).
- **The listener** drops idle_reminder, nudge, escalation and transition rows
  for the ticket (`has_unresolved_blockers`).
- **Stage and owner** are kept. Only the Director's `defer` parks a ticket.

The one gap was that a blocker could name nothing but a local ticket.

## The change

**`blocked_by` accepts a qualified reference, `<project>:<PREFIX>-<n>`** (for
example `syrd:SYRD-269`), beside local ids.
- It is normalized to a lower-case project and an upper-case id.
- It needs a `blocked_reason`, like any blocker.
- It is set only by the role that sets blockers: the Director, or the holder
  of `set_blockers` on a declared board. Ops still cannot set one.
- A qualified reference to the board's own prefix is refused ("block on PGU-9
  instead"), because a local ticket's blocker should resolve when that ticket
  is done.

**It never resolves by itself.** No ticket on this board carries that id, so
nothing that moves anywhere touches it, including a ticket called `SYRD-269`
finishing. The ticket asked for exactly this: "do not infer completion merely
because a SYRD ticket moved". The row is unresolved by construction, so every
existing check above treats it as the blocker it is. None of them changed.

**`release-external-blocker <id> --ref project:PREFIX-N --reason "..."`** is
the only way it ends.
- It is Director-only: the same authority as `set_blockers`, composed from
  that capability on declared boards and checked again by the database.
- It removes that one blocker. When none remain, it clears `blocked_reason`.
- It records `External blocker <ref> released: <reason>` as a comment and
  notifies the owner.
- It moves nothing. The owner still submits through every gate.

**`--commit <sha>` on the release** is checked against **this board's own**
commit repository, the same check a submission makes. The release is refused
("unknown commit_hash") until the board resolves it. For MEFP-4 this is the
Director's criterion: the wait ends when the MEFP board actually recognizes
the commit. The comment records the full hash the board resolved.

**Storage.** `ticket_blockers.blocker_ticket_id`'s CHECK admits the union:
`ticket_id_pattern()` or `external_blocker_pattern()`. Local ids stay valid,
and the old constraint is dropped and re-added, not narrowed. `apply_blockers`
skips the "not found" check for external references and inserts them with a
LEFT JOIN. `set_blockers` compares blocker lists using the same
normalization. The new functions are `external_blocker_pattern`,
`is_external_blocker`, `normalize_blocker_ref` and
`release_external_blocker`. They live in `schema.sql` and
`pgu960_syrd270_external_blockers.sql` as identical copies, and the drift
guard passes.

**Interfaces.**
- API: operation `release_external_blocker`.
- CLI: `ticket-board-write release-external-blocker`.
- `set-blockers --blocked-by` help names the form.
- The board and Director skills and the onboarding guide say what the field
  accepts and how the wait ends.

## For MEFP-4, once this is deployed

The MEFP Director runs:
```
set-blockers MEFP-4 --blocked-by syrd:SYRD-269 --blocked-reason "..."
```
MEFP-4 then stays in_progress/ops, silently, and Ops cannot submit. When the
board has been repointed, the Director runs:
```
release-external-blocker MEFP-4 --ref syrd:SYRD-269 --reason "..." --commit ed68f13
```
That is refused until the board can see the commit. After it succeeds, Ops
submits `ed68f13` to Audit.

## Not done here

- **A human operator** is named through the ticket that owns their step
  (SYRD-269 here), not as a pseudo-role. `await-role user` remains refused.
  A wait on a person with no ticket has nothing durable to release against.
- **The release does not check the other board.** The foreign ticket's state
  is exactly what must not be trusted, and the release's condition is this
  board's own.

## Evidence

- **`tests/external_blocker_test.py`: 33 checks.** It replays MEFP-4 on a
  real cluster with a declared workflow. It uses the real `ticket-board-write`
  CLI over the real HTTP server, and two real bare repositories: a historical
  one without the commit and a canonical one with it. It checks:
  - Ops cannot set the blocker, and a qualified reference to the board's own
    prefix is refused;
  - the Director's blocker is normalized, unresolved, and keeps
    in_progress/ops;
  - suppression is differential: the board's stall, turn-end and due-nudge
    generators queue a reminder for the ticket before the wait and nothing
    during it, for Ops or the Director;
  - Ops's `request-dependency` to the Director is refused, and nothing is
    queued;
  - with the commit resolvable, submission is still refused by the blocker
    itself;
  - a local ticket named SYRD-269 reaching done leaves the wait unresolved;
  - Ops cannot release it, a local id is not released through it, and a
    release with no reason is refused;
  - on the historical repository, `--commit` is refused and the wait stands;
  - on the canonical repository the release succeeds. The blocker and reason
    are gone, nothing moved, the comment records the full resolved hash, the
    owner gets one `ticket_update`, and a second release is refused;
  - Ops then submits the real commit and lands in audit, unsigned;
  - upgrade: main's `schema.sql` takes the migration twice, then records an
    external blocker. Its constraint admits both forms, and the database
    itself refuses an own-board qualified reference.
- **Mutation: 13/13 killed**, each by its assertion. Covered: externals
  treated as missing; the SQL own-board refusal; Ops admitted to release;
  the commit check skipped; nothing deleted; nobody told; the reason left;
  a same-named local ticket resolving it; the API rejecting external
  references; the project upper-cased; evidence dropped; no reason required;
  a local id accepted for release. "No reason required" survived the first
  run, because the CLI requires `--reason`. A direct blank-reason release was
  added, and it is killed.
- **Sweep.** 164 suites that touch blockers, the schema, RBAC, migrations,
  the write client, the server or the skills and docs were run serially under
  `env -i`, against `origin/main` (`49abeb4`). Two became red, and both are
  fixed:
  - **`board_skill_test`:** the skill must not name a tenant prefix, and my
    example did. The example is removed.
  - **`director_defer_unaccepted_review_test`:** it asserted that no
    migration sorts after its own. Narrowed to what that protects: no later
    migration redefines its functions. Its upgraded board also stopped at its
    own migration and then applied the current `rbac.sql`, which now grants a
    function pgu960 creates. It now applies the migration tail first, as the
    real runner does: `ticket-board-service.sh` deploy runs migrations, then
    `rbac.sql`. 45 checks.

  Everything else matches main. The 41 suites red on both trees give
  identical per-case results where they have `test_` functions (257 cases,
  failure lines included), and otherwise identical final lines. The
  exceptions are a temp path, and two migration-order checks already red on
  main whose message now also lists pgu960.
