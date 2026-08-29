# Per-Project Ticket Board Provisioning

Render reviewable artifacts for a new project board:

```bash
scripts/ticket-board-provision-project \
  --project stellaris \
  --owner-user stellaris-agent \
  --port 8871 \
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
  projects this replaces the default PGU workflow data with the visible stages
  `Draft`, `Triage`, `Implementation`, `Audit`, DAT, UAT, and `Final Sign-Off`.
  The generated `Audit` stage uses `entry_gate_field=needs_audit` and
  `gate_skip_to=dat`, so each ticket can opt out of audit while still using
  the same workflow gate machinery as inspection and UAT.
  The default implementation-stage owner is the generic caller role
  `implementer`; pass `--implementer-role <role>` more than once to seed a
  project-specific implementer set instead.
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
