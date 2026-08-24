"""Render per-project ticket-board provisioning artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Sequence


PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
USER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
ROLE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
TICKET_PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9]*$")
DEFAULT_IMPLEMENTER_ROLES = ("main", "app", "ops", "perf", "research")
DEFAULT_PROJECT_DRAFT_ROLES = ("designer",)
DEFAULT_PROJECT_IMPLEMENTER_ROLES = ("ops",)
DEFAULT_PGU_ASSIGNEES = ("unassigned", "main", "app", "perf", "ops", "audit", "inspector", "agent", "director", "research", "user")
DEFAULT_PGU_CALLER_ROLES = ("director", "main", "app", "ops", "perf", "audit", "inspector", "research", "user")


@dataclass(frozen=True)
class ProjectBoardProvision:
    project: str
    ticket_prefix: str
    owner_user: str
    service_user: str
    board_unit: str
    listener_unit: str
    tmpfiles_name: str
    polkit_name: str
    runtime_directory: str
    socket_path: str
    port: int
    database: str
    service_role: str
    listener_role: str
    board_root: str
    board_current: str
    source_repo: str
    asset_dir: str
    frame_dir: str
    board_log: str
    listener_log: str
    board_database_url: str
    listener_database_url: str
    admin_database_url: str
    workflow_seed: str
    draft_roles: tuple[str, ...]
    implementer_roles: tuple[str, ...]
    assignee_roles: tuple[str, ...]
    caller_roles: tuple[str, ...]


def _validate_project(value: str) -> str:
    project = value.strip().lower()
    if not PROJECT_RE.fullmatch(project):
        raise SystemExit("project must match ^[a-z0-9][a-z0-9-]{0,39}$")
    return project


def _validate_user(value: str) -> str:
    user = value.strip()
    if not USER_RE.fullmatch(user):
        raise SystemExit("owner user must look like a local Unix account name")
    return user


def _validate_role(value: str) -> str:
    role = value.strip().lower()
    if not ROLE_RE.fullmatch(role):
        raise SystemExit("role names must match ^[a-z][a-z0-9_-]{0,63}$")
    return role


def validate_ticket_prefix(value: str) -> str:
    prefix = re.sub(r"[^A-Za-z0-9]+", "", value.strip()).upper()
    if not prefix:
        raise SystemExit("ticket prefix must contain at least one ASCII letter or digit")
    if prefix[0].isdigit():
        prefix = f"T{prefix}"
    if not TICKET_PREFIX_RE.fullmatch(prefix):
        raise SystemExit("ticket prefix must normalize to ^[A-Z][A-Z0-9]*$")
    return prefix


def _dedupe(values: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return tuple(result)


def _identifier_from_project(project: str) -> str:
    return project.replace("-", "_")


def allocated_port(project: str, *, base: int = 18_770, span: int = 10_000) -> int:
    if project == "pgu":
        return 8770
    digest = hashlib.blake2s(project.encode("utf-8"), digest_size=2).digest()
    offset = int.from_bytes(digest, "big") % span
    return base + offset


def build_plan(
    *,
    project: str,
    owner_user: str,
    port: int | None = None,
    database: str | None = None,
    service_user: str = "boardsvc",
    service_role: str = "ticket_board_service",
    listener_role: str = "ticket_board_listener",
    board_root: Path | None = None,
    source_repo: Path | None = None,
    asset_dir: Path | None = None,
    frame_dir: Path | None = None,
    implementer_roles: Sequence[str] | None = None,
    ticket_prefix: str | None = None,
) -> ProjectBoardProvision:
    project = _validate_project(project)
    owner_user = _validate_user(owner_user)
    resolved_ticket_prefix = validate_ticket_prefix(ticket_prefix or project)
    if service_user:
        _validate_user(service_user)
    if service_role != "ticket_board_service" or listener_role != "ticket_board_listener":
        raise SystemExit(
            "custom database service/listener roles are not supported yet; "
            "the current schema enforces ticket_board_service and ticket_board_listener"
        )
    if implementer_roles is None:
        resolved_implementer_roles = DEFAULT_IMPLEMENTER_ROLES if project == "pgu" else DEFAULT_PROJECT_IMPLEMENTER_ROLES
    else:
        resolved_implementer_roles = tuple(_validate_role(role) for role in implementer_roles)
        if not resolved_implementer_roles:
            raise SystemExit("at least one implementer role is required")
    resolved_implementer_roles = _dedupe(resolved_implementer_roles)
    if project == "pgu":
        draft_roles: tuple[str, ...] = ()
        assignee_roles = DEFAULT_PGU_ASSIGNEES
        caller_roles = DEFAULT_PGU_CALLER_ROLES
    else:
        draft_roles = DEFAULT_PROJECT_DRAFT_ROLES
        assignee_roles = _dedupe(("unassigned", *draft_roles, *resolved_implementer_roles, "audit", "director", "user"))
        caller_roles = _dedupe(("director", *draft_roles, *resolved_implementer_roles, "audit", "user"))
    ident = _identifier_from_project(project)
    unit_prefix = f"{project}-ticket-board"
    runtime_directory = unit_prefix
    home = Path("/home") / owner_user
    resolved_board_root = board_root or home / f"{project}-ticketboard-live"
    resolved_source_repo = source_repo or Path(__file__).resolve().parents[2]
    resolved_asset_dir = asset_dir or home / ".claude" / f"{project}-tickets-assets"
    if frame_dir is not None:
        resolved_frame_dir = frame_dir
    elif project == "pgu":
        resolved_frame_dir = Path("/tmp/pgu-frames")
    else:
        resolved_frame_dir = home / ".claude" / f"{project}-ticket-frames"
    resolved_database = database or ("pgu" if project == "pgu" else f"{ident}_ticket_board")
    resolved_port = port if port is not None else allocated_port(project)
    if not (1 <= resolved_port <= 65535):
        raise SystemExit("port must be between 1 and 65535")
    board_current = resolved_board_root / "current"
    return ProjectBoardProvision(
        project=project,
        ticket_prefix=resolved_ticket_prefix,
        owner_user=owner_user,
        service_user=service_user,
        board_unit=f"{unit_prefix}.service",
        listener_unit=f"{unit_prefix}-notify-listener.service",
        tmpfiles_name=f"{unit_prefix}.conf",
        polkit_name=f"49-{unit_prefix}-deploy.rules",
        runtime_directory=runtime_directory,
        socket_path=f"/run/{runtime_directory}/ticket-board.sock",
        port=resolved_port,
        database=resolved_database,
        service_role=service_role,
        listener_role=listener_role,
        board_root=str(resolved_board_root),
        board_current=str(board_current),
        source_repo=str(resolved_source_repo),
        asset_dir=str(resolved_asset_dir),
        frame_dir=str(resolved_frame_dir),
        board_log=f"/var/log/{unit_prefix}.log",
        listener_log=str(home / ".local" / "state" / f"{unit_prefix}-notify-listener.log"),
        board_database_url=(
            f"postgresql:///{resolved_database}?host=/var/run/postgresql&user={service_role}"
        ),
        listener_database_url=(
            f"postgresql:///{resolved_database}?host=/var/run/postgresql&user={listener_role}"
        ),
        admin_database_url=f"postgresql:///{resolved_database}?host=/var/run/postgresql&user=postgres",
        workflow_seed="pgu-full" if project == "pgu" else "default-five-stage",
        draft_roles=draft_roles,
        implementer_roles=resolved_implementer_roles,
        assignee_roles=assignee_roles,
        caller_roles=caller_roles,
    )


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def sql_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def sql_text_array(values: Sequence[str]) -> str:
    return "ARRAY[" + ", ".join(sql_literal(value) for value in values) + "]::text[]"


def env_list(values: Sequence[str]) -> str:
    return ",".join(values)


def owned_ancestor_dirs(owner_user: str, target: str, *, include_target: bool) -> tuple[str, ...]:
    home = PurePosixPath("/home") / owner_user
    path = PurePosixPath(target)
    try:
        path.relative_to(home)
    except ValueError:
        return ()
    end = path if include_target else path.parent
    if end == home:
        return ()
    relative_parts = end.relative_to(home).parts
    return tuple(str(home.joinpath(*relative_parts[:index])) for index in range(1, len(relative_parts) + 1))


def owned_directory_command(plan: ProjectBoardProvision, dirs: Sequence[str], *, mode: str = "0755") -> str:
    unique_dirs = _dedupe(tuple(dirs))
    if not unique_dirs:
        return ""
    quoted_dirs = " ".join(shell_quote(value) for value in unique_dirs)
    return (
        f"sudo install -d -m {mode} -o {shell_quote(plan.owner_user)} "
        f"-g {shell_quote(plan.owner_user)} {quoted_dirs}"
    )


def render_board_unit(plan: ProjectBoardProvision) -> str:
    return f"""[Unit]
Description={plan.project} Ticket Board
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User={plan.service_user}
WorkingDirectory={plan.board_current}
RuntimeDirectory={plan.runtime_directory}
ExecStart=/usr/bin/python3 {plan.board_current}/scripts/ticket-board.py --host 127.0.0.1 --port {plan.port} --unix-socket {plan.socket_path} --frames {plan.frame_dir} --assets {plan.asset_dir}
Restart=on-failure
RestartSec=2
Environment=PYTHONUNBUFFERED=1
Environment=HOME=/home/{plan.owner_user}
Environment=TICKET_BOARD_PROJECT={plan.project}
Environment=TICKET_BOARD_TICKET_PREFIX={plan.ticket_prefix}
Environment=PGHOST=/var/run/postgresql
Environment=PGDATABASE={plan.database}
Environment=PGUSER={plan.service_role}
Environment=TICKET_BOARD_SOCKET={plan.socket_path}
Environment=TICKET_BOARD_DATABASE_URL={plan.board_database_url}
Environment=TICKET_BOARD_DRAFT_ROLES={env_list(plan.draft_roles)}
Environment=TICKET_BOARD_IMPLEMENTER_ROLES={env_list(plan.implementer_roles)}
Environment=TICKET_BOARD_ASSIGNEES={env_list(plan.assignee_roles)}
Environment=TICKET_BOARD_CALLER_ROLES={env_list(plan.caller_roles)}
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths={plan.asset_dir} {plan.frame_dir}
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""


def render_listener_unit(plan: ProjectBoardProvision) -> str:
    return f"""[Unit]
Description={plan.project} Ticket Board Notify Listener
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
WorkingDirectory={plan.board_current}
ExecStart=/usr/bin/python3 {plan.board_current}/scripts/ticket-board-notify-listener
Restart=always
RestartSec=2
Environment=PYTHONUNBUFFERED=1
Environment=TICKET_BOARD_PROJECT={plan.project}
Environment=PGHOST=/var/run/postgresql
Environment=PGDATABASE={plan.database}
Environment=PGUSER={plan.listener_role}
Environment=TICKET_BOARD_DATABASE_URL={plan.listener_database_url}
Environment=TICKET_BOARD_NOTIFY_DATABASE_URL={plan.listener_database_url}
Environment=TICKET_BOARD_PANE_STATE_DIR=%t/{plan.runtime_directory}/pane-state
EnvironmentFile=-%h/.config/{plan.project}/ticket-board-notify-listener.env
StandardOutput=append:{plan.listener_log}
StandardError=append:{plan.listener_log}

[Install]
WantedBy=default.target
"""


def render_tmpfiles(plan: ProjectBoardProvision) -> str:
    if plan.project == "pgu" and plan.frame_dir == "/tmp/pgu-frames":
        return "d /tmp/pgu-frames 1777 root root -\n"
    return f"d {plan.frame_dir} 0775 {plan.owner_user} {plan.owner_user} -\n"


def render_polkit_rule(plan: ProjectBoardProvision) -> str:
    return f"""// Allow {plan.owner_user} to deploy the {plan.project} board without blanket sudo.
// Scope is intentionally limited to fixed, root-owned board service units.
// Do NOT add org.freedesktop.systemd1.manage-unit-files here. On systemd
// policy, that action can imply both reload-daemon and manage-units, which
// turns this narrow per-unit grant into global daemon-reload plus unrestricted
// unit management. reload-daemon cannot be scoped to a unit; it carries no unit
// detail for polkit to inspect.
polkit.addRule(function(action, subject) {{
    if (subject.user !== "{plan.owner_user}") {{
        return polkit.Result.NOT_HANDLED;
    }}
    if (action.id !== "org.freedesktop.systemd1.manage-units") {{
        return polkit.Result.NOT_HANDLED;
    }}

    var unit = action.lookup("unit");
    var verb = action.lookup("verb");
    if (unit === "{plan.board_unit}") {{
        if (verb === "start" || verb === "stop" || verb === "restart") {{
            return polkit.Result.YES;
        }}
        return polkit.Result.NOT_HANDLED;
    }}

    if (unit === "{plan.project}-ticket-board-canary.service") {{
        if (verb === "start" || verb === "stop") {{
            return polkit.Result.YES;
        }}
    }}

    return polkit.Result.NOT_HANDLED;
}});
"""


def render_database_sql(plan: ProjectBoardProvision) -> str:
    db_ident = sql_identifier(plan.database)
    return f"""-- Bootstrap database container for {plan.project}.
-- Run as a PostgreSQL admin before applying schema.sql, migrations, and rbac.sql
-- inside database {plan.database}.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{plan.service_role}') THEN
        CREATE ROLE {plan.service_role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{plan.listener_role}') THEN
        CREATE ROLE {plan.listener_role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
END;
$$;

SELECT format('CREATE DATABASE %I', '{plan.database}')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = '{plan.database}')\\gexec

COMMENT ON DATABASE {db_ident} IS 'ticket board database for project {plan.project}';
"""


def render_project_role_constraint_sql(plan: ProjectBoardProvision) -> str:
    notification_roles = tuple(role for role in plan.assignee_roles if role not in {"unassigned", "agent", "user"})
    return f"""
ALTER TABLE ticket_board.tickets
    DROP CONSTRAINT IF EXISTS tickets_assignee_check;
ALTER TABLE ticket_board.tickets
    ADD CONSTRAINT tickets_assignee_check CHECK (assignee IN ({", ".join(sql_literal(role) for role in plan.assignee_roles)}));

ALTER TABLE ticket_board.ticket_notification_state
    DROP CONSTRAINT IF EXISTS ticket_notification_state_awaiting_role_check;
ALTER TABLE ticket_board.ticket_notification_state
    ADD CONSTRAINT ticket_notification_state_awaiting_role_check
    CHECK (
        awaiting_role = ''
        OR awaiting_role IN ({", ".join(sql_literal(role) for role in notification_roles)})
    );

ALTER TABLE ticket_board.ticket_notification_state
    DROP CONSTRAINT IF EXISTS ticket_notification_state_last_implementer_assignee_check;
ALTER TABLE ticket_board.ticket_notification_state
    ADD CONSTRAINT ticket_notification_state_last_implementer_assignee_check
    CHECK (
        last_implementer_assignee = ''
        OR last_implementer_assignee IN ({", ".join(sql_literal(role) for role in plan.implementer_roles)})
    );

ALTER TABLE ticket_board.ticket_notification_queue
    DROP CONSTRAINT IF EXISTS ticket_notification_queue_target_role_check;
ALTER TABLE ticket_board.ticket_notification_queue
    ADD CONSTRAINT ticket_notification_queue_target_role_check
    CHECK (target_role IN ({", ".join(sql_literal(role) for role in notification_roles)}));
"""


def render_workflow_sql(plan: ProjectBoardProvision) -> str:
    if plan.workflow_seed == "pgu-full":
        return f"""-- {plan.project} keeps the full workflow seeded by schema.sql.
-- No per-project workflow override is applied.
"""

    stages = [
        ("draft", "Draft", 0, plan.draft_roles, None, None, None, False),
        ("analysis", "Triage", 1, ("director",), None, None, None, False),
        ("in_progress", "Implementation", 2, plan.implementer_roles, None, None, None, False),
        ("audit", "Audit", 3, ("audit",), None, None, "audit_signoff", False),
        ("director_review", "Final Sign-Off", 4, ("director",), None, None, None, False),
    ]
    transitions = [
        ("draft", "analysis", "release_draft", _dedupe((*plan.draft_roles, "director", "user")), False, False),
        ("analysis", "in_progress", "route", ("director",), False, False),
        ("analysis", "in_progress", "start_work", plan.implementer_roles, False, False),
        ("in_progress", "audit", "route", ("director",), False, False),
        ("in_progress", "audit", "submit_to_audit", plan.implementer_roles, False, False),
        ("audit", "director_review", "route", ("director",), False, False),
        ("audit", "director_review", "audit_sign_off", ("audit",), False, False),
    ]
    stage_rows = ",\n".join(
        "    ("
        + ", ".join(
            [
                sql_literal(name),
                sql_literal(label),
                str(rank),
                sql_text_array(owner_roles),
                "NULL" if entry_gate_field is None else sql_literal(entry_gate_field),
                "NULL" if gate_skip_to is None else sql_literal(gate_skip_to),
                "NULL" if exit_signoff_field is None else sql_literal(exit_signoff_field),
                "true" if is_terminal else "false",
            ]
        )
        + ")"
        for name, label, rank, owner_roles, entry_gate_field, gate_skip_to, exit_signoff_field, is_terminal in stages
    )
    transition_rows = ",\n".join(
        "    ("
        + ", ".join(
            [
                sql_literal(from_stage),
                sql_literal(to_stage),
                sql_literal(action_name),
                sql_text_array(allowed_roles),
                "true" if owner_scoped else "false",
                "true" if director_override else "false",
            ]
        )
        + ")"
        for from_stage, to_stage, action_name, allowed_roles, owner_scoped, director_override in transitions
    )
    return f"""-- Seed the default five-stage workflow for {plan.project}.
-- Run after schema.sql/migrations and before rbac.sql on a newly provisioned board.
BEGIN;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM ticket_board.tickets) THEN
        RAISE EXCEPTION 'project workflow seed must run before tickets exist';
    END IF;
END;
$$;
{render_project_role_constraint_sql(plan)}

DELETE FROM ticket_board.workflow_transitions;
UPDATE ticket_board.workflow_stages SET gate_skip_to = NULL WHERE gate_skip_to IS NOT NULL;
DELETE FROM ticket_board.workflow_stages;

INSERT INTO ticket_board.workflow_stages (
    name,
    display_label,
    rank,
    owner_roles,
    entry_gate_field,
    gate_skip_to,
    exit_signoff_field,
    is_terminal
) VALUES
{stage_rows};

INSERT INTO ticket_board.workflow_transitions (
    from_stage,
    to_stage,
    action_name,
    allowed_roles,
    owner_scoped,
    director_override
) VALUES
{transition_rows};

COMMIT;
"""


def render_operator_commands(plan: ProjectBoardProvision) -> str:
    q_asset = shell_quote(plan.asset_dir)
    q_frame = shell_quote(plan.frame_dir)
    q_board_root = shell_quote(plan.board_root)
    q_source_repo = shell_quote(plan.source_repo)
    q_deploy_script = shell_quote(f"{plan.source_repo}/scripts/ticket-board-service.sh")
    q_board_unit = shell_quote(f"/etc/systemd/system/{plan.board_unit}")
    q_tmpfiles = shell_quote(f"/etc/tmpfiles.d/{plan.tmpfiles_name}")
    q_polkit = shell_quote(f"/etc/polkit-1/rules.d/{plan.polkit_name}")
    q_listener_unit = shell_quote(
        f"/home/{plan.owner_user}/.config/systemd/user/{plan.listener_unit}"
    )
    workflow_seed_command = ""
    if plan.workflow_seed != "pgu-full":
        workflow_seed_command = (
            f"sudo psql -X -v ON_ERROR_STOP=1 {shell_quote(plan.admin_database_url)} "
            f"-f {shell_quote(plan.project + '-workflow.sql')}\n"
        )
    install_board_root = owned_directory_command(
        plan,
        owned_ancestor_dirs(plan.owner_user, plan.board_root, include_target=True),
    )
    if not install_board_root:
        install_board_root = f"sudo install -d -m 0755 {q_board_root}"
    install_asset_frame_parent = owned_directory_command(
        plan,
        (
            *owned_ancestor_dirs(plan.owner_user, plan.asset_dir, include_target=False),
            *owned_ancestor_dirs(plan.owner_user, plan.frame_dir, include_target=False),
        ),
    )
    if plan.project == "pgu" and plan.frame_dir == "/tmp/pgu-frames":
        install_asset_frame = "\n".join(
            line
            for line in (
                install_asset_frame_parent,
                f"sudo install -d -m 0775 -o {shell_quote(plan.owner_user)} -g {shell_quote(plan.owner_user)} {q_asset}",
                f"sudo install -d -m 1777 -o root -g root {q_frame}",
            )
            if line
        )
        grant_asset_frame = f"sudo setfacl -R -m u:{plan.service_user}:rwx {q_asset}"
    else:
        install_asset_frame = "\n".join(
            line
            for line in (
                install_asset_frame_parent,
                f"sudo install -d -m 0775 -o {shell_quote(plan.owner_user)} -g {shell_quote(plan.owner_user)} {q_asset} {q_frame}",
            )
            if line
        )
        grant_asset_frame = f"sudo setfacl -R -m u:{plan.service_user}:rwx {q_asset} {q_frame}"
    install_listener_unit_parent = owned_directory_command(
        plan,
        owned_ancestor_dirs(
            plan.owner_user,
            f"/home/{plan.owner_user}/.config/systemd/user",
            include_target=True,
        ),
    )
    q_owner_user = shell_quote(plan.owner_user)
    return f"""#!/usr/bin/env bash
set -euo pipefail

# Review generated artifacts first. These commands require host privileges.
{install_board_root}
sudo env TICKET_BOARD_PROJECT={shell_quote(plan.project)} SOURCE_REPO={q_source_repo} BOARD_ROOT={q_board_root} DEPLOY_REF=origin/main TICKET_BOARD_SKIP_MIGRATIONS=1 {q_deploy_script} deploy
sudo setfacl -R -m u:{plan.service_user}:rx {q_board_root}
sudo find {q_board_root} -type d -exec setfacl -m d:u:{plan.service_user}:rx {{}} +
{install_asset_frame}
sudo setfacl -m u:{plan.service_user}:--x {shell_quote('/home/' + plan.owner_user)}
sudo setfacl -m u:{plan.service_user}:--x {shell_quote('/home/' + plan.owner_user + '/.claude')}
{grant_asset_frame}
sudo install -m 0644 {shell_quote(plan.board_unit)} {q_board_unit}
sudo install -m 0644 {shell_quote(plan.tmpfiles_name)} {q_tmpfiles}
sudo install -m 0644 {shell_quote(plan.polkit_name)} {q_polkit}
sudo systemd-tmpfiles --create {q_tmpfiles}
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -f {shell_quote(plan.project + '-database.sql')}
sudo TICKET_BOARD_ADMIN_DATABASE_URL={shell_quote(plan.admin_database_url)} {shell_quote(plan.board_current + '/scripts/ticket-board-migrate')}
{workflow_seed_command.rstrip()}
sudo psql -X -v ON_ERROR_STOP=1 {shell_quote(plan.admin_database_url)} -f {shell_quote(plan.board_current + '/scripts/ticket_board/rbac.sql')}
sudo systemctl daemon-reload
sudo systemctl enable --now {plan.board_unit}
{install_listener_unit_parent}
sudo install -m 0644 -o {q_owner_user} -g {q_owner_user} {shell_quote(plan.listener_unit)} {q_listener_unit}
sudo loginctl enable-linger {q_owner_user}
owner_uid="$(id -u {q_owner_user})"
owner_runtime_dir="/run/user/$owner_uid"
owner_bus="$owner_runtime_dir/bus"
for _ in $(seq 1 300); do
    [ -S "$owner_bus" ] && break
    sleep 0.1
done
if [ ! -S "$owner_bus" ]; then
    echo "ERROR: user bus for {plan.owner_user} did not appear at $owner_bus within 30s after enable-linger" >&2
    exit 1
fi
sudo -u {q_owner_user} env XDG_RUNTIME_DIR="$owner_runtime_dir" DBUS_SESSION_BUS_ADDRESS="unix:path=$owner_bus" systemctl --user daemon-reload
sudo -u {q_owner_user} env XDG_RUNTIME_DIR="$owner_runtime_dir" DBUS_SESSION_BUS_ADDRESS="unix:path=$owner_bus" systemctl --user enable --now {plan.listener_unit}
curl -fsS http://127.0.0.1:{plan.port}/api/board >/dev/null
"""


def write_artifacts(plan: ProjectBoardProvision, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "plan.json": json.dumps(asdict(plan), indent=2, sort_keys=True) + "\n",
        plan.board_unit: render_board_unit(plan),
        plan.listener_unit: render_listener_unit(plan),
        plan.tmpfiles_name: render_tmpfiles(plan),
        plan.polkit_name: render_polkit_rule(plan),
        f"{plan.project}-database.sql": render_database_sql(plan),
        f"{plan.project}-workflow.sql": render_workflow_sql(plan),
        "operator-commands.sh": render_operator_commands(plan),
    }
    for name, text in files.items():
        path = output_dir / name
        path.write_text(text, encoding="utf-8")
        if name.endswith(".sh"):
            path.chmod(0o755)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render per-project ticket-board provisioning artifacts.")
    parser.add_argument("--project", required=True, help="project slug, e.g. pgu or stellaris")
    parser.add_argument("--owner-user", required=True, help="Unix user that owns this project/team")
    parser.add_argument("--port", type=int, help="HTTP port; omitted means deterministic allocation")
    parser.add_argument("--database", help="PostgreSQL database name; omitted means <project>_ticket_board")
    parser.add_argument("--ticket-prefix", help="ticket id prefix; omitted means normalized project slug")
    parser.add_argument("--service-user", default="boardsvc", help="systemd User for the board service")
    parser.add_argument("--service-role", default="ticket_board_service", help="database writer role")
    parser.add_argument("--listener-role", default="ticket_board_listener", help="database listener role")
    parser.add_argument("--board-root", type=Path, help="versioned release root; default under owner home")
    parser.add_argument("--source-repo", type=Path, help="source checkout to export into the board release root")
    parser.add_argument("--asset-dir", type=Path, help="durable attachment directory")
    parser.add_argument("--frame-dir", type=Path, help="frame inbox directory")
    parser.add_argument(
        "--implementer-role",
        action="append",
        dest="implementer_roles",
        help="implementation-stage owner role; repeat for multiple implementers",
    )
    parser.add_argument("--output-dir", type=Path, help="write all artifacts to this directory")
    parser.add_argument("--json", action="store_true", help="print plan JSON")
    parser.add_argument(
        "--render",
        choices=["board-unit", "listener-unit", "tmpfiles", "polkit", "database-sql", "workflow-sql", "commands"],
        help="print one rendered artifact",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    plan = build_plan(
        project=args.project,
        owner_user=args.owner_user,
        port=args.port,
        database=args.database,
        ticket_prefix=args.ticket_prefix,
        service_user=args.service_user,
        service_role=args.service_role,
        listener_role=args.listener_role,
        board_root=args.board_root,
        source_repo=args.source_repo,
        asset_dir=args.asset_dir,
        frame_dir=args.frame_dir,
        implementer_roles=args.implementer_roles,
    )
    if args.output_dir:
        write_artifacts(plan, args.output_dir)
        print(f"wrote {args.output_dir}")
    if args.json:
        print(json.dumps(asdict(plan), indent=2, sort_keys=True))
    if args.render:
        renderers = {
            "board-unit": render_board_unit,
            "listener-unit": render_listener_unit,
            "tmpfiles": render_tmpfiles,
            "polkit": render_polkit_rule,
            "database-sql": render_database_sql,
            "workflow-sql": render_workflow_sql,
            "commands": render_operator_commands,
        }
        sys.stdout.write(renderers[args.render](plan))
    if not args.output_dir and not args.json and not args.render:
        print(json.dumps(asdict(plan), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
