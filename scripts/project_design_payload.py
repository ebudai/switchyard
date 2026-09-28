"""The project design artifact: its record, the payload it is written as, and its document.

`ProjectDesignArtifact` is the frozen record a design is kept as; the payload
`project_design_artifact_payload` renders it to (schema, design document and
the project's slug, name, prefix, owner, repository, roles, CLIs, models,
policies, gates and grants, in that order), with `_role_cli_map` turning its
role/CLI pairs into a map; and `_project_design_markdown` is the starting
design document.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-443), in their original
order. The launcher imports this module and re-exports all four names;
`new_project_support.py`, `project_design_artifact.py` and
`project_design_command.py` still read them there. What the payload reads when
it runs -- the schema and the role/CLI map -- is read through the launcher, so
a suite that rebinds one there still intercepts it. The record's decorator is
the standard `dataclass`, applied when the class is defined, as before. This
module imports `team_launcher` only inside the payload, when it runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


@dataclass(frozen=True)
class ProjectDesignArtifact:
    project: str
    project_name: str
    ticket_prefix: str
    owner_user: str
    repository: Path
    remote: str
    default_branch: str
    worktree_policy: str
    design_document: Path
    implementer_roles: tuple[str, ...]
    audit_roles: tuple[str, ...]
    role_clis: tuple[tuple[str, str], ...]
    include_designer: bool
    include_audit: bool
    push_policy: str
    gates: dict[str, bool]
    capability_grants: dict[str, object]
    #: What each role was chosen to run on, by stable identifier. Optional, so
    #: every artifact written before these existed still loads, and absent means
    #: "not chosen" rather than "no model" -- the launcher reads a missing model
    #: as the runtime's own default (SYRD-115).
    role_models: tuple[tuple[str, str], ...] = ()
    role_efforts: tuple[tuple[str, str], ...] = ()
    #: Which version of the recorded option catalog those identifiers were
    #: chosen from. A label may be reworded and a catalog may gain entries
    #: without changing what this project runs; this says what it was read off.
    catalog_version: int = 0


def _role_cli_map(role_clis: Sequence[tuple[str, str]]) -> dict[str, str]:
    return {role: cli for role, cli in role_clis}


def project_design_artifact_payload(artifact: ProjectDesignArtifact) -> dict[str, Any]:
    from scripts import team_launcher as launcher

    return {
        "schema": launcher.PROJECT_DESIGN_ARTIFACT_SCHEMA,
        "design_document": str(artifact.design_document),
        "project": {
            "slug": artifact.project,
            "name": artifact.project_name,
            "ticket_prefix": artifact.ticket_prefix,
            "owner_user": artifact.owner_user,
            "repository": str(artifact.repository),
            "remote": artifact.remote,
            "default_branch": artifact.default_branch,
            "worktree_policy": artifact.worktree_policy,
            "roles": list(artifact.implementer_roles),
            "audit_roles": list(artifact.audit_roles),
            "include_designer": artifact.include_designer,
            "include_audit": artifact.include_audit,
            "role_clis": launcher._role_cli_map(artifact.role_clis),
            **({"role_models": dict(artifact.role_models)} if artifact.role_models else {}),
            **({"role_efforts": dict(artifact.role_efforts)} if artifact.role_efforts else {}),
            **({"catalog_version": artifact.catalog_version} if artifact.catalog_version else {}),
            "push_policy": artifact.push_policy,
            "gates": artifact.gates,
            "capability_grants": artifact.capability_grants,
        },
    }


def _project_design_markdown(project: str, *, title: str, body: str) -> str:
    heading = title.strip() or f"{project} design"
    body_text = body.strip() or "TBD."
    return f"# {heading}\n\n{body_text}\n"
