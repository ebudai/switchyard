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

    ./switchyard                    menu: `new...` on the first line, then all projects
    ./switchyard new                straight into new-project setup
    ./switchyard My Project Name    straight into that project

The listing is the whole navigation surface -- it is both discovery and **resume**, which is the
question a new user hits on day two. Selecting a project attaches if it is running and resumes if
it is not (today's `start` semantics).

`new` prompts for **slug**, **agent name** (`-agent` appended) and **project name** -- three names
that genuinely differ; otto's slug is `otto` and its code lives in `scheduler`. Then it starts the
design session.

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

1. prompt for slug, agent name, project name
2. create `<agent-name>-agent` (privileged -- switchyard, not the director)
3. create the derived project directory in that user's home
4. launch the designer **as that user, in that directory**

Steps 2 and 3 cannot move after step 4.

**The cwd is effectively permanent.** Sessions must resume across days, and a session's cwd cannot
change -- moving it means handing off to a fresh instance and losing the accumulated context. For
the director, which is usually Claude Opus carrying the most context on the team, that is expensive.

So the derived path must be right the FIRST time. Two things follow:

- **show the derived path in the prompt** before creating anything, so the user sees
  `/home/otto-agent/Projects/otto` and can object while objecting is still free
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

## Open questions

1. ~~Project repo path~~ **DECIDED (Eric, 2026-08-24): derived.** `/home/<agent>/Projects/<slug>`.
   No prompt, no flag.
2. **Reshape** -- changing stages or roles on a project that already exists, with tickets in flight.
   Open question is whether it needs to be a command at all: the director already applies shape at
   project start (adding panes, updating `owner_roles`), and reshape is that same apply-shape
   operation run later. If apply-shape handles in-flight tickets from the outset -- for every ticket
   in an affected stage, where does it go, refusing rather than guessing -- then "reshape" is just
   calling it again, and the naming question disappears.