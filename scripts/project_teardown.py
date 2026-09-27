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
) -> tuple[ProjectBoardProvision, Path, Path, bool]:
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
        plan = launcher._project_board_provision_from_json(plan_path)
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

    quoted = shlex.quote(owner_user)
    linger = (
        f"if id -u {quoted} >/dev/null 2>&1; then "
        f"loginctl disable-linger {quoted} 2>/dev/null || true; "
        "else echo 'account already absent; nothing to unlinger'; fi"
    )
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
    # Bounded, and it says what it is waiting for. A fixed sleep either wastes
    # the time or does not wait long enough, and neither reports what was still
    # running when it gave up.
    settle = (
        f"if ! id -u {quoted} >/dev/null 2>&1; then echo 'account already absent'; exit 0; fi; "
        "for _ in $(seq 1 50); do "
        f"pgrep -u {quoted} >/dev/null 2>&1 || exit 0; sleep 0.2; done; "
        f"echo \"processes still running as {owner_user} after waiting:\" >&2; "
        f"ps -o pid=,comm= -u {quoted} >&2; exit 1"
    )
    remove = (
        f"if id -u {quoted} >/dev/null 2>&1; then userdel {quoted}; "
        "else echo 'account already absent'; fi"
    )
    return [
        launcher.SwitchyardTeardownAction(
            f"disable linger for owner {owner_user}",
            launcher._bash_action(linger),
        ),
        launcher.SwitchyardTeardownAction(
            f"stop the systemd user manager and session for {owner_user}",
            launcher._bash_action(end_session),
        ),
        launcher.SwitchyardTeardownAction(
            f"wait for {owner_user}'s remaining processes to exit",
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
    plan: ProjectBoardProvision,
    *,
    registry_path: Path,
    home_base: Path,
    remove_owner_home: bool,
    remove_owner_user: bool,
) -> tuple[SwitchyardTeardownAction, ...]:
    from scripts import team_launcher as launcher

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
        launcher.SwitchyardTeardownAction(
            f"drop PostgreSQL database {plan.database}",
            launcher._drop_database_command(plan.database),
        ),
        launcher.SwitchyardTeardownAction(
            f"remove board release root {board_root}",
            ("rm", "-rf", "--", str(board_root)),
        ),
        launcher.SwitchyardTeardownAction(
            f"remove switchyard registry entry {registry_path}",
            ("rm", "-f", str(registry_path)),
        ),
    ]
    if remove_owner_home:
        actions.append(
            launcher.SwitchyardTeardownAction(
                f"remove owner home directory {owner_home}",
                ("rm", "-rf", "--", str(owner_home)),
            )
        )
    if remove_owner_user:
        # Four steps, in this order, rather than one `userdel`. The account this
        # removes has linger enabled and a systemd --user manager running under
        # it; userdel refuses a busy account, and the run that produced SYRD-209
        # removed every other trace and left this one behind (SYRD-209).
        actions.extend(launcher._owner_removal_actions(plan.owner_user))
    return tuple(actions)


def _print_teardown_plan(
    teardown: SwitchyardTeardownPlan,
    *,
    dry_run: bool,
    drop_nonempty_board: bool,
    destroy_registered_tenant: bool,
    remove_owner_home: bool,
    remove_owner_user: bool,
    print_func: Callable[[str], None],
) -> None:
    print_func(f"switchyard: teardown plan for {teardown.project}")
    print_func(f"switchyard: owner user: {teardown.owner_user}")
    print_func(f"switchyard: owner home: {teardown.owner_home}")
    print_func(f"switchyard: project checkout: {teardown.project_checkout}")
    ticket_count = "unknown" if teardown.ticket_count is None else str(teardown.ticket_count)
    print_func(f"switchyard: board ticket count: {ticket_count}")
    registered_health = "yes" if teardown.registered_healthy else "no"
    print_func(f"switchyard: registered and launchable: {registered_health}")
    if teardown.registered_healthy and not destroy_registered_tenant:
        print_func("switchyard: live-tenant guard: destructive run requires --destroy-registered-tenant")
    if teardown.ticket_count is None and not drop_nonempty_board:
        print_func("switchyard: board-count guard: destructive run requires --drop-nonempty-board")
    elif teardown.ticket_count and not drop_nonempty_board:
        print_func("switchyard: non-empty board guard: destructive run requires --drop-nonempty-board")
    print_func("switchyard: preserved by default:")
    if not remove_owner_user:
        print_func(f"  - owner user account {teardown.owner_user}")
    if not remove_owner_home:
        print_func(f"  - owner home directory {teardown.owner_home}")
        print_func(f"  - project checkout {teardown.project_checkout}")
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
) -> None:
    from scripts import team_launcher as launcher

    expected = project.strip()
    answer = confirmation
    if answer is None:
        answer = launcher._read_prompt(f"Type {expected} to tear down this project: ", input_func=input_func).strip()
    if answer != expected:
        raise SystemExit(f"switchyard: teardown confirmation failed; expected {expected!r}")


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
) -> int:
    from scripts import team_launcher as launcher

    plan, project_checkout, registry_path, registered_healthy = launcher._teardown_project_context(
        project,
        owner_user=owner_user,
        config_dir=config_dir,
        registry_dir=registry_dir,
        home_base=home_base,
    )
    ticket_count = launcher._ticket_board_existing_ticket_count(plan.database, runner=runner)
    teardown = launcher.SwitchyardTeardownPlan(
        project=plan.project,
        owner_user=plan.owner_user,
        owner_home=home_base / plan.owner_user,
        project_checkout=project_checkout,
        ticket_count=ticket_count,
        registered_healthy=registered_healthy,
        registry_path=registry_path,
        actions=launcher._switchyard_teardown_actions(
            plan,
            registry_path=registry_path,
            home_base=home_base,
            remove_owner_home=remove_owner_home,
            remove_owner_user=remove_owner_user,
        ),
    )
    # Asked before the plan is printed and before anything runs, so a dry run
    # shows the refusal and a real run costs nothing to refuse. One account is
    # in scope -- the one this verified plan names -- and these are the checks
    # that catch a plan aimed somewhere else (SYRD-209).
    owner_refusal = launcher.owner_removal_refusal(plan.owner_user) if remove_owner_user else ""
    launcher._print_teardown_plan(
        teardown,
        dry_run=dry_run,
        drop_nonempty_board=drop_nonempty_board,
        destroy_registered_tenant=destroy_registered_tenant,
        remove_owner_home=remove_owner_home,
        remove_owner_user=remove_owner_user,
        print_func=print_func,
    )
    if owner_refusal:
        print_func(f"switchyard: owner removal refused: {owner_refusal}")
    if dry_run:
        return 0
    if owner_refusal:
        raise SystemExit(f"switchyard: refusing to remove the owner account: {owner_refusal}")
    if registered_healthy and not destroy_registered_tenant:
        raise SystemExit(
            f"switchyard: refusing to tear down registered launchable tenant {plan.project!r}; "
            "pass --destroy-registered-tenant to confirm removing its board unit, database, release root, and registry entry"
        )
    if ticket_count is None and not drop_nonempty_board:
        raise SystemExit(
            f"switchyard: cannot determine whether board database {plan.database} contains tickets; "
            "pass --drop-nonempty-board to confirm dropping it anyway"
        )
    if ticket_count and not drop_nonempty_board:
        raise SystemExit(
            f"switchyard: refusing to drop non-empty board database {plan.database} "
            f"with {ticket_count} ticket(s); pass --drop-nonempty-board to confirm that data loss"
        )
    launcher._confirm_teardown_project(plan.project, confirmation=confirm, input_func=input_func)
    launcher._run_teardown_actions(teardown.actions, runner=runner, print_func=print_func)
    # Read back from the machine before claiming this worked. Every action
    # exiting zero is not the same fact as the account being gone, and the
    # incident this fixes is a teardown that reported success over an owner
    # that still existed, still lingered, and still had a user manager running
    # (SYRD-209).
    if remove_owner_user:
        residue = launcher.owner_removal_residue(plan.owner_user, runner=runner)
        if residue:
            message = [
                f"switchyard: teardown did NOT remove {plan.owner_user}; "
                f"{plan.project} is not safe to recreate under the same slug.",
            ]
            message.extend(f"  - {item}" for item in residue)
            raise SystemExit("\n".join(message))
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
