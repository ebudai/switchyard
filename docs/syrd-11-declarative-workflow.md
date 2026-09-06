# Declarative workflows (SYRD-11)

The opt-in `switchyard.workflow.v1` document makes inspector one instance of a
reusable reviewer. Installing `pgu921_syrd11_declarative_workflow.sql` leaves
unconfigured tenants on the existing workflow. Activation is an authenticated
Director operation. A later reviewer, stage, label, gate, or transition changes
through data in the running board; neither another SQL function nor another
board release is needed.

## Configuration contract

`examples/workflows/inspection.json` is a complete example for `cerulean`.
Replace its project and absolute onboarding path when creating a tenant.

- `roles`: stable name, display label, kind, active flag, bounded capabilities,
  optional runtime/target, optional visible slot and SessionStart onboarding.
  Runtime is one of Claude, Codex, agy, or Hermes. A target must be exactly
  `<project>-<role>:0.0`. A template role copies existing trusted local runtime
  arguments; the document itself cannot supply commands or environment values.
  Identity and notification target do not depend on a visible slot.
- `stages`: ordered names, labels, owners, kind, optional gate/skip target and
  signoff, terminal flag, and explicit notification policy (`none`, `assignee`,
  `fixed_role`, or `stage_owner_fallback`). Silence is deliberate. Exactly one
  implementation stage is supported; its owners must be implementers.
- `flags`: boolean gate/signoff definitions. Existing flag columns retain their
  meaning; additional flags live in `tickets.workflow_flags`. Protected ticket
  fields cannot be repurposed as flags. Signoffs never default to approved.
- `transitions`: from/to/action, label, allowed actors, owner scope, primitive,
  commit/reason requirements and explicit signoff reset list. Primitives are
  move, approve, return, and reopen. A no-code implementation submission must
  explicitly allow no code and require a reason. Otherwise commit checks use
  the real configured Git verifier and published source preflight.
- `queue`: an optional ungated holding stage and valid owner for serial work.
  A tenant can use `analysis/director` without adding backlog. Without a queue,
  a second reserved assignment is rejected. Configured routing is explicit;
  legacy automatic triage/queue activation does not bypass declared actions.
- `reassign`: deliberate stage-to-owner changes, used to retire designer's draft
  ownership. `remove_stages` explicitly names empty stages to remove. Omission
  alone is rejected, including unrelated/custom stages. Occupied historical
  stages and outstanding review obligations cannot be removed.

The database keeps attributed configuration revisions and role identities,
validates graph references and terminal reachability, replaces ranks within one
transaction, and enforces state/role references. Historical roles remain in the
registry as inactive. Invalid graphs, foreign project targets, revision races,
or orphaned owners roll back. Tickets and comments are retained. Gate changes
are Director-only; signoff changes require the declared approval action.

Return actions use the persisted implementation owner. Their explicit reset
list determines which reviews must run again; previously signed gated reviews
are skipped when their signoff was preserved. A held approval records its
signoff while retaining stage/assignee and automatically creates a durable
awaiting-role handoff to the resolved next owner (Director when that would be
self). The existing SYRD-15 generation, bounded retry/escalation, ACK and
resolution rules apply. Repeating a held decision does not reopen a resolved
wait. Director can release the hold and the reviewer can advance the recorded
decision. SYRD-22 separately covers this behavior on legacy boards.

The API exposes configuration and applicable action metadata, and its change
signature includes configuration revision. The UI renders generic actions and
gates; the listener refreshes declared targets and runtimes without a restart.
Both continue using existing caller registration/token boundaries.

## Supported operations

Run these commands from the project's authenticated Director context, using the
release's `scripts/ticket-board-workflow` entry point. It uses existing
`TICKET_BOARD_URL`, caller registration/socket, and write-token conventions.
It never assumes the caller is Director merely because it is a management CLI.

```sh
scripts/ticket-board-workflow show --board-url http://127.0.0.1:23326
scripts/ticket-board-workflow validate --config /path/to/project.json --document /path/to/workflow.json
scripts/ticket-board-workflow apply --config /path/to/project.json --document /path/to/workflow.json --expected-revision 0 --rollback-document /path/to/baseline.json --dry-run
scripts/ticket-board-workflow apply --config /path/to/project.json --document /path/to/workflow.json --expected-revision 0 --rollback-document /path/to/baseline.json --journal /path/to/before.json
scripts/ticket-board-workflow rollback --config /path/to/project.json --journal /path/to/before.json --dry-run
```

First activation requires a reviewed explicit baseline for rollback; disabling
configuration into an implicit legacy graph would lose integrity guarantees.
Both baseline and desired document receive database preflight. Later updates
capture the previous configured revision automatically. Rollback is a new,
attributed configuration revision, retains later historical role identities,
and refuses to orphan work created since the snapshot. If newly required
inspection is outstanding, complete it or deliberately change that ticket's
gate before removing the stage; rollback does not waive review automatically.

The host projection preserves unrelated top-level metadata, including desktop
policy and presentation-controller fields. It updates launcher JSON, generated
layout, saved plan/workflow JSON and existing project artifact. Retired runtime
metadata remains under `retired_workflow_roles`. Apply starts/stops no sessions. Initial privileged provisioning, with or without an initial workflow, assigns only
generated workflow control files and their directory to the tenant; later
management requires no recurring ownership change. Existing root-owned
controls require that one-time operator preparation.
Files are journaled and atomically replaced after the database transaction. If
local writes fail, the error reports the committed board revision; rerun the
same document to repair projection. This is a recoverable two-resource operation,
not a fictitious atomic transaction across PostgreSQL and the filesystem.

Fresh provisioning accepts `--workflow-config` on both `switchyard new` and
`team-launcher <project> new`. Reprovisioning from an artifact reads its saved
workflow. Existing legacy add-role/VCS-close commands reject configured tenants;
use the complete validated workflow operation instead.

```sh
scripts/ticket-board-write workflow-action CER-42 accept_verification
scripts/ticket-board-write set-workflow-flags CER-42 --patch-json '{"needs_verification":true}'
scripts/ticket-board-workflow prepare-role --config /path/to/project.json --role inspector
scripts/team-launcher cerulean pane attach-or-start inspector --config /path/to/project.json
```

Preparation validates project targets before any host work and uses the existing
owner environment, desktop readiness, worktree, hook and first-run trust paths.
It prepares only the requested worktree/runtime and starts no session. A vendor
login/trust warning is an activation failure requiring completion in that role's
terminal. SessionStart reads `TICKET_BOARD_ROLE_ONBOARDING` and injects the actual
remit; setting an environment variable alone is not the acceptance evidence.

## Validation and activation boundary

`ticket_board_declarative_workflow_test.py` uses disposable PostgreSQL, HTTP,
the real served UI, management CLI, and a private tmux socket. It covers gates,
permissions, original return owner, held approval and idempotent resolution,
second custom reviewer, renamed implementation stage, explicit removals, legal
five-stage queues, CAS/dry-run/rollback and actual ticket text receipt. Run with
`--upgrade` for repeated incremental installation. The launcher test executes an
exported release with inspection enabled/disabled and checks slots, retired
metadata, prepared-role isolation and actual SessionStart remit content. Vendor
authentication is a fixture there, not a claim of live Claude acceptance.

Publication is followed by independent Audit and Director-controlled deployment.
Live services, protected configuration, shared releases and working panes are
outside Main's implementation actions. The SYRD-11 handoff contains the syrd
candidate/baseline and exact operator commands. Acceptance requires the served
release marker, successful inspector trust/start, a real ticket delivery and
visible attachment in the former designer terminal. SYRD-12 supplies the full
runtime presentation controller; SYRD-23 owns legacy direct batch-sender cleanup.
