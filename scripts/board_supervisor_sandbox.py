#!/usr/bin/env python3
"""The board supervisor's exec stage: become one tenant's board, then exec it (SYRD-586).

The supervisor runs this as root, once per child start, with a launch document
on stdin. It applies the protections the tenant's unit names, in the order
systemd applies them, and then replaces itself with the board, so the pid the
supervisor watches IS the board:

1. its own mount namespace, with propagation from the host but none back;
2. `ProtectSystem=` and `ProtectHome=read-only`: every mount under the
   protected trees remounted read-only, keeping each mount's other flags;
3. `ReadWritePaths=`: each named path bound over itself writable again -- so
   a board writes its own tenant's asset and frame directories, and another
   tenant's only through what file permissions allow, which for a board that
   shares the `boardsvc` uid is what the read-only home is for;
4. the unit's groups, gid and uid, in that order, with no way back;
5. `NoNewPrivileges=`, then a parent-death signal, so a board never outlives
   the supervisor that is accountable for it;
6. the working directory, as the board's user, and the umask systemd uses;
7. exec, with exactly the environment the supervisor computed.

A step that fails exits with systemd's own code for it (226 namespace, 216
group, 217 user, 227 no-new-privileges, 200 working directory, 203 exec), and
says why on stderr, which the supervisor records against the tenant.
"""

from __future__ import annotations

import ctypes
import errno as errno_module
import json
import os
import signal
import sys

CLONE_NEWNS = 0x00020000
MS_RDONLY = 0x1
MS_NOSUID = 0x2
MS_NODEV = 0x4
MS_NOEXEC = 0x8
MS_REMOUNT = 0x20
MS_NOSYMFOLLOW = 0x100
MS_NOATIME = 0x400
MS_NODIRATIME = 0x800
MS_BIND = 0x1000
MS_REC = 0x4000
MS_SLAVE = 0x80000
MS_RELATIME = 0x200000
MS_STRICTATIME = 0x1000000
PR_SET_PDEATHSIG = 1
PR_SET_NO_NEW_PRIVS = 38
#: Per-mount options that a read-only remount must carry over.
MOUNT_OPTION_FLAGS = {
    "ro": MS_RDONLY, "nosuid": MS_NOSUID, "nodev": MS_NODEV, "noexec": MS_NOEXEC,
    "noatime": MS_NOATIME, "nodiratime": MS_NODIRATIME, "relatime": MS_RELATIME,
    "strictatime": MS_STRICTATIME, "nosymfollow": MS_NOSYMFOLLOW,
}
EXIT_CHDIR, EXIT_EXEC, EXIT_STDIN = 200, 203, 208
EXIT_GROUP, EXIT_USER, EXIT_NAMESPACE, EXIT_NO_NEW_PRIVILEGES = 216, 217, 226, 227
SYSTEM_TREES = {"yes": ("/usr", "/boot", "/efi"), "full": ("/usr", "/boot", "/efi", "/etc")}
HOME_TREES = ("/home", "/root", "/run/user")


class StageError(Exception):
    def __init__(self, code: int, message: str, errno: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.errno = errno


_libc = ctypes.CDLL(None, use_errno=True)
_libc.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_void_p]
_libc.unshare.argtypes = [ctypes.c_int]
_libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]


def _mount(source: str | None, target: str, flags: int) -> None:
    if _libc.mount(source.encode() if source else None, target.encode(), None, flags, None) != 0:
        errno = ctypes.get_errno()
        raise StageError(EXIT_NAMESPACE, f"mount {target} (flags {flags:#x}) failed: {os.strerror(errno)}", errno)


def _unescape(field: str) -> str:
    out, index = [], 0
    while index < len(field):
        if field[index] == "\\" and field[index + 1: index + 4].isdigit():
            out.append(chr(int(field[index + 1: index + 4], 8)))
            index += 4
        else:
            out.append(field[index])
            index += 1
    return "".join(out)


def mounts() -> list[tuple[str, int, str]]:
    """(mount point, per-mount flags, fstype) for every mount, in mountinfo order (later is on top)."""
    found = []
    with open("/proc/self/mountinfo", encoding="utf-8") as handle:
        for line in handle:
            fields = line.split()
            separator = fields.index("-")
            flags = 0
            for option in fields[5].split(","):
                flags |= MOUNT_OPTION_FLAGS.get(option, 0)
            found.append((_unescape(fields[4]), flags, fields[separator + 1]))
    return found


def _under(path: str, tree: str) -> bool:
    return path == tree or path.startswith(tree.rstrip("/") + "/")


def _top_flags(path: str) -> int:
    flags = None
    for mount_point, mount_flags, _fstype in mounts():
        if mount_point == path:
            flags = mount_flags
    if flags is None:
        raise StageError(EXIT_NAMESPACE, f"{path} is not a mount point after binding it")
    return flags


def protect(protect_system: str, protect_home: str, read_write_paths: list[str]) -> list[str]:
    """Make the protected trees read-only, then the named paths writable. Returns what was made read-only."""
    trees = list(SYSTEM_TREES.get(protect_system, ()))
    if protect_home == "read-only":
        trees.extend(HOME_TREES)
    trees = [tree for tree in trees if os.path.isdir(tree) and not os.path.islink(tree)]
    for tree in trees:
        _mount(tree, tree, MS_BIND | MS_REC)
    protected = []
    for mount_point, flags, fstype in mounts():
        if not any(_under(mount_point, tree) for tree in trees):
            continue
        if fstype == "autofs":
            # Remounting an automount point would trigger it; what it mounts is
            # remounted on its own line once present.
            continue
        try:
            _mount(None, mount_point, MS_BIND | MS_REMOUNT | MS_RDONLY | flags)
        except StageError as exc:
            # A FUSE mount without allow_other refuses everyone but its owner,
            # root included -- and so refuses the board too, which is what a
            # read-only remount was for. A mount whose path no longer exists is
            # covered by another mount, and the board cannot reach it. Any
            # other refusal fails the start.
            if fstype.startswith("fuse") or exc.errno == errno_module.ENOENT:
                continue
            raise
        protected.append(mount_point)
    for path in read_write_paths:
        if not os.path.isdir(path) and not os.path.isfile(path):
            raise StageError(EXIT_NAMESPACE, f"ReadWritePaths= entry {path} does not exist")
        # Stricter than systemd, which follows links here: the owner controls
        # these paths, and a link would aim this board's write access elsewhere.
        if os.path.realpath(path) != os.path.normpath(path):
            raise StageError(EXIT_NAMESPACE, f"ReadWritePaths= entry {path} resolves through a link to "
                                             f"{os.path.realpath(path)}")
        _mount(path, path, MS_BIND | MS_REC)
        _mount(None, path, MS_BIND | MS_REMOUNT | (_top_flags(path) & ~MS_RDONLY))
    return protected


def become(document: dict) -> None:
    """Steps 1-6. Raises StageError; on success the caller execs."""
    if _libc.unshare(CLONE_NEWNS) != 0:
        raise StageError(EXIT_NAMESPACE, f"unshare(CLONE_NEWNS) failed: {os.strerror(ctypes.get_errno())}")
    _mount(None, "/", MS_REC | MS_SLAVE)
    protect(document["protect_system"], document["protect_home"], list(document["read_write_paths"]))
    try:
        os.setgroups([int(gid) for gid in document["groups"]])
        os.setresgid(int(document["gid"]), int(document["gid"]), int(document["gid"]))
    except OSError as exc:
        raise StageError(EXIT_GROUP, f"cannot take gid {document['gid']} and groups {document['groups']}: {exc}") from exc
    try:
        os.setresuid(int(document["uid"]), int(document["uid"]), int(document["uid"]))
    except OSError as exc:
        raise StageError(EXIT_USER, f"cannot take uid {document['uid']}: {exc}") from exc
    if os.getuid() == 0 or os.geteuid() == 0:
        raise StageError(EXIT_USER, "still root after dropping to the board's user")
    if document.get("no_new_privileges") and _libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise StageError(EXIT_NO_NEW_PRIVILEGES, f"no_new_privs failed: {os.strerror(ctypes.get_errno())}")
    # After the uid change: a credential change clears the parent-death signal.
    _libc.prctl(PR_SET_PDEATHSIG, int(signal.SIGTERM), 0, 0, 0)
    if os.getppid() != int(document["supervisor_pid"]):
        raise StageError(EXIT_USER, "the supervisor exited while this board was starting")
    try:
        os.chdir(document["cwd"])
    except OSError as exc:
        raise StageError(EXIT_CHDIR, f"cannot enter WorkingDirectory {document['cwd']}: {exc}") from exc
    os.umask(0o022)


def main() -> int:
    try:
        document = json.loads(sys.stdin.buffer.read())
        null = os.open(os.devnull, os.O_RDONLY)
        os.dup2(null, 0)
        os.close(null)
    except (OSError, ValueError) as exc:
        print(f"board-supervisor exec: no launch document: {exc}", file=sys.stderr, flush=True)
        return EXIT_STDIN
    try:
        become(document)
        argv = [str(word) for word in document["argv"]]
        env = {str(key): str(value) for key, value in document["env"].items()}
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            os.execve(argv[0], argv, env)
        except OSError as exc:
            raise StageError(EXIT_EXEC, f"cannot execute {argv[0]}: {exc}") from exc
    except StageError as exc:
        print(f"board-supervisor exec: {document.get('project', '?')}: {exc}", file=sys.stderr, flush=True)
        return exc.code
    return EXIT_EXEC  # pragma: no cover - execve does not return


if __name__ == "__main__":
    raise SystemExit(main())
