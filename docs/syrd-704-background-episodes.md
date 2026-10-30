# SYRD-704: one alert per background episode, and only for a real one

## What happened

On syrd, 2026-10-29, notice 8160 for SYRD-703 waited 942 s behind a finished
turn's waiter in App's pane. It was then delivered past it (SYRD-212's bound,
SYRD-583). The Director was told about the waiter in notice 8162, once, which
was right: it was process group 3030056, 28 min old.

Then the Director got eight more notices, 8163 to 8170, one for each of:
- `postgres: io worker 0`, `1` and `2`;
- `checkpointer`;
- and four more of the same server's processes.

A test under the waiter had started that PostgreSQL server three seconds
earlier. Each notice said "running for 0 min 3 s". All of those processes had
exited before the Director could look.

Two things combined:
- **Each child was a holder of its own.** PostgreSQL puts each of its children
  in a session of its own. A holder is one session under the pane, so each was a
  holder, and SYRD-583 told the Director once per holder.
- **Nothing asked whether it mattered.** Every holder in the hold's last sample
  was told, however young, and nothing checked it was still there, either when
  the alert was recorded or when it was delivered.

## The change

`scripts/ticket_board/background_episode.py`. Delivering a notice past
background work is unchanged: what is told about it afterwards is now an
*episode*.

### Still there

Just before anything is recorded, each holder is read again from /proc. It must
be:
- the same pid;
- started at the same moment, to the clock tick;
- in the same process group;
- not a zombie.

A holder that has exited, or whose number now names another process, is left
out.

### One per command

Holders are grouped under the outermost process above them that is outside the
pane's own session. That is the shell the provider's tool started for the
command. That process leads the episode, whether or not it was itself a holder.
Its process group and command name the episode, and its key (process group and
start time) is the one SYRD-583's record dedupes on.

The alert lists the episode's other process groups: up to five by name, and how
many more. A signal to the lead's group does not reach them, so it gives the one
`ps -o pid,pgid,lstart,args -g <groups>` that shows them all.

Two commands the provider started are two episodes. Each tool command gets a
session of its own directly under the provider, so its shell is the top of its
own episode. When the pane's process cannot be read, nothing is grouped: each
holder is its own episode, as before.

### Material

An episode is told once its lead has run `MIN_AGE_SECONDS` (60 s). That is
SYRD-616's own threshold for recording a command at all, because a short
command can reach no deadline.

A younger episode is told at once when a deadline declared for any part of it
has passed:
- the deadline of its open execution lease (declared, hard, extended or default);
- or a `switchyard-bounded` declaration not yet recorded as a lease.

Whatever its age, two more are told:
- **Lapsed.** An episode whose ticket is no longer the role's: SYRD-583's lapsed
  lease, which holds nothing and is what SYRD-583 exists to report.
- **Endless.** One that waits on a `pgrep -f` whose pattern matches its own
  command line, and so can never end by itself.

A lead whose start time cannot be read is treated as material: nothing says
it is short.

### Still true when delivered

A queued `background_child` alert waits until the Director's pane is free. When
it is claimed, and again just before it is sent, the listener checks that the
process group it names is still there:
- **The process with that number** is live, in that group, and started in the
  second the alert's key says: current.
- **That number is gone or a zombie,** but a member of its group lives on: still
  current. A group outlives its leader, and the kernel does not reuse its
  number while it does.
- **Anything else** is dropped as stale, like any notice that has stopped being
  true: the group has gone, or the number now names a different process.

This applies to SYRD-636's hand-over reports too, which share the kind. An alert
whose key cannot be read is delivered as before.

### Unchanged

- **The delivery.** The notice goes out exactly when it did before, and is
  delivered in every case. Nothing here can hold it, and a failure here is
  logged and changes nothing.
- **What holds a delivery.** SYRD-583's lease, SYRD-212's bound and SYRD-557's
  hold alerts are unchanged. So is the release's record (`gate_escalate`),
  which still lists every holder.
- **One alert per child, for as long as the board remembers it.** The record
  and its key are SYRD-583's. Once an episode has been told, a later delivery
  past any part of it tells nobody again, even if only its PostgreSQL processes
  hold then.
- **Overdue work.** It is told as before, by SYRD-616's lease pass.
- **SYRD-636's hand-over report.** It still reports per session. It shares only
  the check at delivery.
- **Nothing is stopped automatically.**

## Tests

`tests/background_episode_test.py` drives the real listener and its activity
hold against a fake /proc tree. The tree is shaped as the live one was: the
pane's shell, the provider, the tool's shell in a session of its own, and what
runs under it. Ages are start times written into the tree, so nothing sleeps.
The database is a stand-in that answers SYRD-583's record and SYRD-616's leases.

The cases:
- **SYRD-703's tree.** It produces one alert, for the waiter's group. The alert
  names its command, lists the PostgreSQL groups and gives the `ps -g` for all
  eight. The record still lists all nine holders. A later delivery past two of
  the PostgreSQL processes alone tells nobody again.
- **Two short siblings.** Two sessions under a five-second command produce no
  alert, whether they exited before the release or are still running at three
  seconds old. A minute on, the same command is told once, by its shell.
- **Identity at record time.** A number reused by a new process, a zombie, and
  a process since moved to another group are not told.
- **An unknown start.** With no boot time to date it by, the work is told, not
  taken for short.
- **Distinct work stays distinct.** Two VMs started by two commands are two
  alerts.
- **Overdue work.** A 20-second command past its lease's deadline is told; one
  inside it is not. The same holds for a `switchyard-bounded` declaration not
  yet recorded.
- **Lapsed or endless.** An 11-second child whose lease names a done ticket is
  told at once, as SYRD-583 tells it. The same child for a ticket still the
  role's is not. An 11-second self-matching `pgrep -f` wait is told.
- **No pane to stop at.** Each holder is its own episode, and the short ones
  are still not told.
- **A failure.** A record that fails, or any failure while telling the
  Director, leaves the delivery as it was.
- **At delivery:**
  - `alert_is_current` accepts a live leader, and an unreadable key.
  - It refuses the same number with another start, an exited group, a lone
    zombie, a group whose last member has gone, and a number reused in another
    group.
  - It accepts a group outliving its leader, and a live member whose group
    another process leads (a holder in the pane's own session), but not once
    that member has gone.
  - Through the listener's own eligibility, a gone group's alert is dropped and
    acknowledged at the pre-send recheck, and a live one goes only to the
    Director.
