"""Where the shared Switchyard release lives, what its marker says, and which release is running.

- `switchyard_shared_install_root` is the shared install (`/opt/switchyard`
  unless `SWITCHYARD_SHARED_INSTALL_ROOT` names another), and
  `switchyard_shared_target` / `switchyard_shared_pane_launcher` are the
  `switchyard` and pane launcher its `current` release provides.
- `_read_switchyard_release_marker` reads one release directory's marker into
  a `SharedSwitchyardRelease`: absent, unreadable, without a commit, or the
  commit it names.
- `shared_switchyard_release_for_path` maps a path to the installed release
  it lies in: the nearest marker between it and the install root, else a bare
  release under `current` or `releases`, else none;
  `running_launcher_release` asks that of the launcher's own checkout.
- `switchyard_version_text` and `report_installed_release_version` say which
  release this is, and when the installed release is older than the checkout
  it came from -- a notice that can never fail the command.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-416), in their original
order. The launcher imports this module and re-exports all thirteen names,
the class included (the same object); its own callers and the modules that
read them -- trusted bootstrap and upgrade, the staged launch checks, release
alignment and rollback, the tenant reports and the rest -- still reach them
through the launcher. Everything the eight functions read -- each other, the
constants and the class included, and the launcher's name, repository root,
path check and own file -- is read through the launcher at call time, so a
suite that rebinds one there still intercepts it, and `__file__` is still the
launcher's. The defaults Python binds at definition (`subprocess.run`) come
from the standard library. This module imports `team_launcher` only inside the
functions, when they run.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


SWITCHYARD_NAME = "switchyard"


DEFAULT_SWITCHYARD_SHARED_INSTALL_ROOT = Path("/opt/switchyard")
SWITCHYARD_RELEASE_MARKER_NAME = ".switchyard-release.json"


SWITCHYARD_VERSION = "dev"


@dataclass(frozen=True)
class SharedSwitchyardRelease:
    root: Path
    marker_commit: str = ""
    marker_error: str = ""

    @property
    def active(self) -> bool:
        return bool(self.marker_commit)

    @property
    def undeterminable(self) -> bool:
        return bool(self.error)


def switchyard_shared_install_root() -> Path:
    from scripts import team_launcher as launcher

    return Path(os.environ.get("SWITCHYARD_SHARED_INSTALL_ROOT", launcher.DEFAULT_SWITCHYARD_SHARED_INSTALL_ROOT)).expanduser()


def switchyard_shared_target(root: Path | None = None) -> Path:
    from scripts import team_launcher as launcher

    return (root or launcher.switchyard_shared_install_root()) / "current" / launcher.SWITCHYARD_NAME


def switchyard_shared_pane_launcher(root: Path | None = None) -> Path:
    from scripts import team_launcher as launcher

    return (root or launcher.switchyard_shared_install_root()) / "current" / "scripts" / launcher.TEAM_LAUNCHER_NAME


def _read_switchyard_release_marker(path: Path) -> SharedSwitchyardRelease | None:
    from scripts import team_launcher as launcher

    marker = path / launcher.SWITCHYARD_RELEASE_MARKER_NAME
    if not marker.exists():
        return None
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return launcher.SharedSwitchyardRelease(root=path, marker_error=str(exc))
    commit = str(payload.get("commit") or "").strip()
    if not commit:
        return launcher.SharedSwitchyardRelease(root=path, marker_error=f"{marker} has no commit")
    return launcher.SharedSwitchyardRelease(root=path, marker_commit=commit)


def shared_switchyard_release_for_path(path: Path, *, install_root: Path | None = None) -> SharedSwitchyardRelease | None:
    from scripts import team_launcher as launcher

    root = (install_root or launcher.switchyard_shared_install_root()).expanduser().resolve(strict=False)
    candidate = path.expanduser().resolve(strict=False)
    if not launcher._path_is_under(candidate, root):
        return None
    for probe in (candidate, *candidate.parents):
        if not launcher._path_is_under(probe, root):
            break
        marker = launcher._read_switchyard_release_marker(probe)
        if marker is not None:
            return marker
    if launcher._path_is_under(candidate, root / "current") or launcher._path_is_under(candidate, root / "releases"):
        return launcher.SharedSwitchyardRelease(root=candidate)
    return None


def switchyard_version_text(repo_root: Path | None = None) -> str:
    from scripts import team_launcher as launcher

    root = repo_root or launcher._repo_root()
    release = launcher._read_switchyard_release_marker(root)
    if release is not None and release.marker_commit:
        return f"switchyard {release.marker_commit}"
    return f"switchyard {launcher.SWITCHYARD_VERSION}"


def running_launcher_release(root: Path | None = None) -> SharedSwitchyardRelease | None:
    """Which installed release this process is executing out of, if any."""
    from scripts import team_launcher as launcher

    return launcher.shared_switchyard_release_for_path(
        (root or Path(launcher.__file__).resolve().parent.parent)
    )


def report_installed_release_version(
    *,
    root: Path | None = None,
    environ: dict[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] | None = None,
) -> list[str]:
    """Say when the installed release is older than the checkout it came from.

    Pulling a checkout does not change what `switchyard` runs, because it runs
    from the installed release. The only symptom is a bug the user has already
    been told is fixed, so the conclusion they draw is that it was not -- which
    costs trust rather than time. This reports the mismatch and stops there:
    reinstalling is privileged and is theirs to decide (SYRD-94).

    Nothing here can fail the command it is attached to. A version notice that
    can break the tool is worse than the silence it replaces.
    """
    from scripts import team_launcher as launcher

    from scripts.version_notice import release_notice_lines

    emit = print_func or (lambda line: print(line, file=sys.stderr))
    try:
        lines = release_notice_lines(
            (root or launcher._repo_root()),
            environ=dict(os.environ) if environ is None else environ,
            runner=runner,
        )
    except Exception:
        return []
    for line in lines:
        emit(line)
    return lines
