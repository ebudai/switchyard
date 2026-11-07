# SYRD-735: Claude's weekly limit holds the role, and whoever shares its account

Origin: syrd, 2026-11-05/06, during the SYRD-734 reassignment.

## What happened

1. **App was held correctly.** App's Claude account hit its seven-day limit. Its pane hook recorded the StopFailure,
   and App was held until the reset (SYRD-644).
2. **The Director moved the ticket to Main.** `ticket-board-read holds` showed only App held, and Main's queue was
   empty, so the Director reassigned SYRD-734 to Main.
3. **Main hit the same limit and showed a menu.** Main's first turn met the same limit. Claude Code 2.1.318 drew its
   menu, "What do you want to do? ❯ 1. Stop and wait for limit to reset / 2. Wait here, then continue automatically
   at … / 3. Switch to usage credits".
4. **Main was never held.** No hold was recorded for it, and its pane read busy. The listener requeued every notice
   ("Pane syrd-main:0.0 is active"). The role stall (SYRD-595) escalated "busy, nothing moving" to the Director as a
   priority alert.
5. **Neither of the Director's workarounds helped.** Typing `1` and Enter into the menu recorded nothing. The ticket
   went back to App.
6. **The board stall contradicted the hold.** While App's hold stood, the board stall alert counted SYRD-734 as
   runnable with App idle, and sent the Director episode 15.

## What Claude Code actually does

All of this was measured offline. The records are in `tests/fixtures/syrd735_claude_usage_limit.json`, and the kit
is in `tests/fixtures/syrd735_probe`. The setup:
- the installed Claude Code 2.1.318;
- a placeholder Max sign-in;
- a stand-in API that refuses every request with HTTP 429 and the unified rate-limit headers (`status=rejected`,
  `representative-claim=seven_day`);
- `unshare -rn` and a private tmux.

No request left the host, and nothing was paid for. The findings:

- **The menu does not stop the hook.** With a reset 30 h away, Claude draws the menu. With 24 h, it draws "Usage limit
  reached · continuing automatically at …". Either way it runs SessionStart, UserPromptSubmit and **StopFailure**, as
  2.1.288 did.
- **The record lands after the hook starts.** StopFailure runs before Claude writes the turn's API-error record. In
  each of 4 runs, the transcript held 468 bytes and no API-error record when the hook started. In each of 3 runs, the
  record landed between 0.05 s and 0.101 s later (polled every 0.05 s), while the hook was still running.
- **So Main's hook read too early.** It read the transcript at once and found no rejected quota. The listener then
  classed the failure as a `rate_limit` throttle, and the hook leaves a throttle's state as it was: busy. That alone
  explains Main's missing hold, its busy pane, the requeues and the priority alert, menu or no menu.
- **It could also read a stale record.** The old read took the latest API-error record in the transcript's tail,
  whatever its age, so an earlier turn's limit could stand in for this one.
- **Esc dismisses the menu safely.** It returns to the prompt, chooses nothing, sends no request and runs no hook.
  Enter takes the highlighted option.
- **Each session's transcript names its account.** A `session-owner` record with `accountUuid` and `organizationUuid`
  is written at the session's start, and again at some later points. App, Main and Ops on this host carry the same
  account.

## The fix

### The hook waits for this turn's record

In `ticket-board-pane-idle-hook`, `_transcript_facts` replaces `_last_api_error`:
- **StopFailure waits for this turn's record.** It waits up to 2 s for an API-error record stamped no earlier than the
  turn's start (`turn_started_at`). It allows 5 s of slack, because the turn-start hook can stamp after a fast
  refusal is already recorded. An earlier turn's record is never taken.
- **It stops as soon as the record lands.** The measured delay is 0.1 s. With no record of this turn by then, the
  quota stays unknown, as for any non-quota error.
- **It reads the session's account.** At StopFailure, and at every normal turn end (`claude.Stop`), it reads the
  session's own `session-owner` record: from the tail, else from the head, where a long session's first one is. It
  records only a SHA-256 of "account:organization", 32 hex characters, so the account itself never leaves the
  transcript. The record names the session and the pane process: in `provider_error` at a StopFailure, and in the
  carried `last_checkout` at a turn end.

The hook stays at the 1250-line ceiling.

### One account, one hold

`quota_hold._share` runs on each listener pass, after `reconcile_provider_quota_holds`. For each standing hold, it
takes the held role's account (`account_of`: from its latest StopFailure, else its latest turn end, but only when the
record names the registered process). Then any other role whose own record names the same account, for its own
registered process, gets the hold too. It is noted through the same `note_provider_quota`:
- source `claude.shared_account`;
- the source hold's failure time, reset and message ("shares app's account: …");
- the role's own registration.

**What is never inferred:**
- **Sharing comes from evidence only.** It is never inferred from a runtime's name. Another runtime records no
  account, so mixed runtimes and distinct accounts stay apart.
- **A role whose account answered since is not held.** That is a role whose own normal turn ended after the failure.
- **A replacement process is not held** on its predecessor's record.
- **The Director's pane is never held this way**, because it has to go on hearing the board.

**How a shared hold behaves.** It is an ordinary hold:
- its deliveries and nudges wait;
- it joins the source's Director notice (same runtime and reset), so the Director is told once;
- a normal turn end, a new process or session, or the reset ends or releases it;
- a Director's clear sticks: it is not shared again, because the failure is older than the clear.

### Nobody types through the menu

`provider_prompt` recognises the menu as `usage_limit` ("Stop and wait for limit to reset"). The rule:
- **Only for a held role.** While the role's provider hold is current, the listener presses **Esc** in that pane. The
  hold itself rests on the hook's evidence, never on the screen.
- **At most twice per hold.** If Esc doesn't dismiss it, the menu is left to a person.
- **Nothing else is ever pressed.** Never Enter, and never a digit. A typed notice ending in Enter, or carrying a `3`,
  could otherwise choose "Switch to usage credits".
- **Other screens are left alone.** Other questions on a held pane are never answered. A menu on a role that is not
  held, or whose hold has ended, is never pressed. It is named as Claude's usage-limit menu, held behind as SYRD-566
  holds any question, and the remedy is shown to the Director.

So at the reset, the hold's one attempt meets a prompt.

### The board stall asks the same question as the reminders

`pgu1030_syrd735_held_owner_not_runnable.sql` redefines `board_stall_runnable`. A ticket whose owner's provider hold
is current (`role_provider_hold_is_current`, the predicate `ticket_turn_is_resolved` uses) is no longer runnable. The
evaluator says why: "app's provider is out of quota, and its work waits for the reset". During the attempt window
after release it still counts as held, as it does for the reminders. After that, the ticket counts as it did before
the hold. A held hand-over delivered at the release is progress; the hold's own recheck is not.

## What is unchanged

- Which notices a hold requeues or drops, and how holds release, renew and end (SYRD-644).
- Real busy panes: a role that is not held and has a busy pane keeps every alert.
- Codex, Copilot, agy and Hermes quota handling.
- The auto-continue variant. Claude's screen says it will continue by itself at the reset (not observed here). It is
  no menu, so the board neither presses anything nor types into it.

## Tests

`tests/claude_usage_limit_hold_test.py` uses SYRD-644's rig: a production-built board, the real listener and gate,
and the real hook program run by each pane's provider process. Claude's writing is played from the measured records,
in the measured order: the turn starts, StopFailure runs, and the record lands 0.1 s after it starts. App and Ops are
Claude roles on one account; Main is Codex.

Main runs first, from a git archive. On main:
- the late record leaves App unheld, with a busy pane;
- a role stall opens about it, the path of the live priority alert;
- an earlier turn's record is taken as this one's;
- Ops on the exhausted account is woken by a hand-over;
- nothing is pressed on the menu;
- the stall evaluator counts a held owner's ticket as runnable.

On this tree:

| Case | Expected |
|---|---|
| **The live race** | App is held to the record's reset, and its pane is idle. A comment waits. Its turn reads resolved, and no role stall opens. |
| **The hook alone** | With no record of this turn, the quota is unknown after a bounded wait (about 2 s), and the earlier turn's record is not taken. A record that lands late is taken as soon as it lands. A record from just before the turn-start stamp still counts. A long session's account is read from the head. |
| **Sharing** | Ops on another account is not held. Ops on App's account but answered since is not held. A replaced Ops is not held. Ops last heard before App's failure is held to the same reset, joins App's notice, and is never woken: its hand-over waits. The Director's pane is never held. |
| **The menu** | Held App gets one Esc, which dismisses it. Held Ops, whose menu ignores Esc, gets two, and then nothing. Another question on a held pane gets nothing. The Director, not held, gets nothing. A menu after the hold has ended gets nothing. |
| **A clear** | Ops's shared hold, cleared, stays cleared while App's stands, and its hand-over goes. |
| **The stall evaluator** | A held owner's ticket is not runnable, and the evaluator says why. `schema.sql`'s copy is pgu1030's. |
| **The reset** | App is released, and its waiting work is delivered. |

`tests/provider_quota_hold_test.py` changes in one place: its rig's transcript record is stamped with the failure's
time, as Claude stamps it. The recorded 2026-10-08 timestamp would now read as an earlier turn's record.

`tests/board_stall_user_wait_test.py` (SYRD-696) pinned `schema.sql`'s last `board_stall_runnable` to pgu1018. It now
checks it against whichever migration defines the function last, byte for byte, and still requires SYRD-696's clause.

## Mutation

21 mutants, every one killed by a check of this test, none by a crash:

| Where | Mutants |
|---|---|
| **The hook** | no wait; any record taken as fresh; no slack for the turn-start stamp; no head fallback; no account at a StopFailure; no account at a turn end |
| **Sharing** | a replacement's predecessor's account; the turn-end account ignored; the Director shared; "answered since" ignored; no sharing at all; the failure's time replaced by now (a clear would not stick); no reset |
| **The menu** | any question pressed; holds that have ended pressed; no press bound; Enter instead of Esc; no dismissal at all; no `usage_limit` recogniser |
| **The stall evaluator** | the clause removed from pgu1030's copy (killed on the production-built board); the same clause removed from `schema.sql`'s copy (killed by parity) |
