"""Live board assignment resolution and provable runtime divergence refusal.

The board's atomic assignment row is the routing authority for shared-account
workers. This module reads that row, validates its runtime and target, waits
boundedly for just-started workers, and diagnoses declared/live divergence.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Collection
from urllib import error as urllib_error
from urllib import request as urllib_request

from scripts import team_launcher

def runtime_assignment_config(
    config: team_launcher.ProjectConfig,
    *,
    opener: Callable[[str], Any] | None = None,
    wait_seconds: float = 0.0,
    poll_seconds: float = team_launcher.RUNTIME_REGISTRATION_POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    print_func: Callable[[str], None] | None = None,
    unstarted: Collection[str] = (),
) -> team_launcher.ProjectConfig:
    """Resolve every shared-account worker from the board's current assignment.

    The launcher projection remains the desired role list, but it is not live
    routing evidence.  A replacement pane may use a recovery target, so display
    attachment must consume the same atomic row as notifications and write
    authority instead of reconstructing a conventional tmux name (SYRD-69).

    `wait_seconds` is for the one caller that has just started the panes it is
    asking about. Registration is the pane's own asynchronous work, so a launch
    that sampled once refused its own startup: testing journal 0032 stopped on
    a single role that registered seconds later. Waiting is bounded and
    announced, and every refusal below -- a foreign target, a runtime that does
    not match the projection -- still happens on the first reading, because
    those are not races (SYRD-162).
    """
    if not config.role_state_isolation:
        return config
    deadline = monotonic() + max(0.0, wait_seconds)
    announced = False
    while True:
        resolved, missing = _resolved_runtime_assignments(config, opener=opener)
        if not missing:
            return team_launcher.replace(config, roles=resolved)
        # A role whose pane never started cannot register, so waiting for it
        # only delays the same refusal: every pane of a headless VM failing
        # to start sat here for the whole 90 seconds (SYRD-248). A role that
        # did start is still waited for exactly as before.
        never_started = sorted(role for role in missing if role in unstarted)
        if monotonic() >= deadline or (missing and len(never_started) == len(missing)):
            raise SystemExit(
                "switchyard: no live runtime assignment for configured role(s): "
                + ", ".join(sorted(missing))
                + (f"; their panes did not start: {', '.join(never_started)}" if never_started else "")
            )
        if not announced:
            announced = True
            if print_func is not None:
                print_func(
                    f"switchyard: waiting up to {wait_seconds:g}s for "
                    + ", ".join(sorted(missing))
                    + " to register a runtime with the board"
                )
        sleep(poll_seconds)

def _resolved_runtime_assignments(
    config: team_launcher.ProjectConfig,
    *,
    opener: Callable[[str], Any] | None = None,
) -> tuple[list[team_launcher.RoleConfig], list[str]]:
    """One reading: the roles resolved from it, and the ones it does not name."""
    url = f"{config.board_url.rstrip('/')}/api/runtime-assignments"
    open_url = opener or (lambda target: urllib_request.urlopen(target, timeout=3))
    try:
        with open_url(url) as response:
            payload = json.load(response)
    except (OSError, ValueError, urllib_error.URLError) as exc:
        raise SystemExit(
            f"switchyard: cannot resolve {config.project} runtime assignments from {url}: {exc}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("project") != config.project:
        raise SystemExit("switchyard: runtime assignment response belongs to another project")
    if payload.get("authority_mode") != "process":
        raise SystemExit("switchyard: running board still uses legacy uid authority")
    assignments = payload.get("assignments")
    if not isinstance(assignments, dict):
        raise SystemExit("switchyard: runtime assignment response has no assignments object")
    resolved: list[team_launcher.RoleConfig] = []
    missing: list[str] = []
    for role in config.roles:
        assignment = assignments.get(role.role)
        if not isinstance(assignment, dict):
            if role.detached:
                resolved.append(role)
            else:
                missing.append(role.role)
            continue
        target = str(assignment.get("actual_target") or "").strip()
        runtime = str(assignment.get("runtime") or "").strip()
        if not target.split(":", 1)[0].startswith(f"{config.project}-"):
            raise SystemExit(
                f"switchyard: refusing foreign runtime assignment for {role.role}: {target!r}"
            )
        if not role.cli or team_launcher._command_name(role.cli[0]) != runtime:
            raise SystemExit(
                f"switchyard: runtime assignment for {role.role} is {runtime!r}, "
                "which differs from the launcher projection"
            )
        resolved.append(
            team_launcher.replace(
                role, target=target, tmux_session=target.split(":", 1)[0]
            )
        )
    return resolved, missing

def runtime_divergence_refusal(
    config: team_launcher.ProjectConfig,
    *,
    opener: Callable[[str], Any] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    proc_root: Path | None = None,
) -> str:
    """Why a live worker has no runtime assignment, when that can be verified.

    The board shows a role's assignment only while its registered runtime and
    target equal the ones the workflow DECLARES, and it refuses to register any
    other. So a declared workflow that disagrees with the worker actually
    running makes a live role look unregistered, and "no live runtime assignment
    for configured role(s): director, ops" is all the operator was told. That is
    what SYRD-239's live UAT got on mefp: the workflow declared director and ops
    as claude while both workers were Codex.

    Verified, and bounded, rather than guessed:

    * only roles the board's own reading reports missing;
    * only their DECLARED target, taken from the board's workflow document --
      never a conventional tmux name -- and only one inside this project;
    * the process in that exact pane, read from its own command line.

    It changes nothing and authorizes nothing: it returns the refusal text, or
    "" when there is no divergence it can prove, in which case the caller's
    ordinary refusal stands.
    """
    try:
        _resolved, missing = _resolved_runtime_assignments(config, opener=opener)
    except SystemExit:
        return ""
    if not missing:
        return ""
    url = f"{config.board_url.rstrip('/')}/api/workflow"
    open_url = opener or (lambda target: urllib_request.urlopen(target, timeout=3))
    try:
        with open_url(url) as response:
            payload = json.load(response)
        declared_roles = {
            str(spec.get("name") or ""): spec
            for spec in (payload.get("document") or {}).get("roles") or []
            if isinstance(spec, dict)
        }
    except (OSError, ValueError, AttributeError, urllib_error.URLError):
        return ""
    procs = proc_root or Path("/proc")
    divergent: list[str] = []
    for role in sorted(missing):
        spec = declared_roles.get(role) or {}
        declared = str(spec.get("runtime") or "").strip()
        target = str(spec.get("target") or "").strip()
        if not declared or not target.split(":", 1)[0].startswith(f"{config.project}-"):
            continue
        probe = runner(
            ["tmux", "display-message", "-p", "-t", _exact_tmux_target(target), "#{pane_pid}"],
            capture_output=True, text=True, check=False,
        )
        pid = str(getattr(probe, "stdout", "") or "").strip()
        if getattr(probe, "returncode", 1) != 0 or not pid.isdigit():
            continue
        try:
            argv0 = (procs / pid / "cmdline").read_bytes().split(b"\0", 1)[0].decode()
        except OSError:
            continue
        live = team_launcher._command_name(argv0) if argv0 else ""
        # Only a runtime Switchyard runs counts as evidence. A pane whose
        # process is a shell, a wrapper or anything else says nothing about
        # which runtime is there, and "the live worker runs bash" would be a
        # claim this cannot back.
        if live not in team_launcher.SUPPORTED_CONFIG_CLI_NAMES:
            continue
        if live != declared:
            divergent.append(
                f"switchyard:   {role}: declared {declared} at {target}, but the live worker "
                f"in that pane runs {live} (pid {pid})"
            )
    if not divergent:
        return ""
    return "\n".join(
        [
            f"switchyard: {config.project}'s declared workflow disagrees with its live workers, "
            "so the board holds no runtime assignment for them:",
            *divergent,
            "switchyard: the board registers only the runtime a role is declared to run, so "
            "these workers cannot hold an assignment, and nothing is reattached to a target it "
            "cannot verify. Nothing was changed. The Director has to correct the declared "
            "runtime before this can succeed.",
        ]
    )

def _exact_tmux_target(target: str) -> str:
    """Disable tmux prefix matching for a named session, window, or pane."""
    return target if target.startswith("=") else f"={target}"
