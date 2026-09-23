# A finished turn's poll loop must not starve a later handoff

Routing SYRD-211 to App on 2026-09-18. The transition was enqueued at 21:56 as
notification 2842 and was still undelivered 38 minutes later. App's trusted hook
had said idle at 21:02 and its composer was empty. The only descendant under the
pane was a SYRD-210 shell started at 20:49, polling a task-output file every ten
seconds for a marker that was never going to appear. The Director ended it by
stopping that process group by hand; App then found SYRD-211 independently, while
the original notification was still queued.

## The exception that swallowed the rule

SYRD-101 had already taught the gate about a finished turn's leftovers, and it
made one deliberate exception, for a reason it states well: a descendant still
making progress is real work whatever turn started it, so an hour-long sweep
keeps its pane quiet, and no clock may interrupt it.

The exception was tested by asking whether **any** survivor's CPU had risen:

```python
used = sum(max(0, cpu - before[pid]) for pid, cpu in second.cpu_by_pid if pid in before)
return used > self.child_work_cpu_ticks
```

A sum cannot say whose ticks it counted. A poll loop waking every ten seconds
raises it exactly as a build does, so `advanced` was true, `pane_child_work` was
returned, and the prior-turn classification underneath it never ran. The
exception did not merely apply too often; it ran first, so the rule it was an
exception to was unreachable.

## Two changes, and the line between them

**The probe says which turn a hold comes from.** `_survivors_that_advanced`
replaces the tree-wide sum with per-process attribution, and when everything
advancing predates the turn's own idle hook the verdict is named
`prior_turn_child_work` rather than `pane_child_work`. It still holds delivery —
the sweep SYRD-101 protects is untouched — but the record now distinguishes:

| reason | meaning |
| --- | --- |
| `pane_child_work` | the CURRENT turn is working |
| `prior_turn_child_work` | a finished turn's descendant, still progressing |
| `stale_prior_turn_child_work` | a finished turn's descendant, progressing not at all |
| `orphaned_prior_turn_waiter` | a handoff released past a finished turn's hold |

**The wait is bounded, where the wait is known.** A probe cannot know how long a
build should be allowed to run. The handoff can know how long it has been
waiting, so the bound lives in the delivery loop: after fifteen minutes behind a
*finished* turn's hold, the notification stops being requeued.

The two are deliberately separate. Had the probe tried to decide this alone it
would have needed a rule for telling a build from a waiter by how it spends CPU,
and that rule is exactly the fragile heuristic the ticket rules out.

## What happens at the bound

Not simply "deliver anyway". First the board is asked whether the owner already
reached this ticket — `active_work_notified_at`, the newest successful send of
this transition to this role in the state it is in now:

* **Recorded** — the owner has it. Delivering now would hand over the same
  assignment a second time, so the queued copy is discarded as
  `owner_already_acted`, with how long it waited.
* **Empty** — nobody has told them. The handoff is delivered, and the wait is
  recorded as `orphaned_prior_turn_waiter` with the hold, its reason, and the
  bound. An escalation someone can act on, rather than another silent retry.

The current turn's own work is never bounded. `PRIOR_TURN_HOLD_REASONS` contains
only the two finished-turn reasons; `pane_child_work` is absent on purpose, and
a test holds that a day of it still keeps the pane.

## A hypothesis that did not survive, recorded

The first attempt separated waiter from worker at the probe by requiring a
prior-turn child to advance on **consecutive** probes — a build burns
continuously, a poll loop sleeps between wakes. Driven against the fixture it
separated nothing: each probe takes two samples, so a loop waking every ten
seconds advances on nearly every probe too, and the rule called a genuine
prior-turn sweep a waiter on its first probe. It was reverted rather than tuned.
A threshold that happens to fit one fixture is not lifecycle evidence.

## Verification

`tests/ticket_board_orphaned_prior_turn_waiter_test.py`, six cases: the waking
poll loop is no longer read as this turn's work; a sweep from before the
turn-end still holds and says which turn; current-turn work is still named as
such; a handoff waits behind a finished turn only for the bound; the current
turn's work is never bounded; and a handoff the owner already answered is
discarded rather than delivered.

Mutation, 4 mutants over the new decisions, all killed:

| mutant | killed by |
| --- | --- |
| never name a hold as prior-turn | the poll loop read as this turn's work again |
| the bound never expires | the handoff retried instead of delivered |
| treat `pane_child_work` as boundable | a day of current-turn work releasing the pane |
| ignore that the owner was already notified | the duplicate delivered |

Focused suites, against a clean worktree at the base `3b15aa5`:

* `ticket_board_stale_prior_turn_child_work_test` — passes at the base and here.
* `ticket_board_active_turn_suppression_test` — passes at the base and here.
* `ticket_board_notify_listener_test` — fails at
  `test_final_review_handoff_exemption_does_not_broaden_manual_holds`,
  identically at the base and here. Pre-existing; baselined rather than left for
  Audit to chase.

Three assertions in `ticket_board_stale_prior_turn_child_work_test` named the
old reason for prior-turn progressing work and now name `PRIOR_TURN_CHILD_WORK`.
Each still asserts the pane is **busy**, which is the invariant those cases hold;
only the diagnostic is more specific, which is what this ticket asks for.

## Not verified here

A live pane. Everything above drives the real gate and the real delivery loop
through their injected seams — a process-table reader, a fake connection, a
monotonic clock a test can move — so no case waits fifteen real minutes. Whether
the bound is the right length against real builds on the live board is a
judgement the first escalation will inform, and it is a constructor argument
rather than a constant precisely so it can be changed without touching this
reasoning.
