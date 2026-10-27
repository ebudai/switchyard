# SYRD-688: a launch starts idle pull workers only for work

## The problem

MEFP declares fifteen detached, ephemeral Luna implementers under a pull
policy. An ordinary `switchyard mefp` started all fifteen provider CLIs, though
the board said none held a ticket. The host spent a long burst on more than ten
simultaneous Codex start-ups.

Two rules together made that the only way to keep pull capacity:
- `_start_workers_and_present` started every detached role;
- pull pickup gives work only to a worker whose registered provider process is
  already running.

## What a launch starts now

Every role starts as before, except an idle **pull claimant**: an active,
ephemeral implementer that the board's pull policy names (`pull_claimant_pool`)
and that the launcher configuration runs detached. Before starting any
detached role, a launch with mode `start` or `attach-or-start` asks the
project's admission once (`scripts/worker_admission.py admit --mode launch`). It
runs that as the project account. The child reads the board's own answers:

| Read | What it gives |
|---|---|
| `/api/workflow` | the claimant pool, each claimant's runtime, and the cohorts |
| `/api/reservations` → `pull_queue` | ready work, `claimable`, and the author a rework ticket `waiting_for` |
| `/api/provider-holds` | quota holds that are current |
| `/api/context-recovery/<role>` | whether the claimant holds an active ticket or a decided continuation |
| `/api/cohorts` | each cohort's effective ceiling and the tickets it holds |

Each claimant gets one decision. The launch prints it:

| Decision | When |
|---|---|
| `active-ticket recovery` | it holds an active ticket, a continuation, or a ticket not yet handed over; it starts and resumes as SYRD-626 decides |
| `ready-work demand start` | ready unpinned work needs it, or rework is pinned to it |
| `retained warm worker` | `warm_workers` in the launcher configuration keeps it up |
| `already running` | its session is live; it counts as capacity |
| `left stopped: no work needs it` | none of the above |
| `left stopped: …` | a current quota hold, an operator stop, a failed start, its cohort's capacity, or a model or effort off its cohort's pins |
| `left stopped: admission could not decide` | the board did not answer after three asks, or admission could not run |

**How much ready work starts.**
- Unpinned claimable tickets, minus the idle claimants already running or
  starting, is how many start.
- A ticket pinned to an author starts that author and nobody else.
- `warm_workers` adds that many idle workers on top.
- The fixed decisions above come first. Ready work then goes to the remaining
  claimants in pool order (`luna-2` before `luna-10`).

**What a launch leaves out.** A claimant left stopped is left out of the
launch's session-record report and its provider-state record, so its absence
is not reported as a failed start.

**When nothing is decided.** The child asks the board three times (pausing 0.5
s, then 1.5 s). If it still cannot answer, or admission cannot run at all,
every detached ephemeral role is left stopped, never started blind: starting
the whole pool on a failure would recreate the burst this exists to prevent
(Audit). The launch says so, and how it recovers: the listener's next ask, once
the board answers, recovers held work and starts what ready work needs, or the
operator runs `switchyard <project>` again. A `reload` does not ask at all.

## Ready work after the launch

After every pickup pass, the tenant's notify listener checks what is left
(`ticket_board/demand_start.py`). It asks only when one of these holds:
- claimable ready work is left that no running worker took;
- a ticket is pinned to an author that is not running;
- a worker holding a pulled ticket is not running: held work a launch could
  not recover, or a worker that died.

It passes on the running claimants it saw unable to take work -- working, on
a prompt, or running background work -- so the child never counts a busy
worker as idle capacity (Audit). A worker whose readiness is still unknown
counts: that is how a just-started worker reads.

To ask, it runs the same child with `--mode demand`, for its own project, as the
project account it already is. The child decides again under the lock, from
the board and from the project's own tmux sessions. It starts at most two
workers per ask, using the pane start `switchyard worker-pool start` makes, and
records each start's outcome. A stopped claimant holding work is recovered
first, as a launch would.

**What the listener runs.**
- No other command, one child at a time, and at most one ask a minute. A
  finished child's decisions are logged on the next pass.
- A listener without process authority, whose roles run as other accounts,
  asks for nothing.
- The board gains no authority to launch processes.

The started worker registers as a live, idle provider. The next pickup pass
claims for it, through the same `claim_ready_ticket` as before.

## What stays respected

| Rule | How |
|---|---|
| **Atomic selection** | Every decision is taken under an exclusive `flock` on `<session dir>/.worker-admission.lock`. Each start is recorded in `<session dir>/worker-admission.json` until its worker's session is live, or for `START_GRACE_SECONDS` (180) at most. Concurrent asks, or an ask during a launch, count those starts as capacity and never start one worker twice. |
| **Declared capacity** | A provider cohort (SYRD-628) bounds every start beyond recovery: never more of its members running than its `target`, never a worker to claim beyond the board's effective `ceiling`, and never a worker whose model or effort is off the cohort's pins. That is the rule `worker-pool converge` follows. Without cohorts, the claimant pool is the bound. |
| **Quota holds** | A claimant whose provider hold is current is never started for demand or warmth. |
| **Operator stops** | `worker-pool stop` and a `converge` stop record the worker as the operator's. Demand never starts it until `worker-pool start`, `restart` or a `converge` start clears that. |
| **Failed starts** | A start whose pane command failed, or whose worker is not observed running, is recorded with its reason. That worker is left stopped for `FAILURE_BACKOFF_SECONDS` (600), and the work goes to another. |
| **A stopped project** | `stop` (and so `switchyard stop`) first writes a stop record, as the project account and under the lock, so a start already under way finishes before it and the session sweep then ends that worker. Every demand start holds the lock through its pane start and asks again, under it, whether the project is stopped -- by that record, or because none of its core roles (every role that is not a claimant) has a live session -- and whether the worker is already running. The next launch clears the record. If the record cannot be written, `stop` still ends every session, sweeps them a second time (ending a worker whose start landed during the first sweep), and answers nonzero, saying the stop is not clean and to run it again; a demand start that completes after the sweep finds no core session and ends the worker it just started. |
| **Holders** | A stopped claimant that holds work recovers, at launch or on demand, before any new capacity. |
| **Inactive roles** | Inactive roles are not claimants; the pool is active roles only. |

## Tests

**`tests/worker_admission_test.py`**
- The plan, case by case: no work, demand, a running idle worker, a start
  under way, recovery, continuations, pinned rework, holds, operator stops,
  failed starts, warm workers, and MEFP's 4+4 cohorts (target, ceiling, pins).
- The admission child on a fifteen-Luna MEFP-shaped configuration with the
  board's reads stood in for, including three concurrent asks, the lock held
  by the test, and Audit's two races: an ask in flight while the sessions go,
  and while `stop` records (the record's own command line waiting for a start
  under way).
- `stop_project` writing its record before touching a session, through the
  real child; and, with the record's child failing or not runnable while a
  demand start is under way, a nonzero stop, two sweeps, and no worker left
  running whether the start lands after the sweeps or during the first.
- A busy running worker beside a stopped one (Audit's case), and the bounded
  answer when the board or the child fails.
- The real worker-start phase, counting every provider start. A no-work
  launch starts the core roles and none of the fifteen Lunas, with one
  admission child, in under five seconds.

**`tests/demand_start_test.py`**
- The real listener on a disposable board, with the real admission child run
  against that board's HTTP reads.
- A ticket admitted after a no-work launch starts exactly one worker, and the
  next pass claims for it.
- Two more tickets with one worker busy start the other two in one ask. With
  every worker running, nothing more starts.
- Asks are spaced, never overlap, and need process authority.

## Not covered

- **Live acceptance on MEFP.** The live check is an MEFP cold start with no
  Luna work, then one admitted ticket. That is the operator's run on the host.
- **Earlier stops.** Stops made before this change were not recorded. A worker
  an operator stopped by hand earlier can be started for demand, within its
  cohort's target, until it is stopped again with `worker-pool stop`.
