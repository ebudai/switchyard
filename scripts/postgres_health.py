"""Whether the host's running PostgreSQL server can still serve a board (SYRD-826).

On 2026-11-16 pacman upgraded postgresql and postgresql-libs from 18.6 to 18.7 while the 18.6 server ran on. Every new
backend that loaded plpgsql failed ("could not load library .../plpgsql.so: undefined symbol"), so the next deploy's
migration failed -- and its rollback restarted sapi's board, which reconnected and dropped every request until the
operator restarted PostgreSQL. All six boards on the host share that server. Two checks, both read-only; neither
restarts anything:

* `fresh_connection_problem` (root, before a deploy touches anything): a fresh connection, as the cluster's own
  account, runs one plpgsql block. That is exactly what a migration or a restarted board's first request does.
* `server_replaced` (anyone, `switchyard status`): the running server's binary was replaced on disk after it started
  -- `/proc/<pid>/exe` reads "(deleted)" where it can be read, else the binary named on its command line was put in
  place after the process started. That is its ctime: a package manager installs with the build date as the mtime
  (pacman: /usr/bin/postgres mtime 2026-11-14 19:21, its Build Date; ctime 2026-11-16 06:25, its Install Date).

Each answer names the one remedy: RESTART.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Callable

RESTART = "sudo systemctl restart postgresql"
UNIT = "postgresql.service"
POSTGRES_ACCOUNT = "postgres"
#: A fresh backend loads plpgsql for this, as a migration's first function does.
PROBE_SQL = "DO $$ BEGIN NULL; END $$;"
PROBE_DATABASE_URL = "postgresql:///postgres?host=/var/run/postgresql"
TIMEOUT_SECONDS = 30


def probe_command() -> list[str]:
    """What root runs: one plpgsql block in a fresh connection, as the cluster's account, by peer authentication."""
    return ["runuser", "-u", POSTGRES_ACCOUNT, "--", "env", "-i", "PATH=/usr/bin:/bin", "LANG=C.UTF-8",
            "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", PROBE_DATABASE_URL, "-c", PROBE_SQL]


def fresh_connection_problem(*, runner: Callable[..., Any] = subprocess.run) -> str:
    """'' when a fresh connection runs plpgsql; else why not, naming the restart."""
    try:
        done = runner(probe_command(), cwd="/", stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                      stderr=subprocess.PIPE, text=True, timeout=TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"PostgreSQL could not be asked to run plpgsql in a fresh connection ({exc}); check it first"
    if done.returncode == 0:
        return ""
    detail = (str(done.stderr or "").strip() or str(done.stdout or "").strip() or "no output").splitlines()[-1][:300]
    if "could not load library" in detail or "undefined symbol" in detail:
        return (f"the running PostgreSQL server cannot load its own installed libraries ({detail}): it was upgraded "
                f"on disk and is still the old binary. Restart it first: `{RESTART}`")
    return f"a fresh PostgreSQL connection cannot run plpgsql (exit {done.returncode}: {detail}); check it first"


def server_pid(*, runner: Callable[..., Any] = subprocess.run) -> int:
    """The running server's main pid, as systemd knows it; 0 when it does not say."""
    try:
        done = runner(["systemctl", "show", "--property=MainPID", "--value", UNIT], stdout=subprocess.PIPE,
                      stderr=subprocess.DEVNULL, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return 0
    value = str(done.stdout or "").strip()
    return int(value) if done.returncode == 0 and value.isdigit() else 0


def _started_at(pid: int, proc: Path) -> float | None:
    """The process's start, in seconds since the epoch, from /proc: readable for any process."""
    try:
        fields = (proc / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
        ticks = int(fields[19])  # field 22, starttime: clock ticks after boot
        boot = next(int(line.split()[1]) for line in (proc / "stat").read_text().splitlines()
                    if line.startswith("btime "))
        return boot + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def server_replaced(*, runner: Callable[..., Any] = subprocess.run, proc: Path = Path("/proc")) -> str:
    """'' unless the running PostgreSQL server's binary was replaced on disk after it started; then what to run."""
    pid = server_pid(runner=runner)
    if pid <= 0:
        return ""
    try:
        exe = os.readlink(proc / str(pid) / "exe")
    except OSError:
        exe = ""  # another account's process: its exe link is root's to read
    if exe.endswith(" (deleted)"):
        return (f"the running PostgreSQL server (pid {pid}) is a binary a package upgrade has since replaced "
                f"({exe}): a new connection may fail to load its libraries. Restart it: `{RESTART}`")
    try:
        binary = (proc / str(pid) / "cmdline").read_bytes().split(b"\0", 1)[0].decode("utf-8", "replace")
    except OSError:
        return ""
    started = _started_at(pid, proc)
    if not binary.startswith("/") or started is None:
        return ""
    try:
        info = os.stat(binary)
    except OSError:
        return ""
    # Installed, not built: ctime is when the file was put in place; mtime may be the package's build date (SYRD-826).
    if max(info.st_mtime, info.st_ctime) > started + 1:
        return (f"the running PostgreSQL server (pid {pid}) started before its binary {binary} was replaced: a new "
                f"connection may fail to load its libraries. Restart it: `{RESTART}`")
    return ""


def main(argv: list[str] | None = None) -> int:
    """`postgres_health.py --replaced`: say whether the running server predates its binary; exit 1 when it does.

    Stdlib only, so the prerequisites installer runs it with `python3 -I` from the source checkout (SYRD-828)."""
    import sys

    args = sys.argv[1:] if argv is None else argv
    if args != ["--replaced"]:
        print("usage: postgres_health.py --replaced", file=sys.stderr)
        return 2
    said = server_replaced()
    if said:
        print(said)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
