# SYRD-873: the review balancer respects the Director, and a parked review is the Director's

## What happened (MEFP-1234, 2026-11-24)

Audit on MEFP has two owners, `audit` and `audit-2`. `audit-2` is an ephemeral Codex reviewer, and the board runs a
pull policy.

1. `audit-2` reviewed a candidate and kicked it back. Its first hand-off had bound that conversation to the ticket.
   Its pane was then cleared for another ticket.
2. The rework came back to Audit, landed with `audit`, and waited behind `audit`'s current review.
3. The SYRD-745 balancer moved it to `audit-2`, which was live, idle and holding nothing in the stage.
4. That hand-off needed `audit-2`'s saved conversation restored. Codex has no verified in-session resume, so the
   delivery parked it (SYRD-540): nothing was sent, and the Director was told once, in the implementer-pull wording
   ("defer and admit again").
5. The Director reassigned it to `audit`, which was busy. 23 seconds later the balancer moved it back. Nothing recorded
   that the Director had placed it, `audit` was busy, `audit-2` was idle, and the ticket was not parked for `audit`.

## The rules now

**The Director's placement is kept.**
- A same-stage owner change the Director makes in a review stage is recorded in `review_placements`. The trigger is
  `tickets_z_review_placement`, and the actor is the delegated one when the board UI lent the Director's authority.
- `review_placement_current` holds while the ticket stays in that stage, from a placement made after it entered the
  stage. Leaving the stage ends the placement; the Director moving it again replaces it. Its owner meanwhile is the
  one the Director chose: nothing else changes the owner of a placed review.
- The balancer runs as `board`, so its own moves are never placements.
- The balancer never moves a currently placed ticket. A busy chosen owner keeps it, and it is delivered when that
  owner frees up.

**Live and idle is not eligibility.** Before offering owner R a stage's waiting reviews, the listener
(`review_balance.would_park`) skips each ticket T whose hand-off to R would park. The test is the delivery's own:
- `ticket_context_status(T, R)` is parked, or
- R holds a saved conversation of T that `session_context.why_not_restorable` says cannot be restored: a runtime
  with no verified in-session resume, a binding never confirmed, or a different runtime.

The judgement can't flap: it reads durable records (the binding, the failed restore) and the declared runtime, and
never the pane. A reviewer that never held the ticket (no binding) is a first hand-off, and is unaffected. It is
conservative: a Codex reviewer still holding the ticket's conversation in its pane would have been deliverable, and it
is skipped too. The review then waits for its owner, which costs time, never a park.

The board side, `balance_review_stage_skipping(stage, owner, now, skip)`, never moves a placed ticket. A hand-off
parked with its owner is that owner's current ticket, which the SYRD-745 balancer never takes.
`balance_review_stage(stage, owner, now)`, the older listener's call, is the same with nothing skipped. An older listener
during an upgrade or rollback therefore keeps placed reviews where they are, but it can still balance a review to a
reviewer whose hand-off then parks. The test shows that case ending as a Director-owned park, never a silent one.

**A parked review is the Director's.**
- **The notice:** `resolve_ticket_context_restore` still tells the Director once per failed attempt. For a review stage
  it names the ticket, the reviewer, the reason and the exact candidate, and the two recoveries: reassign it to another
  owner of the stage, or `force-fresh-context`.
- **The stall alert:** `board_stall_runnable` counts a hand-off parked with its owner as the Director's next action,
  not as the owner stalling.
- **The balancer** leaves it alone: it is its reviewer's current ticket.

**A fresh review.** For a ticket parked with its current owner in a review stage, `force-fresh-context` (the SQL is
`request_fresh_ticket_context`) now does this, in one transaction:
1. sets that owner's records of the ticket's conversation aside, whole, in `fresh_review_archive`: its binding, its
   session clears, its restore attempts and any continuation;
2. records the Director's reason on the ticket;
3. queues the hand-off "… is now assigned to you for a fresh review of the exact candidate <commit>; the earlier
   findings are in its comments".

The next hand-off is then a first one: the pane is cleared at an idle boundary, as for any new ticket, and never
mid-turn; the new session is bound; and the review is delivered once. The stage, the owner, the candidate and every
sign-off are unchanged. Before a hand-off, `force-fresh-context` behaves as it did (SYRD-626).

**Unchanged:**
- exact-candidate and independent-review provenance;
- where a submission first lands;
- the return of a rework to the reviewer who kicked it back, when that reviewer can take it;
- delivery once;
- no clear or interruption of a busy pane.

## Migration

`pgu1059_syrd873_review_placement.sql`:
- adds `review_placements`, its trigger and `review_placement_current`;
- adds `fresh_review_archive`;
- adds `balance_review_stage_skipping`;
- replaces `balance_review_stage`, `resolve_ticket_context_restore`, `request_fresh_ticket_context` and
  `board_stall_runnable`, each with the same signature.

`schema.sql` carries the same. `rbac.sql` grants the listener the new balancer. `schema_compatibility` declares it
compatible with older code.

## Tests

**`tests/review_recovery_test.py`** runs on a production-built board under a pull policy, with Audit owned by `audit`
(Claude) and `audit-2` (ephemeral, Codex). It uses the real server, the real listener, the hook writer, and stand-in
provider processes registered as panes register them. All tickets are synthetic.
- **Main, from a git archive:**
  - the rework is balanced to `audit-2` and parks;
  - the Director's reassignment of it to busy `audit` is undone.
- **This tree:**
  - the rework stays with `audit`, and `audit-2` is sent nothing;
  - a review the Director moves to busy `audit` stays there, even though idle, empty `audit-2` could take it as a
    first hand-off, and is delivered to `audit` once when it frees up;
  - a placement ends with the stage: a review placed with `audit`, then kicked back and resubmitted, is balanced to
    `audit-2` again;
  - moved to `audit-2` by an older listener's balancer call (which skips nothing), the rework parks, with one notice
    naming the ticket, the reviewer, the reason, the candidate and both recoveries;
  - the stall alert attributes it to the Director, and the balancer does not move it to `audit` once `audit` is idle
    and empty;
  - `force-fresh-context` sends nothing while `audit-2` is busy. Once it is idle, `/clear` and then exactly one
    hand-off naming the candidate, never a `/resume`;
  - the old records are archived, and the new conversation is bound.

`review_balance.would_park` is also checked against a stand-in board, for a Claude reviewer and a Codex reviewer.

**Mutation:** 17 mutants, each killed by an assertion. Two clauses that nothing could reach were removed: a placement's
owner check, and a "parked" filter that duplicated SYRD-745's rule never to take a reviewer's current ticket.

**`tests/review_balance_test.py`** (SYRD-745) still passes. Its schema parity check now names pgu1059.
