"""A role's fresh-per-ticket start, as its project's workflow declares it (SYRD-689).

`fresh_session_per_ticket` makes a launch set a role's recorded conversation aside, and since SYRD-626 ask
the board what the role's work needs first; with no active ticket the answer is a fresh start. That is the
ticket boundary of an *ephemeral* role. MEFP's Director, which its workflow declares a persistent system
role, carried the flag in its generated config -- `project_roles` copied every existing launcher field
through and reconciled only `ephemeral` -- so a restart started it fresh, and the User had to `/resume`.

So in a config the workflow projected (one that carries its `workflow`), the workflow is the authority:

- `declared` is each declared role's ephemerality, from that document;
- `roles_as_declared` is what `load_project_config` returns for those roles: `fresh_session_per_ticket` and
  `ephemeral` exactly as declared, whatever the file says, so no launch, reload or notify path acts on a
  stale flag before anything else runs;
- `mismatches` and `mismatch_lines` say where the file disagrees, for `switchyard status` and, through
  `report_projection_mismatches`, the launch,
  with the repair: the projection writes the flag from the declared role (`workflow_launcher.project_roles`),
  and `switchyard finish-upgrade <project>`, from the Director's own session, rewrites a stale one without
  restarting anything.

A config with no `workflow` -- hand-written, or provisioned before declarative workflows -- keeps the
per-role opt-in `fresh_session_per_ticket` always was (PGU-917).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

if TYPE_CHECKING:
    from scripts.team_launcher import RoleConfig


@dataclass(frozen=True)
class Mismatch:
    role: str
    #: What the generated file says: fresh_session_per_ticket, ephemeral.
    fresh: bool
    ephemeral: bool
    #: What the workflow declares.
    declared: bool


def declared(document: Mapping[str, Any]) -> dict[str, bool] | None:
    """{role: declared ephemeral} from the workflow a projected config carries, or None for a config with none."""
    workflow = document.get("workflow")
    roles = workflow.get("roles") if isinstance(workflow, Mapping) else None
    if not isinstance(roles, list):
        return None
    return {str(spec["name"]): spec.get("ephemeral") is True
            for spec in roles if isinstance(spec, Mapping) and spec.get("name")}


def roles_as_declared(document: Mapping[str, Any], roles: Sequence[RoleConfig]) -> list[RoleConfig]:
    """`roles` with each declared role's fresh-per-ticket start and ephemerality as its workflow declares them."""
    wanted = declared(document)
    if wanted is None:
        return list(roles)
    return [replace(role, fresh_session_per_ticket=wanted[role.role], ephemeral=wanted[role.role])
            if role.role in wanted else role for role in roles]


def mismatches(document: Mapping[str, Any]) -> list[Mismatch]:
    """Each declared role whose generated entry says otherwise than its workflow."""
    wanted = declared(document)
    if wanted is None:
        return []
    found = []
    for raw in document.get("roles") or []:
        name = str(raw.get("role") or "").strip() if isinstance(raw, Mapping) else ""
        if name not in wanted:
            continue
        fresh, ephemeral = raw.get("fresh_session_per_ticket") is True, raw.get("ephemeral") is True
        if (fresh, ephemeral) != (wanted[name], wanted[name]):
            found.append(Mismatch(name, fresh=fresh, ephemeral=ephemeral, declared=wanted[name]))
    return found


def mismatch_lines(project: str, document: Mapping[str, Any]) -> list[str]:
    """What a launch and `switchyard status` say about a stale projection: one line per role, and the repair."""
    lines = []
    for found in mismatches(document):
        said = ", ".join(f"{field} {'true' if value else 'false'}" for field, value in
                         (("fresh_session_per_ticket", found.fresh), ("ephemeral", found.ephemeral)))
        declared_as = ("ephemeral: a fresh conversation per ticket" if found.declared else
                       "persistent: it resumes its recorded conversation")
        lines.append(f"switchyard: {project}'s generated config gives {found.role} {said}, but its workflow declares "
                     f"{found.role} {declared_as}; it is launched as declared")
    if lines:
        lines.append(f"switchyard: to rewrite {project}'s projection from its workflow, its Director runs "
                     f"`switchyard finish-upgrade {project}` from its own session; nothing is restarted")
    return lines


def report_projection_mismatches(config, *, config_path: Path, print_func) -> None:
    """What a launch says, before anything starts, about a projection that disagrees with its workflow."""
    document, _problem = read_document(config_path, config.project)
    for line in mismatch_lines(config.project, document or {}):
        print_func(line)


def read_document(config_path: Path, project: str) -> tuple[dict[str, Any] | None, str]:
    """The generated config as `load_project_config` reads it: root through the no-follow tenant reader."""
    from scripts import team_launcher as launcher

    if os.geteuid() == 0:
        return launcher.read_tenant_document_no_follow(config_path, what=f"{project}'s generated configuration")
    try:
        return launcher._load_json(config_path), ""
    except (OSError, ValueError, SystemExit) as exc:
        return None, str(exc)
