"""The operator packet: the root-run script that provisions one project, and the few commands only it writes.

`render_operator_commands` renders the whole packet a root operator reviews and
runs for a plan -- accounts, directories, the database and its peer
authentication, units, grants, the repository boundary phase and the board's
first start. `PACKET_PROVISION_DIR` and `packet_companion` address a file that
ships beside the packet from the packet's own directory (SYRD-149);
`postgres_sql_file_command`, `service_user_command`, `peer_auth_command` and
`owned_directory_command` are commands the packet alone writes.

Moved out of `scripts/ticket_board/project_provision.py` unchanged (SYRD-474).
`project_provision` imports this module and re-exports every name, so every
module and test that imports them from there, or patches them there, still
reaches the same objects -- including `write_artifacts` and `main`, which
render the packet by the re-exported name. What they read of
`project_provision` -- each other, the plan-path helpers and every earlier
slice's commands -- is read through it when they run, so a patch there still
reaches them. This module imports `project_provision` only inside the
functions that need it, when they run, with the same fallback for direct
script execution.
"""

from __future__ import annotations

import os
from typing import Sequence


#: How the operator packet names a file that ships beside it.
#:
#: The packet is installed root-owned and run by absolute path -- from a
#: journal, from a Polkit transaction, from whatever directory the operator
#: happened to be in. A bare file name is resolved against that directory, so
#: `install -m 0644 testing-ticket-board.conf ...` looked up a root-owned
#: artifact in the caller's cwd and failed there, which is what journal attempt
#: 0007 recorded. `$provision_dir` is the packet's own directory, computed from
#: `BASH_SOURCE` at the top, so a companion is addressed from the packet rather
#: than from whoever started it (SYRD-149).
PACKET_PROVISION_DIR = '"$provision_dir"'


def packet_companion(name: str) -> str:
    """One artifact that ships beside the packet, addressed from the packet."""
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    if name.startswith("/"):
        raise SystemExit(f"a packet companion is a file name beside the packet, not a path: {name}")
    return f"{provision.PACKET_PROVISION_DIR}/{provision.shell_quote(name)}"


def postgres_sql_file_command(sql_file: str, *, database_url: str = "", companion: bool = False) -> str:
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    command = "sudo cat " + (provision.packet_companion(sql_file) if companion else provision.shell_quote(sql_file))
    command += " | sudo -u postgres psql -X -v ON_ERROR_STOP=1"
    if database_url:
        command += " " + provision.shell_quote(database_url)
    command += " -f -"
    return command


def service_user_command(service_user: str) -> str:
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    q_service_user = provision.shell_quote(service_user)
    return f"""if ! getent passwd {q_service_user} >/dev/null 2>&1; then
    service_shell="$(command -v nologin 2>/dev/null || true)"
    [ -n "$service_shell" ] || service_shell="/usr/sbin/nologin"
    [ -x "$service_shell" ] || service_shell="/bin/false"
    if [ ! -x "$service_shell" ]; then
        echo 'ERROR: neither nologin nor /bin/false is executable; install util-linux or provide a non-login shell before provisioning' >&2
        exit 1
    fi
    sudo useradd -r -M -d /nonexistent -s "$service_shell" {q_service_user}
fi"""


def peer_auth_command(plan: ProjectBoardProvision) -> str:
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    q_setup_script = provision.shell_quote(f"{plan.source_repo}/scripts/ticket-board-boardsvc-setup.sh")
    return (
        "sudo env "
        f"PG_DATABASE={provision.shell_quote(plan.database)} "
        f"PG_IDENT_MAP={provision.shell_quote(provision.DEFAULT_PG_IDENT_MAP)} "
        f"SERVICE_USER={provision.shell_quote(plan.service_user)} "
        f"SERVICE_ROLE={provision.shell_quote(plan.service_role)} "
        f"{q_setup_script} --apply-peer-auth"
    )


def owned_directory_command(plan: ProjectBoardProvision, dirs: Sequence[str], *, mode: str = "0755") -> str:
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    unique_dirs = provision._dedupe(tuple(dirs))
    if not unique_dirs:
        return ""
    quoted_dirs = " ".join(provision.shell_quote(value) for value in unique_dirs)
    return (
        f"sudo install -d -m {mode} -o {provision.shell_quote(plan.owner_user)} "
        f"-g {provision.shell_quote(plan.owner_user)} {quoted_dirs}"
    )


def render_operator_commands(plan: ProjectBoardProvision, *, enable_owner_linger: bool = True) -> str:
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    q_asset = provision.shell_quote(plan.asset_dir)
    q_frame = provision.shell_quote(plan.frame_dir)
    q_board_root = provision.shell_quote(plan.board_root)
    q_source_repo = provision.shell_quote(plan.source_repo)
    q_commit_git_dir = provision.shell_quote(plan.commit_git_dir)
    q_deploy_script = provision.shell_quote(f"{plan.source_repo}/scripts/ticket-board-service.sh")
    q_board_unit = provision.shell_quote(f"/etc/systemd/system/{plan.board_unit}")
    # Published from the artifacts the operator is reviewing, so the deployer
    # compares the release's unit against the loaded one rather than being sent
    # into a directory it cannot read (SYRD-127).
    system_unit_proof = "\n".join(
        provision.system_unit_proof_commands(plan.project, '"$system_unit_candidate"')
    )
    q_readable_system_unit = provision.shell_quote(provision.readable_system_unit_path(plan.project))
    q_canary_unit = provision.shell_quote(f"/etc/systemd/system/{plan.canary_unit}")
    q_tmpfiles = provision.shell_quote(f"/etc/tmpfiles.d/{plan.tmpfiles_name}")
    q_polkit = provision.shell_quote(f"/etc/polkit-1/rules.d/{plan.polkit_name}")
    q_role_control_sudoers = provision.shell_quote(f"/etc/sudoers.d/{plan.role_control_sudoers_name}")
    github_identity = "\n".join(
        provision.owner_github_identity_commands(
            plan.owner_user,
            plan.owner_home,
            key_name=plan.owner_github_key_name,
            host_alias=plan.owner_github_host_alias,
            comment=f"{plan.owner_user} switchyard {plan.project}",
        )
    )
    if provision.role_control_sudoers(plan).strip():
        # visudo -c first: a malformed sudoers file can lock the host out of
        # sudo entirely, so it is validated before it is installed.
        install_role_control_sudoers = (
            f"sudo install -m 0440 -o root -g root {provision.packet_companion(plan.role_control_sudoers_name)} "
            f"{q_role_control_sudoers}.staged\n"
            f"sudo visudo -c -f {q_role_control_sudoers}.staged\n"
            f"sudo mv {q_role_control_sudoers}.staged {q_role_control_sudoers}"
        )
    else:
        install_role_control_sudoers = (
            "# no role control interface: this project declares no director role"
        )
    q_tenant_control_sudoers = provision.shell_quote(f"/etc/sudoers.d/{plan.tenant_control_sudoers_name}")
    if plan.control_user:
        # After the role tooling staging, because the rule names the staged
        # helper and should not be live before it exists. Same
        # visudo-before-install order, and the grant lands first so the rule is
        # never live for a moment without the data that constrains it.
        install_tenant_control = "\n".join(
            [
                f"sudo install -d -m 0755 -o root -g root {provision.shell_quote(provision.TENANT_CONTROL_ROOT + '/' + plan.project)}",
                f"sudo install -m 0644 -o root -g root {provision.packet_companion(provision.tenant_control_grant_name(plan.project))} "
                f"{provision.shell_quote(provision.tenant_control_grant_path(plan.project))}",
                f"sudo install -m 0440 -o root -g root {provision.packet_companion(plan.tenant_control_sudoers_name)} "
                f"{q_tenant_control_sudoers}.staged",
                f"sudo visudo -c -f {q_tenant_control_sudoers}.staged",
                f"sudo mv {q_tenant_control_sudoers}.staged {q_tenant_control_sudoers}",
            ]
        )
    else:
        install_tenant_control = (
            "# no lifecycle control bridge: this project records no human controller"
        )
    q_listener_unit = provision.shell_quote(
        f"{plan.owner_home}/.config/systemd/user/{plan.listener_unit}"
    )
    q_owner_home = provision.shell_quote(plan.owner_home)
    q_hook_installer = provision.shell_quote(f"{plan.board_current}/scripts/ticket-board-install-pane-hooks")
    q_hook_source = provision.shell_quote(f"{plan.board_current}/scripts/ticket-board-pane-idle-hook")
    q_hook_bin = provision.shell_quote(f"{plan.owner_home}/.local/bin/ticket-board-pane-idle-hook")
    q_board_skill_installer = provision.shell_quote(f"{plan.board_current}/scripts/switchyard-board-skill")
    # Roles get the same preparation as the owner, from the deployed release,
    # once the board exists (SYRD-39).
    role_runtime_step = provision.role_runtime_command(plan) or (
        "# no per-role runtime preparation: this project declares no role accounts"
    )
    q_pane_session_dir = provision.shell_quote(
        f"{plan.owner_home}/.local/state/{plan.runtime_directory}/pane-sessions"
    )
    workflow_seed_command = ""
    if plan.workflow_seed != "pgu-full":
        workflow_seed_command = provision.postgres_sql_file_command(
            plan.project + "-workflow.sql",
            database_url=plan.admin_database_url,
            companion=True,
        )
        workflow_seed_command += "\n"
    install_board_root = provision.owned_directory_command(
        plan,
        provision.owned_ancestor_dirs(plan.owner_home, plan.board_root, include_target=True),
    )
    if not install_board_root:
        install_board_root = f"sudo install -d -m 0755 {q_board_root}"
    install_asset_frame_parent = provision.owned_directory_command(
        plan,
        (
            *provision.owned_ancestor_dirs(plan.owner_home, plan.asset_dir, include_target=False),
            *provision.owned_ancestor_dirs(plan.owner_home, plan.frame_dir, include_target=False),
        ),
    )
    if plan.project == "pgu" and plan.frame_dir == "/tmp/pgu-frames":
        install_asset_frame = "\n".join(
            line
            for line in (
                install_asset_frame_parent,
                f"sudo install -d -m 0775 -o {provision.shell_quote(plan.owner_user)} -g {provision.shell_quote(plan.owner_user)} {q_asset}",
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
                f"sudo install -d -m 0775 -o {provision.shell_quote(plan.owner_user)} -g {provision.shell_quote(plan.owner_user)} {q_asset} {q_frame}",
            )
            if line
        )
        grant_asset_frame = f"sudo setfacl -R -m u:{plan.service_user}:rwx {q_asset} {q_frame}"
    install_listener_unit_parent = provision.owned_directory_command(
        plan,
        provision.owned_ancestor_dirs(
            plan.owner_home,
            f"{plan.owner_home}/.config/systemd/user",
            include_target=True,
        ),
    )
    q_owner_user = provision.shell_quote(plan.owner_user)
    q_board_env_file = provision.shell_quote(f"{plan.owner_home}/.config/{plan.project}/ticket-board.env")
    q_owner_config_dir = provision.shell_quote(f"{plan.owner_home}/.config")
    q_board_env_dir = provision.shell_quote(f"{plan.owner_home}/.config/{plan.project}")
    install_board_env_parent = "\n".join(
        [
            f"sudo install -d -m 0755 -o {q_owner_user} -g {q_owner_user} {q_owner_config_dir}",
            f"sudo install -d -m 0700 -o {q_owner_user} -g {q_owner_user} {q_board_env_dir}",
        ]
    )
    if plan.board_service_traversal:
        grant_board_root = "\n".join(
            [
                f"sudo setfacl -R -m u:{plan.service_user}:rx {q_board_root}",
                f"sudo find {q_board_root} -type d -exec setfacl -m d:u:{plan.service_user}:rx {{}} +",
            ]
        )
        grant_home_traversal = "\n".join(
            [
                f"sudo setfacl -m u:{plan.service_user}:--x {provision.shell_quote(plan.owner_home)}",
                f"sudo setfacl -m u:{plan.service_user}:--x {provision.shell_quote(plan.owner_home + '/.claude')}",
            ]
        )
        effective_grant_asset_frame = grant_asset_frame
    else:
        grant_board_root = (
            f"# board_service_traversal=false: not granting {plan.service_user} ACLs on "
            "the owner home, board release, assets, or frames."
        )
        grant_home_traversal = (
            f"# {plan.service_user} must already be able to traverse/read/write configured board paths, "
            "or the board health check will fail."
        )
        effective_grant_asset_frame = ""
    # Before the traversal grant rather than after it: the tenant tree is closed
    # first, and only then is anything allowed to walk through the home. On a
    # tenant already provisioned this is the repair, and re-running it changes
    # nothing (SYRD-156).
    confine_source_tree = (
        "\n".join(
            provision.tenant_source_confinement_commands(
                owner_user=plan.owner_user,
                owner_home=plan.owner_home,
                checkout=plan.project_repository,
            )
        )
        if plan.project_repository
        else ""
    ) or (
        f"# the checkout {plan.project_repository} is outside {plan.owner_home}; "
        "it is not this tenant's tree to confine"
        if plan.project_repository
        else (
            "# this plan does not record where this tenant's checkout is, so there is nothing "
            "to confine here; `switchyard upgrade` and `switchyard resume-provision` record it "
            "from the generated configuration's own location"
        )
    )
    # The commit store the board will resolve against, closed and then granted
    # to the service by name, read-only. Closed first, because `chmod`
    # recomputes the ACL mask and would clip a grant made before it. `install
    # -d` also guarantees the directory exists, so the grant cannot fail on a
    # fresh tenant whose bare repository has not been cloned yet -- git clones
    # into an existing empty directory quite happily (SYRD-157).
    commit_stores = [
        entry for entry in str(plan.commit_git_dir).split(os.pathsep) if entry.strip()
    ]
    repository_boundary_lines: list[str] = list(
        provision.repository_copy_confinement_commands(
            owner_user=plan.owner_user,
            owner_home=plan.owner_home,
            repositories=commit_stores,
            mode=provision.WRITABLE_REPOSITORY_COPY_MODE,
        )
    )
    if plan.board_service_traversal:
        for store in commit_stores:
            repository_boundary_lines.extend(
                provision.commit_store_read_commands(
                    owner_home=plan.owner_home,
                    service_user=plan.service_user,
                    commit_git_dir=store,
                )
            )
    # The worktree base, which SYRD-156 closed one directory over and this did
    # not: closed first, then the repository group granted for a tenant whose
    # roles have their own accounts, and only then the socket group retired --
    # so nothing loses access in the gap between taking one grant away and
    # making the one that replaces it (SYRD-171).
    worktree_base = provision.tenant_worktree_base(plan)
    control_repository = provision.tenant_control_repository(plan)
    repository_boundary_lines.extend(
        provision.tenant_worktree_confinement_commands(
            owner_user=plan.owner_user,
            owner_home=plan.owner_home,
            worktree_base=worktree_base,
            worktrees=[path for _role, path in plan.role_worktrees],
        )
    )
    if plan.role_accounts:
        repository_boundary_lines.extend(
            provision.repository_group_commands(
                plan.project,
                [plan.owner_user, *(account for _role, account in plan.role_accounts)],
            )
        )
        repository_boundary_lines.extend(
            provision.role_worktree_access_commands(
                owner_home=plan.owner_home,
                repository_group=provision.repository_group_name(plan.project),
                worktree_base=worktree_base,
                control_repository=control_repository,
            )
        )
    repository_boundary_lines.extend(
        provision.socket_group_retirement_commands(
            owner_user=plan.owner_user,
            owner_home=plan.owner_home,
            socket_group=provision.roles_group_name(plan.project),
            worktree_base=worktree_base,
            control_repository=control_repository,
        )
    )
    repository_boundary = "\n".join(
        [
            provision.REPOSITORY_BOUNDARY_BEGIN,
            *(
                repository_boundary_lines
                or [
                    f"# the commit store {plan.commit_git_dir} is outside {plan.owner_home}; "
                    "this tenant grants the board service nothing there"
                ]
            ),
            provision.REPOSITORY_BOUNDARY_END,
        ]
    )
    if enable_owner_linger:
        owner_linger_step = f"sudo loginctl enable-linger {q_owner_user}"
        owner_bus_error = (
            f"ERROR: user bus for {plan.owner_user} did not appear at $owner_bus within 30s after enable-linger"
        )
    else:
        owner_linger_step = (
            f"sudo loginctl show-user {q_owner_user} -p Linger --value 2>/dev/null | grep -qx yes || "
            f"{{ echo \"ERROR: linger is not enabled for {plan.owner_user}; switchyard refuses to modify an existing owner user\" >&2; exit 1; }}"
        )
        owner_bus_error = (
            f"ERROR: user bus for {plan.owner_user} did not appear at $owner_bus; enable linger or start that user's systemd manager"
        )
    return f"""#!/usr/bin/env bash
set -euo pipefail

# Review generated artifacts first. These commands require host privileges.
provision_dir="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
system_unit_candidate="$provision_dir/{plan.board_unit}"
canary_unit_candidate="$provision_dir/{plan.canary_unit}"
# The deployer runs as the project account and has to read the reviewed unit to
# compare it against the one systemd loaded. Root publishes a copy it can reach,
# rather than the account being sent into a directory root owns (SYRD-127).
{system_unit_proof}
readable_system_unit={q_readable_system_unit}
{provision.service_user_command(plan.service_user)}
{provision.role_accounts_command(plan)}
{provision.peer_auth_command(plan)}
{install_board_root}
{grant_board_root}
{install_asset_frame}
{confine_source_tree}
{repository_boundary}
{grant_home_traversal}
{effective_grant_asset_frame}
# The deploy exports an immutable release into the board root and then starts a
# canary AS THE SERVICE ACCOUNT, so every grant that account needs is made
# above it rather than below. It used to run first: on a host where the tree
# already carried the grants the deploy succeeded and the grants below it were
# a no-op, and on a fresh one the export landed at 0750 with no named entry and
# no default to inherit, the canary could not traverse its own release, and the
# packet died before reaching the line that would have fixed it. The default
# ACL set above is what carries the grant into releases that do not exist yet
# (SYRD-145).
sudo -u {q_owner_user} -H env HOME={q_owner_home} TICKET_BOARD_OWNER_HOME={q_owner_home} TICKET_BOARD_PROJECT={provision.shell_quote(plan.project)} TICKET_BOARD_COMMIT_GIT_DIR={q_commit_git_dir} TICKET_BOARD_PROVISIONED_SYSTEM_UNIT="$readable_system_unit" SOURCE_REPO={q_source_repo} BOARD_ROOT={q_board_root} DEPLOY_REF=origin/main TICKET_BOARD_SKIP_MIGRATIONS=1 {q_deploy_script} deploy
{install_board_env_parent}
if ! sudo -u {q_owner_user} test -s {q_board_env_file}; then
    report_token="$(/usr/bin/python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    printf 'TICKET_BOARD_TENANT_REPORT_TOKEN=%s\\n' "$report_token" | sudo -u {q_owner_user} tee {q_board_env_file} >/dev/null
    sudo -u {q_owner_user} chmod 0600 {q_board_env_file}
fi
sudo install -m 0644 "$system_unit_candidate" {q_board_unit}
sudo install -m 0644 "$canary_unit_candidate" {q_canary_unit}
sudo install -m 0644 {provision.packet_companion(plan.tmpfiles_name)} {q_tmpfiles}
sudo install -m 0644 {provision.packet_companion(plan.polkit_name)} {q_polkit}
{install_role_control_sudoers}
# The owner's GitHub identity, and the configuration that selects it. Without
# the selection git offers no key at all and every push fails as though there
# were no credential (SYRD-74). Re-runnable: an existing key is left alone.
# This is the whole credential story for a new project: the account every role
# runs as holds it, implementers publish candidates by pushing, and nothing on
# this host runs as root to do it (SYRD-123).
{github_identity}
sudo systemd-tmpfiles --create {q_tmpfiles}
{provision.postgres_sql_file_command(plan.project + '-database.sql', companion=True)}
{provision.postgres_sql_file_command(plan.board_current + '/scripts/ticket_board/schema.sql', database_url=plan.admin_database_url)}
sudo env TICKET_BOARD_ADMIN_DATABASE_URL={provision.shell_quote(plan.admin_database_url)} {provision.shell_quote(plan.board_current + '/scripts/ticket-board-migrate')}
{workflow_seed_command.rstrip()}
{provision.postgres_sql_file_command(plan.board_current + '/scripts/ticket_board/rbac.sql', database_url=plan.admin_database_url)}
sudo systemctl daemon-reload
sudo systemctl enable --now {plan.board_unit}
{install_listener_unit_parent}
sudo install -m 0644 -o {q_owner_user} -g {q_owner_user} {provision.packet_companion(plan.listener_unit)} {q_listener_unit}
{owner_linger_step}
owner_uid="$(id -u {q_owner_user})"
owner_runtime_dir="/run/user/$owner_uid"
owner_bus="$owner_runtime_dir/bus"
for _ in $(seq 1 300); do
    [ -S "$owner_bus" ] && break
    sleep 0.1
done
if [ ! -S "$owner_bus" ]; then
    echo {provision.shell_quote(owner_bus_error)} >&2
    exit 1
fi
sudo -u {q_owner_user} env XDG_RUNTIME_DIR="$owner_runtime_dir" DBUS_SESSION_BUS_ADDRESS="unix:path=$owner_bus" systemctl --user daemon-reload
sudo -u {q_owner_user} -H env XDG_RUNTIME_DIR="$owner_runtime_dir" TICKET_BOARD_PROJECT={provision.shell_quote(plan.project)} TICKET_BOARD_PANE_STATE_DIR="$owner_runtime_dir/{plan.runtime_directory}/pane-state" TICKET_BOARD_PANE_SESSION_DIR={q_pane_session_dir} {q_hook_installer} install --home {q_owner_home} --hook-source {q_hook_source} --bin-path {q_hook_bin} --seed-codex-hook-trust-if-new
sudo -u {q_owner_user} -H {q_board_skill_installer} install --home {q_owner_home}
{role_runtime_step}
{install_tenant_control}
sudo -u {q_owner_user} env XDG_RUNTIME_DIR="$owner_runtime_dir" DBUS_SESSION_BUS_ADDRESS="unix:path=$owner_bus" systemctl --user enable --now {plan.listener_unit}
curl -fsS http://127.0.0.1:{plan.port}/api/board >/dev/null
"""
