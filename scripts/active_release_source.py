"""The source root records for an exact-release upgrade that names none (SYRD-578).

`switchyard upgrade <p> --deploy-ref <sha>` on a tenant with no recorded pin --
what the catalogued `upgrade-tenant-release` runs, and what an operator ran on
Otto -- recorded an empty source beside the exact commit and exited 0. The
`deploy-release` it then advertised refuses a pin whose source is not an
installed release of that commit, so exit 0 promised a continuation that could
not run.

`active_release_source` names the one source root may take without being told:
this host's active installed release of exactly that commit. Everything about
it is checked, and every check fails closed:
- the path is built here from the shared install root and the commit, never
  taken from a caller;
- it is a real directory, not a symbolic link;
- every component of it belongs to root (`root_controlled_problems`, as for an
  installed release named explicitly);
- its own marker records that commit;
- `current` resolves to it, so it is the release the host runs.
Anything else is a reason, which the upgrade prints with the preparation it
needs, before it records or stops anything.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Callable


def active_release_source(commit: str) -> tuple[Path | None, str]:
    """This host's active, root-controlled installed release of exactly `commit`, or why there is none."""
    from scripts import team_launcher as launcher
    from scripts.ticket_board.publication_boundary import root_controlled_problems

    install_root = launcher.switchyard_shared_install_root()
    release = install_root / "releases" / commit
    try:
        info = os.lstat(release)
    except FileNotFoundError:
        return None, f"{release} does not exist"
    except OSError as exc:
        return None, f"{release} cannot be inspected ({exc.strerror})"
    if stat.S_ISLNK(info.st_mode):
        return None, f"{release} is a symbolic link, so where it points is somebody else's choice"
    if not stat.S_ISDIR(info.st_mode):
        return None, f"{release} is not a release directory"
    # The same whole-path proof an explicitly named installed release gets: "/"
    # on a host, the install root only under its documented test override.
    overridden = os.environ.get("SWITCHYARD_SHARED_INSTALL_ROOT", "").strip()
    problems = root_controlled_problems(str(release), base=overridden or "/")
    if problems:
        return None, f"{release} is not root-controlled: {problems[0]}"
    marker = launcher._read_switchyard_release_marker(release)
    recorded = marker.marker_commit if marker is not None else ""
    if recorded != commit:
        return None, f"{release} records {recorded or 'no release'}, not {commit}"
    current = install_root / "current"
    active = os.path.realpath(current)
    if active != os.path.realpath(release):
        return None, f"{release} is installed but not active: {current} is {active}"
    return release, ""


def exact_release_source(project: str, commit: str, *, print_func: Callable[[str], None]) -> tuple[Path | None, bool]:
    """The source an exact-release upgrade with none records, and whether it must refuse.

    Root only: an upgrade that is not root records no pin, and says so. A dry
    run decides exactly as the upgrade it previews would.
    """
    from scripts import team_launcher as launcher

    if os.geteuid() != 0:
        return None, False
    release, reason = active_release_source(commit)
    if release is not None:
        print_func(
            f"switchyard: {project} names no source for {commit}; its source is {release}, this host's "
            "active, root-controlled installed release of that commit."
        )
        return release, False
    releases = launcher.switchyard_shared_install_root() / "releases"
    print_func(
        f"switchyard: {project} names no source for {commit}, and root has no release it may take for one: "
        f"{reason}."
    )
    print_func(
        f"switchyard: install and activate that release first, with `switchyard privileged-action {project} "
        f"install-shared-release commit={commit}`, or name an installed root-owned release of it with "
        f"`--source-repo {releases / commit}`. Refusing before recording or stopping anything: an empty "
        f"source would let this upgrade exit 0 and advertise a `deploy-release` that refuses it. Nothing was changed."
    )
    return None, True
