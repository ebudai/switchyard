# SYRD-595: a stalled ticket is told to one recipient at a time, a bounded number of times, across runtime switches

## What happened on SYRD Audit

This is reconstructed from read-only records on this host: the listener's log,
the retired auditor's own agy log, the runtime registrations, and the
Director's transcript.

- **11:40:18** SYRD-594 entered Audit. Notification 6613 was typed into the
  Gemini (agy 1.3.5) auditor. agy's log shows the turn failing 19 s later:
  `RESOURCE_EXHAUSTED (code 429): Individual quota reached`.
- **11:40 to 17:18** The board queued nothing for anyone. Queue ids are
  global, and the next one is 6614. So the board sent no nudges in those 5.5
  hours. agy's log shows no other input reaching the pane.
- **17:18:20** Something wrote the pane idle. Repair 6614's turn id is that
  instant. agy logged no input then, and the Director's transcript shows no
  action of theirs before 17:19.
- **17:18:57** Repair 6614 ("SYRD-594 is still yours...") was typed into
  Gemini. agy logged the same 429 a second later.
- **17:19:26** The Director switched Audit to Codex (generation 20). The
  replacement got nothing.
- **17:22:40** The Director's comment and a by-hand `directorctl send` reached
  it in the same second.

Two things are clear:

- **No redelivery.** The live board runs release 3d410ca (2026-10-12), which
  predates SYRD-589's redelivery to a replaced provider.
- **Silence, not a loop.** The 5.5 hours of silence fit agy leaving its hook
  busy after a 429: PreInvocation with no PostInvocation or Stop. That is
  inferred, not measured. An offline agy probe (a local TLS proxy answering
  Google's endpoints, with a fake account) reached agy's prompt. It could not
  run a model turn, because agy rejects every model the fake model list
  offered. No hook ran in it either.

## What main (9f762d1) does, measured

These runs use a production-built board, the real listener loop and gate, at
1/30 of production timings. Each covers 45 production minutes.

1. **A provider whose every turn fails, with a turn-end hook:**
   - The owner got 8 "still yours, this turn ended without resolving it"
     prompts, one about every 10 production minutes, with no end.
   - The Director got "may be stuck" about 1.5 minutes in, while the owner was
     being prompted, then "the grace period has passed" every ~20 minutes.
   - Both were told about the same stall, at the same time, without end.
2. **The hook stays busy after taking the work:** nothing at all is sent after
   the delivery, to anyone.
3. **A runtime switch:** SYRD-589 hands the replacement its ticket once. But:
   - a delivery the listener recorded as `send_unconfirmed` is never
     redelivered;
   - a provider that resumed the very conversation is handed it again;
   - `active_work_delivery` reads `delivered` for a send that only the retired
     runtime received;
   - a repair prompt about the retired provider's turn is typed into the new
     one.

## The fix

### One stall episode per ticket round (`note_role_stall`, pgu996)

Each listener pass reports every role's pane (`stall_episode.report`):

- the owner's own turn as its hook recorded it (state, written, turn start);
- whether the hook has said busy for longer than the grace while nothing on
  the screen moved and no question is open (`busy_still`, from the gate's own
  working-timer and open-prompt probes);
- the last line the pane shows above its cursor. This is read only for a role
  with an open episode or a still busy pane.

The board keeps one episode per ticket round (its stage entry or hand-over,
`current_assignment_at`) in `role_stall_episodes`. It is in the database, so a
restarted listener or a replaced runtime cannot fan one episode out to both
recipients.

- **It opens** when the owner's first repair prompt reaches its pane, or when
  its pane took the work and has stayed busy and still past the grace.
- **Phase `role`.** The owner's repair prompts are the attempts: at most
  `TICKET_BOARD_STALL_MAX_ATTEMPTS` (default 3) per phase. While it lasts,
  none of the Director's stall notices about the ticket are delivered:
  `unresolved_turn`, the idle-stall `nudge`, the idle-reminder and stall
  `escalation`. The permission-prompt alert is kept. The owner's second
  automatic reminder (`idle_reminder`) is not delivered either: the repair
  prompt is the one prompt.
- **Escalated** when every attempt has gone and the turn after the last ended
  unresolved too (or none came within the grace), or at once for a still busy
  pane, which cannot be prompted.
  - **The message:** one Director message, `PRIORITY <ID> -- <title>: <role>
    was prompted 3 times (times) ...`, or `took it at T and its pane has said
    busy since, with nothing moving on screen`.
  - **What it names:** what the pane shows, the runtime and its generation (and
    the one it replaced), and the ways out.
  - **The payload:** the attempts, each with the generation it reached.
  - **Then phase `director`:** no automatic prompt to the owner (repair, idle
    reminder, capacity retry) and no second Director stall notice.
- **A new owner phase**, recorded with its reason:
  - a Director comment on the ticket after the escalation hands it back;
  - the owner's runtime being replaced (a new registration generation) starts
    one too, and an escalation about the old runtime not yet sent is withdrawn.
  The new runtime gets its own attempts.
- **Closed** when the ticket moves or changes hands, or its owner writes on
  it. While the ticket is resolved for now (a snooze, a lease, its own notice
  on its way), nothing is decided about it. A first version closed the episode
  then, and SYRD-589's redelivery, which is such a notice, reopened it counting
  the old runtime's attempts.

Delivery asks `role_stall_notice_is_current` for every kind above, so the
phase decides who may be told even for notices queued earlier. It is the same
check, at the same boundary, as SYRD-594's capacity retries.

### Delivery tied to the runtime that received it

- **The read model.** `active_work_delivery` counts a send as delivered only
  if it came after the owner's live runtime registered
  (`role_runtime_assignments.registered_at`). A send that only a replaced
  runtime received reads `unconfirmed` with reason `sent_to_retired_runtime`.
  The same state and hover text the board already shows for a send never seen
  to arrive, so `app.py` (over its size limit) and the page are unchanged.
- **Which generation got a notice** is the registration history's answer at
  the send's time (`role_generation_at`).
- **Retired turns.** A repair prompt whose turn ended before the live runtime
  registered, or a capacity retry whose last failure did, is not typed into the
  new provider.

### SYRD-589's redelivery, once per runtime, never into a conversation that has it

- **An unconfirmed send counts.** A `send_unconfirmed` delivery to the old
  provider counts as delivered to it, so the new one is told.
- **A resumed conversation is not told again.** Each send now records the
  conversation it was typed into: `provider_session`, the session id of the
  pane's latest SessionStart. A provider that resumed that very conversation
  is not handed the ticket again. The redelivery pass reports the conversation
  the replaced provider announced since it started. An older record the hook
  carried forward is not its own.

## Not changed

- **agy's hook behaviour after a 429.** It is not measured, so there is no
  agy-specific recognition. A still busy pane is escalated by the generic rule
  above, after the unresolved grace (10 minutes).
- **SYRD-594's Codex capacity episode.** It is untouched. Once a stall episode
  is escalated, its capacity retries to the owner are held.
- **Every reminder generator.** Each still enqueues as before; delivery decides
  what an open episode allows.
- **Risk:** a long turn with a still screen past 10 minutes reads as stalled.
  The measured runtimes move a timer or a spinner while they work.

## Evidence

`tests/runtime_switch_stall_test.py`, before-run main 9f762d1 from a git
archive:

- **A fails, then B fails too.**
  - Main: the owner is prompted past any bound, and the Director is told in
    between.
  - Here: A gets 3 prompts, then one escalation, then nothing to anyone.
  - After the switch: main reads `delivered`; here it reads
    `unconfirmed / sent_to_retired_runtime`.
  - B is handed the ticket exactly once, and nothing reaches A's generation.
  - B gets its own 3 prompts, then one escalation naming "generation 2, which
    replaced generation 1".
  - The phases are recorded: owner, Director, owner on the new runtime,
    Director.
- **Still busy.** Main: nothing. Here: one escalation, after the grace.
- **Handed back.** A Director comment gives a new phase: 3 prompts, then one
  escalation, never overlapping.
- **Progress.** The owner's comment closes it, and the Director hears nothing.
- **Function cases:**
  - a retired turn's repair is not typed;
  - the recipient matrix for each phase;
  - a 4th prompt is not typed;
  - the escalation is current in its own phase only;
  - a resumed conversation is not told again, a fresh one is;
  - an unconfirmed send is redelivered;
  - only the listener may run or ask;
  - a board built from `schema.sql` alone installs the same functions and
    columns.
- **Session plumbing:** a send records the pane's conversation, and the
  replaced provider is reported with its own.

## Guards adapted

- **The eligibility move guard.** `tests/notification_eligibility_boundary_test.py`
  (SYRD-487's golden) records every call the delivery check makes. Its golden
  is unchanged. The one new call is the check's look at whether the board has
  stall episodes yet. It is asked once and cached, and this probe's connection
  answers no, so every decision is as before. The comparison sets aside exactly
  that call, and pins its number at 1.
- **SYRD-594's false-positive cases.** In `tests/codex_capacity_retry_test.py`
  the pane is told what otto's release told it, except the idle reminder ("is
  waiting in your ... queue"). Beside an unresolved-turn prompt, that reminder
  is now the second prompt this ticket holds back.

## Neighbours

51 suites were run one at a time on main (9f762d1) and on this tree. They
cover the gate, the listener's loop, notice currentness, every reminder and
escalation path, delivery state, pull pickup, runtime handover, the schema and
the grants.

- **44 pass on both.**
- **5 are red on main** with identical normalised output here, apart from the
  worktree path:
  - `ticket_board_notify_listener_test`
  - `ticket_board_listener_pane_state_authority_test`
  - `pane_state_boundary_test`
  - `ticket_board_schema_test`
  - `ticket_board_schema_function_migration_test`
- **Run past their first failure:** each of the five stops at its first
  failure, so each was also run past it on both trees.
  - The listener suite, case by case: the same 21 of 115 fail.
  - `pane_state_boundary_test`, case by case: the same 3 of 12 fail.
  - The authority suite: its 19 unit cases pass on both.
  - The schema suite, its failing assertions softened in turn: the same 4.
  - The schema-function migration guard: run in a clone that has its fixture
    commits, against this commit.
- **The two guards above** were red here only before they were adapted.

## Mutation

37 mutants were run through `tests/bounded_run.py`'s `run_mutation_plan`, in
the foreground. Each SQL edit was mirrored into `schema.sql`, so schema parity
could not be what killed it. All 37 were killed by
`tests/runtime_switch_stall_test.py`.

- **The episode:**
  - escalating one prompt late, or before the last prompt's turn failed;
  - a busy pane never escalated, or escalated without the grace, or for a turn
    from before the handoff;
  - no new phase for a replaced runtime or for a Director comment;
  - the owner's comment not counting as progress;
  - a moved ticket keeping its episode;
  - a new episode counting the whole round;
  - resolved-for-now closing the episode;
  - the pane's text or the replaced generation missing from the message.
- **Who may be told:**
  - the Director's stall notices going during the owner's phase;
  - the permission-prompt alert held;
  - the idle reminder as a second prompt;
  - unlimited prompts in a phase;
  - an escalated owner still prompted;
  - an escalation current in the owner's phase, or in an older phase;
  - a retired turn's repair typed;
  - delivery asking no episode;
  - the escalation falling through to the owner rule, or not dropped when its
    ticket is resolved.
- **Delivery and redelivery:**
  - an unconfirmed send not counted;
  - a resumed conversation handed the ticket again;
  - a retired runtime's send read as delivered, or not reported at all.
- **The listener:**
  - a busy pane never read as still, or a moving one read as still;
  - the pane's text never read;
  - watched roles read as bytes;
  - the report sent as `\u` escapes;
  - the pass not wired;
  - a send recording no conversation;
  - an older conversation carried forward counted;
  - the replaced provider's conversation not reported.

Nine of them first survived, and each named a gap the test now covers:

- the busy grace and the turn-after-handoff rule on the board's side (function
  cases with the clock given);
- a moved ticket closing its episode;
- the pane's text in the escalation (three mutants);
- an escalation that is not current once its episode is back in the owner's
  phase;
- an escalation not typed while its ticket is resolved for now;
- a moving pane not read as still.

## Audit kick-back on e4e948d

**1. The first pass after downtime.** A listener back after the stale-turn and
idle-stall clocks had both matured queued the owner's repair prompt and a
Director nudge on one pass. The episode opened only once a prompt had been
sent, and production delivers everything due after a single stall pass, so
both notices could go together.

- **The fix:**
  - A prompt that is queued but not yet sent now opens the episode, in the
    owner's phase. The stall pass runs before delivery.
  - Delivery also holds the Director's `nudge` and `escalation` about the
    ticket while the owner's repair prompt is pending, episode or not.
- **What is not held:** the `unresolved_turn` handoff. It comes a grace after
  the prompt, and only when the prompt is still undelivered or unanswered
  (SYRD-584). A first version of the rule held it, and the two
  unresolved-turn round suites caught it.
- **Coverage:**
  - Audit's reproduction at board level: a queued prompt, no send, opens a
    role-phase episode, and the nudge is not deliverable.
  - The recipient matrix with a pending prompt and no episode.
  - An end-to-end case: the listener is down while the turn ends and comes
    back after both graces. Its first production-shaped pass tells only the
    owner, then come 3 prompts and one escalation.

**2. Traces of text outside ASCII.** The listener's ledger wrote trace and
dead-letter details with `\u` escapes. A SQL_ASCII board rejects those, so any
notice containing such a character (here the escalation's `■ Quota exceeded…`)
was delivered and acknowledged with no claim or send trace. Both are now
written as raw text.

- **Coverage:** every stall escalation in the suite has `listener_claim` and a
  send trace, and an in-process check shows both writers carry `■` itself.

The same test file on e4e948d's tree fails three checks: the escalations'
traces, the pending-prompt matrix, and the queued prompt opening no episode.

**Re-verified:**
- `tests/runtime_switch_stall_test.py`: 32 checks.
- `tests/codex_capacity_retry_test.py`: 62 checks.
- The 51-suite sweep again: 44 pass on both trees; the same 5 are red on main
  with identical output; the two round suites pass after the narrowing.
- The unresolved-turn, reminder, redelivery and grant suites rerun on the
  final SQL.
- 6 more mutants, all killed: a queued prompt opening no episode; a pending
  prompt holding nothing; holding the permission-prompt alert; holding the
  undelivered-prompt handoff; traces as escapes; dead letters as escapes.

## Audit kick-back on 2ee63e5: a prompt from an earlier round

The pending-prompt rule matched any queued repair prompt for the ticket. One
left queued from an earlier round of the ticket is never delivered (SYRD-527:
its row predates the current assignment). It still:
- held the Director's nudge;
- opened a role-phase episode stamped with the new round, which then kept
  holding the new round's Director notices with no attempt ever made.

**Fix:**
- **A prompt counts only if it is from the ticket's current round.** Both
  checks require the queued prompt to be created in the current round, as
  delivery does. On the delivery side the round is read through `to_jsonb`, so
  a board without the SYRD-514 column stays readable.
  `unresolved_turn_repair_current_round_test` caught a first version that named
  the column directly.
- **An episode whose prompt never went closes.** A first-phase episode with no
  attempt sent, no prompt pending and no busy pane now closes as `withdrawn`,
  so a prompt dropped unsent leaves nothing holding anyone. Later phases (after
  a Director comment or a runtime replacement) are left open.

**Coverage:**
- **Audit's round trip:** a repair queued in round 1, the ticket to Audit and
  back, the new handoff gone. The Director nudge is current, and nothing opens,
  before or after the stale row is discarded.
- **A repair of this round:** it opens the owner's phase. Dropped unsent, the
  episode closes as withdrawn, and the nudge is current again.

On 2ee63e5's tree both fail as Audit found.

**Re-verified:**
- `tests/runtime_switch_stall_test.py`: 34 checks.
- `tests/codex_capacity_retry_test.py`: 62 checks.
- The unresolved-turn, reminder, idle-nudge, snooze, redelivery, eligibility
  guard and rbac suites, rerun on the final SQL.
- 4 more mutants, all killed: an earlier round's prompt opening the episode or
  holding the Director's notices; a never-sent episode staying open; the
  withdrawn close applying to later phases.
