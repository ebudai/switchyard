# SYRD-876: pgu's and otto's tenant-owned commit caches, for the upgrade that adds them

**Decision (2026-11-24): deferred to each tenant's next upgrade.** The User approved tenant-owned caches for pgu and otto,
so that SYRD-872's commit refresh works there. On these two tenants a cache can't be added without upgrading the tenant.
Both are dormant, and upgrading them is the User's separate call. This note is what the operator runs when either
tenant is upgraded. Nothing below has been run against either tenant.

## Where they are today (read-only checks, 2026-11-24)

| | pgu | otto |
|---|---|---|
| board unit `TICKET_BOARD_COMMIT_GIT_DIR` | `/data/git/switchyard.git:/data/git/pgu.git` | `/data/git/otto_scheduler.git` |
| what those are | the tenant's **remotes**, eric's, `0755`, read in place | the same |
| board release | 218a906d (2026-10-30) | 18a7b84b (2026-10-29) |
| has SYRD-872 (45f0d9f7, `commit_refresh.py`) | no | no |
| tenant owner (board unit `TICKET_BOARD_TENANT_USER`, registry `config_path`) | `pgu-agent` | `otto-agent` |
| listener | a user service of **`agent`** (uid 1001), `~/pgu-ticketboard-live/current`, started 2026-11-05 | a user service of `otto-agent`, `~/otto-ticketboard-live/current` |
| last board activity | 2026-09-17 | 2026-10-21 |

- **Why a repoint alone would break them.** Today each board reads its remote directly, so a pushed commit resolves at
  once. On these releases nothing ever fetches a tenant-owned cache: the board never fetches, and their listeners
  predate SYRD-872's refresh. Every new commit would then be unknown.
- **So the cache is set in the upgrade itself.** The upgrade must move to a release at or after 45f0d9f7, with
  `--commit-git-dir`.
- **Every other tenant keeps its cache in its owner's home:**
  - syrd: `/home/switchyard-agent/syrd-source-cache.git`;
  - mefp: `/home/stellaris-agent/mefp-commit-cache.git`;
  - sapi and pie: their own `control.git`.

  The board reads it through provisioning's named grant (`commit_store_read_commands`): `u:boardsvc:rX` with a default
  ACL, and `--x` on the directories above. That grant is made only for a store inside the owner's home.

## The values

**pgu: `/home/pgu-agent/pgu-commit-cache.git:/data/git/switchyard.git`.**
- **The cache's origin is `/data/git/pgu.git`** (the Director, from the User: "pgu.git should be pgu's").
- **The owner is `pgu-agent`, not `agent`.** The board unit and the registry name `pgu-agent`, and the boardsvc grant
  applies only inside the owner's home. Only pgu's legacy listener runs as `agent`.
- **`/data/git/switchyard.git` stays,** read in place as a second entry, and is never refreshed. Of the 635 distinct
  commits pgu's tickets cite in `commit_hash`:
  - 580 resolve in `pgu.git` only;
  - 1 resolves in both;
  - **53 resolve only in `switchyard.git`** (PGU-841 to PGU-919, from September 2026);
  - 1 resolves in neither.

  It hasn't changed since 2026-09-05, so a second fetch source would add nothing.
- **The refresh never writes there.** `refresh_commit_repos` takes the integration lock (`switchyard-integration.lock`)
  in each listed repository before anything else. pgu's account can neither create nor open it in eric's `0755`
  repository, so that entry is skipped and no git runs in it. `tests/commit_refresh_test.py` pins this.

**otto: `/home/otto-agent/otto-commit-cache.git`,** with `/data/git/otto_scheduler.git` as its origin. All 254 commits
otto's tickets cite resolve there.

The measurement read each board with one `GET /api/board`. It read the remotes only with the board's `CacheReader`
prefix (`cat-file`, hooks off, no global config). Their refs and config were byte-identical before and after.

## Preflight (read-only; root or the tenant)

1. **The tenant's real origin.** Read `remote.origin.url` in the tenant's own control repository (the
   `control_repository` in its provision plan). If it isn't the `/data/git` remote above, seed from that origin and say
   so on the ticket.
2. **The plan's `owner_home` and recorded `commit_git_dir`:**
   - the tenant's plan, `~<owner>/Projects/<project>/.switchyard/provision/<project>.json`;
   - root's record under `/etc/switchyard/provision/<project>/`.

   `owner_home` must be the home the cache goes in.
3. **The listeners.** `ps -eo user,pid,args | grep ticket-board-notify-listener`, and `/proc/<pid>/cgroup`.
   - For pgu, expect today's `agent` user service. After the upgrade, pgu's listener must be `pgu-agent`'s user service.
     Once that runs, stop and disable `agent`'s:
     `sudo -u agent XDG_RUNTIME_DIR=/run/user/1001 systemctl --user disable --now pgu-ticket-board-notify-listener.service`.
     Then confirm exactly one pgu listener remains.
   - Two listeners on one board would both deliver.
4. **The baseline.**
   - `sha256sum` of each `/data/git` remote's `config` and `packed-refs`, plus `git --git-dir=<remote> for-each-ref`
     (with the `CacheReader` prefix), to compare afterwards.
   - The cited-commit count, from the measurement above.

## Seed the cache (as the tenant, before the upgrade)

```
sudo -u pgu-agent -H git clone --bare --no-local /data/git/pgu.git /home/pgu-agent/pgu-commit-cache.git
sudo -u pgu-agent -H git --git-dir=/home/pgu-agent/pgu-commit-cache.git config remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'
test ! -e /home/pgu-agent/pgu-commit-cache.git/objects/info/alternates && echo "no alternates: ok"
```

For otto, use `otto-agent`, `/data/git/otto_scheduler.git` and `/home/otto-agent/otto-commit-cache.git`.

**Always `--no-local`, never `--shared`.**
- A plain local clone hardlinks eric's object files into the cache. The upgrade's boundary phase then runs `setfacl -R`
  as root on the cache, which changes the ACLs of those shared inodes, and so modifies eric's repository.
- `--shared` writes an `alternates` file pointing into eric's repository, and the board would read through it.

## The upgrade (the operator)

```
sudo switchyard upgrade pgu --dry-run --deploy-ref <release at or after 45f0d9f7> \
    --commit-git-dir /home/pgu-agent/pgu-commit-cache.git:/data/git/switchyard.git
sudo switchyard upgrade pgu --deploy-ref <the same release> \
    --commit-git-dir /home/pgu-agent/pgu-commit-cache.git:/data/git/switchyard.git
```

For otto: `--commit-git-dir /home/otto-agent/otto-commit-cache.git`.

`--commit-git-dir` replaces and persists the value in the plan and root's pin. The upgrade writes the board unit, with
`--commit-git-dir` on its command line and in `Environment=`.

The boardsvc grant on the cache is provisioning's boundary step, made on the configured store (`commit_store_read_commands`).
Check it after the upgrade: `pkexec switchyard repair-boundary pgu` reports what is still missing, and `--apply` grants it.
Nothing is granted on `/data/git`, and nothing there is changed.

## Proof (after the upgrade)

- **New commits resolve.** A commit pushed to the origin after the switch resolves on the board through the listener's
  refresh: the cache's `switchyard-refresh-served.json` gains the ask's token, the board reports no "unknown commit",
  and boardsvc runs no fetch.
- **Old commits still resolve.** Rerun the cited-commit count against the new list. It must be the same as before:
  for pgu 580 + 1 + 53, with the 1 unresolved unchanged; for otto 254.
- **The remotes are untouched.** The `/data/git` remotes' `config`, `packed-refs` and `for-each-ref` output are
  identical to the baseline.

## Rollback

```
sudo switchyard upgrade pgu --deploy-ref <the release it now runs> --commit-git-dir /data/git/switchyard.git:/data/git/pgu.git
sudo switchyard upgrade otto --deploy-ref <the release it now runs> --commit-git-dir /data/git/otto_scheduler.git
```

These put the old value back, in the same "replace and persist" way. Two cautions:
- `deploy-release --rollback` (SYRD-855) keeps the current pin's `commit_git_dir`, so it **does not** restore the old
  path.
- A release rollback to before 45f0d9f7, while the tenant cache is still configured, would leave the cache unrefreshed.
  Run the cache rollback first.
