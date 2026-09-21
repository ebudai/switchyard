# A kickback that was acked but never seen

A Director DAT kickback on SYRD-221 returned the ticket to ops. The User saw no
handoff. The board said otherwise: the notice was enqueued, deferred while ops
looked busy, sent, acked, and `active_work_notified_at` was set.

Three separate defects produced that, each of which would have hidden the
others.

## The kickback carried the submission's time

`perform_workflow_action_as` moved a ticket's state and assignee without
touching its activity time. Every trigger on that update —
`upsert_ticket_notification_state` and the transition enqueue — reads
`NEW.updated_at` as the moment the change happened, so they all recorded
whatever last set it: ops's own earlier submission.

Reproduced on a temporary cluster with a ticket in DAT whose `updated_at` was
twelve minutes old:

```
submission time (the earlier ops submit) : 07:35:27.436696
kickback happened at about                : 07:47:27.480857
after the kickback:
   ticket_updated_at          07:35:27.436696   <- the submission
   entered_current_state_at   07:35:27.436696
   last_activity_at           07:35:27.436696
   queued transition -> ops   payload updated_at = 07:35:27.436696
```

The same shape as the live trace, whose payload carried 07:21:24.

A transition is now stamped in the **same** `UPDATE` that makes it. Not by the
existing `touch_ticket()`: that is a separate update, and by the time it runs
the triggers have already built the notice from the stale value. This applies
to every transition, not only returns — a transition is activity.

`perform_workflow_action_as` is also defined in four earlier migrations, and an
upgraded board runs the newest. So the fix ships as
`pgu954_syrd225_transition_activity_time.sql` with the identical body, and the
test exercises both: a fresh board on `schema.sql`'s copy, and a board that is
first given the **previous** migration's body — which reproduces the stale
kickback, proving the test can see it — and then has pgu954 applied.

## A previous stint's send reported this one as delivered

`active_work_notified_at` was the latest transition send matching ticket, owner
role and `ticket_state_at_event`. Nothing in that separates one stint in a stage
from the next, and a DAT kickback returns a ticket to the same implementer in
the same state it left — so the send from ops's earlier stint in Implementation
matched.

It is now limited to sends at or after `entered_current_state_at`, which is
only meaningful because the first fix makes that column correct. Rows from
before the board recorded an entry time keep the old reading.

## The listener acked what it could not show arrived

After `directorctl send` returned without an error, the listener traced `send`,
then `listener_ack`, then acked — unconditionally. And `directorctl` types,
submits and prints "delivered" without reading anything back; its
`composer_before`/`composer_after` are diagnostics, not verification. An empty
composer after a submit is also what a *successful* delivery looks like, so
emptiness cannot be the failure signal on its own.

The live send happened while the pane's busy reading was
`stale_prior_turn_child_work`: a previous turn's child processes still attached.
That reading is deliberately "not busy" (SYRD-101 — otherwise a pane can be
starved of notices forever), and a pane holding a child process is exactly where
typed input can go to the child rather than the conversation.

### What counts as proof

The notice's opening words appearing on the pane **more times after the send
than before**.

Merely appearing is not enough, and this is the part worth recording. A
kickback's text is identical every time the same ticket comes back, so the line
from an earlier stint can still be in the scrollback. "It is on the screen"
would report the new handoff as shown on the strength of the old one — the
masking above, one layer down.

The fingerprint is the first 48 characters of the notice's first line,
whitespace-collapsed. That is what survives every way `directorctl` delivers:
typed as-is; collapsed and cut to 200 characters for the director; or staged
behind `please read <path> (<first line, cut to 120>) and follow the
instructions therein`. The pane is read with 2000 lines of history so a notice
cannot scroll out between the two readings, and with `-J` so lines the terminal
wrapped are rejoined; lines a CLI broke itself are handled by collapsing
whitespace, since they break at word boundaries.

### What happens without proof

The notice stays queued. The trace records `send_unconfirmed`, saying whether
the pane did not show it or could not be read (`send_unverifiable`), what was
looked for, and how many times it was seen before and after. It is requeued
through the existing backoff.

There was no ceiling on a requeued notice, so there is one now: after three
unconfirmed sends it is dead-lettered as `delivery_unconfirmed`. Without that, a
pane that renders the text in some way this cannot read would be sent the same
notice forever — its own failure, and one nobody sees either.

A gate with no pane reader at all keeps the old behaviour and says so in the
trace (`delivery_proof: unavailable`). Every production gate has one.

## The tests that had to change, and why

Two existing suites built a real activity gate whose captured pane was a fixed
string that never showed anything sent to it. Against a listener that requires
proof, every delivery in them read as a notice that never landed: 19 cases in
`ticket_board_notify_listener_test` and the SYRD-101 case in
`ticket_board_stale_prior_turn_child_work_test`.

That last one asserted, in so many words, that a send into a pane whose screen
never changed was "delivered and acknowledged". That assertion *was* this
ticket's defect.

Both suites now wire the listener to a pane that shows what it was sent
(`tests/typed_pane.py`), which is what a real pane does. It answers the proof
read directly rather than through the canned capture runners, because those pop
one screen per call and an extra read would consume a screen another probe in
the same test expected. The unwrapped listener remains available for cases that
need a pane which does not show the notice.

With that, both suites match baseline exactly, case for case.

The sweep found three more of the same:

- `ephemeral_role_sessions_test` and `ticket_board_listener_pane_state_authority_test`
  build their own listener against a canned, empty capture, and get the same
  faithful pane.
- `ticket_board_postgres_backend_test` inserts its tickets now and dates their
  sends in July. Scoped to the current stint, every send preceded the stint it
  delivered. The stints are backdated together, so the order they entered in —
  which the active-work ranking reads — is unchanged.

## Verification

- `ticket_board_kickback_handoff_postgres_test`: fresh and upgraded boards both
  stamp the kickback's time; the previous migration's body reproduces the stale
  reading; a previous stint's send is not this handoff's; an unconfirmed send is
  not a send.
- `ticket_board_delivery_proof_test`: runs on the incident's own classification
  (`stale_prior_turn_child_work`, asserted, not assumed). A swallowed kickback is
  not acked and is requeued; the same notice left from an earlier stint proves
  nothing; a notice the pane shows is acked and says how it was proven; an
  unreadable pane is not proof; three unconfirmed sends end in a dead letter
  that says so; the gate reads with history and joined lines; the fingerprint
  survives all three delivery shapes and a CLI's own word wrapping.
- Mutation: 11 mutants across the three fixes, including both copies of the
  function, no survivors.

- Sweep: 221 affected suites, run serially against `565152f`, identical — 56
  failures on each, all pre-existing.

## What this does not cover

**Four listener suites were already red on the baseline**, and each stops at its
first failure. For those, the comparison is where each stops, and all four stop
at the same assertion with the same message before and after — but whatever
comes after that point runs on neither, so a regression there would not show.
Two of them fail during setup (`unassigned_triage_notice_test` in
`apply_workflow`, `ticket_board_durable_awaiting_role_test` in its first `psql`),
so they exercise almost nothing.

**A generation is a stint in a state, not a stint with a role.**
`entered_current_state_at` moves when the state changes. A ticket reassigned
away from a role and back without leaving its state keeps its entry time, so a
send to that role from before the reassignment still counts. The reported case
changes state and is covered; this narrower one is not.

**How a real CLI draws a submitted notice was reasoned, not measured.** The
fingerprint rests on the notice's opening words appearing on the pane after a
send. That matches every notice this session received in its own Claude pane,
and the tests model a CLI's word wrapping, but no real Codex or Claude pane was
driven here — doing that would mean sending into a live one. If a CLI turns out
to draw it differently, the failure is visible rather than silent: three
unconfirmed sends and a dead letter saying `delivery_unconfirmed`, not a notice
acked that nobody saw.
