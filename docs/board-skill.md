# The portable board skill

Every Switchyard pane runs one of four agent CLIs, and each of them reads skills
from a different directory. Switchyard keeps **one** canonical board skill and
projects it into all four, so a pane's instructions do not depend on which
runtime it happens to be.

## The canonical source

`skills/switchyard-board/SKILL.md` in the Switchyard source tree. It is the only
copy that is edited. It is deliberately tenant-neutral: it names no project
prefix, port, role, stage, database, or home directory, because those differ per
project and a skill that hardcodes them is wrong everywhere except where it was
written. It teaches the agent to read its board address and role out of the
environment (`TICKET_BOARD_URL`, `TICKET_BOARD_SOCKET`, `TICKET_BOARD_CALLER_ROLE`,
`TICKET_BOARD_TICKET_PREFIX`) and to read the legal transitions for a ticket out
of that ticket's own `workflow_actions`.

## Adapter locations

`scripts/switchyard-board-skill install` writes the same file, byte for byte,
to each runtime's own discovery path under the project owner's home:

| Runtime | Installed path |
|---|---|
| Claude | `~/.claude/skills/switchyard-board/SKILL.md` |
| Codex | `~/.codex/skills/switchyard-board/SKILL.md` |
| agy | `~/.gemini/config/skills/switchyard-board/SKILL.md` |
| Hermes | `~/.hermes/skills/switchyard-board/SKILL.md` |

These are per-user, not per-checkout, so a pane discovers the skill from any
working directory — which matters because role panes run in worktrees, not in the
project directory.

Hermes roles run with a per-role `HERMES_HOME` whose `skills` entry is symlinked
to the shared `~/.hermes/skills`. The launcher only creates that symlink when the
shared directory already exists, so the skill install runs before any role session
starts. A role home that predates the install picks the symlink up on its next
launch.

## Provenance and ownership

Each installed copy ends with a footer naming the Switchyard commit it came from:

```
<!-- Switchyard board skill snapshot: source commit <sha>; source skills/switchyard-board/SKILL.md. -->
```

The commit comes from the immutable release marker (`.switchyard-release.json`)
that governs the deployed tree, falling back to the checkout's `HEAD`.

That footer is also the ownership marker:

- a copy carrying it is Switchyard-managed and is refreshed in place on upgrade,
  which is what keeps the four locations from drifting into four bodies;
- a file at the same path *without* it was written by someone else and is never
  touched. `--force` overrides that, and is the only way to overwrite it.

The installer runs as the project owner (`sudo -u <owner> -H`), so the installed
files belong to the tenant, not to root.

## When it is installed

- **New tenants** — the generated provisioning script runs the installer next to
  the pane-hook install.
- **Existing tenants** — `switchyard upgrade` refreshes it, and every project
  launch re-runs the install before role sessions start.

## Telling a session to load it

The body is installed once; sessions get a pointer, never a paste.

- **Startup** — the pane idle hook's `SessionStart` output opens with one line
  naming the skill, alongside whatever else that session needed to hear.
- **Hand-off** — the notify listener appends one line to `transition`
  notifications, the ones that hand a ticket to a role. Nudges, idle reminders and
  escalations go to a pane already working the ticket and do not repeat it.

## Recovery

If a pane says it cannot find the skill:

```bash
switchyard-board-skill verify                 # per runtime: present, stale, unmanaged, missing
switchyard-board-skill install                # reinstall from the canonical source
switchyard-board-skill install --dry-run      # show what would change first
switchyard-board-skill source-commit          # provenance the source would stamp
```

`verify` exits non-zero and names the runtimes that cannot use the skill. Run
both from the deployed Switchyard tree (`scripts/` beside the launcher the project
runs), so the source and the provenance match what the tenant is running. A
runtime reported `unmanaged` has a hand-written skill at that path — read it before
using `--force`.

Installing the file is not the same as the runtime using it. A CLI reads its skill
directory at startup, so restart the pane's CLI after an install, and confirm by
asking that session to load `switchyard-board` rather than by checking that the
file exists.
