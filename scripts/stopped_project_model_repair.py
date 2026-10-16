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

    Root that does not claim to be the Director. A caller that does claim it
    keeps the Director's path exactly as before; anyone else who is not root
    is refused there, as before.
    """
    actor = (environ.get("TICKET_BOARD_CALLER_ROLE") or "").strip().lower()
    return actor != DIRECTOR_ROLE and (os.geteuid() if euid is None else euid) == 0


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


def make_owner_dirs(directory: Path, config_path: Path) -> None:
    """Create `directory` and any missing parent, each owned by the configuration's owner."""
    owner = os.stat(config_path)
    missing = [path for path in (directory, *directory.parents) if not path.exists()]
    for path in reversed(missing):
        path.mkdir(mode=0o700)
        os.chown(path, owner.st_uid, owner.st_gid, follow_symlinks=False)


def keep_owner(path: Path, config_path: Path) -> None:
    """As root, hand a file just written back to the configuration's owner; as anyone else, nothing."""
    if os.geteuid() == 0:
        owner = os.stat(config_path)
        os.chown(path, owner.st_uid, owner.st_gid, follow_symlinks=False)
