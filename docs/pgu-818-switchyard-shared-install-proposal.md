# PGU-818 Switchyard Shared Install Proposal

## Decision

Install Switchyard as a host-level application under `/opt/switchyard`, with a
stable public entrypoint at `/usr/local/bin/switchyard`.

The wrapper should execute:

```text
/opt/switchyard/current/switchyard
```

`/opt/switchyard/current` is an atomic symlink to an immutable release directory:

```text
/opt/switchyard/
  releases/
    <git-sha>/
      switchyard
      scripts/
      docs/
      config/
  current -> releases/<git-sha>
```

This removes the executable code path from user homes. A caller who cannot
traverse `/home/eric`, `/home/agent`, or another tenant owner's home can still
run `switchyard --help` and `switchyard --version`, and can run commands for a
project it owns, because the entrypoint and code are world-readable/executable
host files. Cross-tenant commands such as global `switchyard status` and
`switchyard <project>` for a project owned by another user still need elevation:
registered project configs and tenant state live inside tenant homes. That is a
separate access problem from where the shared CLI is installed.

## Ownership And Permissions

`/usr/local/bin/switchyard`, `/opt/switchyard/releases/<sha>`, and the
`/opt/switchyard/current` symlink should be owned by `root:root`.

Release directories should be `0755`; files should be `0644`; executables
should be `0755`. Tenant project directories, control repositories, pane session
state, pane hook state, board databases, sockets, and board service units stay
owned by their existing project users and service accounts.

No tenant or pane role should have write permission to `/opt/switchyard`. Updates
are a privileged operator action so a pane cannot change the code that every
other project will run.

## Wrapper Discovery

`/usr/local/bin/switchyard` should not point at a user checkout. Its default
target should be `/opt/switchyard/current/switchyard`.

The wrapper may keep an operator-only override such as `SWITCHYARD_TARGET` for
tests or emergency rollback, but normal users should not need environment
variables. If `/opt/switchyard/current/switchyard` is missing or not executable:

- `switchyard --help` and `switchyard --version` should use install-time embedded
  fallback text, so agents still get a useful answer without sudo.
- Mutating commands should fail with a clear operator message naming the missing
  target and the install/rollback command to run.

The wrapper should acquire privilege only for commands that actually require
root. It should never require a non-root caller to read a target path under
another user's home before deciding whether help or sudo is needed.

## Upgrade Path

Add a privileged install/upgrade command that exports a tested source commit into
`/opt/switchyard/releases/<sha>` and atomically repoints
`/opt/switchyard/current`.

Recommended flow:

1. Build and test the Switchyard source checkout at an explicit commit.
2. Export that exact commit into `/opt/switchyard/releases/<sha>`.
3. Set root-owned permissions on the release tree.
4. Run a canary command directly from the release:
   `/opt/switchyard/releases/<sha>/switchyard --version`.
5. Atomically update `/opt/switchyard/current` to the new release.
6. Install or refresh `/usr/local/bin/switchyard` so it targets
   `/opt/switchyard/current/switchyard`.
7. Run post-upgrade checks through the wrapper:
   `switchyard --version`, `switchyard --help`, `switchyard status`.

The update command should leave the previous release in place for rollback:

```text
ln -sfn /opt/switchyard/releases/<previous-sha> /opt/switchyard/current
```

Generated project configs should be migrated so `pane_launcher` points at the
shared release path:

```text
/opt/switchyard/current/scripts/team-launcher
```

Existing tenant board deployments can keep serving the board app from
`<tenant>-ticketboard-live/current`; the launcher code path should no longer be
coupled to each tenant board release after the migration.

Shared releases should be immutable and should not self-deploy. The current
launcher freshness probe assumes `scripts/team-launcher` lives inside a git
checkout and runs `git` against that checkout on every real launch. A
root-owned exported release under `/opt/switchyard/current` is deliberately not
a per-tenant git checkout, so the implementation should teach the probe to
recognize the shared install path and skip git freshness/self-deploy checks
there. Freshness for shared installs is enforced by the operator upgrade flow
above: export an explicit tested commit, canary the release directly, then flip
`current`. The release should include a machine-readable commit marker so
`switchyard --version` and diagnostics can report which source commit is active.

## Cutover Breakage And Survival Plan

The live hardcoded wrapper must not be replaced until `/opt/switchyard/current`
exists and passes the direct canary. Otherwise the public command moves from a
bad target to no target.

Existing pane sessions survive the wrapper replacement because already-running
CLI processes do not re-read `/usr/local/bin/switchyard`. The risky moment is
the next `switchyard <project>` or `switchyard <project> pane ...` invocation.
Keep old per-tenant board release directories intact until every tenant has been
verified through the new wrapper.

For `pgu`, keep the hand-maintained config behavior stable during the first
cutover. Verify `switchyard status` and a dry-run or no-op project command before
any restart. Do not rewrite pgu pane workdirs or running sessions as part of the
wrapper target change.

For `otto` and `mefp`, perform a two-step migration:

1. Install the shared wrapper and verify `switchyard --help` and
   `switchyard --version` work without traversing another user's home. Verify
   global status through the existing elevated path because cross-tenant config
   reads remain privileged.
2. Run a project upgrade that rewrites generated config `pane_launcher` values to
   `/opt/switchyard/current/scripts/team-launcher`, then verify each project with
   an elevated or owner-owned `switchyard <project> status` before stopping or
   restarting panes.

If a tenant fails after step 1 but before step 2, it can still survive because
its existing config and board release are untouched. If a tenant fails after
step 2, roll back the config change or repoint `/opt/switchyard/current` to the
previous release; do not delete the tenant's old `<project>-ticketboard-live`
tree during the same maintenance window.

## Follow-Up Implementation Ticket

The implementation ticket should update the installer, wrapper tests, generated
config migration, and docs together. It should explicitly test:

- a user with no traverse access to `/home/eric` or `/home/agent` can run
  `switchyard --help` and `switchyard --version`;
- a user can run commands for a project it owns without reading another user's
  home, while global status and cross-tenant project commands still elevate;
- the wrapper target is `/opt/switchyard/current/switchyard`;
- missing target help/version fallback does not require sudo;
- mutating commands fail clearly when the shared target is absent;
- generated tenants use `/opt/switchyard/current/scripts/team-launcher` as their
  pane launcher after upgrade;
- launcher freshness/self-deploy probes are skipped for the shared immutable
  install and diagnostics report the release commit marker instead of requiring
  `.git`;
- rollback can repoint `current` without editing tenant project state.

This PGU-818 document intentionally does not change the live wrapper or launcher
behavior.
