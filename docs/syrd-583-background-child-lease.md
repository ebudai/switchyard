# SYRD-583: a finished ticket's background shell may not hold a role's later work

## What happened

On 2026-10-11 a shell started for SYRD-579 was still running under App's Claude
18 hours after that ticket was finished. Its command ended with:

```
while pgrep -f "sweep.sh /home/switchyard-agent/syrd-worktrees/syrd-579-app" >/dev/null; do sleep 20; done
```

The shell's own command line contains the pattern, so `pgrep -f` always found
the shell itself and the loop could never end.

SYRD-581's notices to App, 6353 (the transition) and 6354 (the Director's
comment), were deferred from 04:40 to 11:36. Measured from the board's
`notification_trace` and the listener's log, read-only:
- every deferral was `pane_child_work`, at `phase: pre_send_recheck`;
- the first gate passed each time;
- the live listener runs release `2951c5c9`, which predates SYRD-557, so no hold
  alert could reach the Director.

The Director stopped that one process group by hand. App's provider stayed up,
and SYRD-581 went out within seconds.

## Why main would still hold it

Main has SYRD-557's alert, but it would still hold the notice for good, for
three reasons.

1. **Children were dated one by one.** SYRD-101 tells a finished turn's
   leftovers from this turn's work by start time: a descendant that started
   before the runtime's trusted turn end is the finished turn's. The waiter
   started before App's turn ended, but every 20 s it starts a fresh `pgrep`
   and a fresh `sleep 20`, and those always start *after*. So the tree always
   read as App's current work, `pane_child_work`, and SYRD-212's bound, which
   applies only to a finished turn's holds, never applied.
2. **The bound read the wrong probe.** Only the pre-send recheck samples a
   pane's children (`check_trusted_working=True`). SYRD-212's release was worked
   out from the *first* gate's trace, which never names a finished turn's hold.
   That first gate's idle verdict also reset the bound's clock on every pass.
3. **Nothing said which process.** SYRD-557's alert names only the role and the
   reason: no pid, no age, no ticket, and no safe way to end it.

Two more defects surfaced while proving the fix on a real board. Both are in
SYRD-212's release:
- **It discarded every handoff that reached its bound.**
  `_owner_already_notified_at` takes `max(ts)` over earlier sends, which on a
  real database is one row holding NULL when nothing was sent. NULL was read as
  the text `"None"`, so every handoff at the bound was dropped as
  `owner_already_acted` instead of delivered. SYRD-212's suite uses a fake
  connection that returns no row.
- **It dropped a comment's notice as a duplicate.** Its "already sent" check,
  which exists to stop a second copy of the same assignment, also dropped a
  notice of a later comment. It now applies to transitions only.

## The change

### The gate: a session belongs to the turn that started it

`pane_activity_gate.py`:
- `ChildWorkSample` now records each descendant's session.
- A detached session that already existed when the runtime's trusted turn ended
  belongs to that finished turn. So does every process it starts afterwards: a
  provider's tool starts each command in a session of its own, and that
  session's children are that command's.
- Such a session that has started processes since the turn ended is still
  cycling. A poll loop's latest `sleep` is the example. It is held as
  `prior_turn_child_work`: still work, but named as a finished turn's, so the
  bound and the lease below apply to it.
- A finished turn's session with only old members is
  `stale_prior_turn_child_work` and holds nothing, as before.
- The rule needs no memory, so a restarted listener reaches the same verdict.
- For each child-work hold, the gate keeps everything that would hold the pane,
  advancing or not (`child_holders`). Each holder is one detached session with:
  - its process group;
  - its oldest member, normally the shell the tool started;
  - when that member started, its command line, and its working directory.

### The listener: a ticket lease, and a bound that holds

`notification_activity_hold.py`, with `background_child.py` doing the
describing:
- **Origin.** Each holder's ticket is read from its working directory first,
  then its command. Ticket worktrees are named after their ticket
  (`syrd-579-app`). Only an id the board actually has counts, so `claude-1006`
  in a temp path is nobody's ticket.
- **Lease.** A child hold, this turn's or a finished turn's, where every holder
  was started for a ticket that is no longer the role's (another assignee, or a
  terminal state), holds nothing. The notice goes out at once. The delivery is
  recorded as `gate_escalate` with reason `completed_ticket_child`, naming each
  holder and its ticket.
- **Owned work stays held.** Work for a ticket the role still owns keeps the
  pane exactly as before.
- **Bound.** SYRD-212's 15-minute bound is also applied to the recheck's own
  trace, through `_recheck_release`. Its clock is reset only when the pane is
  busy with something else, not by an idle verdict from the first gate.
- **The Director is told, once per child.** Whenever a notice goes out past a
  child, by the lease or by the bound, the Director is told once per child. The
  notice is a new listener-only record (pgu988): `note_background_child` and
  `background_child_alerts`, keyed by role, process group and start time, so a
  reused number is a different child. It is never withdrawn: the child is still
  running. It says:
  - the pid and process group, how long it has run and since when;
  - whether a finished turn left it running;
  - the ticket it was started for, and where that ticket is now;
  - its command and directory;
  - whether it waits on a `pgrep -f` whose pattern matches its own command
    line, and so can never end by itself;
  - the one stop that ends only it:
    `ps -o pid,pgid,lstart,args -g <pgid>`, then `kill -TERM -- -<pgid>`, never
    the provider, whose pid it names. A child in the pane's own process group is
    given `kill -TERM <pid>` instead.
- **SYRD-557's hold alert** names the same children, the same way, when a child
  hold lasts past its threshold. It uses SYRD-566's described form of
  `note_background_hold`.

### Nothing is stopped automatically

No code here signals any process. A current turn (`hook_busy`), a human at the
composer and a provider's own question hold exactly as before. Delivery past a
child happens only while the runtime's own hook says its turn is over.

### Poll loops

The board skill has a new section, "Background commands":
- wait on the exact pid you started, never on a process search;
- always with a deadline;
- past the deadline, stop only that process and check that it stopped.

Its shell recipe was run in bash and zsh, for both endings: the process
finishing in time, and the deadline passing.

## Measured on a real board (`tests/background_child_lease_test.py`)

**Setup.**
- The board is production-built: schema.sql, the real ticket-board-migrate,
  rbac.sql, and the example workflow. It runs with its HTTP server and its Unix
  socket under the production `ProcessRoleAuthority`.
- App's and the Director's panes are real processes under a process named
  `tmux: server`. Each registers itself through the real
  `ticket-board-register-runtime` and then execs the provider stand-in, so the
  board decides who is registered.
- The waiters are real self-matching `pgrep -f` loops, started by the provider
  in sessions of their own from a ticket worktree.
- The listener, gate and hook store are the real ones, and they sample the real
  `/proc`.
- Each pass is timed to start just after the loop's next `sleep`. Between two
  iterations the loop's shell is briefly alone. Measured, a sample sees only the
  shell 0.81% of the time with a 1 s loop and 4.41% with 0.2 s. A probe landing
  there sees only an old shell and calls it stale, which delivers. The gap is
  the same few milliseconds whatever the loop sleeps, so it is rarer the longer
  the sleep is.

**Main, `0b3e2c66`, from a git archive:**
- PGU-581, routed to app after PGU-579 is finished, is held as
  `pane_child_work` behind PGU-579's waiter.
- 18 hours later it is still held. The Director has heard only SYRD-557's
  generic alert, with no process named.
- A restarted listener holds the Director's note too.
- Only stopping the waiter's process group by hand releases both. Each is
  delivered once, and both providers are still running and registered as they
  were.

**This tree:**
- **The lease.** PGU-581 is delivered on the first pass, once, recorded as
  `completed_ticket_child` against the waiter and PGU-579. The waiter runs from
  a directory named like an id the board does not have (`node-18-scratch`), so
  PGU-579 is read from its command.
- **The Director's notice.** The Director is told once:
  - the pid and process group, the age, and "started for PGU-579, which is now
    done (unassigned)";
  - that it can never end by itself;
  - `kill -TERM -- -<pgid>`, never the provider, with the provider's pid.

  Eighteen hours later nothing is held or sent twice, the waiter is still
  running, and nobody is told again.
- **A restart.** A restarted listener lets the Director's note past the same
  child, and nobody is told twice.
- **Stopping only the waiter's group** leaves both providers running, with
  their registration rows unchanged.
- **The bound.** A finished turn's waiter whose ticket is unknown holds PGU-590
  as `prior_turn_child_work`, still held at 14 minutes. Past the 15-minute bound
  PGU-590 goes out once, as `orphaned_prior_turn_waiter`. The Director is told
  "its ticket is not known", and the waiter is still running.
- **The hold alert.** With the threshold at 10 minutes, SYRD-557's alert for
  that waiter's next held notice names the child and the stop. The test runs
  exactly the `kill` the alert prints. The held notice is then delivered once,
  the providers stay registered, and the episode clears as `delivered`.
- **Owned work.** A CPU-bound build the last turn left running for PGU-590,
  which app still owns, keeps app's pane. A finished ticket's waiter running
  beside it does not release the pane. It is held, nobody is told, and neither
  is stopped.
- **A build known only by its directory.** Once PGU-590 is finished, that
  build, whose command names no ticket, is a lapsed lease too. PGU-595 goes out
  at once, and the Director is told once about each of the two children: the
  build, "started for PGU-590", and the waiter, "started for PGU-581, ... can
  never end by itself".
- **The pure parts, checked directly:**
  - a lease lapses for a finished ticket, even one still assigned, and for
    another role's ticket, but never for an unknown one;
  - only a `pgrep -f` that finds itself is called endless (a `[s]weep` pattern,
    a pattern in a variable and a search by name are not);
  - the stop is the child's own process group, or for a child in the pane's
    group the process alone;
  - ids are read from the directory, then the command.
- **Records and grants.**
  - One `background_child_alerts` row per child.
  - Only the listener may call `note_background_child`.
  - A fresh schema.sql board installs the same function and table as the
    migration.

**Where it ran.** The suite passes, 28 checks including its run on main, both
in an ordinary pane under a cleared environment and in a bwrap sandbox. The
sandbox has no network and a private `/tmp` and `/run`, and it refuses
`systemctl`, `sudo`, `tmux` and the like. Nothing was refused.

## Mutation

27 mutants, each run through `tests/bounded_run.py` against the new suite,
then SYRD-212's two suites (`ticket_board_stale_prior_turn_child_work_test`,
`ticket_board_orphaned_prior_turn_waiter_test`). A kill counts only when a
named check fails: a crash or a timeout does not. All 27 were killed, each at
the check that names the behaviour it broke. They cover:
- the gate's lineage, its session sampling, the cycling rule, and the holders it
  keeps, including those that are not advancing;
- the lease, both ways: off, and released by any lapsed holder rather than all;
- the terminal states;
- the bound's clock and the recheck's own release;
- both SYRD-212 fixes;
- the Director's notice: sent, once, current for the Director, and its wording
  (endless, the group stop, a pane-group child's single-process stop);
- how ids are read (directory, command, only ids the board has);
- the self-match detector's `-f` test;
- SYRD-557's alert, and the holders carried through the dispatch detail and the
  ledger.

A 28th mutant survived an earlier round: `child_holders` repeated the check its
only caller already makes of the verdict. That duplicate check was removed.

## Neighbour sweep

157 suites, each run serially under a cleared environment, first on main
`c87409de` and then on the candidate:
- The only change of result is the new suite. It is red on main, where it is
  absent, and green on the candidate.
- 18 suites are red on both. 14 have identical output.
- The other 4 differ only in ways this change does not reach:
  - three name the newest migration or hash `schema.sql`, and this adds pgu988
    (`provision_workflow_projection_boundary_test`,
    `ticket_board_park_blocked_postgres_test`,
    `ticket_board_publication_migration_recovery_test`);
  - `claude_permission_hook_test` drives the installed Claude and fails at a
    different step from run to run.

## Size

- `notify_listener.py` goes from 1,249 lines to 1,248.
- `pane_activity_gate.py` goes from 1,130 lines to 1,163, under 1,250.
- The `schema.sql` addition is the migration's own copy.

## Not changed

- The live tenant. Deploying this to syrd also deploys SYRD-557, which the live
  release `2951c5c9` lacks.
- What a pane's current turn holds.
- SYRD-557's episodes and SYRD-538's owner background work.
- Otto's own watchdog timer: see SYRD-557's note.
