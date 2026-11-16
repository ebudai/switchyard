#!/usr/bin/env python3
"""SYRD-826: whether the host's running PostgreSQL server can still serve a board, asked without touching it.

On 2026-11-16 pacman upgraded postgresql and postgresql-libs under the running 18.6 server; every new backend that
loaded plpgsql failed, a deploy's migration failed, and its rollback restarted sapi's board into an outage. Here,
with stand-ins only -- never this host's server, and never its /proc:
* the deploy's probe is one plpgsql block, as the cluster's account, by peer authentication over the socket; a
  library that will not load is named as such, with `sudo systemctl restart postgresql`; any other failure is said
  without claiming that cause;
* `switchyard status`'s check finds a server whose binary a package replaced after it started: "(deleted)" on its exe
  link, or, where that link is not this account's to read, a binary put in place after the process started -- by its
  ctime, since a package installs with the build date as the mtime (pacman); and says nothing when the binary was in
  place before the process started, or there is no server;
* the status prints the warning, and carries it in --json, only when there is one, and a check that fails costs the
  status nothing.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import postgres_health  # noqa: E402

CHECKS = 0
LIBRARY_ERROR = ('ERROR:  could not load library "/usr/lib/postgresql/plpgsql.so": /usr/lib/postgresql/plpgsql.so: '
                 "undefined symbol: heap_datum_update_type")


def check(condition: object, detail: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")


def test_the_deploy_probe() -> None:
    calls: list[tuple[list[str], dict]] = []

    def answering(code: int, stderr: str = ""):
        def runner(argv, **kwargs):
            calls.append((list(argv), kwargs))
            return SimpleNamespace(returncode=code, stdout="", stderr=stderr)
        return runner

    check(postgres_health.fresh_connection_problem(runner=answering(0)) == "", "a server that runs plpgsql: nothing")
    argv, kwargs = calls[-1]
    check(argv[:6] == ["runuser", "-u", "postgres", "--", "env", "-i"] and argv[-2:] == ["-c", "DO $$ BEGIN NULL; END $$;"]
          and "postgresql:///postgres?host=/var/run/postgresql" in argv and kwargs.get("cwd") == "/"
          and kwargs.get("stdin") is subprocess.DEVNULL and kwargs.get("timeout"),
          f"one plpgsql block in a fresh connection, as the cluster's account, over the socket, bounded: {argv} {kwargs}")
    said = postgres_health.fresh_connection_problem(runner=answering(3, LIBRARY_ERROR))
    check("cannot load its own installed libraries" in said and "undefined symbol" in said
          and "`sudo systemctl restart postgresql`" in said,
          f"sapi's exact failure: named, with the one remedy: {said}")
    said = postgres_health.fresh_connection_problem(runner=answering(2, "psql: error: connection to server failed"))
    check("cannot run plpgsql" in said and "connection to server failed" in said and "restart postgresql" not in said,
          f"another failure is said as itself, without claiming an upgrade: {said}")

    def broken(argv, **kwargs):
        raise FileNotFoundError("runuser")
    said = postgres_health.fresh_connection_problem(runner=broken)
    check("could not be asked" in said, f"a probe that cannot run is a problem, not a pass: {said}")


def fake_proc(base: Path, pid: int, *, binary: Path, exe: str | None, started_ago: float) -> Path:
    """A /proc for one process: its stat (start in clock ticks after boot), cmdline, and optionally its exe link."""
    proc = base / "proc"
    (proc / str(pid)).mkdir(parents=True)
    tick = os.sysconf("SC_CLK_TCK")
    boot = int(time.time()) - 100000
    started = time.time() - started_ago
    ticks = int((started - boot) * tick)
    (proc / "stat").write_text(f"cpu  1 2 3\nbtime {boot}\n")
    (proc / str(pid) / "stat").write_text(f"{pid} (postgres) S 1 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 {ticks} 0 0\n")
    (proc / str(pid) / "cmdline").write_bytes(str(binary).encode() + b"\0-D\0/var/lib/postgres/data\0")
    if exe is not None:
        os.symlink(exe, proc / str(pid) / "exe")
    return proc


def systemd_says(pid: str):
    def runner(argv, **_kwargs):
        assert argv[:2] == ["systemctl", "show"] and "postgresql.service" in argv, argv
        return SimpleNamespace(returncode=0, stdout=f"{pid}\n", stderr="")
    return runner


def test_the_status_check() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd826.") as raw:
        base = Path(raw)
        binary = base / "postgres"
        binary.write_text("binary")

        proc = fake_proc(base / "a", 4242, binary=binary, exe="/usr/bin/postgres (deleted)", started_ago=3600)
        said = postgres_health.server_replaced(runner=systemd_says("4242"), proc=proc)
        check("pid 4242" in said and "(deleted)" in said and "`sudo systemctl restart postgresql`" in said,
              f"an exe link reading (deleted): replaced, and the restart: {said}")

        proc = fake_proc(base / "b", 4242, binary=binary, exe=None, started_ago=3600)  # another account's exe link
        os.utime(binary, (time.time(), time.time()))  # the package replaced it after the server started
        said = postgres_health.server_replaced(runner=systemd_says("4242"), proc=proc)
        check(f"started before its binary {binary} was replaced" in said and "restart postgresql" in said,
              f"no readable exe link: a binary newer than the process is replaced: {said}")

        # pacman: the package was built two days ago (its mtime), installed just now (its ctime), and the server has
        # run for a day -- restarted after the build, before the install. It is as stale as any (Audit, SYRD-826).
        built = time.time() - 2 * 86400
        installed = base / "postgres-installed"
        installed.write_text("binary")
        os.utime(installed, (built, built))
        proc = fake_proc(base / "c", 4242, binary=installed, exe=None, started_ago=86400)
        said = postgres_health.server_replaced(runner=systemd_says("4242"), proc=proc)
        check(f"started before its binary {installed} was replaced" in said,
              f"a package built before the server started but installed after it: replaced: {said}")

        # In place before the server started, however old its build date: nothing to say. (os.utime itself sets the
        # ctime, so the process here starts after the file is written and dated.)
        settled = base / "postgres-settled"
        settled.write_text("binary")
        os.utime(settled, (built, built))
        proc = fake_proc(base / "d", 4242, binary=settled, exe=None, started_ago=-120)
        check(postgres_health.server_replaced(runner=systemd_says("4242"), proc=proc) == "",
              "a binary in place before the process started: nothing to say")
        check(postgres_health.server_replaced(runner=systemd_says("0"), proc=proc) == "",
              "no server running: nothing to say")


def test_the_status_says_it() -> None:
    from scripts import project_status

    with tempfile.TemporaryDirectory(prefix="syrd826s.") as raw:
        empty = Path(raw)
        (empty / "config").mkdir()
        (empty / "registry").mkdir()
        real = postgres_health.server_replaced
        try:
            for warning in ("the running PostgreSQL server (pid 9) is replaced. Restart it: `sudo systemctl restart "
                            "postgresql`", ""):
                postgres_health.server_replaced = lambda *, runner, _w=warning: _w
                lines: list[str] = []
                project_status.switchyard_status_command(config_dir=empty / "config", registry_dir=empty / "registry",
                                                         runner=lambda *a, **k: SimpleNamespace(returncode=1, stdout="",
                                                                                                stderr=""),
                                                         print_func=lines.append)
                out: list[str] = []
                project_status.switchyard_status_command(config_dir=empty / "config", registry_dir=empty / "registry",
                                                         json_output=True, print_func=out.append,
                                                         runner=lambda *a, **k: SimpleNamespace(returncode=1, stdout="",
                                                                                                stderr=""))
                payload = json.loads(out[-1])
                if warning:
                    check(f"switchyard: WARNING: {warning}" in lines and payload.get("postgres_warning") == warning,
                          f"the warning is printed, and carried in --json: {lines} {payload}")
                else:
                    check(not any("WARNING" in line for line in lines) and "postgres_warning" not in payload,
                          f"and nothing when there is none: {lines} {payload}")

            def failing(*, runner):
                raise RuntimeError("the check itself broke")
            postgres_health.server_replaced = failing
            lines = []
            try:
                code = project_status.switchyard_status_command(
                    config_dir=empty / "config", registry_dir=empty / "registry", print_func=lines.append,
                    runner=lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr=""))
            except Exception as exc:  # a crash is the failure this checks for, not a test error
                code = f"raised {type(exc).__name__}: {exc}"
            check(code == 0 and lines and lines[0].startswith("NAME"),
                  f"a check that fails costs the status nothing: {code} {lines}")
        finally:
            postgres_health.server_replaced = real


def main() -> int:
    test_the_deploy_probe()
    test_the_status_check()
    test_the_status_says_it()
    print(f"postgres_health_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
