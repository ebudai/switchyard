"""Whether this host can take a tenant's polkit rule, and whether its polkit authority answers.

- `POLKIT_RULES_DIR`, `POLKIT_ANSWERED_EXIT_CODES`, `POLKIT_RESTART_COMMAND` and
  `POLKIT_QUERY_TIMEOUT_SECONDS` are where the deploy rule goes, what counts as
  an answer from the authority, the command that brings a stopped or masked
  authority back, and how long it is given to answer.
- `_apt_archive_has` and `polkit_install_command` give the one command an
  operator can paste to install polkit, asking the apt archive the way the
  installer does.
- `polkit_service_problem` asks the authority one real question through
  pkcheck; `polkit_readiness_problems` says, before anything is created, why
  the host cannot take the rule or answer for it.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-390), in their original
order. The launcher imports this module and re-exports every name, so its own
caller and every suite that reaches or rebinds these there -- the rules
directory included -- reach the same objects. Every name defined here that
another definition here reads when it runs is read from `team_launcher` when
it runs, as it was, so a patch on the launcher still intercepts. The defaults
-- `shutil.which`, `subprocess.run` and the query timeout -- are bound when the
functions are defined, as they were. The standard-library names are this
module's own imports, the same objects. This module never imports
`team_launcher` at its top.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable


#: Where provisioning installs the board's deploy rule. Its absence is how a
#: host without polkit shows itself, halfway through the packet (SYRD-261).
POLKIT_RULES_DIR = Path("/etc/polkit-1/rules.d")


#: What a polkit authority answers with: authorized, not authorized, needs
#: authentication, dismissed. Any of them means the service answered over the
#: system bus; pkcheck says 127 when it could not ask at all (pkcheck(1)).
POLKIT_ANSWERED_EXIT_CODES = frozenset({0, 1, 2, 3})
#: Brings back a stopped or masked authority. `restart` alone refuses a masked
#: unit, which is one way a host ends up with polkit present and unusable;
#: `unmask` is a no-op on a unit that is not masked (SYRD-261 review).
POLKIT_RESTART_COMMAND = "sudo systemctl unmask polkit.service && sudo systemctl restart polkit.service"
#: Bounded, and long enough for the system bus to activate polkitd on a host
#: where nothing has asked it anything yet -- which is normal, not broken.
POLKIT_QUERY_TIMEOUT_SECONDS = 15.0


def _apt_archive_has(package: str, *, runner: Callable[..., subprocess.CompletedProcess[Any]]) -> bool:
    """The installer's own question, asked the same way (install-switchyard-prereqs)."""
    try:
        result = runner(
            ["apt-cache", "show", "--no-all-versions", package],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return result.returncode == 0


def polkit_install_command(
    *,
    which: Callable[[str], str | None] = shutil.which,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> str:
    """One command an operator can paste, or "" when there is none to give.

    Command text only, never prose around it: a remedy the operator has to
    edit before it runs is not a remedy (SYRD-261 review). On apt the package
    names depend on the release, so the archive is asked, as the installer asks.
    """
    from scripts import team_launcher as launcher

    if which("pacman"):
        return "sudo pacman -S --needed polkit"
    if which("apt-get"):
        if launcher._apt_archive_has("pkexec", runner=runner):
            return "sudo apt-get install -y polkitd pkexec"
        return "sudo apt-get install -y policykit-1"
    return ""


def polkit_service_problem(
    *,
    which: Callable[[str], str | None] = shutil.which,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    timeout_seconds: float = POLKIT_QUERY_TIMEOUT_SECONDS,
) -> str:
    """Why the polkit authority cannot answer an authorization question, or "".

    Presence is not readiness: the rules directory and pkexec can both be there
    while polkitd cannot start or the system bus cannot reach it, and the board
    unit's deploy rule is then never consulted. So it is asked one real
    question, about this very process, through polkit's own client. Whatever
    the answer -- this account may not even be authorized -- an answer is what
    proves the service works; the system bus starts polkitd on demand, so a
    service that is simply not running yet is not a failure.
    """
    from scripts import team_launcher as launcher

    pkcheck = which("pkcheck")
    if pkcheck is None:
        return "pkcheck, polkit's own client, is not on PATH"
    try:
        result = runner(
            [pkcheck, "--action-id", "org.freedesktop.policykit.exec", "--process", str(os.getpid())],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return f"the polkit authority did not answer within {timeout_seconds:g}s"
    except OSError as exc:
        return f"pkcheck could not be run: {exc}"
    if result.returncode in launcher.POLKIT_ANSWERED_EXIT_CODES:
        return ""
    detail = " ".join(str(result.stderr or "").split()) or f"exit {result.returncode}"
    return f"the polkit authority could not answer an authorization query (pkcheck: {detail})"


def polkit_readiness_problems(
    *,
    rules_dir: Path | None = None,
    which: Callable[[str], str | None] = shutil.which,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> list[str]:
    """Why this host cannot take a tenant's polkit rule or answer for it, if it cannot.

    Asked before anything is created. On a minimal host `switchyard new` used
    to create the accounts, the project and the board's units, then fail
    installing the deploy rule into a directory that did not exist -- and a
    re-run was refused, because by then a unit was installed with no database
    beside it (SYRD-261).
    """
    from scripts import team_launcher as launcher

    rules_dir = rules_dir if rules_dir is not None else launcher.POLKIT_RULES_DIR
    missing: list[str] = []
    if not rules_dir.is_dir():
        missing.append(f"{rules_dir} does not exist")
    if which("pkexec") is None:
        missing.append("pkexec is not on PATH")
    if missing:
        install = launcher.polkit_install_command(which=which, runner=runner)
        return [
            f"polkit is not installed ({'; '.join(missing)}). Provisioning installs a polkit "
            "rule so the project account can restart its own board, and privileged commands "
            "run through pkexec. "
            + (
                f"Install it, then run this again:\n    {install}"
                if install
                else "Install your distribution's polkit package, then run this again."
            )
        ]
    service = launcher.polkit_service_problem(which=which, runner=runner)
    if service:
        return [
            f"polkit is installed but not working: {service}. The board's deploy rule is "
            "enforced by that service. Start it, then run this again:\n"
            f"    {launcher.POLKIT_RESTART_COMMAND}"
        ]
    return []
