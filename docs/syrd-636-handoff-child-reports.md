# SYRD-636: background work left by earlier tickets is reported at the next hand-over

## What happened

On 2026-10-20/21, two QEMU VMs that App started for SYRD-627/629 kept running
under App's Claude through the hand-over of SYRD-624, and overnight. They took
2 x 8 vCPU and 2 x 16 GiB until the Director found them in the pane's footer
("2 shells still running") and stopped process groups 2053224 and 2053261 by
hand. App then resumed the same VMs for SYRD-631 and left them running again
once SYRD-631 was on main. They were shut down cleanly on 2026-10-21 when this
ticket arrived.

The VMs had been started from the VM kit's directory:

```
cd ~/syrd-248-vm && ./resume-vm.sh syrd627 8 16384 > syrd627.qemu.log 2>&1 < /dev/null
```

## What did not happen

The first report said the VMs had used up the provider's background-shell
slots, so that SYRD-624 "could not start the shell work it needed". Measured,
that is not so:

- **No shell limit.** Claude Code 2.1.302 accepted 12 background shells at
  once in App's pane (the two VMs plus ten `sleep 90` probes), and a foreground
  command ran beside them.
- **App's commands worked.** App's transcript shows every command in
  SYRD-624's first 21 seconds succeeding with both VMs alive. The VMs' shells
  then ended at the Director's kill.
- **No notice was held.** The live listener log shows nothing requeued for App
  between SYRD-629 leaving it at 21:26 and the listener's restart at 04:05:17.
  SYRD-624's hand-over went out on its first pass, at 04:05:57.

The Director narrowed the ticket to the failure that was measured.

## Why nobody was told

- **SYRD-583 only speaks when a delivery is held.** It describes a pane's
  children only when they hold a delivery, and tells the Director only when a
  notice goes out past one. A finished turn's session that starts nothing new
  is `stale_prior_turn_child_work`, which holds nothing. So the VMs were never
  described, and nobody was told.
- **It would have named the wrong ticket.** Had SYRD-583 described them, it
  would have said they were SYRD-248's: it read a ticket from any directory or
  command word shaped like one, and `~/syrd-248-vm` is named after the ticket
  that built the VM kit. Their commands, `resume-vm.sh syrd627` and
  `file=syrd627.qcow2`, name no ticket at all.

## The change

### A ticket is read only from a worktree git registered

`background_child.worktree_ticket(path)` walks up from a path to the nearest
directory whose `.git` file names a registration whose own `gitdir` points back
at it: what `git worktree add` writes. That directory's name gives the ticket.
None of these counts:
- a plain directory named like a ticket (`~/syrd-248-vm`);
- a clone with its own `.git` directory;
- a copied worktree, whose registration points elsewhere.

`Holder.candidate_tickets()` reads the child's working directory this way
first, then each absolute path in its command line. As before, only an id the
board has counts.

This is the one origin rule for every report: SYRD-583's lease, SYRD-557's
hold alert, SYRD-633's blocked-pane alert and the hand-over report below. The
lease now lapses only for a child whose worktree says which ticket it served.
A child that says nothing is held to SYRD-212's 15-minute bound, as an unknown
one already was. Live, SYRD-579's waiter named its real worktree, so it would
still have lapsed.

### At a hand-over, the Director is told about the leftovers

`handoff_children.run` is one of the listener's passes, run on every
iteration.

It reads the hand-overs delivered since its last pass from `notification_trace`.
A hand-over is a `transition` notice whose receipt was witnessed, sent to a role
other than the Director, in a state that is not terminal. A ticket's own
"entered Done" notice is not one.
- **The board sends those only to the owner.** Every `transition` notice to a
  role other than the Director goes to the ticket's owner. On syrd's live log
  that held for 1,702 of 1,702.
- **Only each role's newest hand-over is judged.** What is current is judged
  now, so an older one, re-read after a restart, would call the work of the
  ticket it handed over leftovers.
- **A restarted listener looks back 15 minutes;** after that it keeps its place
  by trace id.

For that role's pane, it reports each session of its own (the unit a
provider's tool starts a command in) that started at least a second before
the hand-over was sent, except:
- **Work for a ticket the role still owns,** by the origin rule above. That
  includes the ticket just handed over, and the role's queued tickets.
- **A run bounded by coreutils `timeout` whose deadline has not passed.** The
  deadline is the duration plus any `--kill-after`, counted from that
  `timeout` process's start. Once the deadline passes and the run is still
  going, it is reported.
- **Everything in the pane's own session:** the provider and the helpers it
  keeps.
- **What the provider started at its own start,** within the minute the board
  already counts as a start's own (`LAUNCHER_SEED_STARTUP_SECONDS`, SYRD-589).
  Codex starts its app-server daemon in a session of its own, 0.4 s after
  itself, and no tool can run a command before a turn begins. The first VM
  comparison found this: without it, every hand-over to a Codex role reported
  Codex's own daemon (`codex_update_banner_composer_test`,
  `runtime_switch_stall_test`). The cost is that work a tool starts in a
  provider's first minute goes unreported, which is the safe side.

Also left out:
- **A session whose start time cannot be read.**
- **A group that can no longer be read,** because it ended between the sample
  and the report.
- **One that started less than a second before the send.** Start times are
  counted from the kernel's boot time, which `/proc` gives in whole seconds,
  so such a process may have followed the hand-over.
- **The Director's own pane.**

Only the Director is told. Each child is reported once, through
`ticket_board.note_handoff_child` (migration pgu1007, executable only by the
listener). That function writes SYRD-583's `background_child_alerts` row and
uses its dedupe key, so a child named by either report is never named again.
The notice is a `background_child` notice, which the Director's eligibility
already treats as current:

> app was handed PGU-641 with background work still running in its pane from
> earlier work: pid 172358 (process group 172358), running for 0 min 10 s,
> since …, left running by a finished turn, its ticket is not known:
> `bash -c ./resume-vm.sh pgu627 8 16384 > pgu627.qemu.log …` in
> /tmp/…/pgu-248-vm. Nothing was stopped. To end it: run `sh -c '…'`: it
> signals process group 172358 only while its leader is still this process,
> never the provider (pid 172209), and says whether it stopped.

### A stop that checks before it signals

`background_child.checked_stop(pgid, start_ticks)` is one `sh -c` command. It
contains no backslash and no inner single quote, so bash, zsh and fish pass it
on unchanged. What it does:
- **Checks the leader first.** It reads the group leader's start tick from
  `/proc/<pgid>/stat`. If that is not the tick reported, because the process is
  gone or its number was reused, it prints "nothing sent" and exits 1. A pid
  and its start tick are one process, and a session leader keeps its group, so
  a reused number is never signalled.
- **Signals only that group.** Otherwise it sends `TERM` to the group and waits
  up to 10 s.
- **Says whether it stopped.** It counts members that are not zombies,
  through `ps -e -o pgid= -o stat=`. A member orphaned to a parent that reaps
  late (a child subreaper such as `bounded_run`) has stopped. `kill -0` counts
  it as running.

The stop is offered only for a session leader that is still there. For any
other holder, the report gives SYRD-583's own by-hand stop. Nothing stops
anything automatically.

### Guidance

The board skill's "Background commands" now says:
- shut down VMs and long-lived services before submitting the ticket that
  started them, and check they are gone;
- start long-lived work from the ticket's worktree, or name a path in it, so
  the board can tell whose it is;
- what the board reports at a hand-over, and what it leaves out.

## Tests

`tests/handoff_child_report_test.py` uses:
- a production-built board with its HTTP server and Unix socket;
- panes registered through the real `ticket-board-register-runtime`;
- a provider stand-in that starts each command in a session of its own;
- real `git worktree add` worktrees;
- the real listener and gate, sampling the real `/proc`.

The scenario:
- **Two VM stand-ins.** They run from a VM-kit directory named `pgu-248-vm`
  (PGU-248 is a done ticket), through a `resume-vm.sh` that keeps a long
  sleeper running with `-drive file=pgu627.qcow2`, as live.
- **Other work left running.** A sweep from PGU-627's worktree; a job for
  queued PGU-641 from its worktree; a `timeout 3600` run; and a
  `timeout -s CONT 2` run that is past its deadline but still running. They
  start after the provider's first minute, as App's VMs did, hours in.
- **The provider's own daemon.** App's provider stand-in starts a daemon in a
  session of its own at its start, as Codex does.
- **The hand-over.** PGU-627 finishes, and PGU-641 becomes App's active ticket
  and is handed over.

What is checked:
- **Main.** PGU-641 is handed over and nobody is told. Everything runs on, and
  a VM's directory reads as PGU-248's.
- **One report for each leftover.** The Director is told once each about the
  two VMs ("its ticket is not known"), the sweep ("started for PGU-627, which is
  now done") and the overdue run. PGU-641's own job, the bounded run, the
  provider's daemon and the providers are not reported, and nothing is stopped.
- **The next ticket works.** A note on PGU-641 goes out past the leftovers, and
  a job started after the hand-over runs.
- **Restarts.** A restarted listener re-reads the same hand-over and reports
  nothing again.
- **A done notice is not a hand-over.** PGU-641 finishes still assigned to
  App, and App is told so; nothing is reported.
- **The next hand-over.** PGU-650 is handed over, with a listener restart
  between its delivery and the next pass. The new listener reports what has
  since become left over: PGU-641's job, now "done (assigned to app)", and the
  job started after PGU-641's hand-over. Nothing reported before is reported
  again.
- **The Director's own pane.** A ticket handed to the Director (Audit signs
  PGU-650 off into DAT) reports nothing about the Director's own pane.
- **The checked stop.** The test runs the report's stop exactly as printed,
  with the test made a child subreaper so the VM's orphan stays a zombie:
  - it ends that VM and says "stopped";
  - run again from zsh once the leader is reaped, it says "nothing sent";
  - aimed at a reused number, it says "nothing sent" and the other VM keeps
    running.

  Only that VM stopped. The providers stay registered as they were.
- **Records and grants.** One record per child; only the listener may record a
  report; a schema.sql board installs the same function as the migration.
- **Pure parts, checked directly:**
  - the origin rule against a real worktree, one of its subdirectories, a VM
    kit directory, a clone, a copied worktree and a missing path;
  - `timeout` parsing: options, `--kill-after`, units, `timeout 0`, and things
    that are not `timeout`;
  - the stop's shape;
  - `leftovers()` over a fake `/proc`, one case per rule.

SYRD-583's `background_child_lease_test` and SYRD-633's
`blocked_pane_alert_test` cut their ticket worktrees with `git worktree add`
now, because a plain directory no longer names a ticket. Their checks are
unchanged except the lease test's unit check of where ids are read from.

## Not changed

- **Nothing is stopped automatically.**
- **What holds a delivery,** SYRD-583's lease and bound, and SYRD-557's
  episodes. Only the origin they read is narrower.
- **The live tenant.**
