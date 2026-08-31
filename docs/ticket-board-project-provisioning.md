# Per-Project Ticket Board Provisioning

Render reviewable artifacts for a new project board:

```bash
scripts/ticket-board-provision-project \
  --project stellaris \
  --owner-user stellaris-agent \
  --port 8871 \
  --output-dir /tmp/stellaris-board-provision
```

Projects that want source-control work after final sign-off can insert a VCS
stage and make a project role responsible for the terminal close:

```bash
scripts/ticket-board-provision-project \
  --project stellaris \
  --owner-user stellaris-agent \
  --implementer-role ops \
  --vcs-close-role ops \
  --output-dir /tmp/stellaris-board-provision
```

The output directory contains:

- `plan.json`: resolved project name, owner, port, database, unit names, socket,
  asset paths, frame paths, and connection strings.
- `<project>-ticket-board.service`: system board service unit with unique port,
  runtime directory, Unix socket, database, and owner-home asset/frame paths.
- `<project>-ticket-board-notify-listener.service`: owner user unit for the
  LISTEN/notification shim, pointed at the same project database and pane-state
  runtime namespace.
- `<project>-ticket-board.conf`: tmpfiles entry for the project frame inbox.
- `<project>-database.sql`: PostgreSQL database/role bootstrap.
- `<project>-workflow.sql`: per-project workflow seed. For new non-`pgu`
  projects this is rendered as a projection of the authoritative workflow seed
  in `scripts/ticket_board/schema.sql`, with project-specific role lists
  substituted for implementation and draft ownership. Tenant boards
  deliberately omit the pgu-only `Backlog` and `Inspection` stages plus the
  `defer`, `start_task`, `submit_to_inspection`, and `request_commit_exempt`
  actions; future schema workflow additions that are not part of that explicit
  omission policy flow into generated tenant workflow SQL. The generated
  `Audit` stage uses `entry_gate_field=needs_audit` and `gate_skip_to=dat`, so
  each ticket can opt out of audit while still using the same workflow gate
  machinery as UAT.
  This derivation also intentionally aligns tenant director routing with
  `schema.sql`: newly generated tenants no longer get the historical tenant-only
  `in_progress -> audit` or `audit -> director_review` `route` edges, and they
  do get the schema-authoritative `audit -> in_progress` `route` edge. Existing
  tenants are not re-seeded by this path; if an existing tenant needs those old
  escape edges, handle that with an explicit migration or a director override.
  Passing `--vcs-close-role <role>` inserts a `VCS` stage after
  `Final Sign-Off`, routes final-signoff tickets there by director action, and
  changes the generated terminal `mark_done` workflow transition to
  `<role> -> done`. The board service unit also exports
  `TICKET_BOARD_OPERATION_ALLOWED_ROLES=mark_done=<role>` so the HTTP gate
  permits that deployment-specific close request to reach the database.
  For an existing provisioned project, first add the close role if necessary,
  then run `switchyard set-vcs-close-role <project> <role>`. That command
  refuses unknown roles, updates the generated plan and board unit, applies the
  workflow transition update in Postgres, and restarts the tenant board. The
  built-in `pgu` workflow remains fixed; this command is only for provisioned
  tenant projects.
  The default implementation-stage owners are `app` and `main`; pass
  `--implementer-role <role>` more than once to seed a project-specific
  implementer set instead.
- `operator-commands.sh`: ordered privileged commands to review and run.

The provisioner intentionally renders first. It does not mutate the live PGU
board, restart panes, create databases, or install units unless an operator runs
the generated commands during an approved window.

`switchyard new` validates role CLI choices before it reaches this render step.
The accepted CLI names are `claude`, `codex`, and `agy`; retired aliases such as
`gemini` are rejected at prompt/artifact parse time. If the selected owner user
already exists, `new` warns and asks for explicit confirmation, defaulting to No,
before treating that account as the project owner. Pass
`--allow-existing-owner-user` to skip that existing-user confirmation and reuse
the account without prompting. In non-interactive runs, `new` refuses an
existing owner user unless that flag is present.

The `pgu` project is intentionally special-cased to reproduce the deployed
production board: database `pgu`, HTTP port `8770`, and frame inbox
`/tmp/pgu-frames` with a root-owned `1777` tmpfiles entry. It also keeps the
full workflow seeded by `schema.sql`. New projects keep their frame inboxes
under the owner user's home directory and apply `<project>-workflow.sql` after
migrations and before `rbac.sql`.

## Current Schema Constraint

Each project gets its own database. The database roles remain
`ticket_board_service` and `ticket_board_listener` for now because
`schema.sql` enforces those literal actors in `require_actor()` and
`require_ticket_board_listener()`. The separation boundary is the database, not
per-project DB role names. The generated RBAC path reuses `rbac.sql`, including
the listener grant on `ticket_has_unresolved_blockers`.

Changing to per-project DB role names is a separate schema change, not part of
this provisioning step.

Workflow authority role names are still `director`, optional `audit`, and
`user`. Provisioning customizes the implementation-stage owner roles because
those are project/team specific; the board service exports the generated
assignee, caller-role, and implementer-role lists to keep the HTTP gate and
PostgreSQL workflow data aligned.

`TICKET_BOARD_OPERATION_ALLOWED_ROLES` is an optional HTTP preflight override
for deployment-specific operations. Its format is
`operation=role,role;other_operation=role`. Unset keeps the built-in operation
map unchanged. The setting can only decide which callers are allowed past the
HTTP route check; PostgreSQL `workflow_transitions.allowed_roles` remains the
authority for ticket state changes, so the env map cannot grant a transition
the workflow table rejects.
