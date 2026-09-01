# PGU-841 Switchyard Shared Install Cutover Plan

This plan intentionally separates installation from live repointing. Build and
canary `/opt/switchyard` first; then repoint each live surface only in an
approved maintenance window.

## Build Shared Release

Run as a privileged operator. Clone a fresh checkout from the bare repository
for the audited commit, then run that checkout's installer:

```bash
tmpdir="$(mktemp -d)"
git clone /data/git/switchyard.git "$tmpdir"
sudo env \
  SWITCHYARD_SOURCE_REPO="$tmpdir" \
  SWITCHYARD_SOURCE_REF=<audited-commit> \
  SWITCHYARD_SHARED_INSTALL_ROOT=/opt/switchyard \
  SWITCHYARD_INSTALL_PATH=/usr/local/bin/switchyard \
  "$tmpdir/scripts/install-switchyard" --apply
```

This exports `/opt/switchyard/releases/<audited-commit>`, writes
`.switchyard-release.json`, runs a direct release canary, and repoints
`/opt/switchyard/current` with `ln -sfn`.

Rollback is deliberately a plain symlink operation that does not depend on the
active release:

```bash
sudo ln -sfn /opt/switchyard/releases/<previous-sha> /opt/switchyard/current
```

## A. Public Wrapper

Change: `/usr/local/bin/switchyard` becomes a trampoline to
`/opt/switchyard/current/switchyard`.

Order:
1. Install and canary the shared release directly.
2. Install the wrapper.
3. Verify `switchyard --help`, `switchyard --version`, and `switchyard status`.

Rollback:
1. Repoint `/opt/switchyard/current` to the previous release, or reinstall the
   previous wrapper from its source checkout.

Breakage if wrong: new shell invocations of `switchyard` fail. Already-running
panes do not re-read the wrapper.

## B. Tenant Pane Launcher

Change: generated tenant configs should use
`/opt/switchyard/current/scripts/team-launcher` for `pane_launcher`.

Order:
1. Complete A and verify the shared launcher path is executable.
2. For each tenant, run the project upgrade against its config.
3. Verify `switchyard <project> status` as the owner or through the approved
   elevation path before restarting panes.

Rollback:
1. Restore the tenant config from its pre-upgrade copy, or leave the config in
   place and repoint `/opt/switchyard/current` to the previous good release.

Breakage if wrong: future pane starts/reloads fail for that tenant. Running panes
survive until they are restarted.

## C. Board Deploy Source

Change: board deployment should eventually deploy from `/data/git/switchyard.git`
instead of the old pgu source.

Order:
1. Treat this as a separate sharp-edge change unless the director explicitly
   folds it into the maintenance window.
2. First verify the board deploy script, migrations, systemd unit render, and
   canary path from a Switchyard checkout.
3. Only then change the live deploy source.

Rollback:
1. Restore the prior deploy source and rerun the board deploy canary.
2. If the live service was restarted, use the board service rollback release
   path rather than editing tenant project state.

Breakage if wrong: the ticket board can fail to deploy or restart. This is the
only step here that can take the coordination system down, so it should be
separately audited if it is not already covered by the board deploy tests.

## PGU-834 Relationship

PGU-841 wholly subsumes the wrapper-copy part of PGU-834 for future Switchyard
installs: the public wrapper targets `/opt/switchyard/current/switchyard`, and
verb classification comes from the active release. It also partly subsumes the
runtime-copy staleness problem for tenant pane launchers, because generated
configs move to `/opt/switchyard/current/scripts/team-launcher` and shared
releases report their `.switchyard-release.json` commit marker instead of
requiring `.git`. It does not close tenant board release drift or stale project
checkouts; those remain visible status rows rather than auto-updated state.
