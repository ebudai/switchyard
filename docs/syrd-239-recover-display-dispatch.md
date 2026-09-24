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

## Third live failure: success reported, the wrong displays rewired

This happened after the declared runtime was repaired (SYRD-262, revision 4).
`switchyard recover-display mefp` exited 0 and said the Director was recovered.
On the User's four-pane window, though, the Director was not restored, and Ops
turned into an inert "mefp: display slot hidden" screen. The command's own
report called slot 1 Director and slot 5 Ops `client=connected`.

**Cause, reproduced on a real tmux server.**

- **Recovery re-applied the whole stored mapping.** It called `_mutate` with a
  no-op transform, which runs `_apply_mapping` over every slot and does
  `respawn-pane -k` on each slot's proxy.
- **The stored mapping was not what the window showed.** While the layout is
  "default", `_read_state` re-derives the mapping from the *current* config on
  every read. After mefp's role set changed, it named Director slot 1 and Ops
  slot 5 of 6. The window that was open still showed display 0-3 as they were
  launched. Re-applying the mapping rewired the visible panes: in the
  reproduction, the pane that showed the Director got `designer`, the Director
  moved to another pane, and the pane that showed Ops got `audit` (on mefp it
  got an empty slot, hence "hidden").
- **"connected" was the `@switchyard_role` label.** That label is set right
  after a respawn whether or not the nested attach inside it lives.

**The fix.**

- **Recovery finds the display from the live display sessions themselves.**
  It reads the `<project>-display-<n>` sessions and each one's label, which is
  what the open window is showing. It reconfigures only the displays showing
  that role, and never the stored mapping.
- **Every other display is left alone,** as are the viewer layout and the
  mapping. Viewer observer flags are reconciled, which respawns nothing.
- **Success means proven attachment.** Each recovered display's pane must be
  alive, and its `pane_tty` must be a client of the worker's own session. The
  proxy is a nested `tmux attach` in that pane, so nothing else puts that tty
  in the worker's client list. There is a bounded wait of 5 seconds.
  Otherwise recovery refuses, and nothing is recorded as a recovery.
- **History records the actor and the displays touched,** without changing any
  slot.
- **With no live display showing the role, recovery refuses before the worker
  step,** naming what each live display does show. Nothing is started or
  restarted on the way.
- **The report measures instead of echoing the label.** `client_state` is now:
  - `connected` only when attachment is proven;
  - `detached` when the slot is labelled for a role but no live attach exists
    from it;
  - `hidden` for a hidden slot;
  - `disconnected` or `failed`, as before.

  `actual_role` is still the label, and the report now says whether it is true.
- **Liveness is checked as well as the tty.** A dead pane is never attached,
  because Linux reuses pty numbers. Another terminal attached to the Director
  can hold a dead slot's old tty.

**Evidence.**

- `tests/display_recovery_live_tmux_test.py` is new, with 34 checks on a real
  but **isolated** tmux server: `TMUX` is removed, `TMUX_TMPDIR` is temporary,
  and the socket path is asserted before anything is created or killed.
- It builds mefp's shape:
  - four display sessions launched in the old order, with real nested
    attaches;
  - four real outer clients standing in for the Konsole panes;
  - a config whose derived mapping says Director 1 / Ops 5.
- It closes the Director's attachment and recovers through the real
  `presentation_action`. Then:
  - display 0 is again a client of the Director;
  - displays 1-3 keep the same proxy pid, the same attachment and the same
    label;
  - every outer client is still attached;
  - history records `[0]`;
  - the mapping is unchanged;
  - the report agrees.
- A recovery whose worker is gone is refused and records nothing. A window
  with no Director display is refused before the worker step.
- **On main (`0a0b08d`) the same test fails.** The report calls the closed
  attachment `connected`. Without that check, display 0 is not reattached. A
  scratch run shows main respawning all four displays and rewiring them:
  `director -> designer`, `main -> director`, `audit -> main`, `ops -> audit`.
- `operator_display_recovery_test`: 74 checks. **Mutation: 10/10** on this
  round, and the earlier 15 are still 15/15.
- **Two test-harness defects of my own, found and fixed:**
  - `team_launcher` is loaded twice, once as `scripts.team_launcher`, so a patch
    on the test's `tl` does not reach `presentation_controller`. The
    divergence cases from the previous round passed by reading **this host's
    real mefp grant**, which names eric. They now patch both modules, and they
    authorize a name no grant contains, so a patch that misses fails the test
    instead of passing it. Disabling the second-module patch makes them fail.
  - The first draft of the live test let the real worker start run for the
    same reason.
- **One neighbour fixture was extended, not rewritten.**
  `test_recover_command_requires_desktop_readiness_and_uses_prepared_role_environment`
  had no display sessions at all; on main, "recover" created them by
  re-applying the mapping. It now uses `AttachingPresentationRunner`, a
  subclass that adds one live display labelled `app` and the nested client
  its respawn produces. The shared fake and the case's assertions are
  unchanged.
- **Sweep:** 31 suites give identical pass/fail against `0a0b08d`, and 198
  cases are identical. The one difference is
  `test_separate_bootstrap_attaches_konsole_leaves_to_stable_slots`: same
  failure, same message, but its line moved from 796 to 834 because the 38-line
  subclass was inserted above it.

## Still required, and what this does NOT fix

The declared-runtime defect was repaired as SYRD-262 (revision 4).
**SYRD-239's User UAT has still not passed.** The third live failure above is
what this round fixes, and the Director has held further live retries until an
audited correction is ready. Then repeat the live check:

1. Close only the Director's display attachment.
2. Run `switchyard recover-display mefp` from the desktop operator's shell,
   without any role variable.
3. Confirm the same Director worker comes back and is usable.
4. Confirm cross-project recovery and ordinary non-Director presentation
   changes are refused.
5. Confirm the recovery is recorded as `operator:<desktop-user>` in the history.

The e2e case stands in for sudo's setuid hop and the tmux reattachment itself.
Only the live check covers those.
