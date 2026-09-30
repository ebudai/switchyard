# SYRD-272 final report: oversized source files, before and after

This report finishes the SYRD-272 parent's acceptance. The slice-by-slice
evidence stays in
[`syrd-272-launcher-inventory.md`](syrd-272-launcher-inventory.md). This file
holds the before/after inventory, the reviewed exceptions, a reproducible
navigation comparison and the aggregate preservation record. The Director
decides whether it closes the parent and the exclusive window; this report
does not claim either.

**Trees measured:**
- `d327858b`: the source the parent originally reported on (2026-09-25).
- `d5ffdd00a2b162ac9bee545f52ba0840f70fc7ed`: the exclusive-window baseline.
- `98d454806374513a1506647ff25ee429b51693cc`: the reviewed tree after SYRD-518.

## 1. The nine original targets

| original target | reported d327858 | baseline d5ffdd00 | reviewed 98d45480 | new owner files | new owner lines | largest new owner | children | status |
| --- | ---: | ---: | ---: | ---: | ---: | --- | ---: | --- |
| `scripts/team_launcher.py` | 38,232 | 38,527 | 2,542 | 146 | 45,141 | agent_cli_promotion.py (1,099) | 156 | reviewed exception (SYRD-462) |
| `scripts/ticket_board/project_provision.py` | 4,741 | 4,741 | 1,242 | 12 | 4,598 | provision_path_confinement.py (555) | 12 | under 1,250 |
| `scripts/ticket_board/notify_listener.py` | 4,057 | 4,057 | 1,202 | 8 | 3,412 | pane_activity_gate.py (1,058) | 9 | under 1,250 |
| `scripts/presentation_controller.py` | 2,632 | 2,632 | 1,175 | 6 | 1,625 | presentation_display_session.py (425) | 6 | under 1,250 |
| `scripts/ticket_board/app.py` | 2,414 | 2,417 | 1,685 | 5 | 943 | attachment_store.py (289) | 5 | reviewed exception (SYRD-518) |
| `scripts/ticket_board/server.py` | 1,887 | 1,900 | 1,209 | 4 | 809 | local_peer_authority.py (360) | 4 | under 1,250 |
| `scripts/ticket_board/frontend_script_core.py` | 1,761 | 1,761 | 1,200 | 3 | 609 | frontend_script_attachments.py (285) | 3 | under 1,250 |
| `scripts/ticket_board/write_client.py` | 1,608 | 1,608 | 1,035 | 1 | 600 | write_cli.py (600) | 1 | under 1,250 |
| `scripts/ticket-board-service.sh` | 1,517 | 1,517 | 1,146 | 1 | 391 | ticket-board-service-health.sh (391) | 1 | under 1,250 |
| **nine targets together** | 58,849 | 59,160 | 12,436 | 186 | 58,128 | | 197 | |

"New owner" means a file under `scripts/` that the window added and that was
split out of that target. Each file is attributed through the child ticket
whose commit added it; all 186 added files are attributed. Two targets remain
above the 1,250-line soft limit, each under a reviewed exception (section 3).
No new owner is over the limit: the largest is `scripts/agent_cli_promotion.py`
at 1,099 lines.

**Size moved, it did not disappear.** The nine targets went from 59,160
lines to 12,436. Their 186 new owners hold 58,128, so the same behaviour now
takes 70,564 lines, 11,404 more. The difference is module docstrings,
imports, explicit re-exports and a few kept seams, and it matches the growth
of `scripts/` Python and shell below.

## 2. Tracked source, by kind

| tracked text files | d327858 files / lines | d5ffdd00 files / lines | 98d45480 files / lines | bytes d5ffdd00 → 98d45480 |
| --- | ---: | ---: | ---: | ---: |
| Python under `scripts/` | 84 / 81,664 | 84 / 82,097 | 269 / 93,482 | 3,508,289 → 3,985,555 |
| shell under `scripts/` | 20 / 5,700 | 20 / 5,700 | 21 / 5,720 | 201,255 → 202,387 |
| SQL (schema and migrations) | 132 / 45,187 | 136 / 46,361 | 136 / 46,361 | 2,051,286 → 2,051,286 |
| tests | 404 / 171,998 | 414 / 174,873 | 595 / 253,878 | 7,668,971 → 16,361,638 |
| docs and Markdown | 80 / 18,509 | 85 / 19,215 | 86 / 43,497 | 1,216,998 → 2,865,910 |
| everything else (deploy, config, ...) | 30 / 5,575 | 30 / 5,575 | 30 / 5,575 | 215,519 → 215,519 |

Between `d327858b` and `d5ffdd00`, ordinary backlog work landed before the
window opened. Inside the window every commit is a SYRD-272 slice (section 5).
- **Tests** gained 181 files and about 79,000 lines, and their bytes more
  than doubled. Most of this is per-slice boundary suites with recorded
  expectations. It is real review and maintenance weight, and is reported
  here rather than netted out.
- **Docs** growth is almost all the refactor inventory itself: 24,274 lines.

### Tracked files above 1,250 lines at the reviewed tree

| class | files | notes |
| --- | --- | --- |
| reviewed exceptions (parent targets) | `scripts/team_launcher.py` 2,542; `scripts/ticket_board/app.py` 1,685 | Section 3. |
| database material | `scripts/ticket_board/schema.sql` 12,019; migrations `pgu921_syrd11_declarative_workflow.sql` 1,773, `pgu528_workflow_rbac_config_authoritative.sql` 1,442, `pgu589_depersonalize_user_role.sql` 1,299 | All unchanged since the baseline and not among the nine targets. Migrations are applied history. No exception is claimed; the SYRD-462 note that they are "candidates for their own exception decision" remains a Director decision, not one this report makes. |
| deploy one-off | `deploy/SYRD-87-recover-syrd-runtime.sh` 1,292 | Unchanged since the baseline and not a parent target. No exception is claimed. |
| tests | 18 files, 1,262 to 4,034 lines | 16 were already over at the baseline. Two were new in the window: `tests/upgrade_phases_boundary_test.py` 1,924 and `tests/launch_phases_boundary_test.py` 1,690. The parent targets source files; these are listed, not excepted. |
| documentation | `docs/refactor/syrd-272-launcher-inventory.md` 24,274 (new in the window); `docs/team-launcher.md` 1,761 (unchanged) | Listed, not excepted. |

That is 27 tracked files above 1,250 lines at the reviewed tree, against 31 at
the baseline. No original target is left unaddressed.

## 3. Reviewed exceptions

### `scripts/team_launcher.py`, 2,542 lines: the compatibility facade

The Director accepted this on 2026-09-29, after independent Audit of
SYRD-462 at `cd18475913d0dcee5abd48aa4b3863985cd906ef`. Measured at that
checkpoint and unchanged since:
- **What is left:** 1,487 lines are explicit imports and re-exports, 183
  statements naming 1,212 names, 1,174 of them from 152 Switchyard modules.
  The other 541 lines are 73 small functions, with a median of 5 lines and a
  maximum of 30.
- **Why the names stay:** they are the launcher's public contract. Installed
  entry points (`scripts/team-launcher`, `switchyard`, `scripts/switchyard`,
  `scripts/switchyard-viewer-layout`, `deploy/SYRD-87-recover-syrd-runtime.sh`)
  import from it. Production modules read 873 names through it at call time,
  and tests patch 377 names on it.
- **Alternatives measured and rejected:**
  - Packing the imports to 120 columns cuts only to 619 lines, leaving 1,674,
    and hides every change in long lines.
  - A `__getattr__` or star-import proxy makes the surface implicit and breaks
    the explicit patch interception.
  - Removing the 126 names with no measured consumer is a compatibility
    change, not an extraction, and saves at most 126 lines.
  - Moving the remaining helpers one or two at a time would not reach the
    limit either.

### `scripts/ticket_board/app.py`, 1,685 lines: the PostgreSQL authority and transaction core

The Director accepted this on 2026-09-30 when integrating SYRD-518 at
`98d454806374513a1506647ff25ee429b51693cc`. `TicketBoardApp` is the board's
PostgreSQL front. Of its 46 public methods (measured from the AST, following
calls within the class):
- **27 write with the caller's role set first:** they set the RBAC caller
  role and then write through a transaction or a `ticket_board.*` function
  that decides authority. This group includes `crop_attachment`, lifecycle
  actions, staff moves, publication, workflow and create/update.
- **2 write without a caller role:** `file_report` (the tenant report path,
  authorised by the report token before it reaches the app) and
  `register_runtime_assignment` (the launcher's registration, checked for the
  project and decided by `ticket_board.register_role_runtime`).
- **13 read without setting a role:** snapshot, list, get, the columns,
  workflow configuration and document and roles, runtime assignments and
  targets, publication requests, store signature, and the persisted check.
- **4 never touch the database:** three of the four public attachment
  adapters (`list_screenshots`, `resolve_image` and `save_uploaded_image`,
  which call `attachment_store`) and `published_ref_commit` (which calls
  `commit_cache`). The fourth attachment adapter, `crop_attachment`, is in
  the first group.

The rationale is the transaction and authority core. The create/update path,
about 466 method lines, owns validation, commit and rollback order in one
transaction and uses about twelve app members. The lifecycle commands and
staff moves are public server API entry points of the same connect, role,
call and read-back shape.
- **Alternatives measured in SYRD-518 and rejected:**
  - A ticket-view module (−120 lines) would need reverse delegation into the
    app.
  - Moving the publication proof and the store-signature query (−116) only
    reaches about 1,570 lines.
  - Mixins for the lifecycle commands and staff moves (−267) make a method
    bag, and module functions for them are just wrappers.
  - Splitting out the transaction core (−466) splits commit and rollback
    ownership.
- **Rule going forward:** new non-database responsibilities (file effects,
  formatting, policy, external processes) go to separate owners such as
  `attachment_store`, `commit_cache`, `image_asset_policy`,
  `ticket_input_policy` and `ticket_read_query`, not into this class.

## 4. Navigation and review comparison

**Procedure.** Run `python3 docs/refactor/syrd272_navigation.py` in any
clone with both commits. It reads git objects only; it imports and runs no
project code. For six fixed tasks it resolves:
- seed symbols: the entry point or dispatcher, the handler and the core
  function
- the core's Switchyard callees, two calls deep on both commits alike
  (`SYRD272_DEPTH`, default 2). It follows local definitions, names imported
  from other modules, module aliases such as `provision.X`, `self.` methods,
  and the launcher's and `project_provision`'s re-exports to the real
  definitions, counting each re-export crossed as a hop.
- for shell and JavaScript tasks, the named functions by brace matching

**The tasks:**
1. **launcher:** change what `switchyard release-status <project>` reports.
   Reads `switchyard_main`, the release-status parser and handler, and the
   callees.
2. **provisioning:** change a command the operator packet renders. Reads
   `project_provision.main`, `render_operator_commands` and the callees.
3. **app/attachment:** change a crop's saved filename or metadata. Reads the
   server's `handle_ticket_action`, `TicketBoardApp.crop_attachment` and the
   callees.
4. **CLI:** change how `ticket-board-write add-comment --text-file` reads
   text. Reads the wrapper, the `main` forwarder and the free-text functions.
5. **service:** change what the deploy canary proves. Reads `main`,
   `deploy_restart_service`, `run_release_canary` and its shell callees.
6. **frontend:** change how linked-ticket references render. Reads the seven
   linked-ticket functions, plus `openDetail` and `stateLabel`.

Measured with the script at the two commits (depth 2):

| task | units | unit lines | files | whole-file lines | whole-file bytes | re-export/forwarder hops | locate ms (median of 5) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| launcher: release-status report | 39 → 39 | 1,460 → 1,508 | 2 → 16 | 43,268 → 9,180 | 1,887,284 → 375,592 | 0 → 27 | 214.9 → 246.5 |
| provisioning: operator packet command | 41 → 41 | 1,411 → 1,551 | 1 → 7 | 4,741 → 4,047 | 204,415 → 179,654 | 0 → 33 | 230.5 → 262.8 |
| app/attachment: crop filename or metadata | 15 → 16 | 526 → 547 | 2 → 4 | 4,317 → 3,318 | 202,531 → 154,492 | 0 → 0 | 84.6 → 102.8 |
| CLI: ticket-board-write free text | 6 → 7 | 278 → 285 | 2 → 3 | 1,613 → 1,640 | 68,681 → 69,819 | 0 → 1 | 27.6 → 38.2 |
| service: deploy canary | 12 → 12 | 277 → 277 | 1 → 2 | 1,517 → 1,537 | 60,393 → 61,525 | 0 → 1 | 70.0 → 77.5 |
| frontend: linked-ticket references | 9 → 9 | 133 → 133 | 1 → 2 | 1,761 → 1,341 | 64,083 → 49,707 | 0 → 1 | 48.1 → 56.5 |


**Reading the results:**
- **Focused reading is flat or slightly larger.** The same functions are
  read, since behaviour was moved, not rewritten. Wrappers and forwarders add
  a few lines: +3.3% launcher, +2.5% CLI, +4.0% app/attachment and +9.9%
  provisioning.
- **Whole-file context shrinks where the domain was large:**
  - launcher: 43,268 → 9,180 lines, and 1.89 MB → 0.38 MB
  - provisioning: 4,741 → 4,047 lines
  - app/attachment: 4,317 → 3,318 lines
  - frontend: 1,761 → 1,341 lines

  For CLI and service it is unchanged or slightly larger, because those tasks
  now span both halves of a split.
- **More files and more hops.** The same reading now spans more files, and
  crosses facade re-exports (27 for the launcher task, 33 for provisioning)
  or one forwarder or `source`. That is a real navigation cost: a reader must
  follow `launcher.X` or `provision.X` to the owner.
- **The locate timings are mechanical.** They are milliseconds of
  `git grep` over the whole tree, slightly higher after the split, and say
  nothing about human effort. They vary by a few milliseconds between runs;
  a rerun gave 219.6 → 248.7 for the launcher task. Every other column
  reproduced exactly.

**Limits.** This measures reading scope for six chosen tasks, with one
fixed resolution rule. It does not measure real ticket turnaround, reviewer
time or model token use, and none of those should be inferred from it. The
tasks were chosen after the fact to cover each kind of target, not sampled
from real tickets. Unit lines count whole function definitions, and include
large dispatchers (`switchyard_main`, `handle_ticket_action`) a reader would
often skim. Whole-file lines model an agent or reviewer that loads whole
files.

## 5. Preservation evidence

- **All accounted for.** The board records 197 SYRD-272 children, all done
  and Audit-signed:

  | target | children |
  | --- | --- |
  | launcher | 156, including three preservation follow-ups |
  | project_provision | 12 |
  | notify_listener | 9 |
  | presentation_controller | 6 |
  | app | 5 |
  | server | 4 |
  | frontend core | 3 |
  | write_client | 1 |
  | board service | 1 |

  196 recorded an exact commit, and every one is an ancestor of `98d45480`
  inside the window. SYRD-488 was a design-only slice with no commit. The
  window's 197 commits are those 196 plus `f238c22f`, the extraction half of
  SYRD-492's reviewed pair, whose recorded SHA `55d4e03c` corrected one
  inventory line. Audit reviewed both commits. No commit in the window is
  outside SYRD-272.
- **Each slice** was reviewed at its exact SHA by independent Audit and
  integrated by the Director before the next started. Its slice-log entry
  records the focused tests, the mutation checks, the entry-point smoke and
  the checks that could not run.
- **Three preservation follow-ups** repaired behaviour a slice had changed:
  - SYRD-324 restored a role-migration local import binding.
  - SYRD-386 repaired a moved release-default guard.
  - SYRD-393 closed role-state root descriptors on an early return.
- **Known limits**, recorded per slice and not claimed as passing:
  - **Browser suites** need Playwright and a browser, and did not run (for
    example `attachment_crop_browser_test`, `file_attach_frontend_browser_test`,
    `ticket_board_ui_audit_test`, `resolved_blockers_hidden_frontend_test`).
  - **Privileged or nested-namespace suites** could not run (for example
    `legacy_release_root_repair_test`, and `ticket_board_release_mode_test`
    inside a sandbox).
  - **Suites red on main before their slice** were compared with baseline
    output or per case, rather than reported as passing. In the last three
    slices: `ticket_board_postgres_triggers_test` and
    `ticket_board_listener_pane_state_authority_test` at the listener's
    `listen_once`, `ticket_board_schema_test`, the env-widening case of
    `ticket_board_write_api_test`, `merge_gate_helper_test`'s stale-worktree
    case, and the pgu-era service shell suites.
- **Nothing was rerun for this report.** It adds documentation and one
  read-only measurement script, and changes no source, test or installed
  path.

## Reproducing the numbers

    # sections 1-2: file sizes at the three trees
    for c in d327858b d5ffdd00a2b162ac9bee545f52ba0840f70fc7ed 98d454806374513a1506647ff25ee429b51693cc; do
        git show "$c:scripts/team_launcher.py" | wc -l    # repeat per target
    done
    git diff --name-only --diff-filter=A d5ffdd00a2b162ac9bee545f52ba0840f70fc7ed 98d454806374513a1506647ff25ee429b51693cc -- scripts | wc -l
    # section 4: the navigation comparison (JSON, including every file:lines read)
    python3 docs/refactor/syrd272_navigation.py
    SYRD272_DEPTH=1 python3 docs/refactor/syrd272_navigation.py   # sensitivity check

At depth 1, the unit and line counts differ from depth 2 because the crop
task's helpers sit one call deeper after the split. The whole-file and hop
pattern is the same.
