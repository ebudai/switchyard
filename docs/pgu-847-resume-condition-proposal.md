# PGU-847: Resume Conditions Proposal

This is a proposal only. It does not add fields, change workflow enforcement,
or alter nudge behavior.

## Problem

The board has vocabulary for work, review, dependency blockers, and director
overrides, but not for a ticket that is legitimately not startable because the
world is not ready.

Three cases exposed the same gap:

- PGU-841 waits on a human decision: Eric must choose the shared-install source
  of truth.
- PGU-845 waits on a human action: Eric must perform a privileged import the
  agents cannot perform.
- PGU-853 waits on a future gate: the handoff document should be re-verified at
  cutover, not now.

Today's workarounds all misstate the ticket. `awaiting_role` is pane/role
vocabulary and rejects `user`; `blocked_reason` is tied to ticket blockers;
backlog makes the work look abandoned; `manually_controlled` mutes nudges but
also bypasses workflow gates.

## Recommendation

Add a first-class resume condition. A resume condition means: this ticket is
paused on purpose, the condition for resuming is written down, and the board
knows who is responsible for judging when that condition is satisfied.

Recommended shape:

```text
resume_condition:
  active: boolean
  kind: operator_action | operator_decision | future_gate | external_event
  summary: non-empty text
  judge_role: director | user | other project role
  visibility: operator_attention | director_hold | quiet
  created_by: role
  created_at: timestamp
  resolved_by: role, optional
  resolved_at: timestamp, optional
```

This should live beside blockers, not inside `awaiting_role` or `blocked_by`.
It is a ticket-level hold, not an assignment and not a dependency edge. The
ticket should remain in its real workflow state so the board still shows what
stage it will resume into.

Examples:

- PGU-841: `kind=operator_decision`, `judge_role=user`,
  `visibility=operator_attention`, summary names the repo decision.
- PGU-845: `kind=operator_action`, `judge_role=user`,
  `visibility=operator_attention`, summary names the privileged import.
- PGU-853: `kind=future_gate`, `judge_role=director`,
  `visibility=director_hold`, summary names the handoff/cutover gate.

## Nudge Behavior

An active resume condition should suppress normal idle nudges for the held
ticket. The ticket is not forgotten; it is waiting honestly.

Instead of five-minute role nudges, the board should expose held tickets in
purpose-built views:

- Operator attention: one place where Eric can see every ticket waiting on his
  action or decision.
- Director holds: one place where the director can see tickets waiting on a
  date, cutover, external state, or a decision the director owns.

For operator-attention holds, prefer visible aggregation over repeated nags.
A slow digest or badge is acceptable, but the useful thing is a stable list:
"these are the things only Eric can unblock." The board should not notify Eric
every few minutes as if he were an agent pane.

When the condition is resolved, clearing it should restore normal nudge
behavior and should reset the ticket's idle timer so it does not immediately
fire a stale nudge.

## Interaction With PGU-822

PGU-822's data-driven role vocabulary helps validate `judge_role`, but it does
not make this concept fall out for free. A resume condition is not just another
role assignment:

- a human operator is not an implementer queue;
- a future cutover gate is not a role at all;
- suppressing idle nudges is not the same as routing work to a stage owner;
- visibility needs operator/director aggregation, not transition notification.

So this should not wait for all of PGU-822. Build it in a way that can later
reference the PGU-822 roles table when that table exists. Until then, use the
current role validation for `judge_role` plus an explicit `user`/operator value,
but do not add `user` to `awaiting_role` as a ninth literal.

## Blocked Reason

`blocked_reason` should remain coupled to ticket dependencies. Its job is to
explain `blocked_by`, not to become a general-purpose hold note.

Two changes are still worth making:

1. Creating or editing a ticket with `blocked_reason` but no `blocked_by` should
   fail loudly instead of silently discarding or ignoring the reason.
2. Editing dependency blockers and their reason should continue to go through a
   dedicated `set_blockers` operation so the dependency list and explanation
   stay atomic.

For PGU-841-style work, use a resume condition, not standalone
`blocked_reason`. That keeps "blocked by ticket PGU-N" separate from "waiting
on an operator decision."

## Manual Control

`manually_controlled` should stay as the director override escape hatch. It is
still useful for unusual cases, but it is too broad for ordinary holds because
it bypasses gates. A resume condition should mute nudges without disabling
workflow enforcement.

If a manually controlled ticket also has a resume condition, the UI should show
both facts separately:

- manually controlled: director is bypassing normal enforcement;
- resume condition: ticket is waiting for a stated condition.

## Minimal Implementation Plan

1. Add storage for one active resume condition per ticket, or a small history
   table if auditability is preferred.
2. Add board actions: `set_resume_condition` and `clear_resume_condition`.
3. Include active resume-condition data in ticket payloads and board summaries.
4. Suppress normal idle nudges for tickets with an active condition.
5. Add operator-attention and director-hold filters/views.
6. Make create/edit reject a dependency `blocked_reason` without `blocked_by`.
7. Add regression tests for all three motivating shapes: operator decision,
   operator action, and future gate.

## Decision

Do not solve this by expanding `awaiting_role`. Use a resume condition: a
first-class, visible, gate-preserving hold that can represent "waiting on Eric"
and "waiting until cutover" without pretending either is an implementer queue.
