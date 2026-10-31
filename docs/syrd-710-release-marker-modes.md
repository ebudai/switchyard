# SYRD-710: release modes are the installer's, not the caller's umask

## What happened

SYRD-705's v7 PGU cutover (2026-10-30) ran its root packet under `umask 077`,
as root packets do. `install-switchyard` wrote each release's
`.switchyard-release.json` with a shell redirect. It then only removed group
and other write bits (`chmod go-w`), which never adds a read bit. So both
releases it installed carried a root-owned 0600 marker.

PGU's tenant deploy migrated the database (90 to 191 migrations), then failed
before activation: Permission denied reading
`/opt/switchyard/releases/218a906d…/.switchyard-release.json`. The bounded
rollback restored the database and the legacy service. Unprivileged
`release-status` could not read the marker either.

On this host three installed releases carry a 0600 marker: `0b9fe6ce`
(current), `218a906d` and `a542ed35`. Nothing in any of them is group- or
other-writable.

### A second defect found on the way

`export_release` runs inside `release_dir="$(export_release …)"`, and bash
ignores `set -e` inside a command substitution. So the old
`verify_release_marker` could fail and the install still went on to repoint
`current` and write the trampoline: a marker naming another commit never
refused anything. The new check's failure exits explicitly.

## The change

All of it is in `scripts/install-switchyard`.

### A new release

- **Directories:** 0755, as before.
- **Files:** `chmod go=u-w`. Group and other get the owner's bits, minus
  write, so every file is readable by all and writable by its owner alone,
  whatever the umask left. An executable stays executable.
- **The marker:** exactly 0644.

### Every installed release, verified and repaired as root (`release_markers`)

This runs as the installing account (root under sudo) on every `--apply`,
after the new release is exported and before anything is activated.

- **The directory walk.** From the shared root down (`/opt/switchyard`,
  `releases`, each `<commit>`), each directory is opened with `O_NOFOLLOW` and
  must belong to the installing account and be writable by nobody else.
- **Release directories.** Each directory named by a 40-hex commit is checked.
  Staging directories and other names are skipped.
- **Each marker,** opened without following a link and non-blocking, must be:
  - a regular file;
  - the installing account's;
  - linked once;
  - at most 0644 (no group or other write, no execute, no special bits);
  - at most 64 KiB;
  - JSON whose `commit` is its own release's name.
- **The repair.** A marker that passes, but is narrower than 0644 (the 0600 an
  earlier install left), is set to 0644 through the descriptor that was checked.
  Each repair is printed.
- **A refusal.** Anything else (a symlink, another account's file, a hard link,
  a FIFO, a wider mode, a mismatched or unreadable commit) refuses the install
  with every reason. It does so before any marker is changed and before
  `current` moves.
- **The release being installed** must have a verified marker. One missing
  there refuses; one missing in an older release is left alone.

Reinstalling any release with this installer repairs all three markers on this
host. Since the repair covers every release, a rollback target is readable too.

### What is not changed

- Who owns a release (root), and that no release content is tenant-writable.
- The release layout, the canary, `current`, the trampoline and rollback.
- `__pycache__` directories that root's own Python runs create inside a release
  under `umask 077`. These are 0700 on this host and unreadable to tenants. They
  are harmless, because Python falls back to the source. They are outside this
  ticket.

## Tests

`tests/install_switchyard_release_marker_test.py` runs the real installer, with
this repository's own commit as the source and `umask 077`, as uid 0 of a user
namespace (`unshare --map-root-user --map-auto`). The tenant is a mapped uid
that reads root's files only through their "other" bits. It reads through the
deploy's own `source_release_commit` (from the installed release's
`ticket-board-service.sh`) and through `shared_release._read_switchyard_release_marker`.

- **A fresh install.** The marker is 0644 and names its commit. No release
  content is writable by group or others, every file is readable by others,
  every directory is 0755, and commands stay executable.
- **The tenant.** Its deploy and its Python reader both read and verify the
  marker. It cannot write anything in the release.
- **Repair.** A 0600 marker reproduces the cutover failure: the tenant's deploy
  check exits 2. A reinstall repairs that marker and an older release's, and the
  tenant reads again.
- **Refusals.** Each of these refuses with its reason, repairs nothing else and
  leaves `current` untouched, and a restore installs again:
  - a symlinked marker;
  - another account's marker;
  - one for another commit;
  - one that is not JSON;
  - a hard-linked marker;
  - one writable by others;
  - an executable marker;
  - a FIFO;
  - a missing marker;
  - a release another account owns;
  - a releases directory another account owns;
  - a releases symlink.

On the base commit the suite fails at its first mode check: the marker is 0600.
It needs user namespaces with subordinate ids, so it runs outside bwrap like the
other namespace suites.
