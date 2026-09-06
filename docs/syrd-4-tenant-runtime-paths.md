# Tenant runtime paths and release deployment

Provisioned boards use `scripts/directorctl` from the selected immutable board release. The generated board and listener units pass that absolute path, and generated pane configuration places the same release's `scripts` directory first in `PATH`. Shared runtime code does not select `/home/agent` or install a tenant-local copy.

`plan.json` records `owner_user` and `owner_home`. The generated board root, assets, frames, environment files, hook paths, and user unit all derive from that home. The service helpers accept either an explicit absolute `BOARD_ROOT` or `TICKET_BOARD_OWNER_HOME`; with neither they exit before deployment. `scripts/install-directorctl` remains available for explicit legacy use, but it requires an absolute `DIRECTORCTL_INSTALL_PATH`.

Fresh provisioning writes project-specific board, canary, listener, tmpfiles, and polkit artifacts. `operator-commands.sh` creates the board release parent as the tenant owner, installs the reviewed board and canary units as root-owned files, and grants `boardsvc` only the traversal/read or asset/frame write ACLs rendered for that tenant. The shared source tree does not need a unit named for every tenant.

For an existing generated tenant, run `switchyard upgrade <project>` from the reviewed Switchyard release. It refreshes the generated artifacts and pins every pane to the tenant board release's `directorctl`. If the selected source is an exported shared release, configure and refresh the intended bare source cache first:

```bash
export SWITCHYARD_BARE_REPO=/path/to/the/configured/switchyard-source-cache.git
git --git-dir="$SWITCHYARD_BARE_REPO" fetch origin
switchyard upgrade <project>
```

Use the deployment sequence printed by the upgrade command. It deliberately:

1. stops the old user listener;
2. installs the generated board, canary, and listener units and reloads systemd;
3. runs the release export, migration, unit-drift check, canary, activation, restart, and live build verification as the tenant owner; and
4. reloads the owner's user manager and starts the listener from the matching release.

Do not start a listener from the new release before its migrations. For SYRD, retain the SYRD-3 listener override until this sequence has completed from an audited release and a real notification has been delivered. Then remove the override/copy as a separate operator action and verify the listener command, board build id, unit fragments, and notification delivery again.
