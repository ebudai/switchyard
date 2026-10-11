# SYRD-580: `present recover` starts one missing role while others are still gone

## What went wrong

On 2026-10-11, MEFP's Audit and Main were both absent. The Director ran
`switchyard present mefp recover audit` and then `... recover main`. Both
refused before launching anything:

    switchyard: no live runtime assignment for configured role(s): audit, main

`presentation_action()` resolved every configured role's live assignment
(`runtime_assignment_config`) before reaching its `recover` branch, and that
resolver requires every non-detached role to be live. So a missing role refused
its own recovery, and two missing roles could not be recovered in either order.

## What recovery does now

1. **The ordinary question first.** Recovery still resolves every role with
   `runtime_assignment_config`. A project whose roles are all live is handled
   exactly as before.
2. **Authority before anything more.** When that refuses, a caller who is
   neither the Director nor the bridge-authenticated operator recovering the
   Director, or whose `TICKET_BOARD_PROJECT` names another project, gets that
   refusal unchanged, and nothing is probed for them, as before. For the
   Director, `assignment_config_with_absent` reads the board again:
   - every live assignment is resolved and checked exactly as before, so a
     foreign target, or a runtime the saved projection does not run, is still
     refused;
   - a role with no assignment keeps its saved projection and is reported
     absent;
   - other roles that are absent are not required to be live.
3. **Validate the requested role, and only that one** (`absent_role_refusal`),
   before desktop preparation or any start:
   - a live worker in its declared pane running another runtime is named, as
     before, and never replaced;
   - its saved configuration must match the board's workflow declaration
     (declared, active, same runtime, same target), because the board registers
     nothing else. A board with no workflow document declares a role at its
     first registration, so there is nothing to compare yet. A workflow that
     cannot be read refuses;
   - a pane already running at its target but not registered is not replaced.

   Each refusal says "Nothing was changed", and nothing was.
4. **Start exactly that role** through the launcher's own start, as before.
5. **Wait for its new registration** (`await_recovered_registration`). The wait
   is bounded by `RUNTIME_REGISTRATION_TIMEOUT_SECONDS` and announced. The role
   had no assignment when recovery began, so any assignment now is the new
   worker's. It counts only when bound to a live process in that pane, with the
   same target, pid, start time and uid checks `pane_liveness` makes. A pane that
   exits ends the wait at once. If the role never registers, recovery refuses,
   and its display is not touched and nothing is recorded. The started worker is
   left running.
6. **Attach the display to the registered worker**, then prove it, exactly as
   before.

`present list`, which the `switchyard present` command also prints after every
action, still asks the ordinary question first. When that refuses only because
roles have no assignment, it reports each such role as `worker=unregistered
client=unregistered` instead of refusing the whole report. Any other refusal is
still a refusal. Without that,
`recover audit` would have started Audit and then exited 1 because Main was
still gone. An unregistered role is never probed by its conventional name.

## Not changed

- Every other presentation action still requires every role to be live.
- So do launch and `reconnect_role_slots`.
- A project without `role_state_isolation` is unchanged.
- An existing assignment still decides which session a role's display attaches
  to (SYRD-69).
- The Director-only gate and the operator path for the Director's slot
  (SYRD-239) are unchanged.

## Tests

`tests/presentation_recover_absent_roles_test.py`:

- **Real parts:** an isolated tmux server; workers started by the launcher's own
  start, whose pane command runs the real `ticket-board-register-runtime`; real
  display sessions.
- **Stand-in board:** registers by `SO_PEERCRED` and filters assignments as
  `runtime_targets` does.
- **Cases:**
  - Audit then Main;
  - Main then Audit through the real `switchyard present` command;
  - a divergent requested role;
  - an unrelated divergent role that does not block;
  - a saved configuration the workflow disowns;
  - an undeclared role, an inactive role, an unreadable workflow;
  - a board with no workflow document;
  - a registration naming a reused pid;
  - a worker that exits during the wait;
  - a running pane that never registered;
  - a start that never registers;
  - callers who could not act;
  - a live assignment the projection does not run.

In `tests/operator_display_recovery_test.py`, a recovery of the Director now
probes only the Director's declared pane. App, which is also absent, is not
read.
