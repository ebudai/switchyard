# SYRD-628: provider cohorts in a pull pool

## The ask

MEFP runs an eight-worker pull pool split by hand across two providers: four
Claude Sonnet 5.5 (effort high) and four Codex GPT-5.6-Luna (effort xhigh).
The split preserves both providers' usage headroom, but the Director keeps it
role by role. This makes the split declarative. The board enforces it on every
claim, and the operator CLI converges the live pool to it without interrupting
any turn.

## A cohort is scheduling capacity

The workflow's pull policy gains `scheduling.cohorts`, one entry per runtime:

```json
"cohorts": [
  {"runtime": "claude", "target": 4, "ceiling": 4, "model": "claude-sonnet-5-5", "effort": "high"},
  {"runtime": "codex",  "target": 4, "ceiling": 4, "model": "gpt-5.6-luna",      "effort": "xhigh"}
]
```

- **Membership** is a claimant's declared runtime: every active, ephemeral
  implementer that owns the implementation stage (SYRD-573's pool). A role
  outside the pool has no cohort. So does a pool member whose runtime no cohort
  names: it is reported as uncohorted, and nothing limits or moves it.
- **`ceiling`** is how many tickets the cohort's members may hold at once,
  counted by the serial reservation every claim already respects
  (`ticket_current_reserved_ticket`).
- **`target`** is how many of its members should be running.
- **`model` and `effort`** are pins. They live in each worker's launcher
  configuration, and nothing here rewrites them. Convergence only starts a
  worker whose own model and effort match.
- **Validation** is done twice, in Python (`scheduling_cohorts.validate_cohorts`)
  and in SQL (`validate_declared_scheduling`), and the suite compares the two
  answers case by case.
- **Provenance:** changing the policy is a workflow revision, so who changed it,
  and when, is recorded like any other revision.

## The board enforces the ceiling on every claim

`cohort_claim_refusal(role)` is asked by all three claim paths:

| Path | Where | Refusal |
|---|---|---|
| `claim-next` | `claim_ready_ticket`, after `holding` | `{"claimed": false, "reason": "ceiling", "detail": "the codex cohort holds 4 ticket(s), its ceiling is 4"}` |
| the listener's pickup | `claim_ready_ticket` | the same; pickup reports only `nothing ready` as idle, so a capped worker is never called idle capacity |
| the canonical claim action | `lock_pull_assignment` | raises "role X cannot claim now: ..." |

**Locking.** The refusal takes a cohort-wide advisory lock and then counts with
a fresh read, so two members cannot both count the cohort under its ceiling.
Lock order is the same on both paths: the per-implementer lock first, then the
cohort lock.

**Other cohorts keep pulling.** A refusal is a cohort's, never the pool's.

**Concurrent claims.** On main, claims made at the same moment returned "nothing
ready" while work waited, with no cohorts involved at all. `claim_ready_ticket`'s
`FOR` loop fetched candidates in batches, so its `FOR UPDATE SKIP LOCKED`
locked every ready ticket at once, and a concurrent claimant skipped them all.
On the previous release's schema, 8 concurrent claims against 10 ready tickets
gave 2 claims, then 1. The loop now takes one candidate at a time
(`LIMIT 1 ... SKIP LOCKED`, remembering what it tried). Under the same race the
ceiling holds exactly, and the other cohort claims alongside.

## Quota

`cohort_quota` holds one record per cohort and source:
- `operator`, the Director's (`ticket-board-write set-cohort-quota <runtime>
  --ceiling N --reason ... [--expires-minutes M]`, and `clear-cohort-quota`);
- or a provider signal's, under its own name.

A cohort's effective ceiling is its declared ceiling, lowered by every record
still in force. 0 is exhausted: running workers drain, and nothing new is
claimed or started in that cohort. Clearing or expiry is recovery.

`note_cohort_quota_signal(runtime, source, ceiling, reason, expires)` is the
listener-only plug for a provider's own usage telemetry. A signal is a record
that must be renewed before it expires, and only a change is an event. No
provider exposes reliable usage today, so nothing calls it yet. Status says so
plainly: "usage telemetry: none; static limits only".

## Convergence never switches a runtime

`switchyard worker-pool <project> converge` brings each cohort's running count
to its target by stopping and starting *declared* workers, as MEFP already
rotates which eight of its fifteen Lunas run (SYRD-573). It never calls
`set-role-runtime`, which hard-restarts a pane and loses its conversation.
Every runtime, model and effort choice is therefore kept exactly as configured.

**Each pass makes at most one stop and one start:**
- **Stop** a worker in a cohort over its target only if all of these hold:
  - it is running;
  - it is idle by the pane gate, which fails closed: no record reads as busy;
  - it holds no ticket;
  - its serial slot is reported;
  - it is not on a prompt;
  - no rework is pinned to it.

  The highest-numbered such worker goes first.
- **Start** a stopped worker in a cohort under its target only if all of these
  hold:
  - its model and effort match the cohort's pins;
  - `worker-pool start` would not refuse it;
  - the cohort is not exhausted;
  - the pool runs fewer workers than its targets add up to, counting this
    pass's stop.

  A stopped worker that rework is pinned to starts first.

  The last condition makes a right-sized pool with the wrong mix converge by
  replacement, one drained worker at a time, never running more sessions than
  it was sized for.

  **A replacement waits for its stop.** A start that fits only because of the
  pass's stop names that stop (`Move.after_stop`). It is carried out only if
  the stop actually happened. If the drain found the worker had just taken a
  ticket, or a turn began, or the stop failed, the start is recorded as
  deferred ("it was to replace luna-12, which was not stopped") and the pool
  stays at its size. A start into a pool under its size needs no stop. Audit
  found this on 80c9999: a raced drain left Claude 3 / Codex 6, nine sessions.
- **Everything else is deferred, with the reason:** busy, holding a ticket,
  pinned, off-pin, blocked, exhausted, or the pool at its size. Active turns
  and tickets drain naturally.

**A stop is bracketed by a drain.** `begin_cohort_drain` takes the same
per-worker lock every claim takes, then marks the worker. From then on,
`cohort_claim_refusal` refuses its claims ("luna-8 is being stopped by
convergence"). A claim already under way finishes first and is returned as
`held`, and the worker is then kept and the deferral recorded. Convergence
also asks the pane gate once more after the drain. The drain is short-lived
(600 s by default) and is ended in a `finally`.

**Every move is recorded on the board** (`cohort_events`) as intended before it
acts, then as done or failed after. Each record carries:
- the source: `operator` for a single run, `auto` under `--watch`;
- the actor and the reason;
- what worker-pool said.

Deferrals are recorded too. Under `--watch`, an unchanged set of deferrals is
recorded once, not every pass. If a record cannot be written, nothing acts.

**A hand-run start, stop or restart is recorded too.** `switchyard worker-pool
<project> start|stop|restart` of a cohort member changes the cohort as surely as
convergence does, so it is recorded the same way, as the operator's: intended,
then done or failed. A record the board refuses is printed, not hidden. The
session move itself goes ahead, because start and stop are also how an
operator recovers a worker.

**Each pass is computed from what is observed:** sessions, the pane gate and
the board's reservations. A launcher restart, a lost provider process, a stale
registration (its session is gone, so it is not running) or a worker that went
idle since are all simply the next pass's facts.

`--dry-run` prints the plan with "would", including who is deferred because
they are busy. It changes and records nothing.

## Status

- **`switchyard worker-pool <project> cohorts`.** For each cohort:
  - target, ceiling and its current effective value, holders, and pins;
  - live, busy and idle counts, with deficit or excess;
  - quota records;
  - each worker's state: busy and why, idle, stopped and startable, off-pin,
    blocked, draining, or with rework pinned to it.

  Then uncohorted members, and the plan.
- **`GET /api/cohorts` and `ticket-board-read cohorts`:** the board's side. That
  is the policy, effective ceilings, holders, quota records, draining workers,
  the telemetry state, and the last 50 changes.

## Deliberately not done

- **No daemon.** The board never starts a worker. Convergence runs when the
  operator runs it, once or with `--watch`.
- **No runtime, model or effort rewrites.** A cohort whose declared workers are
  all off-pin, or all blocked, is reported as unable to fill. Fixing a worker's
  configuration is the existing `set-role-runtime` path, an operator decision.
- **Persistent implementers** (`main`, `ops`) are not in the pool, so no cohort
  limits them, as before.

## Tests

- **`tests/provider_cohort_claims_test.py`:** the board.
  - Python and SQL validators agree.
  - On main the policy is refused, and six Codex workers claim six tickets.
  - The ceiling holds on claim-next and on the canonical action, on fresh,
    migrated and upgraded boards, with a mixed eight-worker pool started
    imbalanced (2 Claude, 6 Codex). The Claude cohort keeps pulling, and a
    released slot is claimable again.
  - Eight concurrent claims never pass the ceiling and never share a ticket.
  - Quota: exhaustion, recovery and expiry, a signal and its dedupe, and only
    the listener may signal.
  - Convergence records: the Director's only, naming a member.
  - `ticket-board-read cohorts` output.
  - The real listener pulls for the open cohort, never calls the capped one
    idle, and pulls for it again after recovery.
  - Schema and migration carry the same functions.
- **`tests/provider_cohort_convergence_test.py`:** convergence, pass by pass,
  over a real board and a simulated fleet that fails the test if a stop ever
  lands on a busy or holding worker.
  - A twelve-worker bench with eight running, all busy: nothing moves.
  - Right-sized and imbalanced: a raced drain, and then a failed stop, each
    leave the pool at eight, with the replacement start deferred. Under its
    size, a start needs no stop.
  - One-at-a-time drain to 4/4, skipping an off-pin worker.
  - A released ticket drains.
  - A lost process is restarted.
  - Exhaustion holds starts; recovery releases them.
  - A claim racing a stop is caught by the drain.
  - Dry run.
  - `--watch` deferral dedupe.
  - The plan's own rules.
  - The default wiring reaches worker-pool's own stop and start.
