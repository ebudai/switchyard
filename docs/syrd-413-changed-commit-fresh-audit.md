# SYRD-413: a changed commit needs a fresh Audit (diagnosis and production regression)

## Finding: no current-source defect; MEFP has not installed the fix

MEFP-22 had an Audit approval for one commit. The Director routed it back to
Main with MEFP's declared `route director_review -> in_progress`, a plain
move that clears nothing. Main submitted a new commit, and the board went
straight to director_review with `audit_signoff=true`. That is the
changed-commit carry-over that **SYRD-271 (`19132fe`) fixed on main**. When
work leaves implementation carrying a different commit, every review
sign-off is cleared before the gates are walked.

**The fix is intact on current main (`c873b70`).**
`pgu961_syrd271_signoff_follows_commit.sql` is still the newest migration
defining both `enforce_declared_ticket_update` and
`enforce_ticket_workflow_update`. No write can change `commit_hash` ahead of
a submission: every write is transition-bound, namely
- `perform_workflow_action_as`, which sets it in the same UPDATE as the
  state change;
- the legacy `submit_to_audit`;
- `mark_done`.

**Measured (a scratch probe, since the document is tenant data).** Boards
were built the production way: companion roles, `schema.sql`, the real
`ticket-board-migrate`, `rbac.sql`. They used **MEFP's live workflow
document, revision 51**, fetched read-only, and replayed MEFP-22's exact
steps: approval, `route` back, an `audit_prompt` edit, submission of a new
commit.

| release | after submitting the new commit |
|---|---|
| 49abeb4 (MEFP's release in SYRD-274/285, and when MEFP-22 happened) | `director_review`, `audit_signoff=true`: **reproduced** |
| c873b70 (current main) | `audit`/`audit`, `audit_signoff=false` |

**Supporting evidence that MEFP ran a pre-fix release.** On 2026-09-27 the
MEFP Director's `override-move` was refused with "director force_move". That
is the SYRD-275 defect, fixed on main at `65dbe04`, which is also absent
from 49abeb4. SYRD-285 recorded MEFP on 49abeb4 the day before.

## Duplicate triage

SYRD-274 (MEFP-5), SYRD-315 (MEFP-22, 2026-09-27), SYRD-394 (MEFP-22,
2026-09-28) and SYRD-413 (MEFP-22) are one symptom on the same unupgraded
tenant. None needs a separate patch.
- **SYRD-394's "updated ticket commit_hash … and submitted"** was the
  submission itself; no other path writes a commit.
- **SYRD-315's other ask, a sanctioned way to withdraw an approval,**
  already exists. `director_edit` sets `audit_signoff=false` with a
  recorded reason and can never grant one. MEFP's Director used it
  successfully on MEFP-22. An ordinary field edit stays refused.

**The remedy is an upgrade of MEFP** to a release containing `19132fe`
(SYRD-271) and `65dbe04` (SYRD-275). That is for MEFP's team and the
Director; nothing on MEFP was touched here.

## Regression: `tests/changed_commit_fresh_audit_test.py` (13 checks)

SYRD-271's own suite loads `schema.sql` alone. SYRD-275 showed that such a
suite cannot prove what production runs. This regression builds both boards
the production way, with a MEFP-shaped document: the shipped one plus MEFP's
`route` move.

- **49abeb4:** the defect, reproduced.
- **This tree:**
  - the changed commit enters Audit unsigned and cannot be closed from
    there;
  - only Audit's approval of that exact commit moves it on;
  - the same commit resubmitted keeps its review;
  - an ordinary flag edit is refused, `director_edit` withdraws the
    approval, and it cannot grant one.

It passes under `env -i` and in the normal pane environment.

**Mutation.** SYRD-271's rule was removed **from the migration only**,
leaving schema.sql intact. That is the case production runs, and the SYRD-78
class of miss:
- this test **fails** ("the changed commit enters Audit, unsigned:
  director_review … True");
- `signoff_follows_commit_test` **still passes**.

With the rule removed everywhere, both fail.
