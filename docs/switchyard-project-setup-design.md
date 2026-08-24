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

`new` also **creates the user**, which means switchyard is run by a person and sudoes internally --
not run by an agent account as it is today.

### Dry-run becomes a confirmation, not a flag

`--execute` should not survive as a flag, but the safety property behind it must: print the plan,
then `Proceed? [y/N]`. Short command, review preserved before anything privileged runs, and `--yes`
for CI and scripted installs.

## Open questions

1. **Project repo path.** Full derivation gives `/home/<slug>-agent/Projects/<slug>`. Eric wanted
   otto's code at `Projects/scheduler` -- a name that differs from the slug. Is the derived path the
   default with renaming handled post-design/by the director, or does the repo name need to survive
   as an answerable question? UNDECIDED -- Eric is considering.
2. Naming for the reshape command.