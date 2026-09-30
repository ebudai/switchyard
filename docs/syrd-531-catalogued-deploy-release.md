# SYRD-531: the catalogued `deploy-release` deploys the prepared release

## What happened

MEFP's Director followed SYRD-529 on the installed 4e91233:
- the pinned `preview-upgrade` (attempt 0068) exited 0;
- `upgrade-tenant-release` (0069) exited 0, with the right source, artifacts
  and role tooling refreshed.

`release-status` still showed the board and live build at 49abeb4, and the
release phase `ready`. The Director skill says the tenant's "upgrade and board
deploy are yours … `deploy-release` deploys an approved release", but that
action's catalogue entry ran the same preparation.

## Measured cause, on main 4ebb846

- **An upgrade prepares, and never deploys the board.** Its release phase is
  owned by the operator (`UPGRADE_PHASES`). `report_tenant_release_upgrade`
  prints the sequence, each step wrapped in the rollout recorder:
  1. stop the listener;
  2. install root's copy of the units;
  3. `deploy-restart`, as the tenant;
  4. start the listener;
  5. `release-status --close`.

  The upgrade then exits 0, saying the artifacts are prepared.
- **`deploy-release` mapped to that same preparation.** Its operation was
  `[LAUNCHER, "upgrade", <p>, "--deploy-ref", <c>]`, identical to
  `upgrade-tenant-release`'s.
- **So nothing a Director could run deployed a board.** The pieces all
  existed: `stop_owner_listener`, `deploy_release_in_transaction` (used only
  by the identities cutover), `start_owner_listener`, and `close_release_phase`.

## The change

- **`switchyard deploy-release <project> --commit <sha>`**
  (`scripts/tenant_release_deploy.py`), root only. The catalogued action now
  maps to it (`privileged_operations._deploy_release`).
- **It refuses first, deploying, stopping and recording nothing,** unless:
  - root's pin for the tenant is exactly this commit (preparation is not
    activation: `preview-upgrade` then `upgrade-tenant-release` first);
  - the pin's source is root's installed release of it;
  - the release resolves to that commit;
  - the units would come from root's own provision copy.
- **Then the printed sequence, in its order:**
  1. stop the listener (migrations run inside the deploy);
  2. install root's units;
  3. `deploy_release_in_transaction`, which runs the tenant's own
     `deploy-restart` with its health gates, live-build check and rollback;
  4. start the listener;
  5. `close_release_phase`, which records `done` only after re-proving from the
     running board that it serves the release its link names, and that this is
     the pin.
- **On any failure the phase is recorded `blocked`,** with the reason and
  where the board is:
  - a failed deploy that left the board brings the listener back;
  - a failed deploy that moved the board leaves the listener stopped, and
    says so;
  - a listener that does not come back is not closed over.
- **A board already serving the release** is only closed; nothing is
  restarted.
- **Panes are not restarted.**
- **Wording.** The close's own "Nothing was deployed" and "Close it with …"
  lines are reworded for this context.
- **The upgrade's closing report** names `switchyard privileged-action <p>
  deploy-release commit=<sha>` once a tenant is prepared for an exact release.
  The operator's sequence is unchanged.

**Unchanged:**
- `preview-upgrade`, `upgrade-tenant-release` and `select-shared-release`;
- the helper's registered-control-pane check;
- root's release validation and the tenant cache/publication separation;
- the pin checks (SYRD-529) and the tenant-specific rollback (SYRD-528);
- the workflow;
- `deploy-restart` itself, where Main's SYRD-530 changes apply unchanged.

## Guards that name the command set

These guards are updated only by what was measured:
- `switchyard_dispatch_boundary_test`: two launcher reads, one branch in
  order, the shape count, and a dispatch case whose golden trace was checked
  by running it.
- `switchyard_commands_boundary_test`: the six golden answers, rewritten from
  measurement. A word diff shows only `deploy-release` added.
- `privileged_front_door_test`: its allowed literal verbs gain
  `deploy-release`. The suite matches main case for case, with one
  pre-existing failure.
- `upgrade_records_boundary_test`: the report's one new read, with the host
  pin read stood in and a case for the Director's line. The line is inlined,
  because the module's definitions are pinned.
- `README.md`: the help-drift check required `deploy-release` in the listed
  commands.

## Evidence

- **`tests/catalogued_deploy_release_test.py`.** It drives the helper's real
  `parse_request` and `run_privileged_action`. Their runner hands the argv the
  helper built to the tree's `switchyard_main`, which dispatches to the real
  command, as root in-process.
  - **Stand-ins.** systemd and the deploy are a recording runner, whose deploy
    really moves the board's `current`. The live build is a stand-in, reusing
    `release_phase_journal_test`'s tenant. Any other subprocess raises.
  - **Reproduction.** Main's catalogue, run in a child from a git worktree,
    maps `deploy-release` to exactly `upgrade-tenant-release`'s argv.
  - **Cases.**
    - A prepared tenant is deployed in order and closed against the live
      build, and the helper's attempt record says succeeded.
    - An unprepared tenant, a missing release and a non-root caller are
      refused before anything runs.
    - A failed deploy either leaves the board (listener back) or moves it
      (listener left down).
    - A listener that fails to start is not closed over.
    - A live board reporting another build is not closed.
    - An already-deployed board is closed only.
    - The helper's record matches the exit in every case.
    - `preview-upgrade` and `upgrade-tenant-release` argv are unchanged.
  - It passes both under `env -i` and in the pane: 26 checks.
- **Mutation, with `tests/bounded_run.py mutate`: 12 of 12 killed.** Each
  kill was confirmed from its assertion line. The mutants cover:
  - the catalogue back to the preparation;
  - no preparation check, or no held-release check;
  - no listener stop or no unit install;
  - success without the close;
  - the listener handling reversed on each failure path;
  - a failure not recorded, or a start failure ignored;
  - no root check;
  - the close's words passed through.
- **Sweep against 4ebb846.** 78 suites were run under `env -i`, with refusing
  stubs for claude, codex, agy and konsole; none was reached. They are the
  upgrade/release/privileged set plus every suite naming the dispatch, the
  command tables, the operations, `project_status` or `upgrade_records`. 64
  pass on both trees and 14 fail on both, with the same final line. The one
  exception is `claude_permission_hook_test`, which failed differently under
  the parallel sweep and identically on both trees when rerun twice.

Not done: no live deploy. The next MEFP packet would install a shared release
containing this fix, then run from the Director pane with the shared client
by path:
1. `preview-upgrade commit=<C>`;
2. `upgrade-tenant-release commit=<C>`;
3. `deploy-release commit=<C>`;
4. `release-status`.
