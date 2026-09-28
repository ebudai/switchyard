"""What must already be true before a new project is created, checked before anything is.

`precheck_new_project` collects every reason a new project cannot be
provisioned yet, in order, and refuses with all of them at once: polkit is
not ready; the owner account or the repository does not exist; the deploy
source is not a clean checkout or an installed release
(`_precheck_deploy_source`, `_switchyard_release_source_error`,
`_looks_like_switchyard_release_tree`); the board's unit, database, socket or
port already belongs to something (`_system_unit_file_exists`,
`_database_exists`, `_ticket_board_table_count`, `_installed_unit_is_this_plans`,
with the `_path_exists` and `_tcp_port_in_use` probes); or the project is
already registered. When the database cannot be asked, `postgres_availability_remedy`
says how to bring the local cluster up (`POSTGRES_SERVICE_UNIT`,
`POSTGRES_ADMIN_SOCKET_DIR`, `postgres_cluster_script`).

Moved out of `scripts/team_launcher.py` unchanged (SYRD-421), in their original
order -- the two probes with them, because `precheck_new_project` binds both
as defaults when it is defined. The launcher imports this module and
re-exports all fourteen names; `new_project_command` and
`scripts/new_project_phases.py` still reach the precheck through the launcher.
Everything the fourteen read -- each other included, and the launcher's
config and registry directories, unit helpers, registry lookup, launcher name
and its own file, the release marker readers, the git status, the unit
renderer and the account lookup -- is read through the launcher at call time,
so a suite that rebinds one there still intercepts it. The defaults Python
binds at definition -- the two probes, `subprocess.run` and the polkit check --
are the same objects the launcher bound; the plan type is imported under
TYPE_CHECKING. This module imports `team_launcher` only inside the functions,
when they run.
"""

from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from scripts.polkit_readiness import polkit_readiness_problems

if TYPE_CHECKING:
    from scripts.ticket_board.project_provision import ProjectBoardProvision


def _path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def _tcp_port_in_use(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def _looks_like_switchyard_release_tree(path: Path) -> bool:
    from scripts import team_launcher as launcher

    return (
        (path / "switchyard").is_file()
        and (path / "scripts" / launcher.TEAM_LAUNCHER_NAME).is_file()
        and (path / "scripts" / "ticket-board-service.sh").is_file()
    )


def _switchyard_release_source_error(source_repo: Path) -> str:
    from scripts import team_launcher as launcher

    direct_marker = launcher._read_switchyard_release_marker(source_repo)
    if direct_marker is not None:
        if direct_marker.marker_error:
            return (
                f"deploy source {source_repo} has an invalid {launcher.SWITCHYARD_RELEASE_MARKER_NAME}: "
                f"{direct_marker.marker_error}"
            )
        if launcher._looks_like_switchyard_release_tree(source_repo):
            return ""
        return (
            f"deploy source {source_repo} has {launcher.SWITCHYARD_RELEASE_MARKER_NAME}, but is missing "
            "the expected exported launcher files"
        )

    shared_release = launcher.shared_switchyard_release_for_path(source_repo)
    if shared_release is None:
        return "not-release"
    if shared_release.marker_error:
        return (
            f"deploy source {source_repo} has an invalid shared release marker "
            f"at {shared_release.root}: {shared_release.marker_error}"
        )
    if shared_release.marker_commit or launcher._looks_like_switchyard_release_tree(source_repo):
        return ""
    return (
        f"deploy source {source_repo} is under the Switchyard shared install, but is missing "
        f"{launcher.SWITCHYARD_RELEASE_MARKER_NAME} and the expected exported launcher files"
    )


def _precheck_deploy_source(
    source_repo: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> list[str]:
    from scripts import team_launcher as launcher

    if not launcher._path_exists(source_repo):
        return [f"deploy source {source_repo} does not exist"]

    release_error = launcher._switchyard_release_source_error(source_repo)
    if release_error == "":
        return []
    if release_error != "not-release":
        return [release_error]

    has_git_metadata = (source_repo / ".git").exists()
    try:
        status = launcher._git_status_porcelain(source_repo, runner=runner)
    except SystemExit:
        if has_git_metadata:
            raise
        return [
            f"deploy source {source_repo} is neither a git checkout nor a Switchyard release; "
            f"expected a clean Switchyard source checkout, or an exported release with "
            f"{launcher.SWITCHYARD_RELEASE_MARKER_NAME}"
        ]
    if status.strip():
        return [f"deploy checkout {source_repo} has uncommitted changes"]
    return []


def _system_unit_file_exists(unit: str, *, runner: Callable[..., subprocess.CompletedProcess[Any]]) -> bool:
    from scripts import team_launcher as launcher

    try:
        result = runner(
            ["systemctl", "list-unit-files", "--no-legend", unit],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError:
        return launcher._path_exists(Path("/etc/systemd/system") / unit)
    if result.returncode != 0:
        return launcher._path_exists(Path("/etc/systemd/system") / unit)
    stdout = str(getattr(result, "stdout", "") or "").strip()
    return bool(stdout and not stdout.startswith("0 unit files listed"))


#: The unit every generated board unit already declares `Wants=`, and the
#: socket directory the generated connection strings use.
POSTGRES_SERVICE_UNIT = "postgresql.service"
POSTGRES_ADMIN_SOCKET_DIR = "/var/run/postgresql"


def postgres_cluster_script() -> Path:
    """The helper that initializes or starts the local cluster, in this tree."""
    from scripts import team_launcher as launcher

    return Path(launcher.__file__).resolve().parent / "ensure-postgres-cluster"


def postgres_availability_remedy(
    *, runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run
) -> str:
    """Why the local cluster did not answer, and the command that repairs it.

    A fresh Arch-family host installs the PostgreSQL package without a cluster:
    the service cannot start, and the first thing to notice was this preflight,
    which printed only psql's `No such file or directory` for the socket after
    every provisioning answer had been collected (SYRD-235). The raw error says
    what failed and nothing about what to do, so the state is read here --
    package, service, socket -- and the matching repair is named.
    """
    from scripts import team_launcher as launcher

    script = launcher.postgres_cluster_script()
    lines: list[str] = []
    if not launcher._system_unit_file_exists(launcher.POSTGRES_SERVICE_UNIT, runner=runner):
        lines.append(
            f"this host has no {launcher.POSTGRES_SERVICE_UNIT}, so no PostgreSQL server is installed."
        )
        lines.append("  install the host packages first: sudo scripts/install-switchyard-prereqs")
        return "\n".join(lines)
    if not launcher._system_unit_is_active(launcher.POSTGRES_SERVICE_UNIT, runner=runner):
        lines.append(
            f"{launcher.POSTGRES_SERVICE_UNIT} is installed but not running, so nothing is serving "
            f"{launcher.POSTGRES_ADMIN_SOCKET_DIR}."
        )
        lines.append(
            f"  initialize the cluster if this host has none, then start the service: sudo {script}"
        )
        lines.append(
            f"  it is idempotent, never re-initializes an existing cluster, and verifies the same "
            f"socket this check uses."
        )
        return "\n".join(lines)
    lines.append(
        f"{launcher.POSTGRES_SERVICE_UNIT} is active, but the admin connection over "
        f"{launcher.POSTGRES_ADMIN_SOCKET_DIR} did not answer."
    )
    lines.append(f"  read why: sudo systemctl status {launcher.POSTGRES_SERVICE_UNIT} --no-pager")
    lines.append(f"  and: sudo journalctl -u {launcher.POSTGRES_SERVICE_UNIT} -n 50 --no-pager")
    lines.append(f"  then re-verify the socket: sudo {script}")
    return "\n".join(lines)


def _database_exists(database: str, *, runner: Callable[..., subprocess.CompletedProcess[Any]]) -> bool:
    from scripts import team_launcher as launcher

    escaped = database.replace("'", "''")
    command = [
        "psql",
        "-XAt",
        "postgresql:///postgres?host=/var/run/postgresql",
        "-c",
        f"SELECT 1 FROM pg_database WHERE datname = '{escaped}'",
    ]
    if os.geteuid() == 0:
        command = ["sudo", "-u", "postgres", *command]
    try:
        result = runner(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise SystemExit(
            f"team-launcher: cannot verify PostgreSQL database availability: {exc}\n"
            f"team-launcher: {launcher.postgres_availability_remedy(runner=runner)}\n"
            "team-launcher: nothing was created; re-run `switchyard new` once that answers."
        ) from exc
    if result.returncode != 0:
        stderr = str(getattr(result, "stderr", "") or "").strip()
        detail = f": {stderr}" if stderr else ""
        raise SystemExit(
            f"team-launcher: cannot verify PostgreSQL database availability{detail}\n"
            f"team-launcher: {launcher.postgres_availability_remedy(runner=runner)}\n"
            "team-launcher: nothing was created; re-run `switchyard new` once that answers."
        )
    return str(getattr(result, "stdout", "") or "").strip() == "1"


def _ticket_board_table_count(database: str, *, runner: Callable[..., subprocess.CompletedProcess[Any]]) -> int:
    command = [
        "psql",
        "-XAt",
        f"postgresql:///{database}?host=/var/run/postgresql",
        "-c",
        "SELECT count(*)::int FROM pg_catalog.pg_tables WHERE schemaname = 'ticket_board'",
    ]
    if os.geteuid() == 0:
        command = ["sudo", "-u", "postgres", *command]
    try:
        result = runner(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise SystemExit(f"team-launcher: cannot inspect PostgreSQL database {database!r}: {exc}") from exc
    if result.returncode != 0:
        stderr = str(getattr(result, "stderr", "") or "").strip()
        detail = f": {stderr}" if stderr else ""
        raise SystemExit(f"team-launcher: cannot inspect PostgreSQL database {database!r}{detail}")
    raw_count = str(getattr(result, "stdout", "") or "").strip()
    try:
        return int(raw_count)
    except ValueError as exc:
        raise SystemExit(f"team-launcher: cannot parse ticket_board table count for database {database!r}: {raw_count!r}") from exc


def _installed_unit_is_this_plans(plan: ProjectBoardProvision) -> bool:
    """Whether the installed board unit is exactly the one this plan renders.

    The packet installs the units before it creates the database, so a run that
    stopped between the two leaves precisely this unit and no database. That is
    this provisioning, half done, and running it again finishes it. A unit
    that says anything else is somebody else's state, and stays refused.
    """
    from scripts import team_launcher as launcher

    try:
        installed = launcher._installed_unit_path(plan.board_unit).read_text(encoding="utf-8")
    except OSError:
        return False
    return installed == launcher.render_board_unit(plan)


def precheck_new_project(
    plan: ProjectBoardProvision,
    *,
    source_repo: Path,
    repository: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    port_in_use: Callable[[int], bool] = _tcp_port_in_use,
    socket_exists: Callable[[Path], bool] = _path_exists,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
    require_owner_user: bool = True,
    require_repository: bool = True,
    polkit_problems: Callable[..., list[str]] = polkit_readiness_problems,
) -> None:
    from scripts import team_launcher as launcher

    errors: list[str] = list(polkit_problems(runner=runner))
    if require_owner_user and launcher.uid_for_user(plan.owner_user) is None:
        errors.append(f"target user {plan.owner_user!r} does not exist")
    if require_repository and not launcher._path_exists(repository):
        errors.append(f"project repository {repository} does not exist")
    errors.extend(launcher._precheck_deploy_source(source_repo, runner=runner))
    unit_exists = launcher._system_unit_file_exists(plan.board_unit, runner=runner)
    database_exists = launcher._database_exists(plan.database, runner=runner)
    socket_path = Path(plan.socket_path)
    socket_is_present = socket_exists(socket_path)
    port_is_live = port_in_use(plan.port)
    if not unit_exists:
        if database_exists:
            errors.append(f"database {plan.database!r} already exists but {plan.board_unit} is not installed")
        if socket_is_present:
            errors.append(f"socket {plan.socket_path} already exists but {plan.board_unit} is not installed")
        if port_is_live:
            errors.append(f"port {plan.port} is already in use but {plan.board_unit} is not installed")
    elif not database_exists:
        # Exactly this plan's unit and no database is this provisioning, stopped
        # between installing its units and creating its database; running it
        # again completes it. Anything else is not ours to build over (SYRD-261).
        if not launcher._installed_unit_is_this_plans(plan):
            errors.append(
                f"{plan.board_unit} is installed but database {plan.database!r} does not exist, "
                "and the installed unit is not the one this provisioning would install.\n"
                f"  to inspect recovery: switchyard teardown {plan.project} --dry-run"
            )
    else:
        table_count = launcher._ticket_board_table_count(plan.database, runner=runner)
        if table_count > 0:
            launch_entry, broken_entries = launcher._usable_switchyard_entry_for_project(
                plan.project,
                config_dir=config_dir,
                registry_dir=registry_dir,
            )
            if launch_entry is not None:
                errors.append(
                    f"project {plan.project!r} is already provisioned "
                    f"(database {plan.database} has {table_count} ticket_board tables, {plan.board_unit} is installed).\n"
                    f"  to launch it:      switchyard {plan.project}\n"
                    f"  to start over:     switchyard teardown {plan.project} --dry-run"
                )
            else:
                effective_config_dir = config_dir or launcher.DEFAULT_CONFIG_DIR
                effective_registry_dir = registry_dir or launcher.switchyard_registry_dir()
                broken_entry_detail = f"  unusable launch entry: {broken_entries[0]}\n" if broken_entries else ""
                errors.append(
                    f"project {plan.project!r} is partially provisioned but not registered "
                    f"(database {plan.database} has {table_count} ticket_board tables, "
                    f"{plan.board_unit} is installed, but no usable launch entry exists in "
                    f"{effective_config_dir} or {effective_registry_dir}).\n"
                    f"{broken_entry_detail}"
                    f"  to inspect recovery: switchyard teardown {plan.project} --dry-run"
                )
    if errors:
        raise SystemExit("team-launcher: new project precheck failed:\n- " + "\n- ".join(errors))
