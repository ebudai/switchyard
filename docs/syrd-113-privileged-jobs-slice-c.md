# SYRD-113 slice C: a tenant's journal is its own, kept for a bounded time, and a rollback is an outcome

Slices A and B made each privileged run a job in root's rollout journal and
recorded it on its ticket. Slice C covers what remained:
- access control for the journal, and repair of the records already written;
- retention;
- a rolled-back deploy as an outcome of its own;
- installation and repair through provisioning and upgrade;
- the live acceptance.

## What was there

- **Every account could read every tenant's journal.** Attempt directories
  were 0755, and files 0444, or 0644 while live. MEFP's output was readable by
  Otto's account and every other account on the host. Every attempt's `open`
  reset the journal directory to 0755, so a repair from outside would not
  have lasted.
- **Nothing was ever removed.**
- **A rolled-back deploy exited 1, like every failure.** A health gate that
  failed after the deploy, with a rollback that restored the previous
  release, was recorded as an ordinary `failed`.

## The change

**Whose journal.** A tenant's journal is owned by root, in the group that
tenant's board service runs with:
- **Where the group comes from:** the `Group=` in the `[Service]` section of
  root's own unit for that board, `/etc/systemd/system/<project>-ticket-board.service`
  (`rollout_journal.tenant_group`). The unit is read without following a link,
  and believed only if it is a regular file root owns that nobody else may
  write.
- **Why that group:** it is the group that lets the board service, slice B's
  reader, read the journal. On this host that is `stellaris-agent` for MEFP,
  `otto-agent` for Otto and `syrd-roles` for SYRD. The owner's own gid would
  be wrong for SYRD.
- **Modes:** directories 0750, a live result and live logs 0640, finished
  files 0440, the index 0640. Another tenant's account can read none of it.
- **No group:** a project whose unit names none (the legacy `pgu`), or a
  projectless `_host`, keeps the modes above. The repair reports such a
  tenant rather than guessing a group.
- **Only root decides:** only root resolves the group. Any other writer keeps
  the legacy modes, because only root can give a file to a tenant's group.

**Repair and install.** Closing an attempt repairs the whole journal
(`repair_journal`) and applies retention. Every root write to a journal
closes an attempt, so this covers each path the ticket names:
- **New-project provisioning:** the recorded operator packet is an attempt.
  Its unit, and so its group, may only appear during that run, so the group is
  resolved again at close.
- **`upgrade-tenant`, `repair-boundary` and every other privileged action:**
  each records an attempt.

Existing records are therefore repaired by the next root write.
`repair_journal` follows nothing: a link is reported and left alone, and so is
anything root does not own. A second repair finds nothing to do.

This replaced an earlier plan to call a repair from the upgrade, provisioning
and `repair-boundary` modules. Their guard suites pin the exact imports and
launcher reads of each, and `team_launcher.py` is over the review ceiling.
Putting the repair in the one writer every root path already uses covers the
same paths without widening them.

**Retention.**
- **What is kept:** the newest 200 attempts always. An older attempt is
  removed once it finished more than 180 days ago, and never while running.
- **What a removal leaves:** a chained `pruned` entry in the index, so
  `verify_index` still passes, `switchyard rollout-log` says "removed by
  retention" for it, and attempt numbering goes on.
- **The board:** a job slice B recorded keeps its row and its event; only
  root's directory goes.

**A rollback is an outcome.**
- **The script:** `ticket-board-service.sh deploy-restart` exits 3
  (`ROLLED_BACK_EXIT`) when a health gate failed after the restart and the
  rollback restored and smoke-checked the previous release. A rollback that
  itself fails still dies with 1.
- **`deploy-release`:** it marks that problem `rolled back:`
  (`tenant_release_deploy.ROLLED_BACK`) and exits 3 when the board is
  back where it was.
- **The helper:** it records `deploy-release`'s exit 3 as `rolled_back`
  (`privileged_operations.ROLLED_BACK_EXITS`).
- **The board** (migration `pgu992_syrd113_rolled_back_jobs.sql`, with
  `schema.sql` the same) accepts the state, whose check is rebuilt as the
  union of every state. Its event says the deploy "failed its health gate
  after the deploy and was rolled back to the previous release", and it wakes
  the owner and the controller as for any job that did not succeed.
- **Other callers:** every other caller of deploy-restart already treats any
  non-zero exit as a failure.

**The live acceptance.** The Director approved one inert catalogued action, `job-selftest`, so a
success and a failure can both be run through the real boundary without
touching the host.
- **Its values:** it requires `project`, the originating `ticket` and
  `outcome=succeed|fail`, and accepts no other value, free text, flag or
  repeat.
- **Its boundary:** pre-authorized for an active session like the other tenant
  actions. The helper's control-pane identity check, its ticket check and the
  polkit policy apply unchanged.
- **What root runs:** as root runs every catalogued operation, the pinned
  launcher with literals and validated values:
  `/opt/switchyard/current/switchyard job-selftest <project> --outcome <outcome>`.
  This is the SYRD-112 rule `privileged_front_door_test` enforces. Its allowed
  literals gain `job-selftest`, as SYRD-531's gained `deploy-release`.
- **How the launcher runs it:** both `switchyard` wrappers hand `job-selftest`
  to `scripts/ticket_board/privileged_selftest.py` before they import the
  launcher or anything else. So a self-test imports only `sys`, reads only its
  arguments, touches no file, process, socket or environment, and goes nowhere
  near the dispatcher. The dispatcher, its guards and `team_launcher.py` are
  unchanged.
- **What it prints:** fixed lines, one of them a fixed fake credential and one
  a fake bearer, so a run shows its record redacting them.
- **How it ends:** 0 for `succeed`, 1 for `fail`, and 2 with a usage line for
  any other shape.

A first version ran the program inline (`python -I -S -c <program>`), which
broke that launcher rule. It also showed something worth knowing: `result.json`
records the operation's command verbatim, unredacted, so an inline program's
fake credential was persisted there. Every catalogued command is built only
from literals and validated values, so nothing secret reaches a command. The
self-test's text now lives in its file, and the record names only the
launcher and its arguments.

The live acceptance is for after this commit's Audit, from the Director's
pane:
1. `switchyard privileged-action <project> job-selftest ticket=<id> outcome=succeed`
2. the same with `outcome=fail`

Both results should reach the ticket with nothing copied from the
terminal.

## Tests

`tests/privileged_job_journal_lifecycle_test.py` (11 checks) and
`tests/privileged_job_selftest_test.py` (24 checks).
- **Setup:** the test can't be root. Where root's ownership and a tenant group
  are needed, it uses its own uid and one of its own supplementary groups.
- **The deploy:** it runs through `catalogued_deploy_release_test`'s own
  harness, with the real `switchyard deploy-release` and the helper. The fake
  host's failed deploy-restart exits 3, as the script now does after a
  rollback.

**Main, from a git archive in a child process:** every attempt is 0755/0444
(and 0644) to every account; there is no repair and no retention; and a
deploy its rollback undid exits 1 and is recorded `failed`.

**This tree:**
- **A non-root writer** still writes the legacy modes.
- **The group:** taken from root's own unit's `[Service]` `Group=`, and only
  from a regular file root owns that nobody else may write. A writable,
  linked, foreign-owned, missing or groupless unit, a `Group=` in another
  section, and an unknown group all give none. So does `_host`.
- **Modes:** a tenant's live attempt is root's and its group's only. Closing
  it leaves the whole journal in the tenant's modes, repairing the two legacy
  attempts beside it, and a second repair finds nothing.
- **Repair edge cases:** a link is left alone and reported, a running attempt
  keeps its live modes, and a groupless tenant is reported.
- **Retention:** of seven attempts, the newest three kept, the three old
  finished ones are removed and the old running one is kept. Each removal is
  chained, the journal verifies, `rollout-log` says so, and the next attempt
  is numbered 0008.
- **Rollback:** a deploy its rollback undid exits 3 and is recorded
  `rolled_back`, while one that left the board moved is still a failure. The
  script's five health-gate rollbacks all end with the one rolled-back exit,
  and the script, the deploy and the helper agree it is 3.

`tests/privileged_job_reconcile_test.py` (slice B) gains a rolled-back job.
The board records it as `rolled_back` with exit 3, writes an urgent event
that says the deploy was rolled back, and wakes the owner and the Director
once each. Its real board process now gets the unit's environment instead of
the pane's: under a pane's `TICKET_BOARD_PROCESS_AUTHORITY=1`, which the
Director's run had, it demanded `TICKET_BOARD_TENANT_USER`.

## Evidence

- **Mutation:** thirty-one mutants, run through `tests/bounded_run.py mutate`,
  all killed. Each kill was read on its failing assertion.
  - **The tenant group (four):** a `Group=` outside `[Service]` counts; a
    writable unit is believed; a linked unit is followed; anybody's unit is
    believed.
  - **Modes (two):** tenant directories world-readable; finished files
    world-readable.
  - **Repair (two):** closing does not repair the journal; the repair follows
    links.
  - **Retention (three):** a running attempt is pruned; the newest are pruned;
    a pruning is not chained.
  - **The root-only rule:** any writer resolves a tenant group. Its mutant is
    caught by a crash: it tries to give a scratch file to this host's real Otto
    group, which is the leak the rule prevents.
  - **Rollback (seven):**
    - one health gate's rollback exits 1;
    - the report doesn't mark a rollback;
    - `deploy-release` reports a rollback as a failure (run twice, once on
      each version of the file);
    - the helper records it as a failure;
    - the board refuses the state, and the reader refuses it.
  - **The reader:** a missing journal is an error.
  - **The self-test (eleven, over its two shapes):**
    - the outcome takes any text, and the ticket is optional (each run in both
      shapes);
    - it cannot fail (both shapes);
    - it reads the environment (both shapes);
    - it runs unisolated (inline shape);
    - it takes any shape (launcher shape);
    - the launcher never hands it over (launcher shape).
- **The sweep:** the 266 suites that touch the journal, `rollout-log`,
  record-rollout, the privileged catalogue, helper, front door and operations,
  the release deploy and its report, the deploy script, the schema, rbac and
  migrations, and the journal's other writers. Each was run serially on this
  tree and on main 2c23f01, with provider stubs that refuse to start.
  - **Exit codes:** identical except `ticket_board_service_deploy_test.sh`.
    That suite is red on both trees in the same environment failure, and its
    rollback passes on both. Here that rollback exits 3, the outcome this slice
    adds.
  - **The same reds:** 39 are red on both. Their normalised output is
    identical, apart from:
    - migration listings that now name pgu992;
    - two failure messages that print the policy, which now holds the new
      action;
    - a two-line shift in `privileged_front_door_test`. Run test by test, it
      gives 35 pass and 1 fail on each tree, the same test.
  - **Neighbours changed, each because what it pinned changed by design:**
    - `ticket_board_service_health_owner_test`: a rolled-back health gate now
      exits 3.
    - `board_env_file_commit_cache_test`: the text check reads the rolled-back
      exit after the rollback.
    - `tenant_release_report_boundary_test`: its simulated failed deploy used
      exit 3, now the rolled-back exit, so it uses 7. The rollback constants
      live in `tenant_release_deploy`, so the moved report module gains no
      name its guard pins.
    - `privileged_front_door_test`: allows the `job-selftest` literal.
    - `provision_role_tooling_boundary_test` and
      `switchyard_parsers_boundary_test`: their recorded answers carry the
      rendered policy and the privileged-action help. Both were re-run with
      each suite's own runner. With `job-selftest` left out of the policy, all
      four changed tooling answers reproduce the old ones exactly; the parser's
      differs only by `, job-selftest`.
    - `privileged_job_capture_test` (slice A): its failing child exits 5,
      since 3 is now `deploy-release`'s rolled-back exit.
  - **Main moved:** to 2c23f01 (SYRD-577) during the work. Its `ended()`
    lifecycle report conflicted with the rolled-back exit in
    `tenant_release_deploy.py`. The resolution passes 3 through `ended()`, and
    SYRD-577's own `upgrade_lifecycle_preservation_test` passes (36 checks).

**The self-test** (`tests/privileged_job_selftest_test.py`) runs the helper's
installed entry point, `main()`, against a real board:
- **The board:** production-built, with a declared workflow and process
  authority, and the Director role registered to a live process.
- **The real path:** the control-caller check, the ticket check, the journal,
  the streaming runner and the action's own operation. The pinned launcher is
  this tree's own `switchyard` wrapper, the file root's installed release
  carries.
- **Stood in:** only the host's own facts: the installed boundary, the pane
  identity and the registry's board URL.

Main refuses `job-selftest` as uncatalogued. Here:
- **The two runs:** the control pane's success exits 0 and its failure 1. Each
  is recorded as a job for its ticket, with only the fixed lines, the fake
  credential and bearer redacted, while the pane shows them. The working
  directory and home are untouched.
- **Refusals:** a sibling's pane, a missing ticket, another board's ticket, a
  ticket the board doesn't hold, an outcome not offered, free text, an extra
  value, an environment value, a flag and a repeated value are each refused
  before anything is run or recorded.
- **The board:** it records both on the ticket. With no journal yet, its
  reader reports no jobs instead of the error main raised.
- **The code itself:** the self-test imports only `sys` and names no file,
  process, socket or environment access. Root's command is the pinned
  launcher with literals and validated values, and both wrappers hand the
  self-test over before importing anything else.
- **The launcher directly:** success 0, failure 1, and any other shape 2.
