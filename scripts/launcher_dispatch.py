"""The team-launcher command dispatch: `main` and the removed-command check it runs first.

`main` rejects the config-free commands that were removed
(`_reject_removed_commands`), parses the command line, and hands each command to
its own function -- design and new before any config is read; provision-runtime
with or without one; then, with the project's config loaded, upgrade, add-role,
set-vcs-close-role, stop, teardown, deploy-launcher, every pane mode (re-exec as
the role's own account, the launcher checkout check, attach-role and
detach-role, a viewer session, a detached role or the role's pane) and,
otherwise, the launch itself -- returning that command's answer.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-452). The launcher
imports this module and re-exports both names, so `scripts/team-launcher`, the
launcher's own `if __name__ == "__main__"` entry point and every caller of
`team_launcher.main` still reach the same function. Everything they read when
they run -- the parser, every command function, the config resolution and
loader, the role, account, desktop, checkout and session helpers, the
removed-command table and each other -- is read through the launcher, so a
suite that rebinds one there still intercepts it. This module imports
`team_launcher` only inside the functions, when they run.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Sequence


def _reject_removed_commands(argv: Sequence[str]) -> None:
    from scripts import team_launcher as launcher

    if len(argv) > 1 and str(argv[1]).strip() in launcher.REMOVED_CONFIG_FREE_COMMANDS:
        project = str(argv[0]).strip() or "<project>"
        replacement = launcher.BOOTSTRAP_REPLACEMENT_COMMAND.replace("<project>", project)
        raise SystemExit(
            f"team-launcher: {argv[1]} has been removed; use `{replacement}` to create a launchable project config"
        )


def main(argv: list[str] | None = None) -> int:
    from scripts import team_launcher as launcher

    argv = list(sys.argv[1:] if argv is None else argv)
    launcher._reject_removed_commands(argv)
    args = launcher._build_parser().parse_args(argv)
    if args.command == "design":
        return launcher.design_project_command(
            args.project,
            output_dir=args.design_output_dir,
            artifact_path=args.project_artifact,
            design_document=args.design_document,
            design_title=args.design_title,
            design_body=args.design_body,
            repository=args.repository,
            remote=args.remote,
            default_branch=args.default_branch,
            worktree_policy=args.worktree_policy,
            owner_user=args.owner_user,
            ticket_prefix=args.ticket_prefix,
            push_policy=args.push_policy,
            audit_signoff=args.audit_signoff,
            needs_inspection=args.needs_inspection,
            needs_user_signoff=args.needs_user_signoff,
            board_service_traversal=args.board_service_traversal,
            supplementary_groups=args.supplementary_groups,
            linger=args.linger,
            owner_shell=args.owner_shell,
        )
    if args.command == "new":
        return launcher.new_project_command(
            args.project,
            from_artifact=args.from_artifact,
            owner_user=args.owner_user,
            desktop_policy=args.desktop_policy,
            port=args.port,
            database=args.database,
            source_repo=args.source_repo,
            workflow_config=args.workflow_config,
            commit_git_dir=args.commit_git_dir,
            repository=args.repository,
            output_dir=args.new_output_dir,
            execute=args.execute,
            dry_run=args.dry_run,
            upstream_report_url=args.upstream_report_url or "",
            upstream_report_token_file=args.upstream_report_token_file or "",
        )
    if args.command == "provision-runtime" and args.config is None:
        try:
            resolved = launcher._resolve_launcher_project_config(args.project)
            config_path = resolved.config_path
            config_project = resolved.slug
        except SystemExit:
            config_path = launcher.DEFAULT_CONFIG_DIR / f"{args.project}.json"
            config_project = args.project
    else:
        resolved = launcher._resolve_launcher_project_config(args.project, explicit_config=args.config)
        config_path = resolved.config_path
        config_project = resolved.slug
    if args.command == "provision-runtime":
        config = launcher.load_project_config(config_project, config_path) if config_path.exists() else None
        return launcher.provision_runtime_command(args.runtime_user, config)
    config = launcher.load_project_config(config_project, config_path)
    if args.command == "upgrade":
        return launcher.upgrade_project_command(
            config,
            config_path=config_path,
            dry_run=args.dry_run,
            source_repo=args.source_repo,
            commit_git_dir=args.commit_git_dir,
            deploy_ref=args.deploy_ref,
            desktop_policy=args.desktop_policy,
            publish_remote=getattr(args, "publish_remote", ""),
            upstream_report_url=getattr(args, "upstream_report_url", "") or "",
            upstream_report_token_file=getattr(args, "upstream_report_token_file", "") or "",
        )
    if args.command == "add-role":
        if not args.pane_mode or args.role:
            raise SystemExit("add-role requires exactly one <role> argument")
        return launcher.add_project_role_command(
            config,
            config_path=config_path,
            role_name=args.pane_mode,
            cli=args.add_role_cli,
            audit_role=args.add_role_audit,
            detached=args.detached,
            slot=args.slot,
            relayout=args.relayout,
            start=not args.no_attach,
            script_path=args.script_path,
            pane_state_dir=args.pane_state_dir,
        )
    if args.command == "set-vcs-close-role":
        if not args.pane_mode or args.role:
            raise SystemExit("set-vcs-close-role requires exactly one <role> argument")
        return launcher.set_project_vcs_close_role_command(
            config,
            config_path=config_path,
            role_name=args.pane_mode,
            runner=subprocess.run,
        )
    if args.command == "stop":
        # The session-level stop, deliberately: this is the lower-level entry
        # point, and `switchyard stop` is the tenant suspension that also takes
        # the board, the listener and the window (SYRD-193).
        return launcher.stop_project(config)
    if args.command == "teardown":
        return launcher.switchyard_teardown_command(
            config_project,
            dry_run=args.dry_run,
            confirm=args.confirm,
            drop_nonempty_board=args.drop_nonempty_board,
            destroy_registered_tenant=args.destroy_registered_tenant,
            remove_owner_home=args.remove_owner_home,
            remove_owner_user=args.remove_owner_user,
            owner_user=args.owner_user,
        )
    if args.command == "deploy-launcher":
        return launcher.deploy_launcher_checkout(
            config,
            launcher_repo=args.launcher_repo,
            clean=args.clean_launcher,
        )
    if args.command != "pane" and (args.pane_mode or args.role):
        raise SystemExit(f"{args.command} does not accept extra pane arguments")
    if args.command == "pane":
        if not args.pane_mode or not args.role:
            raise SystemExit("pane mode requires <start|attach|attach-or-start|reload|attach-role|detach-role> and <role>")
        if args.pane_mode not in {"start", "attach", "attach-or-start", "reload", "attach-role", "detach-role"}:
            raise SystemExit(f"unknown pane mode: {args.pane_mode}")
        if args.pane_mode not in {"attach", "detach-role"}:
            config = launcher.prepare_project_desktop(config)
        role = launcher._role_by_name(config, args.role)
        # Every lifecycle path for this role runs as the role's own account, so
        # its tmux server, pane processes and board writes all carry that uid.
        # Re-execing as the shared project owner here is what previously undid
        # the per-role accounts the layout had already selected (SYRD-39).
        pane_user = launcher.role_run_as_user(config, role)
        pane_state_dir = args.pane_state_dir or launcher.default_pane_state_dir_for_user(pane_user, project=config.project)
        if pane_user and launcher.current_user_name() != pane_user:
            return subprocess.run(
                launcher.pane_command_args(
                    config.project,
                    role,
                    config_path=config_path,
                    mode=args.pane_mode,
                    script_path=args.script_path,
                    slot=args.slot,
                    pane_state_dir=pane_state_dir,
                    force_reload=args.force,
                    skip_launcher_check=args.skip_launcher_check,
                    allow_stale_launcher=args.allow_stale_launcher,
                    no_attach=args.no_attach,
                    run_as_user=pane_user,
                )
            ).returncode
        if not args.skip_launcher_check:
            launcher.ensure_launcher_checkout_current(
                config,
                runner=subprocess.run,
                auto_deploy=False,
                allow_stale=args.allow_stale_launcher,
            )
        launcher.ensure_configured_runtime_user(config)
        launcher.seed_default_session_dir_from_legacy_sources(config.session_dir)
        if args.pane_mode == "attach-role":
            if args.slot is None:
                raise SystemExit("pane attach-role requires --slot")
            return launcher.attach_role_to_slot(
                config,
                config_path=config_path,
                role_name=role.role,
                slot=args.slot,
                session_dir=launcher.role_session_dir(config, role),
                pane_state_dir=pane_state_dir,
            )
        if args.pane_mode == "detach-role":
            return launcher.detach_role_from_slot(
                config,
                config_path=config_path,
                role_name=role.role,
            )
        if args.no_attach and not role.detached:
            return launcher.ensure_visible_role_session_for_viewer(
                role,
                mode=args.pane_mode,
                session_dir=launcher.role_session_dir(config, role),
                pane_state_dir=pane_state_dir,
                force_reload=args.force,
                bin_user=pane_user,
            )
        if role.detached:
            return launcher.run_detached_role(
                role,
                mode=args.pane_mode,
                session_dir=launcher.role_session_dir(config, role),
                pane_state_dir=pane_state_dir,
                force_reload=args.force,
                bin_user=pane_user,
            )
        return launcher.run_role_pane(
            role,
            mode=args.pane_mode,
            session_dir=launcher.role_session_dir(config, role),
            pane_state_dir=pane_state_dir,
            force_reload=args.force,
            bin_user=pane_user,
        )
    return launcher.launch_project(
        config,
        config_path=config_path,
        mode=args.command,
        script_path=args.script_path,
        dry_run=args.dry_run,
        layout_output=args.layout_output,
        pane_state_dir=args.pane_state_dir,
        force_reload=args.force,
        allow_stale_launcher=args.allow_stale_launcher,
        no_launcher_self_deploy=args.no_launcher_self_deploy,
        report_session_records=True,
        layout_mode=args.layout,
    )
