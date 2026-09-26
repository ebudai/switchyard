# SYRD-272: source size inventory and launcher extraction plan

This document serves the SYRD-272 parent. Each extraction child updates it:
the before/after table, the slice log, and the plan's next entry. SYRD-286 was
the first slice, SYRD-287 the second, SYRD-288 the third, SYRD-289
the fourth, SYRD-290 the fifth, SYRD-291 the sixth, SYRD-292
slice 6a, SYRD-293 slice 6b, SYRD-294 slice 6c,
SYRD-295 slice 7a and SYRD-296 slice 7b.

- Baseline: public main `d5ffdd00a2b162ac9bee545f52ba0840f70fc7ed`. The
  exclusive refactor window opened at this commit.
- SYRD-286 was integrated as `4af050e436123fc1ad7d2aed0034ed771cc5bba9`, which
  is SYRD-287's baseline.
- SYRD-287 was integrated as `95c11f0ae3f8e6b873aa1e42c99a7064558fa2af`, which
  is SYRD-288's baseline.
- SYRD-288 was integrated as `fd85a84a91636b3230fbf238d47c1ae7e24fe957`, which
  is SYRD-289's baseline.
- SYRD-289 was integrated as `3e7337bef48d93363dd8e3ae018cc03cb5b79fe1`, which
  is SYRD-290's baseline.
- SYRD-290 was integrated as `9c804c7d26731af80a347a5612424c899f79e352`, which
  is SYRD-291's baseline.
- SYRD-291 was integrated as `9ceed50d1b1f9c4c95565865240164cd9cf8574c`, which
  is SYRD-292's baseline.
- SYRD-292 was integrated as `6f0ccb733c0b6df1a8627c77d021238056cad973`, which
  is SYRD-293's baseline.
- SYRD-293 was integrated as `37bbb53ce823f222e20fa6beb20a0bffa57827af`, which
  is SYRD-294's baseline.
- SYRD-294 was integrated as `501ca7697dabc0eea58641cfa3393965d9598aeb`, which
  is SYRD-295's baseline.
- SYRD-295 was integrated as `fede447580805287cbdfb8b149d1e1d7a846636d`, which
  is SYRD-296's baseline.
- Soft limit: 1,250 lines, advisory. The pre-commit warning is unchanged.

## 1. Tracked source inventory

These are the tracked non-test files over the soft limit at the baseline,
followed by the parent's list. Each "after" column is that child's candidate.

| File | Baseline | After SYRD-286 | After SYRD-287 | After SYRD-288 | After SYRD-289 | After SYRD-290 | After SYRD-291 | After SYRD-292 | After SYRD-293 | After SYRD-294 | After SYRD-295 | After SYRD-296 | Notes |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| `scripts/team_launcher.py` | 38,527 | 37,808 | 36,670 | 36,379 | 35,730 | 34,987 | 34,365 | 33,826 | 33,573 | 33,377 | 32,030 | 31,654 | Plan in §3. |
| `scripts/worker_pool_command.py` | — | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | 777 | New in SYRD-286. |
| `scripts/role_credentials.py` | — | — | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | 832 | New in SYRD-287. |
| `scripts/agy_credential.py` | — | — | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | 484 | New in SYRD-287. |
| `scripts/upstream_report.py` | — | — | — | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | 351 | New in SYRD-288. |
| `scripts/host_accounts.py` | — | — | — | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | 23 | New in SYRD-288: a dependency-free leaf. |
| `scripts/project_onboarding.py` | — | — | — | — | 753 | 753 | 753 | 753 | 753 | 753 | 753 | 753 | New in SYRD-289. |
| `scripts/role_command.py` | — | — | — | — | — | 212 | 212 | 212 | 212 | 212 | 212 | 212 | New in SYRD-290. |
| `scripts/model_validation.py` | — | — | — | — | — | 694 | 694 | 694 | 694 | 694 | 694 | 694 | New in SYRD-290. |
| `scripts/project_worktrees.py` | — | — | — | — | — | — | 742 | 742 | 742 | 742 | 742 | 742 | New in SYRD-291. |
| `scripts/launcher_checkout.py` | — | — | — | — | — | — | — | 632 | 632 | 632 | 632 | 632 | New in SYRD-292. |
| `scripts/owner_git.py` | — | — | — | — | — | — | — | — | 323 | 323 | 323 | 323 | New in SYRD-293. |
| `scripts/pane_hooks.py` | — | — | — | — | — | — | — | — | — | 262 | 262 | 262 | New in SYRD-294. |
| `scripts/agent_cli_discovery.py` | — | — | — | — | — | — | — | — | — | — | 435 | 435 | New in SYRD-295. |
| `scripts/agent_cli_promotion.py` | — | — | — | — | — | — | — | — | — | — | 1,099 | 1,099 | New in SYRD-295. |
| `scripts/first_run_setup.py` | — | — | — | — | — | — | — | — | — | — | — | 443 | New in SYRD-296. |
| `scripts/ticket_board/schema.sql` | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | Proposed exception: one DDL document applied whole. It is still reviewed as its own child. |
| `scripts/ticket_board/project_provision.py` | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | Parent list; needs a child. |
| `scripts/ticket_board/notify_listener.py` | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | Parent list; needs a child. |
| `scripts/presentation_controller.py` | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | Parent list; needs a child. |
| `scripts/ticket_board/app.py` | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | Parent list; needs a child. |
| `scripts/ticket_board/server.py` | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | Parent list; needs a child. |
| `scripts/ticket_board/migrations/pgu921_syrd11_declarative_workflow.sql` | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | Proposed exception: a migration is immutable history. |
| `scripts/ticket_board/frontend_script_core.py` | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | Parent list; generated front-end asset. Its boundary is the asset, not Python modules. |
| `scripts/ticket_board/write_client.py` | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | Parent list; needs a child. |
| `scripts/ticket-board-service.sh` | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | Parent list; a shell entry point. Split along its own subcommands. |
| `scripts/ticket_board/migrations/pgu528_workflow_rbac_config_authoritative.sql` | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | Proposed exception: migration. |
| `scripts/ticket_board/migrations/pgu589_depersonalize_user_role.sql` | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | Proposed exception: migration. |
| `deploy/SYRD-87-recover-syrd-runtime.sh` | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | Proposed exception: a one-off recovery packet kept as a record. |

For comparison only: 16 test files are over 1,250 lines, the largest being
`tests/ticket_board_postgres_triggers_test.py` at 4,034. They are not in this
plan.

The exceptions above are proposals. The Director reviews and records them on
SYRD-272; none is taken as granted here.

To reproduce the counts:

```sh
git ls-files | grep -v '^tests/' | grep -Ev '\.(md|json|txt|png|svg|lock)$' \
  | xargs wc -l | sort -rn | awk '$1 > 1250'
```

## 2. `scripts/team_launcher.py`: responsibilities and seams

### The public surface a split has to keep

- **Entry points.** `./switchyard`, `scripts/switchyard`, `scripts/team-launcher`
  and `scripts/switchyard-viewer-layout` all import `scripts.team_launcher`, so
  every entry point uses one module object.
- **Importers.** 114 files import the launcher: 104 tests and 10 non-test files. These
  include `presentation_controller`, `worker_pool`, `workflow_launcher`,
  `workflow_manage` and `onboarding_readiness`. Together they read about 487
  distinct attributes as `team_launcher.<name>`.
- **CLI verbs.** The verbs come from `SWITCHYARD_COMMANDS`. Each parser and
  handler is dispatched from `switchyard_main`.
- **Monkeypatch seams.** Tests patch launcher facilities on the module, for
  example:
  - `team_launcher.current_user_name` (98 sites);
  - `_owner_home_for_auth`;
  - `_cli_auth_status`;
  - `_workdir_is_trusted`;
  - `load_project_config`.

  Moved code that calls such a facility must look it up on `team_launcher` at
  call time. Otherwise the patches silently stop reaching it.
- **Git ownership lint.** `tests/team_launcher_git_ownership_lint_test.py`
  scans every module in its `GIT_LINTED_MODULES`. Since SYRD-293 that is
  `team_launcher.py`, `project_worktrees.py`, `launcher_checkout.py` and
  `owner_git.py` (the chokepoint and its call sites). The test
  `test_every_module_that_defines_a_git_builder_is_linted` fails if any
  `scripts/` module defines a `git_*_args` builder outside that set, so a slice
  that moves builders must add its module in the same commit.

### Responsibility map (heuristic)

Each top-level definition is assigned to a domain by ordered name rules. The
preceding comments and blank lines are counted with it, so the column sums to
the whole file. This is a map for planning, not an exact boundary: every child
re-derives its own closure before moving anything.

| Domain | Lines | Defs |
|---|---:|---:|
| provisioning (new/register/teardown/owner accounts) | 5,051 | 182 |
| release selection, install and upgrade | 4,787 | 120 |
| desktop, presentation windows and display bridge | 4,437 | 193 |
| project config and registry | 2,721 | 68 |
| privileged boundary, tenant control and repair | 2,717 | 80 |
| agent CLI discovery, promotion and first-run auth | 2,633 | 70 |
| general helpers (unclassified) | 2,442 | 222 |
| tmux panes and sessions | 2,076 | 94 |
| provider state, runtime registration and role identities | 1,761 | 46 |
| workflow declaration and rebind | 1,567 | 36 |
| model/effort/CLI runtime selection — **command construction and model validation moved by SYRD-290** | 1,456 | 70 |
| credentials (agy, role seeding, upstream report) — **agy and role seeding moved by SYRD-287; upstream report by SYRD-288** | 1,436 | 65 |
| repository hooks, git and worktrees — **project worktrees and control repository moved by SYRD-291** | 1,196 | 68 |
| CLI parsers and dispatch | 1,157 | 15 |
| board service, listener and status | 1,095 | 54 |
| onboarding docs, prompts and skills — **docs, director onboarding and board skill moved by SYRD-289** | 796 | 32 |
| worker pool — **moved by SYRD-286** | 737 | 19 |
| launcher checkout self-update — **moved by SYRD-292** | 462 | 15 |

### How a slice is chosen and cut

1. **Compute the closure.** Take the domain's definitions, plus every helper
   whose callers all lie inside it.
2. **Measure the coupling.** Count:
   - references from the closure to the rest of the launcher;
   - references from the rest into the closure;
   - how many tests patch its names.

   Prefer few inbound edges and no patched names.
3. **Move the code unchanged.** Move it into one named module. The launcher
   imports that module at top level, by explicit name, and nothing else. The
   module never imports `team_launcher` at top level; it reads launcher
   facilities through `from scripts import team_launcher as launcher` inside
   the functions that need them. `scripts/worker_pool.py` already uses this
   pattern.
4. **Keep old names importable.** Every moved name that was reachable as
   `team_launcher.<name>` stays so, through that explicit import list. There is
   no `import *`, no `__getattr__` and no generated globals.
5. **Prove the move is unchanged.** Since SYRD-287 the proof is by AST and
   comments, not line spans:
   - every moved definition's AST equals the baseline's once `launcher.X` is
     read as `X` and the function-level launcher import is dropped;
   - the launcher's top-level nodes equal the baseline's minus the moved ones,
     plus only the new import statements;
   - comment lines are conserved as a multiset;
   - every name the baseline bound at top level is still bound, unless it is a
     moved private name nobody outside reads.

   SYRD-286's text diff shared its line spans with the tool that did the cut.
   In SYRD-287 the same kind of span bug cut two stay-behind constants and
   still passed that diff. The independent proof caught it, and it was shown to
   fail on that fault, on a changed body and on a lost comment. Run
   retroactively on SYRD-286 (`4af050e` against `d5ffdd0`), it also holds.
6. **Default arguments are bound at import.** A moved function whose
   parameter defaults to a launcher name cannot defer that name to call time.
   Such a dependency moves to a leaf module that both the launcher and the
   moved code import, so it stays one object. The extractor refuses a launcher
   name in a default or decorator (SYRD-288).
7. **No name may be left unbound.** The proof also fails on any name used but
   bound nowhere, in the launcher or a new module. A module header built by hand
   can miss an import: SYRD-288 caught `dataclasses.replace` this way.
8. **Imported launcher names are launcher attributes too.** A name the
   launcher binds by importing it from another Switchyard module (such as
   `home_dir_for_user` since SYRD-288) is still patched on the launcher by the
   suites. Moved code that calls it reads it from the launcher at call time,
   unless it is only a default value (see rule 6). The extractor treats such
   names as launcher facilities since SYRD-289. The first cut of SYRD-289 left
   one unrewritten, and the unbound-name check (rule 7) caught it.
9. **Source-scanning guards pin text to a file.** Some suites read
   `team_launcher.py` as text. `launch_without_model_probes_test` requires
   exactly one `validate_models=True` call site there, and the git ownership
   lint scans only that file. Before moving a definition, check whether such a
   guard names it. Either leave the guarded site in the launcher (SYRD-290 left
   `switchyard_validate_models_command`), or widen the guard in the same commit
   with the reason stated.
10. **No agent CLI runs in these suites.** Claude, Codex and agy are installed
    on this host, and Claude is logged in, so a suite that finds a real
    authenticated CLI would make a paid request
    (`team_launcher_model_tool_call_probe_test` has such a case). Slices that
    touch model or CLI code run their suites with stubs for `claude`, `codex`,
    `agy`, `hermes` and `gemini` ahead of `/usr/bin` on PATH; each stub refuses
    and exits 1. The one exception is `codex_effort_config_key_test`, whose
    Codex runs only inside `unshare --net`.
11. **A second launcher module object.** `desktop_access_test`'s exported-release
    case loads `team_launcher.py` again, under another module name. Moved code
    resolves `scripts.team_launcher`, not that copy. That case has failed at the
    same assertion since before SYRD-286 (checked on `d5ffdd0`), before it
    reaches any moved code, so it gives no evidence either way. A slice that
    wants it as evidence must first make it pass on the baseline.
12. **A name defined twice binds to its last definition.** The launcher
    defines `_normalized_path` twice: at line 3091, returning a `str`, and at
    line 3999, returning a `Path`. Every caller gets the later, `Path`
    version. SYRD-293's first cut moved that later copy, and the leftover
    `str` copy then rebound the launcher's name. The node check (rule 5)
    caught it. The extractor now refuses to move any name defined more than
    once. `presentation_window_processes` is also defined twice, which matters
    for the presentation slices. Removing the dead copy would be a fix, and is
    left for its own ticket.
13. **Keep contractual patch seams.** Suites patch launcher names and expect the
   patch to reach code that is now elsewhere. A moved caller reads such a name
   from `team_launcher` when it runs, even if the definition itself moved.
   SYRD-287 does this for the owner-traversal checks.

## 3. Sequenced plan

Each child is one coherent extraction, audited and integrated before the next
starts. The sizes are closure sizes at the baseline. Every child re-measures.

| # | Child | Approx. lines | Coupling at baseline |
|---|---|---:|---|
| 1 | **Worker pool** declaration, preflight and `worker-pool` verb (SYRD-286) | 680 | 3 inbound edges, 0 patched names |
| 2 | **Agent credentials**: agy credential source and role credential seeding (`agy-credential`, `seed-role-credentials`) (SYRD-287) | 1,180 moved | 17 inbound (mostly `switchyard_new_command`); 2 patched names found on re-measure |
| 3 | **Upstream report link and credential** (SYRD-288) | 296 moved | 2 inbound (`upgrade_project_command`); 2 patched names |
| 4 | **Onboarding docs, director onboarding and generated board skill** (SYRD-289) | 636 moved | 9 inbound; 3 patched names, all at call sites that stay in the launcher |
| 5 | **Role CLI command construction and model validation** (SYRD-290) | 212 + 694 in two modules | 5 + 11 inbound; 0 patched names; `validate-models` verb kept in the launcher (rule 9) |
| 6 | **Project worktrees and control repository** (SYRD-291); widened the git ownership lint | 742 | 6 inbound; `_control_repository_owner_home` patched (routed through the launcher) |
| 6a | **Launcher checkout self-update** (SYRD-292) | 632 | 7 inbound edges; `ensure_launcher_checkout_current` patched at launcher call sites; 12 builders linted |
| 6b | **Owner-correct git execution and project git helpers** (SYRD-293) | 323 | 10 inbound edges from the launcher, plus the three git modules through `launcher.run_owner_correct_git`; that name is patched on the launcher and kept there as the seam, including for owner_git's own helpers |
| 6c | **Board pane hooks and Codex hook trust** (SYRD-294) | 262 | Measured on SYRD-293's candidate: repository (pre-commit) hooks already live in `scripts/repository_hooks.py`, so there is little repository-hook glue left in the launcher. The tmux viewer-relayout hooks belong to presentation (§3 row 12). |
| 7 | Agent CLI discovery, promotion and first-run auth | 2,600 | several children |
| 7a | **Agent CLI discovery and host-wide promotion** (SYRD-295) | 435 + 1,099 in two modules | vendor install table and its three text formatters kept in the launcher for the no-execution guard (rule 9) |
| 7b | **First-run workdir trust and setup manifest** (SYRD-296) | 443 | `_workdir_is_trusted` patched on the launcher and kept there as the seam; the auth facilities stay in the launcher |
| 7c | First-run provider auth phase | ~1,900 | measured on SYRD-296's candidate; split as below |
| 7c-1 | Provider auth-status probing (`_cli_auth_status`, `FIRST_RUN_AUTH_STATUS_COMMANDS`, `_run_owner_cli_probe`, `OWNER_CLI_PROBE_*`, `_provider_account_setup_complete`, `_owner_cli_is_installed`, `_owner_home_for_auth`) — **suggested next** | ~147 (10 defs) | heavily patched (`_cli_auth_status`, `_owner_home_for_auth`, `FIRST_RUN_AUTH_STATUS_COMMANDS`): all callers must keep reaching them through the launcher |
| 7c-2 | The interactive provider first run (`_run_provider_first_run`, `provider_is_waiting_for_an_answer`, the foreground-step helpers) | ~166 (12 defs) | re-measure |
| 7c-3 | Auth-phase orchestration and report (`run_first_run_auth_phase`, `FirstRunAuthReport`, `stop_before_launch_for_unauthenticated_providers`, `report_first_run_auth_warnings`) | the remainder; re-measure after 7c-1 and 7c-2 | keeps the install-table formatters with the guard (rule 9) |
| 8 | Board service, listener and status (`status`, `release-status` reads) | 1,100 | 19+ inbound |
| 9 | Workflow declaration, adopt/migrate/rebind verbs | 1,600 | re-measure |
| 10 | Provider state, runtime registration and role identities | 1,800 | re-measure |
| 11 | tmux panes and sessions | 2,100 | heavily patched; later |
| 12 | Desktop, presentation windows and display bridge | 4,400 | 46 inbound, 9 patched; several children |
| 13 | Privileged boundary, tenant control and repair | 2,700 | several children |
| 14 | Release selection, install and upgrade | 4,800 | several children |
| 15 | Provisioning (`new`, `register`, `teardown`, owner accounts) | 5,000 | several children |
| 16 | Config and registry core, then the remaining CLI dispatch | — | last; what remains is the thin entry module |

After the launcher, the parent's other files each get their own children:
`project_provision.py`, `notify_listener.py`, `presentation_controller.py`,
`app.py`, `server.py`, `write_client.py` and `ticket-board-service.sh`. The
generated front-end core follows its asset boundary.

## 4. Slice log

### SYRD-286: worker pool

**Moved** into `scripts/worker_pool_command.py` (777 lines), from two regions
of `team_launcher.py` that were 35,000 lines apart:

- lines 581–953:
  - the `WORKER_POOL_*` limits;
  - `WorkerPool`, `parse_worker_pool` and `WorkerPoolFinding`;
  - `worker_pool_member_role`, `worker_pool_preflight`, `_is_pool_member_role`
    and `_board_known_roles`;
  - `format_worker_pool_preflight`.
- lines 35696–36057:
  - `switchyard_worker_pool_command`;
  - its board read and apply helpers (`_worker_pool_document`,
    `_worker_pool_readiness`, `_read_board_snapshot`,
    `_apply_worker_pool_document`, `_read_board_workflow_document`);
  - `WORKER_POOL_ACTIONS` and `_build_switchyard_worker_pool_parser`.

**Boundaries:**
- **Into the new module.** `load_project_config` calls `parse_worker_pool`, and
  `switchyard_main` calls the parser and the verb. `ProjectConfig.worker_pool`
  is annotated `WorkerPool`. `team_launcher` imports those 12 names
  explicitly: the 3 edges above, plus every public name tests and scripts read
  as `team_launcher.<name>`.
- **Out to the launcher, at call time.** The moved code reads
  `current_user_name`, `_owner_home_for_auth`, `_cli_auth_status`,
  `_owner_cli_is_installed`, `_missing_cli_install_clause`,
  `_workdir_is_trusted`, `_role_cli_name`, `_resolve_switchyard_project`,
  `load_project_config` and `MAX_VISIBLE_PANES_PER_WINDOW`. `ProjectConfig`
  and `RoleConfig` are imported only for type checking.

**Navigation measurement.** The task: find and read everything that implements
the `worker-pool` preflight and verb, for example to change one finding's
wording. The counts come from the files; no timing is claimed, and no claim is
made about tokens.

| | Baseline | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 38,527 lines) | 1 (`worker_pool_command.py`, 777 lines) |
| Where it sits in that file | two regions, lines 581–953 and 35696–36057 | contiguous, the whole file |
| `grep -ci pool` over the file a reader opens | 166 hits in 38,527 lines | 153 hits in 777 lines |
| `grep -ci pool` left in `team_launcher.py` | 166 | 32 (import list, `ProjectConfig` field, verb table, dispatch) |
| `def`/`class` names with "pool" in `team_launcher.py` | 12 | 0 |

A reader who already knows both line ranges reads about 740 lines either way.
The gain is in finding them: one named file instead of two distant ranges in a
38,527-line module. Whether that shortens tickets is for the parent's final
measurement to show.

### SYRD-287: agent credential sourcing and role seeding

The domain was re-measured on `4af050e` before editing: 46 definitions in three
regions of `team_launcher.py`, at lines 436–453, 19632–19856 and 20357–21345.
It became two modules, each below the soft limit. Upstream-report credentials
stay in the launcher for their own child.

- **`scripts/role_credentials.py`** (832 lines) holds role seeding:
  - the credential artifact catalogue (`RoleCredentialArtifact`,
    `ROLE_CREDENTIAL_ARTIFACTS`, the Hermes owner dir and provider env keys,
    `hermes_credential_target`, `select_hermes_provider_env`);
  - state and manifest (`_credential_state`, `_role_credential_target`,
    `role_credential_manifest`);
  - the copy itself (`seed_role_credential` and its no-follow openers);
  - the `seed-role-credentials` verb;
  - the owner-safe primitives it stands on: `_openat_no_follow`,
    `_openat_no_follow_keep_parent`, `_copy_fd_contents`, `_read_fd_bytes`,
    `_write_all`, and the owner-traversal checks;
  - the agy token layout constants (`AGY_CREDENTIAL_DIR_NAME`,
    `AGY_CREDENTIAL_TOKEN_NAME`), which the artifact catalogue needs at import.
- **`scripts/agy_credential.py`** (484 lines) holds the agy credential source:
  - the root-owned host setting and its read/write;
  - resolution of a project's source (host default, override, opt-out);
  - validation of the source token by descriptor;
  - the owner's own token state (`AGY_CREDENTIAL_*`);
  - `_open_owner_credential_dir` and `_seed_agy_credential_for_owner`;
  - the `agy-credential` verb.

**Boundaries.**
- **Imports.** `agy_credential` imports five names from `role_credentials`, by
  name; `role_credentials` never imports `agy_credential`. Neither imports the
  launcher at top level.
- **Into the modules.** `team_launcher` imports 31 names explicitly. These are
  the launcher's own uses (`switchyard_new_command`,
  `provider_state_generation`, `repatriate_role_runtime_state`,
  `_resume_preflight_allows_attempt`, dispatch), plus every public moved name,
  plus the private names suites read. Fifteen private helpers that nothing
  outside reads are no longer launcher attributes.
- **Out to the launcher, at call time.** `role_credentials` reads
  `uid_for_user`, `_uid_for_user`, `home_dir_for_user`, `current_user_name`,
  `role_run_as_user`, `pending_identity_for`, `session_file_name`,
  `_command_name`, `_walk_no_follow` and `_group_ids_for_user`.
  `agy_credential` reads `_uid_for_user`, `current_user_name`,
  `default_gui_user`, `_is_valid_owner_user_name`, `_prompt_bool` and
  `_write_json_atomic`.
- **The one routing change.** `_require_owner_home_traversable` and
  `_require_owner_traversable` are defined in `role_credentials`, but both
  no-follow openers call them as `launcher.<name>`.
  `team_launcher_new_project_test` patches them on the launcher. Bypassing the
  launcher turns that suite red and the new boundary test's named check red.

**Security boundaries kept.** The code is AST-identical, so ownership
assignment, modes, O_NOFOLLOW openat walks and descriptor-based copies are the
same code. The effects were also compared. For each of the 10 cases in
`team_launcher_agy_credential_seeding_test` that are red on the baseline, both
sides were compared: every provisioning command attempted, the output, and the
seeded token's contents and mode. With worktree roots and a pkcheck pid
normalised, they are identical: 6 seed a 0600 token through fd-based chowns,
and 4 correctly seed nothing.

No git builder moved, so the ownership lint's scope is unchanged.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`4af050e`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 37,808 lines) | 2 (`agy_credential.py` 484, `role_credentials.py` 832) |
| Where it sits | three regions: 436–453, 19632–19856, 20357–21345 | each module contiguous |
| `grep -ci credential` in the file(s) a reader opens | 262 in 37,808 lines | 72 in 484 (agy), 67 in 832 (role) |
| `grep -ci credential` left in `team_launcher.py` | 262 | 162 (upstream report, first-run auth, import list, `new`) |
| credential/agy/hermes `def`/`class` names in `team_launcher.py` | 37 | 13 |

### SYRD-288: upstream report link and credential

Re-measured on `95c11f0` before editing: 9 definitions in one contiguous region
of `team_launcher.py`, lines 26806–27101, about 296 lines. There are 2 inbound
edges, both from `upgrade_project_command`.

- **`scripts/upstream_report.py`** (351 lines) holds:
  - `UPSTREAM_REPORT_CREDENTIAL_NAME` and `UPSTREAM_REPORT_TOKEN_KEY`;
  - `upstream_report_credential_path`, the tenant's owner-private copy of the
    report token;
  - `_board_env_report_token`;
  - `upstream_report_board`, which resolves the upstream board from the host
    registry;
  - `record_upstream_report_link`, which persists the link in the tenant
    config;
  - `refresh_upstream_report_credential`;
  - `_credential_is_private` and `_write_owner_private_file`, the owner-only
    write through an owned directory chain.
- **`scripts/host_accounts.py`** (23 lines) holds `home_dir_for_user`, unchanged.

**Boundaries.**
- **Into the modules.** `team_launcher` imports 9 names explicitly: the 8 moved
  names callers or suites read, and `home_dir_for_user`.
  `upstream_report_credential_test` patches
  `team_launcher.record_upstream_report_link` and
  `refresh_upstream_report_credential` and then drives the launcher's
  `upgrade_project_command`. That call site stayed in the launcher, so the
  patches still reach it. `_credential_is_private` is no longer a launcher
  attribute; nothing outside read it.
- **Out to the launcher, at call time.** `uid_for_user`, `_load_json`,
  `_write_json_atomic`, `_registry_project_entries` and
  `_open_owned_directory_chain`.
- **The one boundary change.** Five moved functions take
  `home_for_user=home_dir_for_user` as a default argument. A default is
  evaluated when the `def` runs, and the suite asserts it `is
  team_launcher.home_dir_for_user`. So `home_dir_for_user`, an 8-line pure
  `pwd` lookup, moved into the leaf `host_accounts`. The launcher and
  `upstream_report` both import it from there, so it is one object, and
  patches on `team_launcher.home_dir_for_user` still reach every launcher
  caller. The defaults bind exactly as before: once, to the real function.

**Evidence.**
- The AST proof holds for both modules, including the stay-behind bound-name
  check. It fails on a planted lost comment, a changed default, and the missing
  `replace` import.
- The new `tests/upstream_report_boundary_test.py` has 8 checks, and 6 of 6
  mutations are killed.
- `upstream_report_credential_test` (54 checks), the home-patching suites and
  the previous slices' boundary tests are green and identical to the baseline.
- The pre-existing reds are identical per case.
- CLI help output is byte-identical to the baseline.
- A staged release contains and loads both modules, with the default identity
  intact.

No git builder moved.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`95c11f0`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 36,670 lines) | 1 (`upstream_report.py`, 351 lines) |
| Where it sits | lines 26806–27101 | the whole file |
| `grep -ciE 'upstream.?report'` in the file a reader opens | 78 in 36,670 lines | 39 in 351 lines |
| `grep -ciE 'upstream.?report'` left in `team_launcher.py` | 78 | 54: the `upstream_report_url` config field, CLI flags, provisioning plumbing and the import list |
| `def` names with `upstream_report` in `team_launcher.py` | 4 | 0 |

### SYRD-289: onboarding documents, director onboarding and the board skill

Re-measured on `fd85a84` before editing: 24 definitions, about 636 lines, in
five regions of `team_launcher.py`: 219–238, 7188–7258, 15575–16038,
27831–27883 and 28550–28625. They moved into one module,
**`scripts/project_onboarding.py`** (753 lines):
- **Onboarding documents:**
  - the `SWITCHYARD_*ONBOARDING*` names;
  - the director seed text and `seed_director_onboarding`;
  - writing, stamping and installing the docs;
  - source-commit provenance (`_switchyard_source_commit`, snapshot parsing,
    history and ancestry reads through `run_owner_correct_git`);
  - `upgrade_switchyard_onboarding_docs`.
- **Director onboarding:** `director_onboarding_state` and
  `migrate_declarative_director_onboarding`.
- **The generated project board skill:** `BOARD_SKILL_NAME`, the installer
  path, the install args and `ensure_generated_project_board_skill`.

**Not moved.** The `role-prompt` verb is an inline block of `switchyard_main`
that forwards to `scripts/workflow_manage.py`, where the role-prompt logic
already lives. Extracting it would rewrite `switchyard_main` rather than move
definitions. The `board-skill` verb is already `scripts/board_skill_cli.py`.
Model and runtime selection is the next slice.

**Boundaries.**
- **Into the module.** `team_launcher` imports 19 names explicitly: its
  callers' names in `new`, `upgrade`, `finish-upgrade` and launch, and every
  name the suites or `workflow_manage` read. Five private helpers nothing
  outside reads are no longer launcher attributes. The suites patch
  `_install_switchyard_onboarding_docs`, `director_onboarding_state` and
  `migrate_declarative_director_onboarding` around launcher call sites that
  stayed, and no moved function calls a patched moved name.
- **Out to the launcher, at call time.** 12 names: `run_owner_correct_git`,
  `_chown_project_file`, `_open_board_url`, `current_user_name`,
  `home_dir_for_user` (rule 8), `_load_json`, `_switchyard_dir`,
  `_proc_failure_reason`, `_read_switchyard_release_marker`,
  `shared_switchyard_release_for_path`, `_is_generated_project_layout_template`
  and `director_phase_required`. `ProjectConfig` and `DeclaredWorkflowPresence`
  are annotation-only.
- **Workflow-config names.** `DIRECTOR_ONBOARDING_MIGRATION` and
  `DIRECTOR_ROLE` come from `scripts.ticket_board.workflow_config`, imported by
  the module. The launcher's own import of them is unchanged.
- No default or decorator names a launcher name. No `git_*_args` builder moved,
  so the lint scope is unchanged.

**Evidence.**
- The AST proof holds, including the bound-name check. It fails if the
  `home_dir_for_user` call is left unrewritten.
- The new `tests/project_onboarding_boundary_test.py` has 7 checks, and 4 of 4
  mutations are killed. One of them binds the home lookup from the leaf module
  instead of the launcher.
- Green and identical to the baseline: the board skill and staged bundle,
  role onboarding prompt, role-prompt command integration, director
  verification policy, the finish-upgrade suites, legacy workflow migration,
  the release phase journal, desktop policy generation, single-owner staged
  tooling, installed-release deploy, workflow migration, the skill reminder,
  cutover, and the three earlier boundary tests.
- Pre-existing reds are identical per case or by whole log:
  `team_launcher_onboarding_git`, `desktop_access`, `claude_permission_hook`
  and `team_launcher_declarative_workflow` (`unshare --map-auto`).
- CLI help output is byte-identical to the baseline for 9 invocations.
- A staged release contains and loads the module, and its own `board-skill`
  and `role-prompt` help exit 0.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`fd85a84`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 36,379 lines) | 1 (`project_onboarding.py`, 753 lines) |
| Where it sits | five regions, from line 219 to line 28625 | the whole file |
| `grep -ci onboarding` in the file a reader opens | 154 in 36,379 lines | 94 in 753 lines |
| `grep -ci onboarding` left in `team_launcher.py` | 154 | 83: callers in `new`/`upgrade`/`finish-upgrade`/launch, the inline `role-prompt` dispatch, prompt limits and the import list |
| onboarding/board-skill `def`/`class` names in `team_launcher.py` | 16 | 0 |

### SYRD-290: role CLI command construction and model validation

Re-measured on `3e7337b` before editing. The ~630 estimate covered two
responsibilities, so they became two modules with a one-way import:

- **`scripts/role_command.py`** (212 lines, 13 definitions) builds a role's CLI
  command:
  - the per-CLI adapter tables: `YOLO_ARGS_BY_CLI`, `STARTUP_ARGS_BY_CLI`,
    `EFFORT_STYLE_BY_CLI`, `DEFAULT_MODEL_ARG_BY_CLI` and
    `DEFAULT_RESUME_{MODE,FLAG,SUBCOMMAND}_BY_CLI`;
  - `yolo_args_for_role`, `startup_args_for_role` and `effort_args_for_role`
    (Codex's `-c model_reasoning_effort="<level>"`);
  - `_resume_args_for_role`, `hermes_env_for_role` and `cli_command_for_role`.

  Inbound: `tmux_new_session_args` and `_role_from_json`.
- **`scripts/model_validation.py`** (694 lines, 24 definitions) checks a
  role's model:
  - the probe constants and prompt;
  - `ModelValidationFailure`, `ModelProbeAttempt` and `_ModelProbeWorkspace`;
  - the probe command, evidence and suggestion helpers, `_probe_role_model` and
    `validate_role_models`;
  - the unknown-model report, confirm and record steps and the stop gate;
  - the interactive model and effort fields.

  Inbound: first-run auth, `new`, `switchyard_main` and the role-runtime
  prompts. It imports `YOLO_ARGS_BY_CLI` from `role_command`.

**Not moved.**
- `switchyard_validate_models_command` stays in the launcher. It is the one
  call site allowed to pass `validate_models=True`, and
  `launch_without_model_probes_test` checks that in `team_launcher.py`'s text
  (rule 9). The first cut moved it, and that suite went red only on the
  candidate. The cut was redone rather than the guard edited.
- `_command_name`, which has callers across the launcher, stays as a shared
  facility.
- CLI discovery, promotion, auth and running the probes stay in the launcher.

**Boundaries.**
- **Into the modules.** `team_launcher` imports 32 names explicitly: 12 from
  `role_command` and 20 from `model_validation`. No moved name is patched by
  any suite. Five private helpers nobody outside reads are no longer launcher
  attributes.
- **Out to the launcher, at call time.**
  - `role_command`'s reads include `_command_name`, `role_runtime_binding`,
    `session_id_for_role`, `hermes_home_for_role`, `_uses_hermes`,
    `_uses_fresh_session_per_ticket`, `default_user_bin_dirs`,
    `_prepend_paths` and `_env_unset_prefix`.
  - `model_validation`'s reads include `_role_cli_name`, `_run_owner_cli_probe`,
    `_owner_catalog_args`, `_proc_failure_reason`,
    `OWNER_CLI_PROBE_TIMEOUT_SECONDS`, `load_project_config`,
    `_write_json_atomic` and `ensure_owner_file`.
- **Library imports.** `runtime_catalog`, `terminal_select` and the
  prompt-schema names are imported directly. They are shared module objects,
  and none is patched on the launcher.

**Evidence.**
- The AST proof holds for 37 definitions, including the bound-name check.
- A **192-case argv matrix** of `cli_command_for_role`, `yolo_args_for_role`,
  `startup_args_for_role`, `effort_args_for_role` and `hermes_env_for_role` is
  **byte-identical** to the baseline. It covers:
  - 4 runtimes;
  - 3 effort levels;
  - yolo on and off;
  - fresh and resumed sessions;
  - with and without extra arguments and a model.
- The new `tests/role_command_boundary_test.py` has 9 checks, and 6 of 6
  mutations are killed.
- `codex_effort_config_key_test` gives 15 checks on both sides, including
  Codex's own header, offline.
- The model-probe, first-run, catalog, selector, pane, tmux, clone-hook,
  adoption, Hermes, process-authority and role-runtime suites, and the four
  earlier boundary tests, are green and identical to the baseline.
- Pre-existing reds are identical per case: `team_launcher_pane_paths` 9/2,
  `team_launcher_project_artifacts` 10/6, `team_launcher_env_config` 24/2. The
  two env-config failures are both command-construction cases. The command
  each builds and the assertion each fails at are byte-identical on both
  sides; they assume a real home and a real owner's PATH.
- CLI help output is byte-identical to the baseline for 7 invocations.
  `validate-models --help` treating `--help` as a project is existing
  behaviour, the same on both sides.
- A staged release contains and loads both modules.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`3e7337b`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 35,730 lines) | 2 (`role_command.py` 212, `model_validation.py` 694) |
| Where it sits | regions from line 439 to 18786 | each module contiguous |
| model/effort/yolo/startup/cli-command `def`/`class` names in `team_launcher.py` | 27 | 5 (live-model inspection and the `validate-models` verb) |
| `grep -ci effort` in the launcher / the new modules | 59 / — | 44 / 15 + 6 |
| `grep -ciE 'model.?probe\|model.?validation'` in the launcher / the new modules | 50 / — | 27 / 38 |

### SYRD-291: project worktrees and the control repository

Re-measured on `9c804c7` before editing. Everything named git, worktree, hook,
repo, clone or commit forms a closure of 146 definitions and about 3,044 lines,
far more than the ~1,200 estimate. So this child took one coherent portion and
proposes 6a–6c (§3) for the rest.

**Moved** into **`scripts/project_worktrees.py`** (742 lines): 41 definitions
from line 672 and lines 3914–5212.
- **Git argv builders:** 16 of them for the control repository (clone, remote
  rename, fetch refspec, fetch ref, worktree add), role worktrees (check,
  status, reset, clean and dry run) and the shared checkout (check, checkout,
  status, clean and dry run), plus `git_fetch_worktree_ref_args`,
  `control_repository_refspec` and `mkdir_p_args`.
- **Refresh warnings:** `_config_git_owner_rules`, and the dirty-tree
  warnings and their parsers.
- **The control repository:** `CONTROL_REPOSITORY_*`,
  `control_repository_state`, `ensure_control_repository`,
  `chown_control_repository_args`, and ownership repair
  (`_control_repository_*`, `repair_control_repository_ownership`).
- **Worktrees:** `ensure_control_role_worktrees`, `ensure_project_worktrees`,
  `fetch_project_worktree_ref` and `WorktreeProvisionResult`.

**Stayed.**
- The owner-correct chokepoint (`run_owner_correct_git`, `GitOwnerRule`).
- Launcher-checkout self-update and its `git_launcher_*` builders.
- `worktree_ref`, which has 16 callers.
- The worktree helpers inside other domains.
- `_path_owner_label` and `_path_owner_ids`. They are generic path-owner
  helpers, siblings of `_path_owner_user`, and are patched in
  `team_launcher_control_repo_test`.

**Boundaries.**
- **Into the module.** `team_launcher` imports 36 names explicitly.
  `ensure_project_worktrees` (patched in 2 suites) is called only from
  launcher sites that stayed.
- **The one routing change.** `_control_repository_owner_home` moved, but four
  suites patch it on the launcher, so its two moved callers call
  `launcher._control_repository_owner_home` (rule 12).
- **Out to the launcher, at call time.** `run_owner_correct_git`,
  `GitOwnerRule`, `worktree_ref`, `current_user_name`, `role_run_as_user`,
  `_normalized_path`, `_proc_failure_reason`,
  `_control_repository_owned_roots`, `_path_owner_label` and `_path_owner_ids`.

**Git ownership lint, widened in the same commit.**
- `GIT_LINTED_MODULES` now includes `project_worktrees.py`.
- A new guard fails if any `scripts/` module defines a `git_*_args` builder
  outside that set.
- Mutation-checked. A builder run through `runner(...)` in the new module, and
  a builder's argv assigned before being run, are each reported *at
  `project_worktrees.py`*. Dropping the module from the set fails the guard.
- The launcher's two lint findings are the baseline's, compared by source
  text: the deploy-ref builders in an error message.
- In a normal run the suite stops at that baseline-red case, so the new guard
  was exercised case by case.

**Evidence.**
- The AST proof holds for 41 definitions, including the bound-name check.
- The new `tests/project_worktrees_boundary_test.py` has 8 checks, and 4 of 4
  mutations are killed.
- Green and identical to the baseline:
  - control-repository placeholder, pane commands, repository boundary
    repair, worktree inheritance and worktree cleanup;
  - repository hooks, install-all hooks and clone hooks;
  - first-run setup (492), cutover, first-run auth and Codex folder trust;
  - all five earlier boundary tests.
- Red on the baseline, compared case by case:
  - `team_launcher_control_repo` 7/3 and `team_launcher_add_role_vcs` 6/2. For
    every red case, each git command run through `run_owner_correct_git` (14,
    29, 15, 4 and 4 commands), its keyword arguments, and where the case
    fails are byte-identical to the baseline.
  - `team_launcher_pane_paths` 9/2.
- CLI help output is byte-identical to the baseline for 7 invocations.
- A staged release contains and loads the module, and the lint is clean on the
  release's own copy of it.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`9c804c7`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 34,987 lines) | 1 (`project_worktrees.py`, 742 lines) |
| Where it sits | line 672, and lines 3914–5212 interleaved with launcher-checkout code | the whole file |
| control-repository/worktree `def`/`class` names in `team_launcher.py` | 31 | 8 (callers in launch and first-run, and `worktree_ref`) |
| `git_*_args` builders in `team_launcher.py` | 30 | 14 (launcher checkout and deploy ref) |
| `grep -c control_repository` in the launcher / the new module | 104 / — | 46 / 68 |

### SYRD-292 (slice 6a): launcher checkout self-update

Re-measured on `9ceed50` before editing: 27 definitions, about 516 lines, in
four regions (lines 619–620, 777, 3948–4561 and 22144). They moved into
**`scripts/launcher_checkout.py`** (632 lines):
- `ALLOW_STALE_LAUNCHER_ENV`, `LEGACY_ALLOW_STALE_LAUNCHER_ENV` and
  `LauncherCheckoutProbe`;
- the **12 launcher-checkout `git_*_args` builders**, with
  `_parse_ahead_behind`, `_format_behind_count` and `_short_head`;
- `probe_launcher_checkout`, `probe_checkout_against_worktree_ref` and
  `launcher_checkout_status`;
- `_auto_fast_forward_launcher_checkout`, `ensure_launcher_checkout_current`
  and `deploy_launcher_checkout`;
- `warn_if_artifact_source_checkout_is_stale`;
- `_launcher_checkout_runner`, and `_owner_correct_git_runner`, the runner
  adapter over the chokepoint whose only caller is `_launcher_checkout_runner`.

**Boundaries.**
- **Into the module.** `team_launcher` imports 22 names explicitly for its
  callers: `launch_project`, `main`, `upgrade_project_command`,
  `_runtime_checkout_copy_status` and `_format_checkout_probe_status`.
  `ensure_launcher_checkout_current` is patched in
  `team_launcher_presentation_test` around launcher call sites that stayed.
  Five private helpers nothing outside reads are no longer launcher
  attributes.
- **Out to the launcher, at call time.** The chokepoint
  (`run_owner_correct_git`, `GitOwnerRule`, `_path_owner_user`), `_repo_root`
  (patched in 3 suites), `worktree_ref`, `_env_truthy_any`,
  `_parse_ls_remote_head` (shared with deploy-ref resolution),
  `_proc_failure_reason` and `shared_switchyard_release_for_path`.
- **Unchanged and still in the launcher.** Host shared-release install and
  upgrade.

**Git ownership lint.**
- `launcher_checkout.py` joined `GIT_LINTED_MODULES`, and the module has no
  findings.
- Mutation-checked:
  - a launcher-checkout builder run through `runner(...)` is reported at
    `launcher_checkout.py:172`, as a third finding beside the launcher's two;
  - dropping the module from the set fails the all-builder-module guard.
- The launcher's two documented baseline findings (the deploy-ref builders)
  are still reported, not masked.

**Evidence.**
- The AST proof holds for 27 definitions, including the bound-name check.
- The new `tests/launcher_checkout_boundary_test.py` has 6 checks. It drives
  `probe_launcher_checkout` through a patched chokepoint and `_repo_root` with
  canned git answers, and 4 of 4 mutations are killed.
- Green and identical to the baseline: cutover, release phase journal,
  read-only status, privileged artifacts, tenant deploy identity, authority
  before deploy, the SYRD-87 deploy probe target, and the six boundary tests.
- Red on the baseline, compared case by case with a fresh `HOME` per run:
  - `team_launcher_freshness` 15/1. For the red case
    (`…uses_owner_runner_when_launcher_user_differs`), the 9 git commands
    through the chokepoint and the failing line are byte-identical to the
    baseline.
  - `team_launcher_resume_detached` 8/1, `team_launcher_presentation` 11/5,
    `team_launcher_pinned_release_resume` 14/1,
    `team_launcher_trusted_migration_artifact` 6/7 and
    `team_launcher_viewer` 21/5. The viewer suite differs only in the hashes
    of its temporary fixture commits.
- CLI help output is byte-identical to the baseline for 5 invocations. The
  `team-launcher` help, including the deploy and stale-launcher options, is
  among them.
- A staged release contains and loads the module, and the lint is clean on the
  release's own copy of it.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`9ceed50`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 34,365 lines) | 1 (`launcher_checkout.py`, 632 lines) |
| launcher-checkout `def`/`class` names in `team_launcher.py` | 20 | 0 |
| `git_*_args` builders in `team_launcher.py` | 14 | 2 (the deploy-ref pair) |

### SYRD-293 (slice 6b): owner-correct git execution and project git helpers

Re-measured on `6f0ccb7` before editing. **`scripts/owner_git.py`** (323
lines) received 13 definitions:
- **Owner-correct execution:**
  - `GitOwnerRule`;
  - `_path_owner_user` and `_path_is_under`;
  - `_git_target_path_from_args`, `_git_owner_for_target` and
    `_git_owner_failure`;
  - **`run_owner_correct_git`**.
- **Project git helpers:** `_run_owner_git`, `_git_status_porcelain`,
  `_ensure_project_git_repository`, `_require_existing_project_git_repository`,
  `_commit_project_git_changes` and `_owner_project_git_runner`.

**Stayed.**
- `_normalized_path`, defined twice (rule 12).
- `_project_config_path_owner_user`, which is about who owns a config file, not
  git execution.
- The deploy-ref builders and their two baseline lint findings.
- `_proc_failure_reason`.

**Boundaries.**
- **The chokepoint seam stays on the launcher.** Suites patch
  `team_launcher.run_owner_correct_git`. `project_worktrees`,
  `launcher_checkout` and `project_onboarding` already call
  `launcher.run_owner_correct_git`, and owner_git's own helpers
  (`_git_status_porcelain`, `_owner_project_git_runner`, `_run_owner_git`) do
  the same. None of them binds the new function directly.
- **The chokepoint's own dependencies stay on the launcher too.**
  `current_user_name` (patched in 27 suites), which decides whether git runs
  directly or through `sudo -u <owner>`, is read from the launcher when it
  runs, as are `_proc_failure_reason` and `_normalized_path`.
- **Into the module.** `team_launcher` imports all 13 names explicitly.
  `_require_existing_project_git_repository`, `_commit_project_git_changes`
  and `_ensure_project_git_repository` are patched only around
  `switchyard_new_command`, which stayed.
- **Lint.** `owner_git.py` joined `GIT_LINTED_MODULES`. It defines no builders,
  but the chokepoint's call sites now live there. A builder run through
  `runner(...)` there is reported at `owner_git.py:120`.

**Evidence.**
- The AST proof holds for 13 definitions, including the bound-name check. The
  launcher's `_normalized_path` still returns a `Path`.
- The new `tests/owner_git_boundary_test.py` has 9 checks, and 6 of 6
  mutations are killed by named checks. The mutations:
  - a top-level launcher import;
  - a helper binding the chokepoint locally;
  - the chokepoint deciding who is running itself;
  - the later `_normalized_path` being lost;
  - an export dropped;
  - a builder bypass (caught by the lint).
- Green and identical to the baseline:
  - cross-account presentation (SYRD-66), desktop policy generation, cutover
    and finish-upgrade;
  - installed-release deploy, tenant deploy identity, the SYRD-87 deploy probe
    target, release bootstrap rollback and authority before deploy;
  - the seven earlier boundary tests.
- Red on the baseline, compared case by case with a fresh `HOME` per run:
  - `team_launcher_freshness` 15/1: its red owner-runner case sends the same 9
    git commands through the chokepoint and fails at the same line.
  - `team_launcher_onboarding_git` 7/3: the same 9, 0 and 0 git commands and
    the same failing lines.
  - `team_launcher_new_project` 18/1 and `desktop_access` 3/1.
  - The git lint is 9/1 on both sides.
- CLI help output is byte-identical to the baseline for 6 invocations.
- A staged release contains and loads the module. `run_owner_correct_git` and
  `GitOwnerRule` are the same objects through the launcher, and the lint is
  clean on the release's own copy.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`6f0ccb7`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 33,826 lines) | 1 (`owner_git.py`, 323 lines) |
| owner-git `def`/`class` names in `team_launcher.py` | 13 | 0 |
| Where it sits | three regions: lines ~3979–4054, ~10049 and ~14243–14421 | the whole file |

### SYRD-294 (slice 6c): pane hooks and Codex hook trust

Re-measured on `37bbb53` before editing: 9 definitions, about 190 lines, in four
regions of `team_launcher.py`. They moved into one module,
**`scripts/pane_hooks.py`** (262 lines), because both parts concern the hooks
that run in a role's pane:
- **Board pane hooks:** `install_generated_project_pane_hooks_args`,
  `ensure_generated_project_pane_hooks`, `tenant_hook_accounts` and
  `refresh_role_pane_hooks`.
- **Codex hook trust:** `CodexHookTrustMismatch`,
  `stale_codex_hook_trust_for_roles`, `_codex_hook_trust_reason`,
  `_codex_hook_trust_affected_roles` and `_format_codex_hook_trust_report`.

**Stayed.**
- The viewer-relayout tmux hooks, which are presentation.
- Repository hooks, which are `scripts/repository_hooks.py`.
- `scripts/ticket_board/codex_hook_trust.py` itself, which does the board-side
  hashing and trust reading.

**Boundaries.**
- **Into the module.** `team_launcher` imports the 7 names its callers and the
  suites read, explicitly; its callers are launch, upgrade and first-run setup.
  `workflow_launcher.py` reaches `launcher.ensure_generated_project_pane_hooks`.
- **Patched names.** `ensure_generated_project_pane_hooks` (2 suites) and
  `refresh_role_pane_hooks` (1 suite) are patched only around launcher call
  sites that stayed, and no moved function calls a patched moved name.
- **Out to the launcher, at call time.** `uid_for_user`, `runtime_dir_for_uid`,
  `current_user_name`, `home_dir_for_user`, `role_run_as_user`,
  `_staged_tooling_dir`, `_is_generated_project_layout_template`,
  `_proc_failure_reason`, `_role_cli_name` and `_role_names`.
- **Library imports.** `codex_command_hook_trust_entries` and
  `codex_trusted_hashes` are imported directly from `codex_hook_trust` under
  the launcher's own `_`-aliases, so the moved text is unchanged. Neither is
  patched.

**Evidence.**
- The AST proof holds for 9 definitions, including the bound-name check. No
  name involved is defined twice.
- The new `tests/pane_hooks_boundary_test.py` has 7 checks. It builds the
  hook-installer argv from the launcher's patched home, uid, runtime dir and
  user, both with `sudo` for another user and without it for the owner, and
  drives the trust check through the launcher's patched CLI lookup. 5 of 5
  mutations are killed. The one that first survived, the installer deciding
  who is running by itself, showed the test lacked the owner case; that case
  was added.
- Green and identical to the baseline:
  - pane hook install, generated-layout upgrade, first-run auth, missing-CLI
    install hint and first-run setup (492);
  - Codex folder trust (53), process authority, project provision and
    cutover;
  - the eight earlier boundary tests.
- Red on the baseline, compared case by case:
  - `team_launcher_layout_upgrade_commands` 12/8 (its own cases write pane
    state, which trips the runner's live-state guard);
  - `team_launcher_env_config` 24/2, `team_launcher_new_project` 18/1 and
    `claude_permission_hook` 22/1.
- **Not counted as coverage:**
  - `team_launcher_declarative_workflow` is identical by whole log, but it
    stops at `unshare --map-auto` on both sides, before it reaches its patched
    `ensure_generated_project_pane_hooks`.
  - `claude_permission_hook`'s red case needs a real Claude, which the suite
    stubs refuse.
  - `ticket_board_hermes_hook_events` skips on both sides, because Hermes is
    not installed.
- CLI help output is byte-identical to the baseline for 5 invocations.
- A staged release contains and loads the module.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`37bbb53`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 33,573 lines) | 1 (`pane_hooks.py`, 262 lines) |
| pane-hook/hook-trust `def`/`class` names in `team_launcher.py` | 9 | 0 |
| Where it sits | four regions, lines ~5857 to ~24260 | the whole file |

### SYRD-295 (slice 7a): agent CLI discovery and host-wide promotion

Re-measured on `501ca76` before editing: 52 definitions, about 1,280 lines, in
one main region (12474–13832) plus 17580–17640. That is above the soft limit,
so it became two modules with a one-way import:

- **`scripts/agent_cli_discovery.py`** (435 lines, 21 definitions) holds:
  - the `AGENT_CLI_SCOPE_*` and `CALLER_*` limits;
  - `InvokingAccount` and `invoking_account`, and the account execute-bit
    checks;
  - the caller's PATH from its process tree, `caller_command_search_path`,
    `caller_executable` and `caller_aware_which`;
  - `AgentCliAvailability`, `agent_cli_binary`, `classify_agent_cli`,
    `classify_selected_agent_clis` and `agent_cli_scope_explanation`;
  - `AgentCliUnavailable`.
- **`scripts/agent_cli_promotion.py`** (1,099 lines, 31 definitions) holds:
  - the policies and `_parse_agent_cli_sources`;
  - source validation (`AgentCliSourceRejected`, reachability by strangers,
    unreachable dependencies, detected-path problems, self-contained,
    `resolve_agent_cli_source`);
  - root promotion through the promoter with its rollout journal, and the
    version check in the tenant's context;
  - the offer before launch;
  - require and owner verification for `new`;
  - refreshing the registered CLIs.

  It imports 9 names from discovery. `AgentCliSourceRejected` subclasses
  `AgentCliUnavailable` at import, and discovery never imports promotion.

**Stayed, and why.**
- **The vendor install table and its text.** `AGENT_CLI_INSTALL_COMMANDS`,
  `host_wide_install_instruction`, `_missing_cli_install_clause` and
  `_format_missing_cli_launch_failure` stay in the launcher, because
  `team_launcher_missing_cli_install_hint_test` is a security guard that scans
  `team_launcher.py` (rule 9). Only those formatters may read the table, and
  none may mention anything that executes. Moving
  `host_wide_install_instruction` would have put a table reader outside the
  guard's view. Promotion calls it through the launcher, and the new boundary
  test fails if any other module ever names the table.
- **`PROC_ROOT`,** patched in 3 suites and shared with `process_uid`. The moved
  process-tree walk reads it from the launcher at call time.
- **`recorded_install_command` and `INSTALL_ROLLOUT_LABEL`,** which belong to
  release bootstrap.
- **First-run auth,** left for 7b and 7c.

**Boundaries.**
- **Into the modules.** `team_launcher` imports 42 names explicitly: 18 from
  discovery and 24 from promotion. No moved name is patched by any suite.
  Ten private helpers nothing outside reads are no longer launcher
  attributes.
- **Out to the launcher, at call time.** `PROC_ROOT`,
  `FIRST_RUN_AUTH_STATUS_COMMANDS`, `DEFAULT_PANE_BASE_PATH`,
  `_run_owner_cli_probe`, `current_user_name`, `_repo_root`, `_role_cli_name`,
  `switchyard_registry_dir`, `switchyard_shared_install_root`,
  `host_wide_install_instruction`, and the launcher-imported
  `untrusted_root_executable_reasons` and `TENANT_CONTROL_OWNER_UID`
  (rule 8).
- No default or decorator names a launcher name, and no member is defined
  twice.

**Evidence.**
- The AST proof holds for 52 definitions, including the bound-name check.
- The new `tests/agent_cli_boundary_test.py` has 12 checks, and 7 of 7
  mutations are killed by named checks, including a second reader of the
  install table planted in promotion. It drives:
  - the caller-PATH walk through a fake `/proc` behind the launcher's patched
    `PROC_ROOT`;
  - `agent_cli_binary` through the patched probe table;
  - the promoter path through the patched shared install root and checkout.
- Green and identical to the baseline:
  - `agent_cli_host_wide`;
  - `agent_cli_privileged_promotion` (92): the real promoter run across a user
    and mount namespace, with a fake `sudo` on PATH and nothing written to the
    host;
  - `caller_cli_discovery` (19) and `caller_cli_discovery_privileged` (3);
  - `tenant_launch_unused_cli` (31);
  - the install-command guard, registry, cutover and first-run setup (492);
  - the nine earlier boundary tests.
- Red on the baseline, compared case by case:
  - `tenant_resume_cli_promotion`, 1 case: identical normalised logs. The
    cause depends on this host: its installed
    `/opt/switchyard/current/scripts/switchyard-promote-agent-cli` is
    root-owned, so the crossing goes on to the test's injected runner, which
    is recording only.
  - `team_launcher_new_project` 18/1, `team_launcher_presentation` 11/5,
    `team_launcher_unprivileged_presentation` 7/4 and
    `team_launcher_desktop_layout_path` 8/4. The last differs only in the
    worktree path.
- CLI help output is byte-identical to the baseline for 6 invocations,
  including `new --help` with its agent-CLI options.
- A staged release contains and loads both modules.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`501ca76`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 33,377 lines) | 2 (`agent_cli_discovery.py` 435, `agent_cli_promotion.py` 1,099) |
| agent-CLI discovery/promotion `def`/`class` names in `team_launcher.py` | 51 by name pattern, of which 13 were unrelated (role-account and provider-account helpers the pattern also matches) | 0; the 13 unrelated ones remain, and the 3 install-text formatters were kept on purpose |
| `grep -c agent_cli` in the launcher / the new modules | 68 / — | 35 / 8 + 54 |

### SYRD-296 (slice 7b): first-run workdir trust and setup manifest

Re-measured on `fede447` before editing: 20 definitions, 351 lines, in five
regions of `team_launcher.py` (12421–12511, 12592, 12947–12958, 14482–14829).
They moved into **`scripts/first_run_setup.py`** (443 lines):
- **The manifest:**
  - the step types (`FirstRunAuthLoginStep`, `FirstRunProviderSetupStep`,
    `FirstRunFolderTrustStep`) and `FirstRunSetupManifest`;
  - `build_first_run_setup_manifest`, `_format_first_run_setup_manifest` and
    `print_first_run_setup_manifest`;
  - `_provider_setup_reason`, `_owner_shell_issue`, `_roles_by_first_cli` and
    `_role_names`.
- **Workdir trust:**
  - `FIRST_RUN_TRUST_CLIS` and `_workdir_is_trusted`;
  - the Claude, agy and Codex probes;
  - the trust-path candidates, `_git_common_dir_for` and
    `_first_run_trust_command`.

**Stayed.** The auth phase stays in the launcher for 7c:
`run_first_run_auth_phase`, `FirstRunAuthReport`, `_cli_auth_status`,
`FIRST_RUN_AUTH_STATUS_COMMANDS`, `FIRST_RUN_AUTH_LOGIN_COMMANDS`,
`FIRST_RUN_SETUP_CLIS`, `_provider_account_setup_complete`,
`_owner_user_cli_reminder`, `_read_json_object`, `OwnerShellIssue`, and the
guarded install-table formatter `_missing_cli_install_clause` (rule 9).

**Boundaries.**
- **Into the module.** `team_launcher` imports 11 names explicitly.
  `role_runtime.py` reaches `launcher._workdir_is_trusted` and
  `FIRST_RUN_TRUST_CLIS`, and `pane_hooks.py` reaches `launcher._role_names`.
  Nine private helpers nothing outside reads are no longer launcher
  attributes.
- **The one routing change.** `_workdir_is_trusted` moved, but
  `team_launcher_first_run_models_test` patches it on the launcher and asserts
  the probe is **not** called for detached roles. A direct call from the moved
  manifest would have made that assertion pass vacuously, so the manifest
  calls `launcher._workdir_is_trusted` (rule 13).
- **Out to the launcher, at call time.** All the auth facilities above, plus
  `_role_cli_name`, and `stale_codex_hook_trust_for_roles` and
  `_format_codex_hook_trust_report`, which the launcher imports from
  `pane_hooks` (rule 8).
- **Source guards checked.** The single-login order guard reads
  `run_first_run_auth_phase`, which stayed. No install-table reader moved.
- No default or decorator names a launcher name, and no member is defined
  twice.

**Evidence.**
- The AST proof holds for 20 definitions, including the bound-name check.
- The new `tests/first_run_setup_boundary_test.py` has 8 checks. It builds a
  real manifest whose login, setup and trust steps are decided by the
  launcher's patched `_cli_auth_status`, `_provider_account_setup_complete`,
  `stale_codex_hook_trust_for_roles` and `_workdir_is_trusted`. 5 of 5
  mutations are killed, including the manifest binding the trust probe
  locally, which the existing suite would not have caught.
- Green and identical to the baseline:
  - Codex folder trust (53), first-run setup (492), first-run models,
    single login (34), trust-step silence (32), login inheritance (82) and
    first-run auth;
  - role runtime, worker-pool preflight (47) and start/status (32);
  - the install-command guard and launch without model probes (22);
  - the ten earlier boundary tests.
- Red on the baseline, compared case by case:
  - `team_launcher_first_run_hermes` 5/1: the red case runs the auth phase,
    and the one manifest it builds, and the line it fails at, are identical
    to the baseline.
  - `team_launcher_env_config` 24/2, `team_launcher_new_project` 18/1 and
    `claude_permission_hook` 22/1.
- CLI help output is identical to the baseline for 5 invocations
  (`validate-models --help` exits 1 on both sides, as recorded in SYRD-290).
- A staged release contains and loads the module.

**Navigation measurement** (counts only; no timing or token claim):

| | Baseline (`fede447`) | After |
|---|---|---|
| Files holding the responsibility | 1 (`team_launcher.py`, 32,030 lines) | 1 (`first_run_setup.py`, 443 lines) |
| manifest/trust `def`/`class` names in `team_launcher.py` (16 by name pattern) | 16 | 0 |
| Where it sits | five regions, lines 12421–14829 | the whole file |
