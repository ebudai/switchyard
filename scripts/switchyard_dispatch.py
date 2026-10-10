"""The `switchyard` command dispatch: `switchyard_main`.

`switchyard_main` answers the wrapper's requires-root probe, reports an
installed release's version notice, and then hands each verb to its own
function -- the menu, help and version; new; the credential, identity,
registration, boundary, desktop, workflow, provisioning, upgrade, release,
publication, worker-pool, role, prompt, presentation, display, stop, start,
teardown, status and model-validation verbs -- and, for a bare project name,
the ordinary launch, returning that command's answer.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-452's measured next
closure, SYRD-453). The launcher imports this module and re-exports the name,
so the `switchyard` wrappers and every caller of `team_launcher.switchyard_main`
still reach the same function. Everything it reads when it runs -- every
command function and parser, the project resolution and config loading, the
tenant, desktop and first-run checks, and the launcher's own file, from which
the team-launcher script beside it is named -- is read through the launcher,
so a suite that rebinds one there still intercepts it. Its four nested imports
are kept where they were. This module imports `team_launcher` only inside the
function, when it runs.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def switchyard_main(argv: list[str] | None = None) -> int:
    from scripts import team_launcher as launcher

    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--switchyard-wrapper-requires-root"]:
        print("requires-root" if launcher.switchyard_invocation_requires_root(argv[1:]) else "no-root")
        return 0
    # Before the work, so it is read alongside whatever the command says rather
    # than scrolled past after it. `release_notice_lines` is silent unless this
    # process really is running from an installed release whose source checkout
    # is present and ahead of it, so an ordinary checkout run prints nothing.
    launcher.report_installed_release_version()
    if not argv:
        return launcher.switchyard_menu_command()
    if argv[0] in {"-h", "--help", "help"}:
        print(launcher.switchyard_help_text(), end="")
        return 0
    if argv[0] in {"--version", "version"}:
        print(launcher.switchyard_version_text())
        return 0
    if argv[0].casefold() == "new":
        args = launcher._build_switchyard_new_parser().parse_args(argv[1:])
        return launcher.switchyard_new_command(
            slug=args.slug,
            agent_name=args.agent_name,
            project_name=args.project_name,
            project_path=args.project_path,
            from_artifact=args.from_artifact,
            source_repo=args.source_repo,
            workflow_config=args.workflow_config,
            commit_git_dir=args.commit_git_dir,
            output_dir=args.output_dir,
            agent_cli_policy=args.agent_cli_policy,
            agent_cli_sources=args.agent_cli_source,
            port=args.port,
            database=args.database,
            yes=args.yes,
            desktop_policy=args.desktop_policy,
            headless=args.headless,
            desktop_gui_user=args.desktop_gui_user,
            allow_existing_owner_user=args.allow_existing_owner_user,
            agy_credential_source=args.agy_credential_source,
            no_agy_credential=args.no_agy_credential,
            layout_mode=args.layout,
            git_init=not args.no_git_init,
        )
    if argv[0].casefold() == "agy-credential":
        parser = argparse.ArgumentParser(prog="switchyard agy-credential")
        parser.add_argument("action", choices=("show", "set", "clear"))
        parser.add_argument("user", nargs="?", help="Unix user to seed agy credentials from (set only)")
        args = parser.parse_args(argv[1:])
        if args.action == "set" and not args.user:
            raise SystemExit("switchyard: agy-credential set requires a user name")
        return launcher.switchyard_agy_credential_command(args.action, source_user=args.user)
    if argv[0].casefold() == "seed-role-credentials":
        parser = argparse.ArgumentParser(prog="switchyard seed-role-credentials")
        parser.add_argument("project", help="registered project name or slug")
        parser.add_argument("--role", default="", help="seed only this role")
        parser.add_argument(
            "--reseed",
            action="store_true",
            help="replace credentials a role already has; the deliberate repair path",
        )
        args = parser.parse_args(argv[1:])
        entry = launcher._resolve_switchyard_project(args.project)
        config = launcher._load_switchyard_project_config_for_command(entry, argv)
        return launcher.switchyard_seed_role_credentials_command(
            config, role_name=args.role, reseed=args.reseed
        )
    if argv[0].casefold() == "set-owner-identity":
        parser = argparse.ArgumentParser(
            prog="switchyard set-owner-identity",
            description=(
                "Record which of the tenant owner's existing SSH keys this project publishes "
                "with, and select it for the forge. Creates no key and reads no private material."
            ),
        )
        parser.add_argument("project", help="registered project name or slug")
        parser.add_argument(
            "--key-name",
            default="",
            help="file name of an existing key pair in the owner's ~/.ssh, without a path",
        )
        parser.add_argument(
            "--clear",
            action="store_true",
            help=(
                "for a tenant that does not publish to GitHub: clear a recorded GitHub identity "
                "from both plans and remove Switchyard's managed block"
            ),
        )
        parser.add_argument(
            "--host-alias",
            default="",
            help="an additional Host pattern the managed block should answer to",
        )
        parser.add_argument("--host", default="github.com", help="forge host, default github.com")
        parser.add_argument("--dry-run", action="store_true", help="say what would change")
        args = parser.parse_args(argv[1:])
        if args.clear == bool(args.key_name):
            parser.error("give exactly one of --key-name or --clear")
        entry = launcher._resolve_switchyard_project(args.project)
        config = launcher._load_switchyard_project_config_for_command(entry, argv)
        if args.clear:
            return launcher.clear_owner_github_identity_command(
                config, config_path=entry.config_path, dry_run=args.dry_run
            )
        return launcher.set_owner_github_identity_command(
            config,
            config_path=entry.config_path,
            key_name=args.key_name,
            host_alias=args.host_alias,
            host=args.host,
            dry_run=args.dry_run,
        )
    if argv[0].casefold() == "register":
        args = launcher._build_switchyard_register_parser().parse_args(argv[1:])
        return launcher.switchyard_register_command(args.config_path)
    if argv[0].casefold() == "repair-boundary":
        args = launcher._build_switchyard_repair_boundary_parser().parse_args(argv[1:])
        return launcher.switchyard_repair_boundary_command(args.project, apply=args.apply)
    if argv[0].casefold() == "approve-desktop":
        args = launcher._build_switchyard_approve_desktop_parser().parse_args(argv[1:])
        return launcher.switchyard_approve_desktop_command(
            gui_user=args.gui_user,
            reference=args.reference,
            show=args.show,
            revoke=args.revoke,
        )
    if argv[0].casefold() == "rebind-workflow-panes":
        args = launcher._build_switchyard_rebind_workflow_panes_parser().parse_args(argv[1:])
        runtimes: dict[str, str] = {}
        for item in args.runtime:
            name, sep, value = item.partition("=")
            if not sep or not name.strip() or not value.strip():
                raise SystemExit(f"switchyard: --runtime takes ROLE=RUNTIME, not {item!r}")
            runtimes[name.strip()] = value.strip()
        slots: dict[str, int] = {}
        for item in args.slot:
            name, sep, value = item.partition("=")
            if not sep or not name.strip() or not value.strip().isdigit():
                raise SystemExit(f"switchyard: --slot takes ROLE=SLOT, not {item!r}")
            slots[name.strip()] = int(value.strip())
        return launcher.switchyard_rebind_workflow_panes_command(
            args.project, apply=args.apply, expect=args.expect, runtimes=runtimes, slots=slots,
            config_path=args.config_path,
        )
    if argv[0].casefold() == "migrate-workflow":
        args = launcher._build_switchyard_migrate_workflow_parser().parse_args(argv[1:])
        return launcher.switchyard_migrate_workflow_command(
            args.project, apply=args.apply, config_path=args.config_path
        )
    if argv[0].casefold() == "adopt-workflow":
        args = launcher._build_switchyard_adopt_workflow_parser().parse_args(argv[1:])
        return launcher.switchyard_adopt_workflow_command(
            args.project,
            apply=args.apply,
            despite_board=args.despite_board,
            from_live=args.from_live,
            replacing=args.replacing,
            add_role=args.add_role,
            config_path=args.config_path,
        )
    if argv[0].casefold() == "resume-provision":
        args = launcher._build_switchyard_resume_provision_parser().parse_args(argv[1:])
        return launcher.switchyard_resume_provision_command(
            args.project, source_repo=args.source_repo, config_path=args.config_path
        )
    if argv[0].casefold() == "upgrade":
        args = launcher._build_switchyard_upgrade_parser().parse_args(argv[1:])
        entry = launcher._resolve_switchyard_project(args.project)
        config = launcher._load_switchyard_project_config_for_command(entry, argv)
        return launcher.upgrade_project_command(
            config,
            config_path=entry.config_path,
            dry_run=args.dry_run,
            source_repo=args.source_repo,
            commit_git_dir=args.commit_git_dir,
            deploy_ref=args.deploy_ref,
            desktop_policy=args.desktop_policy,
            publish_remote=getattr(args, "publish_remote", ""),
            upstream_report_url=getattr(args, "upstream_report_url", "") or "",
            upstream_report_token_file=getattr(args, "upstream_report_token_file", "") or "",
        )
    if argv[0].casefold() == "install-shared-release":
        args = launcher._build_switchyard_install_shared_release_parser().parse_args(argv[1:])
        return launcher.switchyard_install_shared_release_command(
            args.commit, rollback=args.rollback, dry_run=args.dry_run
        )
    if argv[0].casefold() == "privileged-action":
        args = launcher._build_switchyard_privileged_action_parser().parse_args(argv[1:])
        values: dict[str, str] = {}
        for item in args.values:
            key, sep, value = item.partition("=")
            if not sep or not key:
                raise SystemExit(f"switchyard: expected key=value, got {item!r}")
            if key in values:
                raise SystemExit(f"switchyard: {key} was given twice")
            values[key] = value
        from scripts.ticket_board import privileged_front_door

        return privileged_front_door.privileged_action_command(
            args.project,
            args.action,
            values,
            dry_run=args.dry_run,
            rollback_commands=lambda project: launcher.release_rollback_commands(project),
        )
    if argv[0].casefold() == "rollout-log":
        args = launcher._build_switchyard_rollout_log_parser().parse_args(argv[1:])
        entry = launcher._resolve_switchyard_project(args.project)
        # The registry entry's slug, which is the tenant's `project` field and
        # so the key the journal is written under. `entry.project` does not
        # exist and crashed every invocation of this command (SYRD-132); the
        # config is deliberately not loaded, because reading a record must keep
        # working for a tenant whose configuration does not.
        return launcher.rollout_log_command(entry.slug, attempt=args.attempt, output=args.output)
    if argv[0].casefold() == "publication-status":
        args = launcher._build_switchyard_publication_status_parser().parse_args(argv[1:])
        entry = launcher._resolve_switchyard_project(args.project)
        config = launcher._load_switchyard_project_config_for_command(entry, argv)
        return launcher.publication_status_command(
            config, config_path=entry.config_path, verify=args.verify
        )
    if argv[0].casefold() == "cutover-roles":
        args = launcher._build_switchyard_cutover_roles_parser().parse_args(argv[1:])
        print(
            f"switchyard: cutover-roles is retired for {args.project}; run `switchyard upgrade "
            f"{args.project}` to repatriate resumable state without creating or deleting accounts"
        )
        return 1
    if argv[0].casefold() == "finish-upgrade":
        args = launcher._build_switchyard_finish_upgrade_parser().parse_args(argv[1:])
        entry = launcher._resolve_switchyard_project(args.project)
        config = launcher._load_switchyard_project_config_for_command(entry, argv)
        return launcher.finish_upgrade_command(
            config,
            config_path=entry.config_path,
            dry_run=args.dry_run,
            source_repo=args.source_repo,
            commit_git_dir=args.commit_git_dir,
            deploy_ref=args.deploy_ref,
        )
    if argv[0].casefold() == "deploy-release":
        args = launcher._build_switchyard_deploy_release_parser().parse_args(argv[1:])
        return launcher.switchyard_deploy_release_command(args.project, commit=args.commit)
    if argv[0].casefold() == "release-status":
        args = launcher._build_switchyard_release_status_parser().parse_args(argv[1:])
        return launcher.switchyard_release_status_command(args.project, close=args.close)
    if argv[0].casefold() == "worker-pool":
        args = launcher._build_switchyard_worker_pool_parser().parse_args(argv[1:])
        if launcher.WORKER_POOL_ACTIONS[args.action] and not args.member.strip():
            raise SystemExit(
                f"switchyard: worker-pool {args.action} needs the worker it acts on, "
                f"e.g. `switchyard worker-pool {args.project} {args.action} <pool>-3`"
            )
        return launcher.switchyard_worker_pool_command(
            args.project,
            action=args.action,
            member=args.member.strip(),
            apply_changes=args.apply_changes,
            force=args.force,
            out=args.out,
            journal=args.journal,
        )
    if argv[0].casefold() == "add-role":
        args = launcher._build_switchyard_add_role_parser().parse_args(argv[1:])
        entry = launcher._resolve_switchyard_project(args.project)
        config = launcher._load_switchyard_project_config_for_command(entry, argv)
        return launcher.add_project_role_command(
            config,
            config_path=entry.config_path,
            role_name=args.role,
            cli=args.cli,
            audit_role=args.audit,
            detached=args.detached,
            slot=args.slot,
            relayout=args.relayout,
            start=not args.no_start,
            script_path=Path(launcher.__file__).resolve().with_name(launcher.TEAM_LAUNCHER_NAME),
        )
    if argv[0].casefold() == "board-skill":
        from scripts import board_skill_cli

        return board_skill_cli.main(argv[1:], prog="switchyard board-skill")
    if argv[0].casefold() == "onboarding-readiness":
        from scripts import onboarding_readiness

        return onboarding_readiness.main(argv[1:])
    # `design-stage` shares this block: like role-prompt it writes the tenant's own
    # declared workflow through workflow_manage as the caller (SYRD-567).
    if argv[0].casefold() in ("role-prompt", "design-stage"):
        design_stage = argv[0].casefold() == "design-stage"
        parser = argparse.ArgumentParser(
            prog=f"switchyard {argv[0].casefold()}",
            description=(
                "Add the optional design stage to a project's declared workflow: the designer "
                "owns design work and submits it to the Director's design review, which returns "
                "it or accepts it into triage. release_draft is unchanged."
            ) if design_stage else (
                "Show, set, or clear the onboarding prompt a role receives when its next "
                "conversation starts fresh. A running conversation is never interrupted or "
                "rewritten: a changed prompt is used by the next fresh session or an "
                "explicit role restart."
            ),
        )
        if design_stage:
            parser.add_argument(
                "--dry-run", action="store_true",
                help="print the document and projection it would apply, and change nothing",
            )
        else:
            parser.add_argument("action", choices=("show", "set", "clear"))
            parser.add_argument("role")
        parser.add_argument(
            "--project",
            default=os.environ.get("TICKET_BOARD_PROJECT", ""),
            help="project name or slug; defaults to TICKET_BOARD_PROJECT in the caller's pane",
        )
        if not design_stage:
            parser.add_argument("--prompt", help="prompt text; use --prompt-file for anything long")
            parser.add_argument(
                "--prompt-file",
                type=Path,
                help="read the prompt from a file, or from stdin when given as -",
            )
        args = parser.parse_args(argv[1:])
        if not args.project.strip():
            raise SystemExit(
                "switchyard: no project selected; pass --project or run where "
                "TICKET_BOARD_PROJECT is set"
            )
        entry = launcher._resolve_switchyard_project(args.project)
        config = launcher._load_switchyard_project_config_for_command(entry, argv)
        from scripts import workflow_manage

        if design_stage:
            forwarded = ["add-design-stage", "--board-url", config.board_url, "--config", str(entry.config_path)]
            return workflow_manage.main(forwarded + (["--dry-run"] if args.dry_run else []))
        forwarded = [
            f"{args.action}-role-prompt",
            "--role",
            args.role,
            "--board-url",
            config.board_url,
        ]
        if args.action != "show":
            forwarded += ["--config", str(entry.config_path)]
        if args.prompt is not None:
            forwarded += ["--prompt", args.prompt]
        if args.prompt_file is not None:
            forwarded += ["--prompt-file", str(args.prompt_file)]
        return workflow_manage.main(forwarded)
    if argv[0].casefold() == "set-role-runtime":
        args = launcher._build_switchyard_set_role_runtime_parser().parse_args(argv[1:])
        entry = launcher._resolve_switchyard_project(args.project)
        config = launcher._load_switchyard_project_config_for_command(entry, argv)
        return launcher.set_project_role_runtime_command(
            config,
            config_path=entry.config_path,
            role_name=args.role,
            runtime=args.cli,
            model=args.model,
            effort=args.effort,
            force=args.force,
            reason=args.reason,
            dry_run=args.dry_run,
        )
    if argv[0].casefold() == "present":
        args = launcher._build_switchyard_present_parser().parse_args(argv[1:])
        entry = launcher._resolve_switchyard_project(args.project)
        config = launcher._load_switchyard_project_config_for_command(entry, argv)
        return launcher.switchyard_present_command(config, config_path=entry.config_path, args=args)
    if argv[0].casefold() == "attach":
        args = launcher._build_switchyard_attach_parser().parse_args(argv[1:])
        entry = launcher._resolve_switchyard_project(args.project)
        config = launcher._load_switchyard_project_config_for_command(entry, argv)
        return launcher.switchyard_attach_command(config, args=args)
    if argv[0].casefold() == "replace-window":
        args = launcher._build_switchyard_replace_window_parser().parse_args(argv[1:])
        entry = launcher._resolve_switchyard_project(" ".join(args.project))
        config = launcher._load_switchyard_project_config_for_command(entry, argv)
        return launcher.replace_presentation_window_command(config, config_path=entry.config_path)
    if argv[0].casefold() == "set-vcs-close-role":
        args = launcher._build_switchyard_set_vcs_close_role_parser().parse_args(argv[1:])
        entry = launcher._resolve_switchyard_project(args.project)
        config = launcher._load_switchyard_project_config_for_command(entry, argv)
        return launcher.set_project_vcs_close_role_command(
            config,
            config_path=entry.config_path,
            role_name=args.role,
        )
    if argv[0].casefold() == "recover-display":
        return launcher.switchyard_recover_display_command(argv)
    if argv[0].casefold() == "stop":
        args = launcher._build_switchyard_stop_parser().parse_args(argv[1:])
        project = " ".join(args.project)
        entry = launcher._resolve_switchyard_project(project)
        config = launcher._load_switchyard_project_config_for_command(entry, argv)
        # SYRD-193: the public verb is a whole-tenant suspension now -- window,
        # sessions, escaped processes, listener and board -- and it preserves
        # every byte of restart state. The worker-only stop it used to be is
        # still `stop_role_sessions`, which is what the upgrade transaction
        # calls and what must not take a board or a window down.
        problems = launcher.suspend_tenant(config, config_path=entry.config_path)
        for problem in problems:
            print(f"switchyard: {problem}")
        if problems:
            print(
                f"switchyard: {config.project} is partially stopped; nothing was removed and "
                f"`switchyard start {config.project}` still resumes what is down."
            )
            return 1
        print(
            f"switchyard: {config.project} is suspended. Its board database, history, worktrees, "
            f"credentials, provider state and session records are untouched; "
            f"`switchyard start {config.project}` brings it back."
        )
        return 0
    if argv[0].casefold() == "start":
        args = launcher._build_switchyard_start_parser().parse_args(argv[1:])
        project = " ".join(args.project)
        entry = launcher._resolve_switchyard_project(project)
        config = launcher._load_switchyard_project_config_for_command(entry, argv)
        problems = launcher.resume_tenant(config, config_path=entry.config_path)
        for problem in problems:
            print(f"switchyard: {problem}")
        if problems:
            return 1
        config = launcher.prepare_project_desktop(config)
        return launcher.launch_project(
            config,
            config_path=entry.config_path,
            mode="start",
            script_path=Path(launcher.__file__).resolve().with_name(launcher.TEAM_LAUNCHER_NAME),
            report_session_records=True,
        )
    if argv[0].casefold() == "teardown":
        args = launcher._build_switchyard_teardown_parser().parse_args(argv[1:])
        return launcher.switchyard_teardown_command(
            args.project,
            dry_run=args.dry_run,
            confirm=args.confirm,
            drop_nonempty_board=args.drop_nonempty_board,
            destroy_registered_tenant=args.destroy_registered_tenant,
            remove_owner_home=args.remove_owner_home,
            remove_owner_user=args.remove_owner_user,
            owner_user=args.owner_user,
        )
    if argv[0].casefold() == "status":
        args = launcher._build_switchyard_status_parser().parse_args(argv[1:])
        selection = " ".join(args.project)
        if selection:
            # One project needs no root: its own owner can answer for it. That
            # matters beyond tidiness -- crossing accounts then uses the same
            # policy as every other project command, which is what lets the
            # recorded human reach it over the control bridge without a
            # password. The unscoped listing still reads every tenant, so it
            # still takes the root path (SYRD-50 rollout review).
            # Resolved, not loaded: loading the tenant's configuration here
            # would cross to its owner -- or re-exec under root when the file
            # cannot be read -- for a command that only reports. The status
            # itself reads what this account may read and says what it may not
            # (SYRD-241).
            entry = launcher._resolve_switchyard_project(selection)
            return launcher.switchyard_status_command(json_output=args.json, project=entry.slug)
        return launcher.switchyard_status_command(json_output=args.json)
    if argv[0].casefold() == "validate-models":
        if len(argv) < 2:
            raise SystemExit("switchyard validate-models requires <project>")
        project = " ".join(argv[1:])
        entry = launcher._resolve_switchyard_project(project)
        launcher._load_switchyard_project_config_for_command(entry, argv)
        return launcher.switchyard_validate_models_command(project)
    selection = " ".join(argv)
    entry = launcher._resolve_switchyard_project(selection)
    config = launcher._load_switchyard_project_config_for_command(entry, argv)
    # Model validation intentionally runs only for `switchyard new` and the
    # explicit validate-models command. It performs provider API calls, so a
    # routine team start should not depend on provider availability.
    # SYRD-193: a suspended tenant is recovered by the ordinary launch, not only
    # by the explicit `start` verb. The bridge maps its `start` operation onto
    # this bare path, so a resumption that only lived in the named verb would be
    # unreachable through the very route a desktop user takes. Units already
    # active are left alone, so a running tenant is untouched by this.
    for problem in launcher.resume_tenant(config, config_path=entry.config_path):
        print(f"switchyard: {problem}")
        return 1
    config = launcher.prepare_project_desktop(config)
    first_run_auth_report = launcher.run_switchyard_launch_first_run_auth(config)
    if launcher.stop_before_launch_for_missing_owner_clis(first_run_auth_report):
        return 1
    if launcher.stop_before_launch_for_unauthenticated_providers(first_run_auth_report):
        return 1
    if launcher.stop_before_launch_for_unknown_models(first_run_auth_report, project=config.project):
        return 1
    launch_result = launcher.launch_project(
        config,
        config_path=entry.config_path,
        mode="start",
        script_path=Path(launcher.__file__).resolve().with_name(launcher.TEAM_LAUNCHER_NAME),
        report_session_records=True,
    )
    if launch_result != 0:
        return launch_result
    launcher.report_first_run_auth_warnings(first_run_auth_report)
    return 0
