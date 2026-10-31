# Documentation map

Which Switchyard documents are maintained and normative, which are historical
records, and which still need review. This is the seed for SYRD-716's
reconciliation: it inventories what exists on main, without rewriting any of it.
The Librarian owns the reconciliation that follows, and keeps this map current.

**How to read a document's status:**
- **Normative** documents describe how Switchyard behaves now, and are
  corrected when it changes.
- **Historical** documents record why something was built, or what was
  measured at the time. They may describe behaviour that has since changed. Do
  not act on them without checking a normative surface.
- Where the two disagree, the normative surface, the code, and the CLI's own
  `--help` win.

"Last changed" is the date of the file's last commit on main when this map was
written. It is evidence of age, not of accuracy.

## Start here

| Reader | Read | For |
|---|---|---|
| Anyone | [`README.md`](../README.md) | What Switchyard is, and where to go next |
| Operator, new host | [`docs/fresh-machine-install.md`](fresh-machine-install.md) | Installing Switchyard and its prerequisites |
| Every role | [`docs/onboarding/README.md`](onboarding/README.md) | The onboarding packet, in reading order |
| Every role, exact commands | `switchyard --help`, `ticket-board-read --help`, `ticket-board-write --help`, `directorctl --help` | Flags and verbs as the installed release has them |

## Normative: role and LLM-facing

These are the surfaces a role reads while working. They are kept short and
exact.

| Surface | What it is | Last changed |
|---|---|---|
| [`skills/switchyard-board/SKILL.md`](../skills/switchyard-board/SKILL.md) | Every role's board manual: reading, legal moves, writing, provenance, blockers, background commands | 2026-10-25 |
| [`skills/switchyard-director/SKILL.md`](../skills/switchyard-director/SKILL.md) | The Director's overlay on the board skill | 2026-10-25 |
| [`skills/switchyard-librarian/SKILL.md`](../skills/switchyard-librarian/SKILL.md) | The Librarian's overlay on the board skill | 2026-10-21 |
| [`docs/board-skill.md`](board-skill.md) | How one canonical skill body is projected into each agent CLI | 2026-10-21 |
| [`docs/onboarding/`](onboarding/README.md) | The packet `switchyard new` copies into each project: methodology, board guide, Director guide, Librarian guide | 2026-10-26 |
| A ticket's `workflow_actions` (`ticket-board-read ticket <id> --json`) | Which transitions exist right now, and who may take them | live |

`switchyard new` also generates `DIRECTOR_ONBOARDING.md` and
`DESIGNER_ONBOARDING.md` in each project (`scripts/project_onboarding.py`). They
are generated, so correct their source rather than a copy.

## Normative: operator guides and runbooks

| Document | Scope | Last changed |
|---|---|---|
| [`docs/team-launcher.md`](team-launcher.md) | The `switchyard` command, launcher, panes, roles, first-run auth, lifecycle verbs | 2026-10-31 |
| [`docs/ticket-board-service.md`](ticket-board-service.md) | Deploying and operating a tenant's board service | 2026-10-21 |
| [`docs/board-supervisor.md`](board-supervisor.md) | The host board supervisor that runs every tenant board | 2026-10-12 |
| [`docs/ticket-board-project-provisioning.md`](ticket-board-project-provisioning.md) | Per-project board provisioning artifacts | 2026-10-24 |
| [`docs/fresh-machine-install.md`](fresh-machine-install.md) | Fresh-host install and version notices | 2026-10-30 |
| [`docs/switchyard-new-host-dependencies.md`](switchyard-new-host-dependencies.md) | What `switchyard new` needs from the host | 2026-09-22 |
| [`docs/role-runtime.md`](role-runtime.md) | `switchyard set-role-runtime`: changing a role's agent CLI | 2026-10-27 |
| [`docs/runtime-presentation.md`](runtime-presentation.md) | `switchyard present`: which role sessions a window shows | 2026-09-21 |
| [`docs/desktop-access.md`](desktop-access.md) | Desktop policy: screenshots and clipboard, or headless | 2026-10-18 |
| [`docs/user-notifications.md`](user-notifications.md) | Phone notifications for the User (Pushover) | 2026-10-31 |
| [`docs/git-update-hook.md`](git-update-hook.md) | Repository policy hooks on managed repositories | 2026-09-24 |
| [`docs/testing.md`](testing.md) | Running the test suites | 2026-09-06 |
| [`CONTRIBUTING.md`](../CONTRIBUTING.md) | Where and how to contribute | 2026-09-06 |
| [`examples/workflows/inspection.json`](../examples/workflows/inspection.json) | An example declared workflow | 2026-10-20 |

## Needs review

These read as operational guidance, but their own text ties them to one
cutover, host or date. The Librarian decides whether each is still normative,
should be folded into a guide above, or is historical.

| Document | Why it needs review | Last changed |
|---|---|---|
| [`docs/switchyard-director-handoff.md`](switchyard-director-handoff.md) | A Switchyard-specific handoff "for the incoming director", written for one moment | 2026-09-07 |
| [`docs/ticket-board-boardsvc-peer-auth-runbook.md`](ticket-board-boardsvc-peer-auth-runbook.md) | States it is a prep-only package for one supervised cutover (PGU-197) | 2026-09-03 |
| [`docs/tailscale-resolved-nm-runbook.md`](tailscale-resolved-nm-runbook.md) | One host DNS contract (PGU-583) | 2026-08-22 |
| [`docs/ticket-board-collation-maintenance.md`](ticket-board-collation-maintenance.md) | A PostgreSQL collation-version procedure | 2026-08-22 |

## Historical records (not normative)

| Records | What they are |
|---|---|
| `docs/syrd-*.md` (128 files), `docs/pgu-*.md` (14 files) | One per ticket: what happened, the design chosen, and the evidence at the time. Normative guides link to them for the why. |
| [`docs/switchyard-project-setup-design.md`](switchyard-project-setup-design.md) | A design draft; its own status line reads "DRAFT … Nothing here is implemented." |
| [`docs/board_workflow_engine_design.md`](board_workflow_engine_design.md) | A design spike (PGU-516), "no implementation" |
| [`docs/refactor/`](refactor/) | The SYRD-272 launcher refactor's inventory and final report |
| [`docs/size-review/`](size-review/) | The carried size exceptions passed to `ticket-board-write enable-size-review --carried-file` (SYRD-541): data, not prose |
| [`docs/director-handoff.md`](director-handoff.md), [`docs/director-handoff-2026-06-28-packet-36-36.md`](director-handoff-2026-06-28-packet-36-36.md), [`docs/director-handoff-2026-07-02-current.md`](director-handoff-2026-07-02-current.md) | Handoffs from a different, unrelated project, as their own provenance notes say |
| [`docs/validator_audit.md`](validator_audit.md) | A retroactive audit report of another codebase's commit |
| [`docs/otto-milestone-1-hand-standup.md`](otto-milestone-1-hand-standup.md) | One dated standup runbook (PGU-590, PGU-601) |
| [`docs/board-ui-functional-audit-PGU-504.md`](board-ui-functional-audit-PGU-504.md) | One dated board UI audit |
| [`docs/Sample_Implementer_Prompt.md`](Sample_Implementer_Prompt.md), [`docs/audit_is_ready_prompt.md`](audit_is_ready_prompt.md) | Prompt samples from earlier packets |
