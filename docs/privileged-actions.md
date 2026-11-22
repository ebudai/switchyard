# Privileged actions without a prompt

A tenant's Director upgrades and deploys its own project through the approved
privileged boundary:

```
switchyard privileged-action <project> preview-upgrade commit=<release commit>
switchyard privileged-action <project> upgrade-tenant-release commit=<release commit>
switchyard privileged-action <project> deploy-release commit=<release commit>
```

Each one runs a single root-owned program, the privileged helper, through
`pkexec`. None of them asks for a password from the tenant's own Director.

## What is prompt-free, and for whom

Each tenant has a polkit rule of its own, `/etc/polkit-1/rules.d/49-<project>-tenant-upgrade.rules`.
It answers yes without a prompt only when all three of these hold:

- **The account is that tenant's owner,** and no other account.
- **The action is one of the tenant's own upgrade actions:** `preview-upgrade`,
  `upgrade-tenant-release`, `deploy-release`, `upgrade-tenant` and
  `rollback-release`.
- **The program is the root-owned privileged helper,** as `pkexec` names it.

Everything else asks for an administrator, as before. That covers installing
the shared release everyone runs (`install-shared-release`), choosing any
earlier staged release for a tenant (`select-shared-release`), and the database
actions.

## Why it is still safe

The rule decides only that the owner's process reaches the helper without a
password. The helper still decides whether the action runs:

- **It runs only for the project's registered control pane.** It walks its
  own ancestry to the pane the command was typed in, and refuses any process
  that is not that pane. That includes another role's pane on the same
  account, and a shell detached from any pane.
- **It acts only on the pinned release commit given,** with fixed arguments,
  never a command line of the caller's.
- **It journals every attempt.** It records who ran it, through which route,
  and how it ended, in `/var/lib/switchyard/rollout/<project>`.

Without the rule, polkit skips the password only when it judges the calling
process to be in an active desktop session. A Director's CLI can run its
commands somewhere polkit does not judge active, such as a tool sandbox. So
the same upgrade asked for a password on one tenant and nothing on another.
The rule removes that difference without widening the shipped policy for
every account.

## Installing it

- **A new tenant** gets the rule from provisioning, beside the board's deploy rule.
- **An existing tenant** gets it from its next root upgrade. That is
  `upgrade-tenant-release`, so the upgrade that installs the rule asks once,
  and later ones do not.

Every upgrade says whether the rule is in place:

- **Run as root,** it confirms the rule, installs a missing one, or replaces
  one whose bytes or mode differ. It never writes through a symlink.
- **Run as the tenant,** it cannot read root's rules directory, so it says so
  in a note and names the root upgrade that installs the rule. It never
  counts that against readiness.

Removing a tenant (`switchyard teardown`) removes its rule.

## The dry run and root-only checks

A tenant's own `switchyard upgrade <project> --dry-run` cannot make checks only
root can make, such as reading the tenant-control sudoers rule. It reports each
one as a note, names `upgrade-tenant-release` as the step that makes it as
root, and does not hold it against readiness. A real upgrade run as the tenant
still stops there, because it would have to install the rule.

## Rolling one tenant back

```
switchyard privileged-action <project> rollback-release commit=<previous release>
```

It returns the tenant to the board release its last upgrade replaced: its
board, its notify listener and its role tooling. That release must still be
staged for the tenant (`<board root>/releases/<commit>`) and installed by root
(`/opt/switchyard/releases/<commit>`). It never changes the host's shared
release or any other tenant. It is pre-authorized like `deploy-release`, from
the control pane, so it works in an outage with nobody there to type a
password.

Migrations are never reversed. The older board and listener run against the
newer schema, so the rollback first reads which migrations the database has
that the older release does not carry. Each must be declared safe for older
code in `scripts/ticket_board/schema_compatibility.py`, and any condition on
that (such as "the research stage is not declared") must hold on the tenant's
database. Otherwise it refuses, names each migration, and changes nothing.
Then root's pin names the older release, `deploy-release` deploys it with its
usual health gates, and the role tooling is restaged from it. A deploy that
fails puts the pin back. Every run is journalled, like any privileged action.

An administrator can choose any earlier staged release, not only the last one:

```
switchyard privileged-action <project> select-shared-release commit=<release>
```

They run it from their own terminal and authenticate as an administrator.
The helper asks no pane for this action: the administrator's authentication is
its authority. From a pane, or with no terminal to ask on, it is refused before
anything is asked.

The operator's own form is one command, with the same checks:

```
sudo switchyard deploy-release <project> --commit <release> --rollback [--any-staged]
```
