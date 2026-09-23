#!/usr/bin/env python3
"""The one root-owned program every catalogued privileged action runs through.

polkit answers one question: may this account run this helper for this named
action. It cannot answer the question that matters here -- *which* process
asked -- because a polkit rule sees `subject.user`, and in a Switchyard tenant
every role shares that account. The existing board grant says so in its own
comment, and a uid-shaped grant is why a non-Director role under the same
account would be indistinguishable from the Director.

So this program answers the second question itself, before it does anything:

1. the action must be in the catalogue, and its values must parse;
2. this process must be able to establish which pane it was run from;
3. that pane must be the board's **registered** runtime for the control role,
   compared as `(pid, start_time, uid)` -- not as a uid;
4. the board must be running on process authority at all.

Every one of those fails closed, and none of them consults anything the caller
supplied. The project's board URL is re-derived from root-owned registration
state rather than taken from argv, because a caller that could name the board
could name one that would agree with it.

Nothing is mutated before a durable attempt record exists. The failure this
comes from was invisible precisely because nothing was written down until
something succeeded (SYRD-112).
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

if __package__ in (None, ""):  # pragma: no cover - direct execution as a program
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from ticket_board import (
        peer_identity,
        privileged_actions,
        privileged_install,
        privileged_operations,
        rollout_journal,
    )
else:
    from . import (
        peer_identity,
        privileged_actions,
        privileged_install,
        privileged_operations,
        rollout_journal,
    )

PROC_ROOT = Path("/proc")
DEFAULT_REGISTRY_DIR = Path("/etc/switchyard/projects")
#: What makes a role the controller. The same pair `control_role_of` uses.
CONTROL_CAPABILITIES = frozenset({"set_manually_controlled", "merge"})


class Refused(Exception):
    """The boundary said no. Carries what an operator needs, and nothing else."""


@dataclass(frozen=True)
class CallerIdentity:
    pid: int
    start_time: int
    uid: int

    def describe(self) -> str:
        return f"process {self.pid} (started {self.start_time}, uid {self.uid})"


def caller_identity(
    pid: int | None = None, *, proc_root: Path = PROC_ROOT, uid: int | None = None
) -> CallerIdentity:
    """Which pane this was run from, by walking our own ancestry.

    Not taken from argv or the environment: those are the caller's to write.
    `pkexec` authorizes and then execs in place, so the ancestry above this
    process is still the caller's shell and its pane. When that walk finds no
    pane -- run detached, run from a service, run from something that is not a
    pane at all -- this refuses rather than guessing, which is the same thing
    `require_control_caller` does for publication.
    """
    pid = os.getpid() if pid is None else pid
    identity = peer_identity.session_identity(pid, proc_root=proc_root)
    if identity is None:
        raise Refused(
            "cannot establish which pane this was run from, so there is no process "
            "this could belong to. A privileged action must be run from the control "
            "role's own pane, not detached from it"
        )
    resolved_uid = _uid_of(identity.pid, proc_root=proc_root) if uid is None else uid
    if resolved_uid is None:
        raise Refused(f"cannot read the owner of process {identity.pid}")
    return CallerIdentity(identity.pid, identity.start_time, resolved_uid)


def _uid_of(pid: int, *, proc_root: Path = PROC_ROOT) -> int | None:
    try:
        for line in (proc_root / str(pid) / "status").read_text(encoding="utf-8").splitlines():
            if line.startswith("Uid:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def board_url_for(
    project: str, *, registry_dir: Path = DEFAULT_REGISTRY_DIR,
    load: Callable[[Path], Any] = None,
) -> str:
    """The project's board, from root-owned registration state.

    Deliberately not an argument. A caller that could name the board could name
    one that would happily agree it is the Director.
    """
    loader = load or _load_json
    entry = registry_dir / f"{project}.json"
    try:
        record = loader(entry)
    except (OSError, ValueError) as exc:
        raise Refused(f"{project} is not registered on this host ({exc})") from None
    if not isinstance(record, dict):
        raise Refused(f"{entry} is not a registration record")
    config_path = str(record.get("config_path") or "").strip()
    if not config_path:
        raise Refused(f"{entry} records no configuration for {project}")
    try:
        config = loader(Path(config_path))
    except (OSError, ValueError) as exc:
        raise Refused(f"{project}'s configuration cannot be read ({exc})") from None
    board_url = str((config or {}).get("board_url") or "").strip()
    if not board_url:
        raise Refused(f"{project}'s configuration records no board URL")
    return board_url


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def require_registered_control_caller(
    project: str,
    *,
    board_get: Callable[[str, str], Any],
    board_url: str,
    caller: CallerIdentity,
    proc_root: Path = PROC_ROOT,
) -> str:
    """Refuse anything but the live registered process of the control role.

    The same shape `require_control_caller` uses for publication, asked here
    for a privileged action. The comparison is the whole point: a sibling role
    running under the tenant's account has a different pane, so it is refused
    by identity rather than trusted by uid.
    """
    workflow = board_get(board_url, "/api/workflow")
    document = workflow.get("document") if isinstance(workflow, dict) else None
    control_role = _control_role_of(document if isinstance(document, dict) else {})
    if not control_role:
        raise Refused(
            f"{project}'s board declares no role with control authority, so nothing here "
            "may act privileged. A missing or malformed workflow document is not permission"
        )
    listing = board_get(board_url, f"/api/runtime-assignments/{control_role}")
    if not isinstance(listing, dict):
        raise Refused(
            f"{project}'s board has no registered runtime for {control_role}; its session is "
            "not running, so there is no process this action could belong to"
        )
    if str(listing.get("authority_mode") or "") != "process":
        raise Refused(
            f"{project}'s board does not run on process authority; a privileged action cannot "
            "be authorized from a shared uid alone"
        )
    assignment = listing.get("assignment")
    if not isinstance(assignment, dict):
        raise Refused(f"{project}'s board has no registered runtime for {control_role}")
    registered = (
        int(assignment.get("process_pid") or 0),
        int(assignment.get("process_start_time") or 0),
        int(assignment.get("process_uid") or -1),
    )
    if (caller.pid, caller.start_time, caller.uid) != registered:
        raise Refused(
            f"only {control_role}'s registered process may run a privileged action. This ran "
            f"under {caller.describe()} and the board registered process {registered[0]} "
            f"(started {registered[1]}, uid {registered[2]})"
        )
    # The row outlives the process it names. A pid is reused; a start time is
    # not, so this is what makes a replaced session fail closed rather than
    # inherit the authority of the one it replaced.
    live = peer_identity.read_process(caller.pid, proc_root=proc_root)
    if live is None or live.start_time != caller.start_time:
        raise Refused(f"the registered {control_role} process is no longer running")
    return control_role


def _control_role_of(document: Mapping[str, Any]) -> str:
    """The role holding control authority, by CAPABILITY rather than by name.

    Deliberately the same rule `switchyard_publication_authority.control_role_of`
    uses, and deliberately no fallback to the name `director`: a document that
    is absent, empty or malformed would otherwise mean "whatever process
    registered itself under a familiar name may act privileged", which is the
    authority this boundary exists to take away (SYRD-49).

    An earlier version of this read a `control_authority` flag that no workflow
    document has. It would have found no control role on a real board and
    refused everything -- failing closed, but for the wrong reason, and hiding
    the real check behind an error nobody could act on.
    """
    roles = document.get("roles") if isinstance(document, dict) else None
    if isinstance(roles, list):
        for role in sorted(
            (r for r in roles if isinstance(r, dict)), key=lambda r: str(r.get("name") or "")
        ):
            capabilities = role.get("capabilities")
            if not isinstance(capabilities, list) or not role.get("active"):
                continue
            if CONTROL_CAPABILITIES <= set(capabilities):
                return str(role.get("name") or "")
    return ""


#: How long a privileged action may run before it is abandoned and recorded as
#: abandoned. Finite for the same reason the pre-flight exists: the failure this
#: replaces had no upper bound, so nobody learned anything for three minutes.
ACTION_TIMEOUT_SECONDS = 900.0


def run_privileged_action(
    action: privileged_actions.PrivilegedAction,
    values: Mapping[str, str],
    *,
    project: str,
    attempt_factory: Callable[..., Any],
    runner: Callable[..., Any],
    command_for: Callable[[str, Mapping[str, str]], list[str]] = None,
    timeout: float = ACTION_TIMEOUT_SECONDS,
    print_func: Callable[[str], None] = print,
) -> int:
    """Build the command, record the attempt, run it, and close the record.

    The order is the requirement. The attempt is opened BEFORE the command
    runs and closed on every path out of here -- success, failure, timeout and
    an unexpected exception alike -- because the failure this comes from was
    invisible precisely in that nothing was written down until something
    succeeded. A run that is abandoned at the timeout is a FAILED attempt with
    a duration, not an absent one.

    The command is built first, though, and deliberately outside the attempt.
    `trusted_release_root` refuses a commit no root-controlled source holds,
    and `NotExecutableYet` says an action is catalogued but has nothing to run:
    both are decisions, not runs, and recording them as failed attempts would
    fill root's journal with entries for things that never touched the host.
    They are reported to the caller instead, in bounded time and with the
    reason.
    """
    builder = command_for or privileged_operations.command_for
    try:
        command = builder(action.name, values)
    except privileged_operations.NoTrustedSource as exc:
        raise Refused(str(exc)) from None
    except privileged_operations.NotExecutableYet as exc:
        raise Refused(str(exc)) from None
    except KeyError as exc:
        raise Refused(str(exc).strip("'")) from None

    attempt = attempt_factory(
        project, command, target_commit=str(values.get("commit") or "")
    )
    directory = attempt.open()
    print_func(f"switchyard: recording attempt {attempt.attempt} in {directory}")
    try:
        result = runner(command, timeout=timeout)
    except TimeoutError:
        attempt.close(
            status="failed",
            exit_status=None,
            detail=(
                f"{action.action_id} did not complete within {timeout:g}s and was abandoned"
            ),
        )
        print_func(
            f"switchyard: {action.action_id} did not complete within {timeout:g}s. The attempt "
            f"is recorded as failed in {directory}; nothing further will happen"
        )
        return 124
    except BaseException as exc:  # noqa: BLE001 - the record must close either way
        attempt.close(status="failed", exit_status=None, detail=f"{type(exc).__name__}: {exc}")
        raise
    exit_status = int(getattr(result, "returncode", 1) or 0)
    attempt.close(
        status="succeeded" if exit_status == 0 else "failed",
        exit_status=exit_status,
        detail=action.action_id,
    )
    print_func(
        f"switchyard: {action.action_id} exited {exit_status}; recorded in {directory}"
    )
    return exit_status


def parse_request(argv: Sequence[str]) -> tuple[privileged_actions.PrivilegedAction, dict[str, str]]:
    """`<action> key=value ...` and nothing else.

    No flags, no positional overloading, no repeats. Anything this does not
    recognise is refused before privilege is used, which is the difference
    between a bounded action surface and a command channel.
    """
    if not argv:
        raise Refused("no action was named")
    name, *rest = argv
    try:
        action = privileged_actions.action_for(name)
    except privileged_actions.ArgumentError as exc:
        raise Refused(str(exc)) from None
    values: dict[str, str] = {}
    for item in rest:
        if item.startswith("-"):
            raise Refused(f"{action.name} takes no options; refusing {item!r}")
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise Refused(f"expected key=value, got {item!r}")
        if key in values:
            raise Refused(f"{key} was given twice")
        values[key] = value
    try:
        return action, action.validate(values)
    except privileged_actions.ArgumentError as exc:
        raise Refused(str(exc)) from None


#: Where a host-wide action's attempt is recorded. An action with no project
#: still has to leave a record somewhere, and inventing a tenant to file it
#: under would put root's host-wide decisions in a tenant's journal. No project
#: can collide with it: a slug may not contain an underscore.
HOST_JOURNAL_PROJECT = "_host"


def journal_project(values: Mapping[str, str]) -> str:
    return str(values.get("project") or "") or HOST_JOURNAL_PROJECT


def main(argv: Sequence[str] | None = None) -> int:
    """`switchyard-privileged-helper <action> key=value ...`, as root, via pkexec.

    Everything that can refuse does so before anything is mutated, and in this
    order: what was asked, whether this program is still the installed one, who
    asked, and only then the action itself.

    The installation check comes second on purpose. polkit's `exec.path` names
    a path, not a hash -- it will happily execute a helper whose owner or mode
    has drifted, or a package beside it that somebody else can write. Checking
    from inside the helper is not circular: a rewritten helper could of course
    skip the check, but the check is what makes *drift* -- a mode widened by a
    careless install, a package directory left group-writable -- fail closed
    instead of running with root's authority.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        action, values = parse_request(argv)
        problems = privileged_install.verify_installation()
        if problems:
            raise Refused(
                "the installed privileged boundary is not the one this release installs, "
                "so it will not be used: " + "; ".join(problems)
            )
        caller = caller_identity()
        project = values.get("project") or ""
        if project:
            require_registered_control_caller(
                project,
                board_get=_board_get,
                board_url=board_url_for(project),
                caller=caller,
            )
        # A host-wide action names no tenant, so there is no single board to
        # prove the caller against -- which is exactly why every action without
        # a project is `auth_admin` in the catalogue: a human authenticates,
        # rather than a pane proving itself. `test_a_projectless_action_must_
        # ask_a_human` asserts that pairing over the whole table, so a future
        # projectless action cannot quietly be added as pre-authorized.
        return run_privileged_action(
            action,
            values,
            project=journal_project(values),
            attempt_factory=_attempt_factory,
            runner=_run_command,
        )
    except Refused as exc:
        print(f"switchyard-privileged-helper: {exc}", file=sys.stderr, flush=True)
        return 1


def _attempt_factory(project: str, command: Sequence[str], **kwargs: Any) -> Any:
    return rollout_journal.Attempt(project, list(command), **kwargs)


def _run_command(command: Sequence[str], *, timeout: float) -> Any:
    import subprocess

    try:
        return subprocess.run(list(command), timeout=timeout)
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"{command[0]} exceeded {timeout:g}s") from None


def _board_get(board_url: str, path: str) -> Any:
    import urllib.request

    with urllib.request.urlopen(f"{board_url.rstrip('/')}{path}", timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":  # pragma: no cover - the installed entry point
    raise SystemExit(main())
