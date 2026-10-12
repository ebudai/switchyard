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
"""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path
from typing import Any, Callable


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

    if status.unchanged:
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
        code = launcher.close_release_phase(config, config_path=config_path, opener=opener, print_func=closing)
        return ended(code, board_back())
    if status.provisioned_system_unit is None:
        return refuse(launcher.release_update_blocked(status) or f"{config.project} has no provisioned board units")
    if not status.provisioned_system_unit.is_relative_to(launcher.switchyard_privileged_provision_root()):
        return refuse(
            f"{config.project}'s units would be installed from {status.provisioned_system_unit.parent}, which "
            f"the tenant owns; run `switchyard upgrade {config.project}` as root to stage root's copy first"
        )

    def blocked(reason: str, listener: str) -> int:
        print_func(f"switchyard: {reason}")
        print_func(f"switchyard: {listener}")
        launcher.record_upgrade_phase(config, config_path=config_path, phase="release", state="blocked", detail=reason)
        print_func(
            f"switchyard: recorded {config.project}'s release phase blocked with that reason; "
            f"`switchyard release-status {config.project}` shows where the board is."
        )
        return 1

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
    units = runner(["sh", "-c", launcher.tenant_release_unit_install_command(status, config.project)],
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if units.returncode != 0:
        listener_said = listener_back()
        return ended(blocked(
            f"installing {config.project}'s board units failed (exit {units.returncode}): "
            f"{(str(units.stderr or '').strip() or 'no output')[:300]}",
            f"nothing was deployed; {listener_said}",
        ), _listener_leftover(listener_said) + board_back())
    # The tenant's own deploy-restart restarts the board -- and so starts one
    # that was stopped -- because its health gates and live-build check need a
    # serving board. A board that was stopped is put back down after the close.
    problems, _restarted = launcher.deploy_release_in_transaction(
        config, config_path=config_path, source_repo=source, commit_git_dir=pinned.get("commit_git_dir") or None,
        deploy_ref=commit, runner=runner, print_func=print_func,
    )
    if problems:
        now = launcher._current_tenant_release(status.board_root)[1]
        if now == before:
            listener_said = listener_back()
            return ended(
                blocked("; ".join(problems), f"the board still names {before or 'no release'}; {listener_said}"),
                _listener_leftover(listener_said) + board_back(),
            )
        return ended(blocked(
            "; ".join(problems),
            f"the board now names {now or 'no release'}, not the {before or 'no release'} it had, so the "
            "listener was left stopped: bring it back only once release-status shows a board it can read",
        ), (["the notify listener was running before and was left stopped"] if listener_was_active else [])
           + board_back())
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
    code = launcher.close_release_phase(config, config_path=config_path, opener=opener, print_func=closing)
    return ended(code, board_back())


def _listener_leftover(said: str) -> list[str]:
    """A listener that was running and did not come back, as a lifecycle leftover."""
    benign = ("the notify listener is running again", "the notify listener stays")
    return [] if said.startswith(benign) else [f"the notify listener: {said}"]
