# Switchyard Director Handoff

This is the Switchyard-specific handoff for the incoming director. It does not
replace `docs/onboarding/`; use onboarding for the generic director method and
this document for the project state, tenant map, and decisions that should not
be rediscovered.

## What Switchyard Owns

Switchyard owns the coordination system around the work, not the PGU game
renderer. Its surface is the ticket board, workflow engine, pane launcher,
tenant provisioning, immutable shared install, cross-tenant references,
notification routing, update hooks, and operator handoff.

PGU-specific renderer work, galaxy rendering, capture quality, and visual QA
belong to the PGU/game team unless the ticket is explicitly about board or
launcher machinery.

## Tenant Map

- `pgu` is the ARCHIVE board. It remains available at `http://127.0.0.1:8770`
  with prefix `PGU` and keeps the historical record, including roughly 800
  closed tickets. Act on PGU only when the work is still PGU-owned or the
  archive ticket needs a forward pointer.
- `syrd` is the planned LIVE Switchyard board. Its slug is `syrd`, prefix is
  `SYRD`, port is `23326`, and socket should be
  `/run/syrd-ticket-board/ticket-board.sock`. New Switchyard work goes there
  after cutover.
- `mefp` is a live peer tenant on port `26623`.
- `otto` is a live peer tenant on port `20740`.

Port allocation for Switchyard tenants is
`18770 + (blake2s(slug, digest_size=2) mod 10000)`. PGU's `8770` is a
compatibility special case, not an output of that formula.

## Current State

The direction is to action the remaining Switchyard backlog on PGU before
standing up `syrd`, so the final migration set should be small. As of this
handoff, `/etc/switchyard/projects` lists `mefp` and `otto`; `syrd` has not
been provisioned.

Do not provision `syrd` twice. Before any cutover action, verify the registry,
systemd units, board port, socket path, ticket prefix, and pane environment.
Use neutral `TICKET_BOARD_*`, `TEAM_LAUNCHER_*`, `UPDATE_HOOK_*`, and
`ALLOW_MAIN_PUSH` names. Do not introduce `SYRD_*` vocabulary. The known
residual cleanup is that `PGU_TICKET_BOARD_BUILD_ID` and
`PGU_TICKET_BOARD_REFRESH_IDLE_MS` still need neutral browser-global names.

## Migration Rule

Migrate only non-terminal PGU tickets whose remaining work belongs to
Switchyard. Terminal tickets stay on PGU. Review-stage tickets normally finish
their current review on PGU. Borderline tickets move only when the next action
would naturally belong to the dedicated Switchyard team.

For each migrated ticket, create the `SYRD-N` record with
`origin_project="pgu"` and `external_source_ref="pgu:PGU-NNN"`. Add a final
comment on the PGU original pointing to the new `SYRD-N`, then close the PGU
ticket as superseded elsewhere. This keeps both directions resolvable and
leaves existing `PGU-NNN` references untouched.

## Decisions To Preserve

1. The shared install is immutable releases, not a writable checkout.
   Reason: shared code in `/opt/switchyard` must be auditable and repeatable.
   Freshness comes from the operator upgrade flow and commit markers; a
   self-deploying checkout would make production state depend on whoever last
   ran git in `/opt`.

2. Workflow vocabulary moves toward FK-over-CHECK, and the rollout is gated.
   Reason: stages and roles are tenant data, but changing that integrity model
   affects routing, notifications, admin tools, and migration. Gating keeps a
   schema cleanup from becoming an unbounded workflow change.

3. "No notification" must be explicit.
   Reason: a new stage must not inherit `NULL` routing and silently notify
   nobody. Silence is a real routing choice, so it needs to be visible in the
   workflow data.

4. PGU remains the historical archive.
   Reason: bodies, comments, audits, and handoff notes are dense with
   references such as `PGU-820`. Leaving the archive live preserves those links
   without a risky rewrite of hundreds of historical records.

5. A VCS close role is not an implementer.
   Reason: the close role owns source-control closure and provides separation
   from implementation. If it is also an `in_progress` owner, it can review or
   close work it wrote itself and it also picks up serial-focus behavior meant
   for implementers.

6. An assignee must be an owner of the stage it sits in.
   Reason: the board is the routing engine. If a role can sit in a stage it
   does not own, notifications, serial focus, and accountability all lie about
   who is expected to act.

7. Hook rollout must never guard repositories without a Switchyard workflow.
   Reason: a push-blocking hook only belongs where the workflow it names
   exists. Hardcoded repo lists can accidentally guard unrelated operator
   projects; derive guarded repositories from the Switchyard registry.

8. Privileged operations must not write as root into an operator checkout.
   Reason: root-owned files in a user's repository break their git workflow
   later, far from the command that caused it. Fetch as the repository owner
   and suppress bytecode across sudo with an explicit environment.

## Operating Rules

Use the board tools, not direct JSON edits, for ticket changes. Route work
through tickets and preserve origin metadata for cross-tenant records. Do not
self-deploy shared releases. Do not create notification behavior by omission.
Do not move review obligations across tenants without carrying findings,
blockers, commit hashes, and sign-off state explicitly.

## Agent Instruction Files

The extracted Switchyard repo should have its own `AGENTS.md`, `CLAUDE.md`, and
`GEMINI.md`. Do not copy PGU's renderer-heavy instructions.

- `AGENTS.md` should be the source of truth for Codex and implementer panes:
  board read/write commands, branch and submit rules, no hand-edited storage,
  no provisioning or migration without a ticket, neutral environment names,
  immutable shared-install policy, owner-of-stage routing, explicit
  notification policy, and no root-owned writes into operator repositories.
- `CLAUDE.md` should point at `AGENTS.md` for shared rules and contain only
  Claude-specific session or hook expectations.
- `GEMINI.md` should point at `AGENTS.md` for shared rules and add inspector
  expectations: evidence, regression tests for bug fixes, audit/kickback
  reporting through tickets, and workflow/notification checks.

Keeping `CLAUDE.md` and `GEMINI.md` as thin role overlays prevents three
instruction files from drifting while still letting each pane load the file it
expects.

## First-Day Checklist

1. Read `docs/onboarding/`, then this handoff.
2. Verify the live tenant map and that `syrd` is either absent or exactly once
   provisioned.
3. Verify `SYRD` prefix, port `23326`, socket path, board health, and pane
   role environment.
4. Verify notification routing for every workflow stage, including any explicit
   no-notification stage.
5. Verify the shared-install release marker and update path.
6. Review the final migration list immediately before cutover; do not trust an
   old inventory.
