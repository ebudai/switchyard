# Desktop-policy upgrade ownership

Privileged desktop-policy upgrades atomically replace the generated launcher
configuration and `desktop-policy.json`. Both replacements must belong to the
configured tenant, even when an earlier upgrade left the old files owned by
root. Otherwise private `0600` files prevent ordinary Director configuration
and presentation operations after upgrade.

`configure_project_desktop` supplies the project owner to the atomic JSON
writer for these two controls. A privileged writer resolves the tenant account
and applies UID/GID to the temporary inode before replacement. Existing ordinary
permission bits are retained; private `0600` stays private, and a new file starts
at `0600`. Unprivileged tenant writes retain their natural ownership. Failure
before replacement leaves the old destination intact and removes the temporary
file. The normal writer behavior for other callers remains unchanged.

This does not chown directories recursively or alter protected GUI receipts,
desktop ACLs, or unrelated project files. Existing desktop consent, readiness,
installation and rollback behavior remains in place.

## Current-host recovery reference

Director has already supplied this narrow recovery to the operator for the two
files left by the prior `a542ed3` upgrade. This is a record of that recovery,
not a request to repeat it. Main does not execute privileged host operations.

```sh
sudo chown -- switchyard-agent:switchyard-agent \
  /home/switchyard-agent/Projects/switchyard/.switchyard/provision/syrd.json \
  /home/switchyard-agent/Projects/switchyard/.switchyard/provision/desktop-policy.json
```

The existing `0600` modes must remain unchanged. The provision directory was
already tenant-owned. Do not add a recursive option, a broader directory, GUI
receipt paths, or a permission-broadening chmod to the recovery. Director owns
subsequent ordinary workflow validation, presentation and rollout verification.
Future privileged upgrades must use the independently reviewed release carrying
this correction so that ownership survives the next replacement.

## Regression evidence

`tests/team_launcher_desktop_policy_owner_test.py` runs the full supported
`upgrade_project_command` against real generated controls in a disposable user
namespace. It exercises initially tenant-owned and root-owned files, repeated
headless and Wayland upgrades, existing private modes, missing policy creation,
dry-run stability and ownership-failure cleanup. It checks temporary inode
ownership and mode before actual replacement, then drops to the tenant UID/GID
and opens both files for real read/write. Protected receipt and unrelated
metadata/content remain unchanged and the tenant cannot read the receipt.
GUI installation/readiness and host commands use fixtures; file generation,
ownership, replacement and tenant access are real.

Unprivileged execution requires user namespaces and subordinate UID/GID maps;
unsupported environments fail visibly. Removing the owner selection or moving
ownership assignment after replacement fails the pre-publication assertion.
Existing exported desktop-access coverage includes a fixture account for its
simulated privileged owner; it continues executing the actual atomic writer.
