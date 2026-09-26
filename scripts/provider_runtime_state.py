"""What a role's provider state and runtime registration say: generation stamps, and waiting for runtimes to register.

**Provider state.** A role's runtime started before its provider was signed
in cannot see that sign-in. So Switchyard stamps each role with the
generation of provider state it started on:
- `provider_state_generation` reads the current generation;
- `record_provider_state_generation` and
  `recorded_provider_state_generation` write and read the stamp, as the
  account that owns the store;
- `provider_state_store_problem` and `unreadable_provider_state_roles`
  diagnose a store that cannot be read or written.

**Runtime registration.** After a launch, the board's runtime assignments
show which roles registered:
- `read_runtime_assignments` and `read_runtime_assignment_details` read them;
- `await_runtime_registration` waits, bounded by
  `RUNTIME_REGISTRATION_TIMEOUT_SECONDS` and polling every
  `RUNTIME_REGISTRATION_POLL_SECONDS`, and returns a
  `RuntimeRegistrationWait`.

The privilege drop these writes use is `scripts/account_drop.py`, imported
here directly. Role session directories, CLI names, JSON helpers and
provider-setup checks stay in `scripts/team_launcher.py`. This module reads
them from there when a function runs, so the suites' patches on the launcher
reach it. Cutting roles over to new identities is not here.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-306). `team_launcher`
imports this module at its top and still exports every name callers read there.
This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

from scripts.account_drop import _drop_to_account, _run_as_account

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig, RoleConfig


#: What a role's runtime was started against, recorded beside its resumable
#: state so the answer survives this process (SYRD-191).
PROVIDER_STATE_RECORD_NAME = "provider-state.json"


def provider_state_generation(cli: str, *, owner_home: Path) -> str:
    """A digest of the provider state a runtime has to have been started after.

    Deliberately made of decisions, not of bytes or timestamps: whether the
    account holds a credential at all, whether its own first run is complete,
    and which directories it trusts. A token refresh rewrites `auth.json` and
    moves its mtime without changing any of those, so it produces the same
    generation and restarts nobody -- which is the trap an mtime trigger falls
    into. Completing a first run, or trusting a worktree, changes it once.
    """
    from scripts import team_launcher as launcher

    facts: dict[str, Any] = {"cli": cli}
    for artifact in launcher.ROLE_CREDENTIAL_ARTIFACTS.get(cli, ()):  # existence, never contents
        facts[artifact.relative_path] = (owner_home / artifact.relative_path).exists()
    if cli in launcher.FIRST_RUN_SETUP_CLIS:
        facts["account_setup"] = launcher._provider_account_setup_complete(cli, owner_home=owner_home)
    if cli == "claude":
        config = launcher._read_json_object(owner_home / ".claude.json")
        projects = config.get("projects")
        facts["trusted"] = sorted(
            path
            for path, entry in (projects or {}).items()
            if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True
        ) if isinstance(projects, dict) else []
    if cli == "agy":
        settings = launcher._read_json_object(owner_home / ".gemini" / "antigravity-cli" / "settings.json")
        trusted = settings.get("trustedWorkspaces")
        facts["trusted"] = sorted(str(path) for path in trusted) if isinstance(trusted, list) else []
    return hashlib.sha256(
        json.dumps(facts, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _provider_state_record_path(config: ProjectConfig, role: RoleConfig) -> Path:
    # Named for the role as well as placed under its store: a tenant that has
    # not crossed to per-role session directories keeps every role's record in
    # one directory, and a shared file would answer for all of them.
    from scripts import team_launcher as launcher

    return (
        launcher.role_session_dir(config, role).expanduser() / f"{role.role}.{PROVIDER_STATE_RECORD_NAME}"
    )


def recorded_provider_state_generation(config: ProjectConfig, role: RoleConfig) -> str:
    from scripts import team_launcher as launcher

    record = launcher._read_json_object(_provider_state_record_path(config, role))
    return str(record.get("generation") or "")


def _provider_state_store_account(
    config: ProjectConfig, *, geteuid: Callable[[], int] = os.geteuid
) -> object | None:
    """The account the record is actually written as, or None for this process.

    Root does not write there itself -- `record_provider_state_generation` drops
    to the project owner first -- so root asking whether the store works would
    get its own answer, which is yes for a directory the owner cannot write.
    """
    owner = str(config.run_as_user or "").strip()
    if geteuid() != 0 or not owner or owner == "root":
        return None
    return pwd.getpwnam(owner)


def provider_state_store_problem(
    config: ProjectConfig,
    role: RoleConfig,
    *,
    geteuid: Callable[[], int] = os.geteuid,
    drop: Callable[[int, int], None] = _drop_to_account,
) -> str:
    """Why this role's provider-state record cannot be read or written, or "".

    Absent is not a problem: nothing has been recorded yet, and one restart
    settles that. A record that cannot be READ, or a store that cannot be
    WRITTEN, is a different answer entirely -- it means the comparison below
    cannot be made and its result cannot be kept, so a restart would end a live
    pane and change nothing, and the next launch would do it again.

    Both questions are asked as the account that does the writing, which is the
    project owner even when root is running the launch.

    Live on mefp: `<session dir>/roles/main` and `.../roles/ops` were root's
    after a root-run launch created them, the project account got `permission
    denied` on both, and two panes that had been up for hours were killed and
    brought back (SYRD-233 live UAT).
    """
    from scripts import team_launcher as launcher

    path = _provider_state_record_path(config, role)
    directory = path.parent
    probe = directory if directory.exists() else _nearest_existing_parent(directory)
    try:
        account = _provider_state_store_account(config, geteuid=geteuid)
    except KeyError:
        return (
            f"this role's provider state is written as {config.run_as_user}, which is not an "
            "account on this host, so it cannot be recorded"
        )

    def readable() -> None:
        path.read_text(encoding="utf-8")

    def writable() -> None:
        if probe is None or not os.access(probe, os.W_OK | os.X_OK):
            raise PermissionError(str(probe))

    def ask(question: Callable[[], None]) -> bool:
        if account is None:
            try:
                question()
            except OSError:
                return False
            return True
        return _run_as_account(
            account.pw_uid, account.pw_gid, question, geteuid=geteuid, drop=drop
        )

    who = account.pw_name if account is not None else launcher.current_user_name()
    if path.exists() and not ask(readable):
        return f"{path} cannot be read by {who}"
    if not ask(writable):
        owner = _path_owner_name(probe) if probe is not None else ""
        belongs = f", which belongs to {owner}" if owner else ""
        return (
            f"{directory} cannot be written by {who}{belongs}, so this role's "
            "provider state cannot be recorded"
        )
    return ""


def _nearest_existing_parent(path: Path) -> Path | None:
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return None


def _path_owner_name(path: Path) -> str:
    try:
        return pwd.getpwuid(path.stat().st_uid).pw_name
    except (OSError, KeyError):
        return ""


def record_provider_state_generation(
    config: ProjectConfig,
    role: RoleConfig,
    generation: str,
    *,
    geteuid: Callable[[], int] = os.geteuid,
    drop: Callable[[int, int], None] = _drop_to_account,
) -> None:
    """Remember what this role's runtime was started against.

    Written after the restart rather than before it, so a run that fails to
    restart leaves the role still marked stale and the next ordinary launch
    tries again.

    Written AS THE TENANT OWNER. `switchyard new` launches as root, and this
    used to write in-process: its `mkdir` created `pane-sessions/roles/<role>/`
    root-owned, and from then on every write the owner made there -- this record
    on the next launch, and each Claude and Codex session hook -- failed with
    Permission denied. test12 lost its Claude session records that way
    (SYRD-221 UAT).
    """
    from scripts import team_launcher as launcher

    path = _provider_state_record_path(config, role)
    record = {
        "schema": "switchyard.provider-state.v1",
        "cli": launcher._role_cli_name(role),
        "generation": generation,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }

    def write() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        launcher._write_json_atomic(path, record)

    owner = str(config.run_as_user or "").strip()
    if geteuid() == 0 and owner and owner != "root":
        try:
            entry = pwd.getpwnam(owner)
        except KeyError:
            print(
                f"team-launcher: could not record {role.role}'s provider state generation: "
                f"its owner {owner} is not an account on this host",
                file=sys.stderr,
            )
            return
        if not _run_as_account(entry.pw_uid, entry.pw_gid, write, geteuid=geteuid, drop=drop):
            print(
                f"team-launcher: could not record {role.role}'s provider state generation "
                f"as {owner} in {path.parent}",
                file=sys.stderr,
            )
        return
    try:
        write()
    except OSError as exc:
        print(
            f"team-launcher: could not record {role.role}'s provider state generation: {exc}",
            file=sys.stderr,
        )


def unreadable_provider_state_roles(
    config: ProjectConfig, running_roles: Sequence[RoleConfig]
) -> list[tuple[RoleConfig, str]]:
    """Live roles whose provider-state store cannot be read or written, and why."""
    from scripts import team_launcher as launcher

    found: list[tuple[RoleConfig, str]] = []
    for role in running_roles:
        if not launcher._role_cli_name(role):
            continue
        problem = provider_state_store_problem(config, role)
        if problem:
            found.append((role, problem))
    return found


#: How long a role that has just been started may take to register its runtime
#: with the board before anything calls it missing. Registration is the pane's
#: own asynchronous work -- the role's CLI starts, `ticket-board-register-runtime`
#: announces it, and the board records the assignment -- so a check that samples
#: the moment the launcher returns is asking before the answer exists. Testing
#: journal 0032 failed on one role that registered seconds later, and 0037 on
#: all five (SYRD-162).
RUNTIME_REGISTRATION_TIMEOUT_SECONDS = 90.0


RUNTIME_REGISTRATION_POLL_SECONDS = 2.0


def read_runtime_assignments(
    config: ProjectConfig,
    *,
    connection_factory: Callable[[str, float], Any] | None = None,
) -> tuple[set[str], str]:
    """Which roles the running board currently holds a runtime assignment for.

    Read over the board's own socket, and from the same atomic rows write
    authority and notifications use, so this is the board's answer rather than
    an inference from what the launcher just did.
    """
    try:
        from scripts.ticket_board.write_client import UnixHTTPConnection

        factory = connection_factory or (
            lambda socket_path, timeout: UnixHTTPConnection(socket_path, timeout=timeout)
        )
        connection = factory(config.board_socket, 3)
        try:
            connection.request("GET", "/api/runtime-assignments")
            response = connection.getresponse()
            body = response.read().decode("utf-8", errors="replace")
        finally:
            connection.close()
        if response.status != 200:
            return set(), f"the board answered HTTP {response.status} for its runtime assignments"
        payload = json.loads(body)
    except Exception as exc:  # noqa: BLE001 - any failure to read is "cannot say"
        return set(), f"the board's runtime assignments could not be read: {exc}"
    if not isinstance(payload, dict) or payload.get("project") != config.project:
        return set(), "the runtime assignment response belongs to another project"
    assignments = payload.get("assignments")
    if not isinstance(assignments, dict):
        return set(), "the runtime assignment response has no assignments object"
    return {
        str(name)
        for name, assignment in assignments.items()
        if isinstance(assignment, dict) and str(assignment.get("actual_target") or "").strip()
    }, ""


@dataclass(frozen=True)
class RuntimeRegistrationWait:
    """What a bounded wait for runtime registration ended up finding."""

    missing: tuple[str, ...] = ()
    exited: tuple[str, ...] = ()
    problem: str = ""
    waited_seconds: float = 0.0

    @property
    def registered(self) -> bool:
        return not self.missing and not self.exited and not self.problem


def await_runtime_registration(
    config: ProjectConfig,
    roles: Sequence[RoleConfig] | None = None,
    *,
    read: Callable[[], tuple[set[str], str]] | None = None,
    alive: Callable[[RoleConfig], bool] | None = None,
    timeout_seconds: float = RUNTIME_REGISTRATION_TIMEOUT_SECONDS,
    poll_seconds: float = RUNTIME_REGISTRATION_POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    print_func: Callable[[str], None] = print,
) -> RuntimeRegistrationWait:
    """Wait, within a bound, for configured roles to register their runtimes.

    Bounded so a role that never registers is still an answer; observable so
    the wait is not silence; and short-circuited on a session that has exited,
    because a pane that is gone will not register no matter how long anyone
    waits and saying so immediately is more useful than the full timeout.

    Nothing here restarts, clears or re-registers anything. The registration
    belongs to the pane, and this only decides when to stop asking.
    """
    selected = tuple(roles if roles is not None else config.roles)
    if not selected:
        return RuntimeRegistrationWait()
    reader = read or (lambda: read_runtime_assignments(config))
    started = monotonic()
    deadline = started + max(0.0, timeout_seconds)
    announced = False
    while True:
        registered, problem = reader()
        missing = [role for role in selected if role.role not in registered]
        if not missing and not problem:
            return RuntimeRegistrationWait(waited_seconds=monotonic() - started)
        exited = [role for role in missing if alive is not None and not alive(role)]
        if exited:
            # Promptly: the answer will not change, and the operator needs the
            # name of the session that died rather than a minute of waiting.
            return RuntimeRegistrationWait(
                missing=tuple(role.role for role in missing if role not in exited),
                exited=tuple(role.role for role in exited),
                problem=problem,
                waited_seconds=monotonic() - started,
            )
        if monotonic() >= deadline:
            return RuntimeRegistrationWait(
                missing=tuple(role.role for role in missing),
                problem=problem,
                waited_seconds=monotonic() - started,
            )
        if not announced:
            announced = True
            print_func(
                f"switchyard: waiting up to {timeout_seconds:g}s for "
                + ", ".join(role.role for role in missing)
                + " to register a runtime with the board"
            )
        sleep(poll_seconds)


def read_runtime_assignment_details(
    config: ProjectConfig,
    *,
    connection_factory: Callable[[str, float], Any] | None = None,
) -> tuple[dict[str, dict], str]:
    """The board's runtime assignments in full, not just which roles have one.

    The rows carry what a liveness proof needs and an argv search cannot give:
    the pane target the role actually registered, the pid that registered it,
    that process's start time, and the uid it ran as (SYRD-169).
    """
    try:
        from scripts.ticket_board.write_client import UnixHTTPConnection

        factory = connection_factory or (
            lambda socket_path, timeout: UnixHTTPConnection(socket_path, timeout=timeout)
        )
        connection = factory(config.board_socket, 3)
        try:
            connection.request("GET", "/api/runtime-assignments")
            response = connection.getresponse()
            body = response.read().decode("utf-8", errors="replace")
        finally:
            connection.close()
        if response.status != 200:
            return {}, f"the board answered HTTP {response.status} for its runtime assignments"
        payload = json.loads(body)
    except Exception as exc:  # noqa: BLE001 - any failure to read is "cannot say"
        return {}, f"the board's runtime assignments could not be read: {exc}"
    if not isinstance(payload, dict) or payload.get("project") != config.project:
        return {}, "the runtime assignment response belongs to another project"
    assignments = payload.get("assignments")
    if not isinstance(assignments, dict):
        return {}, "the runtime assignment response has no assignments object"
    return {
        str(name): assignment
        for name, assignment in assignments.items()
        if isinstance(assignment, dict)
    }, ""
