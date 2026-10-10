# SYRD-571: the size review reads a commit cache another account owns

## What happened

The board's size review recorded `scan_failed` for SYRD-550 and SYRD-556, with
no file measured. The cause was that Git refused the trusted commit cache,
`/home/switchyard-agent/syrd-source-cache.git`, as dubious ownership. The
board runs as `boardsvc`. The cache belongs to `switchyard-agent`, and the
board reaches it through one named ACL, `user:boardsvc:r-x`.

Every Git call the scan made was `git -C <cache> …`. `-C` makes Git find the
repository by discovery, and discovery refuses a repository owned by another
account. The board's `commit_hash` checks never hit this, because
`commit_cache` names a bare cache with `--git-dir`, and Git does not run the
ownership check on an explicit git directory. The SYRD-541 suite ran as the
cache's owner, so it never saw the difference.

Measured with Git 2.56, as a uid other than the owner of a bare repository:
- `git -C <cache> rev-parse HEAD` fails with "detected dubious ownership";
- `git --git-dir=<cache> rev-parse HEAD` succeeds;
- `git -c safe.directory=<cache> -C <cache> rev-parse HEAD` succeeds.

## The boundary: `scripts/ticket_board/commit_cache_reader.py`

`cache_reader(path)` opens one configured cache for the board's own account
and returns a `CacheReader`. The size review measures only through it. Its
`git_command(args)` builds every Git call the scan makes:

```
git -c safe.directory=<git dir> --git-dir=<git dir> --no-replace-objects <read>
```

**How it names the repository:**
- **Exactly, never by discovery.** It uses the cache's git directory: a bare
  repository itself, or a checkout's `.git` directory.
- **The ownership allowance is per call.** `safe.directory` names that one
  path on the command line. No config file is written, global or system.
- **Each layer is enough alone, for now.** `--git-dir` already satisfies
  Git 2.56. The per-call allowance keeps the reader working if Git ever
  checks explicit git directories too. Mutation shows this: removing either
  one alone survives, and removing both is killed.

**Reads only.**
- It refuses any subcommand except `rev-parse`, `merge-base`, `cat-file`,
  `diff` and `ls-tree`.
- It refuses a `diff` without `--no-ext-diff --no-textconv`.

**Nothing the owner configures runs or redirects the read.** The cache's
config, `info/attributes` and refs are the owner's to write, so:
- **The owner's diff programs and textconv never run.** In the fixture they
  are live: an ordinary `git diff` in the cache runs the owner's program.
- **Replace refs are ignored** (`--no-replace-objects`). Otherwise the
  fixture's candidate is read as a different commit.
- **Global config is not read** (`GIT_CONFIG_GLOBAL=/dev/null`). The board's
  `HOME` is the owner's home (`Environment=HOME=` in the unit). An unparsable
  `~/.gitconfig` there stops every read otherwise.
- **The board never fetches** (`GIT_NO_LAZY_FETCH=1`). If the cache is
  configured as a partial clone, reading a missing object would fetch from its
  promisor remote, and that runs the remote's configured `upload-pack`
  program.
  - In a cache of one's own, that program ran. Measured in a scratch
    repository.
  - With the board's read-only grant, Git 2.56 stops the fetch at a config
    lock first, so the suite cannot observe this guard: its mutant survives.
    The guard is the reader's own promise not to fetch. Without it, safety
    would rest on that grant and on Git's order of steps.
- **Inherited `GIT_*` variables are dropped.** The unit's `EnvironmentFile=`
  is in the owner's home. `GIT_DIR`, `GIT_OBJECT_DIRECTORY`,
  `GIT_EXTERNAL_DIFF` or `GIT_CONFIG_PARAMETERS` set there redirect nothing.

**What it refuses to open,** each with a precise reason:
- **A symlink.** If the configured path, or its `.git`, is a symlink, it is
  refused, even when the symlink points at a repository that has the commit.
- **A linked checkout's `.git` file.**
- **A path that does not exist.**
- **A directory with no `objects` or `refs`.**
- **A cache the board's account cannot read.** This is asked of the board's
  own account, and the refusal names the repair.

`file_size_policy` itself is unchanged for the pre-commit warning. Its `_git`
takes either a checkout path, which it runs with `-C` as before, or a reader.
Its diffs now say `--no-ext-diff --no-textconv`. `resolve` adds
`--end-of-options`.

## Ticket input

The scan measures only commit ids: 7–40 hex digits. These are refused before
Git runs and recorded nowhere:
- an option (`--output=…`);
- a path;
- a ref the cache does have (`roles/main/c550`);
- a tree path (`<sha>:scripts`).

The repository is only ever the configured cache.

**Verified before measuring:**
- **The candidate.**
- **The base an earlier scan recorded,** when a close measures against it.
- **The cache's main,** when a candidate is measured against its merge-base.

## Reasons, and the supported repair

Each failure is recorded as the `scan_failed` finding's detail, so it shows on
the ticket's `size_review`.

| Failure | Recorded |
|---|---|
| The commit is not in the cache | `the candidate <sha> is not in the board's trusted commit cache <path>. Repair: publish it (switchyard-publish-candidate brings the cache up to what it publishes); the next approval or close measures again, or the Director runs ticket-board-write measure-size <ticket> --commit <sha>` |
| A recorded base is not in the cache | the same, for `the base <sha>` |
| The board's account cannot open the cache, or its `objects` or `refs` | `the board cannot read its trusted commit cache <path>: …. Repair: pkexec switchyard repair-boundary <project> --apply grants the board service read access to its commit store` |
| One object the board may not read | Git's own reason (`Permission denied`), never "missing", with the same repair |
| The cache is missing, not a repository, or a symlink | that, with its path |
| `enable-size-review` cannot take its baseline | `cannot take a baseline: <the same reasons>` |

`repair-boundary` re-applies root's own repository-boundary phase. That phase
includes the board service's named, read-only grant on its commit store
(`commit_store_read_commands`).

## Enabling the review

`enable-size-review` takes its baseline inventory through the same reader. On
a board provisioned as today's are, it needs no Git configuration of any kind.
The suite enables it with a board `HOME` that holds no Git config, and then
checks that nothing wrote one.

## Not changed

- **`commit_cache`'s own reads.** These are the `commit_hash` checks, the
  publication proof and the refresh fetch. They use `--git-dir` for a bare
  cache and were not failing. The board's read-only grant also stops a lazy
  fetch there at the config lock. A checkout-shaped cache (`.git` present)
  would still take `-C` there, and could follow as its own ticket.
- **The SQL.** There are no migration, schema or RBAC changes. The recorded
  shape is SYRD-541's: `size_scans.report` lists files at or over 1,100
  lines, and `error` is null for a completed scan.
- **The listener.** It runs no Git and reads nothing from the cache. Findings
  and scans are durable rows, read again by whichever board process comes up
  next.

## Tests: `tests/size_review_cache_ownership_test.py`

**The fixture is the live shape.**
- **The cache's owner.** A bare cache is chowned, inside a user namespace over
  this account's subordinate ids, to a host uid that is not this one.
- **Access.** It is closed (`other::---`) and granted to this account by name,
  read-only.
- **Discovery.** The suite first checks that Git's discovery refuses it.
- **The board.** This account plays the board service.
- **Hostile config.** The owner also configures an external diff, a diff
  driver with textconv and command, an fsmonitor, a replace ref, and a
  partial-clone promisor whose `upload-pack` is the same program.
- **The commits** reproduce the measured shapes:
  - **SYRD-550:** `tests/ticket_board_notify_listener_test.py` grows from
    3,851 to 3,859 lines, and `pane_activity_gate.py` from 1,086 to 1,098;
  - **SYRD-556:** the idle hook grows from 871 to 990 lines, plus a new
    321-line test, on top of SYRD-550.

**The board is a production build.**
- **Database:** `schema.sql`, the real `ticket-board-migrate`, `rbac.sql` and
  a declared workflow.
- **Driven through:** the real write CLI on the real socket.
- **Restarts are real.** Each board is its own child process.

**The before-run** is main at `a025c35b`, from a git archive, on the same
cache:
- both shapes are `scan_failed` on "dubious ownership", with no measurement;
- `enable-size-review` fails the same way.

**This tree:**
- **SYRD-550's shape is measured** against main: the listener test is
  growth, from 3,851 to 3,859 lines.
- **The review is enabled with no Git config.** After that, the same growth
  on another ticket is a valid finding that refuses Audit's approval, naming
  the file.
- **Across a restart:**
  - the next board process finds that measurement and finding unchanged, and
    measures again;
  - SYRD-556's shape is measured against the new main, with nothing at or
    over 1,100 lines, and approved.

  From the restart on, the board's `HOME` holds a Git config that Git cannot
  parse.
- **The missing-commit, unreadable-cache, unreadable-objects,
  unreadable-object and missing-recorded-base reasons** each carry the repair
  above. After the owner publishes the commit, or the grant is restored, the
  next approval measures.
- **No attack takes effect:**
  - none of the owner's configured programs runs;
  - the replace ref and a hostile environment do not change what is measured;
  - option, path, ref and tree-path inputs are never measured;
  - a symlink in place of the cache is not followed.

**Mutation:** 25 mutants, of which 21 were killed. Each run was bounded
(`tests/bounded_run.py`), and each kill's assertion line was read. The 4
survivors, which also pass `size_review_test`:
- removing `--git-dir` alone;
- removing the per-call `safe.directory` alone;
- dropping `--end-of-options`, which sits behind the commit-id check;
- dropping `GIT_NO_LAZY_FETCH`, which the read-only grant hides, as above.

The first two are the layered pair above, and removing both is killed.
