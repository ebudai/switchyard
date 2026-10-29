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


class _ProvenConnection(http.client.HTTPConnection):
    def __init__(self, sock: socket.socket) -> None:
        super().__init__("localhost", timeout=TIMEOUT_SECONDS)
        self.sock = sock

    def connect(self) -> None:  # the socket is connected, and proven, already
        return None


def board_get_for(project: str, *, unit_dir: Path | None = None, owner_uid: int | None = None,
                  anchor: Path | None = None, main_pid: Callable[[str], int] = unit_main_pid,
                  service_uid: Callable[[str], int] = lambda user: pwd.getpwnam(user).pw_uid
                  ) -> Callable[[str, str], Any]:
    """A `board_get(board_url, path)` that asks only the board root's unit names, each answer over a connection the
    kernel shows that board's own process holds. `board_url` is ignored: nothing the caller says chooses it."""
    path, user = unit_endpoint(project, unit_dir=unit_dir, owner_uid=owner_uid, anchor=anchor)
    try:
        expected_uid = service_uid(user)
    except KeyError:
        raise BoardIdentityError(f"{project}'s board account {user} does not exist") from None
    unit = unit_name(project)

    def get(_board_url: str, request_path: str) -> Any:
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
            running = main_pid(unit)
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

    return get
