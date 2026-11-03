"""The directories between a tenant owner's home and its session store (SYRD-732).

`scripts/role_state_ownership.py` checks and repairs the role state store from the session directory down. These
are the directories above it, which an older root run could leave root's: each is read, and given back, on its own.
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig


def walk_state_parents(
    config: ProjectConfig, act: Callable[[Path, os.stat_result, Callable[[int, int], None]], str],
) -> tuple[list[tuple[Path, str]], list[tuple[Path, str]]]:
    """Each directory from the owner's home down to the session store's parent, alone: (findings, refusals).

    The store's roots begin at the session directory, so a parent an older root run created (`.local/state/<project>
    -ticket-board`, root's 0755) was never given back. A local Hermes role's home is walked from the owner's home
    and refuses it (SYRD-732). Opened one component at a time from the home without following a link; none is walked
    into, and none outside the home is named.
    """
    from scripts import team_launcher as launcher

    try:
        home = Path(pwd.getpwnam(config.run_as_user or launcher.current_user_name()).pw_dir)
        session = config.session_dir.expanduser()
        names = session.relative_to(home).parts[:-1]
    except (KeyError, ValueError, AttributeError, TypeError):
        return [], []  # no account, no home, or a store outside it: no parents of its to give back
    # `relative_to` is lexical: a `..` the tenant wrote would carry the walk, and root's chown, out of the home.
    # Nothing is opened for a path that is not absolute and plain (Audit, on fd8114bd).
    if not session.is_absolute() or any(part in (".", "..") for part in session.parts):
        return [], [(session, "is not a plain absolute path below the owner's home (it has `.` or `..` in it)")]
    findings: list[tuple[Path, str]] = []
    refusals: list[tuple[Path, str]] = []
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(home, flags)
    except OSError as exc:
        return [], [(home, f"cannot be opened without following a link ({exc.strerror})")]
    path = home
    try:
        for name in names:
            try:
                below = os.open(name, flags, dir_fd=fd)
            except FileNotFoundError:
                break  # nothing made yet, so nothing to give back
            except OSError as exc:
                refusals.append((path / name, f"cannot be opened without following a link ({exc.strerror})"))
                break
            os.close(fd)
            fd, path = below, path / name

            def chown(uid: int, gid: int, _fd: int = below) -> None:
                os.fchown(_fd, uid, gid)

            reason = act(path, os.fstat(fd), chown)
            if reason:
                findings.append((path, reason))
    finally:
        os.close(fd)
    return findings, refusals


def repair_state_parents(config: ProjectConfig, *, dry_run: bool = False,
                         print_func: Callable[[str], None] = print) -> bool:
    """Give the owner back each directory above its session store. Root's, at upgrade, after the store's repair.

    A link on the way refuses before anything is changed; each wrong-owner directory is given back through the
    descriptor it was read through, alone; and the chain is read again afterwards.
    """
    from scripts import team_launcher as launcher

    owner = config.run_as_user or launcher.current_user_name()
    try:
        ids = pwd.getpwnam(owner)
    except KeyError:
        return True  # no such account: role_state_ownership has already said so

    def inspect(path: Path, info: os.stat_result, _chown: Callable[[int, int], None]) -> str:
        return "" if info.st_uid == ids.pw_uid else f"is owned by uid {info.st_uid}, not {owner}"

    problems, refusals = walk_state_parents(config, inspect)
    if refusals:
        print_func(f"switchyard: refusing to touch {config.project}'s state directories: {refusals[0][0]} "
                   f"{refusals[0][1]}. No ownership was changed.")
        return False
    if not problems:
        return True
    first = f"{problems[0][0]} {problems[0][1]}"
    if os.geteuid() != 0:
        print_func(f"switchyard: {config.project}'s state directories are not all {owner}'s (first: {first}), and "
                   f"only root can give them back. Run `sudo switchyard upgrade {config.project}`.")
        return False
    if dry_run:
        print_func(f"switchyard: would give {owner} back {len(problems)} directory(ies) above {config.project}'s "
                   f"session store, starting with {first}; nothing written")
        return True
    failures: list[str] = []

    def give_back(path: Path, info: os.stat_result, chown: Callable[[int, int], None]) -> str:
        if info.st_uid != ids.pw_uid:
            try:
                chown(ids.pw_uid, ids.pw_gid)
            except OSError as exc:
                failures.append(f"switchyard: could not give {path} back to {owner}: {exc.strerror}")
        return ""

    _found, refused = walk_state_parents(config, give_back)
    remaining, refused_again = walk_state_parents(config, inspect)
    for message in failures + [f"switchyard: {p} {r}" for p, r in refused + refused_again] + [
            f"switchyard: {p} {r} after the repair" for p, r in remaining]:
        print_func(message)
        return False
    print_func(f"switchyard: gave {owner} back {len(problems)} directory(ies) above {config.project}'s session "
               "store, so a role's home under it can be walked as the account's own")
    return True
