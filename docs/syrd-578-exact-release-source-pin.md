# SYRD-578: an exact-release upgrade records a source deploy-release accepts, or refuses

## What happened

Otto had no recorded pin. The operator ran the active root-owned launcher as
root:

```
sudo /usr/local/bin/switchyard upgrade otto --deploy-ref a025c35befb42bbff0b2b17e3f67888f46996daa
```

It exited 0 and advertised `deploy-release`, which then refused:

```
switchyard: root holds no installed release of a025c35b... as otto's pinned source (None)
```

The catalogued `upgrade-tenant-release` runs exactly that argv
(`upgrade <p> --deploy-ref <sha>`), and so does the catalogued
`preview-upgrade` with `--dry-run`.

The cause is in U1, `_pin_upgrade_source`:
- `--deploy-ref` alone makes the selection explicit.
- `resolve_pinned_upgrade_source` has nothing recorded to fill in the source
  from.
- `record_upgrade_source` then writes the exact commit beside an empty
  `source_repo`.
- `deploy-release` proves its pin by reading the marker of the recorded source,
  so it refuses an empty one.
- The upgrade had already exited 0 and named `deploy-release` as the
  continuation.

## The change

In U1, before anything is recorded or stopped:
- **When it applies.** As root, when the selection has no source (none given,
  none recovered) and the deploy ref is an exact 40-hex commit somebody chose,
  given or recovered.
- **What the upgrade takes.** This host's active installed release of that
  commit, from `scripts/active_release_source.py`. It records it, so it counts
  as an explicit pin.
- **What it may take.** Only a release that passes every one of these checks:
  - the path is `<install root>/releases/<commit>`, built from the commit and
    never taken from a caller;
  - it is a real directory, not a symbolic link;
  - every component belongs to root (`root_controlled_problems`, the same proof
    an explicitly named installed release gets in
    `resolve_trusted_upgrade_release`);
  - its own marker records that commit;
  - `<install root>/current` resolves to it.
- **The message.** The upgrade prints which release it took:
  `otto names no source for <sha>; its source is /opt/switchyard/releases/<sha>,
  this host's active, root-controlled installed release of that commit.`
- **Otherwise it refuses with exit 1, before recording or stopping anything.**
  It names the failing check and the supported preparation: install and
  activate the release with
  `switchyard privileged-action <p> install-shared-release commit=<sha>`, or
  name an installed root-owned release with
  `--source-repo /opt/switchyard/releases/<sha>`, the recovery used on Otto.

**Exit 0 is now a continuation guarantee.** The recorded tuple names a release
whose marker is the commit, so `deploy-release` passes its pin proof.

**A tenant already left with an empty source**, as Otto is, needs no flags. An
argument-free root rerun recovers the commit, takes the active release, and
records it.

**The dry run decides exactly as the upgrade does.** That includes the
catalogued `preview-upgrade`. It takes the same release or refuses with the same
lines, and records nothing.

**Unchanged:**
- An upgrade that is not root records no pin and decides nothing new.
- A branch ref such as `origin/main` is not an exact release.
- An explicit `--source-repo` keeps every existing check, including the marker
  mismatch refusal.
- A recovered source wins as before.
- No pane, presentation or live role is needed. The scenarios run against a
  stopped tenant and start none.

## Measured (`tests/exact_release_source_pin_test.py`)

**Setup.**
- An unpinned legacy tenant: the pinned-release suite's declarative config,
  deployed board and staged tooling.
- An installed shared release under an overridden install root.
- The tenant is stopped, with no live role and no presentation.
- The upgrade is the real `upgrade_project_command` as root. The continuation is
  the real `switchyard_deploy_release_command`.
- Host commands go to the recording runner, and the deploy really moves the
  board's `current`.
- The scenarios run in a child, on main `0d9e16de` from a `git archive`, and on
  this tree.

**Main.**
- The upgrade exits 0, recording `{"source_repo": "", "deploy_ref": <sha>}`, and
  advertises `deploy-release commit=<sha>`.
- That `deploy-release` refuses with Otto's exact message and deploys nothing.

**This tree:**
- The catalogued `upgrade-tenant-release` and `preview-upgrade` argv, parsed by
  the real upgrade parser, are the selection the scenarios use: no source, the
  exact commit, and the preview as a dry run.
- **The active release.** The upgrade exits 0, records the active release as the
  source and says so, and starts no pane or presentation. The advertised
  `deploy-release` then deploys that release and closes the phase.
- **The dry run** takes the same release and records nothing.
- **Refusals.** Inactive, missing, mismatched and symlinked releases each
  refuse with exit 1 and name their own reason and the preparation. Each records
  no pin and no phase and launches nothing, and its dry run prints the same
  decision.
- **A release not owned by root** fails the whole-path proof once root's uid is
  the expected owner, as on a host.
- **Otto's state** (the commit recorded beside an empty source) is repaired by
  an argument-free rerun, which then deploys.
- **Not root:** nothing recorded, nothing new decided.
- **A branch ref:** nothing inferred, nothing new refused.

## Not changed

- No live tenant, service, pane or presentation. Otto and MEFP are untouched.
- `deploy-release` and its pin proof.
- `upgrade_phases.py` grows from 1,126 to 1,137 lines.
