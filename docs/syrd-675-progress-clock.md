# SYRD-675: a priority stall alert is about no progress, on one clock

## What happened

On SYRD-674, Ops took the ticket at 14:24:30 UTC and posted a durable design
checkpoint at 14:28:27. It then built and ran the related suites. At about
14:35 the Director was sent:

> PRIORITY SYRD-674 ... ops took it at 14:24:30 UTC and its pane has said busy
> since, with nothing moving on screen.

The pane was running the suites. Ops's next checkpoint was at 14:54:31, and it
submitted at 15:23:34.

The alert is `note_role_stall`'s `busy_no_progress` escalation (SYRD-595), fed
by the listener on every pass. It fired when three things were true:

- the hook had said busy since the hand-over;
- the screen did not change while sampled;
- the unresolved-turn grace (600 s by default) had passed since the hand-over
  or the hook's last word.

The owner's comments counted only through "the owner wrote on it", which closes
an episode that is already open. The busy path opened the episode and escalated
in the same pass, so Ops's checkpoint was never read. Nothing else the board
records as progress was read either.

The regression replays that timeline against main: main escalates at 14:35:00,
630 seconds after the hand-over.

## One progress clock

`ticket_board.role_progress_at(ticket, role, now)` is the latest instant at
which anything showed the role's work on the ticket moving:

- **Durable progress.** This is the board stall alert's own record,
  `board_progress_events`: a stage, an assignee, a candidate commit, a blocker,
  a dependency, a delivered hand-over, and a checkpoint by the owner. The board
  stall alert reads it board-wide; this reads one ticket's.
- **The round's assignment** (`current_assignment_at`).
- **The owner's own comments.** These count on every board, including stages
  the board stall alert does not count.
- **A hand-over delivered to the owner:** a transition, awaiting-role or
  publication notice sent to its pane.
- **A recovery:** the owner's runtime registering. Every registration is a new
  generation, not a heartbeat.
- **A bounded child** (SYRD-616's execution leases) for the ticket. It counts up
  to `least(ended_at or last_seen_at, deadline)`: while the listener sees it
  running, and never past its deadline. A child that never ends cannot hold an
  alert off for ever.

Nothing later than `now` counts. The function is PL/pgSQL because schema.sql
defines it before the tables it reads.

## The busy-pane alert

`busy_no_progress` now fires only when
`now >= greatest(clock, hand-over, hook updated_at) + threshold`.

The threshold is the board's configured stall threshold,
`TICKET_BOARD_BOARD_STALL_SECONDS` (default 1800 s), and never less than the
unresolved-turn grace. The listener passes its own configured value into
`note_role_stall` as `p_busy_threshold`. The board stall pass, which records
the value in `board_stall_state.threshold_seconds`, runs after the stall pass
in each listener loop. So the stored value is still the old one on a fresh
board's first pass, or after a restart with a changed threshold. Audit found
exactly that (B1): stored 1800, configured 3600, and an alert at 1900 s.

The stored value is only a fallback for a caller that passes none. pgu1015
drops the old four-argument signature, so a call by position reaches the one
function. The listener calls the old form on a board that predates this
change.

The busy alert and the board stall alert therefore judge "no progress" on the
same record and the same threshold, and cannot contradict each other. The old 600 s from the hand-over would still have flagged Ops at
14:38, ten minutes after its checkpoint, while the suites ran.

Two smaller changes:

- **The hand-over counted is the round's,** not only the current phase's. An
  episode an owner checkpoint closed opens again with its phase starting at the
  close. With the old phase-only hand-over, the busy alert could never fire
  again that round, however long nothing then moved.
- **A later phase restarts the clock.** That is the Director's hand-back, or a
  replaced runtime, as a recovery.

## Bound to what it saw, rechecked at delivery

The escalation's payload carries what it saw: `progress_at` (the clock),
`round_at`, `threshold_seconds`, and the episode, phase and generation.

`ticket_board.role_stall_escalation_stale_reason(payload)` returns NULL while
the premise holds. Otherwise it returns a stable reason, checked in this order:

| Reason | When |
|---|---|
| `episode_closed` | The episode closed or moved to another phase. |
| `runtime_replaced` | The owner's runtime registered a new generation since. |
| `execution_lease` | A bounded child counts after what it saw. |
| `progress` | The clock moved past what it saw. |
| `hook_activity` | The owner's hook moved since. For a busy alert, also the hook no longer saying busy. |

An escalation queued before this change has no `progress_at`. Its episode alone
decides, as before.

Delivery asks `role_stall_notice_is_current` at claim, at the pre-send recheck
and at the held recheck, and that now asks the stale reason. The listener adds
the owner's hook as it is at that moment (`owner_hook`): the stall pass hands
delivery a reader through `eligibility.stall_owner_hook`, so a long delivery
hold still sees the hook. A dropped escalation's `drop` trace records
`stall_premise: <reason>`.

The next stall pass rechecks an escalation that was never sent:

- **Still queued with a stale reason:** the pass withdraws it, with discard
  trace `stall_premise_<reason>`, and ends the episode as `progress`.
- **Dropped at delivery, for any reason:** the pass ends the episode too. The
  owner is then not left in the Director phase, without its prompts, behind an
  alert nobody received.

While an episode is escalated, `role_stall_notice_is_current` already drops the
Director's other stall notices about it (`unresolved_turn`, `nudge`,
`escalation`). That gives one Director message per role, ticket and episode.

## The unresolved-turn escalation

`notify_unresolved_turn_end` gave the owner a grace from the prompt's delivery,
or from the end of the owner's later turn. That grace now also runs from
`role_progress_at`:

- an owner checkpoint or a delivered hand-over after the prompt starts it again;
- so does a recovery;
- so does a bounded child, up to its deadline. This replaces SYRD-584's "a
  background child holds nothing here".

## Tests

`tests/role_stall_progress_clock_test.py` asks a production-built board's own
functions, as the listener asks them, at explicit instants. The tickets'
records are dated in the past, as each timeline dates them. Main runs first,
from a git archive.

**Main:**

- SYRD-674's timeline escalates at +630 s;
- the busy pane escalates past 600 s;
- a checkpoint and a running bounded child count for nothing;
- a child started after the alert was queued leaves it deliverable.

**This tree:**

- SYRD-674's timeline, with a pass every 30 s from the hand-over to the
  submission, gives no alert;
- the control, with no checkpoint, gives `[0, 0, 1]` at +630, +1790 and
  +1830 s, bound to its clock, round, threshold and episode;
- a configured 600 s threshold is counted from the latest checkpoint;
- a bounded child holds the alert off, and only to its deadline plus the
  threshold;
- after an alert is queued:
  - a checkpoint gets it dropped at delivery with `stall_premise: progress`,
    and the next pass ends the episode;
  - a bounded child gets the next pass to withdraw it, with
    `stall_premise_execution_lease`;
  - the hook moving, or its turn ending, makes it not deliverable;
  - a replaced runtime gets `runtime_replaced`;
- an alert dropped at delivery for its own reason (resolved for now) is
  followed by the next pass ending the episode;
- the Director's hand-back restarts the clock;
- the listener's pass hands delivery the owner's hook;
- a listener's first pass, with 1800 s stored and 3600 s configured, does not
  escalate a pane busy 1900 s; configured at 1800 s, it does;
- the Director's other stall notices are not delivered while the episode is
  escalated;
- the unresolved-turn escalation waits a full grace after a checkpoint;
- schema.sql's five functions are the migration's.

`tests/runtime_switch_stall_test.py` runs every timing at a thirtieth of
production's. It now scales the board stall threshold too: 60 s, the
threshold's floor, is 1800 s divided by thirty. It waits that long for its stuck
pane's one escalation.

`tests/recorded_dependency_unresolved_turn_test.py` re-runs pgu1009 as an
upgrade, then compares the board with a fresh `schema.sql`. pgu1015 replaces
pgu1009's `notify_unresolved_turn_end`. So the test now re-runs, after pgu1009,
every later migration that replaces a function pgu1009 defines. That is today
pgu1015, which is idempotent. The board then ends as an upgraded board does.
