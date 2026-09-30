"""Moving an existing role to another agent runtime: `switchyard set-role-runtime`.

`set_project_role_runtime_command` settles the runtime (the named one, or, at a
terminal, one chosen from the supported list), then the model, against the
project owner's own catalog rather than the caller's: an explicit model is
checked and an unavailable one refused; a runtime that changes gets its model
chosen again, or its own default when nobody can be asked; the same runtime with
a model the owner does not offer is repaired at a terminal and refused without
one. It then hands the change to `scripts.role_runtime.switch_role_runtime` and
reports what happened -- a dry run's verdict, or the switch, and a forced
interruption of a busy role (SYRD-115, SYRD-250). `_role_named` finds the role
being changed.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-358). The launcher
imports this module at its top and re-exports both names, so the CLI and the
suites call the same objects. Every launcher facility the command uses -- the
role lookup, the role's current CLI, the owner's catalog prefix, the runtime and
model fields, the catalog and the terminal selector -- is read from
`team_launcher` when it runs, as it was; `scripts.role_runtime` is still
imported inside the command. The standard-library names are this module's own
imports, the same objects. This module never imports `team_launcher` at its
top.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig, RoleConfig


def _role_named(config: ProjectConfig, role_name: str) -> RoleConfig | None:
    for role in config.roles:
        if role.role == role_name:
            return role
    return None


def set_project_role_runtime_command(
    config: ProjectConfig,
    *,
    config_path: Path,
    role_name: str,
    runtime: str = "",
    #: `None` means "keep whatever is configured, unless the owner does not
    #: offer it"; `""` drops the model; anything else is an explicit choice.
    model: str | None = None,
    #: `None` keeps the role's effort; `""` clears it (the runtime's default);
    #: anything else is an explicit level, checked against the account (SYRD-534).
    effort: str | None = None,
    force: bool = False,
    reason: str = "",
    dry_run: bool = False,
    pane_state_dir: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    interactive: bool | None = None,
    input_func: Callable[[str], str] = input,
    print_func: Callable[[str], None] = print,
) -> int:
    from scripts import team_launcher as launcher

    from scripts import role_runtime

    existing = launcher._role_named(config, role_name)
    current_runtime = launcher._role_cli_name(existing) if existing is not None else ""
    can_ask = (sys.stdin.isatty() if interactive is None else interactive)
    # Whose catalog decides. This used to be nobody's: the model selector below
    # ran with no owner prefix, so a director repairing a tenant was offered
    # their OWN models -- the same account mix-up this ticket is about, in the
    # command meant to repair it (SYRD-250 DAT).
    owner_user, owner_args = launcher._owner_catalog_args(config)
    if not runtime:
        if not can_ask:
            raise SystemExit(
                "switchyard: set-role-runtime needs --cli when there is no terminal to choose at"
            )
        # The same selector `switchyard new` uses, over the same list: a runtime
        # switch is the third place this choice was made and the third list of
        # runtimes somebody had to keep in step (SYRD-115).
        runtime = launcher.terminal_select.select_one(
            launcher._runtime_field(role_name, default=current_runtime or "codex"),
            input_func=input_func,
            print_func=print_func,
        )

    # The model is the dependent half of this choice. Recalculated whenever the
    # runtime actually changes, because a model name belongs to the runtime that
    # advertises it: a role moved from Codex to Claude used to keep `gpt-5.5`,
    # and the new runtime was started with the old one's model (SYRD-115).
    configured_model = str(getattr(existing, "model", "") or "")
    chosen_model: str | None = None
    if model is not None:
        # Explicit, and checked against the owner's own list rather than taken
        # on faith. Refused rather than replaced: substituting a model nobody
        # asked for is what this ticket forbids.
        chosen_model = model.strip()
        if chosen_model:
            unavailable = launcher.runtime_catalog.model_absent_from(
                launcher.runtime_catalog.owner_model_catalog(
                    runtime, runner=runner, owner_args=owner_args
                ),
                chosen_model,
            )
            if unavailable is not None:
                offered = ", ".join(choice.value for choice in unavailable.choices)
                raise SystemExit(
                    f"switchyard: {runtime} on {owner_user or 'the project account'} does not "
                    f"offer {chosen_model!r}; it offers: {offered}. Nothing was changed."
                )
    elif existing is not None and current_runtime and runtime != current_runtime:
        if configured_model:
            print_func(
                f"switchyard: {role_name}'s model {configured_model} belongs to "
                f"{current_runtime}; {runtime} advertises its own"
            )
        if can_ask:
            chosen_model = launcher.terminal_select.select_one(
                launcher._model_field(
                    role_name, runner=runner, owner_args=owner_args, print_func=print_func
                ),
                {"runtime": runtime},
                input_func=input_func,
                print_func=print_func,
            )
        elif configured_model:
            # Nobody to ask, so the honest move is to leave the role on the new
            # runtime's own default rather than on a model it does not have.
            chosen_model = ""
    elif configured_model:
        # Same runtime, and this is the repair path. `set-role-runtime` used to
        # compute a model only when the runtime CHANGED, so an audit role
        # already on `agy` could not be moved off a model its account does not
        # recognise by any supported command at all -- the launch was stopped
        # and the remedy it named did nothing (SYRD-250 DAT).
        unavailable = launcher.runtime_catalog.model_absent_from(
            launcher.runtime_catalog.owner_model_catalog(
                runtime, runner=runner, owner_args=owner_args
            ),
            configured_model,
        )
        if unavailable is not None:
            offered = ", ".join(choice.value for choice in unavailable.choices)
            print_func(
                f"switchyard: {runtime} on {owner_user or 'the project account'} does not "
                f"offer {role_name}'s configured model {configured_model!r}, so the provider "
                f"would ignore it and run something else. That account offers: {offered}."
            )
            if not can_ask:
                raise SystemExit(
                    f"switchyard: nothing was changed. Re-run naming the model, for example "
                    f"`switchyard set-role-runtime {config.project} {role_name} "
                    f"--cli {runtime} --model {unavailable.choices[0].value}`, or pass "
                    f"`--model ''` to take {runtime}'s own default."
                )
            # Not seeded with `configured_model`: the account has just refused
            # it, so offering it first and making it the default would mean
            # pressing Enter keeps the broken value. It is named in the line
            # above; typing it again is still possible, as a deliberate act.
            chosen_model = launcher.terminal_select.select_one(
                launcher._model_field(
                    role_name,
                    runner=runner,
                    owner_args=owner_args,
                    print_func=print_func,
                ),
                {"runtime": runtime},
                input_func=input_func,
                print_func=print_func,
            )

    chosen_effort = None if effort is None else effort.strip()
    if chosen_effort:
        # Checked before anything is written, against the model the role will
        # run: the one chosen now, or the one it keeps.
        effective_model = chosen_model if chosen_model is not None else configured_model
        if not launcher.runtime_catalog.runtime_takes_effort(runtime):
            print_func(
                f"switchyard: {runtime} does not take an effort level, so --effort {chosen_effort} would be "
                "dropped before the command line. Nothing was changed."
            )
            return 1
        efforts = launcher.runtime_catalog.owner_effort_catalog(
            runtime, effective_model, runner=runner, owner_args=owner_args
        )
        offered = [choice.value for choice in efforts.choices]
        if chosen_effort not in offered:
            if efforts.enumerable:
                print_func(
                    f"switchyard: {runtime} does not accept effort {chosen_effort!r} for "
                    f"{effective_model or 'its default model'}; {efforts.detail}: {', '.join(offered)}. "
                    "Nothing was changed."
                )
                return 1
            # A recorded table describes a vendor, not this account, and nothing
            # may be refused on its strength (SYRD-250): said, and carried out.
            print_func(
                f"switchyard: effort {chosen_effort!r} is not among {', '.join(offered)} ({efforts.detail or 'recorded'}); "
                "it could not be checked against the account, so the start will tell"
            )

    result = role_runtime.switch_role_runtime(
        config,
        config_path=config_path,
        role_name=role_name,
        runtime=runtime,
        model=chosen_model,
        # Only when chosen, so a switch without one is called exactly as before.
        **({} if chosen_effort is None else {"effort": chosen_effort}),
        force=force,
        reason=reason,
        dry_run=dry_run,
        pane_state_dir=pane_state_dir,
        runner=runner,
        print_func=print_func,
    )
    if dry_run and result.configured_changed:
        # Read as optional: a result that does not say it is a repair is a move.
        if getattr(result, "argument_repair", False):
            print_func(
                f"switchyard: would keep {result.role} on {result.runtime} and update its effort and arguments as listed; "
                "every check passed and nothing was changed"
            )
            return 0
        print_func(
            f"switchyard: would move {result.role} from {result.previous_runtime} to {result.runtime}; "
            "every check passed and nothing was changed"
        )
        return 0
    print_func(result.describe())
    if result.forced and result.reason:
        print_func(f"switchyard: forced past a busy role: {result.reason}")
    return 0
