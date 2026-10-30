"""A tenant's board deployed to the exact release it was prepared for, and its phase closed.

- `switchyard_deploy_release_command` is `switchyard deploy-release <project>
  --commit <sha>`, root only: the catalogued `deploy-release` action's command.
- `_build_switchyard_deploy_release_parser` is its parser.

An upgrade prepares a tenant -- artifacts, role tooling, root's pin -- and
leaves the board deploy owed: it printed the operator's sequence (stop the
listener, install the units, deploy-restart, start the listener, close the
phase) and exited 0. The Director's catalogued `deploy-release` mapped to the
same preparation, so on MEFP a pinned preview and apply both succeeded while
the board stayed on 49abeb4 and the phase stayed `ready` (SYRD-531). This runs
that sequence, in that order, as the helper's root: only for a tenant already
prepared for exactly this commit, from root's installed release of it, and it
closes the phase only by the same re-proof `release-status --close` makes.

The close reads the running board, so it waits until the board answers. A
board's unit is `Type=simple`: `systemctl start` returns when its process has
forked, not when it serves. On MEFP, suspended and already on its release, the
close ran in that gap -- connection refused, no build, no workflow -- and the
phase was recorded blocked; the board was then put back down 1.17 s after it
was started. The operator's recovery waited for `/api/board` first and closed
the same release cleanly (SYRD-632).
"""

from __future__ import annotations

import argparse
import os
import socket
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable


#: What `ticket-board-service.sh deploy-restart` exits with when a health gate
#: failed after the restart and its rollback restored the previous release; the
#: mark `deploy_release_in_transaction` puts on that problem; and what
#: `deploy-release` then exits with itself (SYRD-113).
ROLLED_BACK_EXIT = 3
ROLLED_BACK = "rolled back: "
#: How long the board has to answer before the close reads it, and how often it is asked (SYRD-632).
BOARD_READY_SECONDS = 30.0
BOARD_READY_POLL_SECONDS = 0.5
SYSTEM_UNIT_DIR = Path("/etc/systemd/system")
TRUSTED_UNIT_OWNER_UID = 0


#: The file a release must carry to run under root's exact-release units: their renderer, which arrived with the
#: board's `--report-credentials-file` (SYRD-699). An older release has neither (SYRD-700).
EXACT_RELEASE_RUNTIME = "scripts/ticket_board/release_units.py"


def exact_release_runtime(release: Path) -> bool:
    """Whether root's ``release`` can run its board under an exact-release unit."""
    return (release / EXACT_RELEASE_RUNTIME).is_file()


def _installed_unit_bytes(path: Path) -> bytes | None:
    """Read one root unit without following a replacement link."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError(f"cannot capture {path} without following links: {exc}") from None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != TRUSTED_UNIT_OWNER_UID
                or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
            raise RuntimeError(f"{path} is not a root-owned, non-writable regular unit")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return handle.read(1 << 20)
    finally:
        os.close(fd)


def _free_canary_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _install_units(
    units: dict[Path, bytes], *, runner: Callable[..., subprocess.CompletedProcess[Any]]
) -> list[str]:
    """Install already-rendered unit bytes and reload, as this root command."""
    with tempfile.TemporaryDirectory(prefix="switchyard-release-units.") as temporary:
        for target, payload in units.items():
            staged = Path(temporary) / target.name
            staged.write_bytes(payload)
            done = runner(["install", "-o", "root", "-g", "root", "-m", "0644", str(staged), str(target)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if done.returncode != 0:
                return [f"installing {target} failed (exit {done.returncode}): "
                        f"{(str(done.stderr or '').strip() or 'no output')[:300]}"]
    reloaded = runner(["systemctl", "daemon-reload"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if reloaded.returncode != 0:
        return [f"systemctl daemon-reload failed (exit {reloaded.returncode}): "
                f"{(str(reloaded.stderr or '').strip() or 'no output')[:300]}"]
    return []


def _candidate_units(status: Any, project: str, release: Path, port: int) -> dict[Path, bytes]:
    """Render board and canary from root's staged provisioning, fixed to ``release``."""
    from scripts.ticket_board import release_units

    board_source = status.provisioned_system_unit
    canary_source = board_source.parent / f"{project}-ticket-board-canary.service"
    try:
        board_template = board_source.read_text(encoding="utf-8")
        canary_template = canary_source.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"cannot read root's staged board units: {exc}") from None
    credentials = release_units.report_credentials_path(board_template)
    board = release_units.render_board_unit(board_template, release=release, report_credentials=credentials)
    canary = release_units.render_canary_unit(board_template, canary_template, release=release, port=port)
    return {
        SYSTEM_UNIT_DIR / board_source.name: board.encode("utf-8"),
        SYSTEM_UNIT_DIR / canary_source.name: canary.encode("utf-8"),
    }


def exact_authority_units(
    config: Any,
    *,
    config_path: Path | None,
    source_repo: Path,
    commit_git_dir: str | None,
    deploy_ref: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> tuple[list[str], dict[str, bytes], int | None]:
    """The board, canary and readable copy rendered for the release ``deploy_ref`` resolves to, and the canary port.

    For the identity cutover, which installs the board's units before it deploys: they are root's exact renders of
    that release from the staged templates, never the templates themselves (SYRD-700). Keyed as
    ``board_services.authority_unit_installs`` names them. Problems, and nothing rendered, when root holds no
    trusted release of that commit that can run under them, or the templates are not root's.
    """
    from scripts import team_launcher as launcher
    from scripts.ticket_board import privileged_operations

    status = launcher.tenant_release_status(config, config_path=config_path, source_repo=source_repo,
                                            commit_git_dir=commit_git_dir, deploy_ref=deploy_ref, runner=runner)
    commit = status.target_sha if status is not None else ""
    if not commit:
        # A tenant with no board release of its own runs the release this cutover runs from, when that is root's.
        held = launcher._read_switchyard_release_marker(source_repo)
        commit = held.marker_commit if held is not None else ""
    if not commit:
        return [f"{config.project}'s release does not resolve, so its exact board units cannot be rendered"], {}, None
    try:
        release = Path(privileged_operations.trusted_release_root(commit))
    except privileged_operations.NoTrustedSource as exc:
        return [f"{config.project}'s board units are rendered from root's release of {commit}: {exc}"], {}, None
    if not exact_release_runtime(release):
        return [f"root's release {commit} predates exact root-release board units (SYRD-699)"], {}, None
    staged = launcher.privileged_provision_dir(config.project, root=launcher.switchyard_privileged_provision_root())
    template = status.provisioned_system_unit if status is not None else None
    template = template or staged / f"{config.project}-ticket-board.service"
    if not template.is_relative_to(launcher.switchyard_privileged_provision_root()):
        return [f"{config.project}'s board units would be rendered from a template root does not own"], {}, None
    port = _free_canary_port()
    try:
        rendered = _candidate_units(SimpleNamespace(provisioned_system_unit=template), config.project, release, port)
    except (RuntimeError, ValueError) as exc:
        return [f"rendering {config.project}'s exact board units failed: {exc}"], {}, None
    board_name = template.name
    board, canary = (next(v for k, v in rendered.items() if k.name == name)
                     for name in (board_name, f"{config.project}-ticket-board-canary.service"))
    return [], {board_name: board, f"{config.project}-ticket-board-canary.service": canary,
                f"{board_name} (readable copy)": board}, port


def install_deploy_and_attest(
    config: Any,
    *,
    config_path: Path | None,
    source_repo: Path,
    commit_git_dir: str | None,
    deploy_ref: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    print_func: Callable[[str], None] = print,
) -> list[str]:
    """The identity cutover's board half: root's exact units installed before the deploy, then the deploy, the
    restart under them, and the board attested; the problem that stopped it otherwise, for the cutover's rollback.

    Installed before the deploy because `deploy-restart` compares the installed unit with the one it is handed and
    the cutover exists to change that unit (SYRD-63); rendered for the release, because the staged ones are
    templates that run the tenant's own tree as the board's account (SYRD-700).
    """
    from scripts import team_launcher as launcher

    problems, exact, canary_port = exact_authority_units(
        config, config_path=config_path, source_repo=source_repo, commit_git_dir=commit_git_dir,
        deploy_ref=deploy_ref, runner=runner,
    )
    if not problems:
        problems = launcher.install_board_authority_files(config, runner=runner, config_path=config_path,
                                                          print_func=print_func, exact=exact)
    restarted = False
    if not problems:
        from scripts.ticket_board import database_admin

        try:
            database: str | None = database_admin.database_from_unit(
                exact.get(f"{config.project}-ticket-board.service", b"").decode("utf-8"))
        except database_admin.DatabaseAdminError as exc:
            problems, database = [f"root's board unit names no database to migrate: {exc}"], None
    if not problems:
        problems, restarted = launcher.deploy_release_in_transaction(
            config, config_path=config_path, source_repo=source_repo, commit_git_dir=commit_git_dir,
            deploy_ref=deploy_ref, runner=runner, print_func=print_func, canary_port=canary_port, database=database,
        )
    if not problems:
        problems = launcher.activate_board_authority(config, runner=runner, restart=not restarted,
                                                     print_func=print_func)
    return problems or attestation_problems(config.project, runner=runner)


def board_unit_for_plan(plan: Any, template: str) -> tuple[str, str]:
    """The board unit `add-role` or `set-vcs-close-role` installs for ``plan``, and the one it replaces.

    Under root's exact-release unit, root's staged templates are refreshed for the plan, so the next deploy-release
    renders the change rather than dropping it, and the unit installed is the exact render of the refreshed template
    for the release that unit runs. Under a legacy one it is the template, as before; that tenant's repair stages
    its templates (SYRD-701, SYRD-702).
    """
    from scripts import team_launcher as launcher
    from scripts.ticket_board import release_units

    board_unit = getattr(plan, "board_unit", "")
    try:
        installed = (release_units.SYSTEM_UNIT_DIR / board_unit).read_text(encoding="utf-8") if board_unit else ""
    except OSError:
        return template, ""
    release = release_units.installed_release(installed)
    if release is None:
        return template, installed
    launcher.install_privileged_artifacts(plan, launcher.render_privileged_artifacts(plan))
    return release_units.render_board_unit(template, release=release,
                                           report_credentials=release_units.report_credentials_path(template)), installed


def install_exact_or_restore(
    plan: Any, *, installing: str, previous: str, runner: Callable[..., Any], install: Callable[..., None]
) -> list[str]:
    """Root's exact unit installed, reloaded, restarted and attested as one transaction, or the previous unit back.

    `install` is the role commands' `_install_and_restart_board_unit`, which raises SystemExit when the install, the
    reload or the restart fails -- by then the replacement may already be in place and the board stopped. Any of
    those, or a board that does not attest, puts ``previous`` back the same way; a restore that fails too is said,
    not swallowed (SYRD-702). Nothing when it all held.
    """
    try:
        install(plan, board_unit=installing, runner=runner)
        problems = attestation_problems(plan.project, runner=runner)
    except SystemExit as exc:
        problems = [str(exc) or f"installing root's exact {plan.board_unit} failed"]
    if not problems:
        return []
    try:
        install(plan, board_unit=previous, runner=runner)
    except SystemExit as exc:
        return [*problems, f"and putting the previous {plan.board_unit} back failed too: {exc}; it needs an operator"]
    return [*problems, "the previous board unit was put back"]


def attestation_problems(project: str, *, runner: Callable[..., Any]) -> list[str]:
    """Why the project's running board is not root's exact release under its effective unit, or none."""
    from scripts.ticket_board import board_identity

    try:
        board_identity.effective_unit(project, runner=runner)
    except board_identity.BoardIdentityError as exc:
        return [f"the board did not attest its exact root release: {exc}"]
    return []


def _installed_canary_port(payload: bytes | None) -> int | None:
    """The port an installed canary unit's ExecStart names, or None if it names none (a legacy canary)."""
    import shlex

    for line in (payload or b"").decode("utf-8", "replace").splitlines():
        if line.startswith("ExecStart="):
            try:
                argv = shlex.split(line[len("ExecStart="):])
            except ValueError:
                return None
            ports = [argv[i + 1] for i, word in enumerate(argv[:-1]) if word == "--port"]
            return int(ports[0]) if len(ports) == 1 and ports[0].isdigit() else None
    return None


def _serves_exact_units(status: Any, project: str, release: Path) -> bool:
    """Whether the installed board and canary units are byte for byte root's exact renders of ``release``.

    The canary is compared at the port its installed unit names, which root chose when it rendered it; a canary
    that names none is a legacy one. Either unit not root's render of this release, one that cannot be read, or
    no staged template to render from, is not proof: the caller converges both, which refuses cleanly if it
    cannot render them (SYRD-700).
    """
    if status.provisioned_system_unit is None:
        return False
    board_target = SYSTEM_UNIT_DIR / status.provisioned_system_unit.name
    canary_target = board_target.with_name(f"{project}-ticket-board-canary.service")
    try:
        installed_board = _installed_unit_bytes(board_target)
        installed_canary = _installed_unit_bytes(canary_target)
        port = _installed_canary_port(installed_canary)
        if port is None:
            return False
        exact = _candidate_units(status, project, release, port)
    except (RuntimeError, ValueError):
        return False
    return installed_board == exact[board_target] and installed_canary == exact[canary_target]


def _restore_units(units: dict[Path, bytes], board_unit: str, *, runner: Callable[..., Any]) -> list[str]:
    restored = _install_units(units, runner=runner)
    if restored:
        return restored
    restarted = runner(["systemctl", "restart", board_unit], stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, text=True)
    if restarted.returncode != 0:
        return [f"restoring {board_unit} failed to restart it (exit {restarted.returncode}): "
                f"{(str(restarted.stderr or '').strip() or 'no output')[:300]}"]
    return []


def _await_board(config: Any, *, opener: Callable[[str], Any] | None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> str:
    """Wait until the running board reports a build: '' once it does, else the last reason it could not be read."""
    from scripts import team_launcher as launcher

    deadline = clock() + BOARD_READY_SECONDS
    while True:
        build, why = launcher._live_board_build_id(config, opener=opener)
        if build:
            return ""
        if clock() >= deadline:
            return why
        sleep(BOARD_READY_POLL_SECONDS)


def _build_switchyard_deploy_release_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard deploy-release",
        description=(
            "Deploy a tenant's board to the exact release its upgrade was prepared for, as root, "
            "and close the release phase only after re-proving from the running board that it "
            "serves that release. Stops the notify listener before the deploy (migrations run "
            "inside it) and starts it after; restarts no panes. Refuses, deploying nothing, "
            "unless root's pin for the tenant is this commit and root holds that release."
        ),
    )
    parser.add_argument("project", help="registered project name or slug")
    parser.add_argument("--commit", required=True, help="the full 40-character commit the tenant was prepared for")
    return parser


def switchyard_deploy_release_command(
    project: str,
    *,
    commit: str,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    opener: Callable[[str], Any] | None = None,
    print_func: Callable[[str], None] = print,
) -> int:
    """Deploy the prepared release and close the phase, or say exactly where it stopped."""
    from scripts import team_launcher as launcher

    commit = (commit or "").strip()
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        print_func(f"switchyard: deploy-release takes a full 40-character commit, not {commit!r}. Nothing was deployed.")
        return 1
    if os.geteuid() != 0:
        print_func(
            f"switchyard: deploying {project}'s board is root's, through the approved boundary: "
            f"`switchyard privileged-action {project} deploy-release commit={commit}`. Nothing was deployed."
        )
        return 1
    entry = launcher._resolve_switchyard_project(project, config_dir=config_dir, registry_dir=registry_dir)
    config = launcher.load_project_config(entry.slug, entry.config_path)
    config_path = entry.config_path

    def closing(line: str) -> None:
        # `release-status --close` speaks as a command that deploys nothing; here
        # it closes over a deploy this command just made, so its hint to run it
        # and its "nothing was deployed" would both be untrue.
        if "Close it with `pkexec switchyard release-status" in line:
            return
        print_func(line.replace("Nothing was deployed, restarted or rolled back.",
                                "The close itself changed nothing else."))

    def refuse(reason: str) -> int:
        print_func(f"switchyard: {reason}")
        print_func(f"switchyard: nothing of {config.project}'s was deployed, stopped or recorded.")
        return 1

    # Prepared for exactly this commit, from root's installed release of it:
    # the pin `upgrade-tenant-release` records. Anything else would deploy a
    # release whose artifacts and tooling were staged for another one.
    pinned = launcher.read_upgrade_source(config)
    if pinned.get("deploy_ref") != commit:
        return refuse(
            f"{config.project} was prepared for {pinned.get('deploy_ref') or 'no recorded release'}, not "
            f"{commit}: prepare it first with `switchyard privileged-action {config.project} preview-upgrade "
            f"commit={commit}` and then `upgrade-tenant-release commit={commit}`"
        )
    source = Path(pinned["source_repo"]) if pinned.get("source_repo") else None
    held = launcher._read_switchyard_release_marker(source) if source is not None else None
    if held is None or held.marker_commit != commit:
        return refuse(f"root holds no installed release of {commit} as {config.project}'s pinned source ({source})")
    from scripts.ticket_board import privileged_operations
    try:
        trusted_source = Path(privileged_operations.trusted_release_root(commit))
    except privileged_operations.NoTrustedSource as exc:
        return refuse(str(exc))
    if source != trusted_source:
        return refuse(f"{config.project}'s pinned source is {source}, not root's trusted release {trusted_source}")
    if not exact_release_runtime(trusted_source):
        return refuse(
            f"root's release {commit} predates exact root-release board units (SYRD-699): its board cannot run "
            "under them, and its own units would run tenant-selected code. Deploy a release that carries "
            f"{EXACT_RELEASE_RUNTIME} instead"
        )
    status = launcher.tenant_release_status(
        config, config_path=config_path, source_repo=source,
        commit_git_dir=pinned.get("commit_git_dir") or None, deploy_ref=commit, runner=runner,
    )
    if status is None:
        return refuse(f"{config.project} serves no tenant board release to deploy")
    if status.target_sha != commit:
        return refuse(
            f"{config.project}'s release resolves to {status.target_sha or 'nothing'}, not {commit}"
            + (f": {status.resolve_error}" if status.resolve_error else "")
        )
    # Where the tenant was before anything here touched it, so it ends the same
    # way: a suspended tenant stays suspended and a running one keeps running.
    # Deploying is not starting -- on MEFP the operator started the whole tenant
    # only so the close could reach a board, and its pull queue began assigning
    # work the user wanted held (SYRD-577). Panes and windows are never touched.
    board_was_active = launcher.board_system_unit_is_active(config, runner=runner)
    listener_was = launcher.capture_listener_state(config, runner=runner, config_path=config_path)
    listener_was_active = listener_was == "active"
    if board_was_active and listener_was_active:
        began = "active (its board and listener were running)"
    elif not board_was_active and not listener_was_active:
        began = f"suspended (its board was not running, and its listener was {listener_was or 'not running'})"
    else:
        began = f"partly running (board {'active' if board_was_active else 'inactive'}, listener {listener_was})"
    print_func(
        f"switchyard: {config.project} began {began}; this deploy leaves it that way and starts no pane, "
        "window or notification delivery."
    )

    def board_back() -> list[str]:
        """Put a board this command started back down, and say if it would not go."""
        if board_was_active:
            return []
        if not launcher.board_system_unit_is_active(config, runner=runner):
            return []
        return launcher._board_system_unit_action(config, "stop", runner=runner)

    def close() -> int:
        """The close, once the board answers: it re-proves the release from what the running board reports."""
        unanswered = _await_board(config, opener=opener)
        if unanswered:
            print_func(f"switchyard: {config.project}'s board did not answer within {BOARD_READY_SECONDS:g}s "
                       f"({unanswered}); the close below records what it could read.")
        return launcher.close_release_phase(config, config_path=config_path, opener=opener, print_func=closing)

    def ended(code: int, leftovers: list[str]) -> int:
        """The lifecycle report every exit after this point ends with."""
        if leftovers:
            for problem in leftovers:
                print_func(f"switchyard: not restored: {problem}")
            print_func(
                f"switchyard: {config.project} did NOT end as it began ({began}); the components above "
                "are not in their prior state and need an operator."
            )
            return code or 1
        print_func(f"switchyard: {config.project} ends as it began: {began}.")
        return code

    converging = status.unchanged and not _serves_exact_units(status, config.project, source)
    if status.unchanged and not converging:
        print_func(f"switchyard: {config.project}'s board already serves {commit}; closing the phase against it.")
        if not board_was_active:
            # The close re-proves the release against what the running board
            # reports, so a stopped board has to answer for that long -- with the
            # listener still down and no pane up, nothing can assign work in the
            # meantime -- and is then put back down.
            print_func(
                f"switchyard: starting {config.project}'s board only for the close; it is stopped again afterwards."
            )
            started_board = launcher._board_system_unit_action(config, "start", runner=runner)
            if started_board:
                return ended(refuse(f"the board could not be started for the close: {'; '.join(started_board)}"),
                             board_back())
        from scripts.ticket_board import board_identity
        try:
            board_identity.effective_unit(config.project, runner=runner)
        except board_identity.BoardIdentityError as exc:
            return ended(refuse(f"{config.project}'s board does not attest its exact root release: {exc}"),
                         board_back())
        code = close()
        return ended(code, board_back())
    if converging:
        # The tenant's `current` already names the commit, but the unit is not root's exact render of it -- a
        # legacy unit, or another release's. Nothing is migrated: the release is unchanged. The unit is
        # replaced, the board restarted under it and attested, or the previous unit is put back (SYRD-700).
        print_func(f"switchyard: {config.project} already names {commit}, but its board unit is not root's exact "
                   "render of it; installing that and restarting the board under it.")
    if status.provisioned_system_unit is None:
        return refuse(launcher.release_update_blocked(status) or f"{config.project} has no provisioned board units")
    if not status.provisioned_system_unit.is_relative_to(launcher.switchyard_privileged_provision_root()):
        return refuse(
            f"{config.project}'s units would be installed from {status.provisioned_system_unit.parent}, which "
            f"the tenant owns; run `switchyard upgrade {config.project}` as root to stage root's copy first"
        )

    def blocked(reason: str, listener: str, code: int = 1) -> int:
        print_func(f"switchyard: {reason}")
        print_func(f"switchyard: {listener}")
        launcher.record_upgrade_phase(config, config_path=config_path, phase="release", state="blocked", detail=reason)
        print_func(
            f"switchyard: recorded {config.project}'s release phase blocked with that reason; "
            f"`switchyard release-status {config.project}` shows where the board is."
        )
        return code

    def listener_back() -> str:
        if not listener_was_active:
            return f"the notify listener stays {listener_was or 'inactive'}, as it was"
        problems = launcher.start_owner_listener(config, runner=runner, config_path=config_path)
        return "; ".join(problems) if problems else "the notify listener is running again"

    before = status.current_sha
    # The order the printed sequence always had: the listener comes down before
    # anything can migrate the schema it reads (SYRD-45), the units are root's
    # reviewed copy, the deploy is the tenant's own deploy-restart -- with its
    # health gates, live-build check and rollback -- and the listener comes back
    # only after it.
    stopped = launcher.stop_owner_listener(config, runner=runner, config_path=config_path)
    if stopped:
        return ended(blocked("; ".join(stopped), "nothing was deployed; the listener's state is as that says"),
                     stopped if listener_was_active else [])
    board_target = SYSTEM_UNIT_DIR / status.provisioned_system_unit.name
    canary_target = board_target.with_name(f"{config.project}-ticket-board-canary.service")
    try:
        previous_units = {
            board_target: _installed_unit_bytes(board_target),
            canary_target: _installed_unit_bytes(canary_target),
        }
    except RuntimeError as exc:
        listener_said = listener_back()
        return ended(blocked(str(exc), f"nothing was deployed; {listener_said}"),
                     _listener_leftover(listener_said) + board_back())
    if any(payload is None for payload in previous_units.values()):
        listener_said = listener_back()
        missing = ", ".join(str(path) for path, payload in previous_units.items() if payload is None)
        return ended(blocked(f"cannot capture the rollback units ({missing} is absent)",
                             f"nothing was deployed; {listener_said}"),
                     _listener_leftover(listener_said) + board_back())

    def restore_units() -> list[str]:
        return _restore_units({path: payload for path, payload in previous_units.items() if payload is not None},
                              board_target.name, runner=runner)

    canary_port = _free_canary_port()
    try:
        candidate_units = _candidate_units(status, config.project, source, canary_port)
    except (RuntimeError, ValueError) as exc:
        listener_said = listener_back()
        return ended(blocked(f"rendering exact-release board units failed: {exc}",
                             f"nothing was deployed; {listener_said}"),
                     _listener_leftover(listener_said) + board_back())
    # The rest of root's reviewed provisioning -- the listener, and the readable copy the tenant's gate compares
    # with, now of the exact board -- but never the staged board and canary themselves: those are templates,
    # so no legacy board or tenant-configured canary is installed at any moment of the deploy (SYRD-700).
    with tempfile.TemporaryDirectory(prefix=f"switchyard-{config.project}-exact.") as exact_dir:
        exact_board = Path(exact_dir) / board_target.name
        exact_board.write_bytes(candidate_units[board_target])
        units = runner(["sh", "-c", launcher.tenant_release_unit_install_command(status, config.project,
                                                                                 exact_board=exact_board)],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if units.returncode != 0:
        unit_leftovers = restore_units()
        listener_said = listener_back()
        return ended(blocked(
            f"installing {config.project}'s board units failed (exit {units.returncode}): "
            f"{(str(units.stderr or '').strip() or 'no output')[:300]}",
            f"nothing was deployed; {listener_said}",
        ), unit_leftovers + _listener_leftover(listener_said) + board_back())
    installed = _install_units(candidate_units, runner=runner)
    if installed:
        unit_leftovers = restore_units()
        listener_said = listener_back()
        return ended(blocked("; ".join(installed), f"nothing was deployed; {listener_said}"),
                     unit_leftovers + _listener_leftover(listener_said) + board_back())
    # The tenant's own deploy-restart restarts the board -- and so starts one
    # that was stopped -- because its health gates and live-build check need a
    # serving board. A board that was stopped is put back down after the close.
    # The tenant deploy's system-unit gate compares against these exact bytes,
    # not the old provisioning template. The directory is searchable by the
    # tenant for the duration of this call; the root-owned file is read-only
    # and disappears before deploy-release returns.
    if converging:
        restart = runner(["systemctl", "restart", board_target.name], stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
        unanswered = (f"restarting {board_target.name} failed (exit {restart.returncode}): "
                      f"{(str(restart.stderr or '').strip() or 'no output')[:300]}" if restart.returncode != 0
                      else _await_board(config, opener=opener))
        if unanswered:
            unit_leftovers = restore_units()
            listener_said = listener_back()
            return ended(blocked(f"the board did not serve under its exact unit ({unanswered}); the previous unit "
                                 "was put back", f"the board still names {commit}; {listener_said}",
                                 ROLLED_BACK_EXIT),
                         unit_leftovers + _listener_leftover(listener_said) + board_back())
        problems: list[str] = []
    else:
        # The database, from the unit root rendered: the deploy's cluster-superuser work is root's (SYRD-706).
        from scripts.ticket_board import database_admin

        try:
            database = database_admin.database_from_unit(candidate_units[board_target].decode("utf-8"))
        except database_admin.DatabaseAdminError as exc:
            unit_leftovers = restore_units()
            listener_said = listener_back()
            return ended(blocked(f"root's board unit names no database to migrate: {exc}",
                                 f"nothing was deployed; {listener_said}"),
                         unit_leftovers + _listener_leftover(listener_said) + board_back())
        with tempfile.TemporaryDirectory(prefix=f"switchyard-{config.project}-candidate.") as proof_dir:
            os.chmod(proof_dir, 0o755)
            candidate_proof = Path(proof_dir) / board_target.name
            candidate_proof.write_bytes(candidate_units[board_target])
            candidate_proof.chmod(0o644)
            problems, _restarted = launcher.deploy_release_in_transaction(
                config, config_path=config_path, source_repo=source,
                commit_git_dir=pinned.get("commit_git_dir") or None,
                deploy_ref=commit, runner=runner, print_func=print_func,
                canary_port=canary_port, provisioned_system_unit=candidate_proof, database=database,
            )
    if problems:
        now = launcher._current_tenant_release(status.board_root)[1]
        if now == before:
            # SYRD-113: deployed, failed its health gate and rolled back is its own outcome.
            rolled_back = any(problem.startswith(ROLLED_BACK) for problem in problems)
            unit_leftovers = restore_units()
            listener_said = listener_back()
            return ended(
                blocked("; ".join(problems), f"the board still names {before or 'no release'}; {listener_said}",
                        ROLLED_BACK_EXIT if rolled_back else 1),
                unit_leftovers + _listener_leftover(listener_said) + board_back(),
            )
        unit_leftovers = restore_units()
        return ended(blocked(
            "; ".join(problems),
            f"the board now names {now or 'no release'}, not the {before or 'no release'} it had, so the "
            "listener was left stopped: bring it back only once release-status shows a board it can read",
        ), unit_leftovers
           + (["the notify listener was running before and was left stopped"] if listener_was_active else [])
           + board_back())
    from scripts.ticket_board import board_identity
    try:
        board_identity.effective_unit(config.project, runner=runner)
    except board_identity.BoardIdentityError as exc:
        pointer_problems = launcher.restore_release_pointer(
            status.board_root / "current", str(status.current_release or ""), runner=runner,
            owner_user=status.owner_user,
        )
        unit_leftovers = restore_units()
        listener_said = listener_back()
        return ended(
            blocked(f"the deployed board did not attest its exact root release: {exc}",
                    f"the previous unit and release selector were restored; {listener_said}",
                    ROLLED_BACK_EXIT),
            unit_leftovers + pointer_problems + _listener_leftover(listener_said) + board_back(),
        )
    if listener_was_active:
        started = launcher.start_owner_listener(config, runner=runner, config_path=config_path)
        if started:
            return ended(
                blocked("; ".join(started), f"the board was deployed at {commit} but its listener is not running"),
                started + board_back(),
            )
    print_func(
        f"switchyard: {config.project}'s board was deployed at {commit}; closing the phase only against what "
        "the running board reports. Panes are not restarted."
    )
    code = close()
    return ended(code, board_back())


def _listener_leftover(said: str) -> list[str]:
    """A listener that was running and did not come back, as a lifecycle leftover."""
    benign = ("the notify listener is running again", "the notify listener stays")
    return [] if said.startswith(benign) else [f"the notify listener: {said}"]
