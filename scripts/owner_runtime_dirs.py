#!/usr/bin/env python3
"""Directories in an owner's own runtime directory, made by that owner, once root has proven it is theirs (SYRD-655).

Root makes what a tenant can reach through `scripts/tenant_paths.py`, which
stages each new directory in a directory only root can change, on the
destination's filesystem, and moves it into place (SYRD-605). The owner's
runtime directory, `/run/user/<uid>`, is a tmpfs of its own, owned by the
owner, mode 0700: root has nowhere there to stage, and refused --

    tenant_paths: refused: /run/user/1018/pie-ticket-board cannot be made without a race

-- on PIE's `resume-provision` and on every headless host's role panes
(SYRD-661). Nothing made there needs root: the directory is the owner's alone
and what goes in it is the owner's. So root proves the root is exactly the
owner's own runtime directory, by descriptor from `/` without following
anything, and a child that has become the owner makes the directories with
`tenant_paths`' own walk, staged in the runtime directory itself -- which only
the owner can change. A link, a swap, or something appearing under a name is
refused as it always is.

As a program it is root's helper, run as `/usr/bin/python3 -I <release>/
scripts/owner_runtime_dirs.py`, with nothing of the tenant's on the import
path: it loads `tenant_paths.py` from beside itself in root's release.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
from pathlib import Path
from typing import Callable, Sequence

sys.dont_write_bytecode = True

if __package__:
    from scripts import tenant_paths
else:  # root's helper: the release's own tenant_paths, by path, standard library only
    import importlib.util

    _spec = importlib.util.spec_from_file_location("tenant_paths", Path(__file__).resolve().with_name("tenant_paths.py"))
    tenant_paths = importlib.util.module_from_spec(_spec)
    sys.modules["tenant_paths"] = tenant_paths  # its dataclasses look their module up while it loads
    _spec.loader.exec_module(tenant_paths)

Refused = tenant_paths.Refused
#: Where logind makes each account's own runtime directory, `/run/user/<uid>`: that account's alone, mode 0700.
RUNTIME_PARENT = ("run", "user")


def helper_argv(home: str, owner: str, mode: int, paths: Sequence[str]) -> list[str]:
    """This helper as root's argv, from the release this code runs from."""
    return [sys.executable or "/usr/bin/python3", "-I", str(Path(__file__).resolve()), "--root", home,
            "--mode", f"{mode:04o}", "--owner", owner, *(str(path) for path in paths)]


def verify_runtime_root(home: str, uid: int) -> None:
    """That `home` is exactly `uid`'s own runtime directory, or Refused.

    `/run/user/<uid>`, walked from `/` by descriptor without following
    anything: `/run` and `/run/user` real directories only root can change,
    and `<uid>` a real directory owned by that uid, mode exactly 0700. Such a
    directory has no entry anybody but its owner can make, rename or remove.
    """
    if home != "/" + "/".join((*RUNTIME_PARENT, str(uid))):
        raise Refused(home, f"is not /{'/'.join(RUNTIME_PARENT)}/{uid}, the owner's own runtime directory")
    fd = os.open("/", tenant_paths.DIRECTORY)
    current = ""
    try:
        for part in (*RUNTIME_PARENT, str(uid)):
            current = f"{current}/{part}"
            try:
                status = os.stat(part, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                raise Refused(current, "does not exist") from None
            if not stat.S_ISDIR(status.st_mode):
                raise Refused(current, "is not a directory; root does not follow it")
            child = tenant_paths._openat(part, tenant_paths.DIRECTORY, fd)
            opened = os.fstat(child)
            os.close(fd)
            fd = child
            if (opened.st_ino, opened.st_dev) != (status.st_ino, status.st_dev):
                raise Refused(current, "was replaced while root was walking to it")
            if part != str(uid):
                if opened.st_uid != 0 or stat.S_IMODE(opened.st_mode) & 0o022:
                    raise Refused(current, "is not a directory only root can change")
            elif opened.st_uid != uid or stat.S_IMODE(opened.st_mode) != 0o700:
                raise Refused(current, f"is not uid {uid}'s own runtime directory (owner {opened.st_uid}, mode "
                                       f"{stat.S_IMODE(opened.st_mode):04o})")
    finally:
        os.close(fd)


def ensure_runtime_dirs(home: str, paths: Sequence[str], mode: int, owner: str, *, preview: bool = False,
                        report: Callable[[str], None] = lambda _line: None) -> None:
    """`install -d -m MODE PATH...` in `owner`'s own runtime directory `home`, made by `owner`; Refused otherwise."""
    uid, gid = tenant_paths.uid_of(owner), tenant_paths.login_gid(owner)
    verify_runtime_root(home, uid)

    def make() -> None:
        root = tenant_paths.Root(home, preview=preview, report=report, stage_in_home=True)
        try:
            tenant_paths.ensure_dirs(root, paths, mode, uid, gid)
        finally:
            root.close()

    if os.geteuid() == uid:
        make()
    else:
        _as_account(uid, gid, make)


def _as_account(uid: int, gid: int, action: Callable[[], None]) -> None:
    """Run `action` in a child that has become `uid`:`gid` alone; its refusal or failure is raised here."""
    reader, writer = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the child; its outcome is read by the parent
        os.close(reader)
        code, said = 0, b""
        try:
            os.setgroups([gid])
            os.setresgid(gid, gid, gid)
            os.setresuid(uid, uid, uid)
            if os.getresuid() != (uid, uid, uid) or os.getresgid() != (gid, gid, gid):
                raise OSError(errno.EPERM, "could not become the owner")
            os.chdir("/")
            action()
        except Refused as refusal:
            code, said = tenant_paths.REFUSED_EXIT, f"R\0{refusal.path}\0{refusal.reason}".encode()
        except BaseException as exc:  # noqa: BLE001 - every outcome is the parent's to report
            code, said = tenant_paths.FAILED_EXIT, f"F\0{exc}".encode()
        try:
            os.write(writer, said)
        finally:
            os._exit(code)
    os.close(writer)
    chunks = []
    while chunk := os.read(reader, 65536):
        chunks.append(chunk)
    os.close(reader)
    _, status = os.waitpid(pid, 0)
    said = b"".join(chunks).decode("utf-8", "replace").split("\0")
    if said[0] == "R" and len(said) == 3:
        raise Refused(said[1], said[2])
    if os.waitstatus_to_exitcode(status) != 0:
        raise OSError(errno.EIO, said[1] if len(said) > 1 else f"the owner's child exited {status}")


def main(argv: Sequence[str]) -> int:
    """`--root HOME --mode MODE --owner OWNER PATH...`: 0 made, 3 refused, 4 failed, as `tenant_paths.py` exits."""
    import argparse

    parser = argparse.ArgumentParser(prog="owner_runtime_dirs.py", description=__doc__.splitlines()[0])
    parser.add_argument("--root", required=True)
    parser.add_argument("--mode", type=tenant_paths._mode, required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args(list(argv))
    try:
        ensure_runtime_dirs(args.root, args.paths, args.mode, args.owner,
                            report=lambda line: print(f"tenant_paths: {line}"))
    except Refused as refusal:
        print(f"tenant_paths: refused: {refusal.path} {refusal.reason}. Nothing further was changed.", file=sys.stderr)
        return tenant_paths.REFUSED_EXIT
    except OSError as exc:
        print(f"tenant_paths: failed: {exc}. Nothing further was changed.", file=sys.stderr)
        return tenant_paths.FAILED_EXIT
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
