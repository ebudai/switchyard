# SYRD-785: one Ctrl-C aborts `switchyard new`, and says what it left

Aborting test13, the User said: "I had to press ctrl-c a LOT". The run was in its first-run steps: a sign-in, then one
folder-trust step per role. Each step runs the provider's CLI on a pty with the User's terminal held raw, so Ctrl-C
arrived as a byte and was passed on to the CLI. Claude Code takes that as "press again to exit", so a press ended at
most that one step, and the phase went on to the next. A six-role project took a dozen presses or more.

## Now

- **One press ends the run.** In a provider's step, the watched session (`PtyForegroundSession.relay_from`) finds
  Ctrl-C in what the person typed. It passes on what came before, not the Ctrl-C itself, and raises
  `setup_abort.SetupInterrupted`. Ctrl-C is recognised in each encoding a terminal sends it in
  (`setup_abort.InterruptScanner`; Audit's kick-back):
  - the byte `0x03`;
  - xterm's modifyOtherKeys `ESC[27;<m>;99~`;
  - the kitty keyboard protocol's `ESC[99;<m>u` or `ESC[99;<m>:1u`.

  Claude switches the last two protocols on in the person's terminal (`ESC[>1u`, `ESC[>4;2m`). tmux with
  `extended-keys on`, or Konsole with its kitty keyboard enabled, then sends Ctrl-C encoded. `<m>` must mean Ctrl
  alone, with the Caps and Num lock bits ignored. Ctrl+Shift+C (Konsole's copy), a key's release or repeat, and
  every other key are passed on whole. A sequence a read cut short is held until the next read completes it, and
  held bytes, such as a lone Escape, go on once nothing more comes. That is a `KeyboardInterrupt`, so no step's `except Exception` catches it, and no
  later step starts. Outside a raw terminal (the questions, provisioning), Ctrl-C is SIGINT and already a
  `KeyboardInterrupt`.
- **The provider's whole process group is stopped,** helpers included. `setup_abort.stop_process_group` sends
  SIGINT, then SIGTERM, then SIGKILL, each a short bound apart, as `switchyard-bounded` does. No extra press is
  needed.
- **`switchyard new` says where it stopped** and exits 130. `scripts/switchyard` catches the interrupt and asks
  `setup_abort.interrupted`. That reads the slug and stages from `switchyard_new_command`'s own frame on the
  interrupt's traceback (`new_progress`); the command keeps them as locals and is not changed. It prints one message:
  - during the questions: nothing was created;
  - before "project accounts and files": none of the project's accounts, files or board were created, with the
    teardown dry run to check if unsure;
  - from then on: the project is partly created and was not started. `sudo switchyard teardown <slug> --dry-run`
    lists what is there, and `sudo switchyard teardown <slug> --confirm <slug> --destroy-registered-tenant
    --remove-owner-home --remove-owner-user` removes it.
- **Every other command** that runs first-run steps (`switchyard <project>`'s launch, `add-role`'s role preparation,
  a login) uses the same watched session, so one press ends it too. `scripts/switchyard` ends any command on a
  `KeyboardInterrupt` with 130 and one line, not a traceback.

## Tests

`tests/setup_ctrl_c_test.py`:

- **The real first-run phase on a pty:** `run_first_run_auth_phase` runs in a child on a real pty, with a sign-in
  and three folder-trust steps. Each step's provider is a stand-in that, like Claude, ignores SIGINT and answers a
  Ctrl-C byte with "press Ctrl-C again to exit". It starts a grandchild that ignores SIGHUP, SIGTERM and SIGINT.
  The stand-in switches on Claude's two keyboard protocols, and the run is repeated with Ctrl-C typed as `0x03`, as
  `ESC[27;5;99~` and as `ESC[99;5u`. After each single Ctrl-C:
  - the phase ends with `SetupInterrupted`;
  - no later step starts;
  - the stand-in's whole process group, grandchild included, is gone.
- **Each encoding:** every form counts, including with lock bits and the press event. A release, Ctrl+Shift+C,
  Alt+C, another key and an arrow do not count, and go on whole. A split sequence is held and recognised whole, and
  a lone Escape is released.
- **Stopping a group:** `stop_process_group` ends a group that ignores SIGINT and SIGTERM, within its bound.
- **The real `switchyard_new_command`, aborted at each point:** the interrupt passes out of it uncaught, and the
  entry point's handler gives 130 and the message for that point, naming the slug and both teardown commands. Any
  other command gets 130 and one line.
