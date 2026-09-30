# SYRD-529: the catalogued pinned preview and upgrade resolve the release they name

## What happened

MEFP followed SYRD-284's chain:
1. The operator installed shared 7fe0aee.
2. The Director ran `/opt/switchyard/current/switchyard privileged-action mefp upgrade-tenant`
   (attempt 0066). It installed the new boundary and, by design, named the
   pinned preview and upgrade.
3. The root preview `preview-upgrade commit=7fe0aee…` (attempt 0067) refused:
   > /opt/switchyard/releases/49abeb4… is the installed release for 49abeb4…, but
   > this upgrade is pinned at 7fe0aee…

## Cause

- **The actions send only a commit.** `preview-upgrade` and
  `upgrade-tenant-release` send root `upgrade <slug> --deploy-ref <commit>
  [--dry-run]`. That is deliberate: a commit and nothing else, no caller path.
- **The upgrade fills the rest from the record.** It completes an invocation
  from root's record of the last pin (SYRD-61), field by field. With only the
  ref given, MEFP kept the recorded source (the installed release for 49abeb4)
  and the recorded commit cache (`/data/git/fixpatch`).
- **So it refused.** An installed release is a tree for one commit, so the
  marker check refused, correctly.
- **SYRD-284's suite never ran the named commands as the catalogue builds
  them.** Its last step passed the new source explicitly.

## Also found: the apply recorded the mismatch first

On apply, the pinned-source step recorded root's pin (old source, new ref)
before the artifacts phase refused. Every later unpinned upgrade would then
recover that mismatched tuple. The reproduction shows it on main.

## The change

- **`upgrade_records.resolve_pinned_upgrade_source`.** When all of these
  hold, the source becomes that running release, and the preview says so:
  - no source was given;
  - the recorded source is an installed release for a different commit;
  - the chosen ref is an exact commit;
  - that commit is exactly the release this launcher runs (the host's
    current release, which the operator activated).
- **What else holds:**
  - The recorded commit cache is kept; it is the project's history, not
    Switchyard's.
  - Any other commit keeps the recorded source and its refusal.
  - An explicit source still wins.
  - Nothing is taken from the caller, and no release the operator did not
    activate is reachable.
- **`upgrade_phases`, the pinned-source step (apply only).** An installed
  release as the source, and an exact chosen commit that disagrees with its
  marker, are refused before the publication remote or root's pin is
  recorded. The refusal says which commit this host can take. Only that
  comparison moved earlier; root-control and the other release checks stay
  before staging.
- **Unchanged:**
  - the catalogue and its argv;
  - the marker and root-control checks;
  - `upgrade-tenant` with a recovered pin (SYRD-284's refusal naming the
    pinned commands);
  - `select-shared-release`, the admin-authenticated pinned rollback.

## Boundary guards

`upgrade_records_boundary_test` pins each moved function's launcher reads.
Its counts for `resolve_pinned_upgrade_source` gain the two reads the
substitution adds (`_read_switchyard_release_marker` and
`running_launcher_release`, both through the launcher), plus cases that reach
them. My first early check reused `resolve_trusted_upgrade_release`. That
added a third read to `trusted_upgrade_release_boundary_test`, and its
root-control walk refused six unprivileged cases in
`team_launcher_pinned_release_resume_test` for the wrong reason. The check
now compares the marker and the commit only. Both suites match main: the
first case for case, and the second is unchanged.

## Procedure for MEFP (for SYRD-528)

Nothing here was run against MEFP. Commands are for the MEFP Director pane.
- **Which client.** In that pane, plain `switchyard` is the tenant's 49abeb4
  client: the owner's `~/bin` and `~/.local/bin` come before `/usr/local/bin`.
  That client does not know the pinned actions. Use the shared client by path:
  `/opt/switchyard/current/switchyard`, or the wrapper
  `/usr/local/bin/switchyard`.
- **Host prerequisite, done.** Shared release 7fe0aee is installed, and the
  boundary was reinstalled from it (attempt 0066).
- **Steps,** after this fix is in the shared release the host runs:
  1. `/opt/switchyard/current/switchyard privileged-action mefp preview-upgrade commit=<C> --dry-run`.
     No privilege is requested. It reports the policy decision and root's
     argv.
  2. The same command without `--dry-run`: root's read-only preview. It
     should print `… in place of the recorded /opt/switchyard/releases/49abeb4…`
     and stage from `/opt/switchyard/releases/<C>`.
  3. `/opt/switchyard/current/switchyard privileged-action mefp upgrade-tenant-release commit=<C>`.
  4. `switchyard release-status mefp`. Then check `command -v switchyard`: if
     the pane still resolves 49abeb4, keep using the shared client by path.
- **What `<C>` must be.** It must be the release the host runs
  (`readlink -f /opt/switchyard/current`). This fix is not in 7fe0aee, so the
  operator first installs a shared release that contains it. The preview then
  runs from that release.
- **Rollback.**
  - The pinned way back is
    `/opt/switchyard/current/switchyard privileged-action mefp select-shared-release commit=49abeb4b00d47805e515af455ce506cb4ae41888`.
    It is admin-authenticated and runs `upgrade mefp --source-repo
    <root's release for 49abeb4> --deploy-ref 49abeb4`. The suite shows
    it neither substituted nor refused, recording the old tuple with the
    commit cache kept.
  - Do not use the rollback that `switchyard upgrade` prints. When the shared
    release was installed before the tenant upgrade, its note names the
    shared release (7fe0aee) as "previous", not MEFP's 49abeb4 (SYRD-528
    finding, left for its own decision).
  - Not verified here: whether the old board code runs against a database
    the new release has migrated.

## Evidence

- **`tests/catalogued_upgrade_source_test.py`.** It runs as uid 0 in a user
  namespace, like SYRD-284's suite, against real staged releases: 49abeb4 as
  the tenant's pin, the tree under test as the host's release, and 7fe0aee
  as installed but not current. It drives the argv the catalogue builds.
  - **Reproduction.** Main's code, run from a git worktree in a child,
    refuses the preview and apply, and the apply records the mismatched pin.
  - **After.** The preview resolves the host's release and writes nothing.
    The apply records (host release, commit, recorded cache).
  - **Negative cases.**
    - An installed but not current commit, and a missing one, are refused;
      the apply refuses before recording.
    - `upgrade-tenant` keeps SYRD-284's refusal.
    - An operator naming the old source with the new commit is refused.
    - `select-shared-release` back to the old release is neither substituted
      nor refused.
  - It passes both under `env -i` and in a role pane, and writes nothing to
    the real home.
- **Limit.** The fixture's runner records staging instead of running it, so
  the apply's later phases stop for fixture reasons, as they do in SYRD-284's
  suite. The test proves source resolution and the recorded tuple, not a
  completed end-to-end apply.
- **Correction after the integration smoke.** The Director's integration
  smoke failed the first audited candidate, b3a6461, at "the apply is not
  refused for its release".
  - That check excluded the phrase "before recording anything" anywhere in
    the output.
  - In a checkout without `origin/main`, the fixture stages the tenant's
    tooling from HEAD, so the apply runs to its end. It then prints an
    unrelated release-status advisory ("… re-verifies the live build before
    recording anything").
  - Reproduced in a clone without `origin/main`, with PATH, USER and LOGNAME
    as the Director used them. The two refusals are now matched as the lines
    they are: the early refusal's own line, and the full "is the installed
    release for <commit>, but this upgrade is pinned at …" sentence.
  - The production code is unchanged. The suite passes in both
    configurations, and all 8 mutants are killed in both.
- **Mutation, with `tests/bounded_run.py mutate`: 8 of 8 killed.** Each kill
  was confirmed from its assertion line. The mutants cover:
  - no substitution;
  - substitution not bounded to the host's release;
  - no early refusal, or one in the wrong mode;
  - the early check ignoring the marker, or refusing matches;
  - no hint;
  - substitution for an equal recorded release.
- **Sweep against 6726370.** 55 suites naming upgrade source pinning, the
  trusted release, privileged operations or `--deploy-ref` were run under
  `env -i`, with refusing stubs for claude, codex, agy and konsole; no stub
  was reached. 43 pass on both trees and 11 fail on both.
  `tenant_deploy_identity_test` failed once on main and passed on rerun on
  both trees (flaky).
