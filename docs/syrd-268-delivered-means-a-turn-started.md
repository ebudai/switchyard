# SYRD-268 — "delivered" means the recipient took the notice

## What happened

MEFP-1 entered Director Final Sign-Off (`director_review`/`director`). The
board recorded the Director's notice as delivered at
2026-09-24T21:51:49-04:00. The User reports it never arrived, and the Director
acted only once told. The MEFP-1 record was preserved: nothing was written to
mefp and no notice was retried.

## Why "delivered" could be recorded for a notice nobody received

A notice became `send` → `listener_ack` → acknowledged whenever
`directorctl send` returned 0. Nothing else was consulted.

`directorctl`'s own check (`verify_submission` / `submission_appears_complete`)
decides from what the pane *looks like*. It returns success if the capture
changed at all between the moment before Enter and a moment after, or if the
pane shows `Working` or `esc to interrupt`. A pane already busy on another
turn satisfies both whatever happens to the notice: its output moves, and its
status line says "Working".

Demonstrated on a real, isolated tmux server. The pane behaves like an agent
mid-turn: it streams `• Working (Ns • esc to interrupt)` and swallows
everything typed into it, so nothing is submitted.

```
directorctl exit: 0
  directorctl: delivered to demo-director:0.0 (38 chars)
pane afterwards (last line):
  • Working (7s • esc to interrupt)
```

So the answer to the ticket's question — can a pane busy on another turn be
falsely marked delivered? — is yes, at the `directorctl` level. The listener's
activity gate is meant to keep notices away from busy panes, but if the gate
reads the pane as idle while it is mid-turn, nothing downstream can catch it.

**Not verified from here:** whether that is what happened to MEFP-1 at
21:51:49. The Director can read it from that notice's `send` trace:
- `anti_clobber.busy` / `reason`, the gate's verdict;
- `composer_before` / `composer_after`;
- `directorctl_diagnostic` (`composer_before` / `composer_after`,
  `forced_delivery`, `typing_wait_attempts`).

## The change

**A turn start is the witness.** The recipient's runtime records turns through
its own hooks: `UserPromptSubmit` (Claude, Codex), `PreInvocation` (Gemini/agy)
and `pre_llm_call` (Hermes) when a prompt is taken. The gate only sends to an
idle pane, so a turn *starting* after the send is the notice being taken.

**A turn ending is not.** An end after the send may belong to a turn that was
already running when the notice was typed into it, which is the busy-pane case
above. My first version accepted `Stop` too. The busy-pane demonstration
showed why it must not: the other turn's `Stop` would have "witnessed" a
notice sitting unsubmitted in the composer.

**The start is carried.** The hook state file holds only the latest state, and
a turn's end overwrites its start. Both writers now record `turn_started_at`
on a turn start and carry it through every later write:
- the real `ticket-board-pane-idle-hook`;
- `PaneHookStateStore.write`.

A test runs the real hook script and the store through the same event
sequence and requires identical results.

**Legacy state files.** A file written by a hook that predates this change has
no `turn_started_at`. For those, only a turn start that is still the latest
write can witness.

**The listener waits, bounded.** After `directorctl` returns 0, it polls the
witness for up to 15 s (`submission_confirm_seconds`):
- **Witnessed:** `send` + `listener_ack` as before, with
  `detail.submission.witnessed = true`.
- **Not witnessed:** a `send_unconfirmed` trace, never `send`. The notice is
  taken off the queue but **not** retyped automatically, because the text may
  be sitting in the composer and typing it again would put it there twice.
- **Cannot tell** (no hook state to ask): behaviour as before, and the trace
  says `witnessed: null` rather than claiming either answer.

**The board reports it.** `active_work_delivery` gains `unconfirmed`,
bounded to the current visit like the other states. The card says "Sent to
Director, not confirmed received". `active_work_notified_at` is unchanged: it
counts only `send`, so an unconfirmed notice is not a notification time.

Every database reader of `send` traces is better off not counting an
unconfirmed one:
- `ticket_state_already_announced` decides "entered" vs "is active again";
- the idle reminder uses the last send only as a timing base.

## Not done here

`directorctl`'s own heuristic is unchanged, and its "delivered" line still
means only that it did not fail. The board no longer trusts it. Making
`directorctl` itself honest would need a runtime-specific reading of each
CLI's composer and deserves its own ticket.

## Evidence

- **`tests/notification_submission_witness_test.py`: 38 checks.** Everything
  runs through the **real** `PaneActivityGate` and `PaneHookStateStore`, with a
  sender that "succeeds" exactly as `directorctl` does. It covers:
  - a notice nobody took: `send_unconfirmed`, acknowledged, not retried;
  - a notice the Director took: `send` + `listener_ack`, witnessed;
  - a turn starting after `directorctl` returns;
  - a quick turn that already ended;
  - every runtime's turn start;
  - a turn end alone is not a witness;
  - **the busy-pane case with the earlier turn's start on record.** The gate
    misreads a pane mid-turn as idle, and that turn ends after the send. This
    is not delivered. A complete earlier turn followed by the notice's own
    turn still is.
  - legacy state files;
  - the real hook script and the store writing identical `turn_started_at`;
  - "cannot tell" reported as `null`;
  - the wait is bounded.
- **`tests/active_work_delivery_state_test.py`: 27 checks.** Unconfirmed on a
  real cluster through `list_tickets` / `get_ticket`, bounded to the visit.
- **`tests/active_work_delivery_frontend_test.py`: 14 checks.** The served
  card's text for `unconfirmed`, run under node.
- **`tests/ephemeral_role_sessions_test.py`.** Its fake sender now behaves like
  a pane that takes what it is sent: it writes the runtime's turn start and
  end. Before this change the suite's sends succeeded without any recipient,
  which is now exactly what is refused. All 22 checks pass, unchanged.
- **Mutation: 12/12 killed.** Covered: the witness never asked; unconfirmed
  still delivered; `Stop` counted as a start; the start not carried; the
  witness reading the latest write; the hook not carrying the start; no wait;
  an unconfirmed notice retyped; the legacy fallback accepting any event; the
  board ignoring or not finding unconfirmed; the card silent.
  The first run left two survivors:
  - "reads the latest write instead of the turn start" survived because no
    case had an earlier turn start on record. The busy-pane case above was
    added for it, and it is now killed.
  - "retyped" had an ambiguous anchor. With a unique anchor it is killed.
- **Sweep.** 81 suites were run serially under `env -i`, on this branch and on
  a clean `origin/main` (`c2f237c`). They touch the listener, the hook script,
  the hook state store, the tickets query or the frontend. Pass/fail is
  identical, with two exceptions:
  - **`ticket_board_notify_listener_test`**, which is red on both trees.
    Compared case by case, one case differed:
    `test_long_idle_live_pane_still_delivers`. It uses the real hook gate with
    a sender that makes the pane do nothing, which is now correctly
    unconfirmed. Its sender now records the turn start and end a live pane's
    hooks would write, the same fixture change as in
    `ephemeral_role_sessions_test`. After that, all 114 cases match main,
    including the failure lines (shifted by the 8 inserted lines).
  - **`ticket_board_director_reassign_test`** failed once on this branch with
    `argument --write-token: expected one argument`, then passed 4/4 on both
    trees. The failure predates this branch: the server's token is
    `secrets.token_urlsafe(32)`, which starts with `-` one time in 64, and
    argparse then rejects `--write-token <token>`. Neither file is touched here.

  The other 17 suites red on both trees give identical per-case results, or
  for the suites without `test_` functions, identical final lines. The
  frontend suites stop at the missing-Playwright banner in both.
