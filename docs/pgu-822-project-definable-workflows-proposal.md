# PGU-822 Project-Definable Workflows Proposal

PGU-822 is proposal-only. This document intentionally does not implement schema,
server, or launcher changes. It records the shape, risks, and migration path for
letting a project director add roles and stages after provisioning.

## Summary

Make roles and stages data owned by each ticket-board database, not literals in
CHECK constraints or generated unit environment. The current system is already
close: transition legality is enforced from `ticket_board.workflow_transitions`,
and the board UI already consumes ordered workflow columns from
`workflow_stages`. The hardcoded parts that block author-defined workflows are:

- `tickets.state` is constrained to a literal list.
- `workflow_stages.name` is constrained to a literal list.
- `tickets.assignee` is constrained to a literal list.
- `workflow_stages.owner_roles`, `workflow_transitions.allowed_roles`, and the
  HTTP operation-role map still store role names without FK-grade validation.
- Notification routing is still hardcoded by stage name in
  `ticket_board.transition_target_role(...)`. A newly added stage currently
  falls through to `NULL`, so no notification row targets the stage owner.
- Other schema functions still compare against literal stage names for gate
  routing, stale-work scans, nudge text, and action functions. The implementation
  plan must size the conversion against those stage literals, not only the three
  CHECK constraints.
- Tenant role changes currently require root-mediated unit writes/restarts
  because the board process receives role vocabulary through environment.

The right implementation direction is two tracks:

1. Convert workflow vocabulary to database data with database-enforced
   referential integrity.
2. Remove unit-file rewrites from mid-flight role/workflow mutation, so a tenant
   director can request bounded DB mutations through the board without gaining
   sudo or write access to `/etc/systemd/system`.

## Proposed Data Model

Add a roles table:

```sql
CREATE TABLE ticket_board.project_roles (
    name text PRIMARY KEY CHECK (name ~ '^[a-z][a-z0-9_-]{0,63}$'),
    display_label text NOT NULL CHECK (btrim(display_label) <> ''),
    role_kind text NOT NULL CHECK (
        role_kind IN ('system', 'draft', 'implementer', 'reviewer', 'support', 'user')
    ),
    notify_target boolean NOT NULL DEFAULT true,
    is_builtin boolean NOT NULL DEFAULT false
);
```

Change stages:

```sql
ALTER TABLE ticket_board.workflow_stages
    DROP CONSTRAINT workflow_stages_name_check;
ALTER TABLE ticket_board.workflow_stages
    ADD CONSTRAINT workflow_stages_name_format_check
    CHECK (name ~ '^[a-z][a-z0-9_]{0,63}$');

ALTER TABLE ticket_board.workflow_stages
    ADD COLUMN notify_kind text NOT NULL DEFAULT 'none'
    CHECK (notify_kind IN ('none', 'assignee', 'fixed_role', 'stage_owner_fallback')),
    ADD COLUMN notify_target_role text REFERENCES ticket_board.project_roles(name),
    ADD CONSTRAINT workflow_stages_notify_target_check CHECK (
        (notify_kind = 'fixed_role' AND notify_target_role IS NOT NULL)
        OR (notify_kind <> 'fixed_role' AND notify_target_role IS NULL)
    );
```

`notify_kind` is required workflow data, not UI decoration. It replaces
`ticket_board.transition_target_role`'s stage-name `CASE` and determines whether
entering a stage can notify anyone:

- `none`: no transition notification is expected for this stage.
- `assignee`: notify the ticket assignee, or no one if the ticket is
  `unassigned`.
- `fixed_role`: notify `notify_target_role`.
- `stage_owner_fallback`: notify the assignee when it is one of the stage owner
  roles; otherwise notify the sole owner role when the stage has exactly one
  owner; otherwise notify no one. This preserves today's audit-owner behavior.

Seed the current behavior as data before allowing arbitrary stages:

| Stage | Notify rule |
| --- | --- |
| `draft`, `backlog`, `done`, `cancelled` | `none` |
| `analysis` | `fixed_role: director` |
| `in_progress` | `assignee` |
| `inspection` | `fixed_role: inspector` |
| `audit` | `stage_owner_fallback` |
| `dat` | `fixed_role: director` |
| `user_review` | `none`, preserving today's behavior unless a later user-notification feature changes it |
| `director_review` | `fixed_role: director` |

The important invariant is that "no notification" must be explicit. A new
non-terminal stage whose owner expects action cannot silently inherit `NULL`.

Change ticket assignment:

```sql
ALTER TABLE ticket_board.tickets
    ADD CONSTRAINT tickets_state_fkey
    FOREIGN KEY (state) REFERENCES ticket_board.workflow_stages(name);

ALTER TABLE ticket_board.tickets
    ADD CONSTRAINT tickets_assignee_fkey
    FOREIGN KEY (assignee) REFERENCES ticket_board.project_roles(name);
```

The same role table must validate `workflow_stages.owner_roles`,
`workflow_transitions.allowed_roles`, `ticket_notification_queue.target_role`,
`ticket_notification_state.awaiting_role`, and
`ticket_notification_state.last_implementer_assignee`. PostgreSQL cannot attach
a normal FK to each element of a text array, so either normalize these arrays
into join tables or add constraint triggers that reject any unknown role in the
arrays. The more durable design is normalized join tables:

- `workflow_stage_owner_roles(stage_name, role_name)`.
- `workflow_transition_allowed_roles(from_stage, to_stage, action_name, role_name)`.
- Optional compatibility views exposing `owner_roles` and `allowed_roles` arrays
  during migration.

If arrays are kept, add BEFORE INSERT/UPDATE constraint triggers and tests that
delete a role referenced from every array-bearing field.

## Mid-Flight Mutations

### Add Role

Allowed when:

- `project_roles.name` does not already exist.
- The role name passes the role format check.
- The director supplies the role kind and CLI/runtime metadata separately from
  board workflow metadata.

Live-ticket behavior:

- Existing tickets are unchanged.
- New assignments to that role become valid immediately after the DB mutation.
- Notifications may target the role only if the project has a live pane/session
  for it or the role is explicitly marked headless/unreachable with a director
  visible status.

Privilege behavior:

- Adding the board role should not require rewriting a systemd unit.
- Adding a pane/runtime may still require operator-controlled host work unless
  the launcher has a safe, narrow self-service path. The board role and workflow
  role can exist before the pane exists, but the UI must show that the role has
  no running pane.

### Add Stage

Allowed when:

- `workflow_stages.name` is new and passes the stage format check.
- `display_label` is non-empty.
- `rank` is placed without uniqueness collisions, preferably through a function
  that reorders all ranks atomically.
- Every owner role exists.
- Every non-terminal stage has at least one reachable terminal path after the
  insertion.
- At least one legal inbound transition exists unless the stage is an initial
  stage.
- At least one legal outbound transition exists unless the stage is terminal.
- `notify_kind` is configured. If the stage is non-terminal and has actionable
  owners, it must be `assignee` or `fixed_role`; `none` is allowed only when the
  director explicitly marks the stage as non-notifying or externally managed.

Live-ticket behavior:

- Existing tickets are unchanged.
- No ticket enters the stage until a transition or route sends it there.
- If the stage has owner roles, PGU-820's invariant applies: assigned tickets
  in that stage must be assigned to an owner role or to `unassigned`.
- Notification routing starts working as soon as tickets enter the stage because
  the target rule is stage data, not a Python or SQL literal that needs another
  release.

### Rename Stage

Prefer "add new stage, migrate tickets, remove old stage" over an in-place
primary-key update. In-place rename is high risk because transitions,
notification state, UI filters, and any open browser state all key on the stage
name.

Allowed only through a dedicated function that:

- Creates or validates the new stage row.
- Rewrites tickets in the old stage to the new stage in one transaction.
- Rewrites workflow transitions and stage gate references.
- Rewrites notification state rows that store the old state.
- Emits one workflow/config notification so browsers refresh metadata.

Live-ticket behavior:

- Tickets in the renamed stage move atomically to the new stage name.
- If the new stage has different owner roles, assignees must still pass the
  stage-owner constraint or the rename is refused.
- If any queued notification names the old stage in a state-specific key, it
  must be rewritten or dropped during the same transaction.

Default answer for v1: refuse rename while any ticket, transition, gate, queued
notification, or notification-state row references the old stage. That is less
convenient but much safer than an incomplete in-place rewrite.

### Remove Stage

Allowed only when:

- No ticket currently has that state.
- No transition references it as `from_stage` or `to_stage`.
- No stage references it as `gate_skip_to`.
- No notification state or queued notification references it.
- It is not the only terminal state and does not break terminal reachability
  from any non-terminal stage.

Live-ticket behavior:

- Existing tickets are unchanged because removal is refused while referenced.
- To remove an occupied stage, the director must first route or bulk-migrate
  tickets to a different valid stage using an explicit operation that names the
  destination and checks assignee ownership.

### Reorder Stages

Allowed when:

- The submitted order contains every existing stage exactly once, or the
  submitted rank move is converted to such an order in a transaction.
- Ranks remain contiguous and unique.
- No transition semantics are inferred from rank. Rank is display order only.

Live-ticket behavior:

- Tickets are unchanged.
- Notifications are unchanged.
- Browsers reload or refresh workflow metadata so columns reorder without
  reclassifying tickets.

## Invariants For Any Author-Defined Workflow

These are the minimum invariants before arbitrary stages are safe:

- Every `tickets.state` references an existing workflow stage.
- Every `tickets.assignee` references an existing role, with `unassigned`
  present as a builtin role.
- If a stage has non-empty owner roles, a ticket in that stage is assigned to an
  owner role or `unassigned`.
- Every owner role and transition-allowed role exists.
- Every non-terminal stage can reach at least one terminal stage through
  configured transitions.
- Every non-terminal stage has at least one outbound transition.
- Every non-initial, non-terminal stage has at least one inbound transition.
- Every non-terminal stage has an explicit notification policy. A stage with
  actionable work must notify either the ticket assignee or a fixed existing
  role, or use an explicit owner-fallback rule whose owner roles are all valid;
  a stage with `notify_kind = 'none'` is intentionally unmanaged by the
  notification system and should be visible as such in admin UI.
- Terminal stages are terminal: no ordinary actor operation should move tickets
  out of them. If reopen is desired, mark it explicitly as a reopen operation
  and keep it visible in the transition table.
- Entry gates must be acyclic: a gate skip cannot loop back to the same stage
  or produce a gate-only cycle.
- Actor authorization is defined by workflow data and operation-policy data,
  not by Python literals or unit environment.
- Director override, owner-scoped transitions, and trusted internal operations
  remain explicit flags, not inferred from names.

Enforce these with `SECURITY DEFINER` workflow-management functions rather than
ad hoc SQL. The functions should validate the entire graph in the same
transaction as a mutation. A deferred constraint trigger can protect direct SQL
changes, but the supported write path should be:

- `ticket_board.add_project_role(...)`
- `ticket_board.add_workflow_stage(...)`
- `ticket_board.configure_workflow_transition(...)`
- `ticket_board.remove_workflow_stage(...)`
- `ticket_board.reorder_workflow_stages(...)`

The HTTP layer should expose these as director-only action routes. The database
functions must still enforce the same rules because scripts and migrations can
otherwise bypass Python.

## Frontend Metadata

The frontend must treat `/api/board` workflow metadata as authoritative. Today
`TicketBoardApp.board()` returns:

- `columns`: `SELECT name, display_label FROM workflow_stages ORDER BY rank`
- `states`: the ordered stage keys derived from those columns

The JS already prefers `state.columns` in `boardColumns()` and falls back to
`DEFAULT_STATE_LABELS` only when columns are absent. For arbitrary tenant
stages:

- Keep `columns` required in the board response for Postgres boards.
- Include `owner_roles`, `is_terminal`, `entry_gate_field`, and available
  actions per stage/ticket as metadata before exposing author-defined stages in
  the UI.
- Include notification metadata (`notify_kind`, and `notify_target_role` when
  fixed) in the director/admin workflow editor so a new stage cannot be created
  without choosing how work in that stage reaches a human.
- Downgrade `DEFAULT_STATE_LABELS` to a last-ditch offline fallback for pgu
  development, not tenant truth.
- Add a metadata-version/build-id style refresh signal when workflow config
  changes, so open browsers learn newly added stages and reordered columns.
- Stop tests from asserting that the compiled fallback contains the whole
  workflow for every tenant; instead, assert DB columns win over fallback labels.

## Role-And-Operation Configuration vs Arbitrary Stages

MEFP's archivist request is one data point, not a trend. The current sample is
`n=1`: that request was really "new role owns this operation", not "new stage".
PGU-821's VCS close role satisfies the archivist need without making stage names
arbitrary: the project can route final-signoff work to a VCS-owned close path,
and `mark_done` is restricted to that role.

That suggests a cheaper first product:

- Make roles addable mid-flight.
- Make per-operation allowed roles configurable mid-flight.
- Make per-stage owner roles configurable mid-flight.
- Provide a few predesigned optional stages, such as VCS, QA, or UAT, before
  allowing arbitrary stage creation.

This would likely satisfy near-term tenant needs with less risk. Arbitrary
stages should wait until graph validation, UI metadata refresh, and
self-service privilege boundaries are implemented.

## Privilege And Self-Service

The director comment on PGU-822 is the hard part. Converting CHECK constraints
to FKs makes roles and stages data, but it does not make them self-service.
Current post-provision role changes require privileged work:

- Applying SQL as a database administrator.
- Installing or rewriting a system unit.
- Running `systemctl daemon-reload`.
- Restarting the tenant board unit.

The safe direction is to remove those privileged steps from routine workflow
mutation:

- The board process should load assignees, caller roles, operation role maps,
  workflow metadata, and notification routing from Postgres at request time or
  from a short-lived cache with explicit invalidation.
- Adding a role to the board should be a DB transaction, not a systemd unit
  rewrite.
- Restarting a tenant's own board unit can be narrowly polkit-scoped if still
  needed, but installing arbitrary unit files cannot be safely delegated to a
  tenant director.
- Pane creation remains host/runtime provisioning. It can be a separate
  operator-mediated action or a narrowly scoped launcher service that only
  writes inside the tenant's configured project/control directories.

So the proposal recommendation is: do not grant tenant directors general unit
installation. Build DB-backed workflow/role config first; keep host runtime
provisioning behind existing operator-controlled switchyard paths until there
is a deliberately scoped helper.

## Migration Path For Live Boards

### Phase 0: Observe And Guard

- Add read-only tests that compare the current literal constraints with
  `workflow_stages` and configured roles.
- Inventory stage-name literals in `schema.sql`, starting with
  `ticket_board.transition_target_role(...)`, gate routing, stale-work scans,
  nudge text, and action functions. Treat that inventory as the conversion
  backlog.
- Add graph-validation queries that run against pgu, mefp, and otto without
  mutating them.
- Add a guard query proving every actionable stage has an explicit notification
  policy and an existing target role when the policy is `fixed_role`.
- Record current role/stage/transition snapshots for rollback.

### Phase 1: Seed Roles And Notification Routing

- Create `project_roles`.
- Seed pgu roles from current builtins.
- Seed mefp and otto roles from each tenant's generated plan/config plus any
  existing ticket assignees.
- Insert builtin `unassigned`, `director`, and `user` where applicable.
- Add triggers validating array role references before changing constraints.
- Add `workflow_stages.notify_kind` and `notify_target_role`.
- Seed the existing `transition_target_role(...)` behavior as stage data for
  every live board.

### Phase 2: Convert Constraints

- Replace `tickets.state` literal CHECK with FK to `workflow_stages(name)`.
- Replace `workflow_stages.name` literal CHECK with a format CHECK.
- Replace `tickets.assignee` literal CHECK with FK to `project_roles(name)`.
- Keep existing stage rows and transition rows unchanged.
- Replace `ticket_board.transition_target_role(...)` with a lookup against
  workflow-stage notification data, and keep tests that prove an unknown or
  unconfigured non-terminal stage cannot be added.
- Continue converting the other recorded stage-name literals only after each
  replacement has an equivalent data-backed rule and regression coverage.

### Phase 3: Move Runtime Role Config Out Of Units

- Move `TICKET_BOARD_ASSIGNEES`, `TICKET_BOARD_CALLER_ROLES`, and
  `TICKET_BOARD_OPERATION_ALLOWED_ROLES` into DB-backed tables or functions.
- Make server-side authorization read those tables.
- Keep environment values only as bootstrap/default seed data for pgu
  development.
- Prove changing a role/operation in DB takes effect without unit file writes.

### Phase 4: Add Managed Mutator APIs

- Add director-only HTTP actions backed by database functions for add role,
  configure stage owners, configure operation roles, reorder stages, and add
  stage.
- Keep rename/remove refused by default while referenced.
- Add full graph validation and mutation tests.

### Phase 5: Tenant Rollout

- Run the migration on otto or mefp first because they are simpler and less
  critical than pgu. Keep workflow shape unchanged; prove rollback on at least
  one real tenant before touching the team coordination board.
- Run pgu after one tenant migration has succeeded and the graph/notification
  guard queries have been checked against pgu's larger live ticket set.
- For mefp archivist, use the role/operation path first: add the role, set the
  VCS close role, and verify close end to end.
- Only after collecting more real requests, evaluate whether any tenant still
  needs a genuinely new stage rather than a role/operation change.

## Recommendation

Do not implement arbitrary stage creation as the next ticket. Implement the
role-and-operation half first, with DB-backed project roles, notification
routing, and operation role maps that no longer require unit rewrites. Then add
graph-validated stage mutation behind director-only actions.

This keeps the database guarantees that caught recent routing mistakes while
moving toward Eric's request. It also gives the team evidence from real tenant
requests before exposing every director to full graph design on day one.
