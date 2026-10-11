# SYRD-584: the unresolved-turn escalation waited for the owner's silence, not the clock

## What Otto showed

Otto runs release f92a3d4d. This is OTTO-139, with main on Codex, on
2026-10-11 (EDT):

1. **12:25:** main ended a turn without resolving OTTO-139. Codex then showed
   its own "Approaching rate limits" menu.
2. **The board reported the held notice:** "main's pane has held 1 ticket
   notice (OTTO-139) for 2 minutes: its CLI is stopped at an unanswered
   menu". The Director answered the menu, and the held repair prompt ("OTTO-139
   is still yours and this turn ended without resolving it") was delivered.
3. **The prompt started a turn** on OTTO-139.
4. **About ten minutes later**, the Director was told "main ended a turn
   without resolving OTTO-139 (in_progress), and the grace period has passed
   after being prompted it is still unresolved". main's pane read "Working
   (10m 05s)", with fresh edits to the ticket's files.

## Why

`notify_unresolved_turn_end` has two halves. The first prompts the owner when
a turn ends unresolved. The second, which runs on every listener pass, tells
the Director when the prompt did not work. That second half escalated as soon
as `prompted_at + grace` had passed. `prompted_at` is the prompt's enqueue
time, and the check never asked two questions:
- **When did the prompt reach the owner?** On Otto it waited behind a menu
  first.
- **Had the owner worked since?** The board had no way to know: the function
  is given only the turns that ended on that pass, and a turn that is still
  running ends none.

## The change

Migration pgu987, with the same text in schema.sql and the grant in rbac.sql.

**The grace runs from delivery.**
- **A delivered prompt:** the grace runs from its first `send` or
  `send_unconfirmed` trace. That is when the owner had something to answer.
- **A prompt still waiting:** the grace runs from its enqueue, as before. A
  hold that outlasts the grace is still news to the Director. The menu,
  composer and background-work hold alerts also report the hold itself.
- **An undeliverable (dead-lettered) prompt:** still reported at once.

**A turn since the prompt cancels the wave.** The listener now passes a fourth
argument, `p_owner_turns`. It holds each role's own turn as its CLI hook
recorded it: `state`, `updated_at` and `turn_started_at`. If the owner's hook
shows a turn started after the prompt was enqueued:
- **while that turn runs** (state not `idle`), the Director is not told;
- **once it ends**, the grace runs again from the end (`updated_at` of the idle
  state), or from delivery if that is later.

So only a later completed unresolved turn arms another escalation, after its
own grace. The Director is still told once: the existing dedupe on "the
Director was told since this prompt" is unchanged.

Using the prompt's enqueue as the lower bound, rather than its delivery,
matters. The witness that records `send` checks for a turn start after the
keystrokes, so the woken turn's recorded start falls just before the `send`
trace.

**The evidence is the hook's alone (SYRD-583).**
- **What is read:** the facts come straight from `PaneHookStateStore`, which
  the CLI writes itself.
- **What is not:** nothing the listener observed is folded in, such as child
  processes, pane movement or the gate's verdict. So a background child left
  running does not make a turn, and a stale busy hook written before the
  prompt does not count either.
- **No hook state:** a role without it is left out. That is "cannot tell", and
  the grace then runs from delivery alone.

**Compatibility.**
- **The signature:** the new parameter defaults to NULL. A three-argument call
  from an older listener, during an upgrade, gets the delivery-time rule.
- **Old signatures:** the three-argument function is dropped, as SYRD-203 did
  for the two-argument one. Beside a defaulted fourth parameter, it would make
  every three-argument call ambiguous. The listener's grant is restated.

## Unchanged

- **The owner's prompt and its episode rule** (SYRD-538). A turn that ends
  unresolved more than a grace after the last prompt is prompted again.
- **The other alerts:** undeliverable escalation, the current-assignment rule
  (SYRD-514), the dedupe, and the hold alerts for menus, composers, background
  work and permission prompts.
- **The gate:** `PaneActivityGate` is untouched.
- **The listener:** its only change is the extra argument.

## Tests

`tests/unresolved_turn_grace_from_delivery_test.py`, 11 checks.

**The board:**
- built the production way: companion roles, `schema.sql`, the real migrate
  runner, `rbac.sql`, and a declared workflow;
- the real notify listener, driven one loop iteration at a time;
- the real `PaneActivityGate` over real hook-state files written by the real
  `PaneHookStateStore`. Only the gate's tmux and `/proc` probes are under the
  scenario's control;
- a grace of four seconds, so time passes for real.

**Main 2b7fb5c, from a git archive in a child process:**
- **Case A:** the prompt is held behind a menu, then delivered, and wakes a
  turn. The Director is told 2 seconds into that turn, and again later.
- **Case B:** after a prompt held for 3 seconds, the owner gets about 1 second
  of grace.

**This tree:**
- **A, Otto's sequence:**
  - the prompt is held behind a menu, then delivered;
  - through the woken turn, two and a half graces long, the Director is told
    nothing;
  - that turn ends unresolved, so the owner is prompted again;
  - the short turn this wakes ends unresolved too. It escalates once, a grace
    after it ended, and is not prompted a third time.
- **B, an owner who never answers:** reported once, a grace after delivery,
  however long the prompt was held. An idle notification written meanwhile is
  not a turn.
- **C, a stale background child (SYRD-583):** the pane looks busy only through
  a background child, and the hook is idle. It is reported a grace after
  delivery.
- **The older listener:** its three-argument call still reaches the guard.
- **Parity:** `schema.sql` and `rbac.sql` install the same guard, owner and
  grants as the migrations, with no three-argument overload left.

## Evidence

- **Mutation:** seven mutants, run through `tests/bounded_run.py mutate`, all
  killed. Each kill was read on its failing assertion:
  - **"The grace runs from enqueue again":** caught by case B, which escalates
    2.0 s after delivery.
  - **"A working owner is escalated":** caught when case A escalates mid-turn.
  - **"The woken turn's end is ignored":** caught when case A escalates twice,
    0.1 s early.
  - **"A turn before the prompt counts":** caught when the idle notification
    in case B moves the escalation to 6.1 s.
  - **"The listener passes no turns":** caught when case A escalates mid-turn.
  - **"No turn start is passed":** caught when case A escalates mid-turn.
  - **"schema.sql escalates a working owner":** caught by the parity check.
- **The sweep:** 96 related suites were run serially on this tree and on main
  2b7fb5c, with provider stubs that refuse to start. Their full output was
  compared with temporary paths, times, ports and hashes normalised.
  - **The same reds:** 13 suites are red on both trees, with identical output,
    apart from this ticket's migration named in two migration listings.
  - **Three neighbouring suites changed.** Each pinned something this change
    alters by necessity:
    - `ticket_board_background_work_test` and `unresolved_turn_guard_test`
      named the old three-argument signature in a parity query and a grant.
      They now name the new one.
    - `idle_nudges_boundary_test`'s recorded calls (`GOLDEN`) were rerun with
      the suite's own case runner. Its 11 changed entries were compared
      structurally with the old ones: each differs only by the new third
      argument, `'{}'`, because the stand-in gate has no hook store. One rule
      check that names those parameters was updated to match.
    - All three pass: 122, 34 and 86 checks.
