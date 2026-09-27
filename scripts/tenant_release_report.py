"""What a tenant's release is, and how to deploy the next one: the status carrier, the rendered commands and the report.

- `TenantReleaseStatus` carries what `tenant_release_status` found: the board
  root and owner, the release deployed now and the one the ref resolves to, and
  where the board listens.
- `tenant_release_deploy_command`, `tenant_release_listener_command`,
  `tenant_release_unit_install_command` and `recorded_rollout_command` render
  the steps an operator runs -- each through `OWNER_BOUNDARY_SCRIPT` and
  `_owner_boundary_env_args` where it has to run as the tenant, and through the
  rollout recorder where root runs it.
- `release_update_blocked`, `_format_release_path` and
  `report_tenant_release_upgrade` say whether a safe update exists and print
  the sequence; a unit install from a directory the tenant can write is omitted,
  not printed.
- `capture_release_pointer`, `deploy_release_in_transaction` and
  `restore_release_pointer` switch the board's release inside the identities
  transaction and put the pointer back if it is rolled back.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-379), in their original
order. The launcher imports this module and re-exports every name, so the
upgrade's phases, `finish-upgrade`, the identities cutover, the release target
resolver and every suite that reaches these through the launcher reach the same
objects, the class included. Every launcher facility these use -- its release
resolver, constants and quoting helpers included -- and every name defined here
that another definition here reads when it runs, is read from `team_launcher`
when it runs, as it was, so a patch on the launcher still intercepts. What is
bound when this module loads is what was bound when the launcher loaded it:
`TenantReleaseStatus`' `dataclass` decorator, the report's default deploy ref
(`DEFAULT_TENANT_RELEASE_DEPLOY_REF`, from its own leaf `scripts.release_refs`)
and its default runner (`subprocess.run`). The project-provision helpers are
still imported inside the two functions that use them. The standard-library
names are this module's own imports, the same objects. This module never
imports `team_launcher` at its top.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

from scripts.release_refs import DEFAULT_TENANT_RELEASE_DEPLOY_REF

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig


@dataclass(frozen=True)
class TenantReleaseStatus:
    board_root: Path
    owner_user: str
    owner_home: Path
    provisioned_system_unit: Path | None
    commit_git_dir: str
    current_release: Path | None
    current_sha: str
    target_sha: str
    deploy_ref: str
    source_repo: Path
    resolve_error: str = ""
    clone_source_repo: Path | None = None
    #: Where this tenant's board actually listens. Carried because the deploy
    #: script's own defaults are a different tenant's board on a shared host
    #: (SYRD-87 R6).
    board_port: str = ""
    board_socket: str = ""

    @property
    def unchanged(self) -> bool:
        return bool(self.current_sha and self.target_sha and self.current_sha == self.target_sha)


def tenant_release_deploy_command(status: TenantReleaseStatus, project: str) -> str:
    from scripts import team_launcher as launcher

    deploy_env = [
        f"TICKET_BOARD_OWNER_HOME={status.owner_home}",
        f"TICKET_BOARD_PROJECT={project}",
        f"BOARD_ROOT={status.board_root}",
        f"DEPLOY_REF={status.deploy_ref}",
    ]
    # Where this tenant's board listens, named rather than defaulted.
    #
    # ticket-board-service.sh falls back to BOARD_PORT=8770, which on a
    # multi-tenant host is another tenant's board and is listening. The deploy's
    # own health gates then probed that board: the HTTP smoke passed against
    # http://127.0.0.1:8770/api/board, and the build-id check spent its whole
    # ten-second budget comparing a foreign board's build to this release's
    # before rolling back a syrd process that had started correctly. A gate that
    # can pass by reaching somebody else's service is not a check on this one
    # (SYRD-87 R6).
    if status.board_port:
        deploy_env.append(f"BOARD_PORT={status.board_port}")
    if status.board_socket:
        deploy_env.append(f"BOARD_UNIX_SOCKET={status.board_socket}")
    if status.provisioned_system_unit is not None:
        # The readable copy, never root's own. The deployer runs as the project
        # account and root's copy lives in the privileged provision directory,
        # which is root-only: handing over that path made a present, correct,
        # byte-identical unit read as ABSENT and refused the release (SYRD-126).
        # The copy is published by the artifacts phase from exactly that file,
        # so the comparison it feeds is still release-against-installed rather
        # than a file against itself (SYRD-127).
        from scripts.ticket_board.project_provision import readable_system_unit_path

        deploy_env.append(
            "TICKET_BOARD_PROVISIONED_SYSTEM_UNIT="
            + readable_system_unit_path(project)
        )
    if status.commit_git_dir:
        deploy_env.append(f"TICKET_BOARD_COMMIT_GIT_DIR={status.commit_git_dir}")
    if status.clone_source_repo is not None:
        marker = json.dumps({"commit": status.target_sha}, separators=(",", ":"))
        script = (
            'tmpdir="$(mktemp -d)"'
            f" && git --git-dir={shlex.quote(str(status.clone_source_repo))} archive {shlex.quote(status.target_sha)}"
            ' | tar -x -C "$tmpdir"'
            f" && printf '%s\\n' {shlex.quote(marker)} >\"$tmpdir/{launcher.SWITCHYARD_RELEASE_MARKER_NAME}\""
            " && env "
            + " ".join(shlex.quote(value) for value in deploy_env)
            + ' SOURCE_REPO="$tmpdir" "$tmpdir/scripts/ticket-board-service.sh" deploy-restart'
        )
        return launcher._quote_command(launcher._owner_boundary_env_args(status.owner_user, status.owner_home, ["sh", "-c", script]))
    service_script = status.source_repo / "scripts" / "ticket-board-service.sh"
    return launcher._quote_command(
        launcher._owner_boundary_env_args(
            status.owner_user,
            status.owner_home,
            ["env", *deploy_env, f"SOURCE_REPO={status.source_repo}", str(service_script), "deploy-restart"],
        )
    )


def tenant_release_listener_command(status: TenantReleaseStatus, project: str, action: str) -> str:
    from scripts import team_launcher as launcher

    if action not in {"stop", "start"}:
        raise ValueError(f"unsupported listener action: {action}")
    listener_unit = f"{project}-ticket-board-notify-listener.service"
    operation = f"systemctl --user {action} {shlex.quote(listener_unit)}"
    if action == "start":
        operation = f"systemctl --user daemon-reload && {operation}"
    # Exported: `start` reloads first, and a prefix would leave the start
    # itself without a bus to talk to (SYRD-61).
    script = (
        'runtime="/run/user/$(id -u)"; '
        'export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; '
        + operation
    )
    # The same boundary as the deploy: an operator pastes this line into the
    # same shell, and a `systemctl --user` that lands on root's manager stops
    # nothing and starts nothing (SYRD-138).
    command = launcher._owner_boundary_env_args(
        status.owner_user,
        status.owner_home,
        ["sh", "-c", script],
    )
    return launcher._quote_command(command)


def recorded_rollout_command(
    status: TenantReleaseStatus, project: str, command: str, *, label: str = ""
) -> str:
    """One privileged step, run so that it leaves a record root owns.

    The step itself is unchanged -- it is handed to the recorder rather than
    rewritten -- so what an operator runs is still exactly what was reviewed,
    and what survives it is a root-owned attempt directory instead of a log
    under the account every role shares (SYRD-128).
    """
    from scripts import team_launcher as launcher

    recorder = str(launcher._repo_root() / "scripts" / "switchyard-record-rollout")
    installed = launcher.switchyard_shared_install_root() / "current" / "scripts" / "switchyard-record-rollout"
    if installed.is_file():
        # The installed release's copy, not this checkout's: the operator is
        # running a release, and root should execute root-owned bytes.
        recorder = str(installed)
    prefix = ["sudo", recorder, project]
    if status.target_sha:
        prefix += ["--target-commit", status.target_sha]
    if label:
        prefix += ["--label", label]
    # The step goes in whole, through one `bash -c`, because it is a command
    # LINE: it carries its own quoting and its own `&&` chain, and leaving the
    # chain outside the recorder would record the first command and run the
    # rest unrecorded -- which is the opposite of the point.
    return " ".join(
        [*(shlex.quote(token) for token in prefix), "--", "bash", "-c", shlex.quote(command)]
    )


def tenant_release_unit_install_command(status: TenantReleaseStatus, project: str) -> str:
    from scripts import team_launcher as launcher

    from scripts.ticket_board.project_provision import system_unit_proof_chain

    if status.provisioned_system_unit is None:
        return ""
    provision_dir = status.provisioned_system_unit.parent
    board_unit = status.provisioned_system_unit
    canary_unit = provision_dir / f"{project}-ticket-board-canary.service"
    listener_unit = provision_dir / f"{project}-ticket-board-notify-listener.service"
    listener_target = status.owner_home / ".config" / "systemd" / "user" / listener_unit.name
    return " && ".join(
        [
            launcher._quote_command(["sudo", "install", "-m", "0644", str(board_unit), f"/etc/systemd/system/{board_unit.name}"]),
            launcher._quote_command(["sudo", "install", "-m", "0644", str(canary_unit), f"/etc/systemd/system/{canary_unit.name}"]),
            launcher._quote_command(
                [
                    "sudo",
                    "install",
                    "-m",
                    "0644",
                    "-o",
                    status.owner_user,
                    "-g",
                    status.owner_user,
                    str(listener_unit),
                    str(listener_target),
                ]
            ),
            # The same reviewed bytes, published where the unprivileged
            # deployer can read them. In this chain rather than a step of its
            # own so the copy cannot exist without the install having happened,
            # and cannot be stale relative to it (SYRD-127).
            *system_unit_proof_chain(project, launcher._quote_command([str(board_unit)])),
            launcher._quote_command(["sudo", "systemctl", "daemon-reload"]),
        ]
    )


def release_update_blocked(status: "TenantReleaseStatus | None") -> str:
    """Why no safe release update can be produced, or "" when one can.

    One reading, used both by the report an operator sees and by the exit status
    the caller returns. Printing `cannot produce a safe release update` and then
    exiting 0 is what let a bounded wrapper announce REAL UPGRADE COMPLETE over a
    board that had not moved (SYRD-100 review).
    """
    if status is None:
        return ""
    if not status.target_sha:
        return status.resolve_error or f"{status.deploy_ref} did not resolve"
    if not status.unchanged and status.provisioned_system_unit is None:
        return "generated board, canary, and listener units are incomplete"
    return ""


def _format_release_path(path: Path | None) -> str:
    return str(path) if path is not None else "(none)"


def report_tenant_release_upgrade(
    config: ProjectConfig,
    *,
    config_path: Path | None = None,
    source_repo: Path | None = None,
    commit_git_dir: str | None = None,
    deploy_ref: str = DEFAULT_TENANT_RELEASE_DEPLOY_REF,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> TenantReleaseStatus | None:
    """Report the tenant's release, and return what was found.

    The status is returned because the caller has to record a phase from the same
    reading: the identities transaction now switches the release itself, so whether
    a deploy is still owed is exactly `status.unchanged`, and asking twice would be
    two readings of a tree that can move between them (SYRD-48).
    """
    from scripts import team_launcher as launcher

    status = launcher.tenant_release_status(
        config,
        config_path=config_path,
        source_repo=source_repo,
        commit_git_dir=commit_git_dir,
        deploy_ref=deploy_ref,
        runner=runner,
    )
    if status is None:
        return None
    print_func(
        f"switchyard: {config.project} deployed board release old: "
        f"{launcher._format_release_sha(status.current_sha)} at {launcher._format_release_path(status.current_release)}"
    )
    if status.target_sha:
        print_func(f"switchyard: {config.project} deployed board release new: {status.target_sha} from {status.deploy_ref}")
    else:
        print_func(
            f"switchyard: {config.project} deployed board release new: "
            f"(unresolved {status.deploy_ref}: {status.resolve_error})"
        )
    blocked = launcher.release_update_blocked(status)
    if not status.target_sha:
        print_func(
            f"switchyard: cannot produce a safe release update for {config.project}: {blocked}"
        )
    elif status.unchanged:
        print_func(f"switchyard: {config.project} deployed board release unchanged; no release deploy needed")
    else:
        if status.provisioned_system_unit is None:
            print_func(
                f"switchyard: cannot produce a safe release update for {config.project}: {blocked}"
            )
            return status
        print_func("switchyard: matching-release deployment sequence (keep the listener stopped through migrations):")
        print_func(f"  {launcher.tenant_release_listener_command(status, config.project, 'stop')}")
        unit_install = launcher.tenant_release_unit_install_command(status, config.project)
        if unit_install:
            privileged_root = launcher.switchyard_privileged_provision_root()
            if status.provisioned_system_unit.is_relative_to(privileged_root):
                print_func(f"  {launcher.recorded_rollout_command(status, config.project, unit_install, label='install units')}")
            else:
                # Not printed at all, rather than printed with a warning above
                # it. This step is executed by root through the recorder, and
                # its source is a directory the tenant can write: printing it
                # asks an operator to install a unit file that anyone with the
                # tenant account could have rewritten between the render and
                # the run. The warning was already here and was not enough --
                # on SYRD-137 the step had to be recognised as unsafe and
                # skipped by hand, against a copy four days stale that would
                # have stripped the live board's socket-group confinement
                # (SYRD-138).
                print_func(
                    f"switchyard: omitting the unit-install step for {config.project}: it would "
                    f"install from {status.provisioned_system_unit.parent}, which the tenant owns, "
                    f"and the step runs as root. Run `switchyard upgrade {config.project}` as root "
                    "to stage a root-owned copy, then re-render this sequence."
                )
        print_func(
            f"  {launcher.recorded_rollout_command(status, config.project, launcher.tenant_release_deploy_command(status, config.project), label='deploy-restart')}"
        )
        print_func(f"  {launcher.tenant_release_listener_command(status, config.project, 'start')}")
        # The step that closes the phase, in the sequence, recorded like the
        # rest. It runs last because it re-proves the end state -- the listener
        # is back, the board is serving the release its own link names -- and a
        # close that ran before the listener was restored would be closing over
        # a system the deploy had not finished putting back (SYRD-117).
        print_func(
            f"  {launcher.recorded_rollout_command(status, config.project, launcher._quote_command(['switchyard', 'release-status', config.project, '--close']), label='close release')}"
        )
        # What to do when a step fails, said before anybody needs it. On mefp the
        # deploy failed with the listener already stopped, and nothing said
        # whether bringing it back was safe (SYRD-231).
        print_func(
            f"switchyard: if a step fails, stop there. `switchyard release-status {config.project}` "
            "says which release the board is serving: if it is still the one it had, the deploy "
            "stopped before activation and the listener can be brought back with "
            f"`{launcher.tenant_release_listener_command(status, config.project, 'start')}`; then run "
            f"`sudo switchyard upgrade {config.project}` to repair what stopped it and re-render "
            "this sequence."
        )
        print_func(
            f"switchyard: each recorded step prints where its record is; read them with "
            f"`switchyard rollout-log {config.project}` -- the journal is root-owned and every "
            "role may read it without sudo, so nobody has to paste output (SYRD-128)."
        )
        print_func(
            "switchyard: panes must be restarted after the release update to pick up hook installer, "
            "hook binary, or pane launcher changes; this command does not restart panes"
        )
    return status


#: Decide who runs a printed command when it is run, not when it is printed.
#:
#: `_owner_command_args` answers from `current_user_name()`, which is right for
#: a command this process is about to run itself and wrong for one that is
#: printed for somebody else to run later. An unprivileged `switchyard upgrade`
#: printed the tenant deploy with no boundary at all, because the renderer WAS
#: the tenant owner -- and the operator then ran that line through the rollout
#: recorder under sudo, so the deploy executed as root, `/run/user/$(id -u)`
#: resolved to `/run/user/0`, and the migration ran with the tenant's real
#: notification listener still up. The root-invoked renderer emitted the
#: boundary and the two disagreed about the same release (SYRD-138).
#:
#: Nothing is interpolated into this script: the owner and the command arrive as
#: positional arguments, so a tenant path containing a space, a quote or a
#: dollar sign is data rather than syntax.
OWNER_BOUNDARY_SCRIPT = (
    'target=$1; shift; '
    'if [ "$(id -un)" = "$target" ]; then exec "$@"; fi; '
    'exec sudo -u "$target" "$@"'
)


def _owner_boundary_env_args(owner_user: str, owner_home: Path, command: Sequence[str]) -> list[str]:
    """The same command, guaranteed to run as `owner_user` whoever starts it."""
    from scripts import team_launcher as launcher

    path = launcher._prepend_paths(launcher.DEFAULT_PANE_BASE_PATH, launcher._owner_home_bin_dirs(owner_home))
    return [
        "sh",
        "-c",
        launcher.OWNER_BOUNDARY_SCRIPT,
        "sh",
        owner_user,
        "env",
        f"HOME={owner_home}",
        f"PATH={path}",
        *command,
    ]


def capture_release_pointer(config: ProjectConfig, *, config_path: Path | None = None) -> tuple[Path | None, str]:
    """Where the board release symlink points now, so it can be put back."""
    from scripts import team_launcher as launcher

    board_root = launcher._tenant_board_root_from_config_or_plan(config, config_path)
    if board_root is None:
        return None, ""
    current = board_root / "current"
    try:
        return current, os.readlink(current)
    except OSError:
        return current, ""


def deploy_release_in_transaction(
    config: ProjectConfig,
    *,
    config_path: Path,
    source_repo: Path,
    commit_git_dir: str | None,
    deploy_ref: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    print_func: Callable[[str], None] = print,
) -> tuple[list[str], bool]:
    """Switch the board to the release that enforces the per-role table.

    The binary is part of the same change as the units, the workers and the
    authority table: an old board does not group-own its runtime directory for
    the roles group, so restarting it against strict units can leave the role
    accounts unable to reach the socket at all. Switching it here means the
    rollback can put it back (SYRD-45).

    Returns its problems and whether it restarted the board. `deploy-restart`
    does restart it, having smoke-checked the service, verified the live build
    id and checked the post-deploy runtime; a deploy with nothing to do restarts
    nothing, and the caller still has to make the installed authority the one
    being served (SYRD-63).
    """
    from scripts import team_launcher as launcher

    status = launcher.tenant_release_status(
        config,
        config_path=config_path,
        source_repo=source_repo,
        commit_git_dir=commit_git_dir,
        deploy_ref=deploy_ref,
        runner=runner,
    )
    if status is None:
        # Not every project serves its board from a tenant release; there is
        # nothing to switch, and nothing to roll back either.
        return [], False
    if not status.target_sha:
        return [f"the release to deploy could not be resolved: {status.resolve_error}"], False
    if status.unchanged:
        return [], False
    command = launcher.tenant_release_deploy_command(status, config.project)
    if not command:
        return ["no deploy command could be built for this release"], False
    result = runner(["sh", "-c", command], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        return [
            f"deploying {status.target_sha} failed (exit {result.returncode}): "
            f"{(str(result.stderr).strip() or 'no output')[:400]}"
        ], False
    print_func(f"switchyard: {config.project} board release deployed: {status.target_sha}")
    return [], True


def restore_release_pointer(
    pointer: Path | None,
    target: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> list[str]:
    """Put the release symlink back where the transaction found it."""
    if pointer is None or not target:
        return []
    try:
        if os.path.islink(pointer) and os.readlink(pointer) == target:
            return []
        staged = pointer.with_name(f".{pointer.name}.rollback")
        if staged.exists() or os.path.islink(staged):
            staged.unlink()
        os.symlink(target, staged)
        os.replace(staged, pointer)
    except OSError as exc:
        return [f"could not restore the release pointer {pointer}: {exc}"]
    return []
