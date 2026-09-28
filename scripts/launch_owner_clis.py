"""The owner's agent CLIs, checked before a launch.

`run_switchyard_launch_first_run_auth` runs the first-run sign-in phase for a
project's owner (the configured account, else the caller) before panes are
launched; `stop_before_launch_for_missing_owner_clis` refuses the launch when
that phase found a configured CLI missing, printing
`_format_missing_cli_launch_failure`: which CLIs, for which roles, and the
vendor's own install command for each -- text only; switchyard never runs a
vendor installer.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-445), in their original
order. The launcher imports this module and re-exports all three names;
`switchyard_main` and `switchyard_validate_models_command` still call the
launcher's names, and `new_project_phases.py` and `workflow_launcher.py` still
read them there. Everything they read when they run -- each other, the current
user, the first-run phase and its report type, the owner's home for auth, the
install-command table and the owner reminder -- is read through the launcher,
so a suite that rebinds one there still intercepts it. The definition-time
defaults -- `subprocess.run` and the builtin `print` -- are the same objects.
This module imports `team_launcher` only inside the functions, when they run.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.first_run_auth import FirstRunAuthReport
    from scripts.team_launcher import ProjectConfig


def _format_missing_cli_launch_failure(report: FirstRunAuthReport) -> str:
    from scripts import team_launcher as launcher

    owner_detail = f" for owner user {report.owner_user}" if report.owner_user else ""
    cli_details = "; ".join(
        f"{cli} (roles: {', '.join(roles)})" for cli, roles in report.missing_cli_roles.items()
    )
    lines = [
        f"switchyard: cannot launch panes because required CLI(s) are missing{owner_detail}: "
        f"{cli_details}. Install the missing CLI(s){owner_detail} and rerun switchyard.",
        launcher._owner_user_cli_reminder(report.owner_user),
    ]
    width = max((len(cli) for cli in report.missing_cli_roles), default=0)
    for cli in report.missing_cli_roles:
        command = launcher.AGENT_CLI_INSTALL_COMMANDS.get(cli, "")
        detail = command or "see that vendor's own installation documentation"
        lines.append(f"switchyard:   {cli.ljust(width)}  {detail}")
    lines.append(
        "switchyard: switchyard never fetches or runs a vendor's installer, so these commands are "
        "yours to run. It can promote an executable you already have to a host-wide copy; that "
        "offer is made before launch."
    )
    return "\n".join(lines)


def stop_before_launch_for_missing_owner_clis(
    report: FirstRunAuthReport,
    *,
    print_func: Callable[[str], None] = print,
) -> bool:
    from scripts import team_launcher as launcher

    if not report.missing_cli_roles:
        return False
    print_func(launcher._format_missing_cli_launch_failure(report))
    return True


def run_switchyard_launch_first_run_auth(
    config: ProjectConfig,
    *,
    validate_models: bool = False,
    #: How this process runs its PROBES -- auth status, "is it installed",
    #: model validation. Ordinary callers pass `subprocess.run` and are right
    #: to; `switchyard validate-models` and the workflow launcher's
    #: `prepare_role` both do.
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    #: Who drives the windows a person sits in front of, which is a separate
    #: question and used to be answered by `runner` alone. Any runner at all
    #: meant "fired and forgotten", so every live caller -- each passing
    #: `subprocess.run` for its probes -- silently gave up the pty, the title,
    #: the countdown and the deadline. `None` is the watched path and the right
    #: default for a person at a terminal; a suite driving the steps itself
    #: passes its own runner here (SYRD-221).
    foreground_runner: Callable[..., subprocess.CompletedProcess[Any]] | None = None,
    print_func: Callable[[str], None] = print,
) -> FirstRunAuthReport:
    from scripts import team_launcher as launcher

    owner_user = (config.run_as_user or launcher.current_user_name()).strip()
    if not owner_user:
        return launcher.FirstRunAuthReport({}, [])
    return launcher.run_first_run_auth_phase(
        config,
        owner_user=owner_user,
        owner_home=launcher._owner_home_for_auth(owner_user),
        validate_models=validate_models,
        runner=runner,
        foreground_runner=foreground_runner,
        print_func=print_func,
    )
