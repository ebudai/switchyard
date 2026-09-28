"""A project's runtime user, lingering with its runtime directory ready.

`ensure_user_linger_runtime` enables systemd linger for a user
(`loginctl_enable_linger_args`) and waits, up to `RUNTIME_READY_ATTEMPTS` polls
`RUNTIME_READY_POLL_SECONDS` apart, for its runtime directory to appear, refusing
with what to do when either step fails. `ensure_configured_runtime_user` does
that for a project's runtime user when its session directory lives under the
user runtime; `provision_runtime_command` is `team-launcher provision-runtime`.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-438), in their original
order. The launcher imports this module and re-exports all six names; `main`
still calls the launcher's names, and `scripts/launch_phases.py` still reads
`ensure_configured_runtime_user` there. Everything they read when they run --
each other, the user and uid lookups, the runtime-directory path and the
session-path check -- is read through the launcher, so a suite that rebinds one
there still intercepts it. The definition-time defaults -- the two retry
constants, `subprocess.run` and `time.sleep` -- are the same objects as before:
the constants moved here with them, and the launcher re-exports these very
objects. This module imports `team_launcher` only inside the functions, when
they run.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig


RUNTIME_READY_ATTEMPTS = 50
RUNTIME_READY_POLL_SECONDS = 0.1


def loginctl_enable_linger_args(user_name: str) -> list[str]:
    return ["loginctl", "enable-linger", user_name]


def ensure_user_linger_runtime(
    user_name: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    runtime_exists: Callable[[Path], bool] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    attempts: int = RUNTIME_READY_ATTEMPTS,
    poll_seconds: float = RUNTIME_READY_POLL_SECONDS,
) -> Path:
    from scripts import team_launcher as launcher

    user = user_name.strip()
    if not user:
        raise SystemExit("team-launcher: cannot provision runtime for an empty user name")
    uid = launcher.uid_for_user(user)
    if uid is None:
        raise SystemExit(f"team-launcher: cannot provision runtime for unknown user {user!r}")
    runtime_dir = launcher.runtime_dir_for_uid(uid)
    exists = runtime_exists or (lambda path: path.is_dir())
    result = runner(
        launcher.loginctl_enable_linger_args(user),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        stderr = str(getattr(result, "stderr", "") or "").strip()
        detail = f": {stderr}" if stderr else ""
        raise SystemExit(
            f"team-launcher: failed to enable linger for {user!r}{detail}; "
            f"run `sudo loginctl enable-linger {user}` and retry"
        )
    for _ in range(max(1, attempts)):
        if exists(runtime_dir):
            return runtime_dir
        sleeper(poll_seconds)
    raise SystemExit(
        f"team-launcher: linger is enabled for {user!r}, but {runtime_dir} is still missing; "
        "start or restart that user's systemd user manager and retry"
    )


def ensure_configured_runtime_user(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    runtime_exists: Callable[[Path], bool] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    attempts: int = RUNTIME_READY_ATTEMPTS,
) -> Path | None:
    from scripts import team_launcher as launcher

    user = config.run_as_user or launcher.current_user_name()
    if not user or not launcher.session_dir_uses_user_runtime(config.session_dir, user):
        return None
    return launcher.ensure_user_linger_runtime(
        user,
        runner=runner,
        runtime_exists=runtime_exists,
        sleeper=sleeper,
        attempts=attempts,
    )


def provision_runtime_command(user_name: str | None, config: ProjectConfig | None = None) -> int:
    from scripts import team_launcher as launcher

    user = (user_name or "").strip()
    if not user and config is not None:
        user = config.run_as_user or launcher.current_user_name()
    if not user:
        user = launcher.current_user_name()
    runtime_dir = launcher.ensure_user_linger_runtime(user)
    print(f"runtime ready for {user}: {runtime_dir}")
    return 0
