#!/usr/bin/env python3
"""SYRD-642: a fresh host chooses PostgreSQL, Oracle or both before anything is installed.

What is proven here, with the real ./install and install-switchyard-prereqs
over stubbed system commands (each stub only records that it was called):
* The question comes first. While `./install` waits for the answer in a real
  terminal, nothing has run. A wrong answer is asked again.
* Oracle alone installs no PostgreSQL. No PostgreSQL server or client package
  and no psycopg are installed; no cluster is initialised; nothing of
  postgresql.service is enabled, started or probed. Its one database
  dependency, python-oracledb, goes into the shared venv. The record says
  oracle.
* The noninteractive equivalents: --databases, --databases=,
  SWITCHYARD_INSTALL_DATABASES. With nothing to ask and nothing given, a host
  keeps what it recorded, or gets PostgreSQL as before. An invalid value
  stops before anything runs.
* `--add-database postgresql` adds PostgreSQL later without installing a
  Switchyard release, and the record keeps both.
* The dry runs of every selection, on apt and pacman, say the same.
* `switchyard new` on an Oracle-only host refuses before asking or probing
  anything. The remedy it prints runs.
* The Oracle test container is test-only: no production installer, deploy or
  provisioning path names it.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install"
PREREQS = ROOT / "scripts" / "install-switchyard-prereqs"
sys.path.insert(0, str(ROOT))

CHECKS = 0
#: What an Oracle-only install must never run or name in a command.
POSTGRES_TOKENS = ("postgresql", "postgres ", "psycopg", "initdb", "psql", "pg_isready", "ensure-postgres-cluster",
                   "systemctl")


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def executed(output: str) -> list[str]:
    return [line for line in output.splitlines() if line.startswith("+ ")]


def stub(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)


class Host:
    """A temporary 'host': stubbed system commands, a venv path, a record path, and a log of every call."""

    def __init__(self, root: Path):
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir()
        self.log = root / "calls.log"
        self.log.touch()
        self.venv = root / "opt" / "switchyard" / "venv"
        self.record = root / "etc" / "switchyard" / "databases.json"
        real_python = shutil.which("python3")
        logged = 'printf "%s\\n" "$(basename "$0") $*" >> "$STUB_LOG"\n'
        for name in ("pacman", "apt-get", "apt-cache", "systemctl", "runuser", "initdb", "psql", "pg_isready",
                     "install", "sudo"):
            stub(self.bin / name, logged + ("exec \"$@\"\n" if name == "sudo" else "exit 0\n"))
        # python3 makes the venv it is asked for (with a python that records and, for the capability record,
        # really runs), and records everything else.
        stub(self.bin / "python3", logged + f'''if [[ "$1" == -m && "$2" == venv ]]; then
    dir="${{@: -1}}"; mkdir -p "$dir/bin"
    cat > "$dir/bin/python" <<'PY'
#!/usr/bin/env bash
printf "%s\\n" "venv-python $*" >> "$STUB_LOG"
if [[ "$1" == */database_capabilities.py ]]; then exec {real_python} "$@"; fi
exit 0
PY
    chmod 755 "$dir/bin/python"
fi
if [[ "$1" == */database_capabilities.py ]]; then exec {real_python} "$@"; fi
exit 0
''')
        self.release = root / "fake-install-switchyard"
        stub(self.release, 'printf "%s\\n" "release $*" >> "$STUB_LOG"\n')

    def env(self, **extra: str) -> dict[str, str]:
        base = {k: v for k, v in os.environ.items() if not k.startswith("SWITCHYARD_")}
        return {**base, "PATH": f"{self.bin}{os.pathsep}/usr/bin:/bin", "STUB_LOG": str(self.log),
                "SWITCHYARD_INSTALL_TEST_ASSUME_ROOT": "1", "SWITCHYARD_PREREQS_TEST_ASSUME_ROOT": "1",
                "SWITCHYARD_PREREQS_ASSUME_MANAGER": "pacman", "SWITCHYARD_PYTHON_VENV": str(self.venv),
                "SWITCHYARD_PYTHON_BIN": "python3", "SWITCHYARD_DATABASE_RECORD": str(self.record),
                "SWITCHYARD_INSTALL_PATH": str(self.root / "usr" / "local" / "bin" / "switchyard"),
                "SWITCHYARD_INSTALL_SWITCHYARD_SCRIPT": str(self.release),
                "SWITCHYARD_SHARED_INSTALL_ROOT": str(self.root / "opt" / "switchyard"), **extra}

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()

    def recorded(self) -> list[str] | None:
        return json.loads(self.record.read_text())["databases"] if self.record.exists() else None

    def run(self, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(INSTALL), *args], cwd=ROOT, env=self.env(**extra), capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=120)


def oracle_only(calls: list[str]) -> list[str]:
    """Every call an Oracle-only install made that touches PostgreSQL (there must be none)."""
    return [c for c in calls if any(token in c + " " for token in POSTGRES_TOKENS)]


def interactive_selection() -> None:
    """In a terminal: the question first, with nothing run yet; a wrong answer asked again; Oracle installed."""
    import fcntl
    import termios

    with tempfile.TemporaryDirectory() as tmp:
        host = Host(Path(tmp))
        master, slave = os.openpty()
        proc = subprocess.Popen([str(INSTALL)], cwd=ROOT, env=host.env(), stdin=slave, stdout=slave, stderr=slave,
                                start_new_session=True, preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
        os.close(slave)
        seen = b""

        def until(text: str) -> None:
            nonlocal seen
            deadline = time.monotonic() + 60
            while text.encode() not in seen and time.monotonic() < deadline:
                if select.select([master], [], [], 0.5)[0]:
                    try:
                        seen += os.read(master, 4096)
                    except OSError:
                        break
            check(text.encode() in seen, f"the installer showed {text!r}: {seen.decode(errors='replace')[-800:]}")

        try:
            until("Choose 1, 2 or 3 [1]: ")
            check(host.calls() == [] and not host.venv.exists() and host.recorded() is None,
                  f"while the database question waits, nothing has run: {host.calls()}")
            check(b"Which databases should ticket boards on this host use?" in seen
                  and b"1) PostgreSQL" in seen and b"2) Oracle" in seen and b"3) Both" in seen,
                  "the question offers PostgreSQL, Oracle and both")
            os.write(master, b"mysql\n")
            until("Answer 1, 2 or 3.")
            check(host.calls() == [], "a wrong answer is asked again, still before anything runs")
            os.write(master, b"2\n")
            until("PostgreSQL can be added later")
            proc.wait(60)
        finally:
            if proc.poll() is None:
                proc.kill()
            os.close(master)
        calls = host.calls()
        check(proc.returncode == 0 and oracle_only(calls) == [],
              f"answering Oracle installs, starts and probes nothing of PostgreSQL: {oracle_only(calls)}")
        pacman = [c for c in calls if c.startswith("pacman ")]
        check(pacman == ["pacman -S --needed python tmux git curl python-pillow acl polkit"],
              f"the packages are Switchyard's own, with no PostgreSQL server or psycopg: {pacman}")
        check(any(c.startswith("venv-python -m pip install oracledb") for c in calls)
              and any(c.startswith("venv-python -c import oracledb") for c in calls),
              f"python-oracledb goes into the shared venv and is imported there: {calls}")
        check(host.recorded() == ["oracle"] and any(c.startswith("release --apply") for c in calls),
              f"the host records oracle, and Switchyard is installed: {host.recorded()}")
        check(b"This host supports Oracle boards only" in seen and b"--add-database postgresql" in seen,
              "the next step says how PostgreSQL is added later")

        # Later, with no terminal and no option: the host keeps what it has, rather than gaining PostgreSQL.
        host.log.write_text("")
        again = host.run()
        check(again.returncode == 0 and "keeping what this host was installed for (oracle)" in again.stdout
              and oracle_only(host.calls()) == [], f"a later unattended install keeps Oracle only: {again.stdout[-600:]}")

        # PostgreSQL added on demand: no Switchyard release installed again, and the record keeps both.
        host.log.write_text("")
        added = host.run("--add-database", "postgresql",
                         SWITCHYARD_INSTALL_PREREQS_SCRIPT=str(fake_prereqs(host)))
        calls = host.calls()
        check(added.returncode == 0 and calls == ["prereqs --databases postgresql"]
              and "added postgresql support; Switchyard itself was not reinstalled" in added.stdout,
              f"--add-database runs the prerequisites for PostgreSQL only, and installs no release: {calls}")


def fake_prereqs(host: Host) -> Path:
    """A prerequisites stand-in that records its arguments and then records the capability, as the real one does."""
    path = host.root / "fake-prereqs"
    stub(path, f'''printf "%s\\n" "prereqs $*" >> "$STUB_LOG"
{shutil.which("python3")} {ROOT / "scripts" / "database_capabilities.py"} record --databases "${{@: -1}}" >/dev/null
''')
    return path


def noninteractive() -> None:
    """The options say the same as the question; a bad value stops before anything; no answer keeps PostgreSQL."""
    for args, extra, want in ((("--databases", "oracle"), {}, "oracle"), (("--databases=both",), {}, "both"),
                              ((), {"SWITCHYARD_INSTALL_DATABASES": "Oracle"}, "oracle"),
                              ((), {}, "postgresql")):
        with tempfile.TemporaryDirectory() as tmp:
            host = Host(Path(tmp))
            done = host.run(*args, SWITCHYARD_INSTALL_PREREQS_SCRIPT=str(fake_prereqs(host)), **extra)
            check(done.returncode == 0 and [c for c in host.calls() if c.startswith("prereqs")]
                  == [f"prereqs --databases {want}"]
                  and "Which databases" not in done.stdout,
                  f"{args or extra or 'nothing given'}: prerequisites for {want}, nothing asked: {done.stdout[-400:]}")
    with tempfile.TemporaryDirectory() as tmp:
        host = Host(Path(tmp))
        done = host.run("--databases", "postgresql")
        calls = host.calls()
        check(any(c.startswith("pacman -S --needed python postgresql tmux") and "python-psycopg" in c for c in calls),
              f"PostgreSQL brings its server and psycopg, as before: {calls[:3]}")
    for bad in (("--databases", "mysql"), ("--add-database", "both"), ("--databases", "")):
        with tempfile.TemporaryDirectory() as tmp:
            host = Host(Path(tmp))
            done = host.run(*bad)
            check(done.returncode != 0 and host.calls() == [] and host.recorded() is None,
                  f"{bad}: refused before anything runs: {done.stderr.strip()}")


def dry_runs() -> None:
    """Every selection's dry run, on both package managers."""
    for manager in ("apt", "pacman"):
        for selection in ("postgresql", "oracle", "both"):
            env = {**{k: v for k, v in os.environ.items() if not k.startswith("SWITCHYARD_")},
                   "SWITCHYARD_PREREQS_ASSUME_MANAGER": manager, "SWITCHYARD_SUDO_BIN": "sudo",
                   "SWITCHYARD_PREREQS_FORCE_SUDO_PREFIX": "1", "SWITCHYARD_PYTHON_VENV": "/opt/switchyard/venv",
                   "SWITCHYARD_INSTALL_PATH": "/nonexistent"}
            out = subprocess.run([str(PREREQS), "--dry-run", "--databases", selection], cwd=ROOT, env=env,
                                 capture_output=True, text=True, timeout=60).stdout
            commands = executed(out)
            touched = [c for c in commands if any(t in c + " " for t in POSTGRES_TOKENS if t != "postgres ")]
            oracle = [c for c in commands if "oracledb" in c]
            if selection == "oracle":
                check(touched == [] and "postgresql.service" not in out and len(oracle) == 2,
                      f"{manager}/oracle: no PostgreSQL in any command, python-oracledb installed and imported: "
                      f"{touched}")
            else:
                check(any("postgresql" in c for c in commands) and any("psycopg" in c for c in commands),
                      f"{manager}/{selection}: PostgreSQL's packages and psycopg: {commands}")
                check(bool(oracle) == (selection == "both"), f"{manager}/{selection}: python-oracledb only with Oracle")
            check(commands[-1].endswith(f"database_capabilities.py record --databases {selection}"),
                  f"{manager}/{selection}: the selection is recorded last, once its dependencies are in: {commands[-1]}")
            check("docker" not in out and "gvenzl" not in out and "oracle-free" not in out,
                  f"{manager}/{selection}: no Oracle test container in production installation")


def capability_record() -> None:
    """The record: parsed strictly, only ever added to, legacy hosts read as PostgreSQL, links refused."""
    from scripts import database_capabilities as dc

    check([dc.parse_selection(v) for v in ("postgres", "ORACLE", "both", "oracle,postgresql")]
          == [("postgresql",), ("oracle",), ("postgresql", "oracle"), ("postgresql", "oracle")],
          "selections parse to capabilities")
    for bad in ("", "mysql", "both,mysql", "both,oracle"):
        try:
            dc.parse_selection(bad)
            check(False, f"{bad!r} accepted")
        except dc.CapabilityError:
            pass
    with tempfile.TemporaryDirectory() as tmp:
        record = Path(tmp) / "etc" / "databases.json"
        check(dc.installed(record) == ("postgresql",), "a host with no record has PostgreSQL, as it always did")
        check(dc.record(("oracle",), path=record) == ("oracle",) and dc.installed(record) == ("oracle",)
              and oct(record.stat().st_mode & 0o777) == "0o644", "a fresh Oracle-only host records oracle, 0644")
        check(dc.record(("postgresql",), path=record) == ("postgresql", "oracle"), "adding PostgreSQL keeps Oracle")
        check(dc.record(("oracle",), path=record) == ("postgresql", "oracle"), "a narrower selection forgets nothing")
        legacy = Path(tmp) / "legacy.json"
        check(dc.record(("oracle",), path=legacy, prior_install=True) == ("postgresql", "oracle"),
              "a host installed before the record existed keeps PostgreSQL when it adds Oracle")
        check(dc.new_project_refusal(record) == "", "a host with PostgreSQL may provision")
        link = Path(tmp) / "link.json"
        link.symlink_to(record)
        check("must be a regular file" in dc.new_project_refusal(link), "a linked record is refused, not followed")
        only = Path(tmp) / "only.json"
        dc.record(("oracle",), path=only)
        refusal = dc.new_project_refusal(only)
        check("installed for the Oracle database only" in refusal and f"sudo {dc.INSTALLER} --add-database postgresql"
              in refusal, f"an Oracle-only host is told how to add PostgreSQL: {refusal}")
        # The printed remedy runs: the very command, as a dry run, without sudo.
        remedy = refusal.split("sudo ", 1)[1].split(". ", 1)[0].split()
        done = subprocess.run([*remedy, "--dry-run"], cwd=ROOT, capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL,
                              env={**os.environ, "SWITCHYARD_PREREQS_ASSUME_MANAGER": "pacman",
                                   "SWITCHYARD_DATABASE_RECORD": str(only)})
        check(done.returncode == 0 and "--databases postgresql" in done.stdout
              and "installs no Switchyard release" in done.stdout, f"the printed remedy runs: {done.stderr[-300:]}")


def switchyard_new_refuses() -> None:
    """`switchyard new` on an Oracle-only host: refused at the dispatch, before the command asks or probes anything."""
    import scripts.team_launcher as launcher
    from scripts.switchyard_dispatch import switchyard_main

    reached: list[object] = []
    original = launcher.switchyard_new_command
    launcher.switchyard_new_command = lambda **kw: reached.append(kw) or 0
    previous = os.environ.get("SWITCHYARD_DATABASE_RECORD")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            outcomes = []
            for databases in (["oracle"], ["postgresql", "oracle"]):
                record = Path(tmp) / "databases.json"
                record.write_text(json.dumps({"databases": databases}))
                os.environ["SWITCHYARD_DATABASE_RECORD"] = str(record)
                try:
                    outcomes.append(("returned", switchyard_main(["new", "--headless"])))
                except SystemExit as exc:
                    outcomes.append(("refused", str(exc)))
    finally:
        launcher.switchyard_new_command = original
        if previous is None:
            os.environ.pop("SWITCHYARD_DATABASE_RECORD", None)
        else:
            os.environ["SWITCHYARD_DATABASE_RECORD"] = previous
    # An Oracle-only host creates the board on Oracle (SYRD-642): without a declared workflow it stops first.
    check(outcomes[0][0] == "refused"
          and outcomes[0][1].startswith("switchyard new: an Oracle board runs a declared workflow")
          and outcomes[1] == ("returned", 0) and len(reached) == 1,
          f"on an Oracle-only host, a board without a declared workflow is refused before the command runs; a host "
          f"with PostgreSQL goes on: {outcomes}")


def oracle_cli_finds_its_driver() -> None:
    """ticket-board-oracle runs under the shared venv, where python-oracledb is, when the system Python lacks it.

    A real venv: its python is a link to the system one, so only its prefix tells it apart. A stand-in oracledb in
    it says, when imported, that the CLI ran there.
    """
    with tempfile.TemporaryDirectory() as tmp:
        venv = Path(tmp) / "venv"
        subprocess.run(["python3", "-m", "venv", "--without-pip", str(venv)], check=True, timeout=120)
        site = next(venv.glob("lib/python*/site-packages"))
        (site / "oracledb.py").write_text("import sys\nprint('oracledb imported in', sys.prefix)\n")
        system_has_it = subprocess.run(["python3", "-c", "import oracledb"], capture_output=True).returncode == 0
        done = subprocess.run(["python3", str(ROOT / "scripts" / "ticket-board-oracle"), "teardown", "--config",
                               "/nonexistent"], cwd=ROOT, capture_output=True, text=True, timeout=60,
                              env={**os.environ, "SWITCHYARD_PYTHON_VENV": str(venv)})
        if system_has_it:
            check(f"oracledb imported in {venv}" not in done.stdout, "with python-oracledb here, the CLI runs where it is")
        else:
            check(done.stdout.startswith(f"oracledb imported in {venv}") and "cannot be read as JSON" in done.stderr,
                  f"without python-oracledb here, the CLI runs again under the shared venv, once: {done}")


def container_is_test_only() -> None:
    """No production path names the Oracle test container, its image or its script."""
    names = ("oracle-free-test-service", "gvenzl", "syrd642-oracle-free", "oracle_test_credentials")
    found = subprocess.run(["git", "grep", "-l", "-e", names[0], "-e", names[1], "-e", names[2], "-e", names[3],
                            "--", ".", ":!tests/", ":!docs/"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    check(found == [], f"only tests and docs name the Oracle test container: {found}")


def main() -> int:
    capability_record()
    dry_runs()
    noninteractive()
    interactive_selection()
    switchyard_new_refuses()
    oracle_cli_finds_its_driver()
    container_is_test_only()
    print(f"install_database_selection_test: ok ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
