#!/usr/bin/env python3
"""The unprivileged side of a privileged action: plan it, then ask for it.

Everything here runs as the caller. It decides nothing about authority -- the
root-owned helper does that, and would refuse this process just as readily if
it were the wrong one. What this does is make the request *legible before it is
made*: which action, which validated values, which installed policy would
decide it, and the way back if it goes wrong.

That ordering is the point. On SYRD-102 the first thing anybody learned about
the operation was that it had not happened, three minutes later, with nothing
written down. Here the answer to "will this even be allowed, and what will it
do" is available before any privilege is requested, and it is available to the
person running it rather than only to whoever reads a log afterwards.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

if __package__ in (None, ""):  # pragma: no cover - direct execution as a program
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from ticket_board import (
        polkit_preflight,
        privileged_actions,
        privileged_helper,
        privileged_install,
        privileged_operations,
    )
else:
    from . import (
        polkit_preflight,
        privileged_actions,
        privileged_helper,
        privileged_install,
        privileged_operations,
    )

DEFAULT_HELPER = privileged_install.helper_path()
DEFAULT_POLICY = privileged_install.policy_path()
PKEXEC = "pkexec"
#: How long the privileged call may take before it is abandoned and said so.
#: Finite on purpose: the failure this replaces had no upper bound at all.
DEFAULT_ACTION_TIMEOUT_SECONDS = 900.0


@dataclass
class ActionPlan:
    """Everything decided before privilege is requested."""

    action: privileged_actions.PrivilegedAction
    project: str
    values: dict[str, str]
    helper: Path
    policy: Path
    decision: polkit_preflight.PolkitDecision
    rollback: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    #: What root would run, when that is decidable without privilege. Empty
    #: when the trusted-source pre-flight refused, and the reason is then in
    #: `problems` -- so a dry run says which release would be used, or why none
    #: can be.
    command: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not self.problems and self.decision.may_proceed

    def argv(self) -> list[str]:
        """Exactly what the helper will be given. No shell, ever."""
        return [self.action.name, *(f"{key}={value}" for key, value in sorted(self.values.items()))]

    def report(self) -> list[str]:
        """What a dry run prints, and what a refusal prints before stopping."""
        lines = [
            f"action:    {self.action.action_id}",
            f"summary:   {self.action.summary}",
            f"arguments: {' '.join(self.argv()[1:]) or '(none)'}",
            f"helper:    {self.helper}",
            f"policy:    {self.policy}",
            f"authentication: {self.action.authentication}",
            f"polkit:    {self.decision.describe()}",
        ]
        if self.command:
            lines.append(f"would run: {' '.join(self.command)}")
        if self.rollback:
            lines.append("rollback:")
            lines.extend(f"  {command}" for command in self.rollback)
        else:
            lines.append("rollback:  none recorded for this project yet")
        for problem in self.problems:
            lines.append(f"problem:   {problem}")
        lines.append(
            "ready:     yes" if self.ready else "ready:     no -- nothing privileged was attempted"
        )
        return lines


def plan_action(
    project: str,
    action_name: str,
    values: dict[str, str],
    *,
    helper: Path = DEFAULT_HELPER,
    policy: Path = DEFAULT_POLICY,
    rollback_commands: Callable[[str], list[str]] = lambda _project: [],
    decide: Callable[..., polkit_preflight.PolkitDecision] = polkit_preflight.polkit_decision,
    identity: Callable[[], privileged_helper.CallerIdentity] = privileged_helper.caller_identity,
    verify: Callable[..., list[str]] = privileged_install.verify_installation,
    build_command: Callable[..., list[str]] = privileged_operations.command_for,
) -> ActionPlan:
    """Decide everything that can be decided without privilege.

    Collects problems rather than raising on the first one, because an operator
    reading this wants the whole picture -- a missing policy AND a missing
    helper is one repair, not two discoveries a minute apart.
    """
    problems: list[str] = []
    try:
        action = privileged_actions.action_for(action_name)
    except privileged_actions.ArgumentError as exc:
        raise privileged_helper.Refused(str(exc)) from None

    supplied = dict(values)
    if "project" in {name for name, _ in action.arguments}:
        supplied.setdefault("project", project)
    try:
        validated = action.validate(supplied)
    except privileged_actions.ArgumentError as exc:
        raise privileged_helper.Refused(str(exc)) from None

    # Ownership and mode, not merely presence. polkit's `exec.path` names a
    # path rather than a hash, so a helper whose mode has widened is still the
    # helper polkit will run -- and it is the caller, before any privilege is
    # requested, who can cheaply notice that.
    installation = verify(helper.parent, policy.parent)
    if installation:
        problems.extend(installation)
        problems.append(f"Run `sudo switchyard upgrade {project}` to reinstall the boundary")

    # What root would actually run. This is where a commit that no trusted
    # source holds is refused -- before privilege, before mutation, with the
    # reason and the packet that would produce it. An action that is catalogued
    # but has nothing to run says so here too, rather than authorizing
    # something and then quietly doing nothing.
    command: list[str] = []
    try:
        command = list(build_command(action.name, validated))
    except privileged_operations.NoTrustedSource as exc:
        problems.append(str(exc))
    except privileged_operations.NotExecutableYet as exc:
        problems.append(str(exc))
    except KeyError as exc:
        problems.append(str(exc).strip("'"))

    try:
        caller = identity()
        decision = decide(
            action.action_id, pid=caller.pid, start_time=caller.start_time, uid=caller.uid
        )
    except privileged_helper.Refused as exc:
        # No pane means the helper would refuse this caller anyway. Say it here,
        # where it costs nothing, rather than after a privilege request.
        problems.append(str(exc))
        decision = polkit_preflight.PolkitDecision(
            polkit_preflight.UNAVAILABLE, action.action_id, "the caller has no pane identity"
        )

    return ActionPlan(
        action=action,
        project=project,
        values=validated,
        helper=helper,
        policy=policy,
        decision=decision,
        rollback=list(rollback_commands(project)),
        problems=problems,
        command=command,
    )


def run_action(
    plan: ActionPlan,
    *,
    timeout: float = DEFAULT_ACTION_TIMEOUT_SECONDS,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    pkexec: str = PKEXEC,
    which: Callable[[str], str | None] = shutil.which,
    print_func: Callable[[str], None] = print,
) -> int:
    """Ask for the action, bounded, and say what happened either way.

    The privileged call is `pkexec <helper> <action> key=value ...`: a fixed
    program and a catalogued verb. There is no shell and nothing of the
    caller's environment in it, because polkit's exec annotation binds the
    action to that one path and the helper accepts nothing else.
    """
    if not plan.ready:
        for line in plan.report():
            print_func(line)
        return 1
    if which(pkexec) is None:
        print_func(f"switchyard: {pkexec} is not installed, so nothing privileged can be asked for")
        return 1
    argv: Sequence[str] = [pkexec, str(plan.helper), *plan.argv()]
    try:
        result = runner(argv, timeout=timeout)
    except subprocess.TimeoutExpired:
        # The whole point. A privileged call that does not come back is a
        # failure with a duration, not a pane that waits until somebody looks.
        print_func(
            f"switchyard: {plan.action.action_id} did not complete within {timeout:g}s and was "
            "abandoned. Nothing it had not already done will happen now; check "
            f"`switchyard rollout-log {plan.project}` for what it recorded before it stopped"
        )
        return 124
    except OSError as exc:
        print_func(f"switchyard: {plan.action.action_id} could not be started ({exc})")
        return 1
    code = int(getattr(result, "returncode", 1) or 0)
    if code != 0:
        print_func(
            f"switchyard: {plan.action.action_id} exited {code}; "
            f"`switchyard rollout-log {plan.project}` has what it recorded"
        )
    return code


def privileged_action_command(
    project: str,
    action_name: str,
    values: dict[str, str],
    *,
    dry_run: bool = False,
    print_func: Callable[[str], None] = print,
    **plan_kwargs: Any,
) -> int:
    """`switchyard privileged-action <project> <action> [key=value ...]`."""
    try:
        plan = plan_action(project, action_name, values, **plan_kwargs)
    except privileged_helper.Refused as exc:
        print_func(f"switchyard: {exc}")
        return 1
    if dry_run:
        for line in plan.report():
            print_func(line)
        return 0 if plan.ready else 1
    if not plan.ready:
        for line in plan.report():
            print_func(line)
        return 1
    return run_action(plan, print_func=print_func)
