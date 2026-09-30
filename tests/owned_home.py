"""Keep a test's account homes, runtime dirs and processes inside its own sandbox (SYRD-343).

The presentation-handoff and first-run suites ran the real bridge
`publish_handoff(caller=<me>)`, the real `complete_desktop_presentation` and the
real layout writer. Each resolves an account's home the way production must --
`pwd.getpwnam(user).pw_dir`, which a fresh $HOME does nothing against -- so the
suites wrote into this account's real home (a fixture viewer handoff over
`~/.local/state/switchyard/projects/syrd/`, a layout under `projects/testing/`),
read the real desktop user's home, created pane-state under the real
`/run/user/<uid>`, and depended on this host's installed display helper.

Production's lookups are right and are not changed. What changes is who answers
them during a test:

* `OwnedAccounts` answers every account lookup -- `pwd.getpwnam`, `getpwuid`,
  `getpwall`, on the one `pwd` module every caller shares -- with an account
  whose home is inside the case's sandbox and whose uid and gid are this
  process's, so the bridge's own `chown` works and nothing it writes can land in
  a real home. The current uid keeps its real name, so `current_user_name` is
  still true. The runtime directory for any uid is inside the sandbox too.
* `HostGuard` refuses, before the syscall, any write outside the temporary
  directory, any read inside a real account's home except this checkout, any
  process except `getfacl` on temporary paths and read-only `git` in this
  checkout, and any socket outside the temporary directory -- whatever this
  host would have allowed -- and records every attempt.
* `contained(case)` runs a case under both, in a fresh sandbox removed even when
  the case fails, and fails the case for anything the guard had to refuse.
"""

from __future__ import annotations

import builtins
import functools
import io
import os
import pwd
import shutil
import socket
import stat
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
TEMP = os.path.realpath(tempfile.gettempdir())
_REAL_PWD = {name: getattr(pwd, name) for name in ("getpwnam", "getpwuid", "getpwall")}
#: Real account homes a test may not read: every person's and service's, but
#: not system roots such as "/" that the whole tree lives under.
REAL_HOMES = sorted({
    os.path.realpath(entry.pw_dir)
    for entry in _REAL_PWD["getpwall"]()
    if entry.pw_dir.startswith(("/home/", "/root")) and os.path.realpath(entry.pw_dir) != "/"
})
ME = _REAL_PWD["getpwuid"](os.getuid()).pw_name


class Refused(AssertionError):
    """A test tried to reach past its own sandbox."""


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root + os.sep)


class OwnedAccounts:
    """Every account a test asks about has a home in `root`, owned by this process."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def home(self, user: str) -> Path:
        home = self.root / "home" / user
        home.mkdir(parents=True, exist_ok=True)
        return home

    def entry(self, user: str) -> Any:
        return SimpleNamespace(
            pw_name=user, pw_passwd="x", pw_uid=os.getuid(), pw_gid=os.getgid(),
            pw_gecos=user, pw_dir=str(self.home(user)), pw_shell="/bin/sh",
        )

    def __enter__(self) -> "OwnedAccounts":
        from scripts import team_launcher as launcher

        self._saved_pwd = {name: getattr(pwd, name) for name in _REAL_PWD}
        self._saved_runtime = launcher.runtime_dir_for_uid
        pwd.getpwnam = lambda user: self.entry(str(user))
        # Only this process's uid is known; it keeps its real name. Any other
        # uid is an account this host does not have, as far as a test can tell.
        pwd.getpwuid = lambda uid: self.entry(ME) if uid == os.getuid() else (_ for _ in ()).throw(
            KeyError(f"getpwuid(): uid not found: {uid}"))
        pwd.getpwall = lambda: [self.entry(ME)]
        launcher.runtime_dir_for_uid = lambda uid: self.root / "run" / "user" / str(uid)
        return self

    def __exit__(self, *_exc) -> None:
        from scripts import team_launcher as launcher

        for name, value in self._saved_pwd.items():
            setattr(pwd, name, value)
        launcher.runtime_dir_for_uid = self._saved_runtime


class HostGuard:
    """Refuse and record anything that would reach past the temporary directory."""

    WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

    def __init__(self) -> None:
        self.attempts: list[str] = []

    # -- the rules --------------------------------------------------------
    def _refuse(self, what: str):
        self.attempts.append(what)
        raise Refused(f"refused by the SYRD-343 containment guard: {what}")

    @staticmethod
    def _resolve(path: Any, dir_fd: int | None = None) -> str | None:
        if isinstance(path, int):
            return None
        text = os.fsdecode(path)
        if dir_fd is not None and not os.path.isabs(text):
            try:
                text = os.path.join(os.readlink(f"/proc/self/fd/{dir_fd}"), text)
            except OSError:
                pass
        return os.path.realpath(os.path.abspath(text))

    def _write(self, kind: str, path: Any, dir_fd: int | None = None) -> None:
        target = self._resolve(path, dir_fd)
        if target is None or _inside(target, TEMP) or target in ("/dev/null", "/dev/tty"):
            return
        self._refuse(f"{kind} {target}")

    def _read(self, path: Any, dir_fd: int | None = None) -> None:
        target = self._resolve(path, dir_fd)
        if target is None or _inside(target, str(ROOT)) or _inside(target, TEMP):
            return
        if any(_inside(target, home) for home in REAL_HOMES):
            self._refuse(f"read {target}")

    def _spawn(self, args: Any, cwd: Any) -> None:
        argv = [str(part) for part in args] if not isinstance(args, str) else [args]
        program = Path(argv[0]).name if argv else ""
        if program == "getfacl" and all(_inside(os.path.realpath(a), TEMP) for a in argv[1:] if a.startswith("/")):
            return
        if (program == "git" and len(argv) > 1 and argv[1] in ("show", "rev-parse", "cat-file", "log")
                and cwd is not None and _inside(os.path.realpath(str(cwd)), str(ROOT))):
            return
        self._refuse(f"spawn {argv[:8]}")

    # -- installing them --------------------------------------------------
    def __enter__(self) -> "HostGuard":
        guard = self
        self._saved: dict[tuple[Any, str], Any] = {}

        def swap(owner: Any, name: str, value: Any) -> None:
            self._saved[(owner, name)] = getattr(owner, name)
            setattr(owner, name, value)

        real_os_open = os.open

        def os_open(path, flags, mode=0o777, *, dir_fd=None):
            (guard._write("open", path, dir_fd) if flags & guard.WRITE_FLAGS else guard._read(path, dir_fd))
            return real_os_open(path, flags, mode, dir_fd=dir_fd)

        swap(os, "open", os_open)
        for name in ("mkdir", "unlink", "remove", "rmdir", "chmod", "chown", "utime"):
            real = getattr(os, name)

            def one(path, *a, dir_fd=None, _real=real, _name=name, **k):
                guard._write(_name, path, dir_fd)
                return _real(path, *a, **({"dir_fd": dir_fd} if dir_fd is not None else {}), **k)

            swap(os, name, one)
        for name in ("rename", "replace", "symlink", "link"):
            real = getattr(os, name)

            def two(src, dst, *a, src_dir_fd=None, dst_dir_fd=None, dir_fd=None, _real=real, _name=name, **k):
                guard._write(_name, dst, dst_dir_fd if dst_dir_fd is not None else dir_fd)
                if _name in ("rename", "replace"):
                    guard._write(_name, src, src_dir_fd)
                keywords = {key: val for key, val in
                            (("src_dir_fd", src_dir_fd), ("dst_dir_fd", dst_dir_fd), ("dir_fd", dir_fd)) if val is not None}
                return _real(src, dst, *a, **keywords, **k)

            swap(os, name, two)
        real_open = builtins.open

        def any_open(file, mode="r", *a, **k):
            (guard._write(f"open-{mode}", file) if any(ch in mode for ch in "wax+") else guard._read(file))
            return real_open(file, mode, *a, **k)

        swap(builtins, "open", any_open)
        swap(io, "open", any_open)
        real_connect = socket.socket.connect

        def connect(sock, address):
            if sock.family == socket.AF_UNIX:
                target = guard._resolve(address)
                if target is not None and not _inside(target, TEMP):
                    guard._refuse(f"connect {target}")
            elif sock.family in (socket.AF_INET, socket.AF_INET6):
                guard._refuse(f"connect {address}")
            return real_connect(sock, address)

        swap(socket.socket, "connect", connect)
        real_popen = subprocess.Popen.__init__

        def popen(process, args, *a, **k):
            guard._spawn(args, k.get("cwd"))
            return real_popen(process, args, *a, **k)

        swap(subprocess.Popen, "__init__", popen)
        for name in ("execv", "execve", "execvp", "execvpe", "spawnv", "spawnvp", "system"):
            if hasattr(os, name):
                swap(os, name, lambda *a, _name=name: guard._refuse(f"{_name} {a[:2]}"))
        return self

    def __exit__(self, *_exc) -> None:
        for (owner, name), value in reversed(list(self._saved.items())):
            setattr(owner, name, value)


def root_pinned_program() -> Path:
    """A root-owned program every Linux host has, for checks that insist on root's.

    Chosen over this release's installed display helper so that nothing here
    depends on what a host has staged; the root-pinning rules are the same.
    """
    for candidate in ("/usr/bin/env", "/bin/env"):
        info = os.lstat(candidate)
        if stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022:
            return Path(candidate)
    raise AssertionError("no root-owned /usr/bin/env on this host")


def plain_acl_runner(args, **_kwargs) -> subprocess.CompletedProcess:
    """`getfacl -p --omit-header --absolute-names PATH`, answered from PATH's own mode.

    A file with no extended ACL has exactly the three entries its mode bits
    give, which is what getfacl would print; nothing is spawned.
    """
    argv = [str(part) for part in args]
    if not argv or Path(argv[0]).name != "getfacl":
        raise Refused(f"only getfacl is answered here: {argv}")
    mode = stat.S_IMODE(os.lstat(argv[-1]).st_mode)

    def triple(shift: int) -> str:
        bits = (mode >> shift) & 7
        return ("r" if bits & 4 else "-") + ("w" if bits & 2 else "-") + ("x" if bits & 1 else "-")

    return subprocess.CompletedProcess(argv, 0, stdout=f"user::{triple(6)}\ngroup::{triple(3)}\nother::{triple(0)}\n", stderr="")


def bridge_environment() -> tuple[str, ...]:
    """The variables a bridge-launched process inherits: who asked, and where its window goes."""
    from scripts import team_launcher as launcher

    return (launcher.TENANT_CONTROL_CALLER_ENV, launcher.PRESENTATION_HANDOFF_FD_ENV)


def contained(case: Callable[..., Any]) -> Callable[..., Any]:
    """Run a case in a fresh sandbox, with owned accounts and the host guard, and clean up whatever happens."""

    @functools.wraps(case)
    def run(*args, **kwargs):
        # The bridge context the RUNNER was launched with is not the case's: a
        # pane opened through the tenant-control bridge carries the caller and
        # the descriptor its window goes back through, and a case that launched
        # would write into that live descriptor -- a write with no path for the
        # guard to see. Cases that exercise the bridge set their own.
        inherited = {name: os.environ.pop(name, None) for name in bridge_environment()}
        sandbox = Path(tempfile.mkdtemp(prefix="syrd343."))
        guard = HostGuard()
        try:
            with OwnedAccounts(sandbox), guard:
                result = case(*args, **kwargs)
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)
            for name, value in inherited.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        if guard.attempts:
            raise Refused(f"{case.__name__} reached past its sandbox: {guard.attempts}")
        return result

    return run


def assert_owned(path: Path | str) -> None:
    """Fail unless `path` is inside the temporary directory -- never in a real account's home."""
    target = os.path.realpath(str(path))
    if not _inside(target, TEMP) or any(_inside(target, home) for home in REAL_HOMES):
        raise Refused(f"{target} is not an owned test path")
