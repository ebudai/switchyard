# SYRD-264 — a notice the board could not route is not a missing pane

## What happened

MEFP-1 was routed to `in_progress/ops` at 2026-09-24T23:24:45Z. Its one
transition notice was claimed, then dead-lettered 28 seconds later with
`tmux_target_missing` for `mefp-ops:0.0` (Director's read-only triage).
`active_work_notified_at` stayed empty. The Ops pane sat at its first Codex
prompt. The board's card showed the ticket highlighted as Ops's current work,
and the User read that as "Ops has been notified".

## What was measured, read-only

These facts come from `ps`, `/proc/<pid>/{cmdline,cgroup}` and GETs on mefp's
board at `:26623`. Nothing was written to MEFP and no notice was retried; the
MEFP-1 record is preserved.

- **The Ops worker (pid 294420) has been alive since 10:34** and was never
  restarted. Its prompt looked fresh only because it never received work.
- **The listener runs as the owner, `stellaris-agent`, from a user unit.** It
  has no private `/tmp` and no `TMUX_TMPDIR`, so its `tmux` reaches the
  workers' server. The pre-send `tmux has-session` probe could not have
  failed.
- **The listener and `directorctl` answer "where is ops?" separately.** The
  listener reads runtime assignments from the database each cycle, filtered by
  the declared workflow. `directorctl send` resolves the role *again*, through
  `GET /api/runtime-assignments/<role>`, which applies the same filter.
- **Ops is routable today.** The workflow is at revision 5, and ops is at
  generation 5 with the same pid. Earlier that day the declaration and the
  workers disagreed (SYRD-239/SYRD-262), and the rows were being rebound.

## Cause

`directorctl send mefp-ops:0.0` exits like this when the board has no
assignment for the role at that moment:

```
error: cannot resolve runtime assignment for ops: HTTP Error 404: Not Found
```

The listener's `delivery_failure_reason` had a fallback: target session name
in the output **and** "not found" in the output means `tmux_target_missing`.
The "output" included the `CalledProcessError`'s own text:
`Command '[..., 'send', 'mefp-ops:0.0', ...]' returned non-zero exit status 1`.
So the session name was always present, and **any** failure saying "not found"
became a terminal missing-pane dead letter. A notice the board would have been
able to route seconds later was lost for good.

The claim-time target was `mefp-ops:0.0`, so the listener's own read saw ops.
The send came 28 seconds later, after the activity gate. A rebind landing in
between would produce exactly this. **Not verified from here:** the MEFP-1
`send_failed` trace, and the times of workflow revision 5 and ops generation 5,
can confirm it; the Director has that access.

Proven on the real code, using the exact stderr that the real `directorctl`
produces against a board answering 404:

```
main (d05236d):  classified tmux_target_missing           -> dead_lettered [(41, 'tmux_target_missing')]
this branch:     classified runtime_assignment_unresolved -> requeued      [(41, 'runtime_assignment_unresolved')]
```

## The change

- **The command line is not evidence.** `delivery_error_output` is what the
  failed process printed (stderr/stdout), never the exception's own
  `Command '[...]'` text. Real tmux wording ("can't find pane/session…") and
  directorctl's own "`<session>` not found" still mean a missing pane. A
  "not found" about anything else (`sudo: tmux: command not found`) no longer
  does.
- **A routing fault is its own reason: `runtime_assignment_unresolved`.** It is
  requeued with the listener's capped backoff, not dead-lettered, so the notice
  is delivered once the role routes again.
- **Evidence is kept.** The `send_failed` trace and the dead-letter detail
  carry `error_output`, the text the reason was derived from. mefp's dead
  letter kept only the classification, which is why the cause could not be
  read back from the record.
- **The board says whether the owner was told.** The ticket API adds
  `active_work_delivery`, computed from the durable records:
  - `delivered`: a send is traced for this owner at this stage;
  - `failed`: the notice was dead-lettered, with its reason and time;
  - `pending`: the notice is queued, with attempts, last error and next try;
  - `none`: the ticket has an owner and no notice is recorded;
  - `""`: there is no current owner.

  Only a notice for the ticket's **current** stage counts.
- **The card shows it.** A highlighted card whose notice has not arrived
  carries one line: "Not delivered to Ops: tmux_target_missing", "Not yet
  delivered to Ops (attempt 3)" or "No notice recorded for Ops". Details are
  on hover. A delivered notice, or a ticket that is not current work, adds
  nothing.

## Not done here: an explicit retry

The ticket's alternative expectation includes "permits safe retry". A
Director operation to requeue a dead-lettered notice would be a new write
authority, with its own SQL function, grant, API route, CLI command and
review. I have not built that without asking. With this change:

- a routing fault retries by itself;
- a genuinely missing pane is still dead-lettered, and is now visible on the
  card;
- MEFP-1's existing dead letter needs a re-send once this release is on
  mefp's board.

Which Director path should do that re-send, or whether an explicit
redeliver operation should be filed, is a decision for the Director.

## Evidence

- **`tests/notification_routing_unresolved_test.py`: 20 checks.** It runs the
  **real** `directorctl send` against a board answering 404, with no live tmux
  reachable, and feeds that failure through the real listener as project
  mefp. The notice is requeued as `runtime_assignment_unresolved`, the trace
  keeps directorctl's text, and nothing is dead-lettered. A really missing pane
  is still dead-lettered, now with its text. An unrelated "not found" is
  retried, and directorctl's own missing-session wording is still terminal.
- **`tests/active_work_delivery_state_test.py`: 16 checks,** on a real
  cluster through the real `list_tickets`/`get_ticket`. It covers all four
  states, the MEFP-1 shape as `failed`, a notice for another stage not counted,
  and an ownerless ticket reporting nothing.
- **`tests/active_work_delivery_frontend_test.py`: 11 checks.** It runs the
  **served** page's `activeWorkDeliveryLine` under node with a minimal DOM.
  Playwright's browser build is not available in this environment, so the
  `*_browser_test` suites cannot run here on either tree.
- **Mutation: 11/11 killed** across classifier, requeue, evidence, delivery
  state and card.
- **Sweep:** 72 suites touching the listener, the tickets query or the
  frontend give identical pass/fail against a clean `origin/main`
  (`d05236d`). The 16 red on both give identical results: 203 cases compared
  one at a time, and identical first failures (one differs only in a temp
  path).
