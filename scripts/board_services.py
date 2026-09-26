"""A tenant's board and listener services: who runs them, their systemd units, and starting and stopping them.

Each tenant has two kinds of long-running service:
- **The board**, a root-installed system unit run as a dedicated service
  user. `_ensure_board_service_user` and `_ensure_board_service_peer_auth`
  create that user and its database peer mapping; `board_service_user` and
  `_default_board_service_user` name it; `_board_system_unit_action` and
  `board_system_unit_is_active` drive and read the unit.
- **The notify listener**, a user unit in the tenant owner's own systemd
  user manager. `capture_listener_state`, `stop_owner_listener` and
  `start_owner_listener` drive it through `_run_owner_user_systemctl`.
  `owner_user_manager_state` tells a responding manager from a wedged or an
  unreachable one, and `repair_owner_user_manager` restarts a wedged one.

The units are named by `_board_system_unit`, `_listener_user_unit`,
`_canary_system_unit`, `_project_service_units` and `managed_unit_names`, and
located by `_owner_user_unit_path`. `authority_unit_installs`,
`activate_board_authority` and `restore_installed_units` install the
authority's units, activate them, and put back what was there if a cutover
fails.

Suspending, resuming, cutting over, upgrading and creating a tenant decide
WHEN these run; that stays in `scripts/team_launcher.py`, which calls them
through its own names. The suites patch several of those names on the
launcher (`capture_listener_state`, `board_system_unit_is_active`,
`_ensure_board_service_peer_auth`, and `OWNER_USER_MANAGER_RESTART_TIMEOUT_SECONDS`
by rebinding), so this module reads them there too, when a function runs.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-302). `team_launcher`
imports this module at its top and still exports every name callers read there.
This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig
    from scripts.ticket_board.project_provision import ProjectBoardProvision


def board_service_user(config: ProjectConfig) -> str:
    """The account the board service runs as, for operator command rendering."""
    from scripts.ticket_board.project_provision import DEFAULT_SERVICE_USER

    return DEFAULT_SERVICE_USER


def _non_login_shell_path() -> str:
    for candidate in (shutil.which("nologin"), "/usr/sbin/nologin", "/bin/false"):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    raise SystemExit(
        "switchyard: cannot create board service user because neither nologin nor /bin/false "
        "is executable; install util-linux or provide a non-login shell"
    )


def _ensure_board_service_user(
    service_user: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    result = runner(["getent", "passwd", service_user], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode == 0:
        return
    shell_path = _non_login_shell_path()
    create_result = runner(["useradd", "-r", "-M", "-d", "/nonexistent", "-s", shell_path, service_user])
    if create_result.returncode != 0:
        raise SystemExit(
            f"switchyard: failed to create board service user {service_user!r}; "
            "run scripts/ticket-board-boardsvc-setup.sh --apply or create a system account "
            f"with `sudo useradd -r -M -d /nonexistent -s {shell_path} {service_user}`"
        )


def _ensure_board_service_peer_auth(
    plan: ProjectBoardProvision,
    *,
    source_repo: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    from scripts import team_launcher as launcher

    setup_script = source_repo / "scripts" / "ticket-board-boardsvc-setup.sh"
    env = {
        **os.environ,
        "PG_DATABASE": plan.database,
        "PG_IDENT_MAP": launcher.DEFAULT_PG_IDENT_MAP,
        "SERVICE_USER": plan.service_user,
        "SERVICE_ROLE": plan.service_role,
    }
    try:
        result = runner([str(setup_script), "--apply-peer-auth"], env=env)
    except OSError as exc:
        raise SystemExit(
            "switchyard: failed to install PostgreSQL peer-auth mapping "
            f"for OS user {plan.service_user!r} to database role {plan.service_role!r} "
            f"on database {plan.database!r}; setup helper {setup_script} is not executable "
            "or is missing. Run scripts/ticket-board-boardsvc-setup.sh --apply or install "
            "the pg_hba/pg_ident mapping manually before provisioning"
        ) from exc
    if result.returncode != 0:
        stderr = str(getattr(result, "stderr", "") or "").strip()
        detail = f": {stderr}" if stderr else ""
        raise SystemExit(
            "switchyard: failed to install PostgreSQL peer-auth mapping "
            f"for OS user {plan.service_user!r} to database role {plan.service_role!r} "
            f"on database {plan.database!r}; run scripts/ticket-board-boardsvc-setup.sh --apply "
            "or install the pg_hba/pg_ident mapping manually before provisioning"
            f"{detail}"
        )


def _default_board_service_user() -> str:
    from scripts.ticket_board.project_provision import DEFAULT_SERVICE_USER

    return DEFAULT_SERVICE_USER


def _board_system_unit(config: ProjectConfig) -> str:
    return f"{config.project}-ticket-board.service"


def _listener_user_unit(config: ProjectConfig) -> str:
    return f"{config.project}-ticket-board-notify-listener.service"


def _canary_system_unit(config: ProjectConfig) -> str:
    return f"{config.project}-ticket-board-canary.service"


def _owner_user_unit_path(
    config: ProjectConfig, unit: str, *, config_path: Path | None = None
) -> Path:
    """Where the tenant owner's user units live.

    The listener is not a system unit: it runs in the owner's own user manager
    from the owner's home. Treating it as a system unit means installing and
    restarting something that is not there, while the listener that IS running
    carries on through the release and schema migration (SYRD-45).
    """
    from scripts import team_launcher as launcher

    return launcher._tenant_owner_home(config, config_path) / ".config" / "systemd" / "user" / unit


def _project_service_units(config: ProjectConfig) -> tuple[str, str]:
    return (_board_system_unit(config), _listener_user_unit(config))


OWNER_USER_MANAGER_TIMEOUT_SECONDS = 20.0


OWNER_USER_MANAGER_RESTART_TIMEOUT_SECONDS = 60.0


# The conventional exit status for "gave up waiting", as `timeout(1)` uses.
OWNER_USER_MANAGER_TIMED_OUT = 124


MANAGER_RESPONDING = "responding"


MANAGER_WEDGED = "wedged"


MANAGER_UNREACHABLE = "unreachable"


def _run_owner_user_systemctl(
    config: ProjectConfig,
    action: str,
    unit: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    config_path: Path | None = None,
    timeout: float = OWNER_USER_MANAGER_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[Any]:
    """Ask the owner's user manager something, and never wait on it forever.

    A wedged user manager answers nothing at all -- the incident behind this had
    one spinning at 97% of a core for hours, with every `systemctl --user` call
    against it hanging indefinitely. Unbounded, that hangs the identities
    transaction with the roles already stopped, which is the worst point in the
    upgrade to stop at (SYRD-54).
    """
    from scripts import team_launcher as launcher

    args = launcher._owner_user_systemctl(config, action, unit, config_path=config_path)
    try:
        return runner(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args,
            OWNER_USER_MANAGER_TIMED_OUT,
            "",
            f"the user manager did not answer within {timeout:g}s",
        )


def owner_user_manager_state(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    config_path: Path | None = None,
    timeout: float = OWNER_USER_MANAGER_TIMEOUT_SECONDS,
) -> tuple[str, str]:
    """Whether the owner's user manager can serve a request. Returns (state, detail).

    `is-system-running` is asked rather than anything about a unit: it is answered
    by the manager itself, so a manager that cannot answer is distinguishable from
    a unit that is simply not running.

    Only silence is `wedged`. A manager that is not there at all answers quickly
    with a bus error and is reported `unreachable` -- that is a tenant that has
    never had one, which starting it on demand fixes and a restart does not.
    """
    result = _run_owner_user_systemctl(
        config, "is-system-running", "", runner=runner, config_path=config_path, timeout=timeout
    )
    detail = (str(getattr(result, "stdout", "") or "").strip()
              or str(getattr(result, "stderr", "") or "").strip())
    if result.returncode == OWNER_USER_MANAGER_TIMED_OUT:
        return MANAGER_WEDGED, detail or "no answer"
    if result.returncode == 0:
        return MANAGER_RESPONDING, detail or "running"
    # `degraded` answers non-zero and is still a manager that answers.
    if detail and "connect" not in detail.casefold() and "bus" not in detail.casefold():
        return MANAGER_RESPONDING, detail
    return MANAGER_UNREACHABLE, detail or f"exit {result.returncode}"


def repair_owner_user_manager(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    config_path: Path | None = None,
    dry_run: bool = False,
    settle_timeout: float | None = None,
    print_func: Callable[[str], None] = print,
) -> list[str]:
    """Restart the owner's wedged user manager and prove it answers again.

    Only the system manager can restart `user@<uid>.service`, so this is root's
    step and is reported rather than attempted otherwise. It takes down that
    manager's own units -- the tenant's notify listener among them, which is why
    the listener is started again here and checked. It does not touch the role
    sessions: those are hosted by whatever started them, not by this manager.
    """
    from scripts import team_launcher as launcher

    owner = config.run_as_user or launcher.current_user_name()
    uid = launcher._uid_for_user(owner)
    if uid is None:
        return [f"cannot resolve a uid for {owner}, so its user manager cannot be named"]
    unit = f"user@{uid}.service"
    if dry_run:
        print_func(f"switchyard: would restart {unit} to recover {owner}'s wedged user manager")
        return []
    if os.geteuid() != 0:
        return [
            f"{owner}'s user manager is wedged and only root can restart {unit}; "
            f"run `sudo switchyard upgrade {config.project}`"
        ]
    print_func(f"switchyard: restarting {unit}: {owner}'s user manager is not answering")
    restart = runner(
        ["systemctl", "restart", unit],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if restart.returncode != 0:
        return [f"could not restart {unit} (exit {restart.returncode})"]
    # Read at call time so the wait is one knob, settable by a caller and by a
    # test that must not spend a minute proving a manager stayed wedged.
    settle = launcher.OWNER_USER_MANAGER_RESTART_TIMEOUT_SECONDS if settle_timeout is None else settle_timeout
    deadline = time.monotonic() + settle
    state, detail = owner_user_manager_state(config, runner=runner, config_path=config_path)
    while state != MANAGER_RESPONDING and time.monotonic() < deadline:
        time.sleep(1.0)
        state, detail = owner_user_manager_state(config, runner=runner, config_path=config_path)
    if state != MANAGER_RESPONDING:
        return [f"{unit} was restarted but {owner}'s user manager still does not answer ({detail})"]
    print_func(f"switchyard: {owner}'s user manager is answering again ({detail})")
    # The manager took its own units down with it, so the listener is started
    # again and its state read back rather than assumed -- which starting it now
    # does itself, so all this caller adds is where in the recovery it happened
    # (SYRD-61).
    problems = start_owner_listener(config, runner=runner, config_path=config_path)
    if problems:
        return [f"{problem} after the user manager restart" for problem in problems]
    print_func(f"switchyard: {_listener_user_unit(config)} is active again")
    return []


def capture_listener_state(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    config_path: Path | None = None,
) -> str:
    """Whether the owner's listener is running now, so it can be put back."""
    result = _run_owner_user_systemctl(
        config, "is-active", _listener_user_unit(config), runner=runner, config_path=config_path
    )
    if result.returncode == OWNER_USER_MANAGER_TIMED_OUT:
        # Not "inactive": nothing was learned, and reporting a guess here is what
        # would let the transaction proceed on it (SYRD-54).
        return MANAGER_WEDGED
    return str(result.stdout).strip() or ("active" if result.returncode == 0 else "inactive")


def stop_owner_listener(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    config_path: Path | None = None,
) -> list[str]:
    """Stop the owner's listener and prove it stopped.

    The release and its schema migration must not run with the old listener
    still reading the board: a stop that failed, or one that returned while the
    unit is still active, is a transaction failure before anything is deployed
    (SYRD-45).
    """
    from scripts import team_launcher as launcher

    unit = _listener_user_unit(config)
    result = _run_owner_user_systemctl(
        config, "stop", unit, runner=runner, config_path=config_path
    )
    if result.returncode == OWNER_USER_MANAGER_TIMED_OUT:
        return [f"{unit} could not be stopped: the owner's user manager is not answering"]
    if result.returncode != 0:
        return [f"could not stop {unit} (exit {result.returncode})"]
    state = launcher.capture_listener_state(config, runner=runner, config_path=config_path)
    if state == MANAGER_WEDGED:
        return [f"{unit} cannot be confirmed stopped: the owner's user manager is not answering"]
    if state == "active":
        return [f"{unit} is still active after being stopped"]
    return []


def start_owner_listener(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    config_path: Path | None = None,
) -> list[str]:
    """Start the owner's listener through the owner's own manager, and prove it ran.

    A zero exit from `restart` says the request was accepted, not that the unit
    is up: a listener that starts and immediately fails leaves the tenant with
    no notifications and an upgrade that believed it restored one. The state is
    read back from the same manager (SYRD-61).
    """
    from scripts import team_launcher as launcher

    unit = _listener_user_unit(config)
    result = _run_owner_user_systemctl(
        config, "restart", unit, runner=runner, config_path=config_path
    )
    if result.returncode == OWNER_USER_MANAGER_TIMED_OUT:
        return [f"could not start {unit}: the owner's user manager is not answering"]
    if result.returncode != 0:
        detail = (str(getattr(result, "stderr", "") or "").strip() or "no output")[:200]
        return [f"could not start {unit} (exit {result.returncode}): {detail}"]
    state = launcher.capture_listener_state(config, runner=runner, config_path=config_path)
    if state == MANAGER_WEDGED:
        return [f"{unit} cannot be confirmed running: the owner's user manager is not answering"]
    if state != "active":
        return [f"{unit} was started but is {state}"]
    return []


def authority_unit_installs(
    config: ProjectConfig, *, config_path: Path | None = None
) -> tuple[tuple[str, Path, list[str]], ...]:
    """Every generated unit this tenant installs, with where and as whom.

    One list, so what the identity transaction installs and what the printed
    operator sequence installs cannot drift apart -- the transaction installed
    two of the three, and the deploy it now runs starts the third (SYRD-63).
    """
    from scripts import team_launcher as launcher

    from scripts.ticket_board.project_provision import readable_system_unit_path

    owner = config.run_as_user or launcher.current_user_name()
    listener = _listener_user_unit(config)
    board_unit = _board_system_unit(config)
    return (
        (board_unit, launcher._installed_unit_path(board_unit), ["-o", "root", "-g", "root"]),
        (_canary_system_unit(config), launcher._installed_unit_path(_canary_system_unit(config)), ["-o", "root", "-g", "root"]),
        (
            listener,
            _owner_user_unit_path(config, listener, config_path=config_path),
            ["-o", owner, "-g", owner],
        ),
        # The same reviewed bytes again, where the unprivileged deployer can
        # read them. It runs as the project account and root's copy is in the
        # privileged provision directory, which is root-only -- so being handed
        # that path made a present, correct, byte-identical unit read as ABSENT
        # and refused the release (SYRD-126). In this list rather than a step of
        # its own, for the reason the list exists: what the identity transaction
        # installs and what the printed operator sequence installs must not
        # drift apart, and a stale copy here would be compared against a release
        # it did not come from (SYRD-127).
        (
            # Its own name in this list, not the unit's a second time. Both the
            # rollback snapshot and the restore key by the first element, so a
            # repeated name would make the copy overwrite the installed unit's
            # entry and the rollback would put back the wrong file -- which is
            # exactly what happened the first time this was written.
            f"{board_unit} (readable copy)",
            Path(readable_system_unit_path(config.project)),
            ["-o", "root", "-g", "root"],
        ),
    )


def activate_board_authority(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    restart: bool,
    print_func: Callable[[str], None] = print,
) -> list[str]:
    """Make the installed authority the one the board is actually serving.

    `restart` is false when the release deploy has just restarted the board
    itself, under the unit installed above and the binary it deployed: it
    smoke-checked the service, verified the live build id and checked the
    post-deploy runtime, and restarting again would throw all of that away for a
    weaker check. The state is still read back either way -- installing a unit
    is not the same fact as the board serving it (SYRD-63).
    """
    board_unit = _board_system_unit(config)
    if restart:
        result = runner(["systemctl", "restart", board_unit], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode != 0:
            return [f"could not restart {board_unit} (exit {result.returncode})"]
    health = runner(
        ["systemctl", "is-active", board_unit],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if health.returncode != 0 or str(health.stdout).strip() not in {"active", ""}:
        return [
            f"{board_unit} is not active after the restart "
            f"({str(health.stdout).strip() or health.returncode})"
        ]
    return []


def restore_installed_units(
    config: ProjectConfig,
    captured: Mapping[str, bytes | None],
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    config_path: Path | None = None,
    listener_state: str = "",
) -> list[str]:
    """Put the previously installed units back and restore what was running.

    Including whether the listener was running: starting one the tenant did not
    have is as wrong as leaving one stopped that it did (SYRD-45).
    """
    problems: list[str] = []
    board_unit = _board_system_unit(config)
    listener_unit = _listener_user_unit(config)
    destinations = {
        unit: path for unit, path, _ownership in authority_unit_installs(config, config_path=config_path)
    }
    for unit, body in captured.items():
        path = destinations.get(unit) or _owner_user_unit_path(config, unit, config_path=config_path)
        try:
            if body is None:
                if path.exists():
                    path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)
        except OSError as exc:
            problems.append(f"could not restore {unit}: {exc}")
    runner(["systemctl", "daemon-reload"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if runner(["systemctl", "restart", board_unit], stdout=subprocess.PIPE, stderr=subprocess.PIPE).returncode != 0:
        problems.append(f"could not restart {board_unit} after restoring it")
    if listener_unit in captured and listener_state:
        if listener_state == "active":
            problems.extend(start_owner_listener(config, runner=runner, config_path=config_path))
        else:
            problems.extend(stop_owner_listener(config, runner=runner, config_path=config_path))
    return problems


def managed_unit_names(config: ProjectConfig) -> frozenset[str]:
    """The units this project manages, which containment must never signal.

    A managed service is stopped through its own manager -- the board through
    its system unit, the listener through the owner's user manager -- and in the
    documented order. Signalling their processes directly stops them out of that
    order, hands systemd a main process that died on its own, and leaves the
    unit-scoped step that follows observing something already killed.
    """
    return frozenset({
        _board_system_unit(config),
        _canary_system_unit(config),
        _listener_user_unit(config),
    })


def _board_system_unit_action(
    config: ProjectConfig,
    action: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> list[str]:
    """Act on the tenant's board unit in the SYSTEM scope, and read the state back.

    The board is a system unit and the listener is the owner's user unit; asking
    the wrong manager is not a smaller mistake than asking the wrong host. A
    `systemctl --user stop` for the board stops nothing and returns cleanly,
    which is how a tenant is reported suspended with its board still serving
    (SYRD-193).
    """
    unit = _board_system_unit(config)
    result = runner(["systemctl", action, unit], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if getattr(result, "returncode", 1) != 0:
        detail = (str(getattr(result, "stderr", "") or "").strip() or "no output")[:200]
        return [f"could not {action} {unit} (exit {result.returncode}): {detail}"]
    state = runner(
        ["systemctl", "is-active", unit], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    observed = str(getattr(state, "stdout", "") or "").strip()
    wanted = {"stop": {"inactive", "failed", "deactivating"}, "start": {"active"}}[action]
    if observed and observed not in wanted:
        return [f"{unit} is {observed} after {action}"]
    return []


def board_system_unit_is_active(
    config: ProjectConfig, *, runner: Callable[..., subprocess.CompletedProcess[Any]]
) -> bool:
    result = runner(
        ["systemctl", "is-active", _board_system_unit(config)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    return str(getattr(result, "stdout", "") or "").strip() == "active"
