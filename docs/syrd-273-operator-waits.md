# SYRD-273: waiting on a person: `operator:<name>`

## What happened

MEFP-2 (in_progress/ops) needed Eric, who was at work, to run an
already-reviewed 4.5.1 export script after work. Nothing else could happen
until then. The board had no honest way to record that:

- **`await-role MEFP-2 --role user`** is refused ("invalid awaiting_role:
  user"). `user` is a declared, active role with no pane target, and a wait
  needs a pane to hand off to.
- **`request-dependency --role director`** hands the Director a "new
  handoff". The Director can do nothing with it and clears it. Ops, then
  looking stalled, asks again, and the same non-decision is delivered over
  and over.
- **SYRD-270's external blocker** needs another board's ticket, and there is
  none. Inventing one would be false provenance, as the Director said.

## Reproduced

`tests/operator_wait_test.py --before` runs on the schema as it stood before
this change. It shows:
- `await-role user` refused;
- three rounds of request → the real listener delivers → the Director
  clears → request again. **Each round delivers another "New handoff" to the
  Director's pane**;
- `operator:eric` refused as a blocker.

## The change

A blocker is already the wait needed: it keeps the stage and owner,
silences every reminder, handoff and escalation, refuses forward promotion,
and clears the dependency handoff it replaces (SYRD-148, SYRD-270). The only
thing missing was a blocker that names a person. **`operator:<name>`** is
one.

- **Setting it.** `set-blockers MEFP-2 --blocked-by operator:eric
  --blocked-reason "..."` is Director-only, like any blocker. It is
  normalized to lower case.
- **Not a project.** `operator` is reserved and is never read as a project.
  A name is letters, digits and underscores, so it is never shaped like a
  ticket id: `operator:PGU-3` is refused.
- **It never resolves by itself.** It is released only by the Director, with
  `release-external-blocker MEFP-2 --ref operator:eric --reason "<Eric's
  result>"`. The required reason is where the result is recorded, as
  `External blocker operator:eric released: <result>`. The owner gets one
  notice, and nothing moves.

**Storage.** The migration is `pgu962_syrd273_operator_waits.sql`. It
redefines `external_blocker_pattern()` and `normalize_blocker_ref()`,
identical to `schema.sql`, and **rebuilds the blocker CHECK**. A board
upgraded through pgu960 reads the function, but one provisioned fresh from
`schema.sql` carries the inline literal, which a function change cannot
widen. The upgrade test caught this on the first run. `schema.sql`'s inline
literal admits the same union. The Python layer mirrors the pattern and the
normalization.

**Docs.** The board and Director skills and the onboarding guide name the
form. Ops asks the Director once and is told once when it is released. The
Director records the person and never invents a ticket reference.

## For MEFP-2, once deployed

1. Ops keeps the one `request-dependency --role director` it already made.
2. The MEFP Director records `operator:eric`, which closes that handoff.
   From then on nothing repeats.
3. When Eric reports, the Director relays the result as the release reason.
4. Ops is woken once and continues, through Audit as usual.

## Also fixed: reproductions that stopped reproducing

SYRD-270's and SYRD-271's tests built "the schema before this change" from
`merge-base HEAD origin/main`. That stops meaning "before" once the change is
merged. **`signoff_follows_commit_test` is red on main today**: its
reproduction runs against the fix. All three tests now use one helper,
`schema_function_drift.schema_before(migration)`: the schema at the parent of
the commit that added the migration. The SYRD-263 suite already worked this
way.

## Evidence

- **`tests/operator_wait_test.py`: 23 checks.** It uses real PostgreSQL, a
  declared workflow and the real `TicketBoardNotifyListener`, whose fake
  panes witness only what they are sent.
  - **Before:** the refusal, three rounds of repeated Director handoffs, and
    no operator form.
  - **After:**
    - Ops asks once and the Director hears it once. Ops cannot record the
      blocker itself.
    - The Director records `Operator:Eric`, normalized to `operator:eric`;
      MEFP-2 stays in_progress/ops and the awaiting handoff is closed.
    - Ops is told once that the wait is recorded.
    - With the rest of the handoff schedule brought due and every reminder
      generator run, nothing is delivered.
    - Ops cannot reopen the handoff or submit.
    - `operator:PGU-3` is refused.
    - A release needs a reason, and Ops cannot release.
  - **Release:** Eric's result is recorded verbatim, nothing moves, and the
    listener wakes Ops exactly once. Nothing repeats after that. Ops then
    submits into Audit, unsigned.
  - **Upgrade:** a pre-change board takes the migration twice, records a
    person, and its database refuses a ticket-shaped name.
- **Mutation: 7/7 killed.** Covered:
  - in the database: no operator form, the name not lower-cased, a
    ticket-shaped name allowed, the constraint not rebuilt;
  - in the API: no operator form, and a person not lower-cased.
- **Sweep.** 220 suites touching blockers, the listener, awaiting-role,
  schema, RBAC, migrations, sign-offs, workflows or the skills were run
  serially under `env -i`, against `origin/main` (`19132fe`).
  - **Pass/fail is identical** except `external_blocker_test`: red on main
    because of its merge-base "before" schema, green here.
  - **`signoff_follows_commit_test`** (not in that list) is red on main for
    the same reason, confirmed, and green here.
  - **`claude_permission_hook_test`** is red on both trees. It gave a
    different message once on main, then an identical one twice on each
    tree.
  - **Otherwise**, the suites red on both trees end on the same line, apart
    from temp paths and the migration-order checks that already fail on main
    and now also list pgu962.
