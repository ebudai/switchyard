#!/usr/bin/env python3
"""Ask polkit what it would decide, before anything privileged is attempted.

On SYRD-102 a Director invoked a privileged operation and the pane sat in
authorization for three minutes: no prompt anybody could answer, no child
process, nothing on the board, and no way for the User to know a command was
even wanted. The operation had not failed -- it had not started, and nothing
said so.

The fix is to stop treating "ask for privilege" as the first step. polkit can be
asked what it *would* decide without prompting, and its answers are distinct
enough to act on. Measured against polkit 126 on the host this ships to:

    pkcheck --action-id <id> --process <pid,start,uid>

    exit 0    authorized now, with no interaction
    exit 1    refused
    exit 2    would require authentication ("polkit.result=auth_admin...")
    exit 127  `Action <id> is not registered` -- the policy is not installed

So a missing policy, a refusal, and "this needs a human who is not there" are
all decidable *before* privilege is requested, in bounded time. What used to be
a silent wait becomes a durable, actionable answer.

Nothing here requests privilege or runs anything privileged. It only asks.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Sequence

#: The caller is authorized now; the action may proceed without a prompt.
AUTHORIZED = "authorized"
#: polkit would ask a human. Whether that is acceptable is the caller's policy
#: decision, but it must never become an unbounded wait.
NEEDS_AUTHENTICATION = "needs-authentication"
#: The action id is not installed. The release did not install its policy, or
#: this host has an older one.
NOT_REGISTERED = "not-registered"
#: polkit answered, and the answer was no.
REFUSED = "refused"
#: polkit could not be asked at all -- no `pkcheck`, no polkitd, or it did not
#: answer inside the deadline. Deliberately distinct from `REFUSED`: "I could
#: not ask" is not "the answer was no", and reporting it as a refusal would
#: send an operator looking for a permission that was never the problem.
UNAVAILABLE = "unavailable"

DEFAULT_TIMEOUT_SECONDS = 10.0
PKCHECK = "pkcheck"

_EXIT_STATES = {
    0: AUTHORIZED,
    1: REFUSED,
    2: NEEDS_AUTHENTICATION,
    127: NOT_REGISTERED,
}


@dataclass(frozen=True)
class PolkitDecision:
    """What polkit said, and enough detail to put in front of a person."""

    state: str
    action_id: str
    detail: str = ""

    @property
    def may_proceed(self) -> bool:
        return self.state == AUTHORIZED

    def describe(self) -> str:
        """One line an operator can act on, naming the action every time."""
        if self.state == AUTHORIZED:
            return f"{self.action_id} is authorized for this process"
        if self.state == NEEDS_AUTHENTICATION:
            return (
                f"{self.action_id} requires a human to authenticate, and this "
                "pane has nobody watching it. Run it from a session with an "
                "authentication agent, or have the action pre-authorized for "
                "this account."
            )
        if self.state == NOT_REGISTERED:
            return (
                f"{self.action_id} is not installed on this host, so nothing can "
                "authorize it. Run `sudo switchyard upgrade <project>` to install "
                "this release's privileged action catalogue."
            )
        if self.state == REFUSED:
            return f"{self.action_id} was refused for this process{_suffix(self.detail)}"
        return (
            f"{self.action_id} could not be checked, so nothing was attempted"
            f"{_suffix(self.detail)}"
        )


def _suffix(detail: str) -> str:
    detail = detail.strip()
    return f": {detail}" if detail else ""


def polkit_decision(
    action_id: str,
    *,
    pid: int,
    start_time: int | None = None,
    uid: int | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    pkcheck: str = PKCHECK,
) -> PolkitDecision:
    """What polkit would decide for `action_id` performed by that process.

    The process is named by `(pid, start_time, uid)` rather than by uid alone,
    because that is the identity the board already uses for a role's live
    runtime -- and because a uid is shared by every role in a tenant, so a
    uid-only question cannot tell the Director apart from anybody sitting
    beside it.

    Never prompts: `--allow-user-interaction` is deliberately not passed, so
    this returns in bounded time whatever the answer is.
    """
    if not action_id.strip():
        return PolkitDecision(UNAVAILABLE, action_id, "no action id was given")
    process = str(pid)
    if start_time is not None:
        process = f"{process},{start_time}"
        if uid is not None:
            process = f"{process},{uid}"
    args: Sequence[str] = [pkcheck, "--action-id", action_id, "--process", process]
    try:
        result = runner(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return PolkitDecision(
            UNAVAILABLE,
            action_id,
            f"polkit did not answer within {timeout:g}s",
        )
    except (OSError, ValueError) as exc:
        return PolkitDecision(UNAVAILABLE, action_id, f"{pkcheck} could not be run ({exc})")
    output = f"{getattr(result, 'stdout', '') or ''}\n{getattr(result, 'stderr', '') or ''}"
    code = getattr(result, "returncode", 1)
    state = _EXIT_STATES.get(code)
    if state is None:
        # An exit code this does not know is not a refusal. Say what happened.
        return PolkitDecision(
            UNAVAILABLE, action_id, f"{pkcheck} exited {code}{_suffix(_first_line(output))}"
        )
    if state is NOT_REGISTERED or "is not registered" in output:
        return PolkitDecision(NOT_REGISTERED, action_id, _first_line(output))
    return PolkitDecision(state, action_id, _first_line(output))


def _first_line(text: str) -> str:
    for line in str(text).splitlines():
        line = line.strip()
        if line:
            return line[:400]
    return ""
