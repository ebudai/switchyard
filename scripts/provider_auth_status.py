"""Whether a provider CLI is installed and signed in, asked as the project owner.

A role's provider (Claude, Codex, Hermes, agy) has to be installed, and
authenticated, for the account that owns the project. It is the owner's
signed-in state that counts, not the operator's. This module covers:
- **Probing as the owner.** Each provider's status command runs in the owner's
  context (`_run_owner_cli_probe`: owner home, scrubbed pane identity,
  timeout). The commands live in the table `FIRST_RUN_AUTH_STATUS_COMMANDS`.
- **Classifying the answer:** authenticated, unauthenticated, not installed or
  timed out (`_cli_auth_status`), without printing anything the probe
  returned.
- **Provider first run.** Whether a provider's account-wide first run is
  complete (`_provider_account_setup_complete`).

Running logins and the interactive first run is the auth phase, and stays in
`scripts/team_launcher.py`.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-297). `team_launcher`
imports this module at its top and still exports every name. Its facilities
are **patched on the launcher** by the suites: `FIRST_RUN_AUTH_STATUS_COMMANDS`
(by rebinding it), `_owner_home_for_auth`, `_cli_auth_status` and
`_provider_account_setup_complete`. So everything here reads them as
`launcher.<name>` when it runs, including this module's own functions, and so
do the other extracted modules. This module never imports `team_launcher` at
its top.
"""

from __future__ import annotations

import json
import pwd
import shlex
import subprocess
from pathlib import Path
from typing import Any, Callable, Sequence


FIRST_RUN_AUTH_STATUS_COMMANDS: dict[str, list[str]] = {
    "agy": ["agy", "models"],
    "claude": ["claude", "auth", "status", "--json"],
    "codex": ["codex", "login", "status"],
    "hermes": ["hermes", "config", "check"],
}


def _owner_home_for_auth(owner_user: str, fallback: Path | None = None) -> Path:
    try:
        return Path(pwd.getpwnam(owner_user).pw_dir)
    except KeyError:
        return fallback or (Path("/home") / owner_user)


#: How long a non-interactive probe of a provider may take before it is treated
#: as a failure rather than waited on.
#:
#: These probes are the one part of the first-run phase nobody can see: stdin is
#: /dev/null, stdout and stderr are captured, and nothing is drawn. Unbounded,
#: that is a launcher that stops dead with no process to look at, no output, and
#: a window whose title has already been handed back -- which is what a fresh
#: test15 and then test16 both showed after the trust steps completed
#: (SYRD-245). A single `-p` prompt that has not answered in three minutes is
#: not going to; an interactive step that legitimately waits on a person is
#: bounded separately and much longer.
OWNER_CLI_PROBE_TIMEOUT_SECONDS = 180.0


#: The exit status recorded for a probe that had to be given up on. 124 is what
#: `timeout(1)` uses, so a reader who has seen one recognises the other.
PROBE_TIMED_OUT_STATUS = 124


def _run_owner_cli_probe(
    *,
    owner_user: str,
    owner_home: Path,
    command: Sequence[str],
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    cwd: Path | None = None,
    timeout_seconds: float = OWNER_CLI_PROBE_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[Any]:
    from scripts import team_launcher as launcher

    args = launcher._owner_command_env_args(owner_user, owner_home, command)
    try:
        return runner(
            args,
            cwd=str(cwd if cwd is not None else owner_home),
            env=launcher._pane_identity_scrubbed_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        # Reported as a failed probe rather than raised: the phase below turns
        # every other probe failure into a named, resumable line, and a
        # traceback here would lose which role and which model it was.
        return subprocess.CompletedProcess(
            args,
            PROBE_TIMED_OUT_STATUS,
            stdout="",
            stderr=(
                f"no answer after {timeout_seconds:g}s; gave up. Run this yourself to see "
                f"what it is waiting for: {shlex.join(str(part) for part in args)}"
            ),
        )
    except TypeError:
        # A caller's runner that predates the bound. Kept working rather than
        # made to accept a keyword it never had, because these probes are
        # driven by several suites' fakes.
        return runner(
            args,
            cwd=str(cwd if cwd is not None else owner_home),
            env=launcher._pane_identity_scrubbed_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(args, 127, stdout="", stderr=str(exc))


def _cli_auth_probe_passed(cli: str, proc: subprocess.CompletedProcess[Any]) -> bool:
    if proc.returncode != 0:
        return False
    stdout = str(getattr(proc, "stdout", "") or "")
    stderr = str(getattr(proc, "stderr", "") or "")
    combined = f"{stdout}\n{stderr}".casefold()
    if cli == "claude":
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            return False
        return parsed.get("loggedIn") is True
    if cli == "hermes":
        # PGU-773 measured `hermes auth list` as a false positive: a pooled
        # manual OpenRouter credential appears there while Hermes still reports
        # no resolved API keys and opens `hermes setup`. `config check` reports
        # only environment/config-resolved keys, which is the runnable shape.
        return any(line.strip().startswith("\N{CHECK MARK} ") and "_API_KEY" in line for line in stdout.splitlines())
    if cli in {"agy", "codex"}:
        return "not logged" not in combined and "not authenticated" not in combined
    return True


def _owner_cli_is_installed(
    cli: str,
    *,
    owner_user: str,
    owner_home: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> bool:
    from scripts import team_launcher as launcher

    command = launcher.FIRST_RUN_AUTH_STATUS_COMMANDS.get(cli)
    if command is None or not command:
        return True
    binary = command[0]
    proc = _run_owner_cli_probe(
        owner_user=owner_user,
        owner_home=owner_home,
        command=["sh", "-c", f"command -v {shlex.quote(binary)}"],
        runner=runner,
    )
    return proc.returncode == 0


def _cli_auth_status(
    cli: str,
    *,
    owner_user: str,
    owner_home: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> str:
    from scripts import team_launcher as launcher

    command = launcher.FIRST_RUN_AUTH_STATUS_COMMANDS.get(cli)
    if command is None:
        return "authenticated"
    proc = _run_owner_cli_probe(owner_user=owner_user, owner_home=owner_home, command=command, runner=runner)
    if _cli_auth_probe_passed(cli, proc):
        return "authenticated"
    if not _owner_cli_is_installed(
        cli,
        owner_user=owner_user,
        owner_home=owner_home,
        runner=runner,
    ):
        return "not_installed"
    return "unauthenticated"


def _claude_account_setup_complete(owner_home: Path) -> bool:
    """Whether Claude's own first run has been completed for this account.

    Credentials and setup are separate: the live testing tenant held a valid
    `.claude/.credentials.json` beside a `.claude.json` carrying an
    `oauthAccount` and neither `hasCompletedOnboarding` nor a `theme`, and every
    pane opened the theme flow instead of a prompt (SYRD-191).

    Completion is now `hasCompletedOnboarding` alone; a `theme` is
    deliberately no longer accepted as a second signal. Measured on Claude Code v2.1.270: answering
    the theme prompt writes neither key, during the session or after it, and a
    genuinely onboarded account carries no top-level `theme` at all. So the
    fallback could never be the thing that reported success -- and had some
    path written a theme mid-flow it would have reported success early, which
    is the same class of defect as reading an unanswered menu as a finished
    prompt. It cost nothing to keep and could only ever be wrong (SYRD-221).
    """
    from scripts import team_launcher as launcher

    config = launcher._read_json_object(owner_home / ".claude.json")
    if not config:
        return False
    return config.get("hasCompletedOnboarding") is True


def _provider_account_setup_complete(cli: str, *, owner_home: Path) -> bool:
    if cli == "claude":
        return _claude_account_setup_complete(owner_home)
    return True
