"""Bringing a generated project's config and layout template up to what this release generates.

- `upgrade_generated_project_config` leaves a hand-maintained layout, or one
  outside a provision directory, alone. For a generated one it works out what
  is stale -- each role's pinned `directorctl`, the pane launcher, a session
  directory still under the user runtime (`_generated_project_durable_session_dir`
  names the durable one) and the layout template itself -- and upgrades only
  what it may: a layout template only from a known generated shape, never an
  unknown one; the configuration only after reading it without following a
  link, then written atomically and handed back to its owner. A dry run says
  what would change and writes neither the layout template nor the
  configuration, nor hands either to its owner. A stale `directorctl` pin was
  still written by a dry run until SYRD-418; it is still read without
  following a link, so a dry run refuses what the upgrade would refuse.
- `upgrade_generated_project_layout` is the public entry point `launch_project`
  and the upgrade phases call.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-417), in their original
order. The launcher imports this module and re-exports all three names;
`launch_project` and `scripts/upgrade_phases.py` still reach them through the
launcher. Everything the three read -- each other included, and the layout
payloads and template check, the board root, the session directories, the
shared pane launcher, the no-follow read, the atomic writer, the owner
normalization and the result type -- is read through the launcher at call
time, so a suite that rebinds one there still intercepts it. The default
Python binds at definition (`subprocess.run`) comes from the standard library;
the annotation-only types are imported under TYPE_CHECKING. This module
imports `team_launcher` only inside the functions, when they run.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import LauncherUpgradeResult, ProjectConfig


def _generated_project_durable_session_dir(config: ProjectConfig) -> Path | None:
    from scripts import team_launcher as launcher

    if not config.run_as_user:
        return None
    return Path(launcher._new_project_session_dir(config.project, config.run_as_user))


def upgrade_generated_project_config(
    config: ProjectConfig,
    *,
    config_path: Path,
    dry_run: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    document: dict | None = None,
    written: dict | None = None,
) -> LauncherUpgradeResult:
    from scripts import team_launcher as launcher

    if not launcher._is_generated_project_layout_template(config, config_path=config_path):
        return launcher.LauncherUpgradeResult(
            changed=False,
            message=f"switchyard: {config.project} layout is hand-maintained or outside a provision directory; leaving it unchanged",
    )
    changed_messages: list[str] = []
    durable_session_dir = launcher._generated_project_durable_session_dir(config)
    session_dir_upgrade: Path | None = None
    shared_pane_launcher = launcher.switchyard_shared_pane_launcher()
    pane_launcher_upgrade: Path | None = None
    board_root = launcher._tenant_board_root_from_config_or_plan(config, config_path)
    directorctl_upgrade = str(board_root / "current" / "scripts" / "directorctl") if board_root is not None else ""
    roles_need_directorctl_upgrade = bool(directorctl_upgrade) and any(
        role.env.get("TICKET_BOARD_DIRECTORCTL") != directorctl_upgrade for role in config.roles
    )
    if roles_need_directorctl_upgrade:
        if dry_run:
            changed_messages.append(f"pane directorctl can be pinned to {directorctl_upgrade}")
        else:
            changed_messages.append(f"pinned pane directorctl to {directorctl_upgrade}")
    if (
        config.pane_launcher is not None
        and config.pane_launcher.expanduser().resolve(strict=False)
        != shared_pane_launcher.expanduser().resolve(strict=False)
    ):
        if dry_run:
            changed_messages.append(
                f"pane launcher can be upgraded from {config.pane_launcher} to {shared_pane_launcher}"
            )
        else:
            pane_launcher_upgrade = shared_pane_launcher
            changed_messages.append(f"upgraded pane launcher to {shared_pane_launcher}")
    if (
        durable_session_dir is not None
        and launcher.session_dir_uses_user_runtime(config.session_dir, config.run_as_user)
        and config.session_dir.expanduser().resolve(strict=False) != durable_session_dir.expanduser().resolve(strict=False)
    ):
        if dry_run:
            changed_messages.append(
                f"session dir can be upgraded from {config.session_dir} to {durable_session_dir}"
            )
        else:
            session_dir_upgrade = durable_session_dir
            changed_messages.append(f"upgraded session dir to {durable_session_dir}")
    role_count = sum(1 for role in config.roles if not role.detached)
    current_layout = launcher._new_project_layout_payload(role_count)
    known_layouts = launcher._known_generated_project_layout_payloads(role_count)
    handover = ""
    try:
        if config.run_as_user and launcher.root_writes_for(config.run_as_user):
            # As root, by descriptor: a layout root left as its own record -- 0600, which PIE's first start as the
            # owner could not open -- is handed to the owner: the inode that was read, and only if it is a layout
            # this release generates (SYRD-657).
            def generated(raw: bytes) -> bool:
                try:
                    return json.loads(raw.decode("utf-8")) in (current_layout, *known_layouts)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return False

            try:
                raw, handover = launcher.claim_owner_file(config.run_as_user, config.layout, accept=generated,
                                                          mode=0o644, apply=not dry_run)
            except SystemExit as refusal:
                raise OSError(str(refusal)) from None
            if raw is None:
                raise FileNotFoundError(f"{config.layout} is not there")
            existing_layout = json.loads(raw.decode("utf-8"))
        else:
            existing_layout = json.loads(config.layout.read_text(encoding="utf-8"))
    except OSError as exc:
        return launcher.LauncherUpgradeResult(
            changed=False,
            message=f"switchyard: cannot read generated layout template {config.layout}: {exc}",
        )
    except json.JSONDecodeError as exc:
        return launcher.LauncherUpgradeResult(
            changed=False,
            message=f"switchyard: generated layout template {config.layout} is not valid JSON: {exc}",
        )
    if handover == "handed":
        changed_messages.append(f"handed generated layout template {config.layout} to {config.run_as_user}, 0644")
    elif handover == "would hand":
        changed_messages.append(f"layout template {config.layout} is root's and can be handed to {config.run_as_user}")
    # Any other answer is a file root leaves as it is: no change to report, and the shape check below says so.
    if existing_layout != current_layout:
        if existing_layout not in known_layouts:
            if not changed_messages:
                return launcher.LauncherUpgradeResult(
                    changed=False,
                    message=f"switchyard: {config.project} layout template differs from the known generated legacy shapes; leaving it unchanged",
                )
            changed_messages.append("layout template differs from the known generated legacy shapes; leaving it unchanged")
        elif dry_run:
            changed_messages.append(f"layout template can be upgraded: {config.layout}")
        else:
            launcher.write_owner_file(config.run_as_user, config.layout, json.dumps(current_layout, indent=2, sort_keys=True) + "\n")
            launcher.ensure_owner_file(config, config.layout, runner=runner)
            changed_messages.append(f"upgraded generated layout template: {config.layout}")
    elif not changed_messages:
        return launcher.LauncherUpgradeResult(
            changed=False,
            message=f"switchyard: {config.project} layout template is already current",
        )
    if session_dir_upgrade is not None or pane_launcher_upgrade is not None or roles_need_directorctl_upgrade:
        # Root rewrites this document, so it must be the tenant's own and not a
        # link to somebody else's file (SYRD-228) -- or the one root itself wrote and holds, which the tenant may
        # have rewritten on disk since (SYRD-665).
        raw_config, config_problem = (dict(document), "") if document is not None else launcher.read_tenant_document_no_follow(
            config_path, what=f"{config.project}'s generated configuration"
        )
        if config_problem:
            return launcher.LauncherUpgradeResult(
                changed=False, message=f"switchyard: {config_problem}. Nothing was changed."
            )
        if session_dir_upgrade is not None:
            raw_config["session_dir"] = str(session_dir_upgrade)
        if pane_launcher_upgrade is not None:
            raw_config["pane_launcher"] = str(pane_launcher_upgrade)
        if roles_need_directorctl_upgrade:
            for raw_role in raw_config.get("roles", []):
                if not isinstance(raw_role, dict):
                    continue
                raw_env = raw_role.get("env")
                if not isinstance(raw_env, dict):
                    raw_env = {}
                    raw_role["env"] = raw_env
                raw_env["TICKET_BOARD_DIRECTORCTL"] = directorctl_upgrade
        # A dry run reports a stale pin and refuses a linked configuration as
        # an upgrade would, but neither writes the document nor hands it to
        # its owner (SYRD-418).
        if not dry_run:
            launcher._write_json_atomic(config_path, raw_config, owner_user=config.run_as_user)
            launcher.ensure_owner_file(config, config_path, runner=runner)
            if written is not None:
                written["document"] = raw_config
    return launcher.LauncherUpgradeResult(
        changed=not dry_run,
        message=f"switchyard: upgraded generated project config for {config.project}: {'; '.join(changed_messages)}",
    )


def upgrade_generated_project_layout(
    config: ProjectConfig,
    *,
    config_path: Path,
    dry_run: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    document: dict | None = None,
    written: dict | None = None,
) -> LauncherUpgradeResult:
    from scripts import team_launcher as launcher

    trusted = {} if document is None and written is None else {"document": document, "written": written}
    return launcher.upgrade_generated_project_config(config, config_path=config_path, dry_run=dry_run, runner=runner,
                                                     **trusted)
