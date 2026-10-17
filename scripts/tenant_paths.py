#!/usr/bin/env python3
"""Root's changes to paths a tenant controls, made on descriptors that never follow a link.

The operator packet and `repair-boundary` run as root over directories inside a
tenant's home and its role accounts' homes: they create them, set their mode
and owner, and add or remove ACL entries, some recursively. As shell lines
(`install -d -o`, `setfacl -R`, `find -exec chmod`, `chown -R`) every one of
them acted on whatever a link at that path pointed to, so a tenant who planted
a link had root re-own, re-mode or grant a tree it chose (SYRD-604, SYRD-605).
A `[ -L ]` test in front of them would not help: the tenant can swap the path
between the test and the command.

So every such change goes through here. The principal's home is opened once
as the trusted root. Every component below it is opened relative to the one
above with `O_NOFOLLOW`, so a link is refused rather than followed, and a
component swapped after it was opened changes nothing this holds. A component
on another filesystem is refused as a mount crossing. Mode, owner and ACL are
then set on the opened objects -- `fchmod`, `fchown`, and the ACL xattrs on the
descriptor -- never by path. A recursive change walks by descriptor too, leaves
a link it finds where it was (never followed, never changed), and refuses an
entry that turns into a link or another object between listing and opening.

`--preview` resolves exactly the same paths and raises exactly the same
refusals, and changes nothing. A refusal exits 3 with the path and the reason
on stderr; the operator packet runs under `set -e` inside
`switchyard-record-rollout`, and `repair-boundary` journals its own attempt, so
either way the refusal is recorded.

Standard library only: the packet runs this as `/usr/bin/python3 -I <release>/
scripts/tenant_paths.py`, from the release root installed, with nothing of the
tenant's on the import path.
"""

from __future__ import annotations

import argparse
import errno
import grp
import os
import pwd
import stat
import struct
import sys
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Callable, Iterator, Sequence

DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
#: A regular file is opened only to change it. Non-blocking and no controlling
#: terminal, so whatever replaced it cannot stall or capture root.
ANY = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC
REFUSED_EXIT = 3
FAILED_EXIT = 4

ACCESS, DEFAULT = "system.posix_acl_access", "system.posix_acl_default"
USER_OBJ, USER, GROUP_OBJ, GROUP, MASK, OTHER = 0x01, 0x02, 0x04, 0x08, 0x10, 0x20
UNDEFINED = 0xFFFFFFFF
GROUP_CLASS = (USER, GROUP_OBJ, GROUP)


class Refused(Exception):
    """A change root will not make, and why."""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path} {reason}")
        self.path, self.reason = path, reason


def _openat(name: str, flags: int, dir_fd: int) -> int:
    """The one place a component is opened, so a test can change the path just before it."""
    return os.open(name, flags, dir_fd=dir_fd)


# -- identities -------------------------------------------------------------


def uid_of(name: str) -> int:
    try:
        return pwd.getpwnam(name).pw_uid
    except KeyError:
        if name.isdigit():
            return int(name)
        raise SystemExit(f"tenant_paths: {name} is not an account on this host") from None


def gid_of(name: str) -> int | None:
    try:
        return grp.getgrnam(name).gr_gid
    except KeyError:
        return int(name) if name.isdigit() else None


def login_gid(name: str) -> int:
    try:
        return pwd.getpwnam(name).pw_gid
    except KeyError:
        if name.isdigit():
            return int(name)
        raise SystemExit(f"tenant_paths: {name} is not an account on this host") from None


# -- ACLs -------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    default: bool
    tag: int
    qualifier: int
    perms: str  # "" for a removal

    def bits(self, *, directory: bool, executable: bool) -> int:
        value = 0
        for letter in self.perms:
            value |= {"r": 4, "w": 2, "x": 1, "-": 0}.get(letter, 0)
            if letter == "X" and (directory or executable):
                value |= 1
        return value


def parse_entries(spec: str, *, removal: bool) -> list[Entry]:
    """`setfacl`'s entry syntax: `[d:]u:name:rwX`, `u::rwx`, `g:name:--x`, `m::r-x`, `o::---`."""
    entries = []
    for item in spec.split(","):
        fields = item.strip().split(":")
        default = fields[0] in ("d", "default")
        if default:
            fields = fields[1:]
        if len(fields) not in ((2, 3) if removal else (3,)):
            raise SystemExit(f"tenant_paths: {item!r} is not an ACL entry")
        kind, qualifier = fields[0], fields[1]
        perms = "" if removal else fields[2]
        if perms and set(perms) - set("rwxX-"):
            raise SystemExit(f"tenant_paths: {item!r} has permissions this does not grant")
        if kind in ("u", "user"):
            tag, ident = (USER, uid_of(qualifier)) if qualifier else (USER_OBJ, UNDEFINED)
        elif kind in ("g", "group"):
            if qualifier:
                gid = gid_of(qualifier)
                if gid is None:
                    raise SystemExit(f"tenant_paths: {qualifier} is not a group on this host")
                tag, ident = GROUP, gid
            else:
                tag, ident = GROUP_OBJ, UNDEFINED
        elif kind in ("m", "mask"):
            tag, ident = MASK, UNDEFINED
        elif kind in ("o", "other"):
            tag, ident = OTHER, UNDEFINED
        else:
            raise SystemExit(f"tenant_paths: {item!r} is not an ACL entry")
        if removal and tag not in (USER, GROUP):
            raise SystemExit(f"tenant_paths: only a named entry can be removed, not {item!r}")
        entries.append(Entry(default, tag, ident, perms))
    return entries


def decode(raw: bytes) -> dict[tuple[int, int], int]:
    version, = struct.unpack_from("<I", raw, 0)
    if version != 2:
        raise ValueError(f"ACL version {version}")
    return {(tag, ident): perm for tag, perm, ident in struct.iter_unpack("<HHI", raw[4:])}


def encode(entries: dict[tuple[int, int], int]) -> bytes:
    body = b"".join(struct.pack("<HHI", tag, perm, ident) for (tag, ident), perm in sorted(entries.items()))
    return struct.pack("<I", 2) + body


def from_mode(mode: int) -> dict[tuple[int, int], int]:
    return {(USER_OBJ, UNDEFINED): mode >> 6 & 7, (GROUP_OBJ, UNDEFINED): mode >> 3 & 7, (OTHER, UNDEFINED): mode & 7}


def _read(fd: int, name: str) -> dict[tuple[int, int], int] | None:
    try:
        return decode(os.getxattr(fd, name))
    except OSError as exc:
        if exc.errno in (errno.ENODATA, errno.EOPNOTSUPP):
            return None
        raise


def edited(fd: int, status: os.stat_result, modify: Sequence[Entry], remove: Sequence[Entry]
           ) -> list[tuple[str, bytes]]:
    """The ACL xattrs `setfacl -m`/`-x` would write on this object, or nothing if they are already so.

    The same rules as setfacl: `X` is execute for a directory or for an object
    some entry already lets execute; a default ACL that does not exist yet
    starts from the access ACL's base entries; and the mask is recomputed as
    the union of the group class whenever the ACL is extended or already had
    one, unless this change sets it.
    """
    directory = stat.S_ISDIR(status.st_mode)
    access = _read(fd, ACCESS) or from_mode(stat.S_IMODE(status.st_mode))
    writes = []
    for name, is_default in ((ACCESS, False), (DEFAULT, True)):
        adds = [e for e in modify if e.default is is_default]
        drops = [e for e in remove if e.default is is_default]
        if not adds and not drops:
            continue
        if is_default and not directory:
            continue  # only a directory carries a default ACL; setfacl -R passes files by
        current = access if not is_default else _read(fd, DEFAULT)
        created = current is None
        if created:
            if not adds:
                continue
            current = {key: value for key, value in access.items() if key[0] in (USER_OBJ, GROUP_OBJ, OTHER)}
        new = dict(current)
        executable = any(perm & 1 for (tag, _i), perm in access.items() if tag != MASK)
        explicit_mask = False
        for entry in adds:
            new[(entry.tag, entry.qualifier)] = entry.bits(directory=directory, executable=executable)
            explicit_mask |= entry.tag == MASK
        for entry in drops:
            new.pop((entry.tag, entry.qualifier), None)
        extended = any(tag in (USER, GROUP) for tag, _i in new)
        if (extended or (MASK, UNDEFINED) in current) and not explicit_mask:
            union = 0
            for (tag, _i), perm in new.items():
                if tag in GROUP_CLASS:
                    union |= perm
            new[(MASK, UNDEFINED)] = union
        if new != current or created:
            writes.append((name, encode(new)))
    return writes


# -- walking ----------------------------------------------------------------


class Root:
    """A principal's home, opened once, and everything reached from it by descriptor."""

    def __init__(self, home: str, *, preview: bool, report: Callable[[str], None],
                 planned: set[str] | None = None) -> None:
        if not home.startswith("/") or str(PurePosixPath(home)) != home or home == "/":
            raise SystemExit(f"tenant_paths: {home!r} is not a normalised absolute home")
        self.home, self.preview, self.report = home, preview, report
        #: What earlier previewed steps would have made, so a later step on it
        #: is previewed as the apply would run it rather than refused as missing.
        self.planned = planned if planned is not None else set()
        self.fd: int | None = None
        self.parent_fd: int | None = None
        self.dev = -1
        self._open_home()

    def _open_home(self) -> None:
        """Walk to the home from `/`, by descriptor, refusing any link but one root itself made.

        A link above the home is followed only when root owns it -- `/home` as
        a link to `/var/home` is the host's choice. One owned by anybody else
        is somebody steering root, and the home itself is never a link.
        """
        parts = PurePosixPath(self.home).parts[1:]
        fd = os.open("/", DIRECTORY)
        current = ""
        try:
            for index, part in enumerate(parts):
                current = f"{current}/{part}"
                last = index == len(parts) - 1
                try:
                    status = os.stat(part, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    if not last:
                        raise Refused(current, "does not exist, so neither does anything below it") from None
                    self.parent_fd, fd = fd, -1
                    return
                if stat.S_ISLNK(status.st_mode):
                    if last or status.st_uid != 0:
                        raise Refused(current, "is a symbolic link; root does not follow it")
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=fd)
                else:
                    try:
                        child = _openat(part, DIRECTORY, fd)
                    except OSError as exc:
                        raise self._classify(exc, current) from None
                    opened = os.fstat(child)
                    if (opened.st_ino, opened.st_dev) != (status.st_ino, status.st_dev):
                        os.close(child)
                        raise Refused(current, "was replaced while root was walking to it")
                if last:
                    self.parent_fd, self.fd = fd, child
                    fd = -1
                    self.dev = os.fstat(child).st_dev
                    return
                os.close(fd)
                fd = child
        finally:
            if fd >= 0:
                os.close(fd)

    def close(self) -> None:
        for fd in (self.fd, self.parent_fd):
            if fd is not None:
                os.close(fd)

    def relative(self, path: str) -> tuple[str, ...]:
        if str(PurePosixPath(path)) != path or not path.startswith("/"):
            raise SystemExit(f"tenant_paths: {path!r} is not a normalised absolute path")
        try:
            parts = PurePosixPath(path).relative_to(PurePosixPath(self.home)).parts
        except ValueError:
            raise SystemExit(f"tenant_paths: {path} is not inside {self.home}") from None
        if any(part in (".", "..") for part in parts):
            raise SystemExit(f"tenant_paths: {path!r} is not a normalised absolute path")
        return parts

    def _classify(self, exc: OSError, display: str) -> Exception:
        if exc.errno == errno.ELOOP:
            return Refused(display, "is a symbolic link; root does not follow it")
        if exc.errno == errno.ENOTDIR:
            return Refused(display, "is not a directory")
        return exc

    def open_directory(self, parent: int, name: str, display: str, expect: os.stat_result | None = None) -> int:
        try:
            fd = _openat(name, DIRECTORY, parent)
        except FileNotFoundError:
            # Every caller looked first, or has just made it: gone by now is a
            # change made while root was walking, not a missing path.
            raise Refused(display, "vanished: it changed while root was walking it") from None
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                # O_NOFOLLOW with O_DIRECTORY says ENOTDIR for a link; ask the
                # held parent which it is, so the refusal names it.
                try:
                    if stat.S_ISLNK(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode):
                        exc = OSError(errno.ELOOP, os.strerror(errno.ELOOP))
                except OSError:
                    pass
            raise self._classify(exc, display) from None
        status = os.fstat(fd)
        if status.st_dev != self.dev:
            os.close(fd)
            raise Refused(display, "is on another filesystem than the home; root does not cross a mount")
        if expect is not None and (status.st_ino, status.st_dev) != (expect.st_ino, expect.st_dev):
            os.close(fd)
            raise Refused(display, "was replaced while root was walking it")
        return fd

    def _observe(self, parent: int, name: str) -> os.stat_result | None:
        try:
            return os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def _made(self, parent: int, name: str, display: str, mode: int) -> int:
        """Make `name` and open what was made -- and only that.

        A directory root has just made is root's until root hands it over. If
        what opens under that name is anybody else's, something was put there
        between the `mkdir` and the open, and it is refused. A directory that
        appeared before root could make it is opened as an existing one: bound
        to what is observed there (audit of 42048b91).
        """
        try:
            os.mkdir(name, mode, dir_fd=parent)
        except FileExistsError:
            observed = self._observe(parent, name)
            if observed is None:
                raise Refused(display, "appeared and vanished while root was making it") from None
            return self.open_directory(parent, name, display, observed)
        except OSError as exc:
            raise Refused(display, f"could not be made ({exc.strerror}): what holds it changed while root "
                                   "was walking it") from None
        child = self.open_directory(parent, name, display)
        if os.fstat(child).st_uid != os.geteuid():
            os.close(child)
            raise Refused(display, "is not the directory root just made; something replaced it")
        return child

    def walk(self, parts: Sequence[str], *, create: tuple[int, int, int] | None = None) -> int | None:
        """A descriptor on the directory at `parts`, or None when it is missing (or, previewing, would be made).

        Every component is bound to what was observed there before it was
        opened: a different object under the name by the time of the open --
        a link, or another real directory -- is refused, not adopted.
        """
        if self.fd is None:
            if create is None or self.preview:
                if create is not None:
                    self.report(f"create {self.home}")
                    self.planned.add(self.home)
                return None
            self.fd = self._made(self.parent_fd, PurePosixPath(self.home).name, self.home, create[0])
            self.dev = os.fstat(self.fd).st_dev
            self.report(f"create {self.home}")
            self._own(self.fd, self.home, *create, quiet=True)
        fd = os.dup(self.fd)
        current = self.home
        try:
            for index, part in enumerate(parts):
                current = f"{current}/{part}"
                observed = self._observe(fd, part)
                if observed is not None:
                    child = self.open_directory(fd, part, current, observed)
                elif create is None or self.preview:
                    if create is not None:
                        for depth in range(index, len(parts)):
                            made = "/".join([self.home, *parts[: depth + 1]])
                            self.report(f"create {made}")
                            self.planned.add(made)
                    return None
                else:
                    child = self._made(fd, part, current, create[0])
                    self.report(f"create {current}")
                    self._own(child, current, *create, quiet=True)
                os.close(fd)
                fd = child
            result, fd = fd, -1
            return result
        finally:
            if fd >= 0:
                os.close(fd)

    def _own(self, fd: int, display: str, mode: int | None, uid: int | None, gid: int | None,
             quiet: bool = False) -> None:
        status = os.fstat(fd)
        changes = []
        if (uid is not None and status.st_uid != uid) or (gid is not None and status.st_gid != gid):
            changes.append(f"owner {status.st_uid}:{status.st_gid} -> "
                           f"{status.st_uid if uid is None else uid}:{status.st_gid if gid is None else gid}")
            if not self.preview:
                os.fchown(fd, -1 if uid is None else uid, -1 if gid is None else gid)
        if mode is not None and stat.S_IMODE(status.st_mode) != mode:
            changes.append(f"mode {stat.S_IMODE(status.st_mode):04o} -> {mode:04o}")
            if not self.preview:
                os.fchmod(fd, mode)
        if changes and not quiet:
            self.report(f"{display}: {', '.join(changes)}")

    def made_earlier(self, path: str) -> bool:
        """Previewing, whether an earlier step would have made this path (or what holds it)."""
        return self.preview and any(path == made or path.startswith(made + "/") for made in self.planned)

    def leaf(self, parts: Sequence[str]) -> tuple[int, int, str, os.stat_result] | None:
        """(parent descriptor, object descriptor, display, status) for an existing object, or None if missing."""
        display = "/".join([self.home, *parts]) if parts else self.home
        if not parts:
            if self.fd is None:
                return None
            return os.dup(self.fd), os.dup(self.fd), display, os.fstat(self.fd)
        parent = self.walk(parts[:-1])
        if parent is None:
            return None
        try:
            status = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            os.close(parent)
            return None
        fd = self.open_object(parent, parts[-1], display, status)
        return parent, fd, display, status

    def open_object(self, parent: int, name: str, display: str, status: os.stat_result) -> int:
        if stat.S_ISLNK(status.st_mode):
            raise Refused(display, "is a symbolic link; root does not follow it")
        if stat.S_ISDIR(status.st_mode):
            return self.open_directory(parent, name, display, status)
        if not stat.S_ISREG(status.st_mode):
            raise Refused(display, "is neither a regular file nor a directory")
        try:
            fd = _openat(name, ANY, parent)
        except OSError as exc:
            raise self._classify(exc, display) from None
        opened = os.fstat(fd)
        if (opened.st_ino, opened.st_dev) != (status.st_ino, status.st_dev) or not stat.S_ISREG(opened.st_mode):
            os.close(fd)
            raise Refused(display, "was replaced while root was opening it")
        if opened.st_dev != self.dev:
            os.close(fd)
            raise Refused(display, "is on another filesystem than the home; root does not cross a mount")
        return fd

    def tree(self, fd: int, display: str) -> Iterator[tuple[int, str, os.stat_result]]:
        """Every directory and regular file below an opened directory, by descriptor, links left alone."""
        stack = [(os.dup(fd), display)]
        while stack:
            directory, where = stack.pop()
            try:
                for name in sorted(os.listdir(directory)):
                    path = f"{where}/{name}"
                    try:
                        status = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if stat.S_ISLNK(status.st_mode):
                        self.report(f"leave {path}: a symbolic link, not followed")
                        continue
                    if not (stat.S_ISDIR(status.st_mode) or stat.S_ISREG(status.st_mode)):
                        continue
                    child = self.open_object(directory, name, path, status)
                    if stat.S_ISDIR(status.st_mode):
                        stack.append((os.dup(child), path))
                    try:
                        yield child, path, status
                    finally:
                        os.close(child)
            finally:
                os.close(directory)


# -- operations -------------------------------------------------------------


def ensure_dirs(root: Root, paths: Sequence[str], mode: int, uid: int, gid: int) -> None:
    """`install -d -m MODE -o U -g G PATH...`: each directory made or corrected, intermediates included."""
    for path in paths:
        fd = root.walk(root.relative(path), create=(mode, uid, gid))
        if fd is not None:
            try:
                root._own(fd, path, mode, uid, gid)
            finally:
                os.close(fd)


def set_attrs(root: Root, paths: Sequence[str], mode: int | None, uid: int | None, gid: int | None,
              if_exists: bool) -> None:
    """`chmod`/`chown` on existing objects."""
    for path in paths:
        found = root.leaf(root.relative(path))
        if found is None:
            if root.made_earlier(path):
                root.report(f"{path}: set once an earlier step has made it")
                continue
            if if_exists:
                continue
            raise Refused(path, "does not exist")
        parent, fd, display, _status = found
        try:
            root._own(fd, display, mode, uid, gid)
        finally:
            os.close(fd)
            os.close(parent)


def _acl_one(root: Root, fd: int, display: str, status: os.stat_result, modify, remove) -> None:
    writes = edited(fd, status, modify, remove)
    if writes:
        root.report(f"{display}: ACL {', '.join('default' if name == DEFAULT else 'access' for name, _ in writes)}")
        if not root.preview:
            for name, value in writes:
                os.setxattr(fd, name, value)


def set_acl(root: Root, paths: Sequence[str], modify: Sequence[Entry], remove: Sequence[Entry], *,
            recursive: bool, directories_only: bool, if_exists: bool) -> None:
    """`setfacl [-R] -m/-x ENTRIES PATH...`, and `find PATH -type d -exec setfacl ...`."""
    for path in paths:
        found = root.leaf(root.relative(path))
        if found is None:
            if root.made_earlier(path):
                root.report(f"{path}: ACL once an earlier step has made it")
                continue
            if if_exists:
                continue
            raise Refused(path, "does not exist")
        parent, fd, display, status = found
        try:
            if not directories_only or stat.S_ISDIR(status.st_mode):
                _acl_one(root, fd, display, status, modify, remove)
            if recursive and stat.S_ISDIR(status.st_mode):
                for child, child_path, child_status in root.tree(fd, display):
                    if directories_only and not stat.S_ISDIR(child_status.st_mode):
                        continue
                    _acl_one(root, child, child_path, child_status, modify, remove)
        finally:
            os.close(fd)
            os.close(parent)


def close_children(root: Root, path: str) -> None:
    """`find PATH -mindepth 1 -maxdepth 1 -type d -exec chmod o-rwx {} +`, for a directory that exists."""
    found = root.leaf(root.relative(path))
    if found is None:
        return  # nothing in it to close, now or once an earlier step has made it
    parent, fd, display, status = found
    try:
        if not stat.S_ISDIR(status.st_mode):
            raise Refused(display, "is not a directory")
        for name in sorted(os.listdir(fd)):
            child_path = f"{display}/{name}"
            try:
                child_status = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(child_status.st_mode):
                root.report(f"leave {child_path}: a symbolic link, not followed")
                continue
            if not stat.S_ISDIR(child_status.st_mode):
                continue
            child = root.open_directory(fd, name, child_path, child_status)
            try:
                mode = stat.S_IMODE(child_status.st_mode)
                root._own(child, child_path, mode & ~0o007, None, None)
            finally:
                os.close(child)
    finally:
        os.close(fd)
        os.close(parent)


def chown_tree(root: Root, path: str, uid: int, gid: int, if_exists: bool) -> None:
    """`chown -R U: PATH`: the tree's owner, links changed as links and never followed."""
    found = root.leaf(root.relative(path))
    if found is None:
        if if_exists or root.made_earlier(path):
            return
        raise Refused(path, "does not exist")
    parent, fd, display, status = found
    try:
        if not stat.S_ISDIR(status.st_mode):
            raise Refused(display, "is not a directory")
        root._own(fd, display, None, uid, gid)
        stack = [(os.dup(fd), display)]
        while stack:
            directory, where = stack.pop()
            try:
                for name in sorted(os.listdir(directory)):
                    child_path = f"{where}/{name}"
                    try:
                        child_status = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if child_status.st_dev != root.dev:
                        raise Refused(child_path, "is on another filesystem than the home; root does not cross a mount")
                    if not stat.S_ISDIR(child_status.st_mode):
                        # The object itself, whatever it is -- a link included --
                        # through the directory root holds, never by following.
                        if (child_status.st_uid, child_status.st_gid) != (uid, gid):
                            root.report(f"{child_path}: owner {child_status.st_uid}:{child_status.st_gid} -> {uid}:{gid}")
                            if not root.preview:
                                os.chown(name, uid, gid, dir_fd=directory, follow_symlinks=False)
                        continue
                    child = root.open_directory(directory, name, child_path, child_status)
                    root._own(child, child_path, None, uid, gid)
                    stack.append((child, child_path))
            finally:
                os.close(directory)
    finally:
        os.close(fd)
        os.close(parent)


def install_file(root: Root, source: str, path: str, mode: int, uid: int, gid: int) -> None:
    """`install -m MODE -o U -g G SOURCE PATH`: a new file renamed into place, over a link if one is there."""
    parts = root.relative(path)
    if not parts:
        raise SystemExit("tenant_paths: the home itself is not a file")
    with open(source, "rb") as handle:  # root's own staged file
        content = handle.read()
    parent = root.walk(parts[:-1])
    if parent is None:
        if root.made_earlier(str(PurePosixPath(path).parent)):
            root.report(f"install {path}")
            return
        raise Refused(str(PurePosixPath(path).parent), "does not exist")
    try:
        try:
            existing = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and stat.S_ISDIR(existing.st_mode):
            raise Refused(path, "is a directory")
        if existing is not None and stat.S_ISREG(existing.st_mode):
            fd = root.open_object(parent, parts[-1], path, existing)
            try:
                same = os.read(fd, len(content) + 1) == content
            finally:
                os.close(fd)
            if same and (existing.st_uid, existing.st_gid, stat.S_IMODE(existing.st_mode)) == (uid, gid, mode):
                return
        root.report(f"install {path}")
        if root.preview:
            return
        temporary = f".{parts[-1]}.switchyard-{os.getpid()}"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                     dir_fd=parent)
        try:
            os.fchown(fd, uid, gid)
            os.fchmod(fd, mode)
            os.write(fd, content)
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            os.unlink(temporary, dir_fd=parent)
            raise
        os.close(fd)
        os.rename(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
    finally:
        os.close(parent)


# -- command line -----------------------------------------------------------


def _mode(text: str) -> int:
    if not text or set(text) - set("01234567") or len(text) > 4:
        raise argparse.ArgumentTypeError(f"{text!r} is not an octal mode")
    return int(text, 8)


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="tenant_paths.py", description=__doc__.splitlines()[0])
    top.add_argument("--root", required=True, help="the principal's home, the only trusted starting point")
    top.add_argument("--preview", action="store_true", help="resolve and refuse exactly as an apply would; change nothing")
    commands = top.add_subparsers(dest="command", required=True)
    directory = commands.add_parser("dir", help="install -d -m MODE -o U -g G PATH...")
    directory.add_argument("--mode", type=_mode, required=True)
    directory.add_argument("--owner", required=True)
    directory.add_argument("--group", required=True)
    directory.add_argument("paths", nargs="+")
    attrs = commands.add_parser("attrs", help="chmod / chown existing objects")
    attrs.add_argument("--mode", type=_mode)
    attrs.add_argument("--owner")
    attrs.add_argument("--group")
    attrs.add_argument("--if-exists", action="store_true")
    attrs.add_argument("paths", nargs="+")
    acl = commands.add_parser("acl", help="setfacl [-R] -m/-x ENTRIES PATH...")
    change = acl.add_mutually_exclusive_group(required=True)
    change.add_argument("--modify")
    change.add_argument("--remove")
    acl.add_argument("--recursive", action="store_true")
    acl.add_argument("--directories-only", action="store_true")
    acl.add_argument("--if-exists", action="store_true")
    acl.add_argument("--if-group", default="", help="do nothing when this group does not exist")
    acl.add_argument("paths", nargs="+")
    children = commands.add_parser("close-children", help="clear 'other' on each directory directly inside PATH")
    children.add_argument("path")
    tree = commands.add_parser("chown-tree", help="chown -R OWNER: PATH")
    tree.add_argument("--owner", required=True)
    tree.add_argument("--if-exists", action="store_true")
    tree.add_argument("path")
    install = commands.add_parser("install", help="install -m MODE -o U -g G SOURCE PATH")
    install.add_argument("--mode", type=_mode, required=True)
    install.add_argument("--owner", required=True)
    install.add_argument("--group", required=True)
    install.add_argument("source")
    install.add_argument("path")
    return top


def run(argv: Sequence[str], *, report: Callable[[str], None] | None = None,
        refuse: Callable[[str], None] | None = None, preview: bool | None = None,
        planned: set[str] | None = None) -> int:
    """Run one command line; 0 done, 3 refused.

    In-process callers (`repair-boundary`) pass `report` and `refuse` to
    receive the lines, `preview` to override `--preview`, and one `planned`
    set across a sequence of previews so each sees what the earlier would make.
    """
    args = parser().parse_args(list(argv))
    previewing = args.preview if preview is None else preview
    lead = "would " if previewing else ""
    say = report or (lambda line: print(f"tenant_paths: {lead}{line}"))
    tell = refuse or (lambda line: print(f"tenant_paths: {line}", file=sys.stderr))
    if args.command == "acl" and args.if_group and gid_of(args.if_group) is None:
        return 0
    root: Root | None = None
    try:
        root = Root(args.root, preview=previewing, report=say, planned=planned)
        if args.command == "dir":
            ensure_dirs(root, args.paths, args.mode, uid_of(args.owner), gid_of_required(args.group))
        elif args.command == "attrs":
            set_attrs(root, args.paths, args.mode, None if args.owner is None else uid_of(args.owner),
                      None if args.group is None else gid_of_required(args.group), args.if_exists)
        elif args.command == "acl":
            entries = parse_entries(args.modify or args.remove, removal=args.remove is not None)
            set_acl(root, args.paths, entries if args.modify else [], entries if args.remove else [],
                    recursive=args.recursive, directories_only=args.directories_only, if_exists=args.if_exists)
        elif args.command == "close-children":
            close_children(root, args.path)
        elif args.command == "chown-tree":
            chown_tree(root, args.path, uid_of(args.owner), login_gid(args.owner), args.if_exists)
        elif args.command == "install":
            install_file(root, args.source, args.path, args.mode, uid_of(args.owner), gid_of_required(args.group))
    except Refused as refusal:
        tell(f"refused: {refusal.path} {refusal.reason}. Nothing further was changed.")
        return REFUSED_EXIT
    except OSError as exc:
        # Not a refusal this recognises, and still not something to push past.
        tell(f"failed: {exc}. Nothing further was changed.")
        return FAILED_EXIT
    finally:
        if root is not None:
            root.close()
    return 0


def gid_of_required(name: str) -> int:
    gid = gid_of(name)
    if gid is None:
        raise SystemExit(f"tenant_paths: {name} is not a group on this host")
    return gid


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
