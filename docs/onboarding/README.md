# Onboarding

Read these three, in this order. They are project-agnostic — they describe Switchyard, not
any particular project.

1. **[adversarial-collaborative-methodology.md](adversarial-collaborative-methodology.md)** — *why*.
   The principles the whole system rests on. Read the principles; its Roles and Phases
   sections predate the ticket board and are marked superseded.

2. **[switchyard-board-guide.md](switchyard-board-guide.md)** — *mechanics*. Stages,
   transitions, who may do what, how to read and write the board, `directorctl`, and the
   behaviours that surprise people. Everyone reads this one.

3. **[switchyard-director-guide.md](switchyard-director-guide.md)** — *judgment*. Writing a
   ticket, review discipline, merge versus kick back, working with audit, routing and
   holds, escalation. Directors read this one.

## Start here if you are new

Your board's address is already in your environment — you do not need to look it up:

```bash
echo "$TICKET_BOARD_URL"
curl -s "$TICKET_BOARD_URL/api/board"
```

When creating a project, `switchyard new` initializes an empty project path as a
git repository on the configured branch and creates the initial commit needed for
role worktrees. Existing repositories are left untouched. Use `--no-git-init`
only for a path that already has a repo with `HEAD`.

`switchyard new` also copies this onboarding packet into the new project at
`docs/onboarding/`, preserving a source-commit header in each copied file.
Existing files there are left untouched and reported as skipped.

During `switchyard new`, the default implementer roles are `main` and `ops`.
Conventional implementer role names are: `main` for core/domain implementation
and integration; `ops` for environment, services, tooling, and infrastructure;
`app` for application/UI work; `research` for investigation and design support;
and `perf` for measurement and performance work. Custom implementer role names
are allowed when the project needs different vocabulary.

## Not onboarding, but nearby in the Switchyard source checkout

- Switchyard source checkout `docs/ticket-board-service.md` — deploying and operating the board service.
- Switchyard source checkout `docs/team-launcher.md` — the launcher, panes, roles and first-run auth.
- Switchyard source checkout `docs/switchyard-project-setup-design.md` — how a new project is provisioned.
