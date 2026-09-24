# SYRD-239 — `recover-display` has to be a command, not only a word

## What the live UAT found

From the operator's ordinary desktop shell:

```
$ switchyard recover-display mefp
switchyard: unknown project 'recover-display mefp'
```

No Director display was recovered. The first candidate (`7747330`) made the
verb show up in four places: the disconnected Director slot printed it,
`SWITCHYARD_COMMANDS` listed it, `TENANT_CONTROL_OPERATIONS` routed it, and the
bridge mapped it to `present <slug> recover director`. But `switchyard_main`
had no branch for it, so the verb fell through to bare project selection and
the whole of `recover-display mefp` was read as a project name.

Nothing caught this, because every existing test entered *after* the missing
piece. `operator_display_recovery_test` checked that the verb was listed and
that the routing helper answered correctly. `tenant_control_bridge_e2e_test`
started at the bridge. None of them ran the operator's own `switchyard`.

## A second defect behind the first

Adding the branch alone would still have failed the UAT. After the bridge
answers 0, `_switchyard_exec_through_tenant_control` calls
`complete_desktop_presentation` for every verb except `stop`, to open whatever
window the owner half handed back. A recovery hands nothing back: it reattaches
the Director inside the window that is already open. On a tenant with desktop
access, "no handoff" is reported as a failure:

```
switchyard: mefp's panes are running, but no presentation window was handed
back to this session, so none was opened ... Run `switchyard mefp` again
```

So a *successful* recovery would have exited 1 and told the operator it had not
worked.

## The change

- **Dispatch.** `switchyard_main` routes `recover-display` to
  `switchyard_recover_display_command`. It resolves the project and loads its
  configuration, which crosses to the owner over the tenant-control bridge in
  the same way `stop` and `status` do. The bridge then runs the fixed
  `present <slug> recover director` as the owner. The verb carries no authority
  of its own; it only chooses the route.
- **The slug crosses, not what was typed.** The bridge serves only
  `<verb> <slug>`, so a project display name in the crossing argv would
  silently fall back to sudo.
- **No window completion after a recovery.** The near side says
  "`<project>`'s Director display was recovered" and exits 0.
- **Without a crossing** (the caller is the owner, or root), no new authority is
  granted. The controller's own check still decides. Its refusal, though, would
  have told the reader to run `switchyard recover-display`, which is the
  command they had just run. So the refusal now says what actually happened: the
  caller did not arrive over the bridge, and the project is registered to a
  named operator.
- `recover-display` is in `switchyard --help`, and the README block is
  regenerated from that help text.

Unchanged from Main's candidate: the bridge mapping, operator authentication
against the root-owned grant, the restriction to the Director slot, ordinary
presentation changes staying Director-only, and `operator:<name>` provenance.

## Evidence

**End to end, from the operator's public entry point.** This is a new case in
`tenant_control_bridge_e2e_test`, using its user-namespace chroot, where
root-owned paths and three distinct accounts are real. The steps:

1. The tenant is staged the production way.
2. The human runs the real `scripts/switchyard recover-display demo`,
   unprivileged, with `SWITCHYARD_SUDO_BIN` recording what it asks sudo for.
3. It must ask for exactly
   `-n /usr/local/lib/switchyard/demo/switchyard-tenant-control demo recover-display`.
4. The harness then takes the one hop the human cannot take: it runs that exact
   recorded request as root, with `SUDO_UID` set, as sudo would.
5. The owner's real `switchyard_main` must receive
   `present demo recover director`, as the owner uid, with the human as the
   bridge caller.
6. The **real** `_require_director` must admit it as `operator:<human>`.

The same case also checks these refusals and non-escalations:

- the trampoline does not ask the human for a password, but does ask the
  intruder;
- the intruder is refused before reaching sudo;
- another project is refused before reaching sudo;
- the bridge still runs no ordinary `present`.

**Red on main, green here.** The same test file run against `origin/main`
fails with the live message, `switchyard: unknown project 'recover-display
demo'`.

**Focused (dispatch part):** `operator_display_recovery_test`, 55 checks at this stage (13 new; 72 with the refusal below). Its
changes:

- The public dispatcher is actually executed.
- A recovery on a desktop tenant exits 0 and opens no window. The control case
  is a `start` on the same seam, which still completes its window and still
  reports one that never came.
- The no-crossing refusal names the bridge and the operator and does not send
  the caller back to the same command.
- The Director's own pane and the bridge-named operator still reach the same
  fixed recovery, while an unmatched operator name is refused.

**Mutation (dispatch part): 7/7 killed.** The mutants:

- no dispatch branch, killed with the live message;
- a recovery demands a window;
- no in-process check;
- the typed selection crosses instead of the slug;
- any claimed operator name is believed;
- the in-process path recovers another slot;
- the help omits the verb.

**Sweep:** 31 related suites give identical pass/fail on this branch and on a
clean `origin/main` (`453b55a`). The 12 suites that are red on both were
compared case by case: 198 cases on each tree, all identical except
`test_readme_names_install_path_and_help_text_cannot_drift`, which goes from
fail to pass.

### Two environment notes on the e2e suite

- **The pane's worktrees are not world-readable.** Files are checked out
  `0750`, so the suite's unprivileged accounts cannot import the repository and
  it fails on main too. It was run from `0755` clones under `/tmp` instead.
- **Some cases fail on both trees.** From those clones, two existing cases fail
  identically on main and here:
  `case_all_three_verbs_run_through_the_real_public_dispatcher` (`start`
  returns 1) and `case_the_bridge_returns_one_validated_handoff_to_its_caller`.
  Because the suite stops at its first failure, all 12 cases were also run one
  at a time on both trees: 11 are identical, and the new case passes.

## The third live failure: a live worker the board will not name

With the dispatch in place, the bridge reached the owner and `present` refused:

```
switchyard: no live runtime assignment for configured role(s): director, ops
```

This was measured read-only, using HTTP GETs on mefp's board, `ps`, and the
root rollout journal:

- mefp's declared workflow (revision 3) says director and ops run `claude`.
- Both live workers run Codex.
- `runtime_targets()` returns a registered row only while its runtime and
  target equal the declared ones, so both roles look unregistered.
- `register_role_runtime` refuses any other runtime, so the live Codex Director
  cannot be re-registered by any process-bound means while the declaration
  disagrees.

**The Director's decision (A):** Codex is intended, and the declaration is the
defect. Correcting it is a separate workflow-migration ticket. This ticket must
not reattach or replace those workers as Claude, and must not bypass
registration. What it adds is a precise, bounded refusal.

`runtime_divergence_refusal`, called from `presentation_action` when assignment
resolution refuses:

- **It runs only for a caller the existing check authorizes** (the Director's
  pane, or the bridge-authenticated operator for a Director recovery). Anyone
  else gets the old refusal unchanged, and no probe runs for them.
- **Only roles the board reports missing are examined.** For each, the target
  is its **declared** target from `GET /api/workflow`, never a conventional
  `<project>-<role>:0.0`, and only if it belongs to this project.
- **The probe is one exact-match read** of that pane:
  `tmux display-message -p -t =<target> '#{pane_pid}'`, then that process's own
  `/proc/<pid>/cmdline`.
- **It claims a divergence only when the pane's process is a supported
  runtime** that differs from the declared one. A shell, a wrapper, a missing
  pane or a matching runtime proves nothing, so the ordinary refusal stands.
- **It changes and authorizes nothing.** It names the declared runtime, the
  target, the live runtime and its pid, says nothing was changed, and says the
  Director has to correct the declared runtime.

On mefp as measured, the operator would now read:

```
switchyard: mefp's declared workflow disagrees with its live workers, so the board holds no runtime assignment for them:
switchyard:   director: declared claude at mefp-director:0.0, but the live worker in that pane runs codex (pid 294308)
switchyard:   ops: declared claude at mefp-ops:0.0, but the live worker in that pane runs codex (pid 294420)
switchyard: the board registers only the runtime a role is declared to run, so these workers cannot hold an assignment, and nothing is reattached to a target it cannot verify. Nothing was changed. The Director has to correct the declared runtime before this can succeed.
```

(That output is inferred from the measured state. I cannot run it against mefp
from here.)

**Evidence.** `operator_display_recovery_test` now has 72 checks, running the
real `presentation_action` against a diverged board. The cases:

- the precise refusal, showing only the verifiable role;
- an unauthorized caller gets the old refusal and no probe;
- no refusal is invented for a matching runtime, a shell in the pane, or a
  foreign declared target;
- the declared recovery target is probed while a decoy at the conventional
  name is ignored.

The decoy case exists because the first mutation round showed that probing the
conventional name survived: every earlier fixture declared exactly the
conventional name, so the two could not be told apart.

**Mutation: 15/15 killed** (the 7 above plus 8 on the refusal).

## Still required, and what this does NOT fix

Successful live recovery on mefp stays blocked by the declared-runtime
defect, which the Director is filing separately. Until that is repaired,
`switchyard recover-display mefp` gives the precise refusal above, not a
recovered Director. **SYRD-239's User UAT has not passed** and must not be
recorded as passed.

Once the declaration is corrected, repeat the live check:

1. Close only the Director's display attachment.
2. Run `switchyard recover-display mefp` from the desktop operator's shell,
   without any role variable.
3. Confirm the same Director worker comes back and is usable.
4. Confirm cross-project recovery and ordinary non-Director presentation
   changes are refused.
5. Confirm the recovery is recorded as `operator:<desktop-user>` in the history.

The e2e case stands in for sudo's setuid hop and the tmux reattachment itself.
Only the live check covers those.
