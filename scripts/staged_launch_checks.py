"""What an ordinary launch may conclude about a tenant's staged role tooling, and which release it names.

- `tenant_pinned_release_root` is the release a tenant's staged bundle was
  made from, read from the bundle's own release marker, when that release is
  installed: an older tenant's complete bundle is judged against its own
  release, and `current` only stands in for a tenant with no staged release.
- `director_readable_pinned_release` is the same pin as the unprivileged
  director may believe it: only after every directory to the marker is proven
  root-controlled, and only a 40-hex commit whose release is installed.
- `staged_bundle_launch_problems` asks the staged-tooling inspection about
  the bundle against an explicit release, else the pinned one, else
  `current`, and sorts the answer: absence (`STAGED_TOOLING_ABSENT_MARKER`)
  an ordinary launch may restage, hostile ownership, mode or type
  (`STAGED_TOOLING_HOSTILE_MARKERS`) it refuses, and drift it leaves to
  `switchyard upgrade`.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-415), in their original
order. The launcher imports this module and re-exports all five names;
`scripts/command_crossing.py`, `scripts/director_upgrade.py` and
`scripts/staged_role_tooling.py` still reach them through the launcher.
Everything the three functions read -- each other and the two markers
included, and the staging directory, the shared install root, the release
marker name and reader, and the staged-tooling inspection -- is read through
the launcher at call time, so a suite that rebinds one there still intercepts
it. The staged tooling owner Python binds as a default at definition is bound
here from `scripts.staged_role_tooling`: the same object. This module imports
`team_launcher` only inside the functions, when they run.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from scripts.staged_role_tooling import STAGED_TOOLING_OWNER_UID


#: A staged file that is simply not there. Absence is the one shape an
#: ordinary launch may repair by itself: SYRD-211 deliberately refused to
#: restage on DRIFT, because "this release would install different bytes" is
#: true of every tenant the moment the shared release moves, and would make
#: every launch privileged. Nothing being there at all is not drift.
STAGED_TOOLING_ABSENT_MARKER = "is not staged at"
#: Shapes an ordinary launch refuses rather than overwrites: somebody else's
#: file, one anybody may write, one that is not a regular executable.
STAGED_TOOLING_HOSTILE_MARKERS = ("owned by uid", "writable", "is not a regular file")


def tenant_pinned_release_root(
    project: str, *, root: Path | None = None, install_root: Path | None = None
) -> Path | None:
    """The release a tenant's staged bundle was made from, when it says so.

    A launch is not an upgrade. Validating a tenant against whatever the host
    installed most recently would call an older tenant's complete bundle
    incomplete -- a file the NEWER release added is not missing from the older
    one -- and repairing it from `current` would move that tenant's role
    tooling while its board stayed pinned to the build it was deployed with.
    So the bundle's own release marker decides, and `current` is used only when
    a tenant has no staged release at all, which is the tenant that has nothing
    (SYRD-249 review).
    """
    from scripts import team_launcher as launcher

    staged = Path(launcher.role_tooling_staging_dir(project, root=root))
    marker = launcher._read_switchyard_release_marker(staged)
    if marker is None or not marker.marker_commit:
        return None
    candidate = (
        (install_root or launcher.switchyard_shared_install_root()) / "releases" / marker.marker_commit
    )
    return candidate if candidate.is_dir() else None


def director_readable_pinned_release(
    project: str, *, root: Path | None = None, install_root: Path | None = None
) -> tuple[Path | None, str, str]:
    """The release root pinned for this tenant, as the director is allowed to see it.

    Returns (release root, commit, "") or (None, "", why not). Root's own record
    of the pin sits in the privileged provision directory, which is 0700 root,
    so `finish-upgrade` -- unprivileged by design -- could not read it, took
    `origin/main` in its place without a word, and failed resolving a branch
    nobody had asked for after the operator had pinned and deployed a release
    (SYRD-255).

    Root already publishes where every role can read it the tenant's staged
    bundle, and the bundle's release marker names the release it was staged
    from. That marker is root's, not the tenant's: every directory to it is
    proven root-owned and unwritable by anybody else before a byte is
    believed, and the release it names is then held to the installed-release
    rules like any operator-named one -- root-controlled, and carrying root's
    marker for exactly this commit.
    """
    from scripts import team_launcher as launcher

    staging_override = os.environ.get("SWITCHYARD_TENANT_CONTROL_ROOT", "").strip()
    staged = Path(launcher.role_tooling_staging_dir(project, root=root))
    marker_path = staged / launcher.SWITCHYARD_RELEASE_MARKER_NAME
    from scripts.ticket_board.publication_boundary import root_controlled_problems

    # The documented seam, as for the installed release: the walk starts at "/"
    # on a host and moves only with the staging root's test override, because a
    # fixture cannot own "/".
    base = str(root) if root is not None else staging_override
    problems = root_controlled_problems(
        str(marker_path),
        expect_uid=os.getuid() if base else 0,
        base=base or "/",
    )
    if problems:
        return None, "", f"the staged release marker {marker_path} is not root's: " + "; ".join(problems)
    marker = launcher._read_switchyard_release_marker(staged)
    if marker is None or not marker.marker_commit:
        reason = marker.marker_error if marker is not None else "it is absent"
        return None, "", f"the staged release marker {marker_path} names no release: {reason}"
    commit = marker.marker_commit.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        return None, "", f"the staged release marker {marker_path} names {commit!r}, not a commit"
    release = (install_root or launcher.switchyard_shared_install_root()) / "releases" / commit
    if not release.is_dir():
        return None, "", f"{release}, the release {marker_path} names, is not installed"
    return release, commit, ""


def staged_bundle_launch_problems(
    project: str,
    *,
    release_root: str = "",
    root: Path | None = None,
    install_root: Path | None = None,
    expect_uid: int = STAGED_TOOLING_OWNER_UID,
) -> tuple[list[str], list[str], Path]:
    """What an ordinary launch may say about a staged bundle: (absent, hostile, release).

    One policy, asked by both halves of a launch -- the operator's, before it
    crosses to the tenant account, and the owner's, on the way back up -- so
    they cannot answer differently about the same tenant. Everything else a
    full verification reports (an older release marker, a name this release no
    longer carries) is deliberately not here: moving a tenant between releases
    is what `switchyard upgrade` is for.
    """
    from scripts import team_launcher as launcher

    pinned = launcher.tenant_pinned_release_root(project, root=root, install_root=install_root)
    release = (
        Path(release_root) if release_root
        else pinned or (install_root or launcher.switchyard_shared_install_root()) / "current"
    )
    problems = launcher.staged_role_tooling_problems(
        project, str(release), staging_root=Path(launcher.role_tooling_staging_dir(project, root=root)),
        expect_uid=expect_uid,
    )
    absent = [problem for problem in problems if launcher.STAGED_TOOLING_ABSENT_MARKER in problem]
    hostile = [
        problem for problem in problems
        if problem not in absent
        and any(marker in problem for marker in launcher.STAGED_TOOLING_HOSTILE_MARKERS)
    ]
    return absent, hostile, release
