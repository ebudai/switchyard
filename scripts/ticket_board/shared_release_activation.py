#!/usr/bin/env python3
"""Make an already-built release this host's current one, by commit alone.

`/opt/switchyard/current` decides which Switchyard code root executes for every
tenant on the machine. Moving it is therefore the most consequential thing in
this whole boundary, and until now it was done in the operator packet with a
bare `ln -sfn` -- no record of what it had been, no verification that it landed,
and no way back except whatever an operator could reconstruct from timestamps
under /opt at the moment they were least able to reconstruct anything.

This is the bounded version of that line. It takes a commit and nothing else --
never a path, so there is nothing for a caller to aim somewhere -- and it goes
in one order that the rest of the file exists to keep:

1. **resolve** the commit against root's own release cache, proving the release
   is there, is not a symlink, is root-owned, and carries a marker recording
   that same commit;
2. **record** what `current` points at now, durably, BEFORE anything moves;
3. **swap** atomically, by renaming a new symlink over the pointer, so no
   reader ever sees a missing or half-written `current`;
4. **verify** what actually landed, by reading the pointer back and checking
   the marker at the other end of it;
5. **roll back** to the recorded target if that verification fails.

Step 2 before step 3 is the whole point. A record written afterwards is a
record that does not exist for the one failure it was for.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

if __package__ in (None, ""):  # pragma: no cover - direct execution as a program
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from ticket_board import privileged_operations
else:
    from . import privileged_operations

DEFAULT_INSTALL_ROOT = Path("/opt/switchyard")
POINTER_NAME = "current"
RELEASES_NAME = "releases"
#: Root's own note of what `current` pointed at before the last activation.
#: Beside the pointer, because it is about the pointer and not about any one
#: tenant -- a host-wide change recorded in a tenant's directory would be found
#: by nobody looking for it.
ROLLBACK_RECORD_NAME = "current.rollback.json"
ACTIVATION_SCHEMA = "switchyard.shared-release-activation.v1"
RECORD_MODE = 0o644


class ActivationFailed(Exception):
    """Activation did not happen, or was undone. Never a partial success."""


@dataclass
class Activation:
    """What happened, in enough detail to audit it afterwards."""

    commit: str
    release_root: str
    previous_target: str = ""
    previous_commit: str = ""
    record_path: str = ""
    rolled_back: bool = False
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        if self.rolled_back:
            return (
                f"{self.commit} did not verify and {self.previous_target or 'nothing'} "
                "was put back"
            )
        return f"{self.commit} is now current (was {self.previous_commit or 'nothing'})"


def pointer_path(install_root: Path = DEFAULT_INSTALL_ROOT) -> Path:
    return install_root / POINTER_NAME


def rollback_record_path(install_root: Path = DEFAULT_INSTALL_ROOT) -> Path:
    return install_root / ROLLBACK_RECORD_NAME


def read_pointer(install_root: Path = DEFAULT_INSTALL_ROOT) -> str:
    """Where `current` points, without following it.

    `readlink`, not `resolve`: what matters is the link's own value, because
    that is what will be put back on a rollback. A pointer that is a real
    directory rather than a symlink is refused elsewhere -- replacing one
    atomically is not possible, and silently removing a directory somebody
    installed would be worse than failing.
    """
    pointer = pointer_path(install_root)
    try:
        return os.readlink(pointer)
    except OSError:
        return ""


def _release_marker_commit(release_root: str) -> str:
    return privileged_operations._read_marker_commit(
        f"{release_root}/{privileged_operations.RELEASE_MARKER_NAME}"
    )


def _write_record(path: Path, record: dict) -> None:
    """Root's note, written whole or not at all, and readable by everyone.

    Through a temporary file in the same directory and a rename, for the same
    reason the pointer itself is swapped that way: a reader that arrives during
    the write must see the old record or the new one, never half of one. It is
    world-readable because it is evidence -- a role that cannot read the way
    back cannot tell an operator what it is -- and it contains no secret.
    """
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, RECORD_MODE
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    os.chmod(temporary, RECORD_MODE)
    os.replace(temporary, path)


def _swap_pointer(install_root: Path, target: str) -> None:
    """Point `current` at `target`, atomically.

    A symlink cannot be rewritten in place, so a new one is made under a
    temporary name in the same directory and renamed over the pointer.
    `rename` within a directory is atomic, so every reader sees either the old
    target or the new one -- never a moment with no `current` at all, which is
    what `ln -sfn` gives you if it is interrupted between unlink and symlink.
    """
    pointer = pointer_path(install_root)
    temporary = pointer.with_name(f"{POINTER_NAME}.activating")
    try:
        os.remove(temporary)
    except FileNotFoundError:
        pass
    os.symlink(target, temporary)
    os.rename(temporary, pointer)


def activate(
    commit: str,
    *,
    install_root: Path = DEFAULT_INSTALL_ROOT,
    owner_uid: int | None = None,
    now: Callable[[], str] = lambda: datetime.now(timezone.utc).isoformat(),
    print_func: Callable[[str], None] = print,
) -> Activation:
    """Activate an already-built release, or leave the host as it was.

    Raises `ActivationFailed` rather than returning a code, because every
    caller of this has to treat "it did not happen" differently from "it did",
    and a return value is too easy to not look at.
    """
    releases = str(install_root / RELEASES_NAME)
    try:
        release_root = privileged_operations.trusted_release_root(
            commit, releases=releases, owner_uid=owner_uid
        )
    except privileged_operations.NoTrustedSource as exc:
        raise ActivationFailed(str(exc)) from None

    pointer = pointer_path(install_root)
    # A pointer that is not a symlink cannot be swapped atomically, and the
    # alternative -- removing whatever is there -- would destroy an installation
    # this has no business touching.
    if pointer.exists() and not pointer.is_symlink():
        raise ActivationFailed(
            f"{pointer} is not a symbolic link, so it cannot be changed atomically. "
            "Something other than Switchyard installed it; resolve that by hand"
        )

    previous_target = read_pointer(install_root)
    previous_commit = _release_marker_commit(previous_target) if previous_target else ""
    result = Activation(
        commit=commit,
        release_root=release_root,
        previous_target=previous_target,
        previous_commit=previous_commit,
        record_path=str(rollback_record_path(install_root)),
    )
    if previous_target == release_root:
        # Already current. Saying so is better than rewriting the pointer to
        # the value it already has and claiming a change: a no-op recorded as
        # an activation makes the journal describe something that never
        # happened.
        result.notes.append(f"{commit} is already the current release; nothing was changed")
        print_func(f"switchyard: {commit} is already current at {release_root}")
        return result

    record = {
        "schema": ACTIVATION_SCHEMA,
        "recorded_at": now(),
        "activating": commit,
        "activating_root": release_root,
        "previous_target": previous_target,
        "previous_commit": previous_commit,
    }
    try:
        _write_record(rollback_record_path(install_root), record)
    except OSError as exc:
        # Before the swap, so refusing here leaves the host exactly as it was.
        raise ActivationFailed(
            f"could not record the way back in {rollback_record_path(install_root)} ({exc}); "
            "nothing was changed, because an activation with no recorded previous target "
            "is one nobody can undo"
        ) from None

    # Re-read immediately before the swap. It narrows the window in which a
    # concurrent activation could have moved the pointer since it was read --
    # it does not close it, because a symlink rename is not a compare-and-swap
    # and this deliberately does not hold a lock over a privileged operation.
    # What makes a lost race recoverable rather than silent is the record
    # above: whoever swapped last wrote down what they found.
    if read_pointer(install_root) != previous_target:
        raise ActivationFailed(
            f"{pointer} changed while this activation was preparing (it pointed at "
            f"{previous_target or 'nothing'} and now points at "
            f"{read_pointer(install_root) or 'nothing'}). Nothing was changed; "
            "another activation is running, so run this again once it has finished"
        )

    try:
        _swap_pointer(install_root, release_root)
    except OSError as exc:
        raise ActivationFailed(f"could not repoint {pointer} to {release_root} ({exc})") from None

    problems = verify(commit, install_root=install_root, owner_uid=owner_uid)
    if not problems:
        print_func(f"switchyard: {pointer} now resolves to {release_root} for {commit}")
        return result

    # It landed and it is wrong. Put back exactly what was recorded -- but only
    # if the pointer is still the one this wrote, because a concurrent
    # activation that succeeded after this one must not be undone by this
    # one's failure.
    result.notes.extend(problems)
    landed = read_pointer(install_root)
    if landed != release_root:
        raise ActivationFailed(
            f"{commit} did not verify ({'; '.join(problems)}) and {pointer} has since been "
            f"changed to {landed or 'nothing'} by something else, so it was left alone. "
            f"The previous target is recorded in {result.record_path}"
        )
    if not previous_target:
        raise ActivationFailed(
            f"{commit} did not verify ({'; '.join(problems)}) and there was no previous "
            f"release to return to. {pointer} still points at {release_root}"
        )
    try:
        _swap_pointer(install_root, previous_target)
    except OSError as exc:
        raise ActivationFailed(
            f"{commit} did not verify ({'; '.join(problems)}) AND the rollback to "
            f"{previous_target} failed ({exc}). {pointer} points at {release_root}; "
            f"the way back is recorded in {result.record_path}"
        ) from None
    result.rolled_back = True
    raise ActivationFailed(
        f"{commit} did not verify ({'; '.join(problems)}); {pointer} was put back to "
        f"{previous_target}"
    )


def verify(
    commit: str,
    *,
    install_root: Path = DEFAULT_INSTALL_ROOT,
    owner_uid: int | None = None,
) -> list[str]:
    """Read the pointer back and check what is at the other end of it.

    Deliberately not "did the syscall return 0". The question an operator has
    afterwards is whether this host is now running that commit, and the only
    thing that answers it is the marker inside the directory `current` actually
    resolves to.
    """
    problems: list[str] = []
    pointer = pointer_path(install_root)
    expected = str(install_root / RELEASES_NAME / commit)
    landed = read_pointer(install_root)
    if not landed:
        problems.append(f"{pointer} is not a symbolic link after the swap")
        return problems
    if landed != expected:
        problems.append(f"{pointer} points at {landed}, not {expected}")
        return problems
    recorded = _release_marker_commit(landed)
    if recorded != commit:
        problems.append(
            f"the release at {landed} records commit {recorded or 'nothing'}, not {commit}"
        )
    try:
        info = os.stat(pointer)
    except OSError as exc:
        problems.append(f"{pointer} cannot be resolved after the swap ({exc})")
        return problems
    if not stat.S_ISDIR(info.st_mode):
        problems.append(f"{pointer} does not resolve to a directory")
    required = 0 if owner_uid is None else owner_uid
    if info.st_uid != required:
        problems.append(
            f"{pointer} resolves to something owned by uid {info.st_uid} rather than "
            f"uid {required}"
        )
    return problems


def recorded_rollback(install_root: Path = DEFAULT_INSTALL_ROOT) -> dict:
    """The last recorded previous target, for an operator or a later rollback."""
    try:
        record = json.loads(rollback_record_path(install_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(record, dict) or record.get("schema") != ACTIVATION_SCHEMA:
        return {}
    return record


def rollback(
    *,
    install_root: Path = DEFAULT_INSTALL_ROOT,
    owner_uid: int | None = None,
    print_func: Callable[[str], None] = print,
) -> Activation:
    """Return `current` to the target recorded by the last activation."""
    record = recorded_rollback(install_root)
    previous_target = str(record.get("previous_target") or "")
    if not previous_target:
        raise ActivationFailed(
            f"no previous release is recorded in {rollback_record_path(install_root)}, "
            "so there is nothing to return to"
        )
    previous_commit = _release_marker_commit(previous_target)
    if not previous_commit:
        raise ActivationFailed(
            f"the recorded previous release {previous_target} carries no release marker, "
            "so it cannot be confirmed to be the release it claims to be"
        )
    return activate(
        previous_commit,
        install_root=install_root,
        owner_uid=owner_uid,
        print_func=print_func,
    )
