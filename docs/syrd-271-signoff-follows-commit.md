# SYRD-271: a sign-off follows the commit it reviewed

## What happened

MEFP-4 went through these steps:
1. Audit approved commit `6d4ee1a`, and the ticket reached director_review.
2. The Director routed it back to Ops for a safety edit.
3. Ops pushed `4f9fd17` and ran `submit-to-audit` with it.
4. The board accepted the new hash and went **straight to director_review
   with `audit_signoff=true`**, Audit's verdict on `6d4ee1a` standing in for
   a review of `4f9fd17` that nobody had done.

MEFP's Audit later reviewed `4f9fd17` by hand. That is MEFP-4's business and
nothing here changes it.

## Why

Two rules, each correct on its own, combined:

- **The route back was MEFP's declared `route director_review → in_progress`,
  a plain `move`.** Moves clear no sign-off. (The canonical
  `director_kick_back` is a `return` with `clear_signoffs`, which would have
  been safe.)
- **The declared executor skips a review stage whose sign-off is already
  set.** Walking the gates, `audit` has `skip_to: dat`, and with
  `audit_signoff` still true it was skipped. `dat`'s gate was closed
  (`needs_user_signoff` false), so the walk ended at director_review.

Nothing tied a sign-off to the commit it reviewed. The undeclared trigger had
the same shortcut ("entering audit already signed goes on to
director_review").

## Reproduced

`tests/signoff_follows_commit_test.py --before` builds a board from the
schema as it shipped (main's `schema.sql`). It takes MEFP-4's exact steps
with MEFP's `route` row and gets `director_review`, `audit_signoff=true` and
the commit `4f9fd17`. The full run repeats that reproduction before checking
the fix.

## The change

**Declared executor.** One rule, applied after the transition's own
`clear_signoffs` and commit adjustments and before the gates are walked.
When work **leaves an implementation stage carrying a different commit**, or
as **any no-code submission**, **every review sign-off is cleared**: Audit,
Inspector and User alike. The ticket enters review for exactly what it
carries. A no-code submission has no commit to prove it is the same work, so
it is always treated as new.

**Resubmitting the same commit** keeps its review, because that exact work
was seen.

**Undeclared trigger.** The same rule on leaving `in_progress` with a
changed commit. The undeclared `submit_to_audit` already cleared Audit's and
the Inspector's sign-offs but **never the User's**, and entering user_review
already signed skips it the same way. All three are now cleared. Only
sign-offs **carried** from the old row go. A sign-off granted by the same
write stays: several suites stage "inspected, then submitted" in one
statement.

**Both boundaries.** The server hands the commit to these same functions:
`perform_workflow_action` on declared boards, `update_ticket` patches on
undeclared ones. So the rule lives in the database and is tested there. The
MEFP replay's resubmission goes through the real HTTP server.

**Migration.** `pgu961_syrd271_signoff_follows_commit.sql` redefines
`enforce_declared_ticket_update` and `enforce_ticket_workflow_update` whole.
`schema.sql` carried two different bodies of the undeclared trigger: a stale
one at the top, and the pgu928 one that a fresh board ends up running. Both
places now hold the same new body, so the drift guard, which reads the first
copy, compares what actually runs.

## Later review flags and a changed commit: `mark_done`

The Director asked whether any later review flags can survive a changed
commit. One transition can still change the commit after review: `mark_done`
records whatever commit the Director gives, and `audit_signoff` stays set.

**This is left as is, deliberately.** On this board the Director closes on
the **integration commit**, not the audited candidate, whenever main has
moved:
- SYRD-163: the audited patch cherry-picked, with the patch-id matched;
- SYRD-194: a two-parent merge;
- SYRD-226: a cherry-pick.

Of 220 done SYRD tickets that carry a commit, 38 closed on a commit no Audit
comment names. My first version refused `mark_done` with a different commit
behind a sign-off, and it would have broken exactly that practice. It was
removed. The test pins the decision: closing on an integration commit stays
allowed.

If the Director wants the board to check that link, the natural rule is:
the done commit must be the audited one, or carry the same patch-id. That
needs git at the board, like the existing commit check, and a decision about
merges. It belongs in its own ticket.

## Not changed

- **MEFP's `route` stays a `move`.** Moves are for scheduling. The rule
  lives with the commit, not with one tenant's transition table.
- **MEFP-4 itself.** Its Director asked Audit for an explicit review of
  `4f9fd17` before closing, independent of this fix.

## Evidence

- **`tests/signoff_follows_commit_test.py`: 31 checks** on real PostgreSQL.
  - **Before:** on main's `schema.sql`, MEFP-4's exact sequence lands in
    director_review with `audit_signoff=true` for `4f9fd17`.
  - **After, MEFP-4 through the real HTTP server:** the changed commit enters
    audit/audit unsigned and cannot be closed from there. Audit's approval of
    `4f9fd17` moves it on, and it closes.
  - The same commit resubmitted keeps its review.
  - Closing on an integration commit stays allowed.
  - With inspection on the path, the inspector's and Audit's sign-offs both
    go.
  - A User's sign-off, taken the real route (Audit, DAT, User, routed back),
    is cleared.
  - A no-code submission after an audited commit is reviewed, and so is a
    second no-code submission after the first was approved.
  - Undeclared board:
    - an override-carried Audit sign-off is cleared on resubmission;
    - so is a User's sign-off, which `submit_to_audit` never cleared;
    - a sign-off granted in the same write stands.
- **Mutation: 6/6 killed** for the shipped rule. Covered: implementation
  keeping old sign-offs; a repeat no-code submission keeping them; only
  Audit's cleared; the same commit treated as new; the undeclared branch
  removed; a sign-off granted in the same write wiped. The first design's
  close-time refusal had its own mutants, all killed. It was withdrawn for
  the reason above, not for want of tests. The first runs also exposed three
  gaps, now closed:
  - the repeat no-code hole;
  - the User sign-off gap on the undeclared path;
  - a condition that could never be reached.
- **Sweep.** 181 suites touching sign-offs, workflows, submission,
  `mark_done`, the schema or migrations were run serially under `env -i`,
  against `origin/main` (`d327858`).
  - **`director_defer_unaccepted_review_test`** asserted that pgu959 owns
    `enforce_declared_ticket_update`; pgu961 now does. It now asserts what it
    protects: the copy an upgraded board runs, and schema.sql's, still
    contain SYRD-263's parking exemption byte for byte. 45 checks.
  - **`ticket_board_rbac_test`** closes an audited ticket that has no commit
    by supplying one at `mark_done`. It was red under the withdrawn refusal
    and is green now.
  - **`ticket_board_postgres_triggers_test`** (red on main) stopped earlier
    under the first version, which cleared a sign-off granted in the same
    statement. With carried-only clearing it fails exactly where main does.
  - **`ticket_board_declarative_workflow_test`** was red once on main in the
    sweep, then passed twice there and on this branch: a flake on main.

  Everything else matches main. Two already-red migration-order checks now
  also list pgu961 in their message.
