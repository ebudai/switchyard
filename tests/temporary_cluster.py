#!/usr/bin/env python3
"""A PostgreSQL test cluster that cannot outlive the test process (SYRD-54).

The board's Postgres-backed tests each start a cluster with `pg_ctl start` and
stop it in a `finally`. That covers a passing run and an assertion failure and
nothing else: `pg_ctl` daemonises the postmaster, so a run that is interrupted --
Ctrl-C, a harness timeout, an OOM kill -- leaves a live cluster and its data
directory behind. One was found idle in /tmp after 3.6 hours, holding a port and
its shared buffers, with nothing left that knew it existed.

This starts the postmaster as a direct child instead, with PR_SET_PDEATHSIG, so
the kernel takes it down when the test process dies however it dies. The
`finally` and the signal handlers are still there -- they shut the cluster down
cleanly and remove the tree -- but nothing depends on them running.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

PR_SET_PDEATHSIG = 1
STARTUP_TIMEOUT_SECONDS = 30.0
SHUTDOWN_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True)
class Cluster:
    """Where a running test cluster listens, and what it lives in."""

    root: Path
    data_dir: Path
    socket_dir: Path
    port: int
    pid: int
    log_path: Path

    def conninfo(self, dbname: str, user: str = "postgres") -> str:
        return f"host={self.socket_dir} port={self.port} dbname={dbname} user={user}"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _die_with_parent() -> None:
    """Ask the kernel to signal this child when its parent dies.

    Set in the child between fork and exec. The parent pid is re-read afterwards
    because a parent that died in that window would leave the request armed
    against a death that already happened.
    """
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    if libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0) != 0:
        os._exit(127)
    if os.getppid() == 1:
        os._exit(127)


def _running(pid: int) -> bool:
    """Whether a pid names a live process. Tolerates a pid file we did not write."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OverflowError, ValueError):
        # Not a pid this kernel could ever have issued, so it names nothing.
        return False
    return True


def _log_tail(log_path: Path, lines: int = 20) -> str:
    """What the postmaster said. `pg_ctl -w start` used to print this itself."""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        return f"(no postgres log: {exc})"
    if not text:
        return "(the postgres log is empty)"
    return "\n".join(text.splitlines()[-lines:])


def _await_ready(
    cluster_socket: Path,
    port: int,
    child: subprocess.Popen[bytes],
    deadline: float,
    log_path: Path,
) -> None:
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise AssertionError(
                f"postgres exited during startup with {child.returncode}:\n{_log_tail(log_path)}"
            )
        ready = subprocess.run(
            ["pg_isready", "-h", str(cluster_socket), "-p", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if ready.returncode == 0:
            return
        time.sleep(0.1)
    raise AssertionError(f"postgres did not become ready:\n{_log_tail(log_path)}")


# What `pg_ctl -m <mode> stop` sends the postmaster, which is what a fixture is
# choosing when it names a mode: fast rolls back open transactions and exits,
# immediate skips the shutdown checkpoint. Kept as a knob because a fixture that
# deliberately leaves a connection open asks for the second one.
SHUTDOWN_SIGNALS = {"fast": signal.SIGINT, "immediate": signal.SIGQUIT}


def _shut_down(child: subprocess.Popen[bytes], *, shutdown: str = "fast") -> None:
    """The named shutdown, then immediate, then the kernel. Never leaves it running."""
    if child.poll() is not None:
        return
    first = SHUTDOWN_SIGNALS.get(shutdown, signal.SIGINT)
    for sig, wait in ((first, SHUTDOWN_TIMEOUT_SECONDS), (signal.SIGQUIT, 5.0), (signal.SIGKILL, 5.0)):
        try:
            child.send_signal(sig)
        except ProcessLookupError:
            return
        try:
            child.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


STALE_TREE_AGE_SECONDS = 3600.0


def sweep_stale_trees(prefix: str, *, base: Path | None = None, age: float = STALE_TREE_AGE_SECONDS) -> list[Path]:
    """Remove trees a SIGKILLed run left behind, and return what was removed.

    A kill -9 takes the cluster down through PR_SET_PDEATHSIG but nothing is left
    to remove the directory, so the next run does it. Deliberately narrow: the
    name must match this prefix, the tree must contain a cluster this module
    created, its recorded postmaster must not be running, and it must be old
    enough that a run starting right now cannot be inside it.
    """
    removed: list[Path] = []
    root = base or Path(tempfile.gettempdir())
    now = time.time()
    for candidate in sorted(root.glob(f"{prefix}*")):
        data_dir = candidate / "pgdata"
        if not candidate.is_dir() or candidate.is_symlink() or not (data_dir / "PG_VERSION").is_file():
            continue
        try:
            if now - candidate.stat().st_mtime < age:
                continue
        except OSError:
            continue
        try:
            recorded = int((data_dir / "postmaster.pid").read_text(encoding="utf-8").split("\n", 1)[0])
        except (OSError, ValueError):
            recorded = 0
        if recorded and _running(recorded):
            continue
        shutil.rmtree(candidate, ignore_errors=True)
        removed.append(candidate)
    return removed


@contextmanager
def temporary_cluster(
    *,
    prefix: str,
    initdb_args: tuple[str, ...] = ("--username=postgres",),
    shutdown: str = "fast",
):
    """Run a throwaway cluster for the body, and leave nothing behind.

    Cleanup runs on success, on an assertion failure, and on SIGINT or SIGTERM --
    the last by restoring the previous handler and re-raising, so a harness that
    interrupts the run still sees the interrupt it sent.
    """
    sweep_stale_trees(prefix)
    root = Path(tempfile.mkdtemp(prefix=prefix))
    data_dir = root / "pgdata"
    socket_dir = root / "socket"
    socket_dir.mkdir()
    port = free_port()
    subprocess.run(
        ["initdb", "-D", str(data_dir), "-A", "trust", "--no-locale", *initdb_args],
        check=True,
        capture_output=True,
        text=True,
    )
    # Kept rather than discarded: `pg_ctl -w start` reported a refusal to start on
    # its own, and a fixture that cannot say why its cluster died is worse than
    # one that leaks (SYRD-56).
    log_path = root / "postgres.log"
    log_handle = log_path.open("wb")
    try:
        child = subprocess.Popen(
            [
                "postgres",
                "-D",
                str(data_dir),
                "-k",
                str(socket_dir),
                "-p",
                str(port),
                "-h",
                "",
            ],
            stdout=log_handle,
            stderr=log_handle,
            preexec_fn=_die_with_parent,  # noqa: PLW1509 - the point of this module
        )
    finally:
        log_handle.close()

    previous: dict[int, object] = {}

    def _interrupted(signum, frame):  # pragma: no cover - exercised by subprocess
        _shut_down(child, shutdown=shutdown)
        shutil.rmtree(root, ignore_errors=True)
        handler = previous.get(signum)
        signal.signal(signum, handler if handler is not None else signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, _interrupted)
        except ValueError:  # pragma: no cover - not the main thread
            previous.pop(signum, None)

    try:
        _await_ready(
            socket_dir, port, child, time.monotonic() + STARTUP_TIMEOUT_SECONDS, log_path
        )
        yield Cluster(
            root=root,
            data_dir=data_dir,
            socket_dir=socket_dir,
            port=port,
            pid=child.pid,
            log_path=log_path,
        )
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        _shut_down(child, shutdown=shutdown)
        shutil.rmtree(root, ignore_errors=True)


def _sleep_forever_with_a_cluster() -> int:  # pragma: no cover - run as a subprocess
    """Helper entry point: start a cluster, announce it, and wait to be killed."""
    with temporary_cluster(prefix="temporary-cluster-selftest.") as cluster:
        print(f"{cluster.pid} {cluster.root} {cluster.port}", flush=True)
        while True:
            time.sleep(3600)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_sleep_forever_with_a_cluster())
