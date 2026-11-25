# SYRD-875: a resubmitted review returns to the reviewer who kicked it back

## What happened (SAPI-148, release 623c34e5)

Audit on SAPI has two owners, `audit` and `audit2`.

1. 13:15:05Z: round 1 was submitted and landed with `audit`, the first owner. At 13:15:37Z the SYRD-745 balancer moved
   it to `audit2`, which was idle.
2. 13:40:31Z: `audit2` kicked it back with six findings.
3. 14:05:18Z: round 2 was submitted and landed with `audit` again. `audit` signed it off at 14:29:40Z: "audit2 reviewed
   round 1. I take round 2."

The User: "I think we should stick with the same auditor for the ticket instead of whomever is available."

**Why:** every entry into a review stage that names no owner gave the ticket the stage's first owner
(`enforce_declared_ticket_update`). SYRD-745's re-review rule only kept the balancer from moving a review away from a
reviewer that had already been handed it. Nothing sent the rework back to that reviewer in the first place.

## The rule now

**A resubmission returns to the reviewer who kicked it back.**
- Each kick-back out of a review stage that more than one role owns (a declared `return` out of it, such as
  `audit_kick_back` or `audit_documentation_kick_back`) records the owner that held the ticket, in `review_returns`.
  Any other exit from the stage (a sign-off) ends the record. A stage with one owner records nothing.
- Work re-entering that stage with no owner named lands with that reviewer when it can take it. A first submission has
  no such reviewer, and lands with the first owner as before, where the balancer may move it.
- An owner named on the entry is kept.
- It applies to every shared review stage, Audit and any other.

**That reviewer keeps it while busy.** The rework queues behind its current review. The balancer never moves it
(`review_return_held`), whether or not it has been handed over in this round. SYRD-745's re-review rule reads the
hand-offs, so it does not cover a reviewer that kicked a ticket back without being handed it (for example, it acted on
a reassignment before the notice arrived). This rule does.

**The Director's placement still wins** (SYRD-873). The Director reassigning the rework, to another owner or back to
the same one, is a placement. The balancer never moves a placed review, and the fallback below never moves one either.

**Fallback, said out loud.** When the reviewer cannot take the rework, it lands with the stage's first owner as a first
submission does, and may be balanced. The ticket says why in one line: "Review return in audit: audit2 kicked this back,
but cannot take the rework (its provider is out of quota), so it lands with audit, the first owner, and may be
balanced." There are two checks:
- **At the submission, by the board** (`review_return_unavailable`):
  - it no longer owns the stage;
  - it is not an active role;
  - it is not registered;
  - its provider account is held;
  - its provider is out of quota.

  A work pause is not a reason: it holds every reviewer alike, and the rework waits for its own reviewer.
- **On the next listener pass, before the hand-off** (`review_return_fall_back`). The listener alone sees whether the
  reviewer's registered provider process is still running. It gives the return up if:
  - that process is gone;
  - or the reviewer has since become unavailable for one of the board's reasons.

  A rework already handed to its reviewer in this round is never given up, and a placed one is never moved.

**A saved conversation it cannot restore does not park the rework.** This is MEFP-1234's shape: under a pull policy, an
ephemeral reviewer bound its conversation of round 1 to the ticket, and its runtime has no verified in-session resume
(Codex, agy, Hermes). When the rework's hand-off to that same reviewer would park, `resolve_ticket_context_restore`
instead:
1. records on the ticket that the review starts fresh, and why;
2. sets that reviewer's records of the old conversation aside, whole, in `fresh_review_archive`;
3. queues the hand-off "… is now assigned to you for a fresh review of the exact candidate <commit>; the earlier findings
   are in its comments".

The parked notice is dropped. The new hand-off is a first one: the pane is cleared only at an idle boundary, never
mid-turn, the review is delivered once, and the new session is bound. This happens once a round. If the fresh hand-off
fails as well, it parks and is the Director's, as in SYRD-873, and never loops. This is SYRD-873's `force-fresh-context`, which now
shares the helper (`set_aside_review_conversation`). A restore that is attempted and fails (a runtime that can resume)
starts fresh the same way.

**Why the board does this itself, rather than leaving it to the Director:** the User asked for the same auditor, and
that reviewer's findings are on the ticket. A fresh review of the exact candidate changes no stage, owner, candidate or
sign-off, so there is nothing for the Director to decide. A review that reaches a reviewer any other way and cannot be
restored, such as one an older listener balanced there, still parks and is the Director's (SYRD-873).

**Unchanged:**
- exact-candidate and independent-review provenance;
- delivery once;
- a busy pane is never interrupted or cleared;
- the kick-back still returns the work to its implementer (`audit_return`).

## Migration

`pgu1060_syrd875_review_return.sql`:
- **Adds:**
  - `review_returns`;
  - its trigger `tickets_z_review_return` (`record_review_return`);
  - `review_return_reviewer`, `review_return_held`, `review_return_unavailable`, `set_aside_review_conversation`;
  - the listener-only `review_return_fall_back`.
- **Replaces, each with the same signature:**
  - `enforce_declared_ticket_update`;
  - `balance_review_stage_skipping`;
  - `resolve_ticket_context_restore`;
  - `request_fresh_ticket_context`.

`schema.sql` carries the same. `rbac.sql` grants the listener `review_return_fall_back`. `schema_compatibility` declares
it compatible with older code. An older listener never calls the fallback, so on such a board a rework returned to a
reviewer that has stopped waits for that reviewer.

## Tests

`tests/review_pane_rig.py` is the board both review suites run on, moved out of `review_recovery_test`:
- production-built, with Audit owned by `audit` (Claude) and `audit-2` (ephemeral, Codex) under a pull policy;
- the real server and listener, with recording panes.

It also gives a provider hold and a gone provider process.

**`tests/review_return_test.py`**, main first, from a git archive:
- **Main:** round 2 of a review `audit-2` kicked back lands with `audit`, and `audit-2` is sent nothing (SAPI-148).
- **This tree:**
  - round 2 lands with `audit-2` at once. Its Codex conversation cannot be restored, so `/clear` and then exactly one
    hand-off naming the exact candidate. Never a `/resume`, nothing parks, and the Director is told nothing. The ticket
    records the fresh review, and the old records are archived. A fresh review happens once a round: with its new
    conversation displaced and the rework moved away and back by the Director, it parks with one Director notice. Once
    handed over, the rework stays with `audit-2` even when its process goes. A sign-off ends the record.
  - **Busy keeps it:** a rework back with busy `audit-2`, which kicked it back without ever being handed it, queues
    behind its current review. Idle, empty `audit` is not given it. Freed, `audit-2` is handed it once.
  - **The Director wins:** moved by the Director to busy `audit`, the rework stays there while `audit-2` is idle and
    empty. Placed back with `audit-2`, it stays there when `audit-2`'s process goes.
  - **Fallback:** returned to an `audit-2` whose process is gone, the rework goes to `audit`, which is handed it once,
    and the ticket says why.
  - **The record:** Inspection, owned by one role, records no kick-back, and an owner named on entry is kept. The
    board's own reasons are checked one by one: not an owner, not active, not registered, account held, out of quota.
    A work pause is not a reason.

**SYRD-877 pins two more claims** in `review_return_test`, each on its own board and on this tree only (main has the
same code):
- **The fallback acts only on a rework that is with the reviewer that kicked it back.** A rework `audit-2` kicked back
  re-enters naming another owner, whose provider process then goes. It stays with that owner, and no other reviewer is
  handed it. No "Review return" line is written, and `audit-2`'s record is untouched. One variant names `audit-3`, a
  third owner the rig adds. The other names `audit`, the first owner, where a false line and the lost record would be
  the only symptoms.
- **A fresh review happens once per round.** Codex `audit-2` kicks the same ticket back twice, and its pane moves to
  another ticket each time. Round 3 starts fresh as round 2 did: `/clear`, then one hand-off naming its own candidate,
  with no park and no Director notice. That holds although round 2's fresh review is already archived.

Two mutants fail by an assertion: the fallback's join without `rr.role = t.assignee`, and the once-a-round guard
without its `archived_at` bound. The rig takes `extra_reviewers` for the third owner.

**`tests/review_recovery_test.py`** (SYRD-873) holds the kick-back reviewer's provider at the two resubmissions whose
landing it reads, so they land with `audit` as on main. It also checks each fallback line. The second of those has
`audit`, the first owner, giving its own return up.

The rig's `settle` now allows 20 more deliveries on each pass. The listener's cap counts every delivery it has ever
made, so a long scenario used to stop delivering after the 20th.

## Mutation

There are 33 mutants, and each is killed by an assertion. `review_return_test` runs first, and `review_recovery_test`
only when a mutant survives it. The four landing-fallback mutants (E02, E04, T05, T06) and S02 (every parked review
starts fresh) are killed by `review_recovery_test`.
- **The landing:** the return ignored; an unavailable reviewer still given the rework; a named owner overridden; the
  fallback reason not passed to the trigger.
- **The record:** a kick-back not recorded; every exit recorded; single-owner stages recorded; a sign-off keeping the
  record; the landing fallback silent, or keeping the return.
- **The rules:** `review_return_held` ignoring the owner; the balancer ignoring returns.
- **The listener's fallback:** "not running" ignored; a placed rework moved; a handed-over rework given up; the return
  kept; no comment; the first owner not told; nothing moved; the listener never calling it; every eligibility reason
  counted as "not running".
- **The fresh review:** a returned rework parking; every parked review starting fresh; no comment; no set-aside; session
  clears kept; no hand-off queued; more than one fresh review a round.
- **The reasons:** each of the board's five reasons dropped.
