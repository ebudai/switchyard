"""Root's notes for undoing an upgrade: the release rollback record and the pinned publication remote.

- `RELEASE_ROLLBACK_SCHEMA`, `release_rollback_path` and
  `record_release_rollback` write down, before anything is replaced, which
  shared release and staged tooling the host was whole on -- and leave that note
  alone on a retry of the same upgrade.
- `release_rollback_commands` turns root's note back into the exact commands
  that return to it.
- `record_publication_remote` records, in root's private provision directory,
  the remote `--publish-remote` named; `_write_publication_remote` writes it
  atomically and `restore_publication_remote` puts back what was there when a
  later step of the same upgrade refuses.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-378), in their original
order. The launcher imports this module and re-exports every name, so the
upgrade's phases, `switchyard upgrade`'s rollback report and every suite that
reaches these through the launcher reach the same objects. Every launcher
facility these use -- its release marker name included -- and every name
defined here that another definition here reads when it runs, is read from
`team_launcher` when it runs, as it was, so a patch on the launcher still
intercepts. The publication boundary's path helper is still imported inside the
two functions that use it. The standard-library names are this module's own
imports, the same objects. This module never imports `team_launcher` at its
top.
"""

from __future__ import annotations

import json
import os
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig


#: Root's note of what a host was running before an upgrade replaced it.
RELEASE_ROLLBACK_SCHEMA = "switchyard.release-rollback.v1"


def release_rollback_path(project: str) -> Path:
    """Root's own note of what this host was running before an upgrade."""
    from scripts import team_launcher as launcher

    return launcher.privileged_provision_dir(
        project, root=launcher.switchyard_privileged_provision_root()
    ) / "release-rollback.json"


def record_release_rollback(
    config: ProjectConfig,
    *,
    release,
    staging_root: Path | None = None,
    dry_run: bool = False,
    print_func: Callable[[str], None] = print,
) -> list[str]:
    """Write down what to come back to, before anything is replaced.

    An upgrade repoints the shared release, restages the tenant's root-owned
    tooling and rewrites its sudo grant. Each of those is recoverable only if
    something remembers what was there: otherwise the way back is whatever an
    operator can reconstruct from timestamps under /opt, at the moment they are
    least able to reconstruct anything.

    Written before the first replacement and left alone afterwards, so a retry
    that runs after a partial upgrade still names the release the host was whole
    on rather than the half-installed one it is on now (SYRD-93 live
    acceptance).
    """
    from scripts import team_launcher as launcher

    if dry_run:
        return []
    install_root = launcher.switchyard_shared_install_root()
    pointer = install_root / "current"
    previous_root = ""
    try:
        if os.path.islink(pointer):
            previous_root = os.readlink(pointer)
    except OSError as exc:
        return [f"could not read the current release pointer {pointer}: {exc}"]
    previous = launcher.shared_switchyard_release_for_path(Path(previous_root)) if previous_root else None
    staged = launcher._staged_tooling_dir(config, staging_root)
    staged_marker = staged / launcher.SWITCHYARD_RELEASE_MARKER_NAME
    staged_commit = ""
    try:
        if staged_marker.is_file():
            staged_commit = str(
                json.loads(staged_marker.read_text(encoding="utf-8")).get("commit") or ""
            )
    except (OSError, ValueError):
        staged_commit = ""
    # The tenant's deployed board build, apart from the host's shared release
    # and the tenant's staged tooling: three different things an operator has
    # to tell apart on the way back (SYRD-528). Unreadable is "", never a guess.
    board_commit = ""
    try:
        board_root = launcher._tenant_board_root_from_config_or_plan(config)
        if board_root is not None:
            board_commit = launcher._current_tenant_release(board_root)[1]
    except Exception:  # noqa: BLE001 -- a note that cannot name the build still names the rest
        board_commit = ""
    record = {
        "schema": launcher.RELEASE_ROLLBACK_SCHEMA,
        "project": config.project,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "upgrading_to": release.commit,
        "previous_release_root": previous_root,
        "previous_release_commit": previous.marker_commit if previous else "",
        "previous_staged_commit": staged_commit,
        "previous_board_commit": board_commit,
    }
    path = launcher.release_rollback_path(config.project)
    if path.is_file():
        # A retry after a partial upgrade must not overwrite the note taken when
        # the host was last whole. Only a record of a DIFFERENT upgrade is
        # replaced.
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}
        if str(existing.get("upgrading_to") or "") == release.commit:
            return []
    try:
        launcher.ensure_privileged_provision_dir(path.parent)
        launcher._write_private_json_atomic(path, record)
        path.chmod(launcher.privileged_artifact_mode(path.name))
    except OSError as exc:
        return [f"could not record the rollback for {config.project}: {exc}"]
    print_func(
        f"switchyard: recorded the way back for {config.project} in {path}: "
        + (record["previous_release_commit"] or previous_root or "no previous release")
    )
    return []


def record_publication_remote(
    project: str,
    remote: str,
    *,
    dry_run: bool = False,
    print_func: Callable[[str], None] = print,
) -> tuple[bool, str, list[str]]:
    """Root's own record of where this tenant publishes, from `--publish-remote`.

    `resolve_pinned_remote` has always read this file, but the only thing that
    wrote it was the publication boundary's installer, and the boundary was
    retired -- so `switchyard upgrade <project> --publish-remote <url>` stopped
    being remembered, and everything that decides by the remote found none.
    Live on mefp: `set-owner-identity mefp --clear` refused for want of a pin
    right after an upgrade that had been given one (SYRD-229). Written here
    alone, in root's private provision directory; nothing of the boundary is
    recreated. An unchanged value is left as it is.

    Returns (whether it was changed, what it replaced -- "" for nothing --, and
    why not). The upgrade calls this before its first write and treats any
    problem as fatal, so a failed pin never follows a half-done upgrade.
    """
    from scripts import team_launcher as launcher

    from scripts.ticket_board.publication_boundary import publish_remote_registration_path

    value = remote.strip()
    if not value or len(value) > 1024 or any(ch.isspace() or not ch.isprintable() for ch in value):
        return False, "", [
            f"{remote!r} is not a single git remote, so it cannot be recorded as {project}'s "
            "publication remote"
        ]
    path = publish_remote_registration_path(project, launcher.switchyard_privileged_provision_root())
    if path.is_symlink() or (path.exists() and not path.is_file()):
        return False, "", [f"{path} is not a regular file, so {project}'s publication remote cannot be recorded there"]
    try:
        current = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    except OSError as exc:
        return False, "", [f"{path} cannot be read ({exc.strerror})"]
    if current == value:
        print_func(f"switchyard: {project}'s publication remote is already recorded as {value}")
        return False, current, []
    if dry_run:
        print_func(
            f"switchyard: would record {project}'s publication remote as {value} in {path}"
            + (f" (replacing {current})" if current else "")
        )
        return False, current, []
    if os.geteuid() != 0:
        return False, current, [
            f"recording {project}'s publication remote is root's; run the upgrade with sudo"
        ]
    problem = launcher._write_publication_remote(path, value)
    if problem:
        return False, current, [problem]
    print_func(f"switchyard: recorded {project}'s publication remote as {value} in {path}")
    return True, current, []


def _write_publication_remote(path: Path, value: str) -> str:
    from scripts import team_launcher as launcher

    try:
        launcher.ensure_privileged_provision_dir(path.parent)
        staged = path.with_name(f".{path.name}.new")
        descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.write(descriptor, (value + "\n").encode("utf-8"))
        finally:
            os.close(descriptor)
        os.replace(staged, path)
    except OSError as exc:
        return f"could not record the publication remote at {path}: {exc}"
    return ""


def restore_publication_remote(project: str, previous: str) -> str:
    """Put the pin back as it was, after a later step of the same upgrade refused."""
    from scripts import team_launcher as launcher

    from scripts.ticket_board.publication_boundary import publish_remote_registration_path

    path = publish_remote_registration_path(project, launcher.switchyard_privileged_provision_root())
    if previous:
        return launcher._write_publication_remote(path, previous)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        return f"could not remove {path}: {exc}"
    return ""


def release_rollback_commands(project: str, *, publish_remote: str = "") -> list[str]:
    """The way back from root's own note, as the tenant's -- never the host's. Empty when there is no note.

    An upgrade restages the TENANT: its tooling, its grant, its board. It never
    moves the host's shared release, so the way back never does either: the old
    first line, `ln -sfn <previous shared> /opt/switchyard/current`, repointed
    every tenant on the host, and when the operator had installed the shared
    release before the upgrade (the supported order) it named the release being
    left (SYRD-528). The tenant returns to the release its staged tooling was
    from, through the admin-authenticated `select-shared-release`, which only
    takes a commit root already holds -- and only when root holds it. Lines
    beginning `#` are what an operator must know, not commands; a way back that
    cannot be named is said, not replaced by one that runs.

    `publish_remote` is accepted for the callers that pass it; the catalogued
    action keeps the tenant's recorded publication remote itself.
    """
    from scripts import team_launcher as launcher

    path = launcher.release_rollback_path(project)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if str(record.get("schema") or "") != launcher.RELEASE_ROLLBACK_SCHEMA:
        return []
    upgraded_to = str(record.get("upgrading_to") or "").strip()
    host_commit = str(record.get("previous_release_commit") or "").strip()
    tenant_commit = str(record.get("previous_staged_commit") or "").strip()
    board_commit = str(record.get("previous_board_commit") or "").strip()
    lines: list[str] = []
    if host_commit:
        lines.append(
            f"# the host's shared release ({host_commit}) is every tenant's and this upgrade did not move it; "
            "it is not part of this way back (an operator's own activation is undone with "
            "`switchyard install-shared-release --rollback`)"
        )
    exact = len(tenant_commit) == 40 and all(char in "0123456789abcdef" for char in tenant_commit)
    if not exact:
        lines.append(
            f"# no earlier release of {project} was recorded before this upgrade"
            + (f" to {upgraded_to}" if upgraded_to else "")
            + ", so no way back can be named: returning it is an operator's decision, from its release-status"
        )
        return lines
    if tenant_commit == upgraded_to:
        lines.append(f"# {project} was already on {tenant_commit} before this upgrade: there is nothing to go back to")
        return lines
    release_root = launcher.switchyard_shared_install_root() / "releases" / tenant_commit
    held = launcher._read_switchyard_release_marker(release_root)
    if held is None or held.marker_commit != tenant_commit:
        lines.append(
            f"# {project} ran {tenant_commit} before this upgrade, but this host holds no installed release of it "
            f"({release_root}): an operator installs that release first; until then there is no way back to name"
        )
        return lines
    board = (
        f"it was on {board_commit}" if board_commit and board_commit != tenant_commit
        else "it was on the same release" if board_commit else "its earlier build was not recorded"
    )
    lines.append(
        f"# this redeploys {project}'s board at {tenant_commit} ({board}). Database migrations the newer "
        "release applied are not reversed, and running the older board against them is not verified: "
        "confirm before applying"
    )
    action = f"switchyard privileged-action {shlex.quote(project)} select-shared-release commit={tenant_commit}"
    lines.extend([f"{action} --dry-run", action])
    return lines