"""Recovering one role while other configured roles have no live assignment.

`switchyard present <project> recover <role>` exists to start a role that is
gone. It used to resolve EVERY configured role's live assignment first, so a
missing role refused its own recovery, and two missing roles could not be
brought back in either order: MEFP's live UAT on 2026-10-11 got "no live
runtime assignment for configured role(s): audit, main" for both
`recover audit` and `recover main` (SYRD-580).

Recovery still asks the ordinary question first, and a project whose roles are
all live is resolved exactly as before. Only when that refuses -- and only for
a caller who could have acted -- does it read the board again, holding just the
requested role to it:

* every live assignment is still resolved and checked exactly as before -- a
  foreign target or a runtime the projection does not run is refused;
* a role with no assignment keeps its saved projection, and is not required to
  be live unless it is the one being recovered;
* the requested role, when it has no assignment, is started only if nothing
  says it should not be: no live worker in its declared pane running another
  runtime, a saved configuration that matches what the board's workflow
  declares (the board registers nothing else), and no pane already running
  there. Each refusal happens before anything is started or changed;
* after the start, recovery waits -- boundedly -- for that role's new
  registration, bound to a live process in that pane, before its display is
  attached to it.
"""

from __future__ import annotations

import io
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Callable
from urllib import error as urllib_error
from urllib import request as urllib_request

from scripts import team_launcher
from scripts.presentation_runtime_assignments import (
    _exact_tmux_target,
    _resolved_runtime_assignments,
    runtime_divergence_refusal,
)


def _board_opener(opener: Callable[[str], Any] | None) -> Callable[[str], Any]:
    return opener or (lambda target: urllib_request.urlopen(target, timeout=3))


def assignment_config_with_absent(
    config: team_launcher.ProjectConfig,
    *,
    opener: Callable[[str], Any] | None = None,
) -> tuple[team_launcher.ProjectConfig, list[str]]:
    """Every live assignment resolved as always, and the roles that have none.

    A role the board holds no assignment for keeps its saved projection, in
    configuration order, and is named in the second value. Nothing is waited
    for, and every refusal `runtime_assignment_config` makes about a role that
    IS assigned is made here too, by the same reading.
    """
    if not config.role_state_isolation:
        return config, []
    resolved, missing = _resolved_runtime_assignments(config, opener=opener)
    by_name = {role.role: role for role in resolved}
    roles = [by_name.get(role.role, role) for role in config.roles]
    return team_launcher.replace(config, roles=roles), sorted(missing)


def _declared_role(
    config: team_launcher.ProjectConfig,
    role_name: str,
    *,
    opener: Callable[[str], Any] | None,
) -> tuple[dict[str, Any] | None, str]:
    """The board's declaration of ``role_name``, or why it could not be read.

    ``(None, "")`` is a board with no workflow document at all: such a board
    declares a role at its first registration, so there is nothing to disagree
    with yet.
    """
    url = f"{config.board_url.rstrip('/')}/api/workflow"
    try:
        with _board_opener(opener)(url) as response:
            payload = json.load(response)
    except (OSError, ValueError, urllib_error.URLError) as exc:
        return None, f"its declared workflow could not be read from {url}: {exc}"
    if not isinstance(payload, dict):
        return None, f"its declared workflow from {url} is not a document"
    document = payload.get("document")
    if document is None:
        return None, ""
    roles = document.get("roles") if isinstance(document, dict) else None
    if not isinstance(roles, list):
        return None, f"its declared workflow from {url} has no roles"
    for spec in roles:
        if isinstance(spec, dict) and str(spec.get("name") or "").strip().lower() == role_name:
            return spec, ""
    return {}, ""


def absent_role_refusal(
    config: team_launcher.ProjectConfig,
    role: team_launcher.RoleConfig,
    *,
    opener: Callable[[str], Any] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    proc_root: Path | None = None,
) -> str:
    """Why ``role``, which has no live assignment, must not be started -- or "".

    Asked only about the role being recovered; no other role is probed. It
    changes nothing: every answer here comes before the start.
    """
    ordinary = f"switchyard: no live runtime assignment for configured role(s): {role.role}"
    # The verified divergence, as before, but for this role alone: a live
    # worker the declaration disowns is named, and never replaced.
    precise = runtime_divergence_refusal(
        team_launcher.replace(config, roles=[role]), opener=opener, runner=runner, proc_root=proc_root,
    )
    if precise:
        return precise
    declared, problem = _declared_role(config, role.role, opener=opener)
    if problem:
        return f"{ordinary}, and {problem}, so it was not started. Nothing was changed."
    if declared is not None:
        runtime = team_launcher._command_name(role.cli[0]) if role.cli else ""
        declared_runtime = str(declared.get("runtime") or "").strip()
        declared_target = str(declared.get("target") or "").strip()
        active = bool(declared.get("active"))
        if not declared:
            disagreement = f"{config.project}'s workflow does not declare {role.role}"
        elif not active:
            disagreement = f"{config.project}'s workflow declares {role.role} inactive"
        elif declared_runtime != runtime or declared_target != role.target:
            disagreement = (
                f"{config.project}'s workflow declares {role.role} as {declared_runtime or 'nothing'} "
                f"at {declared_target or 'no target'}, but its saved configuration would start "
                f"{runtime or 'nothing'} at {role.target}"
            )
        else:
            disagreement = ""
        if disagreement:
            return (
                f"{ordinary}. {disagreement}; the board registers only the role it declares, so "
                "it was not started. Nothing was changed."
            )
    probe = runner(
        ["tmux", "display-message", "-p", "-t", _exact_tmux_target(role.target), "#{pane_pid}"],
        capture_output=True, text=True, check=False,
    )
    if getattr(probe, "returncode", 1) == 0 and str(getattr(probe, "stdout", "") or "").strip():
        return (
            f"{ordinary}. Its pane {role.target} is running but has not registered, and recovery "
            "starts only a role that is not running. Nothing was changed."
        )
    return ""


def await_recovered_registration(
    config: team_launcher.ProjectConfig,
    role: team_launcher.RoleConfig,
    *,
    opener: Callable[[str], Any] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    proc_root: Path | None = None,
    timeout: float | None = None,
    poll: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    print_func: Callable[[str], None] = print,
) -> team_launcher.RoleConfig:
    """``role`` bound to the registration its new process made, or a refusal.

    The role had no assignment when recovery began, so any assignment it has
    now was made by the worker just started. It counts only once it is bound to
    a live process in the role's pane -- target, pid, start time and uid, the
    proof `pane_liveness` makes -- and every check `runtime_assignment_config`
    makes of an assignment is made of it. Waiting is bounded and announced, and
    a pane that exits ends it at once.
    """
    url = f"{config.board_url.rstrip('/')}/api/runtime-assignments"
    single = team_launcher.replace(config, roles=[role])
    bound: dict[str, team_launcher.RoleConfig] = {}

    def pane_running(target: str) -> bool:
        probe = runner(
            ["tmux", "display-message", "-p", "-t", _exact_tmux_target(target), "#{pane_pid}"],
            capture_output=True, text=True, check=False,
        )
        return getattr(probe, "returncode", 1) == 0 and bool(str(getattr(probe, "stdout", "") or "").strip())

    def read() -> tuple[set[str], str]:
        try:
            with _board_opener(opener)(url) as response:
                raw = response.read()
        except (OSError, ValueError, urllib_error.URLError) as exc:
            return set(), f"the board's runtime assignments could not be read: {exc}"
        # One reading, checked by the same code as every other consumer.
        resolved, missing = _resolved_runtime_assignments(single, opener=lambda _url: io.BytesIO(raw))
        if missing:
            return set(), ""
        candidate = resolved[0]
        assignments = json.loads(raw).get("assignments") or {}
        liveness = team_launcher.pane_liveness(
            config, candidate,
            tmux_targets={candidate.target} if pane_running(candidate.target) else set(),
            assignments=assignments,
            owner_uid=None,
            **({"proc_root": proc_root} if proc_root is not None else {}),
        )
        if not liveness.live:
            return set(), liveness.why
        bound[role.role] = candidate
        return {role.role}, ""

    limit = team_launcher.RUNTIME_REGISTRATION_TIMEOUT_SECONDS if timeout is None else timeout
    waited = team_launcher.await_runtime_registration(
        single,
        [role],
        read=read,
        alive=lambda candidate: pane_running(candidate.target),
        timeout_seconds=limit,
        poll_seconds=team_launcher.RUNTIME_REGISTRATION_POLL_SECONDS if poll is None else poll,
        sleep=sleep,
        monotonic=monotonic,
        print_func=print_func,
    )
    if waited.registered and role.role in bound:
        return bound[role.role]
    if waited.exited:
        reason = f"its pane {role.target} exited before it registered with the board"
    else:
        reason = (
            f"it did not register a live runtime with the board within {limit:g}s"
            + (f" ({waited.problem})" if waited.problem else "")
        )
    raise SystemExit(
        f"switchyard: {role.role} was started for recovery, but {reason}. Its display was not "
        "touched and nothing was recorded."
    )
