# SYRD-723: an implementer keeps its own ticket's blocker explanation current

A ticket's `blocked_reason` says why its work cannot proceed. Only the Director's `set_blockers` (and the board UI's
delegated edit of it) could write it, so when the implementer that owned the ticket found, refined or resolved the
reason, the text went stale unless the Director spent context rewriting it. On main `a5641df4` the owner had no route
at all: `set_blocked_reason` did not exist, `set_blockers` refuses every role but the Director, and `edit_fields` does
not carry `blocked_reason`.

## The change

`ticket-board-write set-blocked-reason <id> --blocked-reason "..."` replaces the text, and `--clear` empties it. It
calls the new operation `set_blocked_reason`, whose whole payload is `{"blocked_reason": <text>}`.

**Who may.** The database function `ticket_board.set_blocked_reason` (pgu1024, mirrored in `schema.sql`, granted to
the board's writer alone in `rbac.sql`) admits the caller only when all of these hold:

- the role holds `edit_fields` and is active (`require_actor`, as every board write);
- it is the ticket's assignee;
- it is an owner of the ticket's stage;
- the workflow declares that stage `implementation`;
- the calling process is the role's registered live runtime.

The ticket row is locked first (`FOR UPDATE`), so a transition or reassignment and the edit are ordered: whichever
commits first, the other sees it. The stage-owner trigger would also refuse an edit by an assignee who is no longer
an owner, but the function refuses it first, with the reason.

**From its own pane.** The board server names the calling process from the kernel (`SO_PEERCRED`), as SYRD-714's
`notify_user` does. The request must come over the board's Unix socket under process authority. The database then
checks that `(pid, start time, uid)` is that role's registration, for the runtime and target the role declares now,
which is the same rule the server resolves a pane by (`runtime_assignment_for_process`). So these are all refused:

- a replaced runtime;
- a retired one, with no registration;
- a registration the role's declaration has moved on from;
- a detached process;
- the HTTP port, even with the write token.

A board that declares no workflow registers no runtime, so it has no such caller: the operation is admitted there
only to answer where it works.

**Admission.** At the HTTP layer the operation is composed from `edit_fields`. A ticket's blocker explanation is its
text, as `edit_fields` edits its implementation notes, and every implementer holds `edit_fields` on the live board,
so no workflow document has to change. The legacy table admits the implementer roles. Neither layer is what narrows
it: the database does.

**What it changes.** The text, and nothing else:

- No blocker is added or removed.
- No state, assignee or wait changes. A request that also names `blocked_by`, `state`, `assignee`, `awaiting_role`
  or anything else is refused, and the refusal names the mechanism that does that change.
- Saving text manufactures no wait (SYRD-285: a reason is a note).
- Clearing it removes no link. While a blocker stands, its explanation can be revised but not emptied, because
  `set_blockers` never lets a standing blocker go unexplained. Once none stands, the text can be cleared, and the
  resolved link stays.
- The ticket is touched so open boards redraw it, which also refreshes its stored document. The touch is marked as
  bookkeeping, as a comment's is, so it cannot clear any wait, even a wait recorded on the owner itself.
- Unchanged text writes nothing.

**Provenance.** Each change writes one `ticket_field_audit` row, as `director_edit` records every field it changes:

- the actor (the role);
- the field, `blocked_reason`;
- the old and new value;
- a reason naming the runtime: "blocker explanation, from app's registered runtime: generation 2, process 41234".

**Unchanged.**
- The Director's `set_blockers` and `release_external_blocker`.
- The ticket editor's `blocked_reason` edit.
- The board UI's delegated authority.
- The ticket read model.

**Documentation.**
- The board skill and the board guide tell implementers when to use it: keep the explanation current; ask for a
  dependency with `request-dependency`; finish with the declared transition; never let prose stand in for either.
- The Director skill says the owner now keeps it current.

**Oracle.** Main has no Oracle backend. SYRD-645's Oracle core (in User Review, not on main) refuses every blocker
operation as not yet in the core ("blockers slice"), so the two backends still agree here. When it lands, the new
operation belongs with those refusals until its blockers slice.

## Evidence

`tests/blocker_explanation_test.py`, 74 checks. Boards are built the production way:
- schema.sql, roles, rbac.sql;
- the real migration runner, then rbac.sql again;
- the shipped workflow;
- the real HTTP server for the Director, and the real Unix-socket server under process authority for the
  implementers, with this process registered as each implementer's runtime in turn.

Main runs first, from a git archive of its own tree.

- **Main.** The owner's three routes (this operation, `set_blockers`, `edit_fields`) are all refused, and the text
  stays as it was.
- **Upgrade.** Main's board, upgraded by this tree's migrations, has the operation, executable by its owner and the
  board's writer only. The last copy in schema.sql is byte for byte the migration's.
- **Owner's edits.** The owner sets (trimmed), revises, repeats and clears the text, from the socket and through
  `ticket-board-write set-blocked-reason` (literal backticks and `$`; exactly one of the text and `--clear`).
  - Five changes give exactly five audit rows: role, field, old, new and runtime.
  - The ticket's `updated_at` moves on every change, and the API and the stored document show the text.
  - Blockers, state, assignee and the wait never move.
  - A Director wait the owner asked for, and a wait recorded on the owner itself, both survive an edit.
- **Standing blocker.** With a standing local and operator blocker, revising the text keeps both links, and clearing
  it is refused, naming them. After the release and the cancellation, clearing works and the resolved link stays.
- **Refused, with nothing changed:**
  - another owner's ticket, and the Director's;
  - the Director and a reviewer;
  - the Director as owner of a Triage ticket (an owner and assignee, but not in implementation);
  - a role without `edit_fields`, or no longer an owner of the stage;
  - a deferred ticket;
  - a request naming another field, or with no text;
  - the HTTP port;
  - a replaced, a retired and a stale registration, to the server and to the database alone;
  - another process's pid, start time or uid;
  - no caller.
  - Only the board's writer may execute the function, even in the window before rbac.sql runs.
  - A re-registered runtime's edit is attributed to generation 2.
- **Races, both orders,** detected through `pg_stat_activity` lock waits:
  - a Director's defer holds the row first, and the owner's waiting edit is then refused;
  - the owner's edit holds it first, and the Director's waiting reassignment lands after it, on top of the new text.
- **Director paths.** The Director's `set_blockers`, the ticket editor's edit, and clearing both are unchanged, and
  are not audited here.
- **Board with no declared workflow.** The implementer is told where the operation works; the Director is refused.

`tests/ticket_board_operation_role_policy_test.py` pins the role tables. It was edited by rule: the composed table
gains `set_blocked_reason`, and the legacy table gains it for the implementers, 60 entries to 61.

**Mutants.** 28, applied one at a time with the mirror guard satisfied (each SQL mutant edits the migration and
schema.sql's copy alike), and each killed by a check, not a crash. They cover:
- every admission term: capability, assignee, stage kind, stage owner, runtime, runtime/target match, pid, start
  time and uid;
- the standing-blocker rule, the unchanged shortcut, the audit row, its old value and its actor;
- the bookkeeping mark, the touch, the trim and the row lock;
- the grant, both policy entries, the extra-field refusal and its message, the text-type check, the
  process-authority check, the dispatch, the CLI's check and output, and the client's payload.

Three lines a mutant showed to have no effect were removed rather than kept:
- the function's own refresh of the stored document (`touch_ticket` already does it);
- the migration's `REVOKE ... FROM PUBLIC` (the migration runner revokes PUBLIC execute on every board function,
  SYRD-530);
- an `active` term in the runtime lookup (`require_actor` already requires an active role).

`write_client.py` was at its 1,250-line review limit. `set_blockers` is written in the file's compact two-line form
beside the new method, and the file is now 1,242 lines.

**Neighbour sweep.** 150 suites ran on main and on this tree: everything that touches the operation tables, the
write CLI and client, the field audit, schema and migration parity, the board and Director skills and the board
guide, blockers and `blocked_reason`, runtime registration, and the size review.
- 131 pass on both.
- `blocker_explanation_test` is new and passes here.
- `ticket_board_operation_role_policy_test` passes here, after the by-rule edit above.
- 18 fail on both, at the same line. 16 have the same failure. In the other two
  (`ticket_board_park_blocked_postgres_test`, `ticket_board_publication_migration_recovery_test`) the failure lists the
  newest migrations, and the list moves on by one name: pgu1024.

The 16 with the same failure are `claude_permission_hook_test`, `field_audit_grants_test`,
`privileged_job_selftest_test`, `publication_proof_test`, `pull_claimant_rotation_test`,
`team_launcher_add_role_vcs_test`, `team_launcher_new_project_test`, `team_launcher_switchyard_new_prompts_test`,
`ticket_board_held_review_reconcile_postgres_test`, `ticket_board_postgres_backend_test`,
`ticket_board_read_query_boundary_test`, `ticket_board_schema_test`, `ticket_board_ui_audit_test`,
`ticket_board_write_api_test`, `unassigned_triage_notice_test` and `upstream_report_default_test`.

Nothing live was changed.
