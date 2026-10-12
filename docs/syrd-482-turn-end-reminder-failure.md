# SYRD-482: a failed turn-end reminder query lost the turn end

## What was seen

SYRD-480's extraction of the listener's reminder generators found this. It
was existing behaviour, not caused by the move.

`IdleNudges.process_idle_turn_end_nudges` runs once per listener pass:
1. It takes the roles whose turn ended since the last pass.
2. It asks the board for turn-end reminders (`notify_idle_turn_end_nudges`).
3. It runs the unresolved-turn guard (`_process_unresolved_turn_end`).
4. It runs the permission-prompt check (`_process_permission_prompt_waits`).

Its own comment says steps 3 and 4 run "whether or not a reminder was due, and
whether or not one was enqueued" (SYRD-194). But step 2 returned 0 early in
three cases, before steps 3 and 4:
- the query raised;
- it gave no row;
- it gave a value that is not an integer.

## What that cost, measured on main e6fe539

Step 1 had already marked the turn-end boundary as seen. So the next pass
handed the guard an empty map: the boundary was lost for good, not delayed.
For each of the three answers, `tests/turn_end_reminder_failure_test.py` found
the same on main:
- **The owner was never prompted.** The guard's first half prompts the owner
  about a turn that ended unresolved, and it acts only on roles in the map it
  is given. Neither that pass nor the next prompted the owner. The escalation
  half runs on every pass, but only for a repair prompt sent in the current
  assignment. So it could still fire on an earlier prompt's elapsed grace,
  but this turn end armed nothing of its own.
- **An earlier turn's continuation lease stayed open.** Leases are retired
  when a later turn's boundary reaches the guard, and this one never did.
- **A pane stopped on a permission prompt was reported a pass late.** That
  check needs no turn end, so the next pass caught it.

## The change

In `scripts/ticket_board/idle_nudges.py`, a failed reminder query, no row, and
a value that is not an integer each now count as no reminders enqueued (0).
The pass then goes on to the guard and the permission check, with this pass's
boundaries. The warning for a failed query is logged as before.

What is unchanged:
- **The return value:** 0 in all three cases, as before.
- **Idempotency:** the boundary is marked seen exactly when it was before, and
  a failed reminder is not retried. Each board function keeps its own dedupe:
  one repair prompt per episode, one permission report per blocked-since.
- **Gate timing:** nothing reads the panes differently or at another time.
- **Order:** the reminder query still runs first, on the same inputs.
- **No new method:** `idle_nudges_boundary_test` pins the class's method list,
  so the change stays inside the existing one.

## Tests

`tests/turn_end_reminder_failure_test.py`, 13 checks.

**The board:**
- built the production way: companion roles, `schema.sql`, the real migrate
  runner, `rbac.sql`, and a declared workflow;
- the real notify listener, driven one loop iteration at a time;
- the real `PaneActivityGate` over real hook-state files written by the real
  `PaneHookStateStore`. Only the gate's tmux and `/proc` probes are under the
  scenario's control.

**Only the reminder query is the scenario's.** A stand-in connection passes
every other statement to the board, and answers the reminder query one of four
ways:
- **ok:** the real query;
- **raise:** a real server error, from `SELECT 1/0`;
- **none:** no row;
- **text:** a real text value, `SELECT 'not a count'`. The listener's
  connection hands text back as bytes.

**For each answer, the same sequence:**
1. The owner holds an open continuation lease for an earlier turn.
2. A turn starts and ends without resolving the ticket.
3. Audit's pane stops on a permission prompt and waits past its grace.
4. One pass runs with that answer, then one ordinary pass.

**Main e6fe539, from a git archive in a child process.** For raise, none and
text alike:
- the boundary was marked seen;
- the owner was never prompted, on that pass or the next;
- the lease stayed open;
- the permission prompt waited for the next pass.

**This tree:**
- **ok:** prompts the owner, retires the lease and reports the permission
  prompt, all on that pass. This is the fixture's own precondition.
- **raise, none and text:** the query was asked once and the pass returns 0.
  On that same pass, the owner is prompted, the lease is retired and the
  permission prompt is reported.
- **Nothing repeats:** the next pass prompts nobody again, and the owner gets
  exactly one prompt.

## Evidence

- **Mutation:** seven mutants, run through `tests/bounded_run.py mutate`, all
  killed. Each kill was read on its failing assertion:
  - **"A failed query returns early":** caught by raise.
  - **"No row returns early":** caught by raise, whose failure also reaches
    that line.
  - **"A non-integer returns early":** caught by raise, for the same reason.
  - **"Only an empty answer returns early":** caught by none alone.
  - **"Only a text answer returns early":** caught by text alone. A first
    version of this mutant tested for `str`, never fired, and survived. It was
    the mutant that was wrong: the connection returns bytes. Rewritten to
    test for `str` or `bytes`, it is killed.
  - **"The guard gets no boundary unless a reminder was enqueued":** caught by
    the ok control, where no reminder was due.
  - **"The permission check runs only after a reminder row":** caught by raise.
- **The sweep:** 97 related suites were run serially on this tree and on main
  e38cedf, with provider stubs that refuse to start. Their full output was
  compared, with temporary paths, times, ports and hashes normalised.
  - **The same reds:** 13 suites are red on both trees, with identical output.
    `ticket_board_notify_listener_test` is one of them, and it stops at its
    first failure. So each of its tests was also run on its own, under its own
    live-pane guard, on both trees. The results match exactly: 93 pass and 21
    fail, with the same messages, and the guard and live-snapshot checks are
    clean.
  - **One neighbouring suite changed:** `idle_nudges_boundary_test`. It
    records SYRD-480's baseline as `GOLDEN`, including this early return, which
    SYRD-480 reported and left in place.
    - **The rerun:** the suite's own case runner was rerun. 3 of its 16
      entries changed: the board answers nothing, text, or by raising.
    - **What changed:** each was compared with the old entry structurally.
      With the two checks' own statements, log lines and method counts left
      out, each new entry is identical to the old one. Those statements are the
      lease, the guard and the permission prompt.
    - **The rule check:** the one rule check that pinned the early return now
      pins the new order.
    - **Result:** the suite passes, 86 checks.
  - **Main moved during the work.** It went from e38cedf to e6fe539 (SYRD-552
    and SYRD-458), and this commit was moved onto it. Those commits touch the
    board service, its notifications, provisioning and role git identity. None
    of the swept suites is among their files, and neither the listener nor
    `idle_nudges.py` imports them. On e6fe539, this test, its child run of main,
    `idle_nudges_boundary_test` and all seven mutants were run again, with the
    same results.
