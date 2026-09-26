# SYRD-272: source size inventory and launcher extraction plan

This document serves the SYRD-272 parent. Each extraction child updates it:
the before/after table, the slice log, and the plan's next entry. SYRD-286 was
the first slice, SYRD-287 the second, SYRD-288 the third and SYRD-289
the fourth.

- Baseline: public main `d5ffdd00a2b162ac9bee545f52ba0840f70fc7ed`. The
  exclusive refactor window opened at this commit.
- SYRD-286 was integrated as `4af050e436123fc1ad7d2aed0034ed771cc5bba9`, which
  is SYRD-287's baseline.
- SYRD-287 was integrated as `95c11f0ae3f8e6b873aa1e42c99a7064558fa2af`, which
  is SYRD-288's baseline.
- SYRD-288 was integrated as `fd85a84a91636b3230fbf238d47c1ae7e24fe957`, which
  is SYRD-289's baseline.
- Soft limit: 1,250 lines, advisory. The pre-commit warning is unchanged.

## 1. Tracked source inventory

These are the tracked non-test files over the soft limit at the baseline,
followed by the parent's list. Each "after" column is that child's candidate.

| File | Baseline | After SYRD-286 | After SYRD-287 | After SYRD-288 | After SYRD-289 | Notes |
|---|---:|---:|---:|---:|---:|---|
| `scripts/team_launcher.py` | 38,527 | 37,808 | 36,670 | 36,379 | 35,730 | Plan in §3. |
| `scripts/worker_pool_command.py` | — | 777 | 777 | 777 | 777 | New in SYRD-286. |
| `scripts/role_credentials.py` | — | — | 832 | 832 | 832 | New in SYRD-287. |
| `scripts/agy_credential.py` | — | — | 484 | 484 | 484 | New in SYRD-287. |
| `scripts/upstream_report.py` | — | — | — | 351 | 351 | New in SYRD-288. |
| `scripts/host_accounts.py` | — | — | — | 23 | 23 | New in SYRD-288: a dependency-free leaf. |
| `scripts/project_onboarding.py` | — | — | — | — | 753 | New in SYRD-289. |
| `scripts/ticket_board/schema.sql` | 12,019 | 12,019 | 12,019 | 12,019 | 12,019 | Proposed exception: one DDL document applied whole. It is still reviewed as its own child. |
| `scripts/ticket_board/project_provision.py` | 4,741 | 4,741 | 4,741 | 4,741 | 4,741 | Parent list; needs a child. |
| `scripts/ticket_board/notify_listener.py` | 4,057 | 4,057 | 4,057 | 4,057 | 4,057 | Parent list; needs a child. |
| `scripts/presentation_controller.py` | 2,632 | 2,632 | 2,632 | 2,632 | 2,632 | Parent list; needs a child. |
| `scripts/ticket_board/app.py` | 2,417 | 2,417 | 2,417 | 2,417 | 2,417 | Parent list; needs a child. |
| `scripts/ticket_board/server.py` | 1,900 | 1,900 | 1,900 | 1,900 | 1,900 | Parent list; needs a child. |
| `scripts/ticket_board/migrations/pgu921_syrd11_declarative_workflow.sql` | 1,773 | 1,773 | 1,773 | 1,773 | 1,773 | Proposed exception: a migration is immutable history. |
| `scripts/ticket_board/frontend_script_core.py` | 1,761 | 1,761 | 1,761 | 1,761 | 1,761 | Parent list; generated front-end asset. Its boundary is the asset, not Python modules. |
| `scripts/ticket_board/write_client.py` | 1,608 | 1,608 | 1,608 | 1,608 | 1,608 | Parent list; needs a child. |
| `scripts/ticket-board-service.sh` | 1,517 | 1,517 | 1,517 | 1,517 | 1,517 | Parent list; a shell entry point. Split along its own subcommands. |
| `scripts/ticket_board/migrations/pgu528_workflow_rbac_config_authoritative.sql` | 1,442 | 1,442 | 1,442 | 1,442 | 1,442 | Proposed exception: migration. |
| `scripts/ticket_board/migrations/pgu589_depersonalize_user_role.sql` | 1,299 | 1,299 | 1,299 | 1,299 | 1,299 | Proposed exception: migration. |
| `deploy/SYRD-87-recover-syrd-runtime.sh` | 1,292 | 1,292 | 1,292 | 1,292 | 1,292 | Proposed exception: a one-off recovery packet kept as a record. |

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
  scans only `team_launcher.py` for `git_*_args` builders. A slice that moves a
  git builder must widen that scan in the same commit.

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
| model/effort/CLI runtime selection | 1,456 | 70 |
| credentials (agy, role seeding, upstream report) — **agy and role seeding moved by SYRD-287; upstream report by SYRD-288** | 1,436 | 65 |
| repository hooks, git and worktrees | 1,196 | 68 |
| CLI parsers and dispatch | 1,157 | 15 |
| board service, listener and status | 1,095 | 54 |
| onboarding docs, prompts and skills — **docs, director onboarding and board skill moved by SYRD-289** | 796 | 32 |
| worker pool — **moved by SYRD-286** | 737 | 19 |
| launcher checkout self-update | 462 | 15 |

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
9. **Keep contractual patch seams.** Suites patch launcher names and expect the
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
| 5 | Model, effort and CLI command construction — **suggested next** | 630 | 11 inbound; re-measure |
| 6 | Repository hooks, git and worktrees; widens the git ownership lint | 1,200 | re-measure |
| 7 | Agent CLI discovery, promotion and first-run auth | 2,600 | several children; re-measure |
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
