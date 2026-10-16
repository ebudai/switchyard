# SYRD-594: a Codex turn that ends on "Selected model is at capacity" is retried, then escalated

## What otto saw

On 2026-10-16 otto's `uiux` pane (Codex CLI 0.163.0, shared release c9fd9c6)
ended a turn on `Selected model is at capacity. Please try a different model.`
and sat idle at its prompt. Nothing more reached it, and nothing reached the
Director, until the User nudged the pane by hand.

## The screen and the hooks, measured

Codex 0.163.0 ran in a private tmux server (160x45), inside bwrap with no
network. It used a throwaway `CODEX_HOME` logged in with a placeholder key,
with its model provider pointed at a local fake Responses API whose answers the
probe chose. Its hooks were the real ones (`ticket-board-install-pane-hooks
--seed-codex-hook-trust-if-new`), wrapped to record every call. The captures are
in `tests/fixtures/syrd594_codex_capacity_screens.json`.

- **A capacity failure.** A turn whose request failed with
  `server_is_overloaded` makes Codex draw this at the margin:

  ```
  ■ Selected model is at capacity. Please try a different model.
  ```

  Codex then returns to its empty prompt. It sent that request once and did not
  retry it.
- **An HTTP 503.** A 503 whose body names `server_is_overloaded` is retried by
  Codex, then draws the same line.
- **No Stop hook.** Codex runs no Stop hook for a turn that ends this way. The
  hook calls were SessionStart, UserPromptSubmit and UserPromptSubmit for two
  failed turns, then UserPromptSubmit and Stop once the provider answered. So the
  pane's hook state stays the failed turn's `busy codex.UserPromptSubmit` until
  the next turn starts.
- **A reply that quotes the text.** It is drawn after the reply's bullet, with
  any `■` line indented under it, and that turn does end with Stop.
- **Other errors.** `insufficient_quota` draws "Quota exceeded. Check your plan
  and billing details." Codex retries `slow_down` itself five times, showing
  "Reconnecting... n/5". Neither is this error.

## What main did with it

These are measured on a production-built board, with the real server, listener
and gate.

- **The gate.** For 120 s the pane reads `hook_busy`. After that, the generic
  stale-Codex rule rewrites the hook to idle
  (`listener.stale_codex_busy_recovery`). It does exactly the same for the quota
  error.
- **At production timings** (a real 4-minute run): 120 s after the failure, the
  owner is sent the generic unresolved-turn prompt, "PGU-901 is still yours and
  this turn ended without resolving it. Do one of: transition it...". That
  misnames the cause: the turn did not end, the provider refused it.
- **With the provider still full,** each prompt starts a new turn that fails the
  same way. So the owner gets the generic "still yours" and "waiting in your
  in_progress queue" prompts again and again, with no bound. The Director hears
  only "main was reminded about PGU-902 ... may be stuck". No message ever names
  the provider's error.

Which board condition kept otto's uiux silent I cannot see from here. The
generic prompt is held back when any of these is true (`ticket_turn_is_resolved`):

- the ticket is under manual control;
- it is awaiting a role;
- its reminders are snoozed;
- it holds a serial reservation;
- it has a blocker;
- the owner's own transition notice is still queued.

## The fix

### Recognised: the gate (`capacity_retry.turn_end_trace`)

A Codex pane is at the capacity error when all of these hold:

- its hook state is busy (a turn started, and no Stop has come);
- its cursor is on Codex's `›` prompt;
- the last line with text above that prompt is exactly the error line, at the
  margin;
- the screen is still (the same four-capture sample the stale path uses);
- no provider question is open;
- the composer is empty.

The gate reads such a pane as the end of the turn, at any age:
`codex_capacity_turn_end`, deliverable at both gates. It does not rewrite the
hook. So the generic reminders, which only fire for idle hooks, stay out of it,
and the pane keeps the failed turn's identity (`turn_started_at`) across
listener restarts.

Every other screen reads exactly as before. That includes the quota error, the
error followed by more output, indented or with more text on its line, a quoted
reply, and a cursor off the prompt. A draft typed under the error, a pane still
drawing, an open question and an unreadable cursor all still hold the pane.

### Retried and escalated: the board (`note_provider_capacity`, pgu995)

Each listener pass reports every Codex role's pane (`capacity_retry.report`) as
one of three things:

- the turn that ended on the error;
- the turn that ended normally (its runtime's Stop);
- working.

A role that cannot be read is left out, and nothing about it changes.

The board keeps one episode per role in `provider_capacity_episodes`. It is in
the database, so a restarted listener joins it and neither resets nor repeats an
attempt.

- **What it is about.** The role's current ticket, selected as SYRD-589's
  redelivery selects it, in its current round with the role. A round is
  `ticket_notification_state.current_assignment_at`: the stage entry, or a
  hand-over within the stage. If there is no current ticket, nothing is
  retried. A failed turn that started before the round began is not about it;
  the ticket's own notice goes first.
- **Retry.** Each failed turn is recorded once, with when it was first seen. A
  retry is queued when the delay for its number has passed since the failure it
  answers (`TICKET_BOARD_CAPACITY_RETRY_DELAYS`, default 60, 120, 300 s). It is
  never queued while anything else for the role is waiting to go: that notice is
  itself the next turn.
- **Its wording.** The repeat notice of the ticket: `<ID> -- <title> is active
  again in <Stage>. Your last turn ended on Codex's error "Selected model is at
  capacity. Please try a different model." before it finished: carry on from
  where it stopped (automatic retry n of 3).` It goes through the ordinary
  delivery path and its gates.
- **Counting.** A retry counts once it was typed into the pane. One withdrawn
  before then does not.
- **Escalated.** When every retry has gone and the turn after the last one
  failed too, the Director is told once. The message is
  `PRIORITY <ID> -- <title>: <role>'s Codex pane keeps stopping on "..."`, with
  each failure's time, the waits, and `switchyard attach <project> <role>`. Its
  payload carries the role, the condition, and the failure and retry history.
  Nothing more is sent on its own.
- **Closed** when a turn after the last failure ends normally (`recovered`), or
  when the ticket moves or changes hands (`ticket_moved`). That includes a
  ticket that has come back to the same stage and owner by the next pass: its
  round differs from the one the episode was opened in. A retry or alert not yet
  sent is withdrawn and traced as a `discard`.
- **Withdrawn** as soon as the pane is reported working: a turn running, or a
  person typing.
- **Delivery asks too.** `provider_capacity_retry_is_current` says whether a
  retry or the Director's alert still belongs to an open episode whose ticket is
  in the round it was opened in, and whether a retry is the episode's latest.
  So a retry or alert queued before the ticket left and came back is not typed,
  even before the next pass closes its episode. A retry for a ticket under
  manual control, parked or blocked is dropped, as an idle reminder is. The
  Director's alert is current only for the Director.

Only the listener may call `note_provider_capacity` and
`provider_capacity_retry_is_current`; the service role is refused. A board built
from `schema.sql` alone installs the same functions.

## Not changed

- **Every other Codex error,** and every other runtime. The quota error,
  `slow_down` and anything else still go through the stale-Codex rule exactly as
  before. Only the measured capacity line is retried, as the Director asked.
- **The Director's own pane.** It is never escalated about, because the alert
  would wait in that same pane (as SYRD-570 decided).
- **One episode per round of the ticket, not per outage.** Once a retry has
  started a turn that ran, an episode closes only on a normal turn end or a new
  round. A long turn that
  works and then fails at capacity counts against the same three retries. That
  is bounded, and errs toward telling the Director sooner.
- **The generic unresolved-turn prompt.** It still follows a turn that ended
  normally without resolving the ticket, retry or not.

## Evidence

`tests/codex_capacity_retry_test.py` runs BEFORE (c9fd9c6, otto's release) in a
child built from a git archive, then this tree.

- **Gate cases.** Every captured screen and each near miss, with the hook as
  Codex left it, 10 s and 130 s after the turn started.
- **Board cases.** Real time, every timing a thirtieth of production's. Every
  pass is the real listener loop.
  - **Transient:** one retry, `DELAYS[0]` after the failure; the provider's
    answer closes the episode; the Director hears nothing.
  - **Exhausted:** three retries, each at least its delay after the failure it
    answers; then one escalation, the only Director message. This runs through 8
    listener restarts.
  - **Changes hands:** nothing is retried to the old owner.
  - **Superseded:** a Director comment that is waiting goes instead of the
    retry, and is the next turn. A draft typed under the error holds the retry,
    which goes once the line is empty.
  - **False positives:** a quoted reply and the quota error make no episode, and
    the pane is told exactly what it was told before.
- **Board-function cases,** with the clock given and production's delays:
  - not at 59 s, at 60 s; at 120 s; at 300 s;
  - a withdrawn retry is not counted;
  - one escalation, and only after the last retry's own turn failed;
  - recovery withdraws the unsent alert;
  - supersession; delivery-time currentness;
  - the inputs that must be ignored;
  - a new round before the next pass (the Audit kick-back on 4ec0e60):
    - sent to Audit and back to the same owner with a retry queued: the retry is
      not deliverable; the next pass closes the episode and discards the retry;
      the old turn opens nothing; a failure in the new round opens a new
      episode, with retry 1 of 3 after 60 s;
    - handed to another role and back within the stage with the Director's
      alert queued: the alert is not deliverable, and the next pass withdraws
      it;
    - a turn that started after the stage entry but before the hand-back opens
      nothing.

  On 4ec0e60's tree the first two fail as Audit found. The retry and the alert
  stay deliverable, the episode stays open, and the new round's failure is
  added to the old episode.

## Guard adapted

`tests/pane_activity_gate_boundary_test.py` is SYRD-475's move guard.

- **Its per-function count of reads through `notify_listener`** now includes
  `_stale_codex_busy_trace`'s one read of `capacity_retry`. That is the same way
  SYRD-538 listed `background_work`.
- **Its golden call record is unchanged.** The only new calls are one look at
  each busy Codex pane: a cursor read and then a capture, which the guard already
  sets aside after a cursor read. The comparison that let SYRD-589's `#{pane_pid}`
  reads through now lets those cursor reads through as well, and pins their
  number: exactly 12, that is six cases through both modules. Any other added
  read still fails it.

## Neighbours

29 suites that build the gate, run the listener's loop, decide a notice's
currentness, or check the schema and grants were run one at a time on main
(53f7f44) and on this tree.

- **24 pass on both.**
- **5 are red on main** with identical normalised output here:
  - `ticket_board_notify_listener_test`
  - `ticket_board_listener_pane_state_authority_test`
  - `pane_state_boundary_test`
  - `ticket_board_schema_test`
  - `ticket_board_schema_function_migration_test`
- **The move guard** was red here only, before it was adapted as above. It now
  passes with 291 checks.

## Mutation

30 mutants were run through `tests/bounded_run.py`'s `run_mutation_plan`, in
the foreground. Each SQL edit was mirrored into `schema.sql`, so the
schema-parity check could not be what killed it. All 30 were killed by
`tests/codex_capacity_retry_test.py`, each by the assertion written for it.

- **The screen.** The cursor need not be on the prompt; the error anywhere on
  the line; only the line just above the cursor.
- **The gate.** Any hook state; a moving screen, an open question, an
  unreadable cursor or a draft counts as stopped; the gate ignores the capacity
  error.
- **The report.** Any idle is a turn end; never working; non-Codex hooks
  reported; the pass never reports.
- **Delivery.** It skips the episode check; manual control does not hold a
  retry; the Director's alert is dropped as not the Director's.
- **The schedule.** Every retry waits the first delay; a retry is due only
  after its delay, not at it; nothing goes first; a withdrawn retry counts.
- **The episode.**
  - It escalates before the last retry's turn has failed.
  - The failed turn's own end counts as a recovery.
  - A moved ticket keeps its episode.
  - An older turn opens one.
  - Working withdraws nothing.
  - Any retry of an open episode is current.
  - The Director's pane is retried.
  - A future turn counts.
  - A failure is counted every pass.
  - Closing withdraws nothing.

Two of them needed the test strengthened first:

- "due only after its delay" was at first killed by an IndexError in the test.
  The function cases now report an empty queue, so the assertion catches it.
- "the failed turn's own end is a recovery" survived until the recovery case
  checked that the episode is still open after that report.

## Audit kick-back: a ticket that leaves and comes back

Audit found on 4ec0e60 that an episode stayed open when its ticket went
in_progress/main -> audit -> in_progress/main before the next pass. The
episode compared only the ticket, its state and its assignee, so a stale retry,
and the retries already spent, carried into the new visit.

The episode now keeps the round it was opened in (`ticket_assigned_at`, from
`current_assignment_at`). Both the pass and delivery compare it. The round,
rather than `entered_current_state_at`, also covers a hand-over away and back
within one stage, which does not move the stage entry.

After the fix the 29 neighbours were run again. 24 pass, and the same 5 are red
with output identical to main's apart from the worktree path. The schema-loading
six were rerun on the final SQL, with the same result.

13 more mutants were run the same way:

- the round not compared, taken from the stage entry, or not stored;
- an older turn opens an episode, or is judged against the stage entry;
- delivery ignores the ticket or its round;
- any retry of an open episode is current;
- an alert is never current;
- delivery does not ask about the alert, or the alert falls through to the
  general checks;
- delivery skips the episode check.

12 were killed by their assertions. "The round is not stored" was at first
killed by a JSON decode error in the test, until the episode reads were made to
aggregate rows. The 13th, an alert current before it was raised, could not be
killed: the alert is queued in the same statement that sets `escalated_at`, so
that condition was dead, and it was removed.

## Director kick-back: one commit, and what the red neighbours hid

06d558a, the round fix, edited pgu995 and its copy in `schema.sql`, but pgu995
had been added by its parent. The schema-function guard
(`tests/ticket_board_schema_function_migration_test.py`) checks each commit
on its own and refused it. The candidate is now one commit on 53f7f44, with
the same tree as 06d558a: pgu995 and the four functions it installs are added
together.

Here that guard had been red on main and on the candidate alike, because this
clone lacks its fixture commits (`97b852f`, `608ae70`, `856fd15`). It died
before it checked anything. Run in a clone that has them:

- it passes on main and on the new commit;
- it fails on 06d558a with the Director's message.

The other four neighbours red on main stop at their first failure, so each was
also run past it on both trees:

- `ticket_board_notify_listener_test`, case by case: the same 21 of 115 fail on
  both, at the same lines.
- `pane_state_boundary_test`, case by case: the same 3 of 12 fail on both.
- `ticket_board_listener_pane_state_authority_test`: its 19 cases pass on both.
  Its database checks stop at the same delivery assertion on both, with four
  asserts after it about that delivery.
- `ticket_board_schema_test`, each failing assertion softened in turn: the same
  4 on both, and everything after them passes.
