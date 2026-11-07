# SYRD-728: the Director's Codex turn that ended on "model at capacity"

On 2026-11-02 the Director's Codex pane (0.164.0, GPT-5.6-Sol high) ended a turn on:

```
■ Selected model is at capacity. Please try a different model.
```

and returned to its empty prompt. A person's "please retry" got the same error. When capacity came back, nothing
resumed the Director.

SYRD-594 retries this error for ticket owners through the notification queue. It deliberately leaves the Director
out, for two reasons:
- an alert about the Director's pane would wait in that same stuck pane;
- its episodes are bound to a ticket, and a Director turn may be about no ticket.

On main `63926b5d` the Director's failed turn is never retried.

## The change

**Recognition** is SYRD-594's, unchanged (`capacity_retry.turn_end_trace`). A pane counts only when all of these
hold:
- the pane runs Codex, and its hook says a turn started and has not ended (Codex runs no Stop hook for this error);
- the exact error line is at the margin, above the empty prompt;
- the pane is still, with no provider question and an empty composer.

These are left alone, and the board never switches the model:
- Codex's quota error;
- an authentication failure, a policy refusal, a model that does not exist (a safety refusal holds the whole
  account instead: SYRD-672, `docs/syrd-672-provider-safety-holds.md`);
- a reply that quotes the error;
- a pane that is not Codex.

The error line carries no retry delay, so the board's own delays are the only ones.

**Episodes.** `director_capacity_episodes` (pgu1028, mirrored at the end of schema.sql) keeps one episode for the
Director, in the database. It is keyed on:
- the Director's runtime registration: generation, process and target;
- the pane's process: the pid and start time tmux and /proc report.

It is not keyed on a ticket. If a board notice started the failed turn, the episode records that ticket, and the
retry names it.

**Which turns are the board's.** Every send records the turn it started, as the pane's own hook recorded its start:
- a Director retry: `director_capacity.run` waits, as a delivery's receipt is waited for, and records that turn on
  the attempt;
- a queue delivery: `notification_dispatch` records the witnessed turn in its `send` trace (`submission.turn`).

A failed turn is the board's only when it is exactly one of those turns. Any other turn is a person's, however soon
after: the episode closes as superseded. A send whose turn was never seen claims none, so the board stops rather
than retry over a person. Audit found the first candidate attributed a failed turn to a retry claimed within 30
seconds of it, which also took a person's "please retry", typed straight after a failed retry, for the board's.

**Retries** (`director_capacity.run`, from SYRD-594's listener pass):
- **Each pass:** the listener reports the Director's pane to `note_director_capacity`.
- **Timing:** each attempt is due at 1 minute, then 2, then 5 (`capacity_retry.delays_seconds`). The wait runs from
  the failure it answers, or from the previous attempt when nothing has come of it.
- **One at a time:** only one attempt is in flight. Anything already queued for the Director goes first, and is
  itself the next turn.
- **No duplicates:** the board claims each attempt before the listener types it, and the listener records whether
  it typed it (`record_director_capacity_attempt`). A claim whose listener stopped before recording it counts, and
  is never typed again: no duplicate prompt, across restarts.
- **Recheck:** just before typing, the pane is read again. If it is no longer the same failed turn at its empty
  prompt, nothing is typed (`not_sent`, which does not count).
- **Delivery:** the retry goes in through the ordinary sender. Nothing in the conversation is cleared.

The retry asks the Director to check what the interrupted turn already did before repeating anything (the board, git
status and log, the commands it ran), then to carry on or say it was finished.

**Stops:**
- a turn that ends normally: `recovered`;
- a person typing in the composer, or a failed turn that is not exactly a turn one of the board's sends recorded
  starting: `superseded`. That person's failed turn is recorded in the closed episode, so it opens no new one;
- a new runtime registration or pane process: `runtime_changed`.

**Exhaustion.** When the last attempt has failed too (a failure seen since it), or nothing came of it within its own
wait, the episode closes as `exhausted`. Nothing more is typed into the Director's pane, and the queue holds no
notice for it. The operator gets one alert through channels that don't depend on the Director:
- a `director_capacity` user event, which the phone bridge sends at high priority where it is configured. The bridge
  gains the kind; `user_events` now admits it without a ticket;
- a banner on the board page, from `GET /api/director-capacity` (`director_capacity_status`).

A Director turn that later ends normally clears the alert: the banner goes, and the event no longer stands.

**Privileges.** The three new functions run as their owner (`SECURITY DEFINER`). Each is closed to PUBLIC in the
migration's own transaction. rbac.sql grants the listener the report and the record, and the service the status.

**Manual recovery** is in the Director guide ("When the Director's model is at capacity"). The Director skill says
what to do when a retry arrives, and the phone-notification guide lists the new kind.

SYRD-594's ticket-owner path is unchanged: its function still leaves 'director' out.

## Evidence

`tests/director_capacity_test.py`, 20 checks. Main runs first, from a git archive.
- **The screens:** SYRD-594's real Codex captures, plus near misses built from them.
- **The board side:** a production-built board, with the real server, the real listener and the real
  PaneActivityGate.
- **The panes:** only tmux is stood in for.
- **Restarts:** a new listener every pass, and a new gate every few seconds in the long case.
- **Timing:** real time, at a thirtieth of production's delays.
- **No model is called.**

**Main:** the Director's failed turn is never retried, and there is no alert anywhere.

**Here:**
- **No-ticket turn:** a turn the Director started itself gets one retry, which recovers it. Nothing else is typed.
- **Ticket-bound turn:** a turn started by a board notice about PGU-911 gets a retry that names PGU-911, and recovers.
- **Still at capacity:**
  - three retries, one at a time, across at least 3 listener restarts, then nothing more;
  - one alert: the phone event, standing, and the board page's banner, with 3 attempts;
  - no queue notice to the Director;
  - a later normal turn clears both.
- **The board stops:**
  - a person typing under the error: superseded, and nothing typed;
  - a person's own turn that fails: superseded;
  - Audit's case, live: retry 1 is typed and fails, and 0.15 s later, before any pass looks, a person submits a turn
    that fails too. Superseded: no more retries, no alert;
  - a new registration, or a new pane process under the same registration: runtime_changed.
- **A board notice during an episode** (PGU-913 entering Analysis), whose turn also fails: the board's own turn, so
  the episode carries on and the next retry recovers it.
- **Not this error:** quota, a quoted reply, authentication, a refusal, a missing model, and a Claude pane showing
  that screen. None opens an episode or types anything.
- **`run()` itself:**
  - a pane that changes between the claim and the typing gets nothing typed (`not_sent`);
  - a sender that fails is recorded as `send_failed`;
  - a sent retry records the turn it started, and records none when the pane's hook recorded none;
  - a gate that reads no hook state names no turn and raises nothing. Other suites' stand-in gates, given to the
    dispatcher, need this.
- **The function, at production's delays:**
  - no pane identity, or no registered runtime: no episode;
  - a notice already queued goes first;
  - attempts at 1, 2 and 5 minutes from the failure each answers, or from the attempt when nothing came of it;
  - a claimed attempt is never typed again, and a second record of it changes nothing;
  - `not_sent` doesn't count, and an unknown outcome is refused;
  - the alert comes once, after the last attempt's own wait;
  - Audit's reproduction: retry 1 is recorded with its turn at t=67, then a person's turn starting at t=80 fails at
    t=85. Superseded, with no retry at t=400 and no alert at t=1000;
  - a retry sent whose turn was never seen claims none: the next failed turn stops the board;
  - the grants are as above.
- **The phone bridge** accepts the event from the board's own answer (no ticket, standing) and sends "pgu: Director
  stopped" at priority 1, on by default.
- **The page's banner,** run in node: shown for an alert; hidden when none stands; unchanged when the answer can't be
  read. A board app with no database answers that no alert stands.
- **Privileges:** main's board, then pgu1028 alone. Before rbac.sql no role may execute the three functions; after
  it, only the listener and the service. The control without the revocations lets app and ops execute them.
- **Mirror:** schema.sql's last copies are pgu1028's.

**Mutants:** 52, each killed.
- **SQL mutants**, planted in the migration and schema.sql's copy alike:
  - the runtime and pane checks;
  - the recovered, human and superseded rules;
  - the guards;
  - the already-over rule;
  - the notice's ticket;
  - counting, timing and the queue-first rule;
  - the escalation, its event, closing and clearing;
  - the record and status;
  - the `user_events` constraint and its standing;
  - each revocation.
- **Python mutants:** the pass, the recheck, the human, pane and Codex readings, and the send failure.
- **Provenance mutants:**
  - the attempt match, the trace match, or both, removed;
  - the old claim window restored, which Audit's reproduction catches;
  - the turn not stored;
  - `started_turn` naming no turn, ignoring the send time, or failing on a gate with no hook store;
  - the dispatcher not recording the turn.
- **Bridge mutants:** its kind, its priority and its default.
- **Page mutants:** the banner's hide and show, its inclusion in the page, and the route's answer on a board app
  with no database (the in-memory app), which is "no alert".
- **Grants:** two.

**Removed as a no-op:** the report's `working` field, which the Director's function never read.

To run only some parts in a mutation run, use `SYRD728_PARTS` (`live`, `functions`, `page`, `boundary`); the default
is all of them.

Nothing live was changed.
