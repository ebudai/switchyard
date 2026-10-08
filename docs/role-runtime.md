# Changing a role's agent runtime

`switchyard set-role-runtime <project> <role> --cli <runtime>` moves an existing
role from one agent CLI to another. It is a Director command and needs no root:
the board authorizes the workflow change by caller role, and everything else it
touches belongs to the project owner.

```bash
switchyard set-role-runtime syrd audit --cli agy --dry-run   # every check, no change
switchyard set-role-runtime syrd audit --cli agy
switchyard set-role-runtime syrd audit --cli agy --force --reason "provider outage"
```

## Why it is one command

Changing a role by hand means editing generated JSON, applying a workflow
change, restarting one worker, and putting its display slot back — four steps
that fail independently. The failure that prompted this is the quiet one: a
display slot's proxy is an attach to a worker session, so when that session is
replaced the proxy's session-closed hook parks it on a recovery message and
nothing re-attaches it. The replacement worker runs perfectly well behind a
blank pane, and every other signal says the change succeeded.

## What it does, in order

Nothing is written until everything that can be checked has been:

1. The role exists, is one the launcher starts, and the requested runtime is
   supported. Switching to the runtime already configured is reported as
   nothing to do, not an error.
2. The new runtime could actually start — installed, logged in, and trusting the
   role's worktree — using the launcher's own first-run checks, so a runtime
   that passes here is one the launcher would also accept.
3. The role is not mid-turn. This is the launcher's activity gate, which fails
   closed: a live session whose hooks have written no state reads as busy,
   because the alternative is ending a turn on the strength of an absent
   record. `--force` requires `--reason`, which is recorded with the change.
4. The board validates the proposed workflow document without applying it.

Then, journalling each step so it can be undone in reverse:

5. Apply the workflow document, at the revision the preflight read, so a
   concurrent change is refused rather than overwritten.
6. Point the launcher config at the new runtime. Resume settings are rewritten
   from the new runtime's defaults, because a resume flag belonging to the old
   CLI would be handed to one that cannot read it.
7. Stop the old session, clear the resume record — it belongs to the runtime
   being left behind — wait until the stopped pane's process has really gone,
   and start the replacement. On a board that takes runtime registrations, the
   replacement counts as started only once the board's own record names it
   (see below).
8. **Only then**, reconnect every display slot mapped to that role. Reconnecting
   into the gap between stop and start attaches the proxy to nothing and parks
   it again for the same reason.

A detached role, or one no slot is showing, simply skips the last step; that is
reported as "no display slot showed it" rather than passed over in silence.

## Handing the board registration over

On a process-authority board, a role's pane registers itself before its CLI
starts, and the board refuses that registration while the role's recorded
holder is still a live process ("role main is held by a live pane"). Stopping a
session with `tmux kill-session` only hangs the pane up; its process can take a
few seconds to leave. Starting the replacement straight away therefore lost the
race every time: the new pane was refused, closed about 47 ms later, and the
command reported a switch that left the role unregistered (SYRD-559).

So the switch:

- notes who holds the role before it stops anything: the pane's root process,
  which is what a pane registers as, and the board's own record of the holder,
  read before the workflow changes (afterwards the board no longer shows a row
  for the old runtime, though its refusal still counts it);
- after the stop, waits up to 15 seconds for those processes to exit, then sends
  SIGTERM and waits 5 more, then SIGKILL and 3 more. Signals go through a pidfd
  checked against the process's start time, so a reused pid is never hit;
- starts the replacement only once none is left. A holder that outlives all
  three steps stops the switch with nothing new started, naming the pid;
- after the start, waits up to 30 seconds for the board's runtime assignment to
  name the new pane's own process. A pane that closes first is reported at once,
  with its registrar's refusal as the place to look; a row naming any other
  process is not accepted.

Any of these failures undoes the switch like any other. The undo restarts the
previous runtime the same way, waiting both for the attempt it stops and for an
original pane that never left.

## A role that is not running

It stays not running. Changing a runtime is a configuration change, and starting
a role nobody asked to start would alter the running shape of the team as a side
effect; an operator who wants it up launches the project, and it comes up under
the new runtime when they do. The command says so — "it was not running, and
will start as `<runtime>` at the next launch" — because that is a different fact
from "nothing happened" to someone deciding what to do next.

The resume record is cleared even then. It belongs to the runtime being left
behind, and a stopped role that kept it would hand the old CLI's session id to a
new CLI that cannot read it at the next launch.

Steps 7 and 8 are therefore skipped: there is no session to replace, and nothing
for a slot to reconnect to.

## When something fails

Every applied step is undone and the command says the switch was undone. The
undo is not a strict reverse: the launcher config and the workflow document go
back first, and only then is the worker restarted, because restarting it while
the config still named the new runtime would bring the role back up under the
CLI that just failed to start.

Stopping the worker is journalled the moment it happens rather than after the
replacement starts. A stop recorded only on success is invisible to the undo if
the start then fails, which leaves the role down while the command reports a
clean rollback — the same class of silent failure this command exists to
prevent. A rollback is only called clean once the previous session is proven
live again, and, where the board takes registrations, registered again. If the undo cannot finish, the command says so explicitly, names what is
still wrong, and leaves the journal in place:

```
.../role-runtime-<role>.journal.json
```

That file holds the previous runtime, the previous workflow document and its
revision, the previous launcher config, and which steps were applied — enough to
finish the repair by hand. It is deleted on success and on a clean rollback, so
its presence means an operator is needed.

## What it records

The result distinguishes configured state from live state: whether the runtime
changed, and whether a live session was actually replaced. A role that was not
running is reconfigured without claiming its session was restarted.

## Previews and the board's locks

Step 4's preview, and every later workflow apply, runs the board's
`apply_declared_workflow` in full and then rolls it back. That function rebuilds
the stage graph and alters constraints on `tickets`, so even a preview takes
`AccessExclusiveLock` on `workflow_stages` and `tickets` for the moment it runs.
On MEFP (SYRD-572) this went wrong two ways, and nothing had a timeout to end
either:

- concurrent previews deadlocked: each read the serial reservations, holding a
  share lock on `tickets`, before reaching the workflow's advisory lock;
- a ticket read held `tickets` and then, on a second connection, waited for
  `workflow_stages`, which a preview held while it queued for `tickets`.
  PostgreSQL saw only a session idle in a transaction, and every later reader
  queued behind the preview for fifteen minutes.

So, on the board (`scripts/ticket_board/workflow_locking.py`):

- an apply or preview takes the workflow advisory lock first, before anything
  holds a share lock, and waits at most 30 seconds for another one to finish;
  that wait blocks no reader;
- its table locks are waited for at most 5 seconds, because readers queue behind
  a pending exclusive request; the whole statement is bounded at 60 seconds;
- a wait that runs out or a detected deadlock rolls back, which changes nothing,
  and the apply is tried up to 3 times in all; then the command is refused with
  "nothing was changed. Retry once they finish";
- a ticket read takes `workflow_stages` before `tickets`, on its own connection
  and transaction, so a read and an apply always lock in the same order.

## If a board's ticket reads hang anyway

Symptom: `/api/workflow` and `/api/runtime-assignments` answer at once while
`/api/board` and ticket reads time out. Do not restart PostgreSQL, kill processes
or edit tickets. As `postgres`, with your own short timeouts so your session can
never join the queue (`SET statement_timeout='5s'; SET lock_timeout='3s';`):

1. Find the wedge, read-only:

   ```sql
   SELECT pid, state, now()-state_change AS age, backend_xid, wait_event_type,
          pg_blocking_pids(pid) AS blocked_by, left(query, 80)
   FROM pg_stat_activity WHERE datname = '<project>_ticket_board' ORDER BY xact_start;
   ```

   The pattern is one `SELECT ticket_board.apply_declared_workflow(...)` waiting
   on `Lock`, blocked by a `ticket_board_service` session that is `idle in
   transaction`, with everything else waiting behind the apply.
2. Require that blocker's `backend_xid` to be empty: it has written nothing.
   Record before-state: the workflow revision and an `md5` of its document, the
   role definitions, and the runtime assignments.
3. `SELECT pg_cancel_backend(<apply pid>);`. A preview rolls back by design and a
   real apply that has not committed changes nothing, so cancelling it discards
   exactly that. Confirm `SELECT txid_status(<its backend_xid>)` is `aborted`.
   Terminate it only if it still holds `AccessExclusiveLock` ten seconds later.
4. Waiters should drop to 0 and `/api/board` should answer. The idle reader
   usually ends by itself here (its request was waiting on the apply). If it is
   still the same backend (same `backend_start`), still idle in a transaction
   and still without a `backend_xid`, `SELECT pg_terminate_backend(<its pid>);`.
5. Compare after-state with before, plus a ticket and comment count and `md5`.

This is the procedure that recovered MEFP on 2026-10-07: the apply's transaction
was aborted, the reader ended on its own, and every record matched before and
after.
