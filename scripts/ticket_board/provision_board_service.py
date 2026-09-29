"""The host artifacts a tenant's ticket board service is installed with.

Its three systemd units (`render_board_unit`, `render_listener_unit`,
`render_canary_unit`, with the helpers they share: `env_list`,
`env_operation_role_map`, `listener_board_url`, `default_ticket_board_python`
and its `DEFAULT_SHARED_PYTHON`), the tmpfiles line for its frame directory
(`render_tmpfiles`), the polkit rule that lets the owner start and stop those
units and nothing else (`render_polkit_rule`), the SQL that bootstraps its
database roles (`render_database_sql`), and `tenant_primary_group`, the socket
group lookup written for the board unit.

Moved out of `scripts/ticket_board/project_provision.py` unchanged (SYRD-471).
`project_provision` imports this module and re-exports every name, so every
module and test that imports them from there, or patches them there, still
reaches the same objects. What they read of `project_provision` -- each other,
`systemd_environment`, `sql_identifier` and `role_accounts_env` -- is read
through it when they run, so a patch there still reaches them. This module
imports `project_provision` only inside the functions that need it, when they
run, with the same fallback for direct script execution.
"""

from __future__ import annotations

import grp
import os
import pwd
from pathlib import Path
from typing import Sequence


DEFAULT_SHARED_PYTHON = "/opt/switchyard/venv/bin/python"


def env_list(values: Sequence[str]) -> str:
    return ",".join(values)


def env_operation_role_map(values: Sequence[tuple[str, Sequence[str]]]) -> str:
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    return ";".join(f"{operation}={provision.env_list(roles)}" for operation, roles in values)


def default_ticket_board_python() -> str:
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    override = os.environ.get("TICKET_BOARD_PYTHON")
    if override:
        return override
    shared_python = os.environ.get("SWITCHYARD_SHARED_PYTHON", provision.DEFAULT_SHARED_PYTHON)
    if Path(shared_python).is_file() and os.access(shared_python, os.X_OK):
        return shared_python
    return "/usr/bin/python3"


def render_board_unit(plan: ProjectBoardProvision) -> str:
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    operation_allowed_roles = provision.env_operation_role_map(plan.operation_allowed_roles)
    operation_allowed_roles_line = (
        f"Environment=TICKET_BOARD_OPERATION_ALLOWED_ROLES={operation_allowed_roles}\n"
        if operation_allowed_roles
        else ""
    )
    # The service's primary group is the project account's group. That makes
    # the 0750 runtime directory and 0660 socket reachable by this project but
    # not by another project's account. Role authority is process-bound data,
    # so the unit carries no role-account table (SYRD-69).
    tenant_group_line = (
        f"Group={plan.roles_group}\nSupplementaryGroups={plan.roles_group}\n"
        if plan.roles_group else ""
    )
    socket_group_env_line = (
        f"Environment=TICKET_BOARD_SOCKET_GROUP={plan.roles_group}\n" if plan.roles_group else ""
    )
    role_accounts_line = (
        f"Environment=TICKET_BOARD_ROLE_ACCOUNTS={provision.role_accounts_env(plan)}\n"
        if plan.role_accounts
        else ""
    )
    process_authority_line = (
        "" if plan.role_accounts else "Environment=TICKET_BOARD_PROCESS_AUTHORITY=1\n"
    )
    # The commit cache is on the command line as well as in `Environment=`. The
    # `EnvironmentFile=` below is the owner's, it outlives every upgrade, and
    # systemd lets a value in it beat the unit's own `Environment=`: a stale
    # TICKET_BOARD_COMMIT_GIT_DIR there silently moved the board back onto a
    # cache nothing refreshes, and every new candidate was an unknown commit
    # (SYRD-251). The board prefers the argument to any environment value. The
    # `Environment=` line stays because it is where the publisher reads the
    # cache it refreshes.
    return f"""[Unit]
Description={plan.project} Ticket Board
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User={plan.service_user}
{tenant_group_line}WorkingDirectory={plan.board_current}
RuntimeDirectory={plan.runtime_directory}
RuntimeDirectoryMode=0750
ExecStart={provision.default_ticket_board_python()} {plan.board_current}/scripts/ticket-board.py --host 127.0.0.1 --port {plan.port} --unix-socket {plan.socket_path} --frames {plan.frame_dir} --assets {plan.asset_dir} --commit-git-dir {plan.commit_git_dir}
Restart=on-failure
RestartSec=2
EnvironmentFile=-{plan.owner_home}/.config/{plan.project}/ticket-board.env
Environment=PYTHONUNBUFFERED=1
Environment=HOME={plan.owner_home}
Environment=TICKET_BOARD_DIRECTORCTL={plan.board_current}/scripts/directorctl
Environment=TICKET_BOARD_PROJECT={plan.project}
{process_authority_line.rstrip()}
{provision.systemd_environment("TICKET_BOARD_PROJECT_NAME", plan.project_name)}
Environment=TICKET_BOARD_TICKET_PREFIX={plan.ticket_prefix}
Environment=TICKET_BOARD_COMMIT_GIT_DIR={plan.commit_git_dir}
Environment=PGHOST=/var/run/postgresql
Environment=PGDATABASE={plan.database}
Environment=PGUSER={plan.service_role}
Environment=TICKET_BOARD_SOCKET={plan.socket_path}
Environment=TICKET_BOARD_TENANT_USER={plan.owner_user}
{socket_group_env_line}{role_accounts_line}Environment=TICKET_BOARD_DATABASE_URL={plan.board_database_url}
Environment=TICKET_BOARD_DRAFT_ROLES={provision.env_list(plan.draft_roles)}
Environment=TICKET_BOARD_IMPLEMENTER_ROLES={provision.env_list(plan.implementer_roles)}
Environment=TICKET_BOARD_ASSIGNEES={provision.env_list(plan.assignee_roles)}
Environment=TICKET_BOARD_CALLER_ROLES={provision.env_list(plan.caller_roles)}
{operation_allowed_roles_line}NoNewPrivileges=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths={plan.asset_dir} {plan.frame_dir}
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""


def tenant_primary_group(owner_user: str) -> str:
    """The group the board socket is shared with, or '' when it is unknown.

    Returning empty rather than guessing keeps a provisioning run on a host
    where the account does not exist yet from baking a wrong group into the
    unit; the server logs loudly when the group is unset (SYRD-39).
    """
    name = (owner_user or "").strip()
    if not name:
        return ""
    try:
        gid = pwd.getpwnam(name).pw_gid
    except KeyError:
        return ""
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return ""


def listener_board_url(plan: ProjectBoardProvision) -> str:
    """The board this tenant's listener and its directorctl ask for runtime assignments.

    The address the board unit itself binds (`--host 127.0.0.1 --port`). Left
    unset, directorctl fell back to 8770 -- another tenant's board, or none --
    so every notice on a non-default-port tenant failed to resolve its role's
    runtime and was retried forever while the listener claimed it from the
    right database (SYRD-265).
    """
    return f"http://127.0.0.1:{plan.port}"


def render_listener_unit(plan: ProjectBoardProvision) -> str:
    """The listener's unit, including where it reads pane hook state.

    Where the hooks write depends on the tenant's identity model, so where the
    listener reads has to depend on it the same way. team_launcher's
    role_pane_state_dir is the runtime authority for that and this mirrors it:

    - roles running as their own accounts write the shared board runtime
      directory, because a role account cannot write the owner's XDG runtime
      directory and the listener cannot read each role's own (SYRD-39);
    - one project account for every role -- process authority, SYRD-69 -- means
      the hooks write the owner's XDG runtime directory, which is what this
      provisioning hands the hook installer, and %t in a user unit is exactly
      that directory. Both halves then resolve to one path without naming a
      uid, a home, or anything else this tenant happens to have.

    This line used to be the shared path unconditionally. On a single-account
    tenant that made it the board service's RuntimeDirectory: a directory the
    listener could read, no hook ever wrote to, and systemd erases and
    recreates whenever the board service restarts. The listener then saw no
    hook state for any pane, called every idle role busy, and deferred every
    notification while looking healthy (SYRD-95).
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    pane_state_dir = (
        f"/run/{plan.runtime_directory}/pane-state"
        if plan.role_accounts
        else f"%t/{plan.runtime_directory}/pane-state"
    )
    role_accounts_line = (
        f"Environment=TICKET_BOARD_ROLE_ACCOUNTS={provision.role_accounts_env(plan)}\n"
        if plan.role_accounts
        else ""
    )
    process_authority_line = (
        "" if plan.role_accounts else "Environment=TICKET_BOARD_PROCESS_AUTHORITY=1\n"
    )
    return f"""[Unit]
Description={plan.project} Ticket Board Notify Listener
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
WorkingDirectory={plan.board_current}
ExecStart={provision.default_ticket_board_python()} {plan.board_current}/scripts/ticket-board-notify-listener --directorctl {plan.board_current}/scripts/directorctl
Restart=always
RestartSec=2
Environment=PYTHONUNBUFFERED=1
Environment=TICKET_BOARD_PROJECT={plan.project}
Environment=TICKET_BOARD_URL={provision.listener_board_url(plan)}
{process_authority_line.rstrip()}
{role_accounts_line.rstrip()}
Environment=PGHOST=/var/run/postgresql
Environment=PGDATABASE={plan.database}
Environment=PGUSER={plan.listener_role}
Environment=TICKET_BOARD_DATABASE_URL={plan.listener_database_url}
Environment=TICKET_BOARD_NOTIFY_DATABASE_URL={plan.listener_database_url}
Environment=TICKET_BOARD_PANE_STATE_DIR={pane_state_dir}
EnvironmentFile=-%h/.config/{plan.project}/ticket-board-notify-listener.env
StandardOutput=append:{plan.listener_log}
StandardError=append:{plan.listener_log}

[Install]
WantedBy=default.target
"""


def render_canary_unit(plan: ProjectBoardProvision) -> str:
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    return f"""[Unit]
Description={plan.project} Ticket Board Deploy Canary
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User={plan.service_user}
WorkingDirectory={plan.board_root}
EnvironmentFile={plan.board_root}/canary.env
RuntimeDirectory={plan.runtime_directory}-canary
ExecStart=/bin/sh -eu -c 'PYTHONUNBUFFERED=1 TICKET_BOARD_PROJECT="${{TICKET_BOARD_PROJECT:-{plan.project}}}" TICKET_BOARD_SOCKET="$BOARD_CANARY_SOCKET" TICKET_BOARD_DATABASE_URL="$BOARD_CANARY_DATABASE_URL" exec {provision.default_ticket_board_python()} "$BOARD_CANARY_RELEASE_DIR"/scripts/ticket-board.py --host "$BOARD_CANARY_HOST" --port "$BOARD_CANARY_PORT" --unix-socket "$BOARD_CANARY_SOCKET" --frames "$BOARD_CANARY_FRAME_DIR" --assets "$BOARD_CANARY_ASSET_DIR" >>"$BOARD_CANARY_LOG" 2>&1'
Restart=no
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=/tmp
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
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    db_ident = provision.sql_identifier(plan.database)
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
