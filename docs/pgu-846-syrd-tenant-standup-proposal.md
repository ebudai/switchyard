# PGU-846: syrd Tenant Stand-Up Proposal

This is a proposal only. It does not provision `syrd`, move tickets, or create
new tickets. The current direction is to action the remaining Switchyard work
before handoff, so the migration set should be a rule-driven final review, not
a stale 26-ticket inventory.

## Fixed Decisions

- Tenant slug: `syrd`.
- HTTP port: `23326`.
- Port rule: `18770 + (blake2s(slug, digest_size=2) mod 10000)`.
- Verified reference ports: `mefp=26623`, `otto=20740`, `syrd=23326`.
  PGU's `8770` is a hardcoded compatibility special case, not an output of
  this formula.
- Ticket prefix: `SYRD`.
- PGU remains the historical archive. Closed PGU tickets stay where they are so
  existing `PGU-NNN` references in comments, bodies, audits, and handoff notes
  keep resolving without a risky rewrite.

## Migration Rule

At cutover, migrate only non-terminal PGU tickets whose primary remaining work
belongs to Switchyard rather than the game. If the backlog is actioned first,
this set should be near-empty.

Use these rules during the final reviewed list:

- Move: unresolved Switchyard platform work for the board, launcher,
  provisioning, tenant workflow, notifications, pane coordination, onboarding,
  shared install, or cross-tenant reporting.
- Stay on PGU: renderer, galaxy, visual QA, capture, game app behavior, PGU-only
  operating rules, and work already effectively completed on PGU.
- Do not move terminal tickets: `done` and `cancelled` remain in PGU as archive
  history.
- Borderline tickets move only when the next action would naturally be owned by
  the dedicated Switchyard team. If the next action is to verify a PGU branch,
  merge PGU work, or document PGU-specific behavior, finish it on PGU.
- Tickets in review stages should normally finish their current review on PGU.
  Avoid moving work mid-audit unless the director explicitly freezes the review
  and copies the audit context into the new `SYRD-N` ticket.

The final migration list should be reviewed immediately before cutover and
should include one line of reasoning for every borderline ticket. Do not build
that list until separation is complete and the live open set has settled.

## Migration Mechanism

The cross-tenant fields already exist and are used in production. A migrated
ticket on `syrd` should be created with:

- `origin_project="pgu"`
- `external_source_ref="pgu:PGU-NNN"`

The PGU original should receive a final forward pointer comment naming the new
`SYRD-N` ticket and URL. Then close the PGU original as a superseded transfer,
not as completed implementation work. If a first-class `migrated` terminal
state exists by then, use it; otherwise use the existing terminal path the
director chooses for "superseded elsewhere" and include the forward pointer in
the close reason.

This keeps both directions resolvable:

- From `syrd`: the ticket detail shows the PGU origin metadata.
- From PGU: the archival ticket comment points to the new `SYRD-N`.
- Existing historical references such as `PGU-820` remain untouched and continue
  to resolve on the PGU board.

## Environment Vocabulary

Eric expected the environment vocabulary to change. For the tenant-critical
board and pane variables, it mostly does not. The launcher and board clients
prefer neutral names and keep `PGU_*` as legacy fallbacks.

For `syrd`, set neutral names:

- `TICKET_BOARD_PROJECT=syrd`
- `TICKET_BOARD_TICKET_PREFIX=SYRD`
- `TICKET_BOARD_URL=http://127.0.0.1:23326`
- `TICKET_BOARD_SOCKET=/run/syrd-ticket-board/ticket-board.sock`
- `TICKET_BOARD_PANE_STATE_DIR=...`
- `TICKET_BOARD_PANE_SESSION_DIR=...`
- `TICKET_BOARD_CALLER_ROLE=...`
- `TICKET_BOARD_WRITE_TOKEN=...`
- `TICKET_BOARD_IMPLEMENTER_ROLES=...`
- `TICKET_BOARD_ASSIGNEES=...`

Checked exceptions:

- `PGU_TICKET_BOARD_BUILD_ID` has no neutral equivalent today. It is
  load-bearing: the server reads it and emits `window.PGU_TICKET_BOARD_BUILD_ID`
  for stale-build detection in the client. Until this is renamed, a `syrd` board
  will still serve a PGU-named browser global for its build identity.
- `PGU_TICKET_BOARD_REFRESH_IDLE_MS` has no neutral equivalent today. It is also
  load-bearing: the client reads `window.PGU_TICKET_BOARD_REFRESH_IDLE_MS` to
  decide when an idle browser may auto-refresh after a build changes.
- `PGU_TICKET_BOARD_TICKET_PREFIX` has a neutral equivalent:
  `TICKET_BOARD_TICKET_PREFIX`. It is only a legacy fallback in the app; the
  provisioner and launcher emit the neutral variable.
- `PGU_ALLOW_MAIN_PUSH` has a neutral equivalent: `ALLOW_MAIN_PUSH`. It remains
  accepted by the update hook and inspector git guard for legacy PGU
  deployments. It is load-bearing as a compatibility override for old PGU
  hooks, but it should not become syrd tenant vocabulary.
- `PGU_TICKET_BOARD_URL`, `PGU_TICKET_BOARD_SOCKET`,
  `PGU_TICKET_BOARD_CALLER_ROLE`, `PGU_TICKET_BOARD_WRITE_TOKEN`,
  `PGU_TICKET_BOARD_PROJECT`, `PGU_TICKET_BOARD_PANE_STATE_DIR`,
  `PGU_TICKET_BOARD_PANE_SESSION_DIR`, `PGU_PANE_TARGET`, and
  `PGU_PANE_SESSION_ID` all have neutral `TICKET_BOARD_*` equivalents.
- Launcher/operator compatibility names such as `PGU_TEAM_LAUNCHER_*`,
  `PGU_HOST_WAYLAND_DISPLAY`, `PGU_UPDATE_HOOK_*`, and
  `PGU_INSPECTOR_GIT_GUARD_*` also have neutral forms in the current scripts.
  They are compatibility debt, not a reason to introduce `SYRD_*` variables.
- `PGU_MERGE_GATE_TRACE_ROOT` and `PGU_MERGE_GATE_TRACE_DIR` have no neutral
  equivalents in `merge-gate-helper`. They are workflow support diagnostics,
  not tenant identity or normal board runtime configuration.
- `PGU_WAYLAND_SERVICE_NAME`, `PGU_WAYLAND_GUI_USER`, and
  `PGU_WAYLAND_AGENT_USER` have no neutral equivalents in the old Wayland
  clipboard helper. They are PGU desktop-helper vocabulary, not tenant-critical
  Switchyard board configuration.
- Renderer and PGU app diagnostic variables are out of scope for a Switchyard
  tenant and should not be carried into `syrd`.

Conclusion: `syrd` should use the existing neutral `TICKET_BOARD_*`,
`TEAM_LAUNCHER_*`, `HOST_WAYLAND_DISPLAY`, `UPDATE_HOOK_*`, and
`ALLOW_MAIN_PUSH` names. No `SYRD_*` vocabulary is needed, but the two
load-bearing build-refresh globals should get neutral `TICKET_BOARD_*` names in
a small follow-up so the extracted Switchyard UI no longer serves PGU-named
globals.

## In-Flight Work at Cutover

The safest cutover is a quiet window:

- no Switchyard ticket in `inspection`, `audit`, `user_review`, or
  `director_review`;
- no implementer branch waiting on a PGU-only merge decision;
- no unmerged migration/provisioning changes in the Switchyard extraction path;
- no board-created notification depending on the old PGU tenant.

If work is in progress:

- Near completion: finish it on PGU, submit through PGU audit/director review,
  and migrate only the follow-up work if any remains.
- Mid-implementation: freeze new PGU work, create the `SYRD-N` replacement with
  the PGU origin metadata, copy the current implementation/audit context into
  the new ticket body or first comment, and close the PGU original with a
  forward pointer.
- Mid-audit: prefer to finish the audit on PGU. If unavoidable, the director
  must explicitly carry findings, blockers, commit hash, and review state into
  the new `SYRD-N`; do not silently reset review obligations.

## Handoff Document Shape

Create one short Switchyard-specific handoff document, separate from
`docs/onboarding/`. It should be short enough for a new director to read before
touching the board.

Recommended sections:

1. What Switchyard owns: board, workflow engine, pane launcher, tenant
   provisioning, shared install, cross-tenant reporting, and operator handoff.
2. Live tenant map: PGU archive, `syrd` board URL/port/socket/prefix, known peer
   tenants such as `mefp` and `otto`, and where each tenant's source of truth
   lives.
3. Current state: what has shipped, what is still gated, what must not be
   provisioned twice.
4. Decisions to preserve, with reasons:
   - Shared install is immutable releases under `/opt/switchyard`; it never
     self-deploys. Freshness comes from the operator upgrade flow and a commit
     marker, not from a writable git checkout in `/opt`.
   - Workflow vocabulary moves toward FK-over-CHECK so stages and roles become
     tenant data. This is deliberately gated because it changes integrity,
     notification routing, and admin operations.
   - "No notification" must be explicit. A new stage must never inherit `NULL`
     routing and silently notify nobody.
   - PGU remains the historical archive so cross-references keep resolving.
   - The VCS close role must not be an implementer; final source-control close
     is a separate accountability step.
   - PGU-820's invariant remains: an assignee must be an owner of the stage it
     sits in.
5. Open work and migration status: the reviewed migration list, near-empty if
   actioning finished first, plus links back to the PGU originals.
6. Operating rules: use neutral env variables, route through the board, do not
   hand-edit ticket storage, do not self-deploy shared releases, and do not
   create cross-tenant tickets without origin metadata.
7. First-day checklist: verify board health, verify prefix allocation, verify
   pane role env, verify notification routing, verify update path, then accept
   new work.

## Switchyard Agent Instruction Files

The extracted repo currently has no `AGENTS.md`, `GEMINI.md`, or `CLAUDE.md`.
That is better than copying PGU's renderer-heavy instructions, but the
Switchyard team still needs repo-local rules.

Proposed contents:

- `AGENTS.md`: Codex/implementer contract. Include board read/write tools,
  branch and submit rules, no direct JSON/SQL writes, no provisioning or
  migration without a ticket, neutral env vocabulary, immutable shared-install
  policy, owner-of-stage assignee invariant, and "no notification must be
  explicit." Exclude PGU render/window-launch guidance.
- `GEMINI.md`: inspector/auditor contract. Include evidence requirements,
  regression-test expectations for bug fixes, no state edits outside board
  commands, audit/kickback reporting through tickets, and workflow/notification
  checks. Exclude game visual QA instructions.
- `CLAUDE.md`: Claude-pane contract. Keep it a short mirror of the board and
  branch rules plus any Claude-specific session/hook expectations. Prefer
  pointing to `AGENTS.md` for shared rules so the three files do not diverge.

These files should be part of the handoff because they determine how the new
team behaves before any director has time to restate the rules verbally.
