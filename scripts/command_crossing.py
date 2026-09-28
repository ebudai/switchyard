"""How a per-project `switchyard` command loads its configuration, and crosses to the owner or root.

One authority decision, in one place:

- `_load_switchyard_project_config_for_command` checks the caller against the
  configuration path's owner, loads the configuration (root's read is the
  no-follow one), crosses to root only on a permission error, and checks the
  caller against the configured owner.
- `_require_switchyard_owner_hint_or_root` and
  `_require_switchyard_project_owner_or_root` let the owner, root, and a role
  account running its own unprivileged command through; anyone else crosses.
- `_switchyard_cross_account` takes the narrowest installed route: the
  tenant-control bridge for a granted lifecycle verb (offering host-wide CLI
  promotion before a start), otherwise sudo.
- `_switchyard_exec_through_tenant_control` runs one verb over the bridge as
  the owner: the caller must be the granted operator, the helper and the staged
  bundle are repaired when absent and refused when hostile
  (`ensure_staged_role_bundle_before_crossing`), and the desktop half of the
  verb is completed on this side.
- `_switchyard_exec_with_root` re-executes under sudo, non-interactively when
  it can and with a prompt only for a caller who can answer one
  (`_switchyard_user_can_prompt_for_sudo`); `_configured_role_account_caller`,
  `_switchyard_command_is_unprivileged` and `_switchyard_command_display` are
  their small helpers.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-414), in their original
order. The launcher imports this module and re-exports all eleven names;
`switchyard_main` and `scripts/presentation_commands.py` still reach the loader
through the launcher. Everything the eleven call -- each other included, and
the grant, the bridge operation, the helper repair, the staged-bundle check,
the desktop halves, the CLI promotion, the configuration loader, the path
owner, the unprivileged-command set and the current user -- is read through
the launcher at call time, so a suite that rebinds one there still intercepts
it. The defaults Python binds at definition -- the staged tooling owner, the
helper repair, the caller-aware `which`, `subprocess.run`, `os.execvp`, `input`
and `print` -- are bound here from their own modules: the same objects. The
annotation-only types are imported under TYPE_CHECKING. This module imports
`team_launcher` only inside the functions, when they run.
"""

from __future__ import annotations

import grp
import os
import pwd
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from scripts.agent_cli_discovery import caller_aware_which
from scripts.staged_role_tooling import STAGED_TOOLING_OWNER_UID
from scripts.tenant_control_helper import ensure_tenant_control_helper

if TYPE_CHECKING:
    from scripts.agent_cli_discovery import AgentCliAvailability
    from scripts.team_launcher import ProjectConfig, SwitchyardProjectEntry


def _switchyard_command_display(argv: Sequence[str]) -> str:
    if not argv:
        return "switchyard"
    return f"switchyard {argv[0]}"


def _switchyard_user_can_prompt_for_sudo() -> bool:
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        return False
    try:
        user = pwd.getpwuid(os.geteuid())
        group_ids = {user.pw_gid, *os.getgroups()}
    except KeyError:
        return False
    group_names: set[str] = set()
    for gid in group_ids:
        try:
            group_names.add(grp.getgrgid(gid).gr_name)
        except KeyError:
            continue
    return bool({"sudo", "wheel", "admin"} & group_names)


def ensure_staged_role_bundle_before_crossing(
    project: str,
    *,
    release_root: str = "",
    root: Path | None = None,
    expect_uid: int = STAGED_TOOLING_OWNER_UID,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> str:
    """Stage what a tenant is missing, before root runs anything as its owner.

    The bridge repair before this one covers the two programs whose wire
    contract the crossing itself depends on. That is the right scope for it and
    the wrong scope for a tenant that has NONE of its bundle: `test` was
    provisioned with a staging directory holding only control-grant.json and
    its board unit, so the bridge crossed cleanly and its panes still had
    nothing to run (SYRD-249).

    So absence is repaired here, through the same recorded privileged command
    the helper repair uses -- one journalled step, scoped to this tenant's own
    directory, rendered from the selected release. The caller is the desktop
    operator, who has sudo; the tenant account is never asked to stage anything.

    A bundle that is merely OLDER is left alone, because restaging on drift
    would make every launch privileged (SYRD-211). A bundle that is present and
    wrong -- another account's, writable by others, not a regular file -- is
    refused rather than overwritten, for the same reason the helper repair
    refuses it. Returns "" when the launch may proceed.
    """
    from scripts import team_launcher as launcher

    absent, hostile, release = launcher.staged_bundle_launch_problems(
        project, release_root=release_root, root=root, expect_uid=expect_uid
    )
    if hostile:
        return f"{project}'s staged tooling is not root's to replace: " + "; ".join(hostile)
    if not absent:
        # The healthy path -- including a complete bundle from an older release
        # -- costs a few stats and no privileged step.
        return ""
    print_func(
        f"switchyard: {project} is missing {len(absent)} of its staged role tooling; "
        f"restaging the bundle from {release} before continuing"
    )
    problem = launcher.repair_tenant_control_helper(
        project, release_root=str(release), root=root, runner=runner, print_func=print_func
    )
    if problem:
        return problem
    still_absent, _hostile, _release = launcher.staged_bundle_launch_problems(
        project, release_root=str(release), root=root, expect_uid=expect_uid
    )
    if still_absent:
        return (
            f"{project}'s staged role tooling is still incomplete after restaging: "
            + "; ".join(still_absent[:4])
        )
    return ""


def _switchyard_exec_through_tenant_control(
    project: str,
    operation: str,
    *,
    grant: dict[str, str],
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    exec_func: Callable[[str, Sequence[str]], Any] = os.execvp,
    print_func: Callable[[str], None] = print,
    ensure_helper: Callable[..., None] = ensure_tenant_control_helper,
) -> None:
    """Run one lifecycle verb as the owner, without a password.

    Only the project and the verb cross the boundary. The bridge decides the
    owner, the launcher and whether this caller is allowed, from root-owned
    data, so nothing here can widen what it will do.
    """
    from scripts import team_launcher as launcher

    sudo_bin = os.environ.get("SWITCHYARD_SUDO_BIN", "sudo")
    helper = str(launcher.TENANT_CONTROL_ROOT / project / "switchyard-tenant-control")
    caller = launcher.current_user_name()
    authorized = grant.get("authorized_user", "")
    if caller != authorized:
        # Refused here, so an unauthorized local user never sees a prompt.
        raise SystemExit(
            f"switchyard: {caller} may not control {project}; it is registered to {authorized}\n"
            "switchyard: ask that user, or run this as the project owner or an operator"
        )
    # Before root is asked to run it. A registered tenant whose staging was
    # interrupted -- or which predates staging -- has a valid grant, a valid
    # sudoers rule and no file, and handing that to sudo produced the whole of
    # SYRD-211: `command not found`, before anything else could say why. Absent
    # is repaired from the current release and the launch resumes; any other
    # shape is refused here rather than executed.
    ensure_helper(project, grant=grant, runner=runner, print_func=print_func)
    # And the rest of the bundle, while this process still belongs to somebody
    # with sudo. Past this line the work happens as the tenant owner, which may
    # not stage root's files -- so a tenant missing everything but its grant
    # would otherwise cross successfully and open panes with nothing to run
    # (SYRD-249).
    bundle_problem = launcher.ensure_staged_role_bundle_before_crossing(
        project, runner=runner, print_func=print_func
    )
    if bundle_problem:
        raise SystemExit(f"switchyard: {bundle_problem}")
    # Run, not replace. The bridge answers with one validated handoff when the
    # owner half could not do the desktop half, and this process -- which owns
    # the desktop -- is the one that can. Its terminal is passed through
    # untouched, so a verb that attaches a tmux client still has one (SYRD-90).
    result = runner([sudo_bin, "-n", helper, project, operation])
    code = int(getattr(result, "returncode", 1) or 0)
    if code == 0:
        # Symmetric with the open. A stop's desktop half is closing the window
        # this account owns; the owner half cannot see it or signal it
        # (SYRD-202).
        if operation == "stop":
            code = launcher.close_desktop_presentation(project, caller=caller)
        elif operation == "recover-display":
            # A recovery reattaches the Director inside the window that is
            # already open; it has no window half to complete. Asking for one
            # would find no handoff and -- on a tenant with desktop access --
            # report the successful recovery as "no presentation window was
            # handed back ... run it again" (SYRD-239 live UAT).
            print_func(f"switchyard: {project}'s Director display was recovered")
        else:
            code = launcher.complete_desktop_presentation(project, caller=caller, runner=runner)
    raise SystemExit(code)


def _switchyard_exec_with_root(
    argv: Sequence[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    exec_func: Callable[[str, Sequence[str]], Any] = os.execvp,
) -> None:
    from scripts import team_launcher as launcher

    sudo_bin = os.environ.get("SWITCHYARD_SUDO_BIN", "sudo")
    command_display = launcher._switchyard_command_display(argv)
    target_argv = [sys.argv[0], *argv]
    if runner([sudo_bin, "-n", "-v"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        exec_func(sudo_bin, [sudo_bin, "-n", *target_argv])
        raise SystemExit(0)
    if launcher._switchyard_user_can_prompt_for_sudo():
        exec_func(sudo_bin, [sudo_bin, *target_argv])
        raise SystemExit(0)
    raise SystemExit(
        f"switchyard: command requires root: {command_display}\n"
        "switchyard: sudo is unavailable for this user or shell; run it as a sudo-capable human or ask an operator"
    )


def _configured_role_account_caller(config: ProjectConfig) -> str:
    """The configured role this caller's Unix account IS, or empty.

    Bound to the account, never to a role name the caller supplies: the
    configuration says which account belongs to which role, and the board still
    decides authority from the peer uid on its own (SYRD-49).
    """
    from scripts import team_launcher as launcher

    caller = launcher.current_user_name()
    owner = (config.run_as_user or "").strip()
    for role in config.roles:
        account = (role.run_as_user or "").strip()
        if account and account != owner and account == caller:
            return role.role
    return ""


def _switchyard_command_is_unprivileged(argv: Sequence[str]) -> bool:
    from scripts import team_launcher as launcher

    return bool(argv) and argv[0].casefold() in launcher.SWITCHYARD_UNPRIVILEGED_COMMANDS


def _switchyard_cross_account(
    project: str,
    argv: Sequence[str],
    *,
    agent_cli_policy: str = "",
    agent_cli_sources: Mapping[str, str] | None = None,
    interactive: bool | None = None,
    which: Callable[..., str | None] = caller_aware_which,
    input_func: Callable[[str], str] = input,
    print_func: Callable[[str], None] = print,
    promoter: Callable[..., AgentCliAvailability] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    ensure_helper: Callable[..., None] | None = None,
) -> None:
    """Reach the owner's account, by the narrowest route that is installed.

    The bridge first: it needs no password and can run only this tenant's
    lifecycle verbs. Sudo remains for everything else, and for tenants that
    have no bridge at all.
    """
    from scripts import team_launcher as launcher

    grant = launcher._tenant_control_grant(project)
    if grant:
        operation = launcher._tenant_control_operation(argv, project)
        if operation:
            if operation == "start":
                # Here, and not on the far side. Past the bridge the launcher
                # runs as the owner with a built PATH and cannot see -- let
                # alone promote -- this operator's private copies, so a resumed
                # tenant reported them as per-owner installs the operator was
                # told to repeat. Offered rather than required: nothing is being
                # created, so declining must leave the launch untouched
                # (SYRD-211).
                launcher.offer_host_wide_promotion_before_launch(
                    project,
                    policy=agent_cli_policy,
                    sources=agent_cli_sources,
                    interactive=(
                        sys.stdin.isatty() if interactive is None else interactive
                    ),
                    which=which,
                    input_func=input_func,
                    print_func=print_func,
                    promoter=promoter,
                    runner=runner,
                )
            bridge_kwargs: dict[str, Any] = {"runner": runner, "print_func": print_func}
            if ensure_helper is not None:
                bridge_kwargs["ensure_helper"] = ensure_helper
            launcher._switchyard_exec_through_tenant_control(
                project, operation, grant=grant, **bridge_kwargs
            )
    launcher._switchyard_exec_with_root(argv)


def _require_switchyard_owner_hint_or_root(entry: SwitchyardProjectEntry, argv: Sequence[str]) -> None:
    from scripts import team_launcher as launcher

    owner = launcher._project_config_path_owner_user(entry.config_path)
    if not owner or launcher.current_user_name() == owner or os.geteuid() == 0:
        return
    if launcher._switchyard_command_is_unprivileged(argv) and os.access(entry.config_path, os.R_OK):
        # A role account running its own unprivileged command. It can read the
        # configuration because provisioning granted that account exactly that,
        # and escalating here would hand the command to root -- which the
        # commands that matter then refuse, leaving no way to run them at all
        # (SYRD-49).
        return
    launcher._switchyard_cross_account(entry.slug, argv)


def _require_switchyard_project_owner_or_root(config: ProjectConfig, argv: Sequence[str]) -> None:
    from scripts import team_launcher as launcher

    owner = (config.run_as_user or "").strip()
    if not owner or launcher.current_user_name() == owner or os.geteuid() == 0:
        return
    if launcher._switchyard_command_is_unprivileged(argv) and launcher._configured_role_account_caller(config):
        return
    launcher._switchyard_cross_account(config.project, argv)


def _load_switchyard_project_config_for_command(entry: SwitchyardProjectEntry, argv: Sequence[str]) -> ProjectConfig:
    from scripts import team_launcher as launcher

    launcher._require_switchyard_owner_hint_or_root(entry, argv)
    try:
        # Root's read of this path is the no-follow one, inside the loader.
        config = launcher.load_project_config(entry.slug, entry.config_path)
    except PermissionError:
        if os.geteuid() != 0:
            launcher._switchyard_exec_with_root(argv)
        raise
    launcher._require_switchyard_project_owner_or_root(config, argv)
    return config
