"""Which verified release an upgrade installs, and whether a remembered pin is behind the host.

- `resolve_trusted_upgrade_release` names the immutable, root-controlled tree
  every privileged artifact is installed from: an installed release consumed
  as it is, when its marker is the commit asked for, or the exact commit
  (`_selected_release_commit`) materialized into a release root owns.
- `_recovered_pin_behind_host` stops an upgrade given no release whose
  remembered pin is behind the release the host now runs, and, as root, first
  installs the host's privileged boundary from the running release -- only
  after root has been shown it is running root-owned code.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-397), in their original
order. The launcher imports this module and re-exports every name, so the
upgrade's phases that read them through it, and every suite that calls or
rebinds them there, reach the same objects. The launcher facilities -- the
running release, the checkout, the shared install root and its default, and
the host-boundary installer -- the name each definition here reads of another,
and the launcher's own file are read from `team_launcher` when they run, as
they were: the file root is asked to trust is the launcher's, resolved then,
never this module's. The provisioning and publication helpers are still
imported inside each function, where they were. The `runner` defaults are
bound when each function is defined, as they were. The standard-library names
are this module's own imports, the same objects. `ProjectConfig` is imported
for annotations only. This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig


def _recovered_pin_behind_host(
    config: "ProjectConfig",
    *,
    source_repo: Path | None,
    deploy_ref: str | None,
    dry_run: bool,
    tooling_root: Path | None,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    print_func: Callable[[str], None],
) -> int | None:
    """Stop an unpinned upgrade whose remembered release is behind the host's.

    An upgrade given no release keeps the one the tenant was last pinned to
    (SYRD-61), which is right for resuming one upgrade and wrong once an
    operator has installed a newer shared release: the launcher running this
    is that newer release, the old pin cannot be staged by it, and the old
    advice -- install the pinned release -- meant reinstalling the obsolete one
    (SYRD-284). Checked before anything of the tenant's is written.

    As root it does the one thing that is the host's rather than the tenant's:
    installs the privileged boundary from the release it is running, so the
    pinned preview and upgrade it then names exist. Returns None when the pin
    is not behind, so the upgrade carries on unchanged.
    """
    from scripts import team_launcher as launcher

    running = launcher.running_launcher_release()
    if running is None or not running.marker_commit:
        return None
    selected, _problems = launcher.resolve_trusted_upgrade_release(
        (source_repo or launcher._repo_root()).expanduser().resolve(strict=False),
        deploy_ref or "", dry_run=True, ref_is_pinned=True, runner=runner,
    )
    pinned = selected.commit if selected is not None else str(deploy_ref or "")
    current = running.marker_commit
    if not pinned or pinned == current:
        return None
    print_func(
        f"switchyard: {config.project} is pinned to {pinned} from its last upgrade, and this host "
        f"now runs {current}. An upgrade given no release keeps the tenant's pin, and the launcher "
        f"running it is not that release, so nothing of {config.project}'s was changed."
    )
    if os.geteuid() == 0 or dry_run:
        untrusted: list[str] = []
        if not dry_run and launcher.switchyard_shared_install_root() == launcher.DEFAULT_SWITCHYARD_SHARED_INSTALL_ROOT:
            from scripts.ticket_board.project_provision import untrusted_root_executable_reasons

            untrusted = untrusted_root_executable_reasons(Path(os.path.realpath(launcher.__file__)), owner_uid=0)
        if untrusted:
            print_func(
                "switchyard: this host's privileged boundary was not installed: this is not "
                f"running from root-owned code ({untrusted[0]})"
            )
            return 1
        problems = launcher.install_host_privileged_boundary(
            running.root, dry_run=dry_run, staging_root=tooling_root, runner=runner, print_func=print_func
        )
        if problems:
            for problem in problems:
                print_func(f"switchyard: {problem}")
            return 1
    else:
        print_func(
            f"switchyard: this host's privileged boundary comes from {current}; as root it is "
            f"installed by `switchyard privileged-action {config.project} upgrade-tenant`, which "
            "stops here in the same way"
        )
    print_func(
        f"switchyard: to move {config.project} to {current}, preview it and then apply it, pinned: "
        f"`switchyard privileged-action {config.project} preview-upgrade commit={current}`, then "
        f"`switchyard privileged-action {config.project} upgrade-tenant-release commit={current}`"
    )
    return 1


def resolve_trusted_upgrade_release(
    source_repo: Path,
    commit: str,
    *,
    ref_is_pinned: bool = False,
    install_root: Path | None = None,
    dry_run: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
):
    """The immutable root-owned tree every privileged artifact is installed from.

    This has to happen before staging, not after it. `refresh_staged_role_tooling`
    copies `switchyard-publish-ref` -- the one program the NOPASSWD rule grants
    root on -- into a root-owned path, and it used to copy it out of the source
    checkout. Under one shared account (SYRD-69) a role can write that checkout,
    so root was copying role-writable bytes into the program root would later run
    for them: planting the file was a root shell. The commit selects what is
    staged now, and `git archive` reads the object store rather than the working
    tree (SYRD-97 review).
    """
    from scripts import team_launcher as launcher

    from scripts.ticket_board.publication_boundary import (
        TrustedRelease,
        materialize_trusted_release,
        read_release_marker,
        root_controlled_problems,
    )

    # The pinned source may already BE an immutable release -- an operator who
    # pinned /opt/switchyard/releases/<sha> has handed us the exact thing this
    # would otherwise go and build. It is a tree, not a checkout, so asking git
    # which commit it is fails; its marker says. Consumed rather than rebuilt,
    # after the same whole-path check (SYRD-97 review).
    marker = read_release_marker(source_repo)
    marked_commit = str(marker.get("commit") or "").strip()
    if marked_commit:
        overridden_root = os.environ.get("SWITCHYARD_SHARED_INSTALL_ROOT", "").strip()
        problems = root_controlled_problems(
            str(source_repo), base=overridden_root or "/"
        )
        if problems:
            return None, [
                f"{source_repo} is an installed release but is not root-controlled, so it "
                "will not be used"
            ] + problems
        # Being a release is not the same as being the release that was asked
        # for. An operator pointing at the installed one while pinning a newer
        # ref would otherwise stage the OLD tools and be told it worked, which
        # is the stale-global-release case this ticket is about (SYRD-97 review).
        wanted = (commit or "").strip()
        # A ref only competes with the marker when somebody actually chose it.
        # The resolver fills one in when only a source was pinned, and treating
        # that default as a pin refuses the ordinary "install exactly this
        # release" case.
        if wanted and ref_is_pinned and wanted != marked_commit:
            if re.fullmatch(r"[0-9a-f]{40}", wanted):
                # An exact commit needs no resolution to be compared, so there is
                # no state in which this can fail open (SYRD-97 review).
                return None, [
                    f"{source_repo} is the installed release for {marked_commit}, but this "
                    f"upgrade is pinned at {wanted}. Nothing was staged: pointing at one "
                    "release while pinning another installs the older tools and reports success."
                ]
            # Symbolic, and there is nowhere trustworthy to resolve it. The
            # release tree is not a repository, and the checkout and cache it
            # records are writable by the account every role runs as -- a role
            # could move that ref back onto the old release and be believed.
            return None, [
                f"{source_repo} is the installed release for {marked_commit}, and this upgrade "
                f"is pinned at {wanted!r}, which is a name rather than a commit. It is not "
                "resolved here: the repositories that could resolve it are writable by the "
                "account every role runs as. Pin the exact commit instead."
            ]
        return TrustedRelease(root=source_repo, commit=marked_commit), []

    selected = launcher._selected_release_commit(source_repo, commit, runner=runner)
    if not selected:
        return None, [
            f"could not resolve which commit {source_repo} is being installed from, so no "
            "release can be verified and nothing privileged can be staged from it"
        ]
    root = install_root or launcher.switchyard_shared_install_root()
    # "/" on a host. It moves only when the shared install root has been
    # overridden, which is the documented seam for exercising the real
    # privileged branch without writing to the host's /opt; a fixture cannot own
    # "/", and refusing its temp directory would be the correct answer to the
    # wrong question.
    overridden = os.environ.get("SWITCHYARD_SHARED_INSTALL_ROOT", "").strip()
    return materialize_trusted_release(
        selected,
        source_repo=source_repo,
        install_root=root,
        trust_base=str(root) if overridden else "/",
        runner=runner,
        dry_run=dry_run,
    )


def _selected_release_commit(
    source_repo: Path,
    ref: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> str:
    """The exact commit the upgrade is installing, named rather than implied."""
    candidate = (ref or "").strip() or "HEAD"
    proc = runner(
        ["git", "-C", str(source_repo), "rev-parse", "--verify", f"{candidate}^{{commit}}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if getattr(proc, "returncode", 1) != 0:
        return ""
    return str(getattr(proc, "stdout", "") or "").strip()
