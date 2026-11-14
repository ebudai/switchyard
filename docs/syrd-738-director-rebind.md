# SYRD-738: rebinding a Director held by a provider safety hold

A safety refusal holds its provider's whole account (SYRD-672). When the account is the Director's own, nothing may
ask that Director for anything -- not to approve its replacement, explain, retry or write a handoff -- and nothing may
act as the Director. The operator replaces it with one command:

```bash
pkexec switchyard rebind-director <project> --cli codex --hold <N> --reason-file why.txt              # preview
pkexec switchyard rebind-director <project> --cli codex --hold <N> --reason-file why.txt \
    --apply --expect sha256:...                                                                       # apply it
```

`--model M` picks the model on the new runtime; without it, the runtime's own default. The hold is never cleared
here: `ticket-board-write clear-provider-account-hold`, by the Director or the User, is still the only way.

Code: `scripts/director_rebind.py`, and migration `pgu1039` (`scripts/ticket_board/schema.sql` identical, grants in
`rbac.sql`). Tests: `tests/director_rebind_test.py` (every seam) and `tests/director_rebind_live_test.py` (a
production-built board, the Director refused first).

## Authority

It extends two boundaries that already existed:
- SYRD-593: root may repair a role's model while the Director is down. The rebind is root's too, but it changes a
  runtime, so it takes the stronger authority below.
- SYRD-262: `rebind-workflow-panes`, root's only way to change a declared pane role without the Director.

| | |
|---|---|
| who | root, elevated by **pkexec** for a named person (`rollout_journal.resolve_operator`). sudo is refused: the operator's identity is part of the record. |
| tenant | the configuration root has verified against its own records (`root_verified_tenant`). |
| reads | the board root's own unit names, over its Unix socket, with the kernel checking who answers (`board_identity.board_get_for`). Nothing comes from the tenant's configured board URL. |
| writes | the database owner's connection, through root-only functions that no role is granted: `begin_director_rebind`, `note_director_rebind_step`, `finish_director_rebind`, `director_rebind_unavailable` and SYRD-262's `rebind_declared_pane_roles`. |

## What it reads, before anything changes

None of this touches the held account.

- **The recovery packet** (SYRD-737) for the named hold. It is validated, then checked against a second read of the
  board (`recovery_packet.fetch_verified`). A packet that does not validate, or a board that changed between the two
  reads, refuses.
- **The hold**, from that packet. It must be current, and `director_held` must be true. A hold on another account is
  the Director's own to handle: it moves the work with `set-role-runtime`.
- **The declared workflow** and its revision, and the Director's registration: pid, start time, uid and generation.
- **Each runtime not under a hold**, and whether the Director could start on it. These are the launcher's own
  first-run checks: installed and signed in for the owner, trusting the worktree where the runtime needs that, and a
  projection the start check can confirm. A held runtime is never a candidate.

The preview prints all of it, then a **review digest**. The digest covers:
- the hold, the packet's state digest, and the workflow revision and digest;
- the old declared binding and registration;
- the configuration's hash;
- the target runtime and model.

`--apply` must name that digest with `--expect`. If anything moved since the preview, the digest differs and nothing
is applied.

## Apply

Each step is recorded on the board as it is made, in `director_rebinds.steps`, and in root's rollout journal:

1. **begin**: the board records the rebind. It holds the old identity (declared binding, registration, and the session
   record kept as evidence), the new runtime and model, the operator, the reason, the hold, and the packet's
   provenance (schema, version, project, `generated_at`, `db_xmax`, both digests). The database refuses:
   - a hold that is not current, or not on the Director's account;
   - the held runtime, or another held one, as the target;
   - another hold's packet;
   - a second rebind while one runs.
2. **old_stopped**: the old Director session is stopped, and the command waits until the processes holding its
   registration are gone (SYRD-559's handover). The held CLI is never started, typed into or resumed.
3. **session_set_aside**: its session record is copied, mode 0600, to `held-session-hold-<N>.json` beside where the
   packet goes, and its digest and session id are in the board's record. It is then cleared while the role is still
   configured as the held runtime, so no SYRD-622 left record is kept: a later switch back to that runtime, even after
   the hold is cleared, starts fresh rather than resuming the refused session.
4. **workflow**: `rebind_declared_pane_roles` moves the Director's declared runtime, with the same target and slot. It
   is a compare-and-swap on the reviewed revision and document, and the previous document stays in
   `workflow_revisions`.
5. **projection**: the launcher configuration and its tenant copies name the new runtime.
6. **packet_written**: the validated packet is written, mode 0600, beside the Director's session records, for the new
   session.
7. **started**: the new Director is started and proven: a live session, running the declared provider, and registered
   by the board on the new runtime.
8. **finish**: the board marks the rebind applied.
   - The Director gets **one fixed notice**, payload kind `director_rebind`. It names the packet file and
     `ticket-board-read recovery-packet --check FILE`, and carries nothing but identifiers, runtime names and that
     path.
   - It rides on the hold's anchor ticket, else the Director's, else the newest open ticket. The listener judges it
     only by the rebind (`director_rebind_notice_is_current`): current while that rebind is the newest applied one,
     and the Director's account is its runtime.
   - Notices that waited on the held Director become due now, and each is checked again before it goes.
   - The operator gets one `director_rebind` user event.

## When it fails

Any failure after **begin** undoes what was done, in reverse. Each step is marked before it is tried, so a step that
took effect and then failed to answer is undone too:
- the new session, if one started, is stopped;
- the workflow: the board is read again, and if it declares the new runtime it is put back (compare-and-swap on what it
  says now). A write that never committed leaves nothing to put back;
- the projection goes back to the journal's copy, which was written before the first byte changed.

It is called rolled back only after that end state is read back: the board declares the held runtime, the
configuration names it, and no Director session is live. Anything else is `needs_operator`, with the journal kept.

**The Director is left stopped.** A rollback never starts the held account. That leaves exactly one authoritative
Director identity: the held runtime, declared and configured, and no live session.

The board then finishes the rebind as `rolled_back`, with what failed and what was undone. It also sets the work pause
(`drain`, attributed to `operator:<name>`) and gives the operator one standing user event.

A workflow undo the board refuses leaves the projection alone, because restoring it would put it against a board that
still declares the new runtime. The rebind is then `needs_operator`, and root's journal file keeps the prior state.

## No eligible alternate

When every runtime is held or cannot start, the preview says so and what each runtime lacks. `--apply` then only keeps
work paused: the drain pause, routing and delivery to every role pane waiting. It also tells the operator, once per
hold, with one standing user event and a `director_rebinds` row of state `unavailable` naming what is missing. Make a
runtime ready (install it, or sign the project account in), then run the rebind again.

## Records

- `director_rebinds`: one row per rebind, readable by the service and the listener (`director_rebind_status()`).
- `user_events` kind `director_rebind`, detail the outcome:
  - `applied`: news, not standing;
  - `rolled_back`, `needs_operator` or `unavailable`: standing until a later rebind for the same hold is applied, or
    the hold is cleared.

## Not done here

- The hold stays until it is cleared. Replacing the Director says nothing about whether the old account is safe.
- The phone bridge (`user_notify`) does not yet show `director_rebind` events, as it does not show
  `provider_safety_hold` events. That is a separate change to its kind tables.
- The break-glass role (SYRD-739) is a separate path: one bounded operation, then hand-back or this command. See
  `docs/syrd-739-break-glass.md`.
