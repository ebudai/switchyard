#!/usr/bin/env python3
"""One host service for every tenant board: a supervisor with an isolated child per tenant (SYRD-586).

`switchyard-board-supervisor.service` runs `serve` as root. For each tenant
with a record under `/etc/switchyard/board-supervisor/tenants/`, it starts that
tenant's board as its own child, built from the tenant's own reviewed unit
(`board_supervisor_spec`) and confined by the exec stage
(`board_supervisor_sandbox`). Children share nothing but this parent: each
keeps its own release, database role, port, socket, runtime directory, user,
groups, mount sandbox and environment. Why not one process for every board
is in docs/board-supervisor.md.

Lifecycle, per tenant and never across tenants:
- **Any exit the supervisor did not ask for is restarted**, a clean exit 0
  included, after the unit's `RestartSec=`. The board exits 0 on SIGTERM
  from anyone, so `Restart=on-failure` read a stray SIGTERM as a finished
  service, and Otto's board stayed down while its panes kept running.
- **The unit's start limit still holds.** More starts than
  `StartLimitBurst=` within `StartLimitIntervalSec=` leave the tenant
  `failed`, not flapping. An explicit start or restart clears it, as
  `reset-failed` would.
- **An intentional stop is persisted** (`/var/lib/switchyard/board-supervisor/<p>.desired`),
  so a suspended tenant stays stopped across a supervisor restart and a reboot.
- **Control is per tenant**, over `/run/switchyard-board-supervisor/control.sock`:
  - root, and that tenant's owner for that tenant only, may start, stop or
    restart it; the peer's uid comes from the kernel (`SO_PEERCRED`);
  - anyone may read status, which is also published to `status.json` beside
    the socket;
  - `reload` (root) picks up added or removed tenant records.

Every log line names the tenant and the child's pid.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import selectors
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

if __name__ == "__main__" and not __package__:
    # Run as a program -- `python3 -I -B .../scripts/board_supervisor.py`, which
    # puts neither the script's directory nor the working directory on the path.
    # The release root goes there instead, so this and every module it uses are
    # imported as the `scripts` package, once (SYRD-586).
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

sys.dont_write_bytecode = True

from scripts import board_supervisor_spec as spec_mod  # noqa: E402

SANDBOX = Path(__file__).resolve().with_name("board_supervisor_sandbox.py")
PROTOCOL = "switchyard.board-supervisor.v1"
REQUEST_LIMIT = 4096
#: A control client that has not sent its whole request by then is dropped.
CLIENT_SECONDS = 10.0
#: When no unit could be read to say how long to wait before a retry.
FALLBACK_RESTART_SEC = 30.0
CONTROL_VERBS = frozenset({"start", "stop", "restart"})
EXIT_UNAVAILABLE = 3


def now_text() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def default_unit_active(unit: str) -> bool:
    try:
        result = subprocess.run(["systemctl", "is-active", "--quiet", unit], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def describe_exit(returncode: int) -> tuple[int | None, str | None, str]:
    if returncode < 0:
        try:
            name = signal.Signals(-returncode).name
        except ValueError:
            name = f"signal {-returncode}"
        return None, name, f"signal {name}"
    return returncode, None, f"status {returncode}"


@dataclass
class Child:
    project: str
    desired: str = "running"
    state: str = "stopped"
    pid: int | None = None
    popen: subprocess.Popen | None = None
    output_fd: int | None = None
    buffer: bytes = b""
    started_at: str = ""
    start_times: deque = field(default_factory=deque)
    restarts: int = 0
    last_exit: dict | None = None
    detail: str = ""
    release: str = ""
    port: int | None = None
    socket: str = ""
    user: str = ""
    owner: str = ""
    runtime_path: Path | None = None
    restart_sec: float = FALLBACK_RESTART_SEC
    start_limit_interval: float = spec_mod.DEFAULT_START_LIMIT_INTERVAL
    start_limit_burst: int = spec_mod.DEFAULT_START_LIMIT_BURST
    stop_timeout: float = spec_mod.DEFAULT_STOP_TIMEOUT
    restart_at: float | None = None
    stop_deadline: float | None = None
    killed: bool = False
    after_stop: str = ""
    waiters: list = field(default_factory=list)

    def snapshot(self, paths: spec_mod.Paths) -> dict:
        return {
            "project": self.project, "unit": paths.unit_name(self.project), "owner": self.owner,
            "desired": self.desired, "state": self.state, "pid": self.pid, "started_at": self.started_at,
            "restarts": self.restarts, "release": self.release, "port": self.port, "socket": self.socket,
            "user": self.user, "last_exit": self.last_exit, "detail": self.detail,
        }


class Supervisor:
    def __init__(
        self,
        paths: spec_mod.Paths,
        *,
        unit_active: Callable[[str], bool] = default_unit_active,
        clock: Callable[[], float] = time.monotonic,
        python: str = sys.executable,
        sandbox: Path = SANDBOX,
        inherited_env: dict[str, str] | None = None,
        out=None,
    ) -> None:
        self.paths = paths
        self.unit_active = unit_active
        self.clock = clock
        self.python = python
        self.sandbox = sandbox
        self.inherited_env = dict(os.environ if inherited_env is None else inherited_env)
        self.out = out or sys.stdout
        self.children: dict[str, Child] = {}
        self.selector = selectors.DefaultSelector()
        self.listener: socket.socket | None = None
        self.wakeup_read = self.wakeup_write = -1
        self.stopping = False
        self.dirty = True

    # -- logging and records -------------------------------------------------

    def log(self, message: str, *, level: int = 6) -> None:
        """One journal line. `<N>` is the syslog priority systemd reads from a service's stdout."""
        self.out.write(f"<{level}>board-supervisor: {message}\n")
        self.out.flush()

    def read_desired(self, project: str) -> str:
        try:
            value = self.paths.desired_path(project).read_text(encoding="utf-8").strip()
        except OSError:
            return "running"
        return value if value in {"running", "stopped"} else "running"

    def write_desired(self, child: Child, desired: str) -> None:
        child.desired = desired
        path = self.paths.desired_path(child.project)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}")
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(desired + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)

    def publish(self) -> None:
        """status.json: what `switchyard status` reads, written whole and replaced atomically."""
        if not self.dirty:
            return
        self.dirty = False
        document = {
            "schema": "switchyard.board-supervisor.status.v1", "supervisor_pid": os.getpid(),
            "updated_at": now_text(),
            "tenants": [self.children[name].snapshot(self.paths) for name in sorted(self.children)],
        }
        path = self.paths.status_path()
        temporary = path.with_name(f".{path.name}.{os.getpid()}")
        try:
            temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
            os.chmod(temporary, 0o644)
            os.replace(temporary, path)
        except OSError as exc:
            self.log(f"could not publish {path}: {exc}", level=4)

    # -- tenants ---------------------------------------------------------------

    def load(self) -> list[str]:
        """Adopt every recorded tenant and forget every one whose record is gone. What changed."""
        changed = []
        recorded = set(spec_mod.supervised_projects(self.paths))
        for project in sorted(recorded - set(self.children)):
            child = Child(project=project, desired=self.read_desired(project))
            self.children[project] = child
            changed.append(f"added {project}")
            self.log(f"tenant={project} is supervised (desired {child.desired})")
            if child.desired == "running":
                self.start(child, reason="adopted")
        for project in sorted(set(self.children) - recorded):
            child = self.children[project]
            changed.append(f"released {project}")
            self.log(f"tenant={project} pid={child.pid or '-'} is no longer supervised; stopping it")
            if child.state in {"running", "stopping"}:
                self.stop(child, then="forget")
            else:
                self.forget(child)
        self.dirty = True
        return changed

    def forget(self, child: Child) -> None:
        self.children.pop(child.project, None)
        self.reply_waiters(child)
        self.dirty = True

    def owner_uid(self, project: str) -> int:
        record = spec_mod.read_tenant_record(project, self.paths)
        try:
            return pwd.getpwnam(record.owner).pw_uid
        except KeyError as exc:
            raise spec_mod.SpecError(f"tenant record for {project} names unknown owner {record.owner!r}") from exc

    def prepare_runtime_directory(self, spec: spec_mod.ChildSpec) -> Path | None:
        if not spec.runtime_directory:
            return None
        path = spec.runtime_path(self.paths)
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise spec_mod.SpecError(f"runtime directory {path} exists and is not a directory")
        # Fresh for every start, as systemd leaves it: what a previous run left
        # there -- a socket file above all -- would make this board's bind fail.
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(mode=0o700)
        os.chown(path, spec.uid, spec.gid)
        os.chmod(path, spec.runtime_directory_mode)
        return path

    def start(self, child: Child, *, reason: str) -> str:
        """Start the tenant's board now. Returns the refusal, or ''."""
        if child.state in {"running", "stopping"}:
            return ""
        child.restart_at = None
        child.start_times.append(self.clock())
        try:
            record = spec_mod.read_tenant_record(child.project, self.paths)
            child.owner = record.owner
            spec = spec_mod.read_unit_spec(child.project, self.paths)
            child.restart_sec = spec.restart_sec
            child.start_limit_interval = spec.start_limit_interval
            child.start_limit_burst = spec.start_limit_burst
            child.stop_timeout = spec.stop_timeout
            foreign = spec_mod.foreign_drop_ins(child.project, self.paths)
            if foreign:
                raise spec_mod.SpecError(
                    f"drop-ins {', '.join(map(str, foreign))} would change {self.paths.unit_name(child.project)} "
                    "under systemd and are not applied by the supervisor")
            if self.unit_active(self.paths.unit_name(child.project)):
                raise spec_mod.SpecError(
                    f"{self.paths.unit_name(child.project)} is active under systemd; two boards would contend "
                    "for one port and socket")
            env = spec_mod.child_environment(spec, owner_uid=self.owner_uid(child.project),
                                             inherited=self.inherited_env, paths=self.paths)
            runtime = self.prepare_runtime_directory(spec)
        except (spec_mod.SpecError, OSError) as exc:
            child.detail = str(exc)
            self.log(f"tenant={child.project} was not started ({reason}): {exc}", level=3)
            self.schedule_restart(child)
            return str(exc)
        document = {
            "project": child.project, "argv": list(spec.argv), "env": env, "uid": spec.uid, "gid": spec.gid,
            "groups": list(spec.groups), "cwd": spec.working_directory,
            "read_write_paths": list(spec.read_write_paths), "protect_system": spec.protect_system,
            "protect_home": spec.protect_home, "no_new_privileges": spec.no_new_privileges,
            "supervisor_pid": os.getpid(),
        }
        try:
            popen = subprocess.Popen(
                [self.python, "-I", "-B", str(self.sandbox)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, start_new_session=True, close_fds=True, env={}, cwd="/",
            )
        except OSError as exc:
            child.detail = f"could not spawn the exec stage: {exc}"
            self.log(f"tenant={child.project} was not started ({reason}): {child.detail}", level=3)
            self.schedule_restart(child)
            return child.detail
        try:
            popen.stdin.write(json.dumps(document).encode("utf-8"))
            popen.stdin.close()
        except OSError:
            pass  # the stage exits and is reaped like any other exit
        child.popen, child.pid, child.state = popen, popen.pid, "running"
        child.killed, child.stop_deadline, child.detail = False, None, ""
        child.started_at, child.release, child.port, child.socket = now_text(), spec.release(), spec.port, spec.socket
        child.user, child.runtime_path = spec.user, runtime
        child.output_fd = popen.stdout.fileno()
        os.set_blocking(child.output_fd, False)
        self.selector.register(child.output_fd, selectors.EVENT_READ, ("output", child.project, popen))
        self.log(f"tenant={child.project} pid={child.pid} started ({reason}): release {child.release}, "
                 f"port {child.port}, socket {child.socket}, user {spec.user}")
        for note in spec.notes:
            self.log(f"tenant={child.project} pid={child.pid}: {note}")
        self.dirty = True
        return ""

    def schedule_restart(self, child: Child) -> None:
        """After an exit or start nobody asked for: wait RestartSec, unless the start limit is spent."""
        child.pid, child.popen = None, None
        if child.desired != "running" or self.stopping:
            child.state = "stopped"
            self.dirty = True
            return
        now = self.clock()
        while child.start_times and child.start_times[0] < now - child.start_limit_interval:
            child.start_times.popleft()
        if len(child.start_times) >= child.start_limit_burst:
            child.state = "failed"
            child.detail = (f"{len(child.start_times)} starts within {child.start_limit_interval:g}s; "
                            f"left failed. Fix the cause, then `switchyard-board-supervisor restart {child.project}`"
                            + (f" (last: {child.detail})" if child.detail else ""))
            self.log(f"tenant={child.project} hit its start limit: {child.detail}", level=3)
        else:
            child.state = "backoff"
            child.restart_at = now + child.restart_sec
            self.log(f"tenant={child.project} restarting in {child.restart_sec:g}s")
        self.dirty = True

    def stop(self, child: Child, *, then: str = "") -> None:
        child.restart_at = None
        if child.state == "running" and child.pid:
            child.state, child.after_stop = "stopping", then
            child.stop_deadline = self.clock() + child.stop_timeout
            self.log(f"tenant={child.project} pid={child.pid} stopping (SIGTERM to its process group)")
            self.signal_group(child, signal.SIGTERM)
        elif child.state == "stopping":
            child.after_stop = then or child.after_stop
        else:
            child.state = "stopped"
            if then == "start":
                self.start(child, reason="restart requested")
            elif then == "forget":
                self.forget(child)
        self.dirty = True

    def signal_group(self, child: Child, sig: int) -> None:
        try:
            os.killpg(child.pid, sig)
        except (ProcessLookupError, PermissionError, TypeError):
            pass

    def drain(self, child: Child, *, final: bool) -> None:
        if child.output_fd is None:
            return
        while True:
            try:
                chunk = os.read(child.output_fd, 65536)
            except BlockingIOError:
                chunk = None
            except OSError:
                chunk = b""
            if chunk:
                child.buffer += chunk
                *lines, child.buffer = child.buffer.split(b"\n")
                for line in lines:
                    self.out.write(f"<6>tenant={child.project} pid={child.pid}: "
                                   f"{line.decode('utf-8', 'replace').rstrip()}\n")
                self.out.flush()
                continue
            if chunk == b"" or final:
                if child.buffer:
                    self.out.write(f"<6>tenant={child.project} pid={child.pid}: "
                                   f"{child.buffer.decode('utf-8', 'replace').rstrip()}\n")
                    child.buffer = b""
                try:
                    self.selector.unregister(child.output_fd)
                except (KeyError, ValueError):
                    pass
                try:
                    child.popen.stdout.close()
                except (AttributeError, OSError):
                    pass
                child.output_fd = None
            return

    def reap(self) -> None:
        for child in list(self.children.values()):
            if child.popen is None or child.popen.poll() is None:
                continue
            self.exited(child, child.popen.returncode)

    def exited(self, child: Child, returncode: int) -> None:
        pid, requested = child.pid, child.state == "stopping"
        self.drain(child, final=True)
        # Whatever the board left in its group goes with it, as a unit's control group would.
        self.signal_group(child, signal.SIGKILL)
        if child.runtime_path is not None:
            shutil.rmtree(child.runtime_path, ignore_errors=True)
            child.runtime_path = None
        status, signal_name, described = describe_exit(returncode)
        child.last_exit = {"pid": pid, "status": status, "signal": signal_name, "at": now_text(),
                           "requested": requested}
        if requested:
            self.log(f"tenant={child.project} pid={pid} exited with {described} (stop requested)")
            child.pid, child.popen, child.state = None, None, "stopped"
            then, child.after_stop = child.after_stop, ""
            if then == "start":
                self.start(child, reason="restart requested")
            elif then == "forget":
                self.forget(child)
                return
        else:
            self.log(f"tenant={child.project} pid={pid} exited with {described}, which the supervisor did "
                     "not request", level=4)
            child.detail = f"pid {pid} exited with {described} unrequested"
            self.schedule_restart(child)
        self.dirty = True
        self.reply_waiters(child)

    def tick(self) -> None:
        now = self.clock()
        for key in list(self.selector.get_map().values()):
            if key.data[0] == "client" and now - key.data[2] > CLIENT_SECONDS:
                self.selector.unregister(key.fileobj)
                key.fileobj.close()
        for child in list(self.children.values()):
            if child.state == "backoff" and child.restart_at is not None and child.restart_at <= now:
                child.state = "stopped"
                child.restarts += 1
                self.start(child, reason=f"automatic restart {child.restarts}")
            elif child.state == "stopping" and not child.killed and child.stop_deadline is not None \
                    and child.stop_deadline <= now:
                child.killed = True
                self.log(f"tenant={child.project} pid={child.pid} did not stop within {child.stop_timeout:g}s; "
                         "SIGKILL to its process group", level=4)
                self.signal_group(child, signal.SIGKILL)

    def next_timeout(self) -> float:
        deadlines = [child.restart_at for child in self.children.values() if child.state == "backoff"
                     and child.restart_at is not None]
        deadlines += [child.stop_deadline for child in self.children.values() if child.state == "stopping"
                      and not child.killed and child.stop_deadline is not None]
        if not deadlines:
            return 5.0
        return max(0.0, min(min(deadlines) - self.clock(), 5.0))

    # -- control -------------------------------------------------------------

    def reply(self, connection: socket.socket, payload: dict) -> None:
        try:
            connection.setblocking(True)
            connection.settimeout(2.0)
            connection.sendall((json.dumps({"protocol": PROTOCOL, **payload}) + "\n").encode("utf-8"))
        except OSError:
            pass
        finally:
            connection.close()

    def reply_waiters(self, child: Child) -> None:
        keep = []
        for connection, verb, previous in child.waiters:
            if child.state == "stopping":
                keep.append((connection, verb, previous))
                continue
            ok = child.state == ("stopped" if verb == "stop" else "running")
            self.reply(connection, {"ok": ok, "verb": verb, "previous_pid": previous,
                                    **({} if ok else {"error": child.detail or f"{child.project} is {child.state}"}),
                                    "tenant": child.snapshot(self.paths)})
        child.waiters = keep

    def accept(self) -> None:
        try:
            connection, _ = self.listener.accept()
        except OSError:
            return
        connection.setblocking(False)
        self.selector.register(connection, selectors.EVENT_READ, ("client", bytearray(), self.clock()))

    def read_client(self, connection: socket.socket, buffer: bytearray) -> None:
        try:
            chunk = connection.recv(REQUEST_LIMIT)
        except BlockingIOError:
            return
        except OSError:
            chunk = b""
        buffer.extend(chunk)
        if b"\n" not in buffer and chunk and len(buffer) < REQUEST_LIMIT:
            return
        self.selector.unregister(connection)
        if b"\n" not in buffer:
            connection.close()
            return
        try:
            request = json.loads(bytes(buffer).split(b"\n", 1)[0])
            if not isinstance(request, dict):
                raise ValueError("not an object")
        except ValueError as exc:
            self.reply(connection, {"ok": False, "error": f"unreadable request: {exc}"})
            return
        try:
            _pid, uid, _gid = struct.unpack("3i", connection.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        except OSError:
            connection.close()
            return
        self.handle(connection, request, uid)

    def handle(self, connection: socket.socket, request: dict, uid: int) -> None:
        verb, project = str(request.get("verb") or ""), str(request.get("project") or "")
        if verb == "status":
            tenants = [self.children[name].snapshot(self.paths) for name in sorted(self.children)
                       if not project or name == project]
            self.reply(connection, {"ok": True, "verb": verb, "tenants": tenants})
            return
        if verb == "reload":
            if uid != 0:
                self.reply(connection, {"ok": False, "verb": verb, "error": "only root may reload the supervisor"})
                return
            self.reply(connection, {"ok": True, "verb": verb, "changed": self.load()})
            return
        if verb not in CONTROL_VERBS:
            self.reply(connection, {"ok": False, "verb": verb, "error": f"unknown verb {verb!r}"})
            return
        child = self.children.get(project)
        if child is None:
            self.reply(connection, {"ok": False, "verb": verb,
                                    "error": f"{project or 'that project'} is not supervised here"})
            return
        try:
            allowed = uid == 0 or uid == self.owner_uid(project)
        except spec_mod.SpecError as exc:
            self.reply(connection, {"ok": False, "verb": verb, "error": str(exc)})
            return
        if not allowed:
            self.log(f"tenant={project}: refused {verb} from uid {uid}, which neither is root nor owns it", level=4)
            self.reply(connection, {"ok": False, "verb": verb,
                                    "error": f"uid {uid} may not {verb} {project}: only root and its owner may"})
            return
        self.log(f"tenant={project} pid={child.pid or '-'}: {verb} requested by uid {uid}")
        previous = child.pid
        if verb == "stop":
            self.write_desired(child, "stopped")
            self.stop(child)
        else:
            self.write_desired(child, "running")
            child.start_times.clear()
            if verb == "restart":
                self.stop(child, then="start")
            elif child.state == "stopping":
                child.after_stop = "start"
            elif child.state != "running":
                child.state = "stopped"
                self.start(child, reason="start requested")
        child.waiters.append((connection, verb, previous))
        self.reply_waiters(child)
        self.dirty = True

    # -- the loop --------------------------------------------------------------

    def open_control_socket(self) -> None:
        self.paths.runtime_root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.paths.runtime_root, 0o755)
        path = self.paths.control_socket()
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        os.chmod(path, 0o666)
        self.listener.listen(16)
        self.listener.setblocking(False)
        self.selector.register(self.listener, selectors.EVENT_READ, ("listener",))

    def install_signals(self) -> None:
        self.wakeup_read, self.wakeup_write = os.pipe()
        os.set_blocking(self.wakeup_read, False)
        os.set_blocking(self.wakeup_write, False)
        signal.set_wakeup_fd(self.wakeup_write)
        for sig in (signal.SIGCHLD, signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, lambda *_: None)
        self.selector.register(self.wakeup_read, selectors.EVENT_READ, ("signals",))

    def take_signals(self) -> None:
        try:
            received = os.read(self.wakeup_read, 512)
        except BlockingIOError:
            return
        for number in received:
            if number in (signal.SIGTERM, signal.SIGINT) and not self.stopping:
                self.log("stopping every board; their desired states are kept")
                self.stopping = True
                for child in self.children.values():
                    self.stop(child)
            elif number == signal.SIGHUP:
                self.load()

    def step(self) -> None:
        for key, _ in self.selector.select(self.next_timeout()):
            kind = key.data[0]
            if kind == "listener":
                self.accept()
            elif kind == "client":
                self.read_client(key.fileobj, key.data[1])
            elif kind == "output":
                child = self.children.get(key.data[1])
                if child is not None and child.popen is key.data[2]:
                    self.drain(child, final=False)
                else:
                    self.selector.unregister(key.fileobj)
            elif kind == "signals":
                self.take_signals()
        self.reap()
        self.tick()
        self.publish()

    def serve(self) -> int:
        self.install_signals()
        self.open_control_socket()
        self.paths.state_root.mkdir(parents=True, exist_ok=True)
        self.log(f"pid={os.getpid()} supervising boards recorded under {self.paths.tenants_dir()}")
        self.load()
        self.publish()
        while not (self.stopping and not any(child.state == "stopping" for child in self.children.values())):
            self.step()
        for child in self.children.values():
            for connection, verb, previous in child.waiters:
                self.reply(connection, {"ok": False, "verb": verb, "previous_pid": previous,
                                        "error": "the supervisor is stopping"})
        self.dirty = True
        self.publish()
        self.log("stopped")
        return 0


# -- the client ------------------------------------------------------------------


class SupervisorUnavailable(Exception):
    pass


def request(payload: dict, *, paths: spec_mod.Paths, timeout: float = 150.0) -> dict:
    """One request, one answer. A stop waits for the board's exit, so the default wait is long."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(str(paths.control_socket()))
            connection.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            data = b""
            while not data.endswith(b"\n"):
                chunk = connection.recv(65536)
                if not chunk:
                    break
                data += chunk
    except OSError as exc:
        raise SupervisorUnavailable(f"the board supervisor at {paths.control_socket()} did not answer: {exc}") from exc
    try:
        return json.loads(data)
    except ValueError as exc:
        raise SupervisorUnavailable(f"the board supervisor answered unreadably: {data[:200]!r}") from exc


def read_status_file(paths: spec_mod.Paths) -> dict | None:
    try:
        return json.loads(paths.status_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def describe_tenant(tenant: dict) -> str:
    last = tenant.get("last_exit") or {}
    exit_text = "-"
    if last:
        how = f"signal {last['signal']}" if last.get("signal") else f"status {last.get('status')}"
        exit_text = f"pid {last.get('pid')} {how} {'requested' if last.get('requested') else 'UNREQUESTED'} at {last.get('at')}"
    return (f"{tenant['project']}: {tenant['state']} (desired {tenant['desired']}), pid {tenant.get('pid') or '-'}, "
            f"restarts {tenant.get('restarts', 0)}, last exit {exit_text}, release {tenant.get('release') or '-'}, "
            f"port {tenant.get('port') or '-'}" + (f"; {tenant['detail']}" if tenant.get("detail") else ""))


def _paths_from(args: argparse.Namespace) -> spec_mod.Paths:
    defaults = spec_mod.Paths()
    return spec_mod.Paths(
        config_root=Path(args.config_root) if args.config_root else defaults.config_root,
        state_root=Path(args.state_root) if args.state_root else defaults.state_root,
        runtime_root=Path(args.runtime_root) if args.runtime_root else defaults.runtime_root,
        unit_dir=Path(args.unit_dir) if args.unit_dir else defaults.unit_dir,
        drop_in_dirs=(Path(args.unit_dir),) if args.unit_dir else defaults.drop_in_dirs,
        run_root=Path(args.run_root) if args.run_root else defaults.run_root,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard-board-supervisor",
        description="Run, control and migrate tenant boards under one host supervisor (SYRD-586).",
    )
    for option in ("--config-root", "--state-root", "--runtime-root", "--unit-dir", "--run-root"):
        parser.add_argument(option, default="", help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="run the supervisor (the service's ExecStart)")
    status = sub.add_parser("status", help="every supervised tenant, or one")
    status.add_argument("project", nargs="?", default="")
    status.add_argument("--json", action="store_true")
    for verb in sorted(CONTROL_VERBS):
        sub.add_parser(verb, help=f"{verb} one tenant's board; root or its owner").add_argument("project")
    sub.add_parser("reload", help="pick up added or removed tenant records; root")
    sub.add_parser("render-unit", help="print switchyard-board-supervisor.service")
    install = sub.add_parser("install", help="install and start the supervisor unit; plans unless --apply")
    install.add_argument("--apply", action="store_true")
    for name, text in (("migrate", "move a tenant's board from its unit to the supervisor"),
                       ("rollback", "move a supervised tenant's board back to its unit")):
        command = sub.add_parser(name, help=f"{text}; plans unless --apply")
        command.add_argument("project")
        command.add_argument("--apply", action="store_true")
        command.add_argument("--verify-timeout", type=float, default=60.0, help=argparse.SUPPRESS)
        if name == "migrate":
            command.add_argument("--owner", default="", help="the tenant's owner account, if the unit does not name it")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = _paths_from(args)
    if args.command == "serve":
        if os.geteuid() != 0:
            print("switchyard-board-supervisor: serve runs as root (it starts each board as its own user)",
                  file=sys.stderr)
            return 1
        return Supervisor(paths).serve()
    if args.command in {"render-unit", "install", "migrate", "rollback"}:
        from scripts import board_supervisor_migration as migration

        return migration.command(args, paths)
    payload = {"verb": args.command, "project": getattr(args, "project", "")}
    try:
        answer = request(payload, paths=paths)
    except SupervisorUnavailable as exc:
        if args.command == "status":
            published = read_status_file(paths)
            if published is not None:
                print(f"switchyard-board-supervisor: {exc}; last published {published.get('updated_at')}:")
                for tenant in published.get("tenants", []):
                    if not args.project or tenant["project"] == args.project:
                        print(f"  {describe_tenant(tenant)}")
                return EXIT_UNAVAILABLE
        print(f"switchyard-board-supervisor: {exc}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    if args.command == "status":
        if args.json:
            print(json.dumps(answer.get("tenants", []), indent=2))
        else:
            for tenant in answer.get("tenants", []):
                print(describe_tenant(tenant))
        return 0
    if not answer.get("ok"):
        print(f"switchyard-board-supervisor: {args.command} {payload['project']}: {answer.get('error')}",
              file=sys.stderr)
        if answer.get("tenant"):
            print(f"switchyard-board-supervisor:   {describe_tenant(answer['tenant'])}", file=sys.stderr)
        return 1
    if args.command == "reload":
        print(f"switchyard-board-supervisor: reloaded: {', '.join(answer.get('changed') or []) or 'no change'}")
        return 0
    tenant = answer["tenant"]
    print(f"switchyard-board-supervisor: {args.command} {tenant['project']}: pid {answer.get('previous_pid') or '-'}"
          f" -> {tenant.get('pid') or '-'}; {describe_tenant(tenant)}")
    return 0


if __name__ == "__main__":
    # The package's module, not this `__main__` copy, so the migration commands
    # and the supervisor share one set of classes and exceptions.
    from scripts.board_supervisor import main as package_main

    raise SystemExit(package_main())
