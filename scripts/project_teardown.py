"""Tearing a project down: what it would remove, who may be removed, and the command that does it.

- `_teardown_project_context` resolves the project, its plan and its owner from
  the registry and the generated configuration; `_port_from_board_url`,
  `_ticket_board_existing_ticket_count`, `_bash_action` and
  `_drop_database_command` are its board and database pieces.
- `TEARDOWN_PROTECTED_USERS`, `TEARDOWN_MINIMUM_OWNER_UID` and
  `owner_removal_refusal` decide whether an owner account may be removed at
  all; `_owner_removal_actions` and `owner_removal_residue` remove it and read
  back what is left.
- `SwitchyardTeardownAction` and `SwitchyardTeardownPlan` are the rendered plan;
  `_switchyard_teardown_actions`, `_print_teardown_plan`,
  `_confirm_teardown_project` and `_run_teardown_actions` build, show, confirm
  and run it, and `switchyard_teardown_command` is the command.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-367). The launcher
imports this module at its top and re-exports every name, so `main` and
`switchyard_main` dispatch to the same command and every suite that reaches
these through the launcher reaches the same objects. Every launcher facility
these use, every name defined here that another definition here reads, and the
action and plan classes they build are read from `team_launcher` when they
run, as they were. `owner_removal_refusal`'s `uid_lookup` default is bound when
this module is loaded, as it was when the launcher was, to `uid_for_user` from
the `host_accounts` leaf -- the launcher's very object. The standard-library
names are this module's own imports, the same objects. This module never
imports `team_launcher` at its top.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from scripts.host_accounts import uid_for_user

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectBoardProvision
    from scripts.teardown_plan import TeardownPlanFields


def _port_from_board_url(board_url: str) -> int | None:
    match = re.fullmatch(r"https?://(?:127\.0\.0\.1|localhost):([0-9]{1,5})(?:/.*)?", board_url.strip())
    if not match:
        return None
    port = int(match.group(1))
    return port if 1 <= port <= 65535 else None


def _teardown_project_context(
    project: str,
    *,
    owner_user: str | None,
    config_dir: Path | None,
    registry_dir: Path | None,
    home_base: Path,
) -> tuple["ProjectBoardProvision | TeardownPlanFields", Path, Path, bool]:  # SYRD-543
    from scripts import team_launcher as launcher

    project_slug = launcher._validate_project_slug(project)
    registry_path = (registry_dir or launcher.switchyard_registry_dir()) / f"{project_slug}.json"
    entry, broken_entries = launcher._usable_switchyard_entry_for_project(
        project_slug,
        config_dir=config_dir,
        registry_dir=registry_dir,
    )
    if entry is None:
        if broken_entries:
            detail = "\n  - ".join(broken_entries)
            raise SystemExit(
                f"switchyard: refusing to infer teardown artifacts for registered project {project_slug!r} "
                f"because its launch entry cannot be loaded:\n  - {detail}\n"
                "switchyard: rerun teardown with permissions that can read the project config, or repair the registry entry first"
            )
        resolved_owner = owner_user or launcher._default_new_project_owner(project_slug)
        owner_home = home_base / resolved_owner
        plan = launcher.build_plan(
            project=project_slug,
            project_name=project_slug,
            owner_user=resolved_owner,
            board_root=owner_home / f"{project_slug}-ticketboard-live",
            commit_git_dir=launcher.commit_git_dir_env_for_project(project=project_slug, owner_home=owner_home),
            asset_dir=owner_home / ".claude" / f"{project_slug}-tickets-assets",
            frame_dir=owner_home / ".claude" / f"{project_slug}-ticket-frames",
        )
        return plan, owner_home / "Projects" / project_slug, registry_path, False

    config = launcher.load_project_config(entry.slug, entry.config_path)
    project_checkout = launcher._project_dir_from_generated_config_path(entry.config_path) or config.repository
    if project_checkout is None:
        project_checkout = home_base / (config.run_as_user or owner_user or launcher._default_new_project_owner(entry.slug)) / "Projects" / entry.slug
    plan_path = entry.config_path.parent / "plan.json"
    if plan_path.exists():
        plan = launcher._teardown_plan_from_json(plan_path, project=entry.slug, home_base=home_base)
    else:
        resolved_owner = owner_user or config.run_as_user or launcher._default_new_project_owner(entry.slug)
        plan = launcher.build_plan(
            project=entry.slug,
            project_name=config.project_name,
            owner_user=resolved_owner,
            port=launcher._port_from_board_url(config.board_url),
            source_repo=launcher._repo_root(),
            ticket_prefix=config.ticket_prefix,
        )
    return plan, project_checkout, registry_path, True


def _ticket_board_existing_ticket_count(database: str, *, runner: Callable[..., subprocess.CompletedProcess[Any]]) -> int | None:
    escaped_database = database.replace("'", "''")
    command = [
        "psql",
        "-XAt",
        "postgresql:///postgres?host=/var/run/postgresql",
        "-c",
        f"SELECT 1 FROM pg_database WHERE datname = '{escaped_database}'",
    ]
    if os.geteuid() == 0:
        command = ["sudo", "-u", "postgres", *command]
    try:
        result = runner(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    if str(getattr(result, "stdout", "") or "").strip() != "1":
        return 0

    count_sql = (
        "SELECT CASE WHEN to_regclass('ticket_board.tickets') IS NULL "
        "THEN 0 ELSE (SELECT count(*)::int FROM ticket_board.tickets) END"
    )
    command = [
        "psql",
        "-XAt",
        f"postgresql:///{database}?host=/var/run/postgresql",
        "-c",
        count_sql,
    ]
    if os.geteuid() == 0:
        command = ["sudo", "-u", "postgres", *command]
    try:
        result = runner(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    raw_count = str(getattr(result, "stdout", "") or "").strip()
    try:
        return int(raw_count or "0")
    except ValueError:
        return None


def _bash_action(script: str) -> tuple[str, ...]:
    return ("bash", "-lc", script)


def _drop_database_command(database: str) -> tuple[str, ...]:
    from scripts import team_launcher as launcher

    return (
        "sudo",
        "-u",
        "postgres",
        "psql",
        "-X",
        "-v",
        "ON_ERROR_STOP=1",
        "postgresql:///postgres?host=/var/run/postgresql",
        "-c",
        f"DROP DATABASE IF EXISTS {launcher.sql_identifier(database)} WITH (FORCE);",
    )


#: Accounts this must never terminate, whatever a plan says. A teardown acts on
#: ONE account -- the owner its verified plan names -- and the damage from
#: getting that wrong is not a failed teardown but a desktop logged out or
#: another tenant killed mid-run (SYRD-209).
TEARDOWN_PROTECTED_USERS = frozenset({"root", "nobody"})


#: Below this, an account belongs to the distribution rather than to a tenant.
#: `useradd` gives ordinary accounts uids from 1000 up, and a teardown that
#: would `userdel` anything under that is aimed at the wrong machine.
TEARDOWN_MINIMUM_OWNER_UID = 1000


def owner_removal_refusal(
    owner_user: str,
    *,
    caller: str = "",
    uid_lookup: Callable[[str], int | None] = uid_for_user,
    desktop_approval: Callable[[], Mapping[str, str]] | None = None,
) -> str:
    """Why this account must not be terminated and removed, or "".

    Asked before anything runs, so a refusal costs nothing and a dry-run shows
    it. The account is already the one the verified plan names; these are the
    checks that catch a plan aimed at the wrong account in the first place --
    the caller's own login, the desktop operator whose session would go with it,
    a system account, or root.
    """
    from scripts import team_launcher as launcher

    owner = (owner_user or "").strip()
    if not owner:
        return "the teardown plan names no owner account, so there is nothing to remove"
    if owner in launcher.TEARDOWN_PROTECTED_USERS:
        return f"{owner} is a system account and is never removed by a tenant teardown"
    invoking = (caller or launcher.current_user_name() or "").strip()
    # SUDO_USER, because this runs as root through sudo and `current_user_name()`
    # is then root: the person to protect is the one who typed the command.
    behind_sudo = str(os.environ.get("SUDO_USER", "") or "").strip()
    for name, what in ((invoking, "the account running this teardown"),
                       (behind_sudo, "the account that invoked this teardown")):
        if name and owner == name:
            return (
                f"{owner} is {what}; removing it would end the session running the removal"
            )
    # Resolved at call time rather than as a default: this function is defined
    # above the approval reader, and binding it here would be an import-order
    # accident waiting to become a NameError.
    read_approval = desktop_approval or launcher.read_host_desktop_approval
    try:
        approval = dict(read_approval() or {})
    except Exception:  # noqa: BLE001 - an unreadable approval record protects nothing
        approval = {}
    operator = str(approval.get("gui_user", "") or "").strip()
    if operator and owner == operator:
        return (
            f"{owner} is this host's approved desktop operator; a tenant teardown does not "
            "remove the person running the desktop"
        )
    uid = uid_lookup(owner)
    if uid is not None and uid < launcher.TEARDOWN_MINIMUM_OWNER_UID:
        return (
            f"{owner} has uid {uid}, below the {launcher.TEARDOWN_MINIMUM_OWNER_UID} an ordinary tenant "
            "account is given; this plan is aimed at a system account"
        )
    return ""


def _owner_removal_actions(owner_user: str) -> list["SwitchyardTeardownAction"]:
    """Disable linger, end the owner's session, then remove the account.

    In that order, and all of it, because `userdel` alone does not do this job.
    A tenant owner with linger enabled keeps a systemd --user manager running
    with sd-pam, PipeWire, WirePlumber and a session D-Bus under it; userdel
    refuses an account whose processes are still running, and the teardown that
    produced SYRD-209 tore down everything else and then left the account -- and
    its linger -- behind, so the next `switchyard new` for the same slug met an
    existing owner.

    Every step tolerates its own absence, because a teardown must be resumable
    after a partial one: linger that was never enabled, a manager that is not
    running, and an account already gone are all states this can be run from.
    """
    from scripts import team_launcher as launcher

    from scripts import teardown_tenant

    quoted = shlex.quote(owner_user)
    # Linger off, and its file gone: `disable-linger` left /var/lib/systemd/linger/<user> on test11 (SYRD-786).
    linger = teardown_tenant.linger_script(owner_user)
    # The manager first by unit, then the session by user: stopping
    # user@UID.service takes the manager and everything under it, and
    # terminate-user catches a session that was started another way. Neither is
    # an error when there is nothing to stop.
    end_session = (
        f"if id -u {quoted} >/dev/null 2>&1; then "
        f"owner_uid=$(id -u {quoted}); "
        'systemctl stop "user@${owner_uid}.service" 2>/dev/null || true; '
        f"loginctl terminate-user {quoted} 2>/dev/null || true; "
        'systemctl stop "user-runtime-dir@${owner_uid}.service" 2>/dev/null || true; '
        "else echo 'account already absent; no session to end'; fi"
    )
    # Every process of the account, not only its manager's: the panes' tmux servers and agents run under neither the
    # manager nor a session, so stopping those left 58 processes on test11 and test12 and userdel refused the account.
    # SIGTERM, then SIGKILL, each bounded, and it fails naming what is left (SYRD-786).
    settle = teardown_tenant.process_stop_script(owner_user)
    remove = (
        f"if id -u {quoted} >/dev/null 2>&1; then userdel {quoted}; "
        "else echo 'account already absent'; fi"
    )
    return [
        launcher.SwitchyardTeardownAction(
            f"disable linger for owner {owner_user} and remove its linger file",
            launcher._bash_action(linger),
        ),
        launcher.SwitchyardTeardownAction(
            f"stop the systemd user manager and session for {owner_user}",
            launcher._bash_action(end_session),
        ),
        launcher.SwitchyardTeardownAction(
            f"stop every process of {owner_user} (its panes, agents and user manager): SIGTERM, then SIGKILL, "
            "and check none remains",
            launcher._bash_action(settle),
        ),
        launcher.SwitchyardTeardownAction(
            f"remove owner user account {owner_user}",
            launcher._bash_action(remove),
        ),
    ]


def owner_removal_residue(
    owner_user: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> list[str]:
    """What is still there after the removal ran. Empty means it worked.

    Read back from the machine rather than inferred from exit statuses: the
    whole of SYRD-209 is a teardown that reported success over an account that
    was still in /etc/passwd, still lingering, and still running a user manager.
    """
    residue: list[str] = []
    quoted = shlex.quote(owner_user)
    account = runner(["bash", "-lc", f"getent passwd {quoted} || true"],
                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    entry = str(getattr(account, "stdout", "") or "").strip()
    if entry:
        residue.append(f"the account still exists: {entry.splitlines()[0]}")
    linger = runner(["bash", "-lc", f"loginctl show-user {quoted} -p Linger --value 2>/dev/null || true"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    if str(getattr(linger, "stdout", "") or "").strip().lower() == "yes":
        residue.append(f"linger is still enabled for {owner_user}")
    processes = runner(["bash", "-lc", f"ps -o pid=,comm= -u {quoted} 2>/dev/null || true"],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    running = [line.strip() for line in str(getattr(processes, "stdout", "") or "").splitlines() if line.strip()]
    if running:
        residue.append(
            f"{len(running)} process(es) still running as {owner_user}: "
            + ", ".join(running[:6]) + (" ..." if len(running) > 6 else "")
        )
    return residue


def _switchyard_teardown_actions(
    plan: "ProjectBoardProvision | TeardownPlanFields",
    *,
    registry_path: Path,
    home_base: Path,
    remove_owner_home: bool,
    remove_owner_user: bool,
    keep_board_data: bool = False,
) -> tuple[SwitchyardTeardownAction, ...]:
    from scripts import team_launcher as launcher
    from scripts import teardown_tenant

    owner_home = home_base / plan.owner_user
    listener_unit_path = owner_home / ".config" / "systemd" / "user" / plan.listener_unit
    owner_runtime_script = (
        f"owner_uid=$(id -u {shlex.quote(plan.owner_user)} 2>/dev/null || true); "
        'if [ -n "$owner_uid" ] && [ -S "/run/user/$owner_uid/bus" ]; then '
        f"sudo -u {shlex.quote(plan.owner_user)} env "
        'XDG_RUNTIME_DIR="/run/user/$owner_uid" '
        'DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$owner_uid/bus" '
        f"systemctl --user disable --now {shlex.quote(plan.listener_unit)} || true; "
        "fi"
    )
    board_service_script = (
        f"if systemctl list-unit-files --no-legend {shlex.quote(plan.board_unit)} | grep -q .; then "
        f"systemctl disable --now {shlex.quote(plan.board_unit)}; "
        "fi"
    )
    board_root = Path(plan.board_root)
    actions = [
        launcher.SwitchyardTeardownAction(
            f"stop and disable system board service {plan.board_unit}",
            launcher._bash_action(board_service_script),
        ),
        launcher.SwitchyardTeardownAction(
            f"stop and disable owner notify-listener service {plan.listener_unit}",
            launcher._bash_action(owner_runtime_script),
        ),
        # While the account still exists, so its uid can be named: its desktop grant, as the desktop user recorded
        # it, then every ACL entry naming the uid -- or a later account given that uid inherits the grant (SYRD-786).
        launcher.SwitchyardTeardownAction(
            f"withdraw {plan.owner_user}'s desktop grant (screenshots and clipboard) and its ACL entries",
            teardown_tenant.step_command("withdraw-desktop", plan.owner_user),
        ),
        launcher.SwitchyardTeardownAction(
            f"remove owner notify-listener unit {listener_unit_path}",
            ("rm", "-f", str(listener_unit_path)),
        ),
        launcher.SwitchyardTeardownAction(
            f"remove system board unit /etc/systemd/system/{plan.board_unit}",
            ("rm", "-f", f"/etc/systemd/system/{plan.board_unit}"),
        ),
        launcher.SwitchyardTeardownAction(
            f"remove tmpfiles config /etc/tmpfiles.d/{plan.tmpfiles_name}",
            ("rm", "-f", f"/etc/tmpfiles.d/{plan.tmpfiles_name}"),
        ),
        launcher.SwitchyardTeardownAction(
            f"remove tenant control grant /etc/sudoers.d/{plan.tenant_control_sudoers_name}",
            ("rm", "-f", f"/etc/sudoers.d/{plan.tenant_control_sudoers_name}"),
        ),
        launcher.SwitchyardTeardownAction(
            f"remove tenant control data /usr/local/lib/switchyard/{plan.project}/control-grant.json",
            ("rm", "-f", f"/usr/local/lib/switchyard/{plan.project}/control-grant.json"),
        ),
        # The helper is inert without its grant, but a torn-down tenant should
        # leave no part of its bridge behind for a later project to inherit.
        launcher.SwitchyardTeardownAction(
            f"remove tenant control helper {launcher.tenant_control_helper_path(plan.project)}",
            ("rm", "-f", launcher.tenant_control_helper_path(plan.project)),
        ),
        launcher.SwitchyardTeardownAction(
            f"remove polkit rule /etc/polkit-1/rules.d/{plan.polkit_name}",
            ("rm", "-f", f"/etc/polkit-1/rules.d/{plan.polkit_name}"),
        ),
        launcher.SwitchyardTeardownAction(
            "reload systemd manager configuration",
            ("systemctl", "daemon-reload"),
        ),
        *(() if keep_board_data else (launcher.SwitchyardTeardownAction(
            f"drop PostgreSQL database {plan.database}",
            launcher._drop_database_command(plan.database),
        ),)),
        launcher.SwitchyardTeardownAction(
            f"remove board release root {board_root}",
            ("rm", "-rf", "--", str(board_root)),
        ),
        launcher.SwitchyardTeardownAction(
            f"remove switchyard registry entry {registry_path}",
            ("rm", "-f", str(registry_path)),
        ),
        # Root's records of the tenant: its staged tooling, provisioning record and publication grant go, its rollout
        # journal stays as root's own record, re-owned to root, and anything else its uid or gid owns is re-owned
        # (SYRD-786).
        launcher.SwitchyardTeardownAction(
            f"remove root's staged tooling, provisioning record and publication grant for {plan.project}, keep its "
            "rollout journal re-owned to root, and re-own anything else its uid or gid owns",
            teardown_tenant.step_command(
                "root-records", plan.project, plan.owner_user,
                str(launcher.privileged_provision_dir(plan.project, root=launcher.switchyard_privileged_provision_root())),
                os.environ.get("SWITCHYARD_ROLLOUT_JOURNAL_ROOT") or "/var/lib/switchyard/rollout"),
        ),
    ]
    # The account's processes stop before anything of it is removed, its panes included: removing the home under a
    # running tenant left it "nearly empty" and still in use on test11 (SYRD-786).
    removal = launcher._owner_removal_actions(plan.owner_user)
    stops = removal[:3] if remove_owner_user else removal[1:3]
    actions[3:3] = stops
    from scripts import board_services

    if board_services.board_is_supervised(plan.project):
        # First: the unit is already disabled, so the next step stops nothing;
        # the board is the host supervisor's child (SYRD-586).
        actions.insert(0, board_services.supervised_board_release_action(plan.project, plan.board_unit))
    if remove_owner_home:
        actions.append(
            launcher.SwitchyardTeardownAction(
                f"remove owner home directory {owner_home}",
                ("rm", "-rf", "--", str(owner_home)),
            )
        )
    if remove_owner_user:
        # Last: linger, the session and every process were ended before the files went, so userdel finds the account
        # idle; the run that produced SYRD-209 removed every other trace and left this one behind (SYRD-209, SYRD-786).
        actions.append(removal[3])
    return tuple(actions)


def _print_teardown_plan(
    teardown: SwitchyardTeardownPlan,
    *,
    dry_run: bool,
    keep_home: bool = False,
    keep_user: bool = False,
    keep_board_data: bool = False,
    facts: Mapping[str, Any] | None = None,
    print_func: Callable[[str], None],
) -> None:
    """The one preview, before anything changes: what goes, what is kept and why, and what is running now (SYRD-786)."""
    from scripts import teardown_tenant

    facts = dict(facts or {})
    print_func(f"switchyard: teardown of {teardown.project} removes the whole tenant")
    size = f" ({facts['home_size']})" if facts.get("home_size") else ""
    print_func(f"switchyard: owner user: {teardown.owner_user}" + ("" if facts.get("account_exists", True)
                                                                  else " (already gone)"))
    print_func(f"switchyard: owner home: {teardown.owner_home}{size}")
    print_func(f"switchyard: project checkout: {teardown.project_checkout}")
    ticket_count = "unknown" if teardown.ticket_count is None else str(teardown.ticket_count)
    print_func(f"switchyard: board ticket count: {ticket_count}")
    print_func(f"switchyard: registered and launchable: {'yes' if teardown.registered_healthy else 'no'}")
    running = facts.get("processes") or []
    print_func(f"switchyard: running now as {teardown.owner_user}: {teardown_tenant.describe_processes(running)}"
               + ("; its panes, agents and user manager are stopped first" if running else ""))
    kept = []
    if keep_user:
        kept.append(f"  - owner user account {teardown.owner_user} (--keep-user)")
    if keep_home:
        kept.append(f"  - owner home directory {teardown.owner_home}{size} and the project checkout in it (--keep-home)")
        if teardown.control_repository is not None and teardown.worktree_base is not None:
            kept.append(
                f"  - commit store {teardown.control_repository} and role worktrees under {teardown.worktree_base}: "
                f"a later `switchyard new` for {teardown.project} names the same paths, and its launch refuses "
                "this store while it fetches from another checkout -- move both aside before that")
    if keep_board_data:
        kept.append(f"  - board database and its {ticket_count} ticket(s) (--keep-board-data)")
    print_func("switchyard: kept:" if kept else "switchyard: kept: nothing of the tenant but root's rollout journal")
    for line in kept:
        print_func(line)
    print_func("switchyard: actions:")
    for index, action in enumerate(teardown.actions, start=1):
        print_func(f"  {index}. {action.label}")
        print_func(f"     command: {action.command_display}")
    if dry_run:
        print_func("switchyard: dry-run only; no changes made")


def _confirm_teardown_project(
    project: str,
    *,
    confirmation: str | None,
    input_func: Callable[[str], str],
    yes: bool = False,
) -> None:
    """The slug typed once -- the safety step in place of the old flags -- or `--yes` / `--confirm <slug>` for a
    script; with nobody to type it, refused (SYRD-786)."""
    from scripts import teardown_tenant

    teardown_tenant.confirm_by_typing(project, yes=yes, confirm=confirmation, input_func=input_func)


def _run_teardown_actions(
    actions: Sequence[SwitchyardTeardownAction],
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    print_func: Callable[[str], None],
) -> None:
    completed: list[SwitchyardTeardownAction] = []
    for index, action in enumerate(actions):
        print_func(f"switchyard: running: {action.label}")
        result = runner(list(action.command), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode == 0:
            completed.append(action)
            # What the step says it did and kept -- root's records, the processes it stopped (SYRD-786).
            for line in str(getattr(result, "stdout", "") or "").splitlines():
                if line.strip():
                    print_func(f"    {line.strip()}")
            continue
        stderr = str(getattr(result, "stderr", "") or "").strip()
        stdout = str(getattr(result, "stdout", "") or "").strip()
        detail = stderr or stdout or f"exit status {result.returncode}"
        remaining = list(actions[index:])
        message = [
            f"switchyard: teardown failed while trying to {action.label}: {detail}",
            "switchyard: completed before failure:",
        ]
        message.extend(f"  - {item.label}" for item in completed)
        if not completed:
            message.append("  - (none)")
        message.append("switchyard: remaining after failure:")
        message.extend(f"  - {item.label}" for item in remaining)
        raise SystemExit("\n".join(message))


def switchyard_teardown_command(
    project: str,
    *,
    dry_run: bool = False,
    confirm: str | None = None,
    #: The old opt-in flags. Removing the whole tenant is now the default, so they are accepted and change nothing,
    #: and old scripts and docs keep working (SYRD-786).
    drop_nonempty_board: bool = False,
    destroy_registered_tenant: bool = False,
    remove_owner_home: bool = False,
    remove_owner_user: bool = False,
    owner_user: str | None = None,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
    home_base: Path = Path("/home"),
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    input_func: Callable[[str], str] = input,
    print_func: Callable[[str], None] = print,
    #: Deviations from the whole tenant, each keeping one part (SYRD-786).
    keep_home: bool = False,
    keep_user: bool = False,
    keep_board_data: bool = False,
    #: Skips the typed confirmation, for scripts.
    yes: bool = False,
) -> int:
    from scripts import team_launcher as launcher
    from scripts import teardown_tenant

    plan, project_checkout, registry_path, registered_healthy = launcher._teardown_project_context(
        project,
        owner_user=owner_user,
        config_dir=config_dir,
        registry_dir=registry_dir,
        home_base=home_base,
    )
    ticket_count = launcher._ticket_board_existing_ticket_count(plan.database, runner=runner)
    owner_home = home_base / plan.owner_user
    teardown = launcher.SwitchyardTeardownPlan(
        project=plan.project,
        owner_user=plan.owner_user,
        owner_home=owner_home,
        project_checkout=project_checkout,
        ticket_count=ticket_count,
        registered_healthy=registered_healthy,
        registry_path=registry_path,
        actions=launcher._switchyard_teardown_actions(
            plan,
            registry_path=registry_path,
            home_base=home_base,
            remove_owner_home=not keep_home,
            remove_owner_user=not keep_user,
            keep_board_data=keep_board_data,
        ),
        # The paths `switchyard new` gives this slug, under this owner's home.
        control_repository=owner_home / ".local" / "state" / "switchyard" / "projects" / plan.project / "control.git",
        worktree_base=owner_home / f"{plan.project}-worktrees",
    )
    # The guards that stay, as plain refusals rather than flags: never the tenant this runs from, and never anything
    # but this tenant's own agent account (SYRD-209, SYRD-786). Asked before the plan is
    # printed and before anything runs, so a dry run shows a refusal and a real run costs nothing to refuse.
    try:
        registry_name = str(launcher._load_json(registry_path).get("name") or "") if registry_path.is_file() else ""
    except (OSError, ValueError, AttributeError, SystemExit):
        registry_name = ""
    # The account's processes are stopped and its home removed even with --keep-user, so the account is judged on
    # every run: never a person's, an administrator's or another tenant's (SYRD-786 Audit).
    refusals = [teardown_tenant.self_tenant_refusal(plan.project, registry_name=registry_name),
                launcher.owner_removal_refusal(plan.owner_user), teardown_tenant.agent_account_refusal(plan.owner_user),
                teardown_tenant.other_tenant_refusal(plan.project, plan.owner_user, registry_dir=registry_path.parent,
                                                     owner_home=owner_home)]
    refusals = [reason for reason in refusals if reason]
    facts = teardown_tenant.preview_facts(plan.owner_user, owner_home, runner=runner)
    launcher._print_teardown_plan(
        teardown, dry_run=dry_run, keep_home=keep_home, keep_user=keep_user, keep_board_data=keep_board_data,
        facts=facts, print_func=print_func,
    )
    for reason in refusals:
        print_func(f"switchyard: refused: {reason}")
    if dry_run:
        return 0
    if refusals:
        raise SystemExit(f"switchyard: refusing to tear down {plan.project}: {refusals[0]}. Nothing was changed.")
    launcher._confirm_teardown_project(plan.project, confirmation=confirm, input_func=input_func, yes=yes)
    launcher._run_teardown_actions(teardown.actions, runner=runner, print_func=print_func)
    # Read back from the machine before claiming this worked. Every action exiting zero is not the same fact as the
    # tenant being gone: SYRD-209's teardown reported success over an owner that still existed, still lingered, and
    # still had a user manager running, and test11's left its home and 58 processes (SYRD-786).
    residue: list[str] = []
    if not keep_user:
        residue += launcher.owner_removal_residue(plan.owner_user, runner=runner)
        if (teardown_tenant.LINGER_DIR / plan.owner_user).exists():
            residue.append(f"the linger file {teardown_tenant.LINGER_DIR / plan.owner_user} is still there")
    if not keep_home and owner_home.exists():
        residue.append(f"the home {owner_home} is still there")
    if residue:
        message = [f"switchyard: teardown did NOT remove all of {plan.project}; it is not safe to recreate under the "
                   "same slug."]
        message.extend(f"  - {item}" for item in residue)
        raise SystemExit("\n".join(message))
    if not keep_user:
        print_func(f"switchyard: owner account {plan.owner_user} removed; no residue")
    print_func(f"switchyard: teardown complete for {plan.project}")
    return 0


@dataclass(frozen=True)
class SwitchyardTeardownAction:
    label: str
    command: tuple[str, ...]

    @property
    def command_display(self) -> str:
        return shlex.join(self.command)


@dataclass(frozen=True)
class SwitchyardTeardownPlan:
    project: str
    owner_user: str
    owner_home: Path
    project_checkout: Path
    ticket_count: int | None
    registered_healthy: bool
    registry_path: Path
    actions: tuple[SwitchyardTeardownAction, ...]
    #: Where `switchyard new` puts this slug's commit store and role worktrees
    #: in the owner's home. Kept with the home, and a later `new` for the slug
    #: names the same paths (SYRD-545).
    control_repository: Path | None = None
    worktree_base: Path | None = None
