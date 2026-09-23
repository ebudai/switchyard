#!/usr/bin/env python3
"""A fresh host is left with a PostgreSQL cluster `switchyard new` can use.

Fresh-host reproduction during onboarding: the operator installed the packages
on an Arch-family host and ran `switchyard new`, which collected every
provisioning answer and then stopped at its database preflight with

    psql: error: connection to server on socket
    "/var/run/postgresql/.s.PGSQL.5432" failed: No such file or directory

`pacman -S postgresql` ships no cluster, so `postgresql.service` cannot start,
and nothing in the install path noticed (SYRD-235).

`scripts/ensure-postgres-cluster` is exercised as the installer runs it, with
`sudo`, `runuser`, `systemctl`, `initdb` and `psql` replaced by stubs on PATH
that keep their state in a real directory. The stubs simulate the host, never
the script: what is asserted is which commands it ran, in which state, and what
it left behind.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from scripts import team_launcher  # noqa: E402
from standalone_test_runner import run_module_tests  # noqa: E402

SCRIPT = ROOT / "scripts" / "ensure-postgres-cluster"
PREREQS = ROOT / "scripts" / "install-switchyard-prereqs"

SOCKET_ERROR = (
    'psql: error: connection to server on socket "/var/run/postgresql/.s.PGSQL.5432" '
    "failed: No such file or directory"
)

STUBS = {
    # Strips its own -n/-u options and runs the rest, so the script's own
    # privilege wrapper is what is under test.
    "sudo": """#!/bin/bash
args=()
while (($#)); do
  case "$1" in
    -n) ;;
    -u) shift ;;
    --) shift; break ;;
    *) break ;;
  esac
  shift
done
exec "$@"
""",
    "runuser": """#!/bin/bash
printf 'runuser %s\\n' "$*" >> "$PG_STUB_STATE/log"
while (($#)); do
  case "$1" in
    -u) shift ;;
    --) shift; break ;;
    *) break ;;
  esac
  shift
done
exec "$@"
""",
    "systemctl": """#!/bin/bash
printf 'systemctl %s\\n' "$*" >> "$PG_STUB_STATE/log"
if [[ "$1" == "show" ]]; then
  printf 'PGROOT=%s\\n' "$PG_STUB_STATE/pgroot"
  exit 0
fi
if [[ "$1" == "enable" || "$1" == "start" ]]; then
  if [[ -e "$PG_STUB_STATE/fail-start" ]]; then
    echo "Job for postgresql.service failed" >&2
    exit 1
  fi
  if [[ -f "$PG_STUB_STATE/pgroot/data/PG_VERSION" ]]; then
    : > "$PG_STUB_STATE/serving"
    exit 0
  fi
  echo "postgresql.service: could not start: no data directory" >&2
  exit 1
fi
exit 0
""",
    "psql": """#!/bin/bash
printf 'psql %s\\n' "$*" >> "$PG_STUB_STATE/log"
if [[ -e "$PG_STUB_STATE/serving" ]]; then
  echo 1
  exit 0
fi
echo 'psql: error: connection to server on socket "/var/run/postgresql/.s.PGSQL.5432" failed: No such file or directory' >&2
exit 2
""",
    "initdb": """#!/bin/bash
printf 'initdb %s\\n' "$*" >> "$PG_STUB_STATE/log"
data=""
while (($#)); do
  case "$1" in
    -D) shift; data="$1" ;;
  esac
  shift
done
[[ -n "$data" ]] || { echo "initdb: no -D" >&2; exit 1; }
[[ ! -f "$data/PG_VERSION" ]] || { echo "initdb: directory already a cluster: $data" >&2; exit 1; }
mkdir -p "$data"
printf '17\\n' > "$data/PG_VERSION"
exit 0
""",
}


class Host:
    """A host whose PostgreSQL state the script has to read for itself."""

    def __init__(self, *, manager: str = "pacman") -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="syrd235."))
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name, body in STUBS.items():
            path = self.bin / name
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)
        self.pgroot = self.state / "pgroot"
        self.manager = manager

    @property
    def data_dir(self) -> Path:
        return self.pgroot / "data"

    def with_cluster(self) -> "Host":
        self.data_dir.mkdir(parents=True)
        (self.data_dir / "PG_VERSION").write_text("17\n", encoding="utf-8")
        (self.data_dir / "postgresql.conf").write_text("# tenant config\n", encoding="utf-8")
        return self

    def with_empty_data_parent(self) -> "Host":
        self.pgroot.mkdir(parents=True)
        return self

    def serving(self) -> "Host":
        (self.state / "serving").touch()
        return self

    def start_fails(self) -> "Host":
        (self.state / "fail-start").touch()
        return self

    def run(self, *args: str, script: Path | None = None) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.tmp),
            "PG_STUB_STATE": str(self.state),
            "SWITCHYARD_PREREQS_ASSUME_MANAGER": self.manager,
            "SWITCHYARD_PREREQS_FORCE_SUDO_PREFIX": "1",
            "SWITCHYARD_SUDO_BIN": "sudo",
            "SWITCHYARD_PG_WAIT_SECONDS": "2",
        }
        return subprocess.run(
            [str(script or SCRIPT), *args], env=env, capture_output=True, text=True, timeout=120
        )

    @property
    def log(self) -> list[str]:
        path = self.state / "log"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def ran(self, program: str) -> list[str]:
        return [line for line in self.log if line.startswith(f"{program} ")]

    def close(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


def test_a_fresh_arch_package_gets_its_cluster_and_a_started_service() -> None:
    host = Host().with_empty_data_parent()
    try:
        result = host.run()
        assert result.returncode == 0, (result.stdout, result.stderr)
        # Initialized once, into the directory this host's own unit names.
        initdb = host.ran("initdb")
        assert len(initdb) == 1, host.log
        assert f"-D {host.data_dir}" in initdb[0], initdb
        assert (host.data_dir / "PG_VERSION").exists()
        assert any("enable --now postgresql.service" in line for line in host.ran("systemctl")), host.log
        # And proved with the connection switchyard new makes, not with the unit state.
        assert any("host=/var/run/postgresql" in line for line in host.ran("psql")), host.log
        assert "is serving /var/run/postgresql" in result.stdout, result.stdout
    finally:
        host.close()


def test_an_initialized_but_stopped_server_is_started_not_reinitialized() -> None:
    host = Host().with_cluster()
    try:
        before = (host.data_dir / "postgresql.conf").read_text(encoding="utf-8")
        result = host.run()
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert host.ran("initdb") == [], "an existing cluster was re-initialized"
        assert any("enable --now" in line for line in host.ran("systemctl")), host.log
        assert (host.data_dir / "postgresql.conf").read_text(encoding="utf-8") == before
    finally:
        host.close()


def test_a_running_server_is_left_completely_alone() -> None:
    host = Host().with_cluster().serving()
    try:
        result = host.run()
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert host.ran("initdb") == [], host.log
        assert [line for line in host.ran("systemctl") if "enable" in line or "start" in line] == []
        assert "already serving" in result.stdout, result.stdout
    finally:
        host.close()


def test_a_nonempty_directory_that_is_not_a_cluster_stops_the_run() -> None:
    host = Host()
    try:
        host.data_dir.mkdir(parents=True)
        (host.data_dir / "somebody-elses.tar").write_text("data\n", encoding="utf-8")
        result = host.run()
        assert result.returncode != 0, result.stdout
        assert str(host.data_dir) in result.stderr, result.stderr
        assert "no PG_VERSION" in result.stderr, result.stderr
        assert host.ran("initdb") == [], "initdb ran over a directory with contents"
        assert (host.data_dir / "somebody-elses.tar").read_text(encoding="utf-8") == "data\n"
        assert not (host.data_dir / "PG_VERSION").exists()
    finally:
        host.close()


def test_a_service_that_will_not_start_names_what_to_read_and_how_to_retry() -> None:
    host = Host().with_cluster().start_fails()
    try:
        result = host.run()
        assert result.returncode != 0, result.stdout
        for expected in (
            "systemctl status postgresql.service",
            "journalctl -u postgresql.service",
            "systemctl enable --now postgresql.service",
        ):
            assert expected in result.stderr, (expected, result.stderr)
        assert host.ran("initdb") == [], host.log
    finally:
        host.close()


def test_a_debian_host_is_never_given_a_second_cluster() -> None:
    """Without a cluster it starts the distro's service and then names its tools."""
    host = Host(manager="apt").with_empty_data_parent()
    try:
        result = host.run()
        assert result.returncode != 0, result.stdout
        assert host.ran("initdb") == [], "initdb ran on a distro-managed cluster host"
        assert any("enable --now" in line for line in host.ran("systemctl")), host.log
        assert "pg_createcluster" in result.stderr, result.stderr
        assert "pg_lsclusters" in result.stderr, result.stderr
        assert "systemctl enable --now postgresql.service" in result.stderr, result.stderr
        # Rendered, not evaluated: the operator gets a command to run, and this
        # script never ran pg_lsclusters itself.
        assert "$(pg_lsclusters" in result.stderr, result.stderr
    finally:
        host.close()


def test_a_debian_host_whose_cluster_is_merely_stopped_is_simply_started() -> None:
    host = Host(manager="apt").with_cluster()
    try:
        result = host.run()
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert host.ran("initdb") == [], host.log
        starts = [line for line in host.ran("systemctl") if "enable --now" in line]
        assert len(starts) == 1, host.log
        assert "is serving /var/run/postgresql" in result.stdout, result.stdout
    finally:
        host.close()


def test_a_dry_run_describes_the_repair_and_changes_nothing() -> None:
    host = Host().with_empty_data_parent()
    try:
        result = host.run("--dry-run")
        assert result.returncode == 0, (result.stdout, result.stderr)
        printed = [line for line in result.stdout.splitlines() if line.startswith("+ ")]
        assert any("initdb" in line and str(host.data_dir) in line for line in printed), printed
        assert any("enable --now postgresql.service" in line for line in printed), printed
        assert host.ran("initdb") == [], "the dry run initialized a cluster"
        assert not host.data_dir.exists(), "the dry run created the data directory"
    finally:
        host.close()


def test_a_dry_run_that_cannot_read_the_host_still_describes_the_procedure() -> None:
    """An unattended install asks what would happen; it must not be told it failed."""
    host = Host()
    try:
        # No data directory and no parent: the probe cannot tell what is there.
        result = host.run("--dry-run")
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert "initdb" in result.stderr, result.stderr
        assert "nothing was changed" in result.stdout, result.stdout
    finally:
        host.close()


def test_the_repair_is_idempotent() -> None:
    host = Host().with_empty_data_parent()
    try:
        first = host.run()
        assert first.returncode == 0, (first.stdout, first.stderr)
        second = host.run()
        assert second.returncode == 0, (second.stdout, second.stderr)
        assert len(host.ran("initdb")) == 1, host.log
        assert "already serving" in second.stdout, second.stdout
    finally:
        host.close()


def test_the_installer_runs_the_cluster_step_on_both_package_managers() -> None:
    """The step is the installer's, not a thing an operator has to know about."""
    for manager in ("apt", "pacman"):
        host = Host(manager=manager).with_cluster().serving()
        try:
            result = host.run("--dry-run", script=PREREQS)
            assert result.returncode == 0, (result.stdout, result.stderr)
            assert "Ensuring the local PostgreSQL cluster" in result.stdout, result.stdout
            assert "already serving" in result.stdout, result.stdout
        finally:
            host.close()


def _runner_for(states: dict[str, object]):
    """A systemctl/psql runner standing in for a host in a named state."""

    def runner(args, **_kwargs):
        argv = list(args)
        if argv[:2] == ["systemctl", "list-unit-files"]:
            installed = states.get("unit_installed", True)
            stdout = "postgresql.service enabled\n" if installed else "0 unit files listed.\n"
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")
        if argv[:2] == ["systemctl", "is-active"]:
            return subprocess.CompletedProcess(argv, 0 if states.get("active") else 3)
        if argv and argv[0] != "psql" and "psql" not in argv:
            # Only the database is down in these cases; everything else the
            # precheck asks about answers normally.
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 2, stdout="", stderr=SOCKET_ERROR)

    return runner


def test_an_uninstalled_server_is_named_as_the_missing_package() -> None:
    said = team_launcher.postgres_availability_remedy(runner=_runner_for({"unit_installed": False}))
    assert "no postgresql.service" in said, said
    assert "install-switchyard-prereqs" in said, said


def test_a_stopped_service_names_the_one_command_that_repairs_it() -> None:
    said = team_launcher.postgres_availability_remedy(runner=_runner_for({"active": False}))
    assert "not running" in said, said
    assert str(team_launcher.postgres_cluster_script()) in said, said
    assert "never re-initializes an existing cluster" in said, said
    assert team_launcher.postgres_cluster_script().exists(), "the remedy names a script that is not shipped"


def test_a_live_service_that_does_not_answer_is_not_blamed_on_the_cluster() -> None:
    said = team_launcher.postgres_availability_remedy(runner=_runner_for({"active": True}))
    assert "is active" in said, said
    assert "journalctl -u postgresql.service" in said, said


def test_the_preflight_names_the_remedy_instead_of_only_psqls_error() -> None:
    """What `switchyard new` prints, before it has created anything (SYRD-235)."""
    original = team_launcher.os.geteuid
    try:
        team_launcher.os.geteuid = lambda: 0  # type: ignore[method-assign]
        raised = ""
        try:
            team_launcher._database_exists("porter_board", runner=_runner_for({"active": False}))
        except SystemExit as exc:
            raised = str(exc)
    finally:
        team_launcher.os.geteuid = original  # type: ignore[method-assign]
    assert SOCKET_ERROR in raised, raised
    assert "postgresql.service is installed but not running" in raised, raised
    assert str(team_launcher.postgres_cluster_script()) in raised, raised
    assert "nothing was created" in raised, raised


def test_the_precheck_carries_that_remedy_before_any_tenant_exists() -> None:
    from scripts.ticket_board.project_provision import build_plan

    with tempfile.TemporaryDirectory(prefix="syrd235-precheck.") as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        repository = Path(tmp) / "repo"
        repository.mkdir()
        plan = build_plan(project="porter", project_name="Porter",
                          owner_user="porter-owner", owner_home=home, source_repo=ROOT)
        raised = ""
        try:
            team_launcher.precheck_new_project(
                plan, source_repo=ROOT, repository=repository,
                runner=_runner_for({"active": False}),
                socket_exists=lambda _path: False,
                require_owner_user=False,
                config_dir=Path(tmp) / "config", registry_dir=Path(tmp) / "registry",
            )
        except SystemExit as exc:
            raised = str(exc)
        assert "cannot verify PostgreSQL database availability" in raised, raised
        assert str(team_launcher.postgres_cluster_script()) in raised, raised
        # The preflight is read-only: it is what stands between a bad host and a
        # half-created tenant.
        assert not (home / "porter").exists()
        assert not (Path(tmp) / "config").exists()


def main() -> int:
    run_module_tests(globals())
    count = sum(1 for name in globals() if name.startswith("test_"))
    print(f"postgres_cluster_readiness_test: {count} tests ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
