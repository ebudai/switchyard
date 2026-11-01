# SYRD-724: each project's own board stall threshold

The board stall alert (SYRD-637) tells the Director once when runnable work has made no durable progress. It fired
after one threshold for every project on a host: the listener's `TICKET_BOARD_BOARD_STALL_SECONDS`, 30 minutes by
default. A project whose normal validation phases (long tests, mutation runs, builds, data processing) run longer
without a checkpoint was alerted for healthy work, and the Director inspected and snoozed it, again and again.

On main `32578266`:
- a workflow document that declares a threshold is refused ("unknown workflow configuration field");
- no command sets one;
- a project that needs an hour is alerted at 31 minutes;
- the status shows one number and says nothing about where it comes from.

## The change

**The setting.** A declared workflow may carry:

```json
"board_stall": {"threshold_seconds": 5400}
```

- **Bounds.** A whole number of seconds from 300 (5 minutes) to 86400 (24 hours).
- **Refusals.** `workflow_config.validate` (`board_stall.validate_declared`) refuses anything else, and the board
  keeps the document it had:
  - zero, a negative number, a value out of bounds;
  - a fraction, text, `true`;
  - an empty object, an extra key, something that is not an object.
- **Absent.** An absent setting is the default and stays absent, so no stored document changes by being revalidated.
- **Why these bounds.** Below 5 minutes, ordinary hand-over latency could alert. Above 24 hours, the alert stops
  being a backstop.

**The default.** For a project that declares none, the threshold is what it was before: the listener's setting,
1800 seconds unless the host sets `TICKET_BOARD_BOARD_STALL_SECONDS`.

**Evaluation** (`ticket_board.note_board_stall`, migration pgu1025, mirrored at the end of schema.sql):
- On every pass it reads the project's declared value (`ticket_board.declared_board_stall_seconds`) and uses it in
  place of the listener's.
- It records both the threshold it evaluated under and the listener's default.
- A stored value outside the bounds, or not a whole number, is read as no value: the default, never monitoring off.
- Each episode already records the threshold it fired under.

**Status.** `ticket_board.board_stall_status` (GET /api/board-stall) reports:
- the effective threshold, from the document as it is now, so a change shows before the next pass;
- `threshold_source`: `workflow` or `default`;
- the default, and the threshold last evaluated under;
- `fires_at`, by the effective threshold.

`ticket-board-read stall` prints, for example, "threshold 3600s (this project's workflow; the default is 1800s)" or
"threshold 1800s (the default)".

**The Director's command.**

```bash
ticket-board-write set-board-stall-threshold --seconds 5400                                  # preview only
ticket-board-write set-board-stall-threshold --seconds 5400 --apply --expected-revision <rev>
ticket-board-write set-board-stall-threshold --default --apply --expected-revision <rev>
```

It changes the declared workflow's `board_stall` and nothing else in it, through the board's own workflow apply, in
the same shape as `add-user-acceptance-relay` (SYRD-535).
- **The preview** shows the revision, the value before and after, the default and the digests. The board's dry run
  refuses an invalid value there, and nothing changes.
- **`--apply`** needs the revision the preview showed and is refused if the workflow moved since. The same value
  again is reported unchanged.
- **Authority.** Only the Director may apply a workflow (`app.apply_workflow`). A role's own pane is refused, the
  board refuses every other role, and over HTTP a request acts as the User, so no HTTP caller is the Director.

**Board UI.** The board UI's verified delegation (SYRD-598) lends the Director's authority for one ticket's named
operation. Applying a workflow is not such an operation and has no ticket, so the User cannot change this setting
from the board page. Lending a board-wide workflow change through the UI would be new authority, and it is not
added here. The Director decided this on the ticket: acceptance 3's "verified UI authority" is over-scoped, and what
must hold is the Director's authority and the refusal of ordinary roles, with SYRD-598's ticket-scoped delegation
unchanged.

**What it does not change.**
- **The busy-pane escalation.** The role stall escalation's busy threshold stays the listener's own value, passed
  into `note_role_stall` on every pass (SYRD-675).
- **Other signals.** Execution leases, held and busy-pane notices, provider quota holds and outage detection read
  nothing new.
- **Readers.** Only `declared_board_stall_seconds` reads the setting. The test pins that.

**Changes mid-interval.** A change counts from the next pass, against the same durable-progress clock. Nothing is
restarted and nothing is replayed:
- **An alert already sent** (an open episode) is never sent again, whether the threshold is raised or lowered. Its
  episode closes on progress or when nothing runnable remains, as before, and the next one runs under the new value.
- **Raised before an alert:** the alert waits for the new threshold.
- **Lowered below the time already elapsed:** that stall's one alert comes at the next pass. It is not a duplicate,
  because none was sent for it, and the Director asked to be told sooner.

Deduplication, progress checkpoints, the exclusions (the User's waits, snoozes, holds, blockers and the rest) and the
recovery semantics are unchanged.

**Lifecycle.**
- **Restarts.** The setting lives in the board's own workflow document, so a board restart and a listener restart
  both keep it: the evaluator reads it from the database each pass.
- **Rebuilt documents.** Every path that rebuilds a document on a running board starts from the live document and
  keeps its other keys: a runtime switch, pane rebind, `ticket-board-workflow` operations, and the Director onboarding
  migration.
- **Upgrade.** The upgrade's phases neither compare nor overwrite a running board's document.
- **The projection** writes the validated document whole. `migrate-workflow` installs only onto a board that has none.
- **Re-provisioning.** Re-running root's provisioning packet keeps the value. This was Audit's finding on the first
  candidate, `7344aa73`: the packet's workflow phase applied root's recorded copy as it is, so a record older than the
  Director's change removed or reverted the threshold. The phase (`provision_workflow_sql.render_workflow_sql`) now
  applies `ticket_board.provisioned_workflow(<root's record>)`:
  - root's record is the document, and wins everywhere else: roles, stages, transitions, capabilities;
  - a board that already runs a declared workflow keeps its own board stall threshold, carrying a valid declared
    value into the document applied, or no key when the board has none;
  - a board running none (first provisioning) takes root's record exactly, threshold included.

  The packet takes the workflow advisory lock in its own statement first, as every Director apply does
  (`workflow_locking`). This was Audit's second finding, on `11735843`: the read inside `provisioned_workflow` is an
  argument, evaluated before `apply_declared_workflow` reached that lock. A packet waiting behind a Director apply
  therefore wrote back the threshold it had read before the Director's commit. Under READ COMMITTED each statement
  takes its own snapshot, so with the lock taken one statement earlier, the read happens after any apply ahead of it
  has committed.

  The value carried is the Director's decision, bounded and admitting nothing, and the Director could set it again
  anyway, so the root/tenant trust boundary is unchanged. Other board-only workflow changes are still re-applied
  from root's record, as before. `adopt-workflow`, on review, records the board's document as root's.

**Oracle.** Main has no Oracle backend. SYRD-645's Oracle core (in User Review, not on main) has no board stall
alert at all, so there is nothing to keep in step. When an Oracle stall evaluator arrives, it should read the same
`board_stall` key with the same bounds.

## Evidence

`tests/board_stall_threshold_test.py`, 61 checks. The boards are built as `router_role_board_test` builds them:
production-built, with the real HTTP server, a real Unix-socket server under process authority for the Director's
registered pane, and the real listener. Time is the evaluator's explicit `p_now`. Main runs first, from a git
archive.

- **Main.** A declaring document is refused, there is no command, a project that needs an hour is alerted at 31
  minutes, and the status says nothing about a source.
- **Concurrency.** Three projects in one cluster, under one listener default of 30 minutes, alert at their own
  times:
  - 15 minutes declared: silent at 14, alerted at 16;
  - nothing declared: alerted at 31;
  - an hour declared: silent at 59, alerted at 61.
  The status and `ticket-board-read stall` give each threshold and its source. `fires_at` follows each threshold, and
  follows a change at once. Each episode records the threshold it fired under.
- **The listener's default.** A project that declares none keeps the listener's own setting, shown here at 20
  minutes.
- **Corrupt stored values.** 200, 90000, "1h", "3600" and 3600.5 are each read as the default.
- **The command, from the Director's pane:**
  - the preview changes nothing;
  - `--apply` without a revision, or at a moved revision, is refused;
  - the apply lands, and the same value again is unchanged;
  - 299, 86401, 0 and -5 are refused, saying the bounds, with the prior value kept, and a preview of 299 is refused
    by the board's dry run;
  - "soon", "1.5", neither flag, or both flags are refused;
  - six malformed documents are refused through the board's own apply;
  - 300 and 86400 are accepted;
  - the new value is in force before any pass, and an idle pass records the default beside it;
  - `--default` restores the default.
- **Authority.** The app, ops and audit panes are refused. The board refuses app, user and audit. The HTTP port, with
  the write token, acts as the User. Nothing they tried changed anything.
- **Changes mid-interval:**
  - a sent alert, raised from 15 minutes to an hour and then lowered to 5 minutes, is not sent again and stays open;
  - progress closes it, and the next stall alerts after the new 5 minutes;
  - raised to an hour 20 minutes in, before any alert: the alert comes at 61, not at 31;
  - lowered to 30 minutes 40 minutes in: exactly one alert at the next pass.
- **Lifecycle:**
  - a restarted board app reads the value;
  - a restarted listener's first real pass evaluates under it;
  - a runtime switch and a role-prompt edit rebuild the live document and keep it;
  - main's board, upgraded with this tree's migrations, alerts at its declared hour and reports the source.
- **The busy pane.** On a project declaring an hour, after the board stall pass recorded that hour, the listener's
  role stall pass escalates a pane busy 1900 seconds, under the listener's 30 minutes, with `threshold_seconds` 1800
  in its payload.
- **Re-provisioning.** This is the Audit kick-back case. A board is provisioned from root's record, the Director's
  command (from its pane over the socket) sets 90 minutes, and the provisioning packet's own workflow SQL, rendered
  from root's pre-change record, is run as the packet runs it (psql as the database owner, the SQL switching to the
  board's writer):
  - the board still declares 90 minutes, everything else is root's record, and the alert stays silent at 31 and 89
    minutes and fires at 91;
  - the previous candidate's packet, applying root's record as it is, drops the value;
  - a record carrying an older value of its own (15 minutes) does not bring it back;
  - after the Director's `--default`, the packet leaves the default;
  - a board running no workflow yet is seeded with root's record exactly, an hour included.
- **The packet racing a Director apply**, in both commit orders, through the real paths: the Director's
  `app.apply_workflow` and the packet's rendered SQL run with psql. A third connection holds the workflow lock so the
  two queue in a known order, and `pg_stat_activity` confirms each is waiting on that advisory lock before the next
  starts.
  - With the Director first, the Director's 2 hours stands; with the packet first, likewise.
  - The control, the previous candidate's packet without its lock statement, reproduces Audit's lost update: it
    writes back the 90 minutes it read.
  - The result was stable across repeat runs.
- **Mirror and readers.** schema.sql's last copies are the migration's, byte for byte, and only the evaluator's own
  function reads the setting.

**Mutants.** 32, each killed, all rerun on the final tree:
- **SQL mutants** edit the migration and schema.sql's copy alike. They cover the helper's value, both bounds, the
  digit and type checks, the evaluator's use of it, both records of the default, the status's effective value,
  source, default and `fires_at`, and the listener's own value.
- **Python mutants** cover validation (the call, both bounds, the exact shape), the whitelist, and the command's
  revision guard, apply guard, unchanged answer, dry run, output, `--default` and key replacement.
- **Provisioning mutants** (Audit's case) cover the packet's call to `provisioned_workflow`, each of its three
  branches (a board running none, a board without a value, a board with one), its grant, and the packet's lock
  statement, whose removal the director-first race check kills.
- **How they died.** 31 died on a check. One, removing the key from the whitelist, made the board refuse every
  declaring document, with its own message.
- **Removed as no-ops.** A preview field that was always true, and a redundant `--default` expression.

`workflow_config.py` stays under its 1,250-line review limit: the five core keys of the top-level whitelist now
share a line. It goes from 1,249 lines to 1,248.

**Neighbour sweep.** It ran on the final tree, 178 suites, against main. That is every suite that touches:
- the board stall or the role stall;
- workflow validation, the write CLI, and the Director's skill and guide;
- schema and migration order, and the size review;
- and, for this round, provisioning: the workflow SQL, the operator packet, root's records, adoption, seed replay and
  rebind.

Results:
- 149 pass on both, including every suite that runs or compares the declared workflow SQL against real boards:
  - `root_workflow_record_test`, `adopt_workflow_test`;
  - `ticket_board_project_workflow_provision_test`;
  - `workflow_seed_replay_test`, `ticket_board_resumed_workflow_replay_test`;
  - `workflow_pane_rebind_test`, `uat_upgrade_parity_test`.
- `board_stall_threshold_test` is new and passes here.
- `provision_workflow_sql_boundary_test` pins the declared packet's SQL, so it was edited by rule. Its golden for the
  declared case is now the test's own measure of the new SQL (its length, line count and sha256, which I checked
  against the SQL by hand), because the longer text is measured rather than stored. One check reads those counts
  instead of the string. It passes, 150 checks, as does `ticket_board_schema_function_migration_test`.
- 28 fail on both at the same line:
  - 25 have the same failure: environment (root-only packet execution, `/etc` and `/usr/local` paths), or goldens and
    guards that main already fails;
  - `publication_handoff_end_to_end_test` differs only by a commit hash made per run;
  - two migration-order guards list the newest migrations, and the list moves on by one name.

Nothing live was changed.
