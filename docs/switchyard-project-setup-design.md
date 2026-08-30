# Switchyard project setup: design → new → specialize

Status: DRAFT for Eric's iteration. Nothing here is implemented.
Parent: PGU-580 (turnkey project start/resume package).

## The problem

Standing up a project currently requires deciding all of this at once, at the command line:

- **Identity** — slug (drives port, database, socket, unit names), ticket prefix, owner user
- **Code** — project repo path, remote, default branch, whether implementers use isolated worktrees
- **Policy** — push rules (pgu runs `PGU_ALLOW_MAIN_PUSH=director`), which gates apply
  (`audit_signoff`, `needs_inspection`, `needs_user_signoff`)
- **Roles** — which ones, and per role: cli, model, reasoning effort, `yolo`, detached vs paned
- **Workflow** — stages, labels, ownership, transitions, who may release a draft
- **Whether the human is a role** — `user` appears in `release_draft`'s allowed roles

That is too much for a flag list and too much for a hand-written JSON file. Both were rejected:
a large flag surface is unusable, and hand-crafting config is exactly the friction the epic exists
to remove.

## Shape: three phases

### 1. `design` — the design document, then the configure questions

New projects only. No tmux, no panes, no board, no privileged steps; it can run before the target
user exists. Two sub-steps, in order:

**1a. Write the project's design document.** What is being built. This is the part that wants
thinking time, and it is why the phase exists separately from `new` — it needs no infrastructure.

**1b. Post-design questions.** Once the design is *done enough*, ask the configuration questions
and emit a reviewable artifact, e.g. `otto.project.json`. These come after 1a deliberately: several
of them are only answerable once you know what the project is.

Post-design questions cover:

| Question | Notes |
|---|---|
| **Code location** | Where the project's own repo lives. Answerable only once the design exists |
| Remote, default branch | Push target; whether implementers use isolated worktrees |
| Slug | Drives port, database, socket, unit names |
| Ticket prefix | Defaults to `PGU` today if unset — see Known gap |
| Owner user + capability grants | See User creation |
| Policy | Push rules; which gates apply |

It does **not** ask about stages or extra roles — those ship as the working five-stage default and
are decided later, once the director is live (see phase 3).

### 2. `new --from otto.project.json` — provision

Consumes the artifact. Creates the user (optional), the board, the units, the config, the layout.
Privileged half stays as it is today: precheck first, dry-run default, one authentication prompt,
one-command rollback.

### 3. Specialize — the director shapes the project

After the team launches, the designer writes the spec. Then the **director and the user decide
together** what the stages and extra implementer roles should be — a discussion, not a proposal
the human rubber-stamps. An explicit *apply-shape* step then updates the database rows **and**
adds panes.

This is why `design` does not ask: the questions are better answered against a real spec, with the
director in the conversation, than as prompts before anything exists.

### Why an artifact, rather than choosing wizard-or-flags

This is the point that resolves the disagreement rather than trading it off:

- interactive user: `design` walks them through it and writes the file
- scripted install, CI, a friend's `curl | bash`: supply the file directly

Same input, one code path. The file is diffable and reviewable before anything privileged runs.

## What is decided when

Split by **when the decision must exist**, not by CLI-vs-web preference.

**Must exist before the board can be created** — these are files and system objects:
slug, owner user, port/database/socket, code location, ticket prefix.

**Can be refined after** — these are *database rows*. pgu currently has 11 stages and 55
transitions, all data:
stages, labels, ownership, transitions, gates.

So `new` should ship a **working** five-stage default and let the team start immediately.
A board setup panel can refine the workflow later, gated on "workflow not yet customized"
(a DB flag) rather than on "setup files are missing" — a board cannot render a form before it
has been provisioned.

## User creation

Currently done by hand from a separate codex instance. Scriptable, and it belongs in `new`.

The parameter is **not** "how sandboxed". That framing does not survive contact with the host:

- `/dev/dri/renderD128` is `crw-rw-rw-` — **world-writable**. Every user has GPU compute access,
  sandboxed or not.
- `/dev/dri/card0` is `root:video` plus an ACL for `eric` only. Nobody else reaches KMS/display.
- `agent` is in **no supplementary groups at all** — it is not in `video`. Its GPU reach is
  identical to otto-agent's and stellaris-agent's.
- `otto-agent` is in *more* groups than `agent` (`kvm`, `adbusers`).
- `/home/agent` and `/home/otto-agent` are both `0710` with `user:boardsvc:--x`.
  `/home/stellaris-agent` is `0700` with no ACL — the strictest of the three.

That `boardsvc:--x` entry is **provisioning, not sandboxing**: it is what lets the board service
traverse into the home to reach assets and frames.

So the real questions are a short list of capability grants:

| Question | Why it is asked |
|---|---|
| Board service traversal into the home? | Only if that project's board serves its assets/frames |
| Supplementary groups (`kvm`, `docker`, `video`) | Case by case; do not grant by default |
| Linger enabled? | Required for the user-level notify-listener |
| Shell | fish/bash; affects any operator block written for that user |

If board-service traversal is declined, `new` does not grant `boardsvc` ACLs on the owner home,
board release, assets, or frame directories. The operator must then choose paths already accessible
to `boardsvc` or pre-grant access outside switchyard; otherwise the board service health check fails
instead of silently changing the owner's home ACLs.

## Known gap this surfaced

`ticket_prefix` defaults to `'PGU'` and the provisioner never sets it, so **otto's board would
number its own tickets `PGU-1, PGU-2…`**. The prefix belongs in the design artifact.

## Decisions (Eric, 2026-08-24)

1. **Stages are not part of `design`.** Ship the defaults; the director and the user decide stages
   and extra roles once the director is live.
2. **`design` is for new projects only.** Reshaping an existing project is a separate command.
3. **Extra implementer roles: same as (1)** — decided in discussion between director and user.

## Reshaping an existing project

A separate command, deliberately. Working name TBD -- `clone`/`fork` read as "copy a project",
which is not what this does; `reshape` or `configure` says it plainly.

The hard part is not editing rows, it is **in-flight tickets**:

- a stage that is removed or renamed may have tickets sitting in it
- `workflow_stages.owner_roles` may reference a role being dropped, which is exactly the defect
  PGU-644 just fixed at provisioning time -- the same hazard exists at reshape time
- transitions referencing a removed stage leave tickets unable to move

So reshape needs migration semantics, not just an editor: for every ticket in an affected stage,
where does it go? That is why it is a separate command rather than a flag on `new`, and why it
should refuse rather than guess.

## The command surface (Eric, 2026-08-24)

**`switchyard <slug> <command>` and nothing more.** Rename the script from `team-launcher` to
`switchyard`. A user supplies a slug and a command; everything else is derived. Long invocations are
a non-starter for distribution -- "normal people look at that and then look elsewhere".

Derivations:

| Was a flag | Derived from |
|---|---|
| `--owner-user` | `<slug>-agent`, created by `new` |
| `--source-repo` | where switchyard itself is installed |
| `--new-output-dir` | a temp dir |
| port / database / socket | already deterministic from slug |
| ticket prefix | slug, uppercased |

`new` also **creates the user**, which means switchyard is run by a person, not by an agent account
as it is today.

It does **not** sudo internally. If `new` is invoked without the privilege to create a user, it
fails the precheck with a plain error telling the operator to re-run under sudo. That avoids
partial-privilege states -- no half-provisioned project where the user exists but the units do not --
and keeps the failure at the precheck, before anything is written.

Implication to honour: run entirely under sudo, everything it creates is root-owned by default.
The generated block already handles this (`install -o '<user>' -g '<user>'`); whatever replaces it
must keep that property, or the project user ends up unable to write its own board root.

### Dry-run becomes a confirmation, not a flag

`--execute` should not survive as a flag, but the safety property behind it must: print the plan,
then `Proceed? [y/N]`. Short command, review preserved before anything privileged runs, and `--yes`
for CI and scripted installs.

## Revised entry surface (Eric, 2026-08-24)

Supersedes the `switchyard <slug> <command>` sketch above. Zero parameters is the default path.

    switchyard                      menu: `new...` on the first line, then all projects
    sudo switchyard new             straight into new-project setup
    sudo switchyard My Project Name straight into that project

The listing is the whole navigation surface -- it is both discovery and **resume**, which is the
question a new user hits on day two. Selecting a project attaches if it is running and resumes if
it is not (today's `start` semantics).

The installed `switchyard` command is a PATH trampoline outside the shared checkout. A bare
`switchyard` from a normal user shell re-execs through sudo before it reaches the live checkout, so
printing the menu may prompt for a password; that is the expected boundary between Eric's account
and the agent-owned checkout, not a menu bug.

`new` prompts for **project name**, **slug**, **agent user** (defaulted with
`-agent`), and **project path**. The first answer is the human name; the later
answers are derived from it but editable. If the operator types an agent-user
value, `new` uses that value verbatim:

    Project name: Otto Scheduler
    Slug         [otto_scheduler]:
    Agent user   [otto_scheduler-agent]:
    Project path [/home/otto_scheduler-agent/Projects/otto_scheduler]:

The default project path follows the project name, not the slug. Editing the slug to a shorter
machine token such as `otto` still keeps the default path at `Projects/otto_scheduler`. The path is
also editable for existing checkouts or shared locations, but it must be readable and writable by
the project agent user. Then `new` starts the design session.

**`design` is dropped as a separate command.** `new` sets things up far enough for the designer to
start, and launches it. The designer runs as a normal CLI with **no tmux session**. It decides the
implementer roles with the user, and when the design is done the director is spawned with the
onboarding docs, the design, and what it needs to set up and launch the pane window.

### Why the ordering is forced: claude scopes sessions by cwd

Claude Code stores sessions under a directory derived from the working directory:

    cwd /home/agent/Projects/pgu   ->  ~/.claude/projects/-home-agent-Projects-pgu/<session>.jsonl
    cwd /home/otto-agent           ->  ~/.claude/projects/-home-otto-agent/

Verified on this host. A resume from a different cwd looks in a different directory and does not
find the session. So the designer **must** be launched from the project's final working directory,
or its session can never be resumed -- and resume is a hard requirement of this epic.

That forces the sequence, and nothing here is a preference:

1. prompt for project name, slug, agent user, project path
2. create the selected agent user (privileged -- switchyard, not the director;
   the default is `<agent-name>-agent`, typed input is verbatim)
3. create the derived project directory in that user's home
4. launch the designer **as that user, in that directory**

Steps 2 and 3 cannot move after step 4.

**The cwd is effectively permanent.** Sessions must resume across days, and a session's cwd cannot
change -- moving it means handing off to a fresh instance and losing the accumulated context. For
the director, which is usually Claude Opus carrying the most context on the team, that is expensive.

So the derived path must be right the FIRST time. Two things follow:

- **show the project path in the prompt** before creating anything, so the user sees
  `/home/otto-agent/Projects/otto_scheduler` and can object or override it while objecting is still free
- **verify the cwd immediately after launching**, not weeks later. Confirm the session landed in
  `~/.claude/projects/<escaped-cwd>/` for the intended directory. Discovering this at the first
  reboot is how PGU-613 happened -- a resume that reports success while the model has nothing.

Details that follow from the unquoted form:

- join all argv into the project name; do not require quoting
- match case-insensitively, fall back to slug
- `new` is a reserved word -- decide what a project named "New" does
- the window follows the role count; pgu has 7 panes, a default project has 4 plus implementers

### Two constraints this shape has to respect

**Roles in design relaxes a guard we just added.** PGU-647 makes `new` REJECT artifacts carrying
`project.roles` or `project.stages`, enforcing the earlier decision that roles are settled after
the director is live. Deciding them in the design session is better -- with a person, against a
real spec -- but the rejection must be relaxed deliberately, not discovered as a bug.

**The director cannot do the privileged half.** Creating `<slug>-agent` and installing units needs
root, and agents deliberately have no sudo. Split it:

- **switchyard**, run by a person, fails with a clear error if not under sudo: creates the user and
  the board
- **the director**, unprivileged: seeds the board, launches panes, onboards

**The design session is not a pane.** It decides roles before the board exists, so it cannot be a
normal pane with hooks and a board connection. A standalone agent in the project directory whose
only outputs are the design document and the artifact.

## Role selection and the pane window (Eric, 2026-08-25; updated by PGU-723/PGU-732)

`new` starts the FULL pane window with every role populated. There is no separate "spawn the director"
step, and no separate provisioning step -- the privileged half all happens once, under the sudo the user
already gave.

`new` now asks for the implementer roles and the CLI for each role instead of
silently creating a fixed programmer set. `director` is always present;
`designer` and `audit` are optional. If the user omits `designer`, the design
phase is skipped and no designer pane or design document is created. If the user
omits `audit`, the provisioned workflow skips the Audit stage and implementation
submissions go directly to Final Sign-Off.

Stage ownership:

| Stage | Owner |
|---|---|
| Draft | designer |
| Triage | director |
| Implementation | **main** |
| Audit (when selected) | audit |
| Final Sign-Off | director |

Implementation moves from `ops` to `main`. Ops owned it only because PGU-644 dropped `implementer` and
left the stage unowned; with real programmers in the defaults that workaround is unnecessary.

The design phase is a VIEW STATE, not a lifecycle stage: maximize the designer pane while designing,
restore when done. In Konsole that is **Ctrl+Shift+E** ("Toggle maximize current view"). Do not automate
it -- every pane runs regardless, so driving the window manager buys nothing and adds display, focus and
timing failure modes. Print the instruction.

Open, not urgent: `app` and `main` are probably in the wrong positions in the layout and should be
swapped.

## Layout: KDE keeps what it has, everything else gets a viewer session (Eric, 2026-08-25)

Eric: "proceed with the non-KDE support, but I don't want anything with my setup to change. if the DE is
KDE, it should do what we are already doing."

TWO PATHS, and the KDE one is the status quo by definition:

| | KDE | everything else |
|---|---|---|
| role sessions | 7, one pane each | **identical** |
| notification address | role session name | **identical** |
| window layout | Konsole split views (user-arranged) | a `viewer` tmux session, 6 panes |
| maximize during design | Konsole `Ctrl+Shift+E` (printed) | `Ctrl+a z` (printed) |

The viewer is PURELY ADDITIVE: an extra tmux session whose panes each run
`TMUX= tmux attach -t <role>`. It creates no new addressable identity, and the role sessions do not know
it exists. That is why the KDE path can be left untouched rather than refactored into a shared shape.

WHY THIS IS NOT THE REJECTED ONE-SESSION DESIGN. That rejection was about ADDRESSING: under one session
the notification target becomes a pane INDEX, and indices renumber when a pane dies. Measured, 2026-08-25:

    address           team:0.0  0.1       0.2    0.3   0.4   0.5
    before            designer  director  audit  ops   app   main
    after pane 1 dies designer  audit     ops    app   main  main

Every address after the gap shifts, and `team:0.5` does not error -- tmux clamps to the last pane, so it
silently resolves to the wrong agent. Under the viewer, the same test moves nothing: the address stays
`audit:0.0` through killing audit's viewer pane AND through killing and restarting the audit session
itself, with delivery confirmed by send-keys afterwards.

DETECTION MUST NOT REST ON `XDG_CURRENT_DESKTOP`. `new` runs under sudo, whose default `env_reset`
strips it. Detect via the INVOKING user (SUDO_USER), and when the desktop cannot be determined at all,
**fall back to the KDE path** -- that is today's behaviour, so an ambiguous detection can only leave a
machine where it already was. The viewer activates on positive identification of a non-KDE desktop, never
on a guess.

## What the deploy actually ships, and what the cleanliness gate really checks

Recorded because the error message implies something it does not do, and this was mid-diagnosis twice.

The board release is materialised by `git archive` of a RESOLVED REF -- ticket-board-service.sh:316,
with DEPLOY_REF=origin/main, and origin is fetched first. Three consequences:

  - untracked files can never reach a release (this is why the precheck uses --untracked-files=no)
  - modified TRACKED files never reach a release either
  - **unpushed local commits never reach a release** -- the ref is origin's, not yours

So `deploy checkout <path> has uncommitted changes` is NOT gating deploy CONTENT. It is a
"you have work in progress, are you sure" guard, and it is worth keeping as one. But do not reason
from it: a clean checkout does not mean the release contains what you are looking at, and a local
commit that has not been pushed will be silently absent from what gets deployed.

## The project directory must stop being the worktree base (PGU-674 design, accepted)

Eric approved the direction 2026-08-25. Today `<project>` does two incompatible jobs: it is the user's
project AND the ref holder that `ensure_project_worktrees` pins with `checkout --detach --force` plus
`clean -fdx` on every launch. PGU-673 made that loud; this makes it stop.

**A premise I asserted and got wrong, corrected here so nobody re-derives it.** I wrote that
"role worktree isolation must survive unchanged". There is no launcher-managed worktree isolation.
Measured: every role in `config/team-launcher/pgu.json` has no `workdir`, so all seven default to
`repository`; the live panes all report `/home/agent/Projects/pgu`; and the only `worktree add` in
`scripts/` is an unrelated demo. `ensure_project_worktrees` is MISNAMED -- it refreshes the shared
checkout and provisions no worktrees. The isolation that exists is agent-level: each CLI makes its own
per-task worktree. So this work CREATES launcher-managed role worktrees; it does not relocate them.

THE SHAPE:

| field | meaning |
|---|---|
| `repository` | the user-visible checkout. Never force-reset, never cleaned. |
| `control_repository` | internal BARE base, `~/.local/state/switchyard/projects/<slug>/control.git` |
| `worktree_base` | existing placement root for managed role worktrees |

Bare, because a bare repo has no working tree and therefore no `checkout --force` or `clean -fdx`
target -- it removes the root cause rather than moving it. Outside the project tree, because users
delete and copy project directories.

VERIFIED COMMAND SHAPE (run in scratch by both ops and audit; do not use the earlier sketch):

    git clone --bare "$source_repo" "$control_repository"
    git -C "$control_repository" config --add remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'
    git -C "$control_repository" fetch origin main
    git --git-dir "$control_repository" worktree add --detach "$worktree_base/<role>" origin/main

Plain `clone --bare` sets NO fetch refspec, so `refs/remotes/origin/main` never appears and
`worktree add ... origin/main` fails with "invalid reference". `clone --mirror` works but maps branches
into `refs/heads/*`, forcing the attach ref to `main` and a change to `worktree_ref()`. Bare-plus-refspec
keeps `origin/main` semantics intact.

MAIN PROTECTION: not achievable in this shape. A pre-receive hook only fires on a PUSH into the bare
base, and this topology is fetch-based -- nothing pushes into it. Client-side hooks in the visible
checkout are decorative (not cloned, trivially bypassed). Getting real local main protection would
require role worktrees pushing INTO the control repo, which is a workflow change, not a hook.

THE RESET IS RELOCATED, NOT ELIMINATED. Managed role worktrees still get `reset --hard origin/main` and
`clean -fdx`. Audit measured it: an uncommitted file in a role worktree is deleted, a dirty tracked edit
reverted. That is defensible -- they are launcher state by construction -- but PGU-673's warning guards
`repository`, so it MUST move with the reset or the defect reappears one directory over, aimed at agents.

SCOPE DISCIPLINE: an absent `control_repository` means today's behaviour EXACTLY. pgu is not migrated by
this work. Migration is an explicit command, never on launch, and moving pgu's panes changes every pane's
cwd and needs a director-approved restart window -- a separate decision from shipping the new shape.

## First-run authentication (PGU-691 design, accepted 2026-08-26)

A newly provisioned project opens six panes each blocked on something different, and a blocked pane looks
alive -- which is how PGU-688 hid a dead role for a day. So `new` sequences authentication BEFORE the
window opens.

THE STRUCTURE, which is what makes it small: **login is per CLI per owner user; trust is scoped to the
CLI's own trust store.** One `codex login` covers all codex roles for that owner, and Codex hook trust is
also per owner user and installed hook entry, not per pane role. On project creation only, Switchyard
seeds Codex trust for the hooks it just wrote when the owner has no existing Codex hooks file and no
existing hook trust records. If hooks or trust records already exist, that is a manual `/hooks`
re-approval path. Six Claude worktrees means six Claude folder-trust prompts. Logins and Codex hook
approvals do not multiply with roles.

**We invoke the CLIs' own login commands and nothing else.** Eric, 2026-08-26: "it can't be automated,
thats a big sec risk." No credential handling, no scripted entry, no storing anything.

| CLI | login | status probe |
|---|---|---|
| claude | `claude auth login` | `claude auth status --json` -> `loggedIn` |
| codex | `codex login` | `codex login status` |
| agy | no standalone login; guided interactive startup | `agy models` (a real authenticated fetch) |
| hermes | `hermes model` | `hermes config check` -> at least one resolved `*_API_KEY` |

Do NOT parse `~/.claude/.credentials.json`, OAuth state, or
`~/.gemini/antigravity-cli/antigravity-oauth-token`. Ask the CLI.

**FOLDER TRUST IS NOT PRE-SEEDED. Decided, not open.** `~/.claude.json` does key `projects` by absolute path
with `hasTrustDialogAccepted: true`, and agy has `trustedWorkspaces` -- so it is technically possible. We
do not do it, and not as an opt-in either:

  - it is a consent bypass through an undocumented config key, and the prompt exists so a human consents
  - an undocumented key breaks silently on a CLI update, and the failure mode is a project that stops
    trusting its own worktrees for reasons nobody can see
  - "opt-in consent bypass" is still a consent bypass

The user answers folder-trust prompts. `new` makes that a short guided step instead of a scavenger hunt.
Codex hook trust is different: at creation the operator already authorized Switchyard to write the hook
file, so Switchyard records trust for that exact new file. Later hook-content changes still require manual
Codex `/hooks` approval.

ORDER:
  1. provision config + role worktrees, so the exact trust paths are known
  2. probe each distinct CLI's auth status as the owner user
  3. for anything unauthenticated, run that CLI's own login -- **from a neutral cwd (the owner's home),
     not a worktree**, so a login does not itself trigger a trust prompt
  4. walk the per-worktree trust prompts
  5. launch, then print what is still unauthenticated or untrusted

FAILURE POLICY: launch with a clear report. A half-authenticated team is still useful; a silent one is
not. No hard abort by default.

## One tmux session with six panes: rejected, twice

Eric tried this when switchyard was first built and abandoned it quickly. The original symptom is
unrecoverable: the attempt predates the ticket system entirely, so there is no ticket to find and no
point searching for one. Do not repeat that search. Director re-proposed it on 2026-08-25 as a way to get portable
`prefix z` zoom instead of Konsole's Ctrl+Shift+E, and it was rejected again.

Recording the CURRENT technical reason so it is not proposed a third time:

**tmux pane indices renumber when a pane dies.** Measured on an isolated server:

    panes 0 1 2 3  ->  kill pane 1  ->  panes 0 1 2

Every notification target and every durable session record is keyed by a stable per-role name --
`pgu-ops:0.0`, `pgu-director_0.0.json`. Under one session those become pane INDICES, so one pane
restarting shifts the identity of every pane after it, and notifications and session records would
address the wrong agent.

That is the exact failure class the whole PGU-6xx run was spent eliminating: writing into the wrong
pane, and clobbering another role's session record. Seven separate sessions are what make the
identifiers stable.

Consequence, accepted: maximize is Konsole-specific (Ctrl+Shift+E). `prefix z` is a no-op here because
each session holds exactly one pane, so there is nothing to zoom. On a non-KDE host the team still
works -- the agents live in tmux -- but the six-pane window and its maximize do not. That is a
Milestone 3 presentation gap, not a functional one.

## Open questions

None. Both are closed (Eric, 2026-08-24):

1. **Project repo path: derived from the PROJECT NAME**, not the slug:
   `/home/<agent>/Projects/<slugified project name>`. Corrected after the first live run -- with slug
   `otto` the path came out `Projects/otto`, which is wrong. The slug is a short machine identifier;
   the directory should be human-meaningful.

   | Name | Example | Job |
   |---|---|---|
   | project name | `Otto Scheduler` | what you type; what `switchyard <name>` matches |
   | slug | `otto` | port, database, socket, unit names, ticket prefix |
   | agent | `otto-agent` | the owning user |
   | project dir | `/home/otto-agent/Projects/otto_scheduler` | derived from the NAME |

   This is also why prompt order matters: the derived path cannot be shown until the name is known,
   so the name must be asked FIRST and the slug derived from it.
2. **Reshape: does not exist.** The director handles changing stages and roles on a live project.
   There is no separate command and no `clone`/`fork`.
