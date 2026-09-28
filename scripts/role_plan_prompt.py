"""The interactive role plan `switchyard new` asks for, and what it turns the answers into.

`_prompt_switchyard_role_plan` asks whether to include the designer and audit
roles, lets the operator pick implementer roles from the conventional list
(`NEW_PROJECT_CONVENTIONAL_IMPLEMENTER_ROLES`, defaulting to
`NEW_PROJECT_DEFAULT_IMPLEMENTER_ROLES`, through `_implementer_roles_field`) or
name one of their own, and then walks every role through runtime, model and
effort (`_prompt_role_runtime_plan`). Model lists are read as the owner account
only when that account exists (`_owner_account_exists`); otherwise nobody is
asked and the operator is told the choice is confirmed later.
`_prompt_switchyard_role_choices` returns just the role/runtime pairs.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-422), in their original
order. The launcher imports this module and re-exports all seven names;
`scripts/new_project_phases.py` still reaches the prompt through the launcher.
Everything the seven read -- each other included, and the launcher's prompt
helpers, field builders, role selection type, CLI defaults, owner command
prefix, the prompt schema and the runtime catalog and terminal selector -- is
read through the launcher at call time, so a suite that rebinds one there still
intercepts it. The only definition-time defaults are the `input` and `print`
builtins. `pwd` is the shared module, so a `team_launcher.pwd.getpwnam` patch
reaches `_owner_account_exists`. The two annotation-only types are imported
under TYPE_CHECKING. This module imports `team_launcher` only inside the
functions, when they run.
"""

from __future__ import annotations

import pwd
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

if TYPE_CHECKING:
    from scripts.team_launcher import RoleSelection
    from scripts.ticket_board.prompt_schema import Field


NEW_PROJECT_DEFAULT_IMPLEMENTER_ROLES = ("main", "ops")
NEW_PROJECT_CONVENTIONAL_IMPLEMENTER_ROLES = (
    ("main", "core/domain implementation and integration"),
    ("ops", "environment, services, tooling, and infrastructure"),
    ("app", "application/UI work"),
    ("research", "investigation, design support, and unknowns"),
    ("perf", "measurement and performance work"),
)


def _implementer_roles_field() -> Field:
    """The conventional roles as things to pick, not a string to compose.

    The list was already printed -- and then the answer was read as one
    comma-separated line, so a typo in the middle of it was a role nobody asked
    for and a role nobody noticed was missing. The same names, selectable, with
    the conventional pair as the default and a deliberate path to a role of
    one's own (SYRD-115).
    """
    from scripts import team_launcher as launcher

    def validate(value: str) -> str:
        return launcher._validate_new_project_implementer_role(value, context="implementer role")

    return launcher.Field(
        name="roles",
        kind=launcher.KIND_MULTI,
        title="Implementer roles",
        choices=tuple(
            launcher.Choice(role, role, description)
            for role, description in launcher.NEW_PROJECT_CONVENTIONAL_IMPLEMENTER_ROLES
        ),
        default=tuple(launcher.NEW_PROJECT_DEFAULT_IMPLEMENTER_ROLES),
        allow_custom=True,
        custom_title="A role of your own",
        validate=validate,
    )


def _prompt_role_runtime_plan(
    role: str,
    *,
    default_cli: str,
    configured: RoleSelection | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] | None = None,
    owner_args: Sequence[str] = (),
    unverified_because: str = "",
    interactive: bool = True,
    input_func: Callable[[str], str] = input,
    print_func: Callable[[str], None] = print,
) -> RoleSelection:
    """One guided path for a role: runtime, then model, then effort.

    In that order because each one narrows the next. The models offered are the
    chosen runtime's, and the effort question is not asked at all for a runtime
    that discards it -- `agy` drops an effort level before it reaches the
    command line, and a question whose answer is thrown away should not be
    asked (SYRD-115).
    """
    from scripts import team_launcher as launcher

    held = configured or launcher.RoleSelection(role=role, cli="")
    schema = launcher.Schema(
        (
            launcher._runtime_field(role, default=default_cli, configured=held.cli),
            launcher._model_field(
                role, configured=held.model, runner=runner,
                owner_args=owner_args, unverified_because=unverified_because,
                print_func=print_func,
            ),
            launcher._effort_field(role, configured=held.effort),
        )
    )
    answers: dict[str, Any] = {}
    runtime_field, model_field, effort_field = schema.fields
    answers["runtime"] = launcher.terminal_select.select_one(
        runtime_field, answers, interactive=interactive,
        input_func=input_func, print_func=print_func,
    )
    model = ""
    if model_field.choices_for(answers) or model_field.allow_custom:
        model = launcher.terminal_select.select_one(
            model_field, answers, interactive=interactive,
            input_func=input_func, print_func=print_func,
        )
    answers["model"] = model
    effort = ""
    if launcher.runtime_catalog.runtime_takes_effort(answers["runtime"]):
        effort = launcher.terminal_select.select_one(
            effort_field, answers, interactive=interactive,
            input_func=input_func, print_func=print_func,
        )
    return launcher.RoleSelection(role=role, cli=answers["runtime"], model=model, effort=effort)


def _prompt_switchyard_role_plan(
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] | None = None,
    #: Whose CLI context the model lists come from. A catalog is a property of
    #: an ACCOUNT, not of a host: `agy models` on the operator's login and on
    #: the tenant owner's are different lists, and the one that matters is the
    #: owner's, because that is the account the role will run as. Asking the
    #: wrong one is how `test2` was configured with a slug its own owner does
    #: not recognise (SYRD-250).
    owner_user: str = "",
    owner_home: Path | None = None,
    input_func: Callable[[str], str] = input,
    print_func: Callable[[str], None] = print,
) -> tuple[RoleSelection, ...]:
    """Every role of a new project, chosen rather than typed."""
    from scripts import team_launcher as launcher

    # The account usually does not exist yet: `switchyard new` chooses its
    # roles before it creates anybody, and it must keep choosing before it
    # creates anybody -- nothing may be mutated before the plan review. So the
    # owner-scoped list is simply not available here, and the honest thing is
    # to say which account could not be asked and that the choice will be
    # confirmed once it can be. Pretending otherwise is what shipped the first
    # time: the recorded fallback offered `gemini-3.7-flash-high`, the very
    # slug test2 was misconfigured with (SYRD-250 DAT).
    owner_exists = launcher._owner_account_exists(owner_user)
    owner_args = (
        launcher._owner_command_env_args(owner_user, owner_home, [])
        if owner_user and owner_home is not None
        else ()
    )
    # One guard, and it is this one. When the owner cannot be asked, NOBODY is
    # asked: leaving the runner in place would enumerate whoever is TYPING and
    # label their models "listed in this account" -- the original defect, moved
    # into the code meant to fix it. Withholding the runner is what makes that
    # impossible; withholding the prefix as well would only look careful.
    catalog_runner = runner if owner_exists else None
    unverified_because = (
        f"the {owner_user} account does not exist yet, so its own list could not be read; "
        f"this choice is confirmed against it after the account is created"
        if owner_user and not owner_exists
        else ""
    )
    include_designer = launcher._prompt_bool("Include designer role", default=True, input_func=input_func)
    include_audit = launcher._prompt_bool("Include audit role", default=True, input_func=input_func)
    fixed: list[str] = []
    if include_designer:
        fixed.append("designer")
    fixed.append("director")
    if include_audit:
        fixed.append("audit")

    try:
        implementers = launcher.terminal_select.select_many(
            launcher._implementer_roles_field(), input_func=input_func, print_func=print_func
        )
    except launcher.terminal_select.Cancelled:
        raise SystemExit("switchyard: too many invalid answers for implementer roles") from None
    if not implementers:
        raise SystemExit("switchyard: at least one implementer role is required")

    plan: list[RoleSelection] = []
    for role in fixed:
        plan.append(
            launcher._prompt_role_runtime_plan(
                role,
                default_cli=launcher.NEW_PROJECT_ROLE_CLI_DEFAULTS.get(role, "claude"),
                runner=catalog_runner, owner_args=owner_args,
                unverified_because=unverified_because,
                input_func=input_func, print_func=print_func,
            )
        )
    for role in implementers:
        plan.append(
            launcher._prompt_role_runtime_plan(
                role, default_cli="codex", runner=catalog_runner, owner_args=owner_args,
                unverified_because=unverified_because,
                input_func=input_func, print_func=print_func,
            )
        )
    return tuple(plan)


def _prompt_switchyard_role_choices(
    *,
    input_func: Callable[[str], str] = input,
    print_func: Callable[[str], None] = print,
) -> tuple[tuple[str, str], ...]:
    """The role/runtime pairs, for callers that want only those."""
    from scripts import team_launcher as launcher

    return tuple(
        (selection.role, selection.cli)
        for selection in launcher._prompt_switchyard_role_plan(
            input_func=input_func, print_func=print_func
        )
    )


def _owner_account_exists(owner_user: str) -> bool:
    """Whether there is an account to ask anything of yet.

    A `switchyard new` chooses its roles' models before it creates the owner,
    so "cannot enumerate" and "does not exist yet" are different answers that
    used to look identical (SYRD-250 DAT).
    """
    if not owner_user:
        return False
    try:
        pwd.getpwnam(owner_user)
    except KeyError:
        return False
    return True
