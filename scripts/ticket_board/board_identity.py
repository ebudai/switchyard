#!/usr/bin/env python3
"""The tenant's real board, as root's own records name it, proven by the kernel on the connection (SYRD-694).

`privileged_helper.board_url_for` takes a project's board URL from the tenant's registered configuration, which the
tenant account can rewrite, and every role shares that account. A sibling role can therefore point the helper at a
board of its own that names the sibling's pane as the control role. For an action that mints durable proof that is
not good enough, so this finds the board from root's side only:

- **Which board.** The board's systemd unit, `/etc/systemd/system/<project>-ticket-board.service`, is root's. It is
  read through descriptors from `/`, refusing a link at any component, and it and every directory above it must be
  root's and writable by nobody else. Its `User=` is the board's service account, and the `--unix-socket` in its
  `ExecStart=` is where the board listens. Nothing comes from the tenant's configuration.
- **That it is that board.** The helper connects to that socket, and the kernel (`SO_PEERCRED`) says who answered:
  the peer must be the unit's service account, and the unit's own main process as systemd reports it. A process
  the tenant started is neither, whatever it listens on.
- **What it says.** The board's answers are read over that one proven connection, as HTTP.

Deliberately dependency-free: the helper polkit runs as root imports this from its own staged copy.
"""

from __future__ import annotations

import errno
import http.client
import json
import os
import pwd
import re
import shlex
import socket
import stat
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

UNIT_DIR = Path("/etc/systemd/system")
#: Whose the unit and every directory above it must be. A suite points this and `ANCHOR` at a tree of its own.
TRUSTED_OWNER_UID = 0
ANCHOR = Path("/")
SHARED_WRITE = stat.S_IWGRP | stat.S_IWOTH
PROJECT = re.compile(r"[a-z][a-z0-9-]{0,31}")
MAX_UNIT_BYTES = 64 << 10
MAX_ANSWER_BYTES = 8 << 20
TIMEOUT_SECONDS = 15.0


class BoardIdentityError(Exception):
    """The board cannot be found or proven from root's own records; the message says which and why."""


def _check(fd: int, where: str, owner_uid: int, *, directory: bool) -> None:
    info = os.fstat(fd)
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise BoardIdentityError(f"{where} is not a {'directory' if directory else 'regular file'}")
    if info.st_uid != owner_uid or info.st_mode & SHARED_WRITE:
        raise BoardIdentityError(f"{where} is not owned by uid {owner_uid} and writable only by it")


def _read_unit(path: Path, *, owner_uid: int, anchor: Path) -> str:
    absolute, anchor = Path(os.path.abspath(str(path))), Path(os.path.abspath(str(anchor)))
    if anchor not in absolute.parents:
        raise BoardIdentityError(f"{absolute} is not under {anchor}")
    fd = os.open(str(anchor), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    walked = anchor
    try:
        _check(fd, str(walked), owner_uid, directory=True)
        for component in absolute.parent.relative_to(anchor).parts:
            walked = walked / component
            try:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise BoardIdentityError(f"{walked} is a link or not a directory") from None
                raise BoardIdentityError(f"{walked} cannot be opened ({exc.strerror})") from None
            os.close(fd)
            fd = child
            _check(fd, str(walked), owner_uid, directory=True)
        try:
            file_fd = os.open(absolute.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
        except OSError as exc:
            raise BoardIdentityError(f"{absolute} cannot be opened without following a link ({exc.strerror})") from None
        with os.fdopen(file_fd, "rb") as handle:
            _check(handle.fileno(), str(absolute), owner_uid, directory=False)
            return handle.read(MAX_UNIT_BYTES).decode("utf-8", "replace")
    finally:
        os.close(fd)


def unit_name(project: str) -> str:
    if not PROJECT.fullmatch(project or ""):
        raise BoardIdentityError(f"{project!r} is not a project")
    return f"{project}-ticket-board.service"


def unit_endpoint(project: str, *, unit_dir: Path | None = None, owner_uid: int | None = None,
                  anchor: Path | None = None) -> tuple[str, str]:
    """(the board's Unix socket, its service account), from root's unit for this project alone."""
    unit = (unit_dir or UNIT_DIR) / unit_name(project)
    text = _read_unit(unit, owner_uid=TRUSTED_OWNER_UID if owner_uid is None else owner_uid,
                      anchor=anchor or ANCHOR)
    user, exec_start = "", ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("User="):
            user = stripped[len("User="):].strip()
        elif stripped.startswith("ExecStart="):
            exec_start = stripped[len("ExecStart="):].strip()
    try:
        argv = shlex.split(exec_start)
    except ValueError:
        argv = []
    sockets = [argv[i + 1] for i, token in enumerate(argv[:-1]) if token == "--unix-socket"]
    if len(sockets) != 1 or not sockets[0].startswith("/") or os.path.normpath(sockets[0]) != sockets[0]:
        raise BoardIdentityError(f"{unit} does not name exactly one absolute --unix-socket for its board")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user) or user == "root":
        raise BoardIdentityError(f"{unit} names no service account (User=) for its board")
    return sockets[0], user


def unit_main_pid(unit: str, *, runner: Callable[..., Any] = subprocess.run) -> int:
    """The unit's main process as systemd knows it; 0 when it is not running."""
    try:
        done = runner(["systemctl", "show", "--property=MainPID", "--value", unit], capture_output=True, text=True,
                      timeout=TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return 0
    value = (getattr(done, "stdout", "") or "").strip()
    return int(value) if value.isdigit() else 0


@dataclass(frozen=True)
class EffectiveUnit:
    user: str
    argv: tuple[str, ...]
    main_pid: int
    release_commit: str


def _show_values(unit: str, *, runner: Callable[..., Any]) -> dict[str, str]:
    properties = ("User", "ExecStart", "EnvironmentFiles", "DropInPaths", "MainPID", "Environment",
                  "PassEnvironment", "WorkingDirectory", "NeedDaemonReload")
    try:
        done = runner(
            ["systemctl", "show", unit, *(f"--property={name}" for name in properties), "--no-pager"],
            capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BoardIdentityError(f"cannot inspect {unit}'s effective service definition ({exc})") from None
    if getattr(done, "returncode", 1) != 0:
        detail = (getattr(done, "stderr", "") or "").strip() or "systemctl show failed"
        raise BoardIdentityError(f"cannot inspect {unit}'s effective service definition ({detail[:200]})")
    values: dict[str, str] = {}
    for line in (getattr(done, "stdout", "") or "").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value.strip()
    return values


def _shown_argv(value: str) -> tuple[str, ...]:
    """Parse systemd's scalar or ``ExecCommand`` rendering of ExecStart."""
    raw = value.strip()
    match = re.search(r"(?:^|[;{]\s*)argv\[\]=(.*?)(?:\s*;|\s*}$)", raw)
    if match:
        raw = match.group(1).strip()
    try:
        return tuple(shlex.split(raw))
    except ValueError:
        return ()


#: Every variable root's exact-release board unit sets (`release_units.render_board_unit` over the provisioned
#: template), and nothing else. A name outside it can choose code or what the board claims to be --
#: `LD_PRELOAD`, `PYTHONPATH`, `TICKET_BOARD_PYTHON`, `TICKET_BOARD_BUILD_ID` -- so a unit that sets one is not
#: the reviewed unit, whatever its ExecStart says.
BOARD_ENVIRONMENT = frozenset({
    "PYTHONUNBUFFERED", "HOME", "PGHOST", "PGDATABASE", "PGUSER",
    "TICKET_BOARD_DIRECTORCTL", "TICKET_BOARD_PROJECT", "TICKET_BOARD_PROCESS_AUTHORITY",
    "TICKET_BOARD_PROJECT_NAME", "TICKET_BOARD_TICKET_PREFIX", "TICKET_BOARD_COMMIT_GIT_DIR",
    "TICKET_BOARD_SOCKET", "TICKET_BOARD_TENANT_USER", "TICKET_BOARD_SOCKET_GROUP", "TICKET_BOARD_ROLE_ACCOUNTS",
    "TICKET_BOARD_DATABASE_URL", "TICKET_BOARD_DRAFT_ROLES", "TICKET_BOARD_IMPLEMENTER_ROLES",
    "TICKET_BOARD_ASSIGNEES", "TICKET_BOARD_CALLER_ROLES", "TICKET_BOARD_OPERATION_ALLOWED_ROLES",
})
#: What the live process may not carry beyond those. Systemd adds its own names (PATH, LANG, LC_*, INVOCATION_ID,
#: the directory and credential variables...), which vary by version, so the live check refuses these families
#: rather than listing systemd's.
LIVE_CODE_PREFIXES = ("LD_", "PYTHON", "GIT_", "GCONV_", "TICKET_BOARD_")


def _shown_environment(value: str, unit: str) -> dict[str, str]:
    try:
        words = shlex.split(value)
    except ValueError:
        raise BoardIdentityError(f"{unit}'s effective Environment cannot be parsed") from None
    environment: dict[str, str] = {}
    for word in words:
        key, separator, item = word.partition("=")
        if not separator or not key or key in environment:
            raise BoardIdentityError(f"{unit}'s effective Environment has a malformed or repeated entry")
        environment[key] = item
    return environment


def _trusted_environment(environment: dict[str, str], release: Path, unit: str) -> None:
    unknown = sorted(set(environment) - BOARD_ENVIRONMENT)
    if unknown:
        raise BoardIdentityError(f"{unit}'s effective Environment sets {', '.join(unknown)}, which the "
                                 "exact-release board unit does not")
    # Unset, the board runs its own release's directorctl (`runtime_paths.directorctl_path`); set, it must be that.
    directorctl = str(release / "scripts" / "directorctl")
    if environment.get("TICKET_BOARD_DIRECTORCTL", directorctl) != directorctl:
        raise BoardIdentityError(f"{unit}'s TICKET_BOARD_DIRECTORCTL is not {directorctl}")
    if environment.get("PYTHONUNBUFFERED", "1") != "1":
        raise BoardIdentityError(f"{unit} sets PYTHONUNBUFFERED to something other than 1")
    home = environment.get("HOME")
    if home is not None and (not home.startswith("/var/lib/") or os.path.normpath(home) != home):
        raise BoardIdentityError(f"{unit}'s HOME {home} is not the board's own /var/lib state")


def _trusted_exec(argv: tuple[str, ...], *, trusted_release: Callable[[str], str]) -> str:
    if len(argv) < 6 or argv[1:4] != ("-E", "-s", "-B"):
        raise BoardIdentityError("the board's effective ExecStart does not isolate Python with -E -s -B")
    script = Path(argv[4])
    if script.name != "ticket-board.py" or script.parent.name != "scripts":
        raise BoardIdentityError("the board's effective ExecStart does not run ticket-board.py from an exact release")
    release = script.parent.parent
    commit = release.name
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise BoardIdentityError(f"the board executes {script}, not a commit-named root release")
    try:
        expected = Path(trusted_release(commit))
    except Exception as exc:  # the resolver's refusal is the useful boundary message
        raise BoardIdentityError(str(exc)) from None
    if release != expected:
        raise BoardIdentityError(f"the board executes {release}, not root's trusted release {expected}")
    python = Path(argv[0])
    if not python.is_absolute():
        raise BoardIdentityError("the board's Python executable is not an absolute root-controlled path")
    try:
        info = os.lstat(script)
        python_info = os.stat(python)
    except OSError as exc:
        raise BoardIdentityError(f"the board's executable files cannot be inspected ({exc.strerror})") from None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != TRUSTED_OWNER_UID
            or info.st_mode & SHARED_WRITE):
        raise BoardIdentityError(f"the board program {script} is not a root-owned, non-writable regular file")
    if (not stat.S_ISREG(python_info.st_mode) or python_info.st_uid != TRUSTED_OWNER_UID
            or python_info.st_mode & SHARED_WRITE):
        raise BoardIdentityError(f"the board's Python {python} is not root-owned and non-writable")
    return commit


def effective_unit(
    project: str, *, runner: Callable[..., Any] = subprocess.run,
    proc_root: Path = Path("/proc"), trusted_release: Callable[[str], str] | None = None,
) -> EffectiveUnit:
    """Attest systemd's effective unit and the exact argv its live process has."""
    if trusted_release is None:
        from .privileged_operations import trusted_release_root
        trusted_release = trusted_release_root
    unit = unit_name(project)
    values = _show_values(unit, runner=runner)
    if values.get("EnvironmentFiles"):
        raise BoardIdentityError(f"{unit} still loads EnvironmentFiles; tenant environment is not board authority")
    if values.get("DropInPaths"):
        raise BoardIdentityError(f"{unit} has drop-ins, so its execution definition is not the reviewed unit")
    if values.get("NeedDaemonReload") != "no":
        raise BoardIdentityError(f"{unit}'s file differs from what systemd loaded (NeedDaemonReload), so its "
                                 "effective definition is not the reviewed unit")
    if values.get("PassEnvironment"):
        raise BoardIdentityError(f"{unit} passes the manager's environment through (PassEnvironment)")
    user = values.get("User", "")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user) or user == "root":
        raise BoardIdentityError(f"{unit} names no service account (User=) for its board")
    argv = tuple(value.replace("%d", f"/run/credentials/{unit}")
                 for value in _shown_argv(values.get("ExecStart", "")))
    release_commit = _trusted_exec(argv, trusted_release=trusted_release)
    release = Path(argv[4]).parent.parent
    if values.get("WorkingDirectory") != str(release):
        raise BoardIdentityError(f"{unit}'s effective WorkingDirectory is not its exact release {release}")
    environment = _shown_environment(values.get("Environment", ""), unit)
    _trusted_environment(environment, release, unit)
    pid_text = values.get("MainPID", "")
    pid = int(pid_text) if pid_text.isdigit() else 0
    if not pid:
        raise BoardIdentityError(f"{unit} is not running")
    try:
        live = tuple(part.decode("utf-8") for part in (proc_root / str(pid) / "cmdline").read_bytes().split(b"\0") if part)
    except (OSError, UnicodeDecodeError) as exc:
        raise BoardIdentityError(f"cannot read {unit}'s live command for process {pid} ({exc})") from None
    if live != argv:
        raise BoardIdentityError(f"{unit}'s live process {pid} does not execute its effective ExecStart")
    # The live process can predate a reload that changed only its environment, so it is read as well.
    try:
        live_cwd = os.readlink(proc_root / str(pid) / "cwd")
        live_environment = dict(
            part.decode("utf-8").partition("=")[::2]
            for part in (proc_root / str(pid) / "environ").read_bytes().split(b"\0") if part
        )
    except (OSError, UnicodeDecodeError) as exc:
        raise BoardIdentityError(f"cannot read {unit}'s live directory and environment for process {pid} "
                                 f"({exc})") from None
    if live_cwd != str(release):
        raise BoardIdentityError(f"{unit}'s live process {pid} does not run in its exact release {release}")
    for key in BOARD_ENVIRONMENT | set(live_environment):
        if key in BOARD_ENVIRONMENT:
            if live_environment.get(key) != environment.get(key):
                raise BoardIdentityError(f"{unit}'s live process {pid} has a {key} its effective unit does not set")
        elif key.startswith(LIVE_CODE_PREFIXES):
            raise BoardIdentityError(f"{unit}'s live process {pid} carries {key}, which the board unit does not set")
    return EffectiveUnit(user, argv, pid, release_commit)


class _ProvenConnection(http.client.HTTPConnection):
    def __init__(self, sock: socket.socket) -> None:
        super().__init__("localhost", timeout=TIMEOUT_SECONDS)
        self.sock = sock

    def connect(self) -> None:  # the socket is connected, and proven, already
        return None


def board_get_for(project: str, *, unit_dir: Path | None = None, owner_uid: int | None = None,
                  anchor: Path | None = None, main_pid: Callable[[str], int] = unit_main_pid,
                  service_uid: Callable[[str], int] = lambda user: pwd.getpwnam(user).pw_uid,
                  runtime_unit: Callable[[str], EffectiveUnit] | None = None,
                  ) -> Callable[[str, str], Any]:
    """A `board_get(board_url, path)` that asks only the board root's unit names, each answer over a connection the
    kernel shows that board's own process holds. `board_url` is ignored: nothing the caller says chooses it."""
    path, disk_user = unit_endpoint(project, unit_dir=unit_dir, owner_uid=owner_uid, anchor=anchor)
    # Explicit ``main_pid`` remains the narrow test seam SYRD-694 shipped.  The
    # production path attests the effective unit and live command instead.
    if runtime_unit is not None or main_pid is unit_main_pid:
        runtime = (runtime_unit or effective_unit)(project)
        user, expected_pid, expected_build = runtime.user, runtime.main_pid, runtime.release_commit
        if user != disk_user:
            raise BoardIdentityError(f"{unit_name(project)}'s effective User={user} differs from its root unit")
    else:
        user, expected_pid, expected_build = disk_user, main_pid(unit_name(project)), ""
    try:
        expected_uid = service_uid(user)
    except KeyError:
        raise BoardIdentityError(f"{project}'s board account {user} does not exist") from None
    unit = unit_name(project)

    def request(request_path: str) -> Any:
        if not request_path.startswith("/api/"):
            raise BoardIdentityError(f"refusing to ask {project}'s board for {request_path!r}")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(TIMEOUT_SECONDS)
            try:
                sock.connect(path)
            except OSError as exc:
                raise BoardIdentityError(f"{project}'s board socket {path} does not answer ({exc.strerror})") from None
            pid, uid, _gid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                                 struct.calcsize("3i")))
            if uid != expected_uid:
                raise BoardIdentityError(f"{path} is answered by uid {uid}, not {project}'s board account {user}")
            running = expected_pid
            if not running or pid != running:
                raise BoardIdentityError(f"{path} is answered by process {pid}, not {unit}'s main process "
                                         f"({running or 'not running'})")
            connection = _ProvenConnection(sock)
            connection.request("GET", request_path, headers={"Host": "localhost", "Connection": "close"})
            response = connection.getresponse()
            body = response.read(MAX_ANSWER_BYTES + 1)
            if response.status == 404:
                return None
            if response.status != 200 or len(body) > MAX_ANSWER_BYTES:
                raise BoardIdentityError(f"{project}'s board answered {request_path} with HTTP {response.status}")
            try:
                return json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise BoardIdentityError(f"{project}'s board answered {request_path} with no JSON") from None
        finally:
            sock.close()

    if expected_build:
        identity = request("/api/board")
        build = str(identity.get("build_id") or "") if isinstance(identity, dict) else ""
        if build != expected_build:
            raise BoardIdentityError(
                f"{project}'s board process reports build {build or 'nothing'}, not its effective "
                f"root release {expected_build}"
            )

    def get(_board_url: str, request_path: str) -> Any:
        return request(request_path)

    return get
