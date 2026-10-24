"""Repairing a role's model while its project's Director is down (SYRD-593).

The launch gate (`model_validation.stop_before_launch_for_unknown_models`)
refuses to start ANY role while one is configured for a model its owner's
account does not offer, and names `switchyard set-role-runtime` as the repair.
That command was the Director's alone (`role_runtime._require_director`), and
the Director is one of the roles the gate has just declined to start. Otto sat
in exactly that state: its designer was configured for a model the owner's
`agy` no longer listed, and the printed repair was refused through sudo and
through pkexec alike.

So there is a second authority, deliberately narrow:

* WHO. Root, by the kernel's effective uid: the desktop operator's own sudo or
  pkexec, which is where `set-role-runtime` already crosses for anyone who is
  not the project's owner. `TICKET_BOARD_CALLER_ROLE` is not consulted for
  this, so claiming to be the Director in the environment earns nothing here.
* WHEN. The project's Director has no session on the project account's tmux
  server, and neither has the role being repaired. A tmux answer that is not a
  plain "no such session" or "no server" refuses: not being able to tell is
  not the same as being stopped.
* WHAT. The role's model and nothing else: the same runtime the launcher and
  the board both declare, no effort, no argument rewrite, no forced
  interruption. The model must be on the list the owner's own account gives
  (`runtime_catalog.owner_model_catalog`), or `""` for the runtime's default.
  A catalog that cannot be read refuses rather than letting anything through.

The board is not written: a model is not part of its declared workflow, and
writing it would need the Director's authority this path exists to do
without. The repair is the launcher configuration and its tenant copies,
through the same projection `set-role-runtime` always wrote, after the same
preflight. Everything root writes on the way is left the owner's: the
projection was already chowned back, and the switch's journal now is too,
with any directory made to hold it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

if TYPE_CHECKING:
    from scripts.role_runtime import RuntimePreflight
    from scripts.team_launcher import ProjectConfig, RoleConfig

DIRECTOR_ROLE = "director"
#: What tmux says when the session is simply not there. Anything else -- sudo
#: refusing, a socket that cannot be opened -- is an unanswered question.
_ABSENT = ("can't find session", "no server running on")
_NO_SOCKET = ("error connecting to", "(No such file or directory)")


def operator_repair(environ: Mapping[str, str], *, euid: int | None = None) -> bool:
    """Whether this caller takes the stopped-project path rather than the Director's.

    Root that does not claim to be the Director. Root that does is refused
    (`refuse_root_director`); anyone else who is not root takes the
    Director's path, and is refused there unless it is the Director.
    """
    actor = (environ.get("TICKET_BOARD_CALLER_ROLE") or "").strip().lower()
    return actor != DIRECTOR_ROLE and (os.geteuid() if euid is None else euid) == 0


def refuse_root_director(operator: bool, *, euid: int | None = None) -> None:
    """Refuse root that claims to be the Director: saying so earns root nothing here either (SYRD-612).

    The Director's switch writes the board and restarts a live worker from the
    Director's own session. Run by root it read display state, reloaded the
    configuration and restarted the worker through the tenant's tree by path.
    No crossing carries the claim -- `sudo` resets the environment it was made
    in -- so root's one switch is this module's repair.
    """
    from scripts.role_runtime import RoleRuntimeRefusal

    if not operator and (os.geteuid() if euid is None else euid) == 0:
        raise RoleRuntimeRefusal(
            "switchyard: root is not the Director by saying so: as root, set-role-runtime only repairs a stopped "
            "role's model while the Director is not running (SYRD-593); the Director changes a runtime from its own "
            "session"
        )


def session_problem(
    config: "ProjectConfig",
    role: "RoleConfig",
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> str:
    """"" when the role has no session on the project account's server; else why not."""
    from scripts import team_launcher as launcher

    if not launcher.role_run_as_user(config, role):
        # Root's own tmux server is not the project's, and nothing says whose is.
        return f"{config.project} names no account its {role.role} runs as, so its session cannot be looked for"
    probe = launcher.role_process_runner_for(config, role, runner=runner)(
        launcher.tmux_has_session_args(role), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    said = str(probe.stderr or "").strip()
    if probe.returncode == 0:
        return f"{role.role} is running ({role.tmux_session})"
    if probe.returncode == 1 and (
        any(marker in said for marker in _ABSENT) or all(marker in said for marker in _NO_SOCKET)
    ):
        return ""
    return f"could not tell whether {role.role} is running ({role.tmux_session}): {said or f'exit {probe.returncode}'}"


def check_repair(
    config: "ProjectConfig",
    checks: "RuntimePreflight",
    *,
    effort: str | None,
    force: bool,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    """Refuse, having changed nothing, unless this is a model-only repair of a stopped role."""
    from scripts import team_launcher as launcher
    from scripts.role_runtime import RoleRuntimeRefusal

    role_name, runtime, model = checks.role, checks.requested_runtime, checks.requested_model
    problems: list[str] = []
    if runtime != checks.current_runtime or checks.board_runtime not in (None, runtime):
        problems.append(f"it changes {role_name}'s runtime, which only the Director may do")
    if effort is not None or checks.argument_changes:
        problems.append(f"it changes {role_name}'s effort or arguments, which only the Director may do")
    if force:
        problems.append("--force interrupts a working role, which only the Director may do")
    if not problems:
        director = next((role for role in config.roles if role.role == DIRECTOR_ROLE), None)
        for role in (director, launcher._role_by_name(config, role_name)):
            problem = session_problem(config, role, runner=runner) if role is not None else ""
            if problem and problem not in problems:
                problems.append(problem)
    if not problems and model:
        owner_user, owner_args = launcher._owner_catalog_args(config)
        catalog = launcher.runtime_catalog.owner_model_catalog(runtime, runner=runner, owner_args=owner_args)
        if catalog is None:
            problems.append(f"{runtime} on {owner_user or 'the project account'} could not list its models to check {model!r} against")
        elif launcher.runtime_catalog.model_absent_from(catalog, model) is not None:
            offered = ", ".join(choice.value for choice in catalog.choices)
            problems.append(f"{runtime} on {owner_user or 'the project account'} does not offer {model!r}; it offers: {offered}")
    if problems:
        raise RoleRuntimeRefusal(
            f"switchyard: root may only repair a stopped role's model while {config.project}'s Director is not "
            f"running, and not this: " + "; ".join(problems) + ". Nothing was changed."
        )


def make_owner_dirs(directory: Path, config_path: Path, *, owner_user: str = "") -> None:
    """Create `directory` and any missing parent, each owned by the configuration's owner.

    As root for `owner_user` -- the owner the configuration names, never read
    off a file -- by descriptor from the owner's root, never through a link at
    any component (SYRD-612).
    """
    from scripts import owner_writes

    if owner_writes.root_writes_for(owner_user):
        owner_writes.ensure_owner_dirs(owner_user, [directory], mode=0o700)
        return
    owner = os.stat(config_path)
    missing = [path for path in (directory, *directory.parents) if not path.exists()]
    for path in reversed(missing):
        path.mkdir(mode=0o700)
        os.chown(path, owner.st_uid, owner.st_gid, follow_symlinks=False)


def clear_role_records(config: "ProjectConfig", role: "RoleConfig", *, pane_state_dir: Path) -> None:
    """The role's resume record set aside and its pane state cleared: as root for the owner, by descriptor.

    `clear_session_record_for_role` and `clear_pane_idle_state_for_role` rename and unlink by path, which as root
    went through any link the tenant left at the store or above it (SYRD-612). A record that cannot be set aside
    fails the switch, which then rolls back; pane state that cannot be cleared is said, as it always was.
    """
    from scripts import owner_writes, tenant_paths
    from scripts import team_launcher as launcher
    from scripts.runtime_return_records import keep_left_record

    session_dir = launcher.role_session_dir(config, role)
    owner = config.run_as_user or launcher.current_user_name()
    record = session_dir / launcher.session_file_name(role.target)
    if not owner_writes.root_writes_for(owner):
        keep_left_record(config, role, session_dir, record)  # SYRD-622: a switch back resumes it
        launcher.clear_session_record_for_role(role, session_dir)
        launcher.clear_pane_idle_state_for_role(role, pane_state_dir=pane_state_dir)
        return
    account = owner if owner_writes.within_owner_root(owner, record) else launcher.role_run_as_user(config, role) or owner
    keep_left_record(config, role, session_dir, record, account=account)
    owner_writes.supersede_owner_record(account, record)
    pane_state = pane_state_dir / launcher.pane_state_file_name(role.target)
    try:
        tenant_paths.remove_file(owner_writes.owner_root(owner, pane_state), str(pane_state))
    except tenant_paths.Refused as refusal:
        if not refusal.reason.startswith("does not exist"):  # no runtime directory is no pane state to clear
            print(f"team-launcher: failed to clear pane state for {role.role}: {refusal}", file=sys.stderr)
    except SystemExit as stop:
        print(f"team-launcher: failed to clear pane state for {role.role}: {stop}", file=sys.stderr)


def remove_journal(path: Path, owner_user: str, print_func: Callable[[str], None] = print) -> None:
    """A finished switch's journal, removed: as root for its owner by descriptor, never through a link (SYRD-612).

    A journal that cannot be removed so is left where it is, and said; the switch it records has finished.
    """
    from scripts import owner_writes

    if not owner_writes.root_writes_for(owner_user):
        path.unlink(missing_ok=True)
        return
    try:
        owner_writes.remove_root_record(owner_user, path)
    except SystemExit as stop:
        print_func(f"{stop}; the finished switch's journal is left there")


def keep_owner(path: Path, config_path: Path) -> None:
    """As root, hand a file just written back to the configuration's owner; as anyone else, nothing."""
    if os.geteuid() == 0:
        owner = os.stat(config_path)
        os.chown(path, owner.st_uid, owner.st_gid, follow_symlinks=False)
