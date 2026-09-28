"""The presentation commands: `switchyard attach`, `switchyard present` and `switchyard recover-display`.

- `switchyard_attach_command` attaches a role's session through the
  presentation controller.
- `switchyard_present_command` lists the presentation, or applies one action
  (show, swap, hide, focus, restore, bootstrap, recover) and reports the result.
- `switchyard_recover_display_command` is the operator's way back to the
  Director (SYRD-239): it resolves the project, lets the tenant-control bridge
  take over when it crosses, and otherwise recovers only for the Director or the
  operator the bridge authenticates, refusing anyone else with the route.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-412), in their original
order. The launcher imports this module and re-exports all three names;
`switchyard_main` still dispatches them by its own globals. Everything the
recovery calls -- both parsers, the project resolver, the configuration
loader, the tenant-control grant, the current user and the present command --
is read through the launcher at call time, so a suite that rebinds one there
still intercepts it. Each command imports the presentation controller when it
runs, as it did. The `subprocess.run` and `print` defaults are bound at
definition, as they were, from this module's own imports: the same objects.
`ProjectConfig` is an annotation only. This module imports `team_launcher` only
inside the recovery command, when it runs.
"""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig


def switchyard_attach_command(
    config: ProjectConfig,
    *,
    args: argparse.Namespace,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> int:
    from scripts import presentation_controller

    return presentation_controller.attach_role_command(
        config,
        role_name=args.role,
        json_output=args.json,
        runner=runner,
        print_func=print_func,
    )


def switchyard_present_command(
    config: ProjectConfig,
    *,
    config_path: Path,
    args: argparse.Namespace,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> int:
    from scripts import presentation_controller

    if args.action == "list":
        report = presentation_controller.presentation_report(
            config,
            config_path=config_path,
            runner=runner,
        )
        presentation_controller.print_presentation_report(report, json_output=args.json, print_func=print_func)
        return 0
    presentation_controller.presentation_action(
        config,
        config_path=config_path,
        action=args.action,
        role_name=getattr(args, "role", None),
        slot=getattr(args, "slot", getattr(args, "slot_a", None)),
        other_slot=getattr(args, "slot_b", None),
        layout=getattr(args, "layout", "default"),
        runner=runner,
    )
    report = presentation_controller.presentation_report(config, config_path=config_path, runner=runner)
    presentation_controller.print_presentation_report(report, json_output=False, print_func=print_func)
    return 0


def switchyard_recover_display_command(
    argv: Sequence[str],
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    """`switchyard recover-display <project>`: the operator's way back to the Director.

    This verb was advertised by the disconnected Director slot, listed in
    SWITCHYARD_COMMANDS and mapped in the tenant-control bridge -- and never
    dispatched. `switchyard_main` fell through to bare-project selection, so the
    live MEFP UAT got `unknown project 'recover-display mefp'` and nothing was
    recovered (SYRD-239).

    For the desktop operator the work happens on the far side. Loading the
    configuration crosses to the owner the same way `stop` and `status` do: the
    bridge maps this verb to `present <slug> recover director` from root-owned
    data and runs it as the owner, and control does not come back here. So the
    verb carries no authority of its own; it only chooses the route.

    Reached past that line only when no crossing happened -- the caller is the
    owner, or root. Neither is the registered operator the bridge authenticates,
    so the recovery is decided by the presentation controller's own check, and
    a caller it would refuse is told why in terms of this command rather than
    being sent back to run it again.
    """
    from scripts import team_launcher as launcher

    from scripts import presentation_controller

    args = launcher._build_switchyard_recover_display_parser().parse_args(list(argv[1:]))
    entry = launcher._resolve_switchyard_project(" ".join(args.project))
    # The slug, not what was typed: the bridge only serves `<verb> <slug>`, so
    # a display name here would silently fall back to sudo.
    config = launcher._load_switchyard_project_config_for_command(entry, ["recover-display", entry.slug])
    env = os.environ if environ is None else environ
    actor = (env.get("TICKET_BOARD_CALLER_ROLE") or env.get("PGU_TICKET_BOARD_CALLER_ROLE") or "").strip().lower()
    if actor != presentation_controller.DIRECTOR_ROLE and not presentation_controller.operator_recovery_caller(
        config, env
    ):
        grant = launcher._tenant_control_grant(config.project)
        registered = grant.get("authorized_user", "")
        route = (
            f"it is registered to {registered}; run this from {registered}'s desktop session"
            if registered
            else f"{config.project} has no tenant-control grant, so no operator is registered for it"
        )
        raise SystemExit(
            f"switchyard: recover-display reaches {config.project}'s Director through its "
            f"tenant-control bridge, as the desktop operator the project is registered to. "
            f"{launcher.current_user_name()} did not arrive over that bridge, so nothing authorizes the "
            f"recovery here: {route}"
        )
    present_args = launcher._build_switchyard_present_parser().parse_args(
        [entry.slug, "recover", presentation_controller.DIRECTOR_ROLE]
    )
    return launcher.switchyard_present_command(config, config_path=entry.config_path, args=present_args)
