# SYRD-708: authorized background work raises no background-work alert

## What happened

On syrd, 2026-10-30, App ran one declared, bounded 350-suite comparison for
SYRD-706 as process group 2036002, under lease 394.

1. **08:53.** The lease's own expiry alert went out, and the Director extended
   the lease through 10:23. App had fresh progress on record.
2. **Snooze.** The Director also snoozed SYRD-706's reminders through 10:30,
   with an instruction not to poll the pane.
3. **09:15.** The board told the Director anyway that App's pane had held one
   SYRD-706 notice for 31 minutes "behind background work under its pane". It
   named process group 2036002, and sixteen PostgreSQL processes, 0 to 3 s old,
   that the comparison had just started under it.

The alert was SYRD-557's held-notice alert. The listener notes each requeue
that background work causes, and the board's `note_background_hold` tells the
Director once an episode passes 30 minutes. Nothing in that path read the
lease or the snooze, so the Director was asked to intervene in an operation
they had just reviewed and authorized.

## The change

The code is in `scripts/ticket_board/background_authorization.py`.
`covered(conn, role, holders, ticket=...)` says why every holder is authorized
right now, or returns "" when any one is not.

### What authorizes a process

- **A Director's extension.** The process is under an open execution lease:
  its own, or the nearest one above it in the process tree. That covers the
  PostgreSQL children, which make sessions of their own under the comparison.
  The lease is matched by pid **and start tick**, so a replaced process or a
  reused number is never covered. The lease must:
  - have `deadline_source = 'extended'`, with that deadline not yet passed;
  - be in state `running`;
  - have no interrupt asked of it;
  - be for a ticket that is still the role's work and not terminal.
- **A reminder snooze.** The ticket the process works for has an active
  reminder snooze, as the board itself decides with
  `ticket_reminders_snoozed`. That function ends the snooze when the ticket
  moves or the batch is cleared. The ticket is the lease's, or, with no lease,
  the git worktree the process runs in.

A holder must still be the process the gate sampled: the same pid, start, group,
and not a zombie. One whose identity is lost authorizes nothing.

### Where it applies

- **The held-notice alert (SYRD-557).** `background_hold.note` asks `covered`
  with the held notice's ticket. The listener's ledger now passes that ticket
  and the role.
  - While every holder is authorized for that ticket, the hold is still noted,
    with an `AUTHORIZED_ALERT_AFTER` of 1000 years in place of the 30 minutes.
    The episode, its start and its notices stay exactly as before.
  - The next note after the authorization ends uses 30 minutes again, so a hold
    that is already over 30 minutes is told at once, for its whole length.
  - A notice for **another** ticket held behind the same work is told: the
    extension and the snooze are for the work's own ticket.
  - So is a hold with any holder that is not authorized, such as a new command
    beside the extended one.
- **The delivery-past alert (SYRD-583, SYRD-704).** An episode whose members
  are all authorized is not told, whatever notice went past it. It is not
  recorded either, so it is told once, as before, the first time a delivery goes
  past it after the authorization ends.
- **Not changed:**
  - **SYRD-636's hand-over report.** It names only work whose ticket is no
    longer the role's, or unknown, and nothing authorizes that.
  - **SYRD-616's own lease alerts.** An extension already moves the expiry, and
    a stale lease is contradictory state, which should be told.
  - **SYRD-633's blocked-pane alert.** It is about a blocked hook, not
    background work.

### What resumes alerting

Each of these immediately makes the next note or delivery alert as before:
- expiry of the extension;
- the lease marked `stale`, ended, or asked to interrupt;
- the process replaced, or its number reused;
- the ticket moving to another role or finishing;
- the snooze being cleared, falling due, or invalidated by the ticket changing;
- a failure to read any of this, which reads as "not authorized".

**Unchanged:** delivery, holds and their accounting. A held notice stays
queued, its episode keeps every notice and its start, and the closing line
SYRD-557 sends when a reported hold ends is unchanged.

### An alert already queued

An alert can be queued before the Director authorizes the work, and then wait
for the Director's pane. It must not arrive during the window, but it must
still go out afterwards, exactly once (Audit, round 1). So the listener asks
about it at claim and again just before the send: `hold_alert`, reached through
`_defer_for_background_work`.

- **A queued held-notice alert** is requeued, 60 s at a time, while every
  holder the gate last saw under that role's pane is authorized for every
  ticket the episode may hold. Those tickets are read from the trace the
  listener may read: the alert's own ticket and the notices it names, plus every
  notice of that role's the gate has deferred since the episode began, so a
  notice held after the alert was queued counts too. If no holder is known, the
  alert goes out.
- **A queued delivery-past alert** is requeued while the process its key names
  (process group and start, re-read from /proc) is authorized.
- **What is kept.** It is requeued, never dropped. The row, the episode's
  `alerted_at` and SYRD-583's per-child record are unchanged, so the same alert
  goes out once the authorization ends. As before, it is withdrawn if the hold
  ends first (SYRD-557's trigger), or dropped if its process has gone
  (SYRD-704's check, which runs before this one).
- **What is never held.** A notice to any role but the Director, and any alert
  that cannot be checked.

## Tests

`tests/background_authorization_test.py` runs on a production-built board:
schema.sql, roles, rbac.sql, every migration. It uses the listener's database
role, the real `NotificationLedger.trace_deferral_once`, the board's own
`note_background_hold`, `observe_execution_lease` and `extend_lease`, and the
Director's `snooze_reminders` and `clear_reminder_snooze`.

The processes are a fake /proc tree shaped as SYRD-706's: the comparison's shell
with PostgreSQL's children under it. Nothing sleeps: the hold's age is moved
back in the database.

The cases:
1. **The 30-minute boundary, without authorization.** 29 minutes is not told,
   and 31 is.
2. **SYRD-706's case.** It is not told at 31 minutes under the extension. The
   episode keeps its notice, the notice stays queued, and the authorization is
   named.
3. **An unrelated notice** held behind the same work is told at once.
4. **A new command** beside the extended one is told.
5. **Expiry.** It is not told at 45 minutes inside the extension, and is told at
   once, "for 45 minutes", when the deadline passes.
6. **Replacement.** The same pid started again is told. The comparison's own
   new extended lease covers it again.
7. **Contradictory state.** Each of these covers nothing:
   - an interrupt;
   - the ticket gone to another role;
   - a stale lease;
   - a default deadline still ahead;
   - a reused number under the extended work;
   - a default deadline past.
8. **Snooze.**
   - It is not told while the work's ticket is snoozed.
   - A notice for an unsnoozed ticket is told.
   - It is told at once when the snooze is cleared.
9. **The delivery-past alert.** It tells nobody under the extension, and tells
   once when the extension has passed.
10. **Alerts queued before the authorization, through the listener's own
    `_defer_for_background_work`:**
    - **A delivery-past alert queued before the extension.** It goes out while
      unauthorized. After the extension it is held at claim and at pre-send,
      and requeued with the same row. It goes out after expiry. It is never
      held for a replacement process, or for a number reused under the
      extended work.
    - **A held-notice alert queued at 31 minutes before anything was
      authorized.** It is held once the Director extends the exact process,
      with the same alert and the episode told once, and no second alert from
      later notes. It goes out when:
      - a holder is outside the extension;
      - no holder is known;
      - an unrelated notice has been held since it was queued;
      - the extension expires.
    - **A snooze begun after the alert was queued** holds it, and clearing the
      snooze lets it out. It is still one alert with one record.
    - **Never held:** a board that cannot be read, or a notice to a role.

The tests without a board:
- an unreadable board means no authorization;
- a PostgreSQL child's ancestry, with each process's start tick, reaches the
  comparison's shell.
