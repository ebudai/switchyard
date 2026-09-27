"""Root's record of the accounts a tenant is moving onto, kept before the tenant moves.

- `PENDING_IDENTITIES_SCHEMA` names the record's format, and
  `pending_identities_path` places it in the tenant's root-only provisioning
  directory.
- `write_pending_identities` publishes the canonical identities there as root
  -- staged beside the record with `O_NOFOLLOW`, at the mode its consumer
  needs, given to root, and renamed into place -- and returns them either way.
- `read_pending_identities` reads the record back, falling back to the
  canonical derivation when there is none or it is not one; and
  `pending_identity_for` answers for one role: the account it runs as now, or
  the one it is being prepared for.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-385), in their original
order. The launcher imports this module and re-exports every name, so the three
modules that read these through it -- credential seeding, the identity cutover
and the upgrade's phases -- and every suite that patches them there reach the
same objects. Every launcher facility these use -- the current user, the
canonical identities, the provision root and directory, the directory repair,
the mode selector and the home lookup, which stay elsewhere -- and every name
defined here that another definition here reads when it runs, the schema
included, is read from `team_launcher` when it runs, as it was, so a patch on
the launcher still intercepts. The standard-library names are this module's
own imports, the same objects. `ProjectConfig` and `RoleConfig` are imported
for annotations only. This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig, RoleConfig


PENDING_IDENTITIES_SCHEMA = "switchyard.pending-identities.v1"


def pending_identities_path(config: ProjectConfig) -> Path:
    """Root's record of the accounts a tenant is moving onto, before it moves.

    Preparation -- creating accounts and homes, staging tooling, seeding
    credentials -- has to know the accounts before the active configuration
    names them, because naming them early is what breaks the running roles. The
    plan is root's, so preparation reads an identity list the tenant did not
    write (SYRD-45).
    """
    from scripts import team_launcher as launcher

    return launcher.privileged_provision_dir(
        config.project, root=launcher.switchyard_privileged_provision_root()
    ) / "pending-identities.json"


def write_pending_identities(config: ProjectConfig) -> dict[str, dict[str, str]]:
    """Publish the pending plan where only root can write it."""
    from scripts import team_launcher as launcher

    identities = launcher.canonical_role_identities(config)
    if os.geteuid() != 0:
        return identities
    path = launcher.pending_identities_path(config)
    payload = {
        "schema": launcher.PENDING_IDENTITIES_SCHEMA,
        "project": config.project,
        "owner": config.run_as_user or launcher.current_user_name(),
        "roles": identities,
    }
    try:
        launcher.ensure_privileged_provision_dir(path.parent)
        staged = path.with_name(f".{path.name}.new")
        descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.write(descriptor, (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"))
            os.fchmod(descriptor, launcher.privileged_artifact_mode(path.name))
            try:
                os.fchown(descriptor, 0, 0)
            except OSError:
                pass
        finally:
            os.close(descriptor)
        staged.replace(path)
    except OSError as exc:
        print(f"switchyard: could not record {config.project} pending identities: {exc}", file=sys.stderr)
    return identities


def read_pending_identities(config: ProjectConfig) -> dict[str, dict[str, str]]:
    """Root's pending plan, or the canonical derivation when none is recorded."""
    from scripts import team_launcher as launcher

    try:
        payload = json.loads(launcher.pending_identities_path(config).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return launcher.canonical_role_identities(config)
    if str(payload.get("schema") or "") != launcher.PENDING_IDENTITIES_SCHEMA:
        return launcher.canonical_role_identities(config)
    roles = payload.get("roles")
    if not isinstance(roles, dict):
        return launcher.canonical_role_identities(config)
    return {
        str(role): {
            "account": str(entry.get("account") or ""),
            "home": str(entry.get("home") or ""),
            "worktree": str(entry.get("worktree") or ""),
        }
        for role, entry in roles.items()
        if isinstance(entry, dict) and str(entry.get("account") or "")
    }


def pending_identity_for(config: ProjectConfig, role: RoleConfig) -> dict[str, str]:
    """The account a role runs as now, or the one it is being prepared for."""
    from scripts import team_launcher as launcher

    owner = config.run_as_user or launcher.current_user_name()
    if role.run_as_user and role.run_as_user != owner:
        return {
            "account": role.run_as_user,
            "home": str(launcher.home_dir_for_user(role.run_as_user) or Path("/home") / role.run_as_user),
            "worktree": role.workdir,
        }
    return launcher.read_pending_identities(config).get(role.role, {})
