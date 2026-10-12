# SYRD-280: withdrawing an approval is a reasoned Director edit

## What MEFP reported

MEFP-8 ran release 49abeb4:
1. Audit approved commit df71cf4, and the Director integrated it.
2. The Director then routed the ticket `director_review -> in_progress/ops`
   for a README-only correction. The route kept `audit_signoff=true`.
3. Wanting the correction reviewed, the Director tried `edit-fields`, and the
   database refused it: "flag change requires authorized workflow action"
   (SYRD-281).
4. `director-edit` with a reason cleared only `audit_signoff` and kept
   `needs_audit` (SYRD-282).

SYRD-271 already clears carried approvals when a different commit is
submitted, and MEFP's release predated it. What remained was documentation:
the difference between an ordinary field edit and a reasoned withdrawal of an
approval.

## The change

The documentation now says how to withdraw an approval, in two places:
- **The Director skill**, in a new section, "Withdrawing an approval";
- **The Director guide**, next to "Narrate every override".

Both say the same things:
- **A sign-off is a verdict on one commit.** A different commit clears every
  carried sign-off by itself, so a route back for rework needs nothing more.
- **The same commit keeps it.** Resubmitting the approved commit skips the
  review it already passed. When that approval no longer holds, withdraw it
  first, with the command both documents print:

      ticket-board-write director-edit <id> --set audit_signoff=false --reason "<why the approval no longer holds>"

  `inspector_signoff` and `user_signoff` work the same way. The reason is
  recorded and the gate flag stays. `director-edit` cannot raise a sign-off.
- **`edit-fields` cannot change a sign-off on a declared board.** On a board
  with no declared workflow it still lowers one, but without a reason, so use
  `director-edit` there too.
- **Never clear `needs_audit` instead.** That removes the review itself.

No code changed.

## Tests

`tests/director_approval_revocation_test.py`, 11 checks. It runs on a board
with MEFP's own composed workflow (and a second one for the changed-commit
case, since an implementer's slot is held until its ticket is done), plus a
board with no declared workflow. Every write goes through the real
`ticket-board-write`, as that role, and Ops submits from a clone whose origin
holds the commits, as the client requires.

**Main a1bcdad, from a git archive in a child process:** neither document
says how to withdraw an approval. The board answers every step exactly as it
does on this tree.

**What the documents say, measured:**
- **The route back:** MEFP's route back is a plain `move` that clears
  nothing, and it keeps the approval.
- **`edit-fields`:** it cannot lower or raise a sign-off on the declared
  board.
- **`director-edit`:**
  - it cannot create a sign-off;
  - it requires a reason;
  - it is refused to Ops;
  - the documented command, run verbatim with its placeholders filled,
    withdraws the approval, keeps `needs_audit`, and records "Director edit:
    <reason>".
- **Resubmitting:**
  - the same commit after the withdrawal goes to Audit;
  - the same commit with the approval standing skips to director_review, as
    the documents warn;
  - a different commit clears the approval and goes to Audit.
- **The undeclared board:** `edit-fields` lowers the sign-off, unreasoned,
  for the Director and for Ops alike. The documented command works there
  too.

## Evidence

- **Mutation:** four mutants, run through `tests/bounded_run.py mutate`, all
  killed. Each kill was read on its assertion:
  - **The skill points to `edit-fields`:** the documents check.
  - **The guide omits the command:** the documents check.
  - **The guide drops the refusal it quotes:** the documents check.
  - **`director-edit` may create a sign-off** (`schema.sql`): the board no
    longer answers as main does.
- **The sweep:** the 31 suites that read the skills, the onboarding
  documents or `director-edit` were run serially on this tree and on main
  167d73b.
  - Exit codes are identical.
  - The 5 red on both trees (browser and launcher suites that cannot run
    here) have identical normalised output.
- **Main moved during the work:** from 167d73b to a1bcdad (SYRD-418). That
  commit touches none of the skills, the onboarding documents, `schema.sql` or
  the CLI. This suite and its child run of main pass on a1bcdad. The mutants
  and the sweep ran on 167d73b.

## Noted, not changed

On a board with no declared workflow, any caller allowed to edit fields
(Ops, in the test) can lower an approval through `edit-fields`, and no reason
is recorded. That forces more review, not less, so it is not a bypass. But it
is an unrecorded change to review state, and is left for the Director to
decide on.
