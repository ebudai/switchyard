# SYRD-715: a store that follows the checkout's own remote is this project's

## What happened

Preparing the newly enabled SYRD Librarian refused before creating its
worktree:

> control.git is another checkout's commit store: its origin is
> https://github.com/ebudai/switchyard.git, but this project's repository is
> /home/switchyard-agent/Projects/switchyard

It is the store behind every existing SYRD role worktree, and its origin is the
canonical repository:
- `~/.local/state/switchyard/projects/syrd/control.git`: origin
  `https://github.com/ebudai/switchyard.git`, push URL
  `git@github-switchyard:ebudai/switchyard.git`.
- The generated config's `repository` is the roles' local checkout,
  `~/Projects/switchyard`, whose own origin is
  `git@github.com:ebudai/switchyard.git`.

SYRD-545's guard compared the store's recorded URL with the checkout's path
alone. It treated two spellings of one project as two projects. The rewrite it
suggested would have pointed the store at a checkout holding generated project
state, instead of public main.

Measured read-only against the live `syrd` config and store: the base commit
refuses with exactly that message; this one accepts it.

## The change

`scripts/project_worktrees.py`, `control_repository_origin_mismatch`.

When the store's recorded URL does not name the checkout, the store is still
this project's when both of these hold.

1. **The checkout follows the same repository.** The checkout's own remote of
   the configured name (`worktree_remote`) is read from its `.git/config`. It
   is read as a file, as the store's is: by descriptor as root, with no git run
   against either. The checkout must have a `.git` directory of its own; a
   `.git` pointer or no `.git` proves nothing. The two URLs must be:
   - the same URL; or
   - one `owner/repository` on github.com itself, whatever the form: `https://`,
     `ssh://`, `git@github.com:`, with or without `.git`, in any case. Both
     hosts must be exactly `github.com`, read by the publication code's own
     `publication_remote_host`. An SSH alias (`github-...`) or a subdomain is
     not equated with it (Audit, round 2). `publication_uses_github` decides
     whether credentials apply, not where an alias resolves, and nothing here
     proves that. The whole `owner/repository` is compared, never a basename. No
     other host is treated as the same across forms.
2. **The store backs this project's worktrees.** Every role directory already on
   disk must positively be one of this store's worktrees (Audit, round 1):
   - a real directory;
   - its `.git` a regular file naming `gitdir: <store>/worktrees/<name>`;
   - that entry a directory of the store;
   - the entry's own `gitdir` naming this `.git` back, as `git worktree add`
     writes it.

   Only an absent role directory contributes no evidence. A missing,
   unreadable, malformed or linked `.git`, a role directory that is a link, a
   wrong back-pointer, or a read the owner's reads refuse is no proof, so the
   store is refused before any git. `role_worktree_foreign_store` is not enough
   here: it answers "" for metadata it cannot read, because its caller checks
   with git later, after this gate has already allowed the fetch. As root,
   every read is by descriptor from the owner's root and compared as written.

**Not changed:**
- A store following another checkout is refused before any git, with
  SYRD-545's message and remedies. So is a store following another owner's or
  another repository of the same name, or a host not proven equivalent.
- The store's remote is never repointed.
- The ordinary per-role foreign-worktree refusal still runs.

## Tests

`tests/control_repository_origin_test.py` (SYRD-545's suite) uses real
repositories and the real owner-correct git chokepoint.

- **The ticket's reproduction.** The checkout's origin is the store's origin,
  and config names the checkout. The store is used with exactly the git it
  always ran (`config`, `fetch origin`), is never repointed, and its worktrees
  refresh.
- **github.com forms.** HTTPS against SCP-style SSH, `ssh://` with other case,
  and `ssh://…:22` against HTTPS without `.git` are one repository.
- **Refused before any git, the store untouched:**
  - another owner's repository of the same name;
  - another repository;
  - one non-GitHub host in its HTTPS and SSH forms;
  - an SSH alias named like GitHub (`git@github-not-github:`) against
    github.com HTTPS, a `github-*` alias against github.com SSH, and a
    subdomain against github.com;
  - two local paths with one basename.
- **An existing worktree that proves nothing** is refused before any git:
  - a `.git` that is not a `gitdir` record;
  - a missing `.git`;
  - a `.git` link;
  - a store entry naming another worktree back;
  - a role directory link;
  - an unreadable `.git`.

  Each is accepted again once restored.
- **The same proof as root,** through the owner's reads: a store with its own
  worktrees is accepted. A malformed record, a `gitdir` named through `..`, a
  wrong back-pointer, a `.git` link and worktrees reached through a link are
  refused.
- **Without the rest of the proof:**
  - a matching remote with a role worktree another store added is refused, and
    accepted once that worktree is gone;
  - a checkout whose origin is renamed away, whose `.git` is a pointer, or that
    has no `.git` proves nothing.
- **SYRD-545's own cases** are unchanged and pass.
