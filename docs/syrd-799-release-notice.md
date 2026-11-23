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
- then `switchyard release-status <tenant>` to check, and the statement that nothing was upgraded;
- the tenant's own reports this release resolves (SYRD-868), above the subjects so no cap drops them.

## Reports the release resolves (SYRD-868)

The User, 2026-11-23: "the notice that goes out when a ticket submitted downstream is completed is kind of redundant
if it results in a new release. they will get the release notification anyway". So a report closed Done upstream on a
commit that neither the reporter's release nor the release installed on the host contains is recorded on the
reporter's board without a notice (`report_closure.awaits_release`). The next install must carry the fix, so its
release notice must say so, or the link is lost:

- **Installed before Done (Audit, round 1).** On this board, publication puts the fix on main before the report is
  Done, so an install often carries a fix before its report closes. That install's notice could not name the report,
  and no later one will, because the reporter is no longer behind on that commit. So the reporter's listener names
  the host's installed release (`/opt/switchyard/current`) to the upstream board (`&installed=`), and the board says
  whether that release contains the closing commit (`installed_contains`). A close the installed release already
  contains is announced, with that release's commit in the three actions. A close where either answer is unknown is
  announced too. No ordering of install, Done and upgrade is silent. A Done close followed by an install within one
  listener poll (15 minutes) can give both notices.
- **An install that told the reporter nothing (SYRD-869).** ./install can list a tenant as not told (not
  registered, no ticket to carry the notice, a board from before release notices), so a silent close stays pending.
  A close recorded without a notice is told late, once (close key `done_installed#<reopens>`), when the installed
  release contains it and the reporter's does not -- unless a release notice this board recorded since it recorded
  that close named it (one that named an earlier close of a reopened report is not this one's). Only that
  ends the wait quietly, or the reporter's own release carrying the fix (SYRD-870): a notice can reach the board and
  name nothing, when root's read of the upstream board failed. pgu1058 keeps each notice's message in
  `release_notices.message`, and the listener looks there for "This release resolves your report(s) ... on
  <upstream>'s board" naming the ticket, a capped list's "and N more" aside. A record it cannot read, of either
  table, counts as owed: a duplicate rather than silence. rbac.sql lets the listener read `report_closures` and
  `release_notices`. (SYRD-869's listeners asked the board whether the newest noticed release carried the fix
  (`&noticed=`); the board still answers that, and no listener now asks.)

- **Where it comes from.** Root reads every tenant board on the host as the database superuser, the same way it makes
  its one write: each ticket in `report_provenance` that is done on a closing commit, with the reporting project the
  board recorded when the report was filed. It also reads the commits from the tenant's release to the new one in the
  installing checkout, as its owner, merges included (a report may close on its integration merge).
- **What is named.** For each tenant, its own reports whose closing commit (full or abbreviated) is among those
  commits, by board: "This release resolves your reports SYRD-9, SYRD-866 on syrd's board." A tenant with none gets no
  such line. A board that cannot be read resolves nothing, and the notice still goes.
- **Still announced as closes:** a cancelled report, a Done close with no commit, one the reporter's release already
  contains, and one where that cannot be told. No release follows those, or none is known to.
- **With the reason, where no release will tell** (the Director, at routing): a won't-fix, recorded as a cancel or as
  a Done close with no commit, and a no-code close (only a configuration change). A cancel carries its Director's
  latest comment, as before. A Done close with no commit carries the reason its no-code submission recorded, or else
  its Director's latest comment. A declared board writes that reason as a plain comment, so the upstream board keeps
  it when the move is made: pgu1057's trigger records the ticket's newest comment on a move whose declared transition
  is `allow_no_code`, and nothing else. A close from before pgu1057, or on an undeclared board, falls back to the
  Director's comment.

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

SYRD-868 adds, in tests/report_close_notice_test.py: the install-before-Done ordering told once, with the installed
release, and the upgrade to it telling nothing more; a close neither release contains, silent; either answer unknown,
told. In tests/release_notice_test.py: the notice naming a tenant's resolved reports, short commits too, above sixty subjects capped to
twenty; another tenant's report, and one closed on a later commit, not named; a tenant with none given no line; root's
read of a production-built board returning exactly the reports closed done on a commit; and the commits between two
releases, merges included. Its six mutants (any reporter, any commit, the line below the subjects, merges dropped, any
state read, no line) are each killed by an assertion.

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
