# SYRD-458: each role's pane commits as that role

## What happened

MEFP's four ephemeral luna workers have worktrees of one control repository.
A Git worktree has no config of its own: it reads its repository's single
config. One worker set `user.name` there, as `MEFP luna-2`, and from then on
every worker without a name of its own committed as luna-2. Luna-3's audited
MEFP-29 commit `d1c55a78` is recorded as `MEFP luna-2 <stellaris-agent@localhost>`,
and the merge `fa412933` carries it.

Switchyard never set any role identity. Nothing in `scripts/` writes
`user.name` for a role. The only identity it sets is its own, pinned with
`git -c` by `owner_git` for machine commits. Whatever a role's commits say
comes from whoever last wrote the repository's config. syrd has the same shape:
its control repository's config says `Switchyard Director`, and every worktree
of it inherits that.

## The change

`scripts/role_git_identity.py`, called from `_with_project_board_env`, gives
every role's pane this environment:

```
GIT_CONFIG_PARAMETERS='user.name=MEFP luna-3' 'user.email=luna-3@mefp.switchyard.invalid'
```

- **Name.** `<ticket prefix> <role>`, the form MEFP already used, such as
  `MEFP luna-3` or `SYRD main`. The prefix is short and always present; the
  display name can carry an apostrophe ("Morfane's Epic Fix Patch").
- **Address.** Under `.invalid`, which by RFC 2606 never routes.
- **One per role,** so a pool worker keeps the same identity across tickets, and
  no worker's name can become another's.
- **A role's own env wins.** If a role's config already sets
  `GIT_CONFIG_PARAMETERS`, its words come after the identity, so its settings,
  `user.name` included, win.

`GIT_CONFIG_PARAMETERS` is how `git -c` hands config to the commands it runs.
It is the "command line" scope, measured on Git 2.56.0:

| What also sets an identity | Who the commit names |
|---|---|
| the shared repository config (`MEFP luna-2`) | the role |
| an agent's own `git config user.name` | the role; the value lands in the shared config, and the pane ignores it |
| `git -c user.name=...` (Switchyard's `owner_git`) | the `-c` identity |
| `GIT_AUTHOR_*` / `GIT_COMMITTER_*` | those |

It changes nothing a commit already records:

| Run by another role's pane | Author | Committer |
|---|---|---|
| cherry-pick, with or without `-x` | the original | the role that ran it |
| rebase | the original | the role that ran it |
| amend | the original | the role that ran it |
| merge `--no-ff` | the role that ran it, and the merged parent keeps its own | the role that ran it |

`GIT_AUTHOR_*` env was the other candidate, and it measured the same in this
table, but it outranks `git -c`. It would have renamed Switchyard's own
`owner_git` commits to whichever role's pane ran them.

The documented `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n` form sets the same scope,
but it was ruled out:
- Codex 0.162.0's binary carries the default shell-environment exclude patterns
  `*KEY*`, `*SECRET*` and `*TOKEN*`, beside its `ignore_default_excludes`
  setting. A Codex pane's commands would likely lose `GIT_CONFIG_KEY_0`. This
  was not run, because running it needs a model call.
- A count without its keys stops every Git command:
  `error: missing config key GIT_CONFIG_KEY_0`, then
  `fatal: unable to parse command-line config`.

`git config --show-origin user.name` in a pane answers
`command line: MEFP luna-3`, so where the name came from is visible. The board
skill tells agents that their pane commits as their role and not to set
`user.name` with `git config`.

## Not changed

- No config, commit or history is rewritten. MEFP's shared config still says
  `MEFP luna-2`, and MEFP-29's commits keep their recorded author. A pane now
  ignores that config's identity, but a shell outside a pane still reads it.
- Ticket and commit provenance: the board verifies commits by hash, and no code
  or test in this repository reads a commit's author.
- A pane's identity is set when the pane starts, so a running pane keeps the
  old behaviour until it is relaunched with this release.

## Measured (`tests/role_git_identity_test.py`)

The same scenario runs on main `e38cedf2`, from a `git archive`, and on this
tree:
- A MEFP-shaped project: a three-member luna pool, an `ops` role whose own
  config sets `user.name` and `core.abbrev`, and a Director.
- Every worker's worktree belongs to one repository whose config already says
  `MEFP luna-2 <stellaris-agent@localhost>`.
- The config is read by the real `load_project_config`. Each pane runs the
  command `tmux_new_session_args` builds, through `sh -c` from the role's
  working directory, as tmux runs it.
- The CLI is a stand-in named `codex` that does what an agent does:
  - luna-2 first sets `user.name` itself, as MEFP's agent did;
  - each worker commits;
  - the Director cherry-picks luna-3's commit, then Switchyard's own `owner_git`
    makes a machine commit from the Director's pane.

**Main.** luna-1 and luna-3 commit as `MEFP luna-2 <stellaris-agent@localhost>`,
read from the shared config, and the Director's cherry-pick records luna-2 as
author and committer. This is MEFP-29.

**This tree:**
- each luna commits as itself, author and committer, and Git reports the name
  as `command line`;
- the shared config is byte-for-byte unchanged;
- `ops` keeps its own `user.name` and `core.abbrev`;
- the cherry-pick keeps luna-3 as author and records the Director as committer,
  and luna-3's own commit is untouched;
- the `owner_git` commit stays `Switchyard <switchyard@localhost>`;
- an apostrophe survives Git's own parsing. A line break in a role name becomes
  a space in the name and is dropped from the address; Git drops angle brackets
  itself.
