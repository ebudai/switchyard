"""Deciding a project's desktop policy, and saying where the decision came from.

- **At `switchyard new`:** `_resolve_desktop_policy` takes, in order, an
  operator's policy file, `--headless`, this host's recorded desktop approval,
  and finally the answer of the person running the command
  (`_prompt_choice`); it never infers a grant, and offers headless only where
  there is no desktop at all (`_desktop_host_is_headless`). The
  `DESKTOP_FROM_*` values name which of these decided.
- **At recovery:** `approved_desktop_policy` is the policy root will install
  for a tenant it is recovering, from the host's own record and never a desktop
  the tenant names; `install_recovered_desktop_access` completes the install an
  interrupted `switchyard new` never did.
- **At upgrade:** `upgrade_desktop_policy_decision` is the policy an upgrade
  leaves the tenant with, or why there is none -- never copied, never assumed.

The host approval records, the desktop installer, the prompts and the current
user are read from `scripts/team_launcher.py` when a function runs; the
desktop-access helpers are each function's own imports.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-326). `team_launcher`
imports this module at its top and still exports every name callers read there.
This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig
    from scripts.ticket_board.project_provision import ProjectBoardProvision

DESKTOP_FROM_POLICY_FILE = "policy_file"
DESKTOP_FROM_HEADLESS_OPTION = "headless_option"
DESKTOP_FROM_HOST_APPROVAL = "host_approval"
DESKTOP_FROM_NEW_APPROVAL = "new_approval"
DESKTOP_FROM_CHOSEN_HEADLESS = "chosen_headless"


def _prompt_choice(
    label: str,
    *,
    options: Sequence[tuple[str, str]],
    default: str,
    input_func: Callable[[str], str] = input,
    print_func: Callable[[str], None] = print,
) -> str:
    """Pick one of a named few, by number or by name, with a real default.

    A provisioning question whose answer is a path somebody has to author is a
    question most people cannot answer; a numbered list of what this host can
    actually do is one they can. Empty takes the default, which is named in the
    prompt rather than implied (SYRD-143).
    """
    from scripts import team_launcher as launcher

    names = [name for name, _description in options]
    if default not in names:
        raise ValueError(f"default {default!r} is not one of {names}")
    for index, (name, description) in enumerate(options, start=1):
        marker = " [default]" if name == default else ""
        print_func(f"  {index}) {name}: {description}{marker}")
    for _attempt in range(launcher.SWITCHYARD_PROMPT_MAX_ATTEMPTS):
        raw = launcher._read_prompt(f"{label} [{default}]: ", input_func=input_func).strip().lower()
        if not raw:
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(names):
            return names[int(raw) - 1]
        if raw in names:
            return raw
        print_func("answer with the number or the name of one of the choices above")
    raise SystemExit(f"switchyard: too many invalid answers for {label}")


def _resolve_desktop_policy(
    *,
    desktop_policy: Path | None,
    headless: bool,
    gui_user: str,
    project: str,
    tenant: str,
    yes: bool,
    input_func: Callable[[str], str],
    print_func: Callable[[str], None],
    settings_path: Path | None = None,
    owner_resolver: Callable[..., str] | None = None,
    owners_lister: Callable[[], Sequence[str]] | None = None,
) -> tuple[dict[str, Any], str]:
    """Decide this project's desktop policy, and say where the decision came from.

    Four ways in, in this order, because each one is more explicit than the
    next: an operator's own policy file, an explicit `--headless`, a standing
    approval this host has already recorded, and finally asking the person
    running the command. Nothing infers a grant: the last one is a choice
    somebody makes, and the one before it is a choice somebody already made and
    signed (SYRD-143).

    The policy is generated rather than authored because every field except the
    approval is something provisioning already knows -- this project, this
    tenant, this desktop, this socket rule -- and asking a desktop user to write
    them into a JSON file was the interruption this ticket exists to remove.
    """
    from scripts import team_launcher as launcher

    from scripts.desktop_access import (
        DesktopAccessError,
        active_wayland_owners,
        generated_policy,
        resolve_gui_owner,
    )

    if desktop_policy is not None:
        if headless:
            raise SystemExit("switchyard: pass either --headless or --desktop-policy, not both")
        if str(desktop_policy) == "headless":
            return {"mode": "headless"}, DESKTOP_FROM_HEADLESS_OPTION
        return launcher._load_json(desktop_policy), DESKTOP_FROM_POLICY_FILE
    if headless:
        return {"mode": "headless"}, DESKTOP_FROM_HEADLESS_OPTION

    resolve = owner_resolver or resolve_gui_owner
    list_owners = owners_lister or active_wayland_owners
    try:
        owner = resolve(preferred=gui_user)
    except DesktopAccessError as exc:
        # No desktop to grant, or more than one and no way to tell which. The
        # first is an ordinary headless host and the person can say so; the
        # second is an ambiguity that must not be resolved by guessing, so it
        # stops either way rather than being offered as a default.
        if yes or not _desktop_host_is_headless(list_owners):
            raise SystemExit(f"switchyard: {exc}") from exc
        print_func(f"switchyard: {exc}")
        if launcher._prompt_bool(
            "Install this project headless (no screenshots or clipboard)",
            default=True,
            input_func=input_func,
        ):
            return {"mode": "headless"}, DESKTOP_FROM_CHOSEN_HEADLESS
        raise SystemExit(
            "switchyard: no desktop was selected and headless was declined; nothing was provisioned"
        ) from exc

    recorded = launcher.read_host_desktop_approval(settings_path)
    if recorded.get("gui_user") == owner:
        policy = generated_policy(
            project=project,
            tenant=tenant,
            gui_user=owner,
            approved_by=recorded.get("approved_by") or owner,
            reference=recorded.get("reference"),
        )
        print_func(
            f"switchyard: using {owner}'s recorded desktop approval for {project}; "
            f"screenshots and clipboard are enabled for {tenant}"
        )
        return policy, DESKTOP_FROM_HOST_APPROVAL
    if yes:
        raise SystemExit(
            "switchyard: --yes does not grant desktop access, and this host has no recorded "
            f"approval from {owner}. Run switchyard new interactively once to record one, or "
            "pass --headless, or supply --desktop-policy FILE."
        )
    print_func(
        f"switchyard: {owner} is signed into this host's desktop. Switchyard can give "
        f"{project} scoped access to that session, which is what lets a role paste a "
        "screenshot."
    )
    choice = _prompt_choice(
        "Desktop access",
        options=(
            ("desktop", f"screenshots and clipboard through {owner}'s session (recommended)"),
            ("headless", "no screenshots or clipboard"),
        ),
        default="desktop",
        input_func=input_func,
        print_func=print_func,
    )
    if choice == "headless":
        return {"mode": "headless"}, DESKTOP_FROM_CHOSEN_HEADLESS
    try:
        path = launcher.write_host_desktop_approval(owner, settings_path=settings_path, confirmed_by=owner)
    except OSError as exc:
        # Root-owned on purpose: an approval a tenant could write is an approval
        # a tenant could give itself. A run that cannot write it is a run that
        # was not privileged, which `new` requires anyway.
        raise SystemExit(
            f"switchyard: cannot record this host's desktop approval ({exc}); "
            "re-run as `sudo ./switchyard new`"
        ) from exc
    print_func(
        f"switchyard: recorded {owner}'s desktop approval ({path}); later projects use it "
        "without asking again"
    )
    recorded = launcher.read_host_desktop_approval(settings_path)
    return (
        generated_policy(
            project=project,
            tenant=tenant,
            gui_user=owner,
            approved_by=recorded.get("approved_by") or owner,
            reference=recorded.get("reference"),
        ),
        DESKTOP_FROM_NEW_APPROVAL,
    )


def _desktop_host_is_headless(list_owners: Callable[[], Sequence[str]]) -> bool:
    """Whether the refusal above was "no desktop" rather than "which desktop".

    Asked by looking again rather than by reading the message: an ambiguous
    host has owners and a headless one has none, and that is the difference
    that decides whether headless may be offered at all. Offering it on an
    ambiguous host would turn "which of these desktops" into "never mind then",
    which is how a project quietly loses the access somebody wanted.
    """
    try:
        return not list(list_owners())
    except Exception:
        return True


def approved_desktop_policy(
    plan: "ProjectBoardProvision",
    config: ProjectConfig,
    *,
    approval_path: Path | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """The desktop policy root will install for a tenant it is recovering.

    The GUI owner comes from this host's approval record, which is root-owned
    for the reason the comment on `write_host_desktop_approval` gives: an
    approval a tenant could write is an approval a tenant could give itself.
    The tenant's own configuration is writable by the account every role runs
    as, so it may say that this project already has a policy and it may carry
    the attribution of the consent that was recorded for it -- but it may not
    name a different desktop, and it cannot conjure an approval this host has
    never recorded.

    Returns no policy at all when there is nothing to install: a headless
    tenant, or a host whose desktop owner never approved anything and whose
    tenant is not asking for access either (SYRD-158).
    """
    from scripts import team_launcher as launcher

    from scripts import desktop_access as desktop

    declared = config.desktop_access
    tenant = config.run_as_user or plan.owner_user
    if isinstance(declared, dict) and declared.get("mode") == "headless":
        return None, []
    recorded = launcher.read_host_desktop_approval(approval_path)
    gui_user = str(recorded.get("gui_user") or "").strip()
    if declared is None and not gui_user:
        return None, []
    if not gui_user:
        return None, [
            f"{plan.project} asks for desktop access, and this host has no recorded desktop "
            f"approval to grant it from. Record one through `switchyard new` on this host, or "
            f"set this project headless; a tenant's own configuration does not authorize a "
            f"grant to somebody else's session."
        ]
    if declared is not None:
        try:
            policy = desktop.validate_policy(declared, project=plan.project, tenant=tenant)
        except desktop.DesktopAccessError as exc:
            return None, [f"{plan.project}'s recorded desktop policy is not usable: {exc}"]
        if policy["gui_user"] != gui_user:
            return None, [
                f"{plan.project}'s configuration asks for {policy['gui_user']}'s desktop, and "
                f"this host's approval record names {gui_user}. Which desktop a tenant may reach "
                "is not the tenant's to answer; nothing was changed."
            ]
        return policy, []
    # Nothing declared, and an approval that covers this host: generate the same
    # policy provisioning would have, from the record rather than from a prompt.
    try:
        policy = desktop.generated_policy(
            project=plan.project,
            tenant=tenant,
            gui_user=gui_user,
            approved_by=recorded.get("approved_by") or gui_user,
            approved_at=recorded.get("approved_at") or "",
            reference=recorded.get("reference") or "",
        )
    except desktop.DesktopAccessError as exc:
        return None, [f"this host's desktop approval cannot be used for {plan.project}: {exc}"]
    return policy, []


def install_recovered_desktop_access(
    plan: "ProjectBoardProvision",
    config: ProjectConfig,
    config_path: Path,
    *,
    source_release: Path | None = None,
    approval_path: Path | None = None,
    installer: Callable[..., ProjectConfig] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> tuple[ProjectConfig | None, bool]:
    """Complete the desktop install the interrupted `switchyard new` never did.

    `switchyard new` installs the scoped grant and then verifies it; a launch
    only ever verifies. So a recovery that went straight to launching asked the
    tenant to prove access it had never been given, and stopped on a receipt
    that nothing had written -- with the approved policy sitting intact on
    disk. The install is the supported one, with its own rollback: what it
    cannot complete it puts back.
    """
    from scripts import team_launcher as launcher

    policy, objections = approved_desktop_policy(plan, config, approval_path=approval_path)
    if objections:
        for objection in objections:
            print_func(f"switchyard: {objection}")
        return None, False
    if policy is None:
        return config, True
    helper = None
    if source_release is not None:
        candidate = Path(source_release) / "scripts" / "desktop_access.py"
        if candidate.is_file():
            helper = candidate
    configure = installer or launcher.configure_project_desktop
    try:
        configured = configure(
            replace(config, desktop_access=policy),
            config_path=config_path,
            helper=helper,
            runner=runner,
        )
    except (SystemExit, OSError) as exc:
        print_func(f"switchyard: {plan.project}'s desktop access could not be installed: {exc}")
        return None, False
    print_func(
        f"switchyard: {plan.project} has scoped access to {policy['gui_user']}'s desktop "
        f"session for {policy['tenant_user']}"
    )
    return configured, True


def upgrade_desktop_policy_decision(
    config: ProjectConfig,
    desktop_policy: Path | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """What stands between this tenant and a desktop policy its roles can launch with.

    The policy the upgrade will leave the tenant with, and nothing standing in
    the way -- or no policy, and why not. Valid means valid for THIS project and
    tenant. Never filled in: Wayland access is per tenant, a policy
    cannot be inferred from another project, and headless is a choice somebody
    has to make rather than a default this can pick for them (SYRD-232).
    """
    from scripts import team_launcher as launcher

    from scripts import desktop_access as desktop

    tenant = config.run_as_user or launcher.current_user_name()
    if desktop_policy is None and config.desktop_access is None:
        return None, [
            f"{config.project} has no desktop policy, and every role launch needs one. "
            "An operator has to choose it for this tenant: it is never copied from another "
            "project, and Wayland access is never assumed.",
            f"  headless -- no desktop access for any role:  "
            f"switchyard upgrade {config.project} --desktop-policy headless",
            f"  Wayland  -- an approved policy scoped to project {config.project!r} and tenant "
            f"{tenant!r}, carrying this tenant's own recorded consent:  "
            f"switchyard upgrade {config.project} --desktop-policy FILE",
        ]
    try:
        if desktop_policy is not None:
            raw = ({"mode": "headless"} if str(desktop_policy) == "headless"
                   else launcher._load_json(desktop_policy))
        else:
            raw = config.desktop_access
        policy = desktop.validate_policy(raw, project=config.project, tenant=tenant)
    except (desktop.DesktopAccessError, OSError, ValueError) as exc:
        where = (f"the policy supplied with --desktop-policy ({desktop_policy})"
                 if desktop_policy is not None else f"{config.project}'s recorded policy")
        return None, [f"{where} cannot be used for {config.project}: {exc}"]
    return policy, []
