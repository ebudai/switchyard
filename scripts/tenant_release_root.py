"""Whether a tenant's owner can publish a release under its board root, and root's repair when it cannot.

- `RELEASE_ROOT_WRITABLE_DIRS` and `RELEASE_ROOT_WRITABLE_FILES` name the only
  entries under a board root the owner-run deploy writes.
- `_open_release_root_entries` opens the board root and those entries without
  following a link, and refuses anything that is not owned by the owner or
  root, is the wrong type, or has another link.
- `prepare_tenant_release_root` repairs them -- attributed only from root's
  baseline and the trusted owner identity, recorded first in
  `release_root_repair_record_path`, then given to the owner -- or only
  inspects and refuses when nothing trusted names the owner.
- `owner_release_root_problems` asks the same question read-only, as the owner.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-380), in their original
order. The launcher imports this module and re-exports every name, so the
upgrade's phases, `finish-upgrade` and every suite that reaches these through
the launcher reach the same objects. Every launcher facility these use -- the
trusted owner identity, which stays in the launcher, included -- and every name
defined here that another definition here reads when it runs, is read from
`team_launcher` when it runs, as it was, so a patch on the launcher still
intercepts. The standard-library names are this module's own imports, the same
objects. This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import errno
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig


#: The entries under a board root the owner-run deploy has to be able to write,
#: and nothing else. `deploy-restart` makes its release in `releases/` (a temp
#: tree, then a rename), replaces `current` and `canary.env` by rename in the
#: board root, and rewrites `system-unit.sha256` in place. Existing release
#: trees are only read -- activation and rollback move a link -- so they are
#: left exactly as they are.
RELEASE_ROOT_WRITABLE_DIRS = ("releases",)
RELEASE_ROOT_WRITABLE_FILES = ("system-unit.sha256",)


def release_root_repair_record_path(project: str) -> Path:
    from scripts import team_launcher as launcher

    return launcher.privileged_provision_dir(
        project, root=launcher.switchyard_privileged_provision_root()
    ) / "release-root-repair.json"


def _open_release_root_entries(
    board_root: Path, allowed_uids: set[int], owner_user: str, project: str
) -> tuple[list[tuple[str, int, os.stat_result, int]], str]:
    """Open the board root and the entries a deploy writes, following nothing.

    Returns (entries, problem): each entry is (path, fd, stat, bits the owner
    needs), board root first, and the caller closes every fd. No entries and
    no problem means there is no board root yet.
    """
    from scripts import team_launcher as launcher

    relative = Path(str(board_root).lstrip("/")) / "_"
    root_fd, walk_problem = launcher._walk_no_follow(Path(board_root.anchor or "/"), relative)
    if root_fd < 0:
        if walk_problem == "missing":
            # Nothing there yet: the owner's own first deploy makes it.
            return [], ""
        return [], f"{board_root} cannot be walked safely: {walk_problem}"
    root_info = os.fstat(root_fd)
    entries = [(str(board_root), root_fd, root_info, 0o700)]

    def refuse(problem: str) -> tuple[list[tuple[str, int, os.stat_result, int]], str]:
        for _path, fd, _info, _bits in entries:
            os.close(fd)
        return [], problem

    if not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid not in allowed_uids:
        return refuse(f"{board_root} is owned by uid {root_info.st_uid}, neither {owner_user} "
                      "nor root, so it cannot be attributed to this tenant")
    for name, directory in (
        *((n, True) for n in launcher.RELEASE_ROOT_WRITABLE_DIRS),
        *((n, False) for n in launcher.RELEASE_ROOT_WRITABLE_FILES),
    ):
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | (
            os.O_DIRECTORY if directory else os.O_NONBLOCK
        )
        try:
            fd = os.open(name, flags, dir_fd=root_fd)
        except FileNotFoundError:
            continue
        except OSError as exc:
            kind = "a symbolic link" if exc.errno in (errno.ELOOP, errno.ENOTDIR) else exc.strerror
            return refuse(f"{board_root / name} is {kind}; refusing to repair {project}'s release root")
        info = os.fstat(fd)
        entries.append((str(board_root / name), fd, info, 0o700 if directory else 0o600))
        if directory and not stat.S_ISDIR(info.st_mode):
            return refuse(f"{board_root / name} is not a directory")
        if not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            return refuse(f"{board_root / name} is not a regular file with one link")
        if info.st_uid not in allowed_uids:
            return refuse(f"{board_root / name} is owned by uid {info.st_uid}, neither "
                          f"{owner_user} nor root, so it cannot be attributed to this tenant")
    return entries, ""


def prepare_tenant_release_root(
    config: ProjectConfig,
    *,
    dry_run: bool = False,
    print_func: Callable[[str], None] = print,
) -> list[str]:
    """Make sure the tenant owner can publish a release under its board root.

    Live on mefp: the upgrade reported the release `ready` and printed a deploy
    sequence; the operator stopped the listener and ran it; and the deploy,
    running as the owner, could not create its release -- `releases/` under the
    board root had been made by root, by an older release that deployed as root
    (SYRD-231). A fresh tenant has this right already; a legacy one is repaired
    here, before the sequence is printed, or refused.

    A repair is attributed only from what root controls: the owner from root's
    baseline and the kernel (`trusted_owner_identity`), the board root from
    root's baseline, and it must be exactly `<home>/<project>-ticketboard-live`.
    Without that, the same entries are only inspected -- a board root that needs
    nothing is ready, and one that needs a repair is refused. Every component is
    walked without following a link. The board root, `releases/` and
    `system-unit.sha256` may each be owned by the owner or -- as legacy state --
    by root, and nothing else: a symlink, another account's entry or a file with
    another link is refused. Only those entries are changed, never recursively:
    owner and owner-writable. The repair is recorded in root's own provision
    directory. A dry run describes it and writes nothing. Returns why not;
    empty means ready.
    """
    from scripts import team_launcher as launcher

    project = config.project
    identity = launcher.trusted_owner_identity(project)
    if identity.trusted:
        baseline, problem = launcher.read_plan_no_follow(
            launcher.privileged_baseline_plan_path(project), require_root_owned=True
        )
        if baseline is None:
            return [f"{project}'s board root cannot be established from root's baseline: {problem}"]
        board_root = Path(str(baseline.data.get("board_root") or ""))
        expected = Path(identity.owner_home) / f"{project}-ticketboard-live"
        if board_root != expected:
            return [f"root's baseline names {board_root or 'no board root'} for {project}, not "
                    f"{expected}; a release root elsewhere is not one this repair will touch"]
        owner_user, owner_uid, owner_gid = identity.owner_user, identity.owner_uid, identity.owner_gid
        unattributed = ""
    else:
        # Only looked at, never changed: which account the tenant says it runs
        # as decides nothing but where to look.
        owner_user = str(config.run_as_user or "").strip()
        owner_uid = launcher.uid_for_user(owner_user) if owner_user else None
        home = launcher.home_dir_for_user(owner_user) if owner_user else None
        if owner_uid is None or home is None:
            return []
        owner_gid = -1
        board_root = Path(home) / f"{project}-ticketboard-live"
        unattributed = "; ".join(identity.problems)
    entries, problem = launcher._open_release_root_entries(board_root, {0, owner_uid}, owner_user, project)
    if problem:
        return [problem]
    try:
        changes = [
            (path, fd, info, bits) for path, fd, info, bits in entries
            if info.st_uid != owner_uid or (info.st_mode & bits) != bits
        ]
        if not changes:
            return []
        described = [
            f"{path} to {owner_user} (now uid {info.st_uid}, mode "
            f"{stat.S_IMODE(info.st_mode):o} -> {stat.S_IMODE(info.st_mode) | bits:o})"
            for path, _fd, info, bits in changes
        ]
        if unattributed:
            if dry_run:
                try:
                    launcher.privileged_baseline_plan_path(project).lstat()
                except FileNotFoundError:
                    # The upgrade this dry run describes stages that baseline
                    # before it gets here; refusing would predict a stop the
                    # real run does not make.
                    for line in described:
                        print_func(f"switchyard: would give {line} once root's baseline is staged")
                    return []
                except OSError:
                    pass
            return [f"{project}'s owner must be given {', '.join(path for path, *_ in changes)} "
                    f"before it can publish a release, and root will not attribute them: "
                    f"{unattributed}"]
        if dry_run:
            for line in described:
                print_func(f"switchyard: would give {line} so the owner can publish a release")
            return []
        if os.geteuid() != 0:
            return [f"{board_root} holds entries {owner_user} cannot write; repairing them is "
                    f"root's -- run `sudo switchyard upgrade {project}`"]
        record = {
            "schema": "switchyard.release-root-repair.v1",
            "project": project,
            "owner": owner_user,
            "at": datetime.now(timezone.utc).isoformat(),
            "entries": [
                {"path": path, "uid_before": info.st_uid,
                 "mode_before": f"{stat.S_IMODE(info.st_mode):o}",
                 "mode_after": f"{stat.S_IMODE(info.st_mode) | bits:o}"}
                for path, _fd, info, bits in changes
            ],
        }
        record_path = launcher.release_root_repair_record_path(project)
        try:
            # Recorded before anything changes, so a repair that stops halfway
            # still says what it was doing.
            launcher.ensure_privileged_provision_dir(record_path.parent)
            launcher._write_private_json_atomic(record_path, record)
            for path, fd, info, bits in changes:
                os.fchown(fd, owner_uid, owner_gid)
                os.fchmod(fd, stat.S_IMODE(info.st_mode) | bits)
                print_func(f"switchyard: gave {path} to {owner_user} so it can publish a release")
        except OSError as exc:
            return [f"could not repair {project}'s release root: {exc}"]
        print_func(f"switchyard: recorded that repair in {record_path}")
        return []
    finally:
        for _path, fd, _info, _bits in entries:
            os.close(fd)


def owner_release_root_problems(config: ProjectConfig) -> list[str]:
    """The same question, asked by a process that cannot repair: can the owner write it?

    Read-only, so it may use the tenant's own view of where its home is: it
    changes nothing and only decides whether a deploy sequence is printed. Asked
    only when this process IS the owner, which is when access() answers for the
    account that will run the deploy.
    """
    from scripts import team_launcher as launcher

    owner = str(config.run_as_user or "").strip()
    if not owner or owner != launcher.current_user_name():
        return []
    home = launcher.home_dir_for_user(owner)
    if home is None:
        return []
    board_root = Path(home) / f"{config.project}-ticketboard-live"
    unwritable = [
        str(path) for path in (board_root, board_root / "releases",
                               *(board_root / name for name in launcher.RELEASE_ROOT_WRITABLE_FILES))
        if path.exists() and not os.access(path, os.W_OK)
    ]
    if not unwritable:
        return []
    return [f"{owner} cannot write {', '.join(unwritable)}, so its deploy could not publish a "
            f"release; `sudo switchyard upgrade {config.project}` repairs a legacy release root"]
