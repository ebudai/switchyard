# SYRD-589: a restored worker gets its current ticket, once

MEFP restored its worker panes on 2026-10-13. Six workers each had a current
ticket that the board showed as `active_work_delivery.state = delivered`, from
a notice sent on 2026-10-10. Every one of those panes showed a fresh Codex
0.162.0 at `Ask Codex to do anything`. The Director routed MEFP-607 out and
back. The new ordinary notice was then requeued as `pane busy` against that
idle pane (`attempts=1`).

There were two separate faults. Both were measured, and both are fixed here.

## 1. A provider that has run no turn read as busy

### What was measured

Codex 0.162.0 was run in a private tmux server, inside `bwrap` with no network
and a throwaway `CODEX_HOME` (logged in with a placeholder key, the worktree
trusted). Nothing was submitted. Its process tree:

```
    PID    PPID     SID STAT ELAPSED COMMAND
      6       1       6 S<s        8 tmux -f /dev/null new-session -d -s mefp-luna-7 -x 160 -y 45 cd /tmp/w/repo && exec codex
      7       6       7 S<sl+      8 codex
     89       7      89 S<sl       7 .../.codex/packages/app-server-daemon/releases/.../bin/codex app-server --listen unix:// ... --managed-daemon
```

Codex starts its app-server daemon as its own child, but in a session of its
own (SID 89, where the pane's session is 7). The screen itself goes still about
4.5 s after start: 60 captures over 30 s gave three digests, and the last 52
were identical.

### Why that held the pane

`PaneActivityGate` checks a pane twice:

- The **first gate** runs `is_busy`. For a launcher seed or `codex.SessionStart`
  it runs the working-timer probe, which compares four captures. That probe
  said idle (`working_timer_idle`).
- The **pre-send recheck** runs `pre_send_busy`. It also samples the pane's
  process tree (`child_work_trace`). Any descendant in a detached session
  holds the pane (`pane_child_work`), unless a trusted turn-end hook dates it
  as an earlier turn's.

A provider that has run no turn has no turn end, so its own daemon held it on
every pass. The previous provider's stale turn end does not help either,
because the daemon started after it.

The same verdict is `eligibility_busy`, so it also kept these panes out of
`idle_since_by_role` (no idle-stall nudges) and out of pull-policy pickup
(`why_not_eligible` reported "working").

The real gate, run against that real pane, gave these verdicts on main:

| hook state | first gate | recheck |
|---|---|---|
| no state | busy `no_hook_state` | busy `no_hook_state` |
| `team_launcher.start`, settled | idle `working_timer_idle` | **busy `pane_child_work`** |
| `codex.SessionStart` | idle `working_timer_idle` | **busy `pane_child_work`** |
| previous provider's `codex.Stop` | idle `working_timer_idle` | **busy `pane_child_work`** |
| `codex.Stop` written now | idle `hook_idle` | idle `stale_prior_turn_child_work` |

Only the last row, a turn ended in this provider, was ever deliverable. A
restored worker had no such turn, and the only way it could get one was by
being sent a notice.

### The fix

`PaneActivityGate.turnless_since(target)` returns when the pane's provider
started, if the pane is idle and has begun no turn since. Otherwise it returns
None.

- **Start time:** the start time of the pane's process (`#{pane_pid}`), from
  the same process table the child-work probe reads. A table without start
  times gives no answer.
- **It takes proof, not the absence of evidence.** A turn start recorded
  since the process started (`turn_started_at`, which the hook writes at
  `UserPromptSubmit` and carries across later writes) rules the pane out.
  Otherwise the current record must be one of four kinds that only a start
  writes:
  - a record written before the process started, which is the previous
    provider's;
  - the launcher's own seed of this start;
  - a `*.SessionStart` record;
  - the listener's recovery of an older turn's stale busy hook, with that
    turn's start on record.

  Any other record this provider wrote is no answer: a turn end, Claude's
  `idle_prompt`, or anything else. That keeps SYRD-101's incident shape as it
  was, where Claude's idle prompt sat on a record with no turn start.
  `ticket_board_stale_prior_turn_child_work_test` caught a first version that
  read that shape as "no turn".
- **A late seed is no answer.** A launcher seed written more than
  `LAUNCHER_SEED_STARTUP_SECONDS` (60 s) after the process started is not the
  seed of that provider's own start. A viewer's attach-or-start
  (`ensure_visible_role_session_for_viewer`) re-seeds a pane that is already
  running, and that erases the turns the hook had recorded. Every other seed
  is written within seconds of its own `tmux new-session`.

In `_trusted_idle_source_trace`, a pane with no turn skips the child-work
probe. Nothing a turn started can be running under it, so everything under it
belongs to the runtime. Every other check still applies:

- the first gate's working-timer probe (a moving screen still holds the pane);
- the cursor and composer;
- a provider question;
- a hook that says busy or blocked.

Once a turn has run, the existing rules apply unchanged, including the
SYRD-101/583 prior-turn classification and the hold on a command a turn left
running.

The same real pane on this tree:

| hook state | first gate | recheck |
|---|---|---|
| `team_launcher.start`, settled | idle `working_timer_idle` | idle `working_timer_idle` |
| `codex.SessionStart` | idle `working_timer_idle` | idle `working_timer_idle` |
| previous provider's `codex.Stop` | idle `working_timer_idle` | idle `working_timer_idle` |
| `codex.Stop` written now | idle `hook_idle` | idle `stale_prior_turn_child_work` (unchanged) |
| previous `codex.PermissionRequest` | busy `hook_blocked` | busy `hook_blocked` (unchanged) |

## 2. A delivery outlived the provider that received it

A notice is typed into one provider process's conversation. The board recorded
the send as the ticket's delivery and never looked at it again. When the pane
was restored, reloaded, or started fresh because its resume failed
(SYRD-569's MEFP Main), the send still read as delivered, and nothing told the
new provider about its current ticket.

`replaced_delivery.run`, a listener pass, now handles this. It runs before due
notices are delivered, on the same line as `pull_pickup` and `delivery_proof`,
so `notify_listener.py` stays at 1250 lines. It asks the gate for every role
whose provider is turnless and hands the start times to a new board function,
`ticket_board.redeliver_replaced_work(jsonb, timestamptz)`. That function is
SECURITY DEFINER and only `ticket_board_listener` may call it. For each role it
handles, it does the following.

- **Picks the role's current ticket** as the board's read model does
  (`is_actionable_current`, first by stage entry then number):
  - not manually controlled;
  - not awaiting another role;
  - not reserved behind a queue, and not waiting in a serial queue;
  - no unresolved blocker;
  - not in a terminal stage.
- **Queues its notice again** only when all of these hold:
  - a `send` was traced to that role in this stage visit;
  - no `send` to it is at or after the provider's start, so this provider
    never received it;
  - nothing for it is queued now, pending or dead-lettered;
  - the start time is not later than now.
- **Uses the repeat wording** from `transition_message(..., true)`:
  `<ID> -- <title> is active again in <Stage>`. The payload carries
  `redelivery: {reason: provider_replaced, provider_started_at}`.
- **Does it once per provider.** The dedupe key is
  `redelivery:<ticket>:<role>:<start epoch>`, and a key already traced as
  enqueued is never used again, whatever became of that notice: delivered,
  held, or dismissed by the Director.

The notice then goes through the ordinary delivery path and its gates, like
any other transition notice of that ticket. The test covers a declared
workflow without a pull policy. How SYRD-540's restore handling treats it for
a pull-policy author was not exercised.

A provider that has begun a turn is never asked about. Whoever started that
turn gave it work the board cannot see: the Director's bounded manual delivery
(which MEFP is doing now), or a person at the pane. A second copy would be a
duplicate pickup. A provider that received the notice itself (a send at or
after its start) is not told again, even if a viewer re-seeds its pane inside
its startup.

### Trust

The listener's account is the tenant's, which every role pane shares
([[listener-is-not-a-trust-boundary]]). A caller with that role can name any
start time not later than now. The most it can cause is the repeat notice of a
role's own current ticket, at most once per send that role receives. That is
the same class of reach the listener already has through
`notify_idle_stall_nudges` and `requeue_notification`. It changes no ticket,
owner or stage.

## Not changed

- MEFP's live panes and its pending manual recovery. Nothing here touches a
  tenant. Once installed, a worker that already got its ticket by hand has run
  a turn, so it is sent nothing.
- The first gate's verdicts, and every verdict for a pane whose provider has
  run a turn.
- A viewer's attach-or-start re-seed of a running pane. It still turns a busy
  hook into idle, as before. This change only stops trusting such a seed as
  evidence that no turn has run.

## Evidence

`tests/replaced_provider_redelivery_test.py` runs main's code and SQL in a
child process built from a git archive (BEFORE = d57749b), then this tree.

**Gate cases.** These use the real gate on the measured Codex 0.162 screen
(`syrd566_provider_screens.json`) and the measured process shape.

- On main, the launcher-seed, SessionStart and stale-turn-end panes are idle at
  the first gate and held `pane_child_work` at the recheck.
- On this tree, all three are deliverable at both gates.
- A previous provider's unfinished turn, recovered by the listener, also reads
  as no turn.
- Unchanged:
  - a turn ended here;
  - a command a turn left running;
  - a running turn;
  - a moving screen;
  - a viewer's re-seed of a running pane;
  - a turn followed by a busy-hook recovery;
  - Claude's idle prompt on a record with no turn start;
  - an unreadable start time.

**Board cases.** These run on a production-built board with the real server,
listener and gate.

- On main, the restored worker is sent nothing and its ticket reads delivered
  to the old provider. The canary's ordinary notice is requeued `pane busy`,
  traced `pane_child_work`.
- On this tree:
  - The restored worker is told once, in the repeat wording. The delivery then
    dates after the new provider's start, and later passes send nothing.
  - The canary's notice is delivered at once, alone.
  - A worker that ran a turn since its restore is sent nothing.
  - A redelivery that is held, then dismissed, is never queued or refreshed
    again for that provider.
  - A ticket sent to the provider now running is not sent again.
  - Only the listener may run the function (the service role is refused), and
    a board built from `schema.sql` alone installs the same text.

## Guards adapted

- `tests/pane_activity_gate_boundary_test.py` is SYRD-475's move guard, and
  records every call each scenario makes.
  - Its golden record is unchanged. Each of the 30 changed runs differs from it
    only by inserted `#{pane_pid}` reads, 164 in all, each asked once more to
    date the provider. Every verdict and value is identical. That was checked
    by a subsequence comparison, and the guard now makes the same comparison
    for those reads only.
  - `DEFAULTS` names the two new methods.

## Seen along the way, not changed

Once a restored pane counts as idle, SYRD-207's unresolved-turn repair can
also reach it. That repair says "<ticket> is still yours and this turn ended
without resolving it", and concerns the previous provider's last turn. The
real-loop step of the test enqueues it beside the redelivery. On main the
restored Codex pane was never idle, so it never got that far. A restored
Claude pane already reaches it today. Whether that repair should be keyed to
the provider that ran the turn is a separate question, left to the Director.

## Mutation

17 mutants, all killed by `tests/replaced_provider_redelivery_test.py`, run
through `tests/bounded_run.py mutate` in the foreground:

- the exemption;
- each of the predicate's clauses (turn start, previous provider's record,
  late seed, SessionStart, recovery, recovery without a turn start, any other
  record, busy hook);
- each of the board function's conditions (sent to this provider, sent in this
  visit, already queued, key already used, start in the future, repeat
  wording);
- the loop wiring;
- the role report.

The two that first survived ("recovery without a turn start", "busy hook")
were killed after adding `recovered_unknown_turn` and the before-the-gate
check.
