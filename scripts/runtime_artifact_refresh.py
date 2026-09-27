"""Refreshing a tenant's generated runtime artifacts: its own copies, and what root installs.

`refresh_generated_project_runtime_artifacts` reads the tenant's generated plan
without following a link, applies the operator's command-line values
(`_plan_replacements`), republishes the tenant's own copies where they differ
(`_tenant_copy_is_current`), and -- as root -- renders what root installs from
root's own baseline plus the one thing the tenant's projection may add, with the
controller taken from the installed grant (`installed_controller`). A tampered
plan's containment refusal is reported by field (`_path_containment_error`), a
legacy root-owned provision directory is returned to its owner on root-controlled
evidence, and root's provisioning directory is kept private (SYRD-39, SYRD-52,
SYRD-176, SYRD-177, SYRD-227, SYRD-228).

Moved out of `scripts/team_launcher.py` unchanged (SYRD-354). The launcher
imports this module at its top and re-exports every name, so the upgrade's
artifacts phase, the role-identity cutover and the suites call the same
objects. Every launcher facility these use -- the tenant document reader, the
plan parser and renderer, the publisher and installer, root's provision roots,
the baseline, projection, ownership and privacy helpers, the result type -- and
every name defined here that another definition here calls is read from
`team_launcher` when it runs, as it was. `os`, `subprocess` and the other
standard-library names are this module's own imports, the same objects. This
module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import LauncherUpgradeResult, ProjectBoardProvision, ProjectConfig


def _plan_replacements(source_repo: Path | None, commit_git_dir: str | None) -> dict[str, str]:
    """Values the operator supplied on the command line, which the plan does not choose."""
    replacements: dict[str, str] = {}
    if source_repo is not None:
        replacements["source_repo"] = str(source_repo.expanduser().resolve(strict=False))
    if commit_git_dir is not None:
        selected = commit_git_dir.strip()
        if not selected:
            raise SystemExit("switchyard: --commit-git-dir must not be empty")
        replacements["commit_git_dir"] = selected
    return replacements


def _tenant_copy_is_current(path: Path, body: bytes) -> bool:
    """A symlink is never current: it is an entry to replace, not a file to read."""
    if os.path.islink(path) or not path.is_file():
        return False
    try:
        return path.read_bytes() == body
    except OSError:
        return False


def installed_controller(project: str, owner_user: str) -> str:
    """The controller root has actually granted, and nobody else.

    `invoking_user` is deliberately not passed. An upgrade must reinstall the
    human root already recorded in the root-owned grant, never record whoever
    happened to run the upgrade -- that is the difference between repairing a
    bridge and handing one out. With no installed grant this is empty, which is
    the state a first pass leaves behind and the state that renders no
    tenant-control artifacts at all (SYRD-52).
    """
    from scripts import team_launcher as launcher

    return launcher.resolve_control_user(project, owner_user=owner_user)


def _path_containment_error() -> type[Exception]:
    """The renderer's containment refusal, imported where it is caught.

    Locally, like every other use of this module here: the import at the top of
    the file is a curated list and this is an exception type used in one place
    (SYRD-177).
    """
    from scripts.ticket_board.project_provision import PathContainmentError

    return PathContainmentError


def refresh_generated_project_runtime_artifacts(
    config: ProjectConfig,
    *,
    config_path: Path,
    dry_run: bool = False,
    source_repo: Path | None = None,
    commit_git_dir: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> LauncherUpgradeResult:
    """Refresh the tenant's plan, and the artifacts root installs, separately.

    The two have different provenance and must not be confused. The tenant's
    plan.json is the tenant's: root rewrites it for the tenant's tooling and
    never reads it back to decide what to install. Everything root installs is
    rendered from a root-owned baseline into a root-owned directory, and the
    only thing the tenant's plan is allowed to contribute is a role the workflow
    projection added, under that role's own canonical account (SYRD-39).
    """
    from scripts import team_launcher as launcher

    # Absolute, never resolved: `Path.resolve()` follows a symlink the tenant
    # planted at any component, and the whole of this function -- the documents
    # it reads and the ownership repair it runs -- would then be pointed at
    # wherever that link leads. Every component is judged instead, unfollowed,
    # by the reads and by the repair below (SYRD-228).
    provision_dir = Path(os.path.abspath(str(config_path.expanduser()))).parent
    tenant_plan_path = provision_dir / "plan.json"
    # Read before anything decides anything, and without following a link at
    # any component: this runs as root against a directory the tenant owns, and
    # `is_file()` on a planted symlink answers for whatever it points at
    # (SYRD-228). The bounded ownership repair below still runs afterwards --
    # this refuses only the document, never the repair's own no-follow work.
    plan_document, plan_problem = launcher.read_tenant_document_no_follow(
        tenant_plan_path, what=f"{config.project}'s generated runtime plan"
    )
    if plan_problem:
        if f"{tenant_plan_path} does not exist" in plan_problem:
            return launcher.LauncherUpgradeResult(False, f"switchyard: {config.project} has no generated runtime plan; leaving artifacts unchanged")
        return launcher.LauncherUpgradeResult(False, f"switchyard: {plan_problem}. Nothing was changed.")
    replacements = launcher._plan_replacements(source_repo, commit_git_dir)
    changed: list[str] = []

    migrated_fields: list[str] = []
    try:
        tenant_plan = launcher._project_board_provision_from_json(
            tenant_plan_path, migrated=migrated_fields, supplied=replacements,
            document=plan_document,
        )
    except SystemExit as exc:
        return launcher.LauncherUpgradeResult(
            False,
            f"switchyard: {config.project} runtime plan is incomplete and cannot be refreshed automatically: {exc}",
        )
    # A plan written before these fields existed is migrated, not rejected. Say
    # so in every outcome below, including the dry run, which reports it without
    # writing anything (SYRD-52).
    migration_note = (
        f"; migrated {config.project} plan fields added since it was provisioned: "
        + ", ".join(sorted(migrated_fields))
        if migrated_fields
        else ""
    )
    # The staged unit carries the table that matches the identities the tenant
    # is actually running, which is not always the one the root baseline was
    # written with.
    #
    # A tenant that has crossed to the one-project-account runtime has no
    # per-role accounts at all: every role runs as the project account and the
    # board authorises a registered process instead of a uid. Rendering the old
    # table for it produces a unit that both names retired accounts and omits
    # TICKET_BOARD_PROCESS_AUTHORITY=1, so the board comes up in legacy_uid mode
    # resolving peers through accounts that no longer exist -- which is a board
    # no role can write to, the director included (SYRD-87 R6).
    #
    # The migration state comes from the tenant's configuration, which is the
    # document the identities transaction writes and then verifies against the
    # kernel-read uid of every role process. The tenant's plan.json is still not
    # consulted for it. The trust boundary is intact in the direction that
    # matters: the only thing this can do is EMPTY the table. It can never name
    # an account, so a tenant that lied about its migration state would remove
    # its own roles' authority rather than acquire anybody else's -- and an
    # empty table grants nothing to nobody, which is the property the previous
    # comment relied on (SYRD-39).
    def _for_current_identities(plan: ProjectBoardProvision) -> ProjectBoardProvision:
        return launcher.plan_for_current_identities(plan, config)

    # What the tenant's document said when this upgrade started, captured before
    # anything below republishes it. Root judges the document it found: a value
    # rewritten by the tenant-copy refresh below would otherwise be laundered
    # past the divergence refusal (SYRD-52).
    tenant_data = dict(plan_document or {})
    # The controller is regenerated from the installed root-owned grant rather
    # than read back from either document, so a bridge installed by the
    # operator's first pass is recorded on the second and its files are
    # reinstalled by an ordinary upgrade (SYRD-52).
    tenant_plan = replace(
        tenant_plan,
        control_user=launcher.installed_controller(config.project, tenant_plan.owner_user),
    )
    # The tenant's own view of its generated files, rendered from the tenant's
    # own plan. Nothing root installs comes from here.
    #
    # And the plan is the tenant's, so it can say anything. The path containment
    # checks in the renderer are exactly right to refuse `owner_home: "/"` --
    # every path under it "only shares its prefix" -- but they refuse by raising,
    # and this is a supported command. Raising here turned
    # `switchyard upgrade <project>` into a traceback for a tampered document
    # instead of the careful refusal every other tampered field gets, and it
    # killed a suite whose remaining twenty cases had not run since (SYRD-177).
    try:
        tenant_rendered = launcher.render_privileged_artifacts(
            _for_current_identities(
                launcher.plan_with_tenant_checkout(
                    replace(tenant_plan, **replacements), config_path=config_path
                )
            ),
            enable_owner_linger=False,
        )
    except launcher._path_containment_error() as exc:
        # Named by FIELD, like every other refusal here. The containment checks
        # answer in paths -- "//porter-worktrees is not inside /" -- and an
        # operator reading that has to work backwards to the one value in the
        # document that produced it. Every path in a rendered plan is derived
        # from owner_home, so that is the field this class of refusal is about.
        return launcher.LauncherUpgradeResult(
            False,
            f"switchyard: cannot establish a root-owned baseline for {config.project} from "
            f"{tenant_plan_path}: owner_home {tenant_plan.owner_home!r} cannot contain the paths "
            f"this plan derives from it: {exc}. Re-provision the project so root generates its own.",
        )
    # A tenant provisioned by an older release as root: its provision directory
    # is root-owned, which is the one owner fact reconstruction trusts, and root
    # has no baseline of its own yet. Establish the owner from root-controlled
    # host records and hand back only the generated files -- described in a dry
    # run, done in an apply -- before anything below writes there (SYRD-227).
    established_owner = ""
    ownership_note = ""
    if (
        os.geteuid() == 0
        and not launcher.privileged_baseline_plan_path(config.project).is_file()
        and launcher._provision_owner(provision_dir)[1].endswith("is owned by root and so does not identify a tenant")
    ):
        owner, evidence, owner_problem = launcher.legacy_owner_from_host_records(config.project, provision_dir)
        if not owner:
            return launcher.LauncherUpgradeResult(
                False,
                f"switchyard: {config.project}'s provision directory {provision_dir} is owned by root, "
                f"and its owner cannot be established from root-controlled host records: "
                f"{owner_problem}. Nothing was changed; the tenant is left as it is.",
            )
        repair, repair_problem = launcher.repair_legacy_provision_ownership(
            provision_dir,
            owner,
            [*tenant_rendered, config_path.name],
            dry_run=dry_run,
        )
        if repair_problem:
            return launcher.LauncherUpgradeResult(False, f"switchyard: {repair_problem}. Nothing was changed.")
        established_owner = owner
        ownership_note = (
            f"; {'would return' if dry_run else 'returned'} {config.project}'s legacy root-owned "
            f"provision directory to {owner}, established by: " + "; ".join(evidence)
            + ". Entries: " + ", ".join(repair)
        )
    # Reported in every outcome below, dry run included.
    migration_note += ownership_note
    for name in sorted(tenant_rendered):
        if launcher._tenant_copy_is_current(provision_dir / name, tenant_rendered[name]):
            continue
        if not dry_run:
            published, problem = launcher.publish_tenant_artifact(config, provision_dir, name, tenant_rendered[name])
            if not published:
                return launcher.LauncherUpgradeResult(False, problem)
        changed.append(name)

    target = launcher.privileged_provision_dir(config.project, root=launcher.switchyard_privileged_provision_root())
    if os.geteuid() != 0:
        if not changed:
            message = f"switchyard: {config.project} generated runtime artifacts are already current"
        elif dry_run:
            # Nothing above wrote anything in a dry run, so this must not say it
            # did: an operator reading "refreshed" would take the migration as
            # already applied (SYRD-52).
            message = (
                f"switchyard: {config.project} generated runtime artifacts can be refreshed: "
                f"{', '.join(changed)}"
            )
        else:
            message = f"switchyard: refreshed {config.project} generated runtime artifacts: {', '.join(changed)}"
        message += migration_note
        if not target.is_dir():
            message += (
                f"; run `switchyard upgrade {config.project}` as root to stage the units, grants "
                "and SQL root installs"
            )
        return launcher.LauncherUpgradeResult(bool(changed) and not dry_run, message)

    baseline, reason = launcher._privileged_baseline_plan(
        config,
        provision_dir,
        tenant_data,
        source_repo=source_repo,
        operator_commit_git_dir=commit_git_dir is not None,
        supplied=replacements,
        established_owner=established_owner,
    )
    if baseline is None:
        return launcher.LauncherUpgradeResult(bool(changed), reason + ownership_note)
    plan, added_roles, refused = launcher.authoritative_refresh_plan(baseline, tenant_data)
    # The stored baseline was written before the operator installed the bridge,
    # so the controller is re-derived here too. From the grant only: the tenant
    # document has no say, and neither does whoever is running the upgrade.
    plan = replace(plan, control_user=launcher.installed_controller(plan.project, plan.owner_user))
    for entry in refused:
        print_func(
            f"switchyard: ignoring {config.project} plan entry {entry}: an unprivileged workflow "
            "projection may only add a role under that role's own canonical account"
        )
    if replacements:
        plan = replace(plan, **replacements)
    # Where this tenant's checkout is, taken from where its configuration is.
    # An upgrade is one of the two supported repairs for a tenant provisioned
    # before the plan recorded it (SYRD-156).
    plan = launcher.plan_with_tenant_checkout(plan, config_path=config_path)
    rendered = launcher.render_privileged_artifacts(_for_current_identities(plan))
    privileged_changed = sorted(
        name
        for name, body in rendered.items()
        if not (target / name).is_file() or (target / name).read_bytes() != body
    )
    # The directory's mode is not one of the rendered bytes, so a tenant whose
    # artifacts are already current would never have been repaired by the branch
    # below -- which is exactly the state syrd was found in. It is asked about
    # and repaired on its own, before anything is compared (SYRD-176).
    privacy = launcher.privileged_provision_privacy_problems(config.project)
    repaired: list[str] = []
    if not dry_run and target.is_dir():
        repaired = launcher.ensure_privileged_provision_dir(target)
        repaired += launcher.close_privileged_artifacts(target, rendered)
    privacy_note = ("; " + "; ".join(repaired)) if repaired else ""
    if privacy and dry_run:
        privacy_note = "; " + "; ".join(f"would repair: {objection}" for objection in privacy)
    if not changed and not privileged_changed:
        return launcher.LauncherUpgradeResult(
            bool(repaired),
            f"switchyard: {config.project} generated runtime artifacts are already current"
            + privacy_note
            + migration_note,
        )
    if dry_run:
        return launcher.LauncherUpgradeResult(
            False,
            f"switchyard: {config.project} generated runtime artifacts can be refreshed: "
            + ", ".join(sorted({*changed, *privileged_changed}))
            + privacy_note
            + migration_note,
        )
    if privileged_changed:
        try:
            launcher.install_privileged_artifacts(plan, rendered)
        except OSError as exc:
            return launcher.LauncherUpgradeResult(
                bool(changed),
                f"switchyard: could not stage {config.project} privileged artifacts under {target}: {exc}. "
                "Nothing privileged was installed; the previous root-owned copy is unchanged.",
            )
    parts: list[str] = []
    if changed:
        parts.append(f"tenant copies: {', '.join(changed)}")
    if privileged_changed:
        parts.append(f"root installs {', '.join(privileged_changed)} from {target}")
    parts.extend(repaired)
    message = f"switchyard: refreshed {config.project} generated runtime artifacts; " + "; ".join(parts)
    message += migration_note
    if added_roles:
        message += f"; role accounts added from the workflow projection: {', '.join(added_roles)}"
    return launcher.LauncherUpgradeResult(True, message)
