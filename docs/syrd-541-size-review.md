# SYRD-541: oversized growth is reviewed before integration

The user's approved policy, from 2026-10-03:
- 1,250 physical lines stays a **soft** limit, and commits stay warning-only.
- Warnings alone did not keep files near the limit. Growth past it now needs
  **review before integration**: a cohesive split, or a bounded, attributed
  Director exception.

## Where it is enforced, and where it is not

Switchyard's `main` lives on the forge, and the Director merges and pushes it by
hand (`docs/ticket-board-service.md`, "Integration"). The program that once
gated that push, `switchyard-integrate-main`, is retired. No Switchyard hook sees
the push.

The step Switchyard does hold is the **board**. Once the Director has enabled
the review:
- **Audit's approval is the checkpoint before integration.** It needs the
  board's own completed scan of the exact candidate, made for that action, and
  no unresolved size finding.
- **`mark_done` is a later backstop.** It applies the same test to the
  integration commit it records, but it comes after the push.

**The supported Director procedure:**
1. Push `main` only with a candidate Audit approved under the size review.
2. If integration changed the tree or base (a cherry-pick, a merge, a rebase),
   first publish the integration commit where the board can see it, for
   example with `switchyard-publish-candidate`. Then run
   `ticket-board-write measure-size <TICKET> --commit <sha>`, which measures it
   and moves nothing, and resolve any finding before pushing.
3. Close with `mark_done` on that commit.

The board cannot stop a raw push to the forge, and this document does not claim
it does; only forge branch protection can.

The gate is `ticket_board.enforce_size_review`. It is its own `BEFORE UPDATE`
trigger, and it touches nothing the workflow guard decides. It gates:
- a declared `approve`, matched on the action and the stage it leaves (never
  on the declared destination, which a gate's skip can pass);
- a `require_commit` move into a terminal stage (`mark_done`; never `cancel`).

What stays as it is:
- the pre-commit hook, which remains warning-only;
- a hand-push to the forge, which only forge branch protection can stop; this
  change does not claim to;
- narrated `force_move`/`override_move` recovery;
- every other gate: provenance, exact-SHA Audit, user gates and blockers.

There is no global cap and no new bypass.

## Measurement (`scripts/ticket_board/file_size_policy.py`)

One module, with no board imports, used by the board and by the pre-commit
warning, so they cannot disagree.
- **What it measures:** git blobs at exact commits, never a working tree.
  Physical lines, from `splitlines` on a UTF-8 decode with replacement
  characters. Binary blobs, detected by a NUL byte, have no lines.
- **Against what:** a candidate is measured against its merge-base with the
  board's `main`, in the commit cache the board already resolves submissions
  in. The integration commit at `mark_done`:
  - is the scan already recorded, if it is the audited candidate itself;
  - otherwise (a cherry-pick or a merge) is measured against its first parent,
    which is `main` just before it.
  - **Limit:** if a ticket lands as several separate commits, only the last
    one's growth is measured at the close. Each was still reviewed as part of
    the candidate.
- **Identity:** the path, plus git rename detection (`-M`), so a renamed file
  keeps its size history and its allowance.
  - A deletion is never a finding.
  - A new file is measured from 0, so a new file over the limit is a crossing.
  - A copy is a new file.

**Scope**, decided in one table in the module:

| Class | Handling |
|---|---|
| Source, scripts, JS, SQL, extensionless executables | in scope, gated on growth |
| Tests | in scope, gated on growth |
| `scripts/ticket_board/schema.sql` | **mirror, by proof.** It carries the newest copy of every migration's functions. A block it adds (a run of added lines) is excused only when its SQL lines occur contiguously, in order, among the lines the candidate's own migrations add, consuming that occurrence. The rest of its growth is judged like any file's, against its allowance and any Director ceiling. Its full before/after is always reported. See the Audit/Director correction below |
| An applied migration | history: never rewritten to meet a number, and never grows, because changing one is already refused. A **new** migration is measured like any file |
| `docs/`, `*.md` | out of scope: prose |
| `external/`, `third_party/` | out of scope: vendored |
| Binary | no lines |

**Bands:**
- **1,100 lines or more:** warn. Reported, never gated.
- **Over 1,250:** review, gated **only on growth the candidate itself made**.
  - A file's **allowance** is the larger of its size at the base and its
    reviewed ceiling.
  - A **crossing** takes a file from 1,250 or fewer to more than 1,250, with
    no approval above 1,250.
  - **Growth** takes a file already over 1,250 past its allowance.
  - Shrinking, holding steady, or staying within the allowance needs nothing.
    So historical debt never stops unrelated work, merge-base movement is
    nobody's growth, and a reduction never needs a new review.

## Director correction (after Audit of f2e2767)

The Director's independent probes found two defects. Both are fixed, and each
is pinned by tests.

**1. No-code reviews.** Once enabled, the gate demanded a commit from every
approval, so a valid commit-exempt, no-code review failed. That reopened
SYRD-267's no-code failure. Now:
- with no commit, a `commit_exempt` ticket is decided by its other gates
  alone, as before the review existed;
- without the exemption, a missing commit is refused;
- an exempt ticket that does carry a commit is measured like any other.

The exemption is the ticket's existing flag; nothing new is added.

**2. The schema mirror.** Line-set membership let one migration line excuse a
hundred copies, and the mirror branch ignored Director ceilings, so an approved
schema finding reopened forever. Now the proof is by block, with occurrence
accounting:
- **Blocks:** a run of lines the candidate adds to `schema.sql` is excused when
  its SQL lines (comment-only and blank lines set aside) occur contiguously and
  in order among one block of lines the candidate's own migrations add.
- **Occurrences:** each occurrence excuses one copy.
- **What proves nothing:** reordered lines, partial blocks, and historical
  migrations, even ones the candidate touches.
- **Annotations:** a proved block's annotations are excused with it, and an
  unproved block's comments count.
- **Allowance:** the file is then judged on its size less the proved lines,
  against max(base, ceiling), as every file is. So a Director's exception at
  the measured size holds on every fresh rescan, growth past it is refused, and
  replacing lines without growing is never a finding.

Checked against real history, the proof clears `schema.sql` completely for
SYRD-537 (581 lines), SYRD-538 (60), SYRD-539 (473, including its header
comment) and this ticket.

**New tests:**
- a commit-exempt no-code review, approved before and after enabling, also run
  on main;
- a missing commit without the exemption, refused;
- an exempt ticket that carries growing code, refused;
- the Director's hundred-copies probe;
- reordered lines;
- an unchanged and a touched historical migration;
- a run holding two copies, and two separate copies of a block held once;
- the same SQL annotated differently;
- same-size replacement;
- on a real board, unproved schema growth: refused, excepted at its measured
  size (standing), passing on a fresh scan elsewhere, and refused one line
  past the ceiling.

**Mutation of the correction:** 10 mutants, all killed on assertion lines:
- the exemption removed;
- any missing commit passing;
- an exempt ticket with a commit passing;
- occurrences not consumed;
- line-membership proof;
- order ignored;
- historical migration text as proof;
- the ceiling ignored;
- the proof ignored;
- annotations required to match. This one first survived; the
  differently-annotated case kills it.

The full sweep and the earlier 34 mutants were not repeated, as directed.
Their code is unchanged apart from these two areas.

**Migration identity:** SYRD-540's candidate also used pgu973, so this one is
now **pgu974**, keeping the two unique whichever lands first. Main was told on
SYRD-540.

## Durable records (migration pgu974, `schema.sql` identical; grants in both and in `rbac.sql`)

- **`size_review_policy`:** whether review is enabled; who enabled it, and
  when; the baseline commit; and the baseline **inventory**, which is retained
  debt, recorded and not approved.
- **`size_exceptions`:**
  - the path;
  - the **ceiling**, the reviewed maximum, always the measured size of the
    reviewed candidate;
  - the exact `reviewed_commit` and `reviewed_base`;
  - the rationale;
  - the approving role;
  - the scope: this ticket, or **standing** for the file on later tickets;
  - its state: active, or superseded by a later one for the same scope.

  Only `approve_size_exception` and the enablement's carried list write it.
  Candidate-authored files and comments cannot.
- **`size_findings`:** one per ticket and file, kept across candidates. A new
  candidate updates it in place, with an incremented revision.
  - **Resolved** when the growth is gone (split or reduced).
  - **Excepted** when the Director approves an exception.
  - **Reopened** when a later candidate goes past the allowance again.
  - A failed scan is a single `*` finding with reason `scan_failed`.
- **`size_scans`:** the latest scan of each ticket's candidate. Its report,
  every changed file at or above 1,100 lines with before, after, growth, band,
  ceiling and finding, is the **packet**.

**Decisions:**
- **Stale approvals:** an exception can only be approved for the ticket's
  current candidate. A finding measured on an earlier candidate must be
  measured again.
- **Scanner failure** fails closed, but only for the gated transitions. Only a
  successful rescan clears it, never an exception, so an exception cannot be a
  bypass.
- **The scan must be authoritative and current.** The gate accepts only the
  board's own completed scan of the exact commit, made within the last 10
  minutes. The board measures immediately before every gated action, and the
  close measures again rather than reusing an earlier scan. So a stale or
  concurrent earlier scan, or a path that skips the measurement, is refused:
  "the measurement of … is from …, not this action".
- **Only the service can record a scan.** No role login can execute
  `record_size_scan`; the grants test shows this.
- **An unmeasured commit,** for instance one the cache cannot resolve, is
  refused: "has not been measured".

## The flow

1. **Implementer:** the pre-commit hook warns on what is staged.
   - A note from 1,100 lines.
   - Over 1,250, the existing warning line, unchanged, plus a note with the
     file's size at HEAD and the growth.
   - The board measures the candidate when it is submitted with its commit, or
     when it reaches a gated transition. Open findings are on the ticket: in
     its JSON as `size_review`, and in the ticket view as "Size review: …".
   - The finding names what resolves it: a split or reduction (every piece at
     1,250 or under, or within its allowance), or a Director exception. The
     implementer owns the split.
2. **Audit:** approving a candidate with an unresolved finding is refused, and
   the refusal names each file, before → after. Audit cannot treat the warning
   as approval. The size evidence is in the ticket's packet.
3. **Director:**
   - **Approve:** `ticket-board-write approve-size-exception <TICKET> --path <file> --rationale "..." [--standing] [--apply]`.
     It previews unless `--apply`. The ceiling is the measured size, never a
     number chosen freely. The decision is posted on the ticket with the
     reviewed commit and base.
   - **Refactor work:** if a split should be its own work, the Director files
     it as an ordinary ticket. Nothing is filed automatically.
   - **Authority:** the integrator's (`merge`), through
     `COMPOSED_OPERATION_CAPABILITIES`. App and Ops are refused at the board
     and in SQL.
4. **The close:** `mark_done` measures the integration commit, as above.

**Notices.** When a finding opens or reopens, the ticket's owner gets one
in-place update notice, through the collapsing `ticket_update` key, and only
when someone else's action opened it. Unchanged debt, a re-measure that
changes nothing, and a role's own refused action wake nobody. No ticket is
created per commit, and nothing is suppressed per path forever: the old
reporter's forever-per-path markers are not used.

## Enabling it (Director)

```bash
ticket-board-write enable-size-review            # preview: baseline commit and inventory
ticket-board-write enable-size-review --carried-file docs/size-review/syrd-carried-exceptions.json --apply
```

The carried file holds the two SYRD-272 reviewed exceptions with their reviewed
ceilings, commits and rationales, measured at their reviewed commits:
- `team_launcher.py`: 2,542 at `cd18475`;
- `app.py`: 1,685 at `98d4548`.

Both files are larger today: 2,549 and 1,766. That excess was never renewed,
and the rationale records it. It does not block a candidate that does not grow
those files further, because the allowance is never below a file's size at the
base. Any further growth needs a renewed decision. Passing the file is the
Director's act; the file itself authorizes nothing.

## Inventory at enablement

Measured with `file_size_policy.inventory` at main `7c573c71f15bfe7f53e9f18acb1cc173d3703b7c` (in-scope files at or above 1100 lines).

| lines | file | class |
|---:|---|---|
| 13265 | `scripts/ticket_board/schema.sql` | mirror (reported, never gated) -- over the review limit |
| 4034 | `tests/ticket_board_postgres_triggers_test.py` | test (gated on growth) -- over the review limit |
| 3851 | `tests/ticket_board_notify_listener_test.py` | test (gated on growth) -- over the review limit |
| 2920 | `tests/first_run_setup_completion_test.py` | test (gated on growth) -- over the review limit |
| 2549 | `scripts/team_launcher.py` | source (gated on growth) -- over the review limit |
| 2470 | `tests/ticket_board_pane_hooks_install_test.py` | test (gated on growth) -- over the review limit |
| 2264 | `tests/ticket_board_write_api_test.py` | test (gated on growth) -- over the review limit |
| 1924 | `tests/upgrade_phases_boundary_test.py` | test (gated on growth) -- over the review limit |
| 1804 | `tests/ticket_board_rbac_test.py` | test (gated on growth) -- over the review limit |
| 1773 | `scripts/ticket_board/migrations/pgu921_syrd11_declarative_workflow.sql` | applied migration (history; never rewritten) -- over the review limit |
| 1766 | `scripts/ticket_board/app.py` | source (gated on growth) -- over the review limit |
| 1690 | `tests/launch_phases_boundary_test.py` | test (gated on growth) -- over the review limit |
| 1655 | `tests/legacy_presentation_launch_test.py` | test (gated on growth) -- over the review limit |
| 1584 | `tests/team_launcher_project_precheck_test.py` | test (gated on growth) -- over the review limit |
| 1570 | `tests/team_launcher_new_project_test.py` | test (gated on growth) -- over the review limit |
| 1442 | `scripts/ticket_board/migrations/pgu528_workflow_rbac_config_authoritative.sql` | applied migration (history; never rewritten) -- over the review limit |
| 1415 | `scripts/ticket_board/migrations/pgu970_syrd537_reminder_snooze.sql` | applied migration (history; never rewritten) -- over the review limit |
| 1414 | `tests/team_launcher_upgrade_cutover_test.py` | test (gated on growth) -- over the review limit |
| 1393 | `tests/team_launcher_viewer_test.py` | test (gated on growth) -- over the review limit |
| 1360 | `tests/legacy_presentation_migration_test.py` | test (gated on growth) -- over the review limit |
| 1355 | `tests/tenant_resume_presentation_handoff_test.py` | test (gated on growth) -- over the review limit |
| 1337 | `tests/team_launcher_presentation_test.py` | test (gated on growth) -- over the review limit |
| 1325 | `tests/team_launcher_test_helpers.py` | test (gated on growth) -- over the review limit |
| 1299 | `scripts/ticket_board/migrations/pgu589_depersonalize_user_role.sql` | applied migration (history; never rewritten) -- over the review limit |
| 1292 | `deploy/SYRD-87-recover-syrd-runtime.sh` | source (gated on growth) -- over the review limit |
| 1262 | `tests/ticket_board_project_workflow_provision_test.py` | test (gated on growth) -- over the review limit |
| 1249 | `scripts/ticket_board/server.py` | source (gated on growth) |
| 1248 | `scripts/ticket_board/notify_listener.py` | source (gated on growth) |
| 1242 | `scripts/ticket_board/project_provision.py` | source (gated on growth) |
| 1233 | `scripts/worker_pool.py` | source (gated on growth) |
| 1209 | `tests/tenant_control_bridge_e2e_test.py` | test (gated on growth) |
| 1206 | `scripts/ticket_board/frontend_style.py` | source (gated on growth) |
| 1200 | `scripts/ticket_board/frontend_script_core.py` | source (gated on growth) |
| 1175 | `scripts/presentation_controller.py` | source (gated on growth) |
| 1161 | `scripts/ticket-board-service.sh` | source (gated on growth) |
| 1155 | `scripts/ticket_board/workflow_config.py` | source (gated on growth) |
| 1148 | `tests/ticket_board_declarative_workflow_test.py` | test (gated on growth) |
| 1140 | `scripts/ticket_board/write_client.py` | source (gated on growth) |
| 1126 | `scripts/role_runtime.py` | source (gated on growth) |
| 1126 | `scripts/upgrade_phases.py` | source (gated on growth) |
| 1123 | `scripts/ticket_board/publication_boundary.py` | source (gated on growth) |
| 1117 | `tests/presentation_window_recovery_test.py` | test (gated on growth) |
| 1109 | `tests/ticket_board_project_provision_test.py` | test (gated on growth) |

The old scanner covered only `.cpp .cu .fish .h .hpp .py .sh`. Tests, SQL and
JS are now in scope, so they appear here; none is excluded without a decision
recorded above.

## Evidence

### `tests/size_review_test.py`: 57 checks

The suite runs on a production-built board: companion roles, `schema.sql`, the
real `ticket-board-migrate`, then `rbac.sql`, with a declared workflow. The
board's commit cache is a disposable repository. It drives the real write CLI
against the real board server over its Unix socket, and runs the real
pre-commit helper.

**Main, 7c573c7, run in a child from a git archive:** Audit approves a
candidate that takes one file over 1,250 lines and grows another already over
it, and there is no review to enable.

**This tree.**

*Defaults and enabling:*
- until enabled, nothing is gated;
- the enablement preview names the inventory and writes nothing;
- only the Director enables it, and App is refused by the board.

*The gate:*
- every declared approval is refused while a finding is open: Inspection's,
  Audit's and the User's. Each names the findings before and after, and the
  ticket stays in review. This covers the approval whose declared destination
  is skipped past;
- a stale scan, used by a path that bypasses the board's measurement, is
  refused, while the board's own path measures again and succeeds;
- the findings survive the refused transaction;
- the packet is on the ticket;
- re-measuring changes nothing.

*Exceptions:*
- App and Ops are refused at the board, and App by the database for both
  operations;
- an exception needs an open finding, is bounded to the measured size, and
  previews without writing;
- once every finding is excepted, the approval goes through, and the decision
  is attributed on the ticket.

*The close:*
- the Director's `measure-size` reports the integration commit's growth before
  the push, and moves nothing; App is refused;
- `mark_done` on a cherry-pick that adds growth is refused;
- `mark_done` on the audited candidate itself succeeds.

*Allowances:*
- a standing exception is the file's allowance elsewhere, and one line past it
  is a renewed review;
- a ticket-scoped one does not travel;
- one finding per file is updated in place, an approval of a stale measurement
  is refused, a finding past its exception reopens, and a reduction resolves
  it.

*Not findings:*
- unchanged or shrinking debt, deletions, and the 1,100 band, which is reported
  but not gated;
- a rename keeps its history and allowance;
- a new file over 1,250 is a crossing in any language;
- `schema.sql` lines that the candidate's own migration carries are not a
  finding, but schema-only lines are;
- `docs/` (Markdown or not) and vendored code are out of scope.

*Failures and other paths:*
- a failed scan fails closed, cannot be excepted, and clears on a successful
  rescan;
- an unknown full SHA is a failed scan on the ticket, and an unresolvable
  commit is refused as unmeasured;
- a kick-back and a cancel are not gated;
- a commit-carrying submission is measured;
- the recovery windows are excluded, and only they: `force_move`,
  `override_move`, and a merge's close of its source. The merge target keeps
  its own gate.

*Schema, grants and view:*
- a fresh `schema.sql` board installs the same functions and tables as the
  migrated board, and grants are as intended on both;
- the ticket view renders the size line, run under node, and the card does not.

*The commit-time hook:*
- it measures the staged blob, not the working tree, with the size at HEAD;
- a note from 1,100 lines;
- quiet under 1,100, and for prose.

It passes under `env -i` and in a role pane.

### Mutation

Mutation ran through `tests/bounded_run.py mutate`, in throwaway worktrees, on
the reworked tree. 34 mutants: 33 are killed, each read on its assertion line,
and one is equivalent.

**Killed:**
- the gate: enablement, destination matching, the scan requirement, the
  freshness rule, open findings, the close, cancel;
- the scan's resolve and reopen;
- the exception: stale approval, failed scan, actor check, the freely chosen
  ceiling, scope, standing versus ticket order;
- enablement's actor check;
- the allowance rule;
- the mirror, in three variants: gated wholesale, every line counted as
  mirrored, and no line counted as mirrored;
- the scope prefixes, rename detection;
- measuring the close against main, submission scans, the rename allowance;
- `measure-size` recording nothing;
- the server's measurement call, the board policy map;
- the hook measuring the working tree, the missing warn band;
- the view line, the read column, the JSON field.

Where the bounded runner's tail cut the assertion prefix (S7, P8), the kill was
reproduced by hand and the assertion line read.

**Equivalent, P17:** the close measuring the audited candidate against its
first parent instead of the audited base. The candidate already passed the same
gate at Audit, so undercounting can only miss growth Audit already judged; no
gate outcome can differ. It is reported, not counted as a kill.

**Earlier rounds:**
- S14 and P8 first hit a fixture `IndexError`;
- P3 first survived, because `.md` was excluded by its suffix as well as
  `docs/`, so a `docs/*.txt` case was added;
- S8's first version was unbalanced SQL.

**Not mutated:** the service-only check inside `record_size_scan`. It is
redundant with that function's grant: only the service may execute it.

### Focused sweep: 115 existing suites, this tree vs 7c573c7

96 pass on both trees and 15 fail on both. The remaining differences:
- the two known "applies last" lists, which now name pgu974;
- `relayed_user_acceptance_test`, which failed on the baseline run and passes
  here.

**Adapted, each to an intended change:**
- the role-policy count and map;
- the read-query hashes, with the fake connection's probe extended;
- `pull_claim_postgres_test`'s upgrade shape, which now applies the migration
  tail;
- `repository_policy_install_test`'s "1,250 is silent" pin, which is now "no
  warning, a note";
- `provision_role_tooling_boundary_test`'s eight records, each mechanically
  proven to differ only by the new helper.

`install-inspector-git-guard.sh` never copied `report_file_size_limit.py`, so
that warning failed on import. It now copies both modules.

## What this change itself measures

- `server.py` is at 1,250 (in the warn band, not over the limit). The SYRD-537
  dispatch became a small registry, `extension_operations.py`, so the new
  operations cost one line.
- `app.py` has a net change of zero: one import name, and the snooze field line
  became the registry's field mapping.
- The new modules are small: `file_size_policy.py`, `size_review.py`,
  `extension_operations.py`, and the test.
