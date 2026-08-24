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

### 1. `design` — produce the artifact

No tmux, no panes, no board, no privileged steps. It can run before the target user exists.
Interactive by default; its only output is a reviewable file, e.g. `otto.project.json`.

### 2. `new --from otto.project.json` — provision

Consumes the artifact. Creates the user (optional), the board, the units, the config, the layout.
Privileged half stays as it is today: precheck first, dry-run default, one authentication prompt,
one-command rollback.

### 3. Specialize — the director shapes the project

After the team launches, the designer writes the spec, then the director proposes the project's
real shape: which implementer roles, which stages. An explicit *apply-shape* step updates the
database rows **and** adds panes.

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

## Open questions for Eric

1. Does `design` also cover **stage** customisation, or only identity/roles/policy — with stages
   left to the board panel? Stages are the most structural and the least suited to a prompt.
2. Should `design` be able to target an **existing** project (re-run to change shape), or is it
   strictly first-run?
3. For implementer roles: does the director *propose* and a human approve, or does the director
   apply directly?
