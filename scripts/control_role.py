"""A tenant's control role: which configured role controls it, and why not.

`control_role_name` names the configured role that controls a tenant -- the one
its workflow gives the control capabilities (`CONTROL_ROLE_CAPABILITIES`) --
and refuses with a reason when none or several do. Only a tenant with no
workflow document falls back to the historical `director` (SYRD-49).
`director_role_name` is the name alone.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-440), in their original
order. The launcher imports this module and re-exports all three names;
`scripts/director_upgrade.py` and `scripts/role_account_migration.py` still
read `control_role_name` there. Everything they read when they run -- each
other, the capabilities and the launcher's JSON reader -- is read through the
launcher, so a suite that rebinds one there still intercepts it. This module
imports `team_launcher` only inside the functions, when they run.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Mapping

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig


# What makes a role the tenant's control role is what the workflow lets it do,
# not what it is called. These are the capabilities that take a ticket out of
# the ordinary flow, and no implementer or reviewer role carries them (SYRD-49).
CONTROL_ROLE_CAPABILITIES = frozenset({"set_manually_controlled", "merge"})


def control_role_name(
    config: ProjectConfig, *, config_path: Path | None = None
) -> tuple[str, str]:
    """The configured role that controls this tenant, and why not when it is not.

    A declarative tenant says which role that is by giving it the control
    capabilities, so the name is the tenant's to choose. Zero matches and more
    than one both fail closed: a privileged grant is not something to guess at.
    Only a tenant with no workflow document falls back to the historical name
    (SYRD-49).
    """
    from scripts import team_launcher as launcher

    document = None
    if config_path is not None:
        try:
            document = (launcher._load_json(config_path) or {}).get("workflow")
        except SystemExit:
            document = None
    configured = {role.role for role in config.roles}
    if isinstance(document, Mapping) and document.get("roles"):
        matches = [
            str(role.get("name") or "")
            for role in document.get("roles") or []
            if isinstance(role, Mapping)
            and role.get("active", True)
            and launcher.CONTROL_ROLE_CAPABILITIES <= set(role.get("capabilities") or [])
        ]
        present = [name for name in matches if name in configured]
        if not present:
            return "", (
                "this project's workflow declares no active role with the control capabilities "
                f"({', '.join(sorted(launcher.CONTROL_ROLE_CAPABILITIES))})"
            )
        if len(present) > 1:
            return "", (
                "this project's workflow gives the control capabilities to more than one role: "
                + ", ".join(sorted(present))
            )
        return present[0], ""
    if "director" in configured:
        return "director", ""
    return "", "this project configures no director role"


def director_role_name(config: ProjectConfig, *, config_path: Path | None = None) -> str:
    """The control role's name, or empty when it cannot be established."""
    from scripts import team_launcher as launcher

    name, _reason = launcher.control_role_name(config, config_path=config_path)
    return name
