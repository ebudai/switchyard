# SYRD-793: an interrupted Claude turn holds no notice, and an idle board tells the Director

On sapi (2026-11-15), the User pasted a prompt meant for another project into the Director's pane and stopped it
with Esc. Claude Code ran `UserPromptSubmit`, so the pane hook wrote `busy`, and then nothing more. From 14:40 to
17:04 UTC:
- **The handoff waited.** SAPI-67's Director handoff was held as "pane busy": 31 attempts over 2 h 24 min.
- **The pane looked idle.** It showed the last turn's "done" line and an empty composer.
- **The stall alert left it out** as "its next action is the Director's".
- **No other alarm looks at the Director.** The per-role busy watchdog skips the Director.

The User noticed by hand.

## Measured: what Claude Code does after Esc

The probe ran offline on Claude Code 2.1.318, against a stand-in API that held the request for 60 s
(`tests/fixtures/syrd793_claude_interrupt`: the probe kit, the hook log, and the two screens):
1. a completed turn;
2. a prompt, interrupted by Esc 1.8 s later while its request was in flight;
3. 110 s of waiting.

What it showed:
- **No hook runs after Esc.** No Stop, no StopFailure, no `Notification`/`idle_prompt`: nothing at all in those
  110 s. The installed `idle_prompt` hook did not fire after the completed turn either, within the window. So the
  idle hook cannot cover this.
- **The screen is an idle one.** It shows the last turn's "done" line and an empty ruled composer. The interrupted
  prompt is not shown, and no "⎿ Interrupted" line is drawn, because no output came before the Esc. That line is not
  a reliable end of turn, so nothing here relies on it.
- **While the request runs, the screen moves.** The spinner animates and the status line ends "esc to interrupt".

## What changed

1. **The delivery gate** (`pane_activity_gate._stale_codex_busy_trace`). The stale-busy recovery that Codex panes
   have had since SYRD-584/589 now takes a Claude pane's `busy` too. The grace is unchanged: the listener's 120 s,
   measured from the hook's last word or the turn's last tool call (SYRD-695). After it, a Claude pane is served
   (`stale_claude_busy`) only when all of these hold:
   - the screen does not move across the working-timer samples;
   - no provider question is open;
   - the composer holds no text.

   Unlike a Codex pane, its hook is not rewritten to idle. A pane that really is stuck (a frozen screen, a notice left
   in the composer) still reaches the Director through the busy-pane watchdog, which reads the hook (SYRD-695). A live
   pane's next turn writes its own state.

   A Claude turn that ended on a provider error other than an exhausted quota (StopFailure, which leaves the hook
   `busy` with its `provider_error`; SYRD-644) is served the same way past the grace: that turn is over. An exhausted
   quota is written idle, and its hold is unchanged.

   SYRD-550's protections are unchanged: a spinner, a question, typed text or a recent tool call still hold the pane.
   This applies at any time, not only when the board is idle. Other runtimes are as before.
2. **The board stall alert** (`note_board_stall`, migration `pgu1043`). When nothing is runnable and no role reports
   working or running background work, tickets left out only as "its next action is the Director's" are counted.
   - **X:** the board stall threshold. That is the project's declared `board_stall.threshold_seconds`
     (`ticket-board-write set-board-stall-threshold`), else the listener's, 30 minutes by default. One knob, already
     a board setting.
   - **The digest:** after X, the Director gets one "Board idle: no role has worked for N minutes … and work waits on
     you" digest, through the same episode, clock and dedupe.
   - **Closing:** a role starting work empties the set, which closes the episode.
   - **Quiet cases:** a board with no such ticket stays quiet, and so does one with other runnable work.

   The digest itself goes to the Director's pane through the gate above. So a stale `busy` left by an interrupted
   turn no longer holds it, nor the handoff it is about.

## Tests

`tests/claude_interrupted_turn_test.py`:
- **The measurement:** no hook between the Esc and the end; the working screen says "esc to interrupt"; the screen
  after Esc has no Interrupted line and no prompt.
- **The gate, on the measured screens** (and Claude's AskUserQuestion, recorded with `probe_ask.sh`: held as a
  provider question past the grace, also with the composer check blinded):
  - recovered after the grace, held within it;
  - held by a moving spinner, typed text, or a recent tool call;
  - Hermes's busy is unchanged, and Codex's is recovered as before.
- **The sapi incident** through a real listener and gate: the Director's handoff is delivered past the interrupted
  turn's hook.
- **A real board** (board_stall_test's):
  - no open ticket: quiet;
  - a ticket in director_review with every role idle: counted, silent before the threshold, then one "Board idle"
    digest delivered to the Director;
  - a role working: not counted, and the episode closes; the same with background work;
  - other runnable work: that work counts, as before;
  - an idle board whose open work is the User's, held, snoozed or parked: quiet past the threshold, no episode; with
    the Director's own ticket added, that ticket alone counts.

12 mutants, all killed, including the question no longer holding a Claude pane, the idle clause widened
to every excluded ticket, the Claude hold restored, every runtime recovered, the idle-board clause counting
while a role works or while other work is runnable, background work not counted as working, and the Director's
tickets never counted.
