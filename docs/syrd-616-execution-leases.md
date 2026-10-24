# SYRD-616: long commands under a role's current turn get a lease the Director can see

## The problem

A role can start a test or other command that loops forever. While it runs
under the role's current turn, the listener treats the role as busy, and none
of the alerts that watch later deliveries ever fires:
- **SYRD-557 and SYRD-570** report a pane that holds a later notice.
- **SYRD-583** reports a finished ticket's child that a notice went past.
- **SYRD-636** reports leftovers at the next hand-over.

The runaway is the role's own current work, so the ticket can stay active for
good, and nobody hears of it.

## The change

### `switchyard-bounded`

`switchyard-bounded` (`scripts/ticket_board/bounded_exec.py`) is staged for
every role. It is the production counterpart of SYRD-403's
`tests/bounded_run.py`, which stays as it is.
- **`--deadline DUR` is a hard deadline.** At it, the command's own process
  group, and every descendant that left it, gets SIGINT, then SIGTERM, then
  SIGKILL, `--grace` apart. The wrapper makes itself a child subreaper, so a
  descendant that left with `setsid` is reparented to it and stopped too.
  Whatever outlives the last step is reported, never waited for. A timeout
  exits 124, never 0.
- **`--expect DUR` declares a longer budget.** It stops nothing; it only tells
  the board not to report a quiet long test early.
- **`--run NAME --total DUR`** gives every command of a run one whole-run
  deadline. Once it is spent, the next command is refused (124) without
  starting.
- **SIGINT, SIGTERM or SIGHUP sent to the wrapper** stops the command's group
  the same way and exits 130. When the board left an interrupt marker, it says
  who asked and why.
- **Output and placement.** Output goes to files, and a bounded head and tail
  is printed. The command runs in a process group of its own inside the
  caller's session, so one tool command is one session to the board, and
  nothing outside its group is signalled.

The wrapper writes its declaration, `leases/<session>-<leader tick>.json`,
beside the pane-state files the listener already reads, and withdraws it at
the end. A declared budget is a claim by the role, so it is capped
(`TICKET_BOARD_LEASE_MAX_DECLARED_SECONDS`, 6 hours by default).

### Leases

`execution_leases` (migration pgu1008) holds one row per role and child: a
process group and its leader's start time, SYRD-583's key, so a reused pid is
a new lease. Each row records:
- the ticket that was the role's active work when it was first recorded;
- the turn generation (the hook's `turn_started_at`);
- pid and process group, and the start time;
- a command summary that keeps only what is provably safe. Environment and
  output are never read.
  - **Kept:** the program's name; option names (`-H`, `--deadline`, and
    `--name=` with its value always masked, even when it names a file); the
    name of an environment assignment; and a standalone argument that names a
    path existing on disk, relative to the process's directory.
  - **Everything else is `[redacted]`:** a value, a header, a JSON document, a
    whole `-c` script, however it is spelled. No rule that looks for secrets
    can be complete. Each of Audit's kick-backs found another carrier:
    - da0689a: the raw argv;
    - 80d9d7e: the second word of a value;
    - 5caba63: an `Authorization` header and a JSON config;
    - bb53588: `--password=<a path that exists>`.
  - **Plain ASCII, bounded with `...`:** a board in SQL_ASCII counts bytes, and
    cutting a multibyte `…` there left a row no UTF-8 reader could read. The
    status endpoint failed on it.
  - **The raw command line stays in memory only.** It is used to find a
    holder's ticket and an endless `pgrep -f`, and never written. Everything
    recorded or told (a lease, its alerts, a trace's holder details, and
    SYRD-557's, SYRD-583's, SYRD-633's and SYRD-636's reports) gets the
    summary. A holder read back from a record keeps its "never ends by itself"
    verdict as a flag.
- the deadline and its source (`default`, `declared`, `hard`, `extended`);
- the state (`running`, `expired`, `stale`, `exited`, `interrupted`);
- the last few looks of evidence (CPU ticks, bytes read and written, member
  count);
- the Director's extensions and interrupt;
- the alert's state.

Ended leases are kept a week.

### The listener's pass

`execution_lease_pass.run` runs once per listener iteration.
- **What it reads.** The process table, once for all roles, and each
  registered role's pane process (never the Director's). It looks only at
  sessions of their own, leaving out what the provider started while it was
  still starting up (`LAUNCHER_SEED_SETTLE_SECONDS`, 10 s): Codex's
  app-server daemon starts 0.4 s in (SYRD-589), and a lease on it would
  alert 15 minutes later. It never captures pane text. An idle role with no such
  session costs nothing but that one table read.
- **What it records.** A session is recorded once it has run 60 s, or at once
  when it was declared. A shorter command can reach no deadline.
- **Expiry.** Wall clock is authoritative. Past its deadline the Director is
  told once for that deadline:
  - the role, ticket, age and command;
  - pid and process group;
  - which deadline it was (the default 15-minute threshold,
    `TICKET_BOARD_LEASE_OBSERVE_SECONDS`; a declared budget; or an extension);
  - recent evidence, labelled as evidence only: silence is not a hang, and
    output is not progress;
  - the commands to inspect, extend or interrupt it, or to leave it running.

  Nothing is stopped. A hard deadline is the wrapper's to enforce, so it is
  reported only if the command is still running a minute after it.
- **Who is told.** Only the Director. The role is never told in the same wave.
- **Stale.** When the lease's ticket stops being the role's active work
  (another assignee, a terminal state, or another ticket in the role's serial
  focus), the lease is stale at once:
  - the Director is told once, with the targeted stop, through SYRD-583's
    `background_child_alerts` row and dedupe key, so SYRD-583's and SYRD-636's
    reports do not name the child again;
  - SYRD-583's origin reader (`background_child.origins`) reads a holder's
    lease ticket first, so a stale command lapses and holds no later delivery.
  - The gate no longer counts as holders a finished turn's sessions that
    neither advance nor have started anything since the turn ended. Such a
    session would hold nothing by itself (`stale_prior_turn_child_work`): a
    provider's idle daemon, a parked VM. As a holder no ticket names, it kept
    every lease from lapsing. With Codex's daemon in the pane, a stale
    CPU-bound command held the next hand-over to SYRD-212's 15-minute bound.
    SYRD-583's cycling waiters and advancing builds are still holders.

  Nothing is killed because a ticket moved.
- **Clearing.** A queued alert notice is taken back as well:
  - a new turn clears the alert;
  - an exit ends the lease and clears it;
  - the Director's extension or interrupt request clears it.

  The alert is deduplicated in the database, so a restarted listener does not
  repeat it.
- **Interrupt.** The Director's interrupt is carried out in steps across
  passes, never waiting inside one: SIGINT, then SIGTERM, then SIGKILL,
  10 s apart. The signals go to the process groups of that lease's session
  only, after checking its leader still has the recorded start tick. The role
  is then told once who stopped it and why. A wrapped command also prints it
  and exits 130.

### The Director's commands

- `ticket-board-read leases [--role R] [--all]`, or `/api/execution-leases`:
  read-only.
- `ticket-board-write extend-lease <id> --minutes N --reason ...`: 1 to 240
  minutes at a time, recorded with who, why and the new deadline.
- `ticket-board-write interrupt-lease <id> --reason ...`.

Both writes take the hold's authority (`set_manually_controlled`), checked in
the database.

### Guidance

The board skill tells roles:
- run tests, mutation plans and comparisons through `switchyard-bounded` with
  a deadline;
- declare a budget for long legitimate work;
- the Director, not they, hears of a long command.

## Tests

`tests/bounded_exec_test.py` runs `switchyard-bounded` as a role would, in a
session of its own with a pane-state directory, and names every process by a
unique `sleep` length, so "nothing left" is read from `/proc`.
- **Each case stopped, nothing left.** A silent loop, a loop that writes
  without end (its output kept as a bounded head and tail), and a nested child
  that ignores SIGINT next to one that left with `setsid`: each is stopped
  within its deadline plus grace, reported as a timeout (124), with nothing
  left behind.
- **Ordinary commands untouched.** Output and exit status pass through.
- **`--expect` stops nothing.** Its declaration is readable while the command
  runs and withdrawn after; `--deadline` is declared as hard.
- **The run budget.** A run's commands share its whole-run budget, and once it
  is spent the next is refused without starting.
- **The interrupt.** Stops the group and says who asked and why, from the
  board's marker.
- **Siblings.** A sibling in the caller's session survives a timeout.
- **Pure parts:** masking, durations, and the cap on declared budgets.

`tests/execution_lease_test.py` uses:
- a production-built board with its HTTP server and Unix socket;
- panes registered through the real `ticket-board-register-runtime`;
- a provider stand-in that starts each command in a session of its own and,
  for App, a daemon of its own at start;
- `switchyard-bounded` from the tree;
- the real listener and gate on the real `/proc`.

Four timings are scaled down: the threshold (through its setting), the
minimum age, the interrupt grace and the hard-deadline slack.
- **Main.** An unbounded loop runs past the threshold under App's turn, and
  nobody tells the Director.
- **Inside the threshold, nothing.** Past it, exactly one alert per unbounded
  command (the silent loop, the loop that shrugs off SIGINT, a decoy). It names
  the role, ticket, age, command, pid and group, evidence and every action.
  None goes out for:
  - the `--expect` suite;
  - the `--deadline` loop, which its wrapper stopped and whose lease ended;
  - a forged hard declaration inside its slack (reported once the slack has
    passed);
  - a short command, never leased;
  - an unrelated service;
  - the provider's daemon;
  - the Director's pane.

  Nothing unbounded is killed, and no pane text is read by the pass. The
  leases can be read through `/api/execution-leases`.
- **Restarts and turns.** A restarted listener tells nobody again, and a new
  turn of App's closes the alerts of the last one.
- **Extension.** The Director extends the quiet suite by 30 minutes,
  recorded with actor and reason, without restarting it. App may not, and
  999 minutes is refused by the database. Extending the expired loop puts it
  back to running.
- **Interrupt.**
  - The loop that ignores SIGINT is stopped by the escalation; only its group
    is touched, and App is told who and why.
  - The decoy, whose recorded start tick no longer matches, gets no signal.
- **Stale.** PGU-700 is finished while its commands live on. Each becomes
  stale, the Director is told once each, nothing is killed, and PGU-701 is
  handed over past a CPU-bound stale command, with Codex's daemon in the pane.
- **No secret from argv is kept.** The loop's script carries a quoted token
  and an `Authorization` header. The declared suite passes its wrapped command
  a three-word `--password`, an `Authorization: Bearer` header and a JSON
  config, plus `--token=<a file that exists>`. No component is in any lease
  row, the status output, any notice sent or waiting, or the notice trace. The
  loop is recorded as `bash -c [redacted]`, and the suite with
  `--token=[redacted]`, and `--password`, `-H` and `--config` each followed by
  `[redacted]`.
- **Records and grants.** An ended lease older than a week is pruned. Only
  the listener records leases, and only the service, for the Director,
  extends or interrupts. A schema.sql board matches the migration.

## Neighbours

- **The role-policy test** pins the two new operations.
- **SYRD-583's and SYRD-636's suites** now check the summaries their reports
  carry (`bash -c [redacted]`), and the 583 suite also checks that a waiter's
  "never ends by itself" verdict is kept in its record.
- **SYRD-467's staging guard.** Its 21 changed goldens are each proved, by a
  translator, to be exactly the old answer with `switchyard-bounded` staged
  after `switchyard-request-publication` (one more parsed entry point, four
  more quoted paths, the same five-line install block).
- **Notification eligibility.** `execution_lease` notices are always current
  for the Director, and `execution_lease_interrupted` for the role it is
  about.

## Not changed

- **Nothing stops an unbounded command automatically.** The listener signals
  only when the Director asked; `switchyard-bounded` stops only what it
  started, at its own hard deadline.
- **SYRD-403's `tests/bounded_run.py`,** and SYRD-557's, SYRD-570's, SYRD-584's
  and SYRD-636's behaviour.
- **The live tenant.**
