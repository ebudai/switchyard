# SYRD-695: a turn's tool calls are the turn moving

## What happened

On 2026-10-27 Ops took SYRD-691 at 09:42:16 UTC. Its own transcript shows one continuous turn, with a tool call every
few seconds, until 10:03:55. At **10:00:54** the Director was sent:

> PRIORITY SYRD-691 ... ops took it at 09:42:16 UTC and its pane has said busy since, with nothing moving on screen.

The live board then ran release d24a15cf, which predates SYRD-675's progress clock. Main had the same blind spot,
only later:

- **Turn-level hooks only.** Claude Code's and Codex's board hooks write a pane's state when a turn starts
  (`UserPromptSubmit`, busy) and ends (`Stop`, idle), and nothing in between. Through a 19-minute turn the hook's
  `updated_at` stayed at the turn's start.
- **No lease.** No single call ran the 60 s a command needs to earn an execution lease (SYRD-616).
- **A short screen sample.** So "nothing moving" was decided by a whole-pane digest sampled over about 1.2 s. It saw
  nothing change.

On main, SYRD-675 only delays the same false alert until the board's stall threshold (1800 s by default) has passed
since the hand-over.

For Codex the same premise did more. `pane_activity_gate` rewrites a Codex hook that has said busy for 120 s, with a
still screen, to idle (`listener.stale_codex_busy_recovery`). Notices are then typed into a pane that is working.

## The record

Every tool call now runs the hook in a mode of its own:

```
ticket-board-pane-idle-hook activity --source claude.PreToolUse   (and PostToolUse; codex.* likewise)
```

The installer adds both events, with no matcher, to `~/.claude/settings.json` and `~/.codex/hooks.json`. The hook
writes `<target>.activity.json` beside the pane's state file:

```json
{"target": "...", "source": "claude.PostToolUse", "at": 1793100000.1, "pane_pid": 9460, "pane_start_time": 7496}
```

**Never into the state file.** The busy/idle state and `turn_started_at` stay the turn hooks' alone. A tool hook racing
a `Stop` therefore cannot leave a finished turn busy.

**Only the pane's own provider.** The hook records only when it was run by the pane's own provider: the same
`/proc` walk as SYRD-569 and SYRD-644. A `claude -p` or `codex exec` started from a tool shell records nothing. Any
failure is ignored, so the hook never gets in the way of a tool call. It costs about 44 ms per call: the median of ten cold runs of the activity mode.

## Which calls count

`tool_activity.turn_tool_activity_at(state, now)` counts a recorded call only when all of these hold:

- the pane says **busy**;
- the call is **at or after the turn started**;
- it came from **the process that took the turn** (`turn_process`: pid *and* start time, so a replaced provider or a
  reused pid does not match);
- it is **not later than now**.

`hook_word_at` is the later of the hook's own write and that call.

## Where it is read

| Reader | Before | Now |
|---|---|---|
| `stall_episode.busy_still` | busy past the grace since the hook's write | past the grace since the hook's word |
| the stall report's `updated_at` (the board's clock; the pass's own recheck of a queued escalation) | the hook's write | the hook's word |
| `owner_hook`, which delivery adds to a queued escalation | the hook's write | the hook's word |
| `_stale_codex_busy_trace` | 120 s since the hook's write | 120 s since the hook's word |

**`tool_activity_at`.** The report and `owner_hook` carry a `tool_activity_at` key only when a call counts. A pane
with none reads exactly as before.

**No SQL change.** `note_role_stall`'s clock already takes the latest of the hand-over, the hook's `updated_at` and
`role_progress_at`. `role_stall_escalation_stale_reason` already answers `hook_activity` when the hook's `updated_at`
is later than the clock an escalation saw. A call after an escalation was queued therefore drops it:
- at delivery, as a `drop` traced `stall_premise: hook_activity`;
- or at the next pass, as a `discard` traced `stall_premise_hook_activity`.

Either way the next pass ends the episode.

**Unchanged:**
- the execution-lease rules;
- the runtime-replacement and progress reasons;
- the deduplication of stall notices;
- how a pane with no tool calls is judged.

A turn quiet past the threshold since its last call still escalates, once.

## Codex trust

The two Codex entries are new hook content. On a fresh owner, `switchyard new` seeds their trust with the rest. An
owner whose trust records predate them approves them in Codex's `/hooks`, the existing path for any hook-content
change. Until then, that pane's tool calls are not recorded, and it is judged as before.

## Tests

`tests/tool_activity_evidence_test.py` runs the real setup, main first from a git archive:

- the real installer, into a disposable home;
- real provider processes under a process named `tmux: server`, running the hook commands the installer wrote;
- the real notify listener (`listen_once`) over a production-built board, with still screens.

Cases:

- **control:** a busy Claude turn with no call escalates once on both.
- **SYRD-691 / Codex:** a run of short calls on main still escalates, and Codex is called over and typed into. Here
  there is no escalation and no episode, the state file is byte-for-byte untouched, and the Codex turn stays busy
  with nothing typed into it.
- **Silence and quiet:** after silence past the threshold, one escalation, bound to the last call. With quiet past
  the grace but not the threshold, none yet.
- **Queued, then a call:** dropped at delivery, or withdrawn at the next pass, and the episode ends.
- **Calls that are not this turn's:** a nested `claude -p`, a replaced provider, an earlier turn, a reused pid, or a
  future time. All still alert, and none is shown as the owner's hook evidence.
- **After the turn ends:** a call after `Stop`, or after the listener calls a Codex turn over, is not the idle pane's
  word.
