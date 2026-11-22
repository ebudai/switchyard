# SYRD-855: one tenant back to a release it already ran

## What happened

On 2026-11-22, syrd's notify listener crash-looped on release bceacabf, and
syrd needed to go back to cdc3a741. Both releases were on the host:
`/opt/switchyard/releases/cdc3a741…` was root's, and
`~/syrd-ticketboard-live/releases/cdc3a741…` was syrd's staged board release.
Every route was refused:

1. **`deploy-release commit=cdc3a741…`:** root's pin named bceacabf
   ("prepare it first").
2. **`preview-upgrade commit=cdc3a741…`:** the recorded source was
   bceacabf's installed release, and only the host's running release replaces
   it (SYRD-529).
3. **The operator's `sudo switchyard upgrade syrd --source-repo …/cdc3a741…`:**
   an upgrade stages only the release its own launcher runs
   (`stale_launcher_problems`). So it printed a host-wide reinstall of
   cdc3a741, which would have moved every tenant.
4. **`select-shared-release`:** this asks root for that same upgrade, so it
   fails the same way. It was also `auth_admin`, and the helper demanded a
   pane. A pane has nobody to authenticate, and a person's terminal is no
   pane, so no caller could satisfy it.

Each refusal is right for an upgrade. A rollback is not an upgrade: nothing
needs building or staging anew.

## The rollback

`switchyard deploy-release <project> --commit <A> --rollback [--any-staged]`,
root only. It is reached three ways:

| who | how | which release |
|---|---|---|
| the control pane | `privileged-action <p> rollback-release commit=<A>`, pre-authorized (`allow_active`, and in the per-tenant rule) | only the board release the tenant's last upgrade replaced |
| an administrator | `privileged-action <p> select-shared-release commit=<A>`, from their own terminal, `auth_admin` | any release staged for the tenant |
| the operator | `sudo switchyard deploy-release <p> --commit <A> --rollback [--any-staged]` | as the flag says |

In order, refusing with nothing changed at each step:

1. **A is staged for the tenant.** `<board root>/releases/<A>` is a real
   directory whose `.pgu-deploy-sha` is A, and the tenant does not already
   serve A.
2. **The control pane goes back one release only.** Root's note of the last
   upgrade (`release-rollback.json`, written before that upgrade replaced
   anything) must say it went to the release the board serves now, from A. So
   a pre-authorized caller cannot pick an older release with known defects.
   This is as narrow as `deploy-release`: one tenant, one release the
   operator already approved by installing it.
3. **Root holds A** (`trusted_release_root`). A must also be able to run
   under root's exact-release units.
4. **The schema rule** (below), read from the tenant's own database as the
   cluster's account.
5. **Root's pin names A** (A's installed release, keeping the recorded commit
   cache). Then the ordinary `deploy-release` runs: the listener is stopped,
   root's exact units for A are installed, A's migrations run (none are new)
   along with A's `rbac.sql`, and the tenant's deploy-restart reuses the
   staged release with its health gates and its own rollback. The listener is
   started, and the phase is closed only against what the running board
   reports.
6. **If the deploy fails** and the board is still on the release it left,
   the pin is put back. If the board did move, the pin keeps naming A, and the
   report says so.
7. **Once the board serves A,** the role tooling and pane hooks are restaged
   from A's installed release. Panes are not restarted.

The host's shared release, its privileged helper and policy, and every other
tenant are untouched. The run is journalled by the helper like any action;
its job records `rollback-release` or `select-shared-release`. Exit 3 (the
deploy failed and rolled back) is recorded as `rolled_back` for both.

The rollback runs the host's current launcher, not A's. In an outage the
broken release is usually that launcher, but only its served board and
listener are broken: the launcher still works, as it did on 2026-11-22. A
tenant can roll back with this command only once the host runs a release
that carries it. bceacabf does not.

## The schema rule

Migrations are never reversed: reversing one loses what was written under it.
So the older board and listener run against the newer schema. That is safe
only for a migration someone has checked against older code.

- **`scripts/ticket_board/schema_compatibility.py`** declares a verdict for
  every migration from pgu1051 on: compatible or not, why, and optionally a
  condition (`holds_while`). The condition is one SQL boolean expression
  that root evaluates on the tenant's database.
- **`tests/schema_compatibility_test.py`** fails for a migration added
  without a verdict, so its author decides. A condition may call only
  functions that its migration, or an earlier one, defines.
- **At the rollback:** each migration the database has applied that A does
  not carry must be declared compatible, and its condition must be true.
  An undeclared or unsafe migration, a false condition, or one that cannot be
  read refuses the rollback, naming each migration.
- **Older migrations are not declared,** so a rollback across one is refused.
  Nobody checked them, and that is said rather than assumed.

Compatible means the migration only adds objects older code never names, and
keeps the signature of every function it replaces. Any trigger it adds must
not refuse an update that older code makes. A's `rbac.sql` runs at the
rollback and takes EXECUTE on the newer functions away from the board roles.
A's code never calls them, and the next deploy of a newer release grants them
again.

**pgu1051 to 1055 (bceacabf over cdc3a741), checked for this ticket:**
- No migration alters an existing table.
- The only pre-existing functions replaced are `publisher_finish`,
  `publication_missing_signoffs` and `enforce_publisher_window`, all with the
  same signatures. Their new branches serve research findings only.
- Every new trigger returns at once unless the research stage or the plan
  gate is declared.

So:
- **1051, 1054 and 1055** are compatible while the research stage is not
  declared;
- **1052** is compatible while the plan gate is not declared;
- **1053** is compatible always. Its trigger writes only its own table, and an
  older Publisher publishes the implementer commit alone, as it did.

**For SYRD-854:** its automatic revert should call `roll_back`, or the same
checks (`staged_board_release_problem`, `previous_release_problem`,
`schema_problems`), and refuse where they refuse. Those are exactly the
releases whose code is not known to be safe against the newer schema.

## select-shared-release: a human-authenticated caller outside a pane

Of the two choices, this accepts the human. An `auth_admin` action's
authority is the administrator who authenticates, so:

- **The helper** proves no pane for an `auth_admin` action. Pre-authorized
  actions still need the registered control pane, which is their only proof.
- **The front door,** for an `auth_admin` action outside any pane, asks polkit
  about its own process. It goes ahead when polkit needs an authentication
  and there is a terminal to ask on. From a pane, or with no terminal, it
  refuses before asking, as before.
- **Why this cannot widen a pre-authorized path:** the per-tenant rule lists
  only `allow_active` actions (its test asserts that). So the helper running
  an `auth_admin` action means polkit had an administrator authenticate, or
  the caller was root.

The other choice, removing `auth_admin`, does not fit. Choosing any earlier
release, past the one-release limit, is wider than what a pane may do
unattended. The narrow, pre-authorized form is the new `rollback-release`.

What `select-shared-release` runs changed too. It is now the rollback with
`--any-staged`, not `upgrade --source-repo`, which could never select any
release but the running one. `install-shared-release`, the host-wide action,
is unchanged.

## Also changed

- **The way back printed after an upgrade** (`release_rollback_commands`)
  names `rollback-release` for the board release the upgrade replaced,
  falling back to its staged tooling's release. It says migrations stay
  applied under the rule, and names `select-shared-release` for an
  administrator.
- **The per-tenant polkit rule** gains `rollback-release`. The root upgrade
  replaces an installed rule whose bytes differ, so existing tenants get it
  at their next upgrade.
- **[Privileged actions: rolling one tenant back](privileged-actions.md#rolling-one-tenant-back)**
  is the maintained procedure; the Director guide and skill say how to roll
  back.

## Evidence

- **`tests/tenant_release_rollback_test.py`,** through the helper's real
  request parsing and `run_privileged_action` into the tree's
  `switchyard_main` as root. Systemd, PostgreSQL and the deploy are a
  recording runner, and the deploy really moves the board's `current`. It
  covers:
  - **B back to staged A:** the board serves and reports A, the listener
    runs, the phase is closed, the pin names A, the tooling is restaged from
    A after the deploy, and the attempt is journalled;
  - **the administrator's** wider choice;
  - **each refusal**, with nothing stopped, pinned or deployed: not the
    previous release, not staged, already on it, an undeclared migration, a
    declared condition false, and a condition unreadable;
  - **a release root does not hold:** refused before privilege;
  - **a failed deploy:** the pin is put back.
- **Reproduction against main bceacabf, in a worktree:**
  - main has no rollback;
  - its `select-shared-release` asks for `upgrade --source-repo`;
  - neither its helper nor its front door lets an administrator outside a
    pane ask for it.

  Now the helper runs it for them, a pre-authorized action still needs the
  pane, and the front door is ready only with a terminal to ask on.
- **`tests/schema_compatibility_test.py`:** every migration from pgu1051 on is
  declared, conditions name functions that exist by then, and each refusal is
  shown.

Not done here: no live rollback.
