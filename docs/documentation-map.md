# Switchyard documentation

Use this page to find the current way to operate Switchyard. It is both the
human-facing capability index and the boundary between maintained instructions
and historical evidence.

## What is authoritative

When two surfaces disagree, use this order:

1. The installed command's `--help` for its names, flags and defaults.
2. A ticket's live `workflow_actions` and the board's `/api/workflow` document
   for stages, actors, gates and transitions in that project.
3. The role skills for authority, invariants and failure handling while an LLM
   role is working.
4. The maintained guides below for explanations and procedures.

The first two are generated from the release and project that are actually
running. Do not copy a transition or option from a historical ticket record.

## Find a task

| I need to | Start here | Confirm with |
|---|---|---|
| Install Switchyard on a new host | [Fresh-machine install](fresh-machine-install.md) | `./install --help`; `sudo ./install --dry-run` |
| Create and onboard a project | [Onboarding index](onboarding/README.md), then [project provisioning](ticket-board-project-provisioning.md) | `switchyard new --help` |
| See projects and attach to a role | [Launcher: attaching](team-launcher.md#attaching-to-a-role) | `switchyard status --help`; `switchyard attach --help` |
| Start or stop a whole project | [Launcher](team-launcher.md) | `switchyard start --help`; `switchyard stop --help` |
| Route, review or move a ticket | [Board guide](onboarding/switchyard-board-guide.md) | the ticket's `workflow_actions`; `ticket-board-write <action> --help` |
| Operate as the Director | [Director guide](onboarding/switchyard-director-guide.md) | the `switchyard-director` skill and `/api/workflow` |
| Recover a role, pane or display | [Role runtime](role-runtime.md), [runtime presentation](runtime-presentation.md), and [launcher](team-launcher.md) | `switchyard status --json`; `switchyard attach`, `recover-display`, `set-role-runtime` or `start --help` |
| Upgrade a tenant | [Launcher: upgrading](team-launcher.md#upgrading-a-tenant) | `switchyard upgrade --dry-run <project>`; `switchyard release-status <project>` |
| Operate or recover a board service | [Board service](ticket-board-service.md) and [board supervisor](board-supervisor.md) | `switchyard release-status`; the service and supervisor status commands in those guides |
| Configure routing or review gates | [Board guide](onboarding/switchyard-board-guide.md) | `switchyard router-role`, `documentation-gate`, `publisher`, and `design-stage --help` |
| Publish approved work | [Board guide: Publisher](onboarding/switchyard-board-guide.md#who-may-do-what) and [board service: publishing](ticket-board-service.md#publishing-a-push-by-the-implementer) | `switchyard publisher --help`; the ticket and publication comment |
| Prepare User acceptance | [Director guide: routing and holding](onboarding/switchyard-director-guide.md#routing-and-holding) | `ticket-board-write uat-instructions --help` and the ticket's User Review action |
| Handle provider quota or a quiet role | [Director guide: quota-safe monitoring](onboarding/switchyard-director-guide.md#after-routing-yield-quota-safe-monitoring) | `ticket-board-read holds`, `cohorts`, `leases` and `stall` |
| Diagnose an incident | [Fresh-machine incident settings](fresh-machine-install.md#host-settings-for-diagnosing-an-incident), then the affected service guide | `switchyard status --json`; `switchyard release-status`; service journals |
| Configure desktop, clipboard or screenshots | [Desktop access](desktop-access.md) | `switchyard approve-desktop --help`; `switchyard paste-image --help` |
| Configure User phone notifications | [User notifications](user-notifications.md) | the setup and UAT commands in that guide |
| Change a role's agent or model | [Role runtime](role-runtime.md) | `switchyard set-role-runtime --help`; `switchyard reconcile-role-models --help` |
| Run or add tests | [Testing](testing.md) and [contributing](../CONTRIBUTING.md) | the repository's current test entry points |

For a low-context route through authority, evidence and failure handling, use
the [LLM operations reference](llm-operations.md).

## Discover what changed before an upgrade

Switchyard installs the checkout you selected; pulling a checkout does not
change the installed release. Before upgrading a tenant:

1. Read the commits and maintained-document diffs between the installed source
   and the candidate source. Ticket records explain individual changes, but
   are evidence, not current instructions.
2. Run `switchyard upgrade <project> --dry-run` from the candidate release.
   It reports the generated artifacts and migrations it would change.
3. Run `switchyard release-status <project>` to compare the shared release,
   deployed board, live build and upgrade journals.
4. Check the current help for any command you will use. New capabilities are
   discoverable from `switchyard --help`; exact flags live under the
   subcommand's own `--help`.
5. Follow [Upgrading a tenant](team-launcher.md#upgrading-a-tenant). The root
   phase and Director-owned phase are separate; do not improvise around a
   refusal or incomplete journal.

There is no hand-maintained release-note list that duplicates every command.
The command help, this task index, and the diff of maintained documents are the
upgrade discovery surfaces.

## Maintained, normative surfaces

These files describe current behavior and must change when that behavior
changes.

| Surface | Scope |
|---|---|
| [`README.md`](../README.md) | Installation, version selection and the generated top-level command list |
| [`docs/onboarding/`](onboarding/README.md) | Project-agnostic onboarding copied into new projects; board, Director, Librarian and methodology guides |
| [`skills/switchyard-board/SKILL.md`](../skills/switchyard-board/SKILL.md) | Shared role authority, board operations, evidence and wait handling |
| [`skills/switchyard-director/SKILL.md`](../skills/switchyard-director/SKILL.md) | Director-only controls and recovery procedures |
| [`skills/switchyard-librarian/SKILL.md`](../skills/switchyard-librarian/SKILL.md) | Librarian-only documentation decision contract |
| [LLM operations reference](llm-operations.md) | Small retrieval index into the skills, live workflow and command help |
| [Launcher guide](team-launcher.md) | Project lifecycle, roles, panes, provisioning and upgrades |
| [Board service](ticket-board-service.md), [supervisor](board-supervisor.md), and [provisioning](ticket-board-project-provisioning.md) | Board installation, operation, migration and tenant artifacts |
| [Fresh-machine install](fresh-machine-install.md) and [host dependencies](switchyard-new-host-dependencies.md) | Host preparation and installation |
| [Role runtime](role-runtime.md), [runtime presentation](runtime-presentation.md), and [desktop access](desktop-access.md) | Agent runtime, display slots and desktop policy |
| [User notifications](user-notifications.md) | User phone notification setup and operation |
| [Git update hook](git-update-hook.md), [testing](testing.md), and [`CONTRIBUTING.md`](../CONTRIBUTING.md) | Repository policy and contribution checks |
| [`examples/workflows/inspection.json`](../examples/workflows/inspection.json) | Executable example workflow; validate it as configuration, not prose |

The onboarding copies in a tenant carry a source-commit header. They are
snapshots, not a second source: edit `docs/onboarding/` here and let the upgrade
path refresh unchanged copies. `DIRECTOR_ONBOARDING.md` and
`DESIGNER_ONBOARDING.md` are generated by `scripts/project_onboarding.py`; fix
their generator rather than an installed copy.

## Historical and evidence records

Everything in this section is non-normative. It may accurately explain why a
decision was made while naming commands, paths or behavior that no longer
exist.

| Records | Meaning |
|---|---|
| `docs/syrd-*.md` and `docs/pgu-*.md` | Ticket-specific designs, results and rollout evidence |
| [`docs/switchyard-director-handoff.md`](switchyard-director-handoff.md) and the dated `docs/director-handoff*.md` files | Point-in-time project handoffs, tenant maps and host state |
| [`docs/ticket-board-boardsvc-peer-auth-runbook.md`](ticket-board-boardsvc-peer-auth-runbook.md), [`docs/tailscale-resolved-nm-runbook.md`](tailscale-resolved-nm-runbook.md), and [`docs/ticket-board-collation-maintenance.md`](ticket-board-collation-maintenance.md) | One-host or one-cutover runbooks retained as evidence |
| [`docs/switchyard-project-setup-design.md`](switchyard-project-setup-design.md) and [`docs/board_workflow_engine_design.md`](board_workflow_engine_design.md) | Unimplemented or superseded design drafts |
| [`docs/refactor/`](refactor/) | SYRD-272 refactor inventory and final report |
| [`docs/size-review/`](size-review/) | Machine-consumed carried-exception data, not guidance |
| `docs/validator_audit.md`, `docs/otto-milestone-1-hand-standup.md`, `docs/board-ui-functional-audit-PGU-504.md`, and the two prompt samples | Dated audits, exercises and packet samples |

If a maintained guide links to one of these, use it for rationale or evidence
only. Return here or to command help before acting.

## Maintenance rule

Add a new current procedure to an existing maintained guide whenever possible.
If a ticket needs a durable evidence record, name it with its ticket ID and
link back to the maintained guide it informed. Do not promote a ticket record
into a second command manual. When behavior retires, remove it from every
maintained surface and leave any historical account clearly in this category.
