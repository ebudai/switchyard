# SYRD-799: ./install tells each tenant's Director, once, that a new release is ready

The User, 2026-11-15: "what do you think about the ./install script automatically informing all active directors that
a new version is ready for them to upgrade to? I find its a lot going to each one and telling them by hand".

Each tenant stays pinned to the release it last deployed. Its Director moves it with the pinned privileged actions
for the new commit, and until now the User relayed that to each Director by hand (sapi and otto for SYRD-791/796).

## What ./install does now

At its end, as root, `./install` runs `scripts/release_notice.py`:
- **Which tenants.** Each tenant that root's provision records name and root's registry registers
  (`database_auth_transition.root_tenants`, which reads only files root owns).
- **Its current release** is the one its board unit runs, as root proves it from systemd and /proc
  (`board_identity.effective_unit`). It is never a tenant-configured path or URL.
- **Skipped:** a tenant already on the installed release. **Not told, with the reason:** a tenant whose board cannot
  be proved (not running, or a unit root does not trust). Nothing it finds fails the install.
- **Told:** every other tenant, through its own board. Root calls `ticket_board.note_release_available` on the
  database root's provision record names, through `runuser -u postgres`, passing the values as psql variables. This is
  the one write root makes, and only the database superuser may make it: `rbac.sql` grants it to no board role.
- **Once per release.** The board records each release it was told (`release_notices`) and queues one Director notice
  (kind `release_available`), delivered like any Director notice: held while the pane is busy, never typed by root.
  A reinstall, or a retry, tells nobody.
- **One line at the end**, for example: `switchyard: told 2 Directors release a1b2c3d4e5f6 is ready: otto, sapi;
  skipped syrd (already on it or told); not told: pie (its board socket ... does not answer)`.

## The notice

The notice is self-contained:
- the new release and the one the tenant runs;
- the commits between them, read from the installing checkout as its owner: up to 20 subjects, the rest counted;
- the three commands with `commit=<new>`:
  - `switchyard privileged-action <tenant> preview-upgrade commit=<new>`;
  - `... upgrade-tenant-release ...`;
  - `... deploy-release ...`;
- then `switchyard release-status <tenant>` to check, and the statement that nothing was upgraded.

## Why the database, not a board route

The ticket suggested a new root write over root's kernel-proven board connection (SYRD-769's `unit_connection`).
That connection is read-only today. A write route on it would mean admitting uid 0 through the board socket's peer
check, and `server.py` is at its size limit.

The write here keeps the socket's boundary exactly as it is, and is as narrowly bound:
- **the right board:** its database is named by root's own provision record, not by the tenant;
- **the right caller:** the kernel proves the caller to PostgreSQL by peer authentication on the local socket, and
  only the superuser may run the function;
- **an existing channel:** root already provisions and migrates every tenant's database the same way.

## Tests

`tests/release_notice_test.py`:
- two tenants behind and one current: exactly two notices, each with its tenant's three commands, both commits and the
  commits between;
- an unreachable board, and a provision record root cannot read, listed as not told, and the run goes on;
- an unregistered tenant not looked at;
- a rerun tells nobody;
- capped notices;
- run by another account: it says so and succeeds;
- on a production-built board:
  - a notice holding quotes and a semicolon is stored and delivered exactly as written, once;
  - the same release again is `already`, with nothing queued;
  - the write is refused to the board service, the listener and every role.

Six mutants, all killed: the behind check dropped, the once-per-release record dropped, an unregistered tenant told,
an unreachable board failing the run, the commands naming the old commit, and `release_available` not treated as the
Director's own kind (a quiet board, whose only Director ticket is done, still gets the notice delivered once).

## For the first install of this release

A board can take the notice only once its tenant has deployed a release that carries `pgu1046`: migrations run at
`deploy-release`. So on the first `./install` of this release, and of any later one until each tenant has deployed it
once, every tenant still behind is listed as not told: "its board predates release notices: tell it of this release
yourself; once it deploys it, later releases are announced to its Director". Relay this one release by hand.

## "Behind" is the release the board runs

A tenant is behind when its board unit runs another release, not when its pin differs. A tenant that has run
`upgrade-tenant-release` for the new commit but not yet `deploy-release` is told again, once. That is harmless: the
notice is the same, and the board records it.

## A quiet board

On a quiet board the notice's anchor can be a finished ticket: the last one handed to the Director. The listener
treats `release_available` as the Director's own kind, current whatever its anchor is, so the notice is still
delivered. Without that, a notice recorded as told and never delivered would be skipped by every later install.
