# SYRD-633: a blocked pane is one episode, and the Director hears of it once

## What happened (syrd, 2026-10-20)

Measured read-only from the live board's `notification_trace`, App's pane-state
file and App's Claude transcript:

- At 11:23:51Z App's response was stopped by its provider's safeguards. At
  11:24:26Z App's pane hook wrote `blocked` from
  `claude.Notification.permission_prompt`, and nothing replaced it until the
  Director's recovery prompt at 21:28Z. The provider session stayed the same
  throughout (resumed at 10:23:01Z).
- SYRD-234's generator did raise one escalation: notification 7022 at
  07:26:29 EDT, deduped `permission-prompt:app:2026-10-20T11:24:26.347921+00:00`.
  It went on **SYRD-624**, App's lowest id, which was blocked on another
  ticket. Escalation eligibility refuses a ticket with unresolved blockers, so
  the listener dropped it as `stale_notification` eight seconds later. The
  Director never saw it.
- The generator loops over the role's tickets, but its dedupe key is per role
  and blocked instant. So only the lowest id could ever be told, and once its
  key was in the trace, every later pass was silent. SYRD-629's activation,
  notification 7024, was requeued 111 times from 08:09 to 17:29.
- The gate's trace for 7024 kept only `hook_blocked`: no source, age or
  session. A blocked hook returns before the child-work probe, so the two
  finished SYRD-627 VMs still running under the pane were never named.

Main also had no escalation at all for a blocked source other than the two it
knows.

## The change

### One episode per blocked instant (pgu1001)

`ticket_board.blocked_pane_episodes` keeps one row per role and blocked
instant, with at most one open per role.

Each pass, before the reminder generators, `blocked_pane.run` reports every
pane the listener could read to `ticket_board.note_blocked_panes`:

- **A role whose hook says `blocked`** opens its episode, or joins the one
  already open for that instant.
- **Once the pane has said blocked for the bound**, the Director is told once.
  The bound is the listener's permission-prompt grace,
  `TICKET_BOARD_PERMISSION_PROMPT_GRACE_SECONDS`, which defaults to 120 s.
  The alert is said:
  - on the ticket whose held notice waits longest;
  - otherwise on the role's active work.

  It is a `ticket_update` whose payload kind is `blocked_pane`.
  `notification_eligibility` treats that kind as current for the Director
  alone, like `background_hold`, so another ticket's blockers cannot drop it
  the way they dropped 7022.
- **A role read and not blocked**, or blocked from a later instant, closes its
  episode, as `cleared` or `replaced`. An alert not yet sent is withdrawn
  (`discard`, `blocked_pane_cleared`), and the Director never hears of a hold
  that is already over.
- **A pane that could not be read** is left as it was. The Director's own pane
  is never reported: an alert about it would wait behind it.

The episode lives in the database, so a restarted listener joins it instead of
starting the clock again. A record whose instant is already past the bound is
told on the first pass after a restart.

On a board with these episodes, SYRD-234's `notify_permission_prompt_waits` is
no longer called, so nobody is told about the same pane twice. A board without
pgu1001 keeps the old generator.

### What the episode says

`blocked_pane.read` classifies the hook's own record:

- **`permission_prompt`**: the record is from one of the two sources known to
  raise a prompt (`claude.Notification.permission_prompt`,
  `codex.PermissionRequest`), and its writer is the live provider.
- **`restored`**: the record says nothing about the live provider. Either it
  was written before the process now in the pane started, or it carries a
  session that another process started, not the role's registered provider.
- **`unknown_source`**: any other source.

The alert names:

- the role, the exact source, the instant and its age, and the classification
  with its reason;
- the held notices and their notification ids;
- what the screen shows: the CLI's own question, or an empty ready prompt with
  no question. The second means the record may be stale, as App's was;
- each detached child under the pane, from one `/proc` sample, as SYRD-583
  describes them:
  - pid, process group and age;
  - whether a finished turn left it, dated against the blocked turn's start;
  - the ticket it was started for and where that ticket is now;
  - the one stop that ends only it;
- the recovery. Look at the pane first. If it shows a question, answer it
  there. If it shows the ready prompt, give the role a prompt with
  `directorctl send <target> <message>` or restart it. The held notices then go
  out by themselves.

### The trace

The gate's verdict is unchanged: `hook_blocked`, busy. Each `gate_defer` behind
a blocked hook now carries `anti_clobber.blocked`:
- source, since, classification and why;
- the session the record carries;
- when the provider now in the pane started;
- the children under the pane.

The children are also listed in `anti_clobber.holders`.

### Not changed

- Nothing is typed into a blocked pane, and nothing is released past one. A
  notice ends in Enter, which answers a question. The children under a blocked
  pane are named, but SYRD-583's lease releases past them only once the hook
  says the turn is over.
- Nothing is stopped.
- Nothing reaches the worker while its pane is blocked, so the worker and the
  Director are never prompted at once. Whatever is queued for the worker waits
  behind the same hold.
- When the hook next says idle, the held notices go out through the ordinary
  gate, past a finished ticket's child by SYRD-583's lease, each exactly once.
- Composer, provider-question, background-work and child holds, and their
  SYRD-557, SYRD-566, SYRD-570 and SYRD-583 alerts.

## Size

- `notify_listener.py` stays at 1,250 lines. The pass is one line, in place of
  a two-line comment that became one.
- The new code is in `blocked_pane.py`.
- The `schema.sql` addition is the migration's own copy.
