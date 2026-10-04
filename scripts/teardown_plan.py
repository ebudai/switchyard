"""A registered tenant's recorded plan, read for teardown only (SYRD-543).

Teardown used to parse `plan.json` through the full upgrade parser, so a tenant
whose older plan lacked fields teardown never reads -- `commit_git_dir`,
`audit_roles`, `operation_allowed_roles` (otto, measured 2026-10-04) -- could not
be torn down with the supported command at all, and the only way through was to
fill them in by hand. This reads what teardown's actions use and nothing else,
and checks each of those against what it is used for. The launcher re-exports
every name here; launcher facilities are read through it when they run.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

#: What teardown's own actions read from a recorded plan, and so all it requires of one (SYRD-543).
TEARDOWN_PLAN_FIELDS = (
    "project", "owner_user", "database", "board_root", "board_unit", "listener_unit",
    "tmpfiles_name", "polkit_name", "tenant_control_sudoers_name",
)
_TEARDOWN_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,127}")
_TEARDOWN_USER_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
_TEARDOWN_DATABASE_RE = re.compile(r"[a-z_][a-z0-9_]{0,62}")


@dataclass(frozen=True)
class TeardownPlanFields:
    """A registered tenant's recorded plan, as teardown uses it: the names it removes by, validated."""

    project: str
    owner_user: str
    database: str
    board_root: str
    board_unit: str
    listener_unit: str
    tmpfiles_name: str
    polkit_name: str
    tenant_control_sudoers_name: str


def _teardown_plan_from_json(plan_path: Path, *, project: str, home_base: Path) -> TeardownPlanFields:
    """Read a registered tenant's plan for teardown: what its actions need, and every one of those checked.

    Teardown removes by these names and nothing else, so a plan an upgrade
    could not parse -- an older one missing fields teardown never reads, such as
    `commit_git_dir` -- is still torn down; nothing is filled with a placeholder.
    Fields a reference plan can supply are taken from it exactly as an upgrade
    would. Every name is held to what it is used for: the plan must be this
    project's, the owner and database plain identifiers, each unit, rule and
    grant a bare file name, and the board root a normalized directory strictly
    inside the owner's home -- teardown removes it recursively.
    """
    from scripts import team_launcher as launcher

    document = dict(launcher._load_json(plan_path))
    owner_home = launcher._recorded_owner_home(document)
    if owner_home:
        document.setdefault("owner_home", owner_home)
    reference = launcher._plan_migration_reference(document)
    fields, _added, _unresolved = launcher.migrate_plan_document(document, reference=reference)
    values: dict[str, str] = {}
    for name in TEARDOWN_PLAN_FIELDS:
        value = fields.get(name)
        if not isinstance(value, str) or not value.strip():
            raise SystemExit(
                f"switchyard: {plan_path} records no usable {name!r}, which teardown removes by: "
                + launcher.unresolved_plan_field_reason(name, document, reference)
            )
        values[name] = value.strip()
    problems = []
    if values["project"] != project:
        problems.append(f"it is the plan of {values['project']!r}, not of registered project {project!r}")
    if not _TEARDOWN_USER_RE.fullmatch(values["owner_user"]):
        problems.append(f"owner_user {values['owner_user']!r} is not a local account name")
    if not _TEARDOWN_DATABASE_RE.fullmatch(values["database"]):
        problems.append(f"database {values['database']!r} is not a plain database name")
    for name in ("board_unit", "listener_unit", "tmpfiles_name", "polkit_name", "tenant_control_sudoers_name"):
        if not _TEARDOWN_NAME_RE.fullmatch(values[name]) or ".." in values[name]:
            problems.append(f"{name} {values[name]!r} is not a bare file name")
    owner_home_path = Path(os.path.normpath(home_base / values["owner_user"]))
    board_root = values["board_root"]
    if not os.path.isabs(board_root) or os.path.normpath(board_root) != board_root or Path(board_root) == owner_home_path \
            or owner_home_path not in Path(board_root).parents:
        problems.append(f"board_root {board_root!r} is not a normalized directory inside the owner's home {owner_home_path}")
    if problems:
        raise SystemExit(f"switchyard: refusing to tear down from {plan_path}: " + "; ".join(problems))
    return TeardownPlanFields(**values)
