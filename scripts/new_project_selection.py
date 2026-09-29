"""How `switchyard new` chooses the roles and runtimes of a new project.

The supported runtimes, the default runtime per role, the reserved role names
and the prompt limit; the validators for a runtime, an implementer role and an
audit role; the default role/runtime pairs and owner name; the line prompts
(text and yes/no) and the runtime picker built from the runtime catalog; and
`RoleSelection`, one role fully chosen.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-462). The launcher imports this
module and re-exports every name, so every module that reads them through the
launcher still reaches the launcher's names. What they read of the launcher --
each other included, the role pattern, the runtime catalog, the prompt schema
and the terminal picker -- is read through it when they run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Sequence

if TYPE_CHECKING:
    from scripts.ticket_board.prompt_schema import Choice, Field


SUPPORTED_NEW_PROJECT_CLIS = ("claude", "codex", "agy", "hermes")
NEW_PROJECT_ROLE_CLI_DEFAULTS = {
    "designer": "claude",
    "director": "claude",
    "audit": "claude",
}
SWITCHYARD_PROMPT_MAX_ATTEMPTS = 5
NEW_PROJECT_RESERVED_ROLE_NAMES = frozenset({"designer", "director", "audit", "user", "unassigned"})
NEW_PROJECT_NON_AUDIT_RESERVED_ROLE_NAMES = frozenset({"designer", "director", "user", "unassigned"})


def _validate_new_project_cli(value: str, *, context: str = "CLI") -> str:
    from scripts import team_launcher as launcher

    cli = value.strip().lower()
    if cli not in launcher.SUPPORTED_NEW_PROJECT_CLIS:
        raise SystemExit(f"{context} must be one of {', '.join(launcher.SUPPORTED_NEW_PROJECT_CLIS)}")
    return cli


def _validate_new_project_implementer_role(value: str, *, context: str = "role") -> str:
    from scripts import team_launcher as launcher

    role = value.strip().lower()
    if not launcher.ROLE_RE.fullmatch(role):
        raise SystemExit(f"{context} must match ^[a-z][a-z0-9_-]{{0,63}}$")
    if role in launcher.NEW_PROJECT_RESERVED_ROLE_NAMES:
        raise SystemExit(f"{context} {role!r} is reserved")
    return role


def _validate_new_project_audit_role(value: str, *, context: str = "audit role") -> str:
    from scripts import team_launcher as launcher

    role = value.strip().lower()
    if not launcher.ROLE_RE.fullmatch(role):
        raise SystemExit(f"{context} must match ^[a-z][a-z0-9_-]{{0,63}}$")
    if role in launcher.NEW_PROJECT_NON_AUDIT_RESERVED_ROLE_NAMES:
        raise SystemExit(f"{context} {role!r} is reserved")
    return role


def _dedupe_role_names(roles: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    for role in roles:
        if role not in result:
            result.append(role)
    return tuple(result)


def _default_role_cli_pairs(
    implementer_roles: Sequence[str],
    *,
    include_designer: bool,
    include_audit: bool = True,
    audit_roles: Sequence[str] | None = None,
) -> tuple[tuple[str, str], ...]:
    from scripts import team_launcher as launcher

    pairs: list[tuple[str, str]] = []
    resolved_audit_roles = tuple(audit_roles) if audit_roles is not None else (("audit",) if include_audit else ())
    if include_designer:
        pairs.append(("designer", launcher.NEW_PROJECT_ROLE_CLI_DEFAULTS["designer"]))
    pairs.append(("director", launcher.NEW_PROJECT_ROLE_CLI_DEFAULTS["director"]))
    pairs.extend((role, launcher.NEW_PROJECT_ROLE_CLI_DEFAULTS["audit"]) for role in resolved_audit_roles)
    pairs.extend((role, "codex") for role in implementer_roles)
    return tuple(pairs)


def _default_new_project_owner(project: str) -> str:
    return f"{project}-agent"


def _read_prompt(
    prompt: str,
    *,
    input_func: Callable[[str], str] = input,
) -> str:
    try:
        return input_func(prompt)
    except EOFError:
        raise SystemExit("switchyard: no input available") from None


def _prompt_text(
    label: str,
    *,
    default: str = "",
    input_func: Callable[[str], str] = input,
) -> str:
    from scripts import team_launcher as launcher

    suffix = f" [{default}]" if default else ""
    value = launcher._read_prompt(f"{label}{suffix}: ", input_func=input_func).strip()
    return value or default


def _prompt_bool(
    label: str,
    *,
    default: bool,
    input_func: Callable[[str], str] = input,
) -> bool:
    from scripts import team_launcher as launcher

    default_text = "Y/n" if default else "y/N"
    for _attempt in range(launcher.SWITCHYARD_PROMPT_MAX_ATTEMPTS):
        raw = launcher._read_prompt(f"{label} [{default_text}]: ", input_func=input_func).strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes", "true", "1"}:
            return True
        if raw in {"n", "no", "false", "0"}:
            return False
        print("answer yes or no")
    raise SystemExit(f"switchyard: too many invalid answers for {label}")


def _runtime_choices() -> tuple[Choice, ...]:
    """The runtimes, in the order `switchyard new` offers them.

    Taken from the catalog and narrowed to what `switchyard new` supports, so
    the list an operator sees cannot drift from the list the validator accepts.
    """
    from scripts import team_launcher as launcher

    described = {choice.value: choice for choice in launcher.runtime_catalog.RUNTIMES}
    return tuple(
        described.get(name, launcher.Choice(name)) for name in launcher.SUPPORTED_NEW_PROJECT_CLIS
    )


def _runtime_field(role: str, *, default: str, configured: str = "") -> Field:
    from scripts import team_launcher as launcher

    choices = launcher._runtime_choices()
    if configured:
        choices = launcher.with_existing_value(choices, configured)
    return launcher.Field(
        name="runtime",
        kind=launcher.KIND_SINGLE,
        title=f"{role} runtime",
        choices=choices,
        default=configured or default,
    )


def _prompt_cli(
    role: str,
    *,
    default: str,
    input_func: Callable[[str], str] = input,
    print_func: Callable[[str], None] = print,
) -> str:
    """Choose a role's runtime from the list, rather than recall one.

    This used to render the alternatives into the prompt text -- `director CLI
    (claude/codex/agy/hermes)` -- and read back whatever was typed. The set was
    always finite and always known; it just was not shown as a set (SYRD-115).
    """
    from scripts import team_launcher as launcher

    default_cli = launcher._validate_new_project_cli(default, context=f"default CLI for {role}")
    try:
        return launcher.terminal_select.select_one(
            launcher._runtime_field(role, default=default_cli),
            input_func=input_func,
            print_func=print_func,
        )
    except launcher.terminal_select.Cancelled:
        raise SystemExit(f"switchyard: too many invalid answers for {role} CLI") from None


@dataclass(frozen=True)
class RoleSelection:
    """One role, fully chosen: what runs it, on which model, at what effort."""

    role: str
    cli: str
    model: str = ""
    effort: str = ""
