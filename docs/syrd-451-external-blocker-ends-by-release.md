# SYRD-451: a person's wait ends by its release, and by nothing else

## What MEFP reported

MEFP-9 was in DAT, owned by the Director, waiting on Eric to decide whether
to prepare a controlled diagnostic event test or defer it. The installed
Director and board skills (snapshot 4470716) said to record that as:

    ticket-board-write set-blockers MEFP-9 --blocked-by operator:eric --blocked-reason "..."

On 2026-09-28 the MEFP board refused it: "invalid blocked_by ticket id:
operator:eric". `request-dependency --role user` was refused too. The
Director held the ticket with manual control instead, which mutes nudges and
bypasses every gate.

## Re-evaluated against main eb7f888

**The board was behind its skill.**
- `operator:<name>` is SYRD-273 (a8a3e06, 2026-09-25).
- The skill snapshot 4470716 (2026-09-26) contains it.
- Run from a git archive of a8a3e06's parent, a board refuses
  `--blocked-by Operator:Eric` with exactly MEFP's message. Every release since
  accepts it.
- MEFP's board unit is inactive as of this ticket, so its release was not
  read. It was not started or touched.

**The User wait does not replace it.** SYRD-549 lets a declared role, the
Director included, record a wait on the declared User. That wait mutes
reminders for its four-hour window and then lapses. The suite asks the board
three days on: the User wait no longer holds the ticket, and the operator
blocker still does. A person is also not necessarily the User, and a board
with no declared workflow cannot await the User at all. So the skills'
`operator:<name>` contract stands, and its text is correct.

**What main still got wrong was removal.** The contract says an external
blocker, meaning another board's work or a person:
- never resolves by itself;
- is ended by `release-external-blocker`, whose reason is where the person's
  answer is recorded.

`apply_blockers` says the same in a comment: "only release_external_blocker
removes it". But it replaces a ticket's whole list, so a `set-blockers` that
named other blockers, or the API's clear (which the web UI sends), dropped the
wait anyway:
- no reason, and no record of the answer;
- the same on a legacy board;
- the release then failed, with nothing left to release.

## The change

- **Migration `pgu990_syrd451_external_blocker_ends_by_release.sql`**, with
  the same text in `schema.sql`: `apply_blockers` refuses to drop a standing
  external blocker. The refusal names the release:
  "external blocker operator:eric stands until it is released:
  release-external-blocker MEFP-9 --ref operator:eric --reason "<what
  happened>"".
- **What still works:**
  - adding a blocker beside it, by naming both;
  - dropping a blocker on this board, which resolves itself;
  - the release itself.
- **Why `apply_blockers`:** it is the one writer that replaces a ticket's
  blockers. Its other callers create tickets, which have none yet. The
  signature is unchanged, so no grant moves.
- **The CLI help** for `set-blockers --blocked-by` and
  `release-external-blocker --ref` now names `operator:<name>`.
- **Unchanged:**
  - only the Director sets or releases a blocker, and the person named gains
    no authority;
  - the blocked ticket keeps its stage and owner;
  - its gate refuses forward promotion;
  - its reminders and escalations are silent.

## Tests

`tests/operator_person_wait_test.py`, 15 checks.

**The boards:** a production-built board with MEFP's own composed workflow
(`legacy_workflow_equivalence_test.mefp_plan`), and a board with no declared
workflow.

**The harness:**
- every write through the real `ticket-board-write`, as that role, or the
  board's HTTP API as the web UI calls it;
- the real listener, driven one loop iteration at a time, over the real gate
  and real hook files;
- each pane role registered with a runtime assignment naming a live process.

**The release before SYRD-273:** MEFP's refusal, exactly.

**Main and this tree alike:**
- **The wait:** the Director records Eric's wait on its own DAT ticket, with
  no fake ticket and no manual hold. The ticket keeps its stage and owner.
- **Authority:** `main` cannot set it, the User cannot release it, and
  `operator` is not a caller.
- **The gate:** the DAT sign-off is refused while it stands.
- **Quiet:** the Director's turns end again and again, past the grace, and
  nothing is sent about MEFP-9: no prompt and no escalation. The control is
  MEFP-13, in the same stage for the same owner with nothing recorded, and it
  is prompted.
- **Three days on:** the blocker still holds the ticket quiet, and a User wait
  has lapsed.
- **The release** records Eric's answer, and the sign-off then proceeds.

**Main:** replacing or clearing the blockers drops the wait with no record,
and on the legacy board the release then has nothing to release.

**This tree:**
- **Replacing or clearing:** refused, naming the release. The wait stands, on
  both boards.
- **Other kinds:** another board's work stands the same way, and a blocker on
  this board is still dropped freely.
- **Keeping it:** naming it (as `Operator:Eric`) beside a new blocker keeps
  it. So does the database alone, given ` Operator:ERIC `.
- **The legacy board:** the release works there after the refused clear.
- **The CLI help** names the person form for both verbs.
- **Parity and upgrade:** `schema.sql` installs what the migration does, and a
  board built on main takes the migration in place.

## Evidence

- **Mutation:** six mutants, run through `tests/bounded_run.py mutate`, all
  killed. Each kill was read on its failing assertion:
  - **"An external blocker is dropped freely":** the replace and clear
    checks.
  - **"Only a person's wait stands":** the cross-board check.
  - **"Every blocker stands, local ones too":** the local-blocker check.
  - **"The new list is compared unnormalised":** the database-alone check. It
    was first caught by parity alone, because the API normalises references
    before the database sees them, so the database-alone call was added.
  - **"schema.sql: an external blocker is dropped freely":** parity.
  - **"The CLI help omits the person form":** the help check.
- **Main moved during the work:** from 450b786 to eb7f888 (SYRD-543, plan
  role derivation and teardown). The suite and its child runs of main and of
  the release before SYRD-273 pass on eb7f888. The mutants ran on 450b786.
  SYRD-543 does not touch `apply_blockers`, `schema.sql`, the migrations or
  `write_cli.py`.
- **The sweep was stopped at the Director's direction.** A serial sweep of
  184 related suites against main was started. The Director stopped it,
  judging the focused regression and the targeted mutants sufficient. It had
  run 9 suites on this tree and none on main, so it supports no comparison,
  and none is claimed.
