# SYRD-557: the Director hears when background work holds a role's notices

## What otto saw (release 2951c5c9, report item 3)

A wait loop whose `pgrep -f "x"` matched itself ran for 2 h 44 min. SYRD-538
rightly read the pane as busy with background work, and held two ticket notices
behind it. Nothing told the Director that ready work had sat undelivered for
hours. Otto worked around it with a user timer, `otto-bg-watchdog.timer`, that
nudges a pane once the same job has held its notices for 30 minutes.

## The hold stays exactly as it was

Two holds count as background work:
- **The owner's own background tasks** (SYRD-538). A role's turn ended on
  in-flight work that is still provably live, so reminders about that owner
  wait. The requeue error is `owner_background_work`.
- **A pane child the activity gate judged to be work.** That is
  `pane_child_work`, or an earlier turn's `prior_turn_child_work` and
  `orphaned_prior_turn_waiter`, until SYRD-212's bound releases those. It holds
  every kind of notice, including a new ticket's transition.

Nothing here delivers through a hold, interrupts the work, or kills anything.
The board cannot tell a long build from a loop that never ends, and is not
asked to: a person can.

These are not background work, and open no episode:
- a pane held for a half-typed line;
- the role's own foreground turn;
- an unreadable cursor;
- a startup latch.

## Hold episodes

`ticket_board.background_hold_episodes` (pgu980) keeps one open episode per
role.

- **Noting.** Every requeue that a background-work hold causes is noted with
  `note_background_hold(notification, reason, alert_after)`. The first opens
  the role's episode. Later ones, for any of that role's notices, join it,
  adding the notice and its ticket.
- **Alert.** Once the episode is older than the threshold, one `ticket_update`
  goes to the Director (payload kind `background_hold`, dedupe
  `background_hold:<episode>`). It names:
  - the role and how long it has been held;
  - the tickets still held and the hold's reason;
  - that nothing was interrupted, and what a job that never ends looks like.

  It is raised at most once per episode, however many passes or listener
  restarts follow.
- **Clearing.** When the last of the episode's notices leaves the queue, the
  trigger `background_hold_notice_left` closes it. The clear reason is:
  - `delivered`, if the notice was sent;
  - `superseded`, if it was dropped without a send. A stale drop is
    acknowledged too, so the send trace decides which it was;
  - `dead_lettered`.

  Then:
  - an alert the Director has not been sent yet is withdrawn, with a `discard`
    trace whose reason is `background_hold_cleared`, so the Director never
    hears of a hold that is already over;
  - an alert already sent is followed by one line saying the hold ended, and
    how.

  So an episode costs the Director at most two notices.

The Director's own pane opens no episode: an alert about it would wait behind
the same hold.

## Configuration

The threshold is `TICKET_BOARD_BACKGROUND_HOLD_ALERT_SECONDS`, in the
listener's environment, and defaults to 1800 s. That is the half hour otto's
watchdog settled on, and an ordinary mutation run or suite sweep finishes
inside it. An empty, negative or unreadable value falls back to the default,
as `TICKET_BOARD_BACKGROUND_WORK_LIMIT_SECONDS` does.

## Restart safety

The episode, its start, and whether the Director was told all live in the
database. A restarted listener's next hold joins the open episode instead of
starting the clock again, and the alert's dedupe key holds across restarts.

## Where it hooks in

`notify_listener.py` sits one line under its 1,250-line review ceiling, so it
is untouched. Every hold already passes through the notification ledger:
- `_defer_for_background_work` requeues with `owner_background_work (phase)`;
- each activity-gate deferral calls `trace_deferral_once` with its
  `busy_reason`.

The ledger notes those through `background_hold.note`. That note:
- checks once that the board has the function, since a listener can be newer
  than its board's migrations;
- ignores every reason that is not background work, without touching the board;
- logs, rather than raises, any failure, so the hold never depends on its own
  bookkeeping.

The alert's payload kind is current for the Director in
`notification_eligibility`, like SYRD-539's and SYRD-565's Director notices.

## Tests

`tests/background_hold_alert_test.py` builds a production board (schema.sql,
the real ticket-board-migrate, rbac.sql) with the example workflow. It then
uses:
- the real HTTP server and notify listener;
- the real PaneActivityGate and hook-state store;
- the real pane hook's own background-work writer;
- the board's own turn-end generator, for the held repair prompt.

Three things are stood in for: tmux, the process table the gate samples, and
the /proc the registered provider identities are read from. Every pass is a
single iteration of the listen loop, run by a newly started listener, so each
case also covers a restart. Elapsed hold time is simulated by moving the
episode's start back.

- **On main (1e8d7ef, from a git archive):**
  - a repair prompt held behind background work;
  - a ticket held behind a self-matching wait loop.

  Both stay held for good, and the Director is never told.
- **A long legitimate task.** app's turn ends unresolved while a background
  build runs, and the repair prompt is held behind it:
  - at 29 minutes, nobody has been told;
  - at 31 minutes, the Director is told once;
  - three restarted passes later, there is still one alert and the prompt has
    never been sent;
  - when the build ends, the prompt is delivered, the episode clears as
    `delivered`, and the Director is told the hold is over.
- **The pathological job.** A real `until ! pgrep -f ...` loop, which matches
  itself, runs detached in ops's pane, with the threshold configured to
  10 minutes:
  - ops's new ticket, and then the Director's note on it, join one episode;
  - at 9 minutes, nobody has been told;
  - at 11 minutes, one alert naming 2 notices, unchanged over five restarted
    passes, and nothing is sent to ops;
  - the loop is still running, and no tmux call that could interrupt the pane
    was made.

  Once somebody ends the loop:
  - the first notice goes out and starts ops's turn;
  - the episode stays open while the second waits behind that turn;
  - the episode clears as `delivered` once the second goes out too.
- **Supersession.** main is held the same way, and so is the Director's own
  pane, so the alert waits:
  - the Director's own hold opens no episode;
  - the ticket is cancelled, and the episode clears as `superseded`;
  - the waiting alert is withdrawn (`background_hold_cleared`), and the
    Director never hears of it.
- **Dead-lettered.** A held notice whose pane is gone is dead-lettered, and
  its episode clears as `dead_lettered`.
- **Not background work.** A notice held behind app's own foreground turn opens
  no episode.
- **Configuration:**
  - 1800 s unless configured;
  - an empty, negative or unreadable value falls back to the default;
  - no other hold reason reaches the board;
  - a failing board call is logged, not raised.
- **Grants and parity.** Only the listener may call `note_background_hold`,
  and nobody may call the trigger function. A fresh schema.sql board installs
  the same function text and trigger as the migration. These checks run last.

A per-case comparison of `ticket_board_notify_listener_test` and
`ticket_board_background_work_test` against main gives the same results on
both trees: 72 pass and 42 fail (red on main already), and 6 pass.

**Mutation:** 21 mutants, run with `tests/bounded_run.py mutate` and
`SYRD557_MUTATION_SKIP_COPY_PARITY=1`. They cover the migration, the trigger,
`background_hold.py`, the ledger hooks and the Director-current kind. All 21
are killed, each by a named behavioural check. A failing check repeats its
label on the last line, so a truncated output still says which one failed.
None was killed by a crash or by copy parity.

## Otto

Otto's workaround is the user timer `otto-bg-watchdog.timer`, under
`~otto-agent/otto-watchdog/`. When this is deployed to Otto, tell Otto's
Director, so that workaround is removed rather than left to drift.
