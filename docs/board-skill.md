# The portable board skills

Every Switchyard pane runs one of four agent CLIs, and each of them reads skills
from a different directory. Switchyard keeps **one** canonical body per skill and
projects it into all four, so a pane's instructions do not depend on which
runtime it happens to be.

Two skills ship:

| Skill | Who is told to load it |
|---|---|
| `switchyard-board` | every role |
| `switchyard-director` | the Director, in addition to the shared one |

## The canonical sources

`skills/switchyard-board/SKILL.md` in the Switchyard source tree. It is the only
copy that is edited. It is deliberately tenant-neutral: it names no project
prefix, port, role, stage, database, or home directory, because those differ per
project and a skill that hardcodes them is wrong everywhere except where it was
written. It teaches the agent to read its board address and role out of the
environment (`TICKET_BOARD_URL`, `TICKET_BOARD_SOCKET`, `TICKET_BOARD_CALLER_ROLE`,
`TICKET_BOARD_TICKET_PREFIX`) and to read the legal transitions for a ticket out
of that ticket's own `workflow_actions`.

`skills/switchyard-director/SKILL.md` is the Director overlay: triage and
routing, gate flags, holds, runtime role and pane control, integration, release
and rollback, UAT preparation, notification recovery, and narrated overrides. It
is an overlay, not a fork — it assumes the shared skill and repeats none of it,
and it defers to the project's director guide for the judgment behind each
control.

### Why the overlay is not hidden

Role panes commonly share one Unix home, so both files land in every session's
skill directory and any pane can read either. That is deliberate, and the
correctness of the arrangement does not depend on hiding a file:

- The overlay's first section is a runtime identity check on
  `TICKET_BOARD_CALLER_ROLE`, telling any other role to stop and use the shared
  skill.
- Only a Director session is *told* to load it, by the startup and hand-off
  pointers below.
- The board authorizes every operation by caller role. Reading the overlay
  grants nothing; a non-Director calling a Director operation is refused by the
  server, and that refusal — not the skill text — is the security boundary.

## Adapter locations

`switchyard board-skill install` writes the same file, byte for byte, to each
runtime's own discovery path under the project owner's home:

| Runtime | Skills root under the owner's home |
|---|---|
| Claude | `~/.claude/skills/<skill>/SKILL.md` |
| Codex | `~/.codex/skills/<skill>/SKILL.md` |
| agy | `~/.gemini/config/skills/<skill>/SKILL.md` |
| Hermes | `~/.hermes/skills/<skill>/SKILL.md` |

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

A deployed tree is an export of exactly one commit, so its release marker
(`.switchyard-release.json`) is the answer and is trusted. A working checkout is
not: `HEAD` there can be any commit, including one that predates this file. The
installer therefore claims a checkout's `HEAD` only when that commit actually
contains these bytes, and otherwise stamps `unknown` and says so on stderr.

A commit passed in with `--source-commit` gets the same treatment. The launcher
supplies one — it resolves a release commit the way it does for onboarding docs
— but over a working checkout that resolution is only `rev-parse HEAD`, which
says nothing about whether HEAD carries this body. Validating inside the
installer rather than at each call site means `switchyard upgrade
--source-repo <working-checkout>` cannot stamp a commit the body has moved on
from. A
copy stamped `unknown` is reported by `verify` as `unprovenanced` and fails it —
so a hand-projected copy can never be mistaken for evidence that a release
shipped the skill.

Copies installed by the first release carry the older wording
`Switchyard board skill snapshot`. They are still recognised as ours and are
refreshed in place, because orphaning them would leave a tenant with a copy that
is never updated again.

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
  naming the skills for that role — the shared one, plus the overlay for a
  Director — alongside whatever else that session needed to hear. Only
  some runtimes label the kind of start in the payload: Claude sends
  `startup`/`resume`/`clear`/`compact`, while Hermes and agy send a session id
  and no label at all. On a session-start event an absent label is treated as a
  plain start, so those panes are told about the skill too. Resumption is still
  detected, because it comes from the launcher's tracked session id rather than
  from the payload.
- **Hand-off** — the notify listener appends one line to `transition`
  notifications, the ones that hand a ticket to a role, naming that role's
  skills. Nudges, idle reminders and
  escalations go to a pane already working the ticket and do not repeat it.

## Recovery

If a pane says it cannot find the skill:

```bash
switchyard board-skill verify              # per runtime: present, stale, unmanaged, unprovenanced, missing
switchyard board-skill install             # reinstall from the canonical source
switchyard board-skill install --dry-run   # show what would change first
switchyard board-skill source-commit       # provenance the source would stamp
```

Use `switchyard`, not a bare `switchyard-board-skill`. Only `switchyard` is
installed host-wide; `switchyard-board-skill` lives in the deployed tree's
`scripts/` directory, which a role pane happens to have on `PATH` and an
operator's shell does not. If you do need that script directly — from a
provisioning context, say — its guaranteed path is
`<board-root>/current/scripts/switchyard-board-skill`, and inside a pane
`"$(dirname "$TICKET_BOARD_DIRECTORCTL")/switchyard-board-skill"` resolves it.
`switchyard board-skill` needs no root.

`verify` exits non-zero and names the runtimes that cannot use the skill.
`unmanaged` means a hand-written skill sits at that path — read it before using
`--force`. `unprovenanced` means the copy was installed from a tree that could
not name the commit carrying it; reinstall from a deployed release rather than
from a working checkout.

Installing the file is not the same as the runtime using it. A CLI reads its skill
directory at startup, so restart the pane's CLI after an install, and confirm by
asking that session to load `switchyard-board` rather than by checking that the
file exists.
