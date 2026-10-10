"""The `switchyard` command-line parsers: one `argparse` parser per verb.

Each `_build_switchyard_<verb>_parser` builds the parser `switchyard <verb>`
parses its arguments with -- its program name, description, help, options,
choices and defaults. Building one runs nothing.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-411), in their original
order; the parsers of verbs that moved earlier stay with their commands. The
launcher imports this module and re-exports all twenty-one names;
`switchyard_main` and the display-recovery command still build them through
the launcher's globals. The launcher constants four of them read -- the agent
CLI policies, the layout modes and the supported CLI names -- are read through
the launcher when a parser is built, so a suite that rebinds one there still
reaches it. The privileged-action parser imports its catalogue when it is
built, as it did. This module imports `team_launcher` only inside those four
functions, when they run.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def _build_switchyard_new_parser() -> argparse.ArgumentParser:
    from scripts import team_launcher as launcher

    parser = argparse.ArgumentParser(prog="switchyard new", description="Create a Switchyard project.")
    parser.add_argument("--slug", help="project slug; prompted when omitted")
    parser.add_argument("--agent-name", help="owner user; prompted default is <slug>-agent when omitted")
    parser.add_argument("--project-name", help="human project name; prompted when omitted")
    parser.add_argument("--project-path", type=Path, help="project working directory; prompted when omitted")
    parser.add_argument("--from", dest="from_artifact", type=Path, help="project artifact emitted by the design session")
    parser.add_argument("--source-repo", type=Path, help="Switchyard source checkout or exported release to deploy")
    parser.add_argument(
        "--commit-git-dir",
        help="git repository path, or colon-separated paths, used to verify board commit hashes",
    )
    parser.add_argument("--output-dir", type=Path, help="write provisioning artifacts here")
    parser.add_argument(
        "--agent-cli-policy",
        choices=list(launcher.AGENT_CLI_POLICIES),
        default="",
        help=(
            "what an unattended run may do about a selected CLI that is not installed host-wide: "
            "require-host-wide refuses before creating anything; promote-local copies an "
            "executable you name to /usr/local/bin, root-owned, reusable by every later project. "
            "Switchyard never fetches a vendor installer. Interactive runs ask instead."
        ),
    )
    parser.add_argument(
        "--agent-cli-source",
        action="append",
        default=[],
        metavar="CLI=PATH",
        help=(
            "local executable to promote host-wide for CLI, required by "
            "--agent-cli-policy promote-local; repeatable"
        ),
    )
    parser.add_argument("--port", type=int, help="HTTP port; omitted means deterministic allocation")
    parser.add_argument("--database", help="PostgreSQL database; omitted means <slug>_ticket_board")
    parser.add_argument("--yes", action="store_true", help="proceed without the confirmation prompt")
    parser.add_argument(
        "--no-git-init",
        action="store_true",
        help="do not initialize --project-path; it must already be a git repository with an initial commit",
    )
    parser.add_argument(
        "--allow-existing-owner-user",
        action="store_true",
        help="reuse an existing owner user without the existing-user confirmation prompt",
    )
    parser.add_argument(
        "--agy-credential-source",
        metavar="USER",
        help=(
            "override this host's recorded agy credential source for this project; copies USER's "
            "agy OAuth token into the owner user so the project's panes do not need their own "
            "agy login, sharing that one Google account across every role"
        ),
    )
    parser.add_argument(
        "--no-agy-credential",
        action="store_true",
        help="do not seed any agy credential for this project, ignoring this host's recorded source",
    )
    parser.add_argument(
        "--layout",
        choices=sorted(launcher.LAYOUT_MODE_CHOICES),
        default=launcher.LAYOUT_MODE_AUTO,
        help="window layout mode: auto detects the invoking desktop, separate keeps the KDE/Konsole path, viewer forces the tmux viewer",
    )
    parser.add_argument(
        "--desktop-policy",
        type=Path,
        help=(
            "advanced: import an explicit policy. headless, or a JSON file recording scoped "
            "Wayland consent. Ordinary desktop provisioning generates this itself"
        ),
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="install without screenshot or clipboard access; works with no compositor present",
    )
    parser.add_argument(
        "--desktop-gui-user",
        default="",
        help=(
            "the desktop account this project may use, when more than one is signed in; "
            "otherwise the single active Wayland session is used"
        ),
    )
    parser.add_argument("--workflow-config", type=Path, help="declarative roles/stages JSON for the new project")
    parser.add_argument(
        "--upstream-report-url",
        default="",
        help=(
            "board URL this project files reports to (a project registered on this host, such as "
            "Switchyard's own board); its panes can file-report from their first launch (SYRD-548)"
        ),
    )
    parser.add_argument(
        "--upstream-report-token-file",
        default="",
        help="where the report-only credential belongs (default: ~/.config/<project>/upstream-report.env)",
    )
    return parser


def _build_switchyard_repair_boundary_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard repair-boundary",
        description=(
            "Apply the reviewed repository and worktree authority boundary to a registered "
            "tenant. The commands are lifted from root's own installed packet, between the "
            "markers it writes around that phase, so what runs is what root installed and "
            "nothing else in the packet runs at all -- no deploy, no schema or workflow seed, "
            "no RBAC, no unit installation or reload, no session startup. Writes nothing "
            "without --apply, needs Polkit, and is kept in the rollout journal."
        ),
    )
    parser.add_argument("project", help="the registered project to repair")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="run the repair; without it this reports what is open and changes nothing",
    )
    return parser


def _build_switchyard_approve_desktop_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard approve-desktop",
        description=(
            "Record, show or withdraw this host's standing desktop approval -- the one a "
            "project's scoped Wayland policy is generated from. Approving and revoking need "
            "root and record who asked, taken from the mechanism that elevated the run "
            "(PKEXEC_UID, then SUDO_USER) rather than from a flag. This is a host-level "
            "standing grant and is not --desktop-policy, which supplies a policy for one "
            "launch."
        ),
    )
    parser.add_argument(
        "--gui-user",
        default="",
        metavar="USER",
        help="whose desktop this host is approving; inferred when exactly one is signed in",
    )
    parser.add_argument(
        "--reference",
        default="",
        metavar="TEXT",
        help="required: on what basis this was approved, kept in the record",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--show", action="store_true", help="print the current record and stop")
    action.add_argument(
        "--revoke",
        action="store_true",
        help="withdraw the approval, keeping a record of who withdrew it and why",
    )
    return parser


def _build_switchyard_resume_provision_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard resume-provision",
        description=(
            "Finish a project whose provisioning stopped before it was registered: rebuild "
            "root's artifacts from an audited release, hand back the operator packet while it "
            "is unfinished, then register the generated configuration and start the configured "
            "roles. Re-runnable -- it continues from wherever the last attempt stopped."
        ),
    )
    parser.add_argument("project", help="the project slug root holds a provisioning record for")
    parser.add_argument(
        "--source-repo",
        type=Path,
        help=(
            "the audited release to rebuild the artifacts from; defaults to the installed "
            "shared release"
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        dest="config_path",
        help=(
            "the generated launcher configuration to register, for a project whose checkout "
            "is not where it was generated; it is checked against root's record either way"
        ),
    )
    return parser


def _build_switchyard_register_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="switchyard register", description="Register an existing Switchyard project config.")
    parser.add_argument("config_path", type=Path, help="path to the project's generated launcher config JSON")
    return parser


def _build_switchyard_upgrade_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="switchyard upgrade", description="Upgrade safe generated artifacts for a Switchyard project.")
    parser.add_argument("project", help="project name or slug")
    parser.add_argument(
        "--only",
        choices=["upstream-report"],
        default="",
        help=(
            "run this one narrow step and no upgrade phase: upstream-report connects the project to the "
            "board it files reports to -- its report URL, its credential and nothing else (SYRD-548)"
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="report what would change without writing files")
    parser.add_argument("--deploy-ref", default=None, help="board release ref to deploy (default: the pinned release, else origin/main)")
    parser.add_argument("--source-repo", type=Path, help="Switchyard source checkout or exported release to deploy")
    parser.add_argument(
        "--commit-git-dir",
        help="replace and persist the git repository path(s) used to verify board commit hashes",
    )
    parser.add_argument("--desktop-policy", type=Path, help="headless, or a JSON file recording scoped Wayland consent; installed before role launch")
    parser.add_argument(
        "--upstream-report-url",
        default="",
        help=(
            "board URL where this tenant files reports; recorded in its configuration and "
            "refreshed from that board's own credential, so panes need no flags afterwards"
        ),
    )
    parser.add_argument(
        "--upstream-report-token-file",
        default="",
        help="where the report-only credential belongs (default: ~/.config/<project>/upstream-report.env)",
    )
    parser.add_argument(
        "--publish-remote",
        default="",
        help=(
            "the exact remote root may publish to, recorded root-owned and reused afterwards; "
            "it is never read from the project account, which every role runs as"
        ),
    )
    return parser


def _build_switchyard_install_shared_release_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard install-shared-release",
        description=(
            "Make an already-built release this host's current one. Takes a full "
            "content-addressed commit and nothing else: the release is resolved against "
            "root's own cache under /opt/switchyard/releases, and a caller never names a "
            "path. The previous target is recorded before anything moves, the pointer is "
            "swapped atomically, what landed is verified, and the previous target is put "
            "back if it does not verify."
        ),
    )
    parser.add_argument("--commit", default="", help="the full 40-character commit to activate")
    parser.add_argument(
        "--rollback",
        action="store_true",
        help="return to the target recorded by the last activation",
    )
    parser.add_argument("--dry-run", action="store_true", help="report and change nothing")
    return parser


def _build_switchyard_privileged_action_parser() -> argparse.ArgumentParser:
    from scripts.ticket_board import privileged_actions

    known = ", ".join(action.name for action in privileged_actions.CATALOGUE)
    parser = argparse.ArgumentParser(
        prog="switchyard privileged-action",
        description=(
            "Run one bounded privileged Switchyard operation. An action is a name from a "
            "fixed catalogue plus typed values -- never a program, a path, a command string "
            "or an environment -- and polkit is asked only whether one installed, root-owned "
            "helper may run that named action. Use --dry-run first: it reports the exact "
            "action, the validated arguments, the installed policy, what root would run and "
            "the rollback path, and asks for no privilege at all. "
            f"Catalogued actions: {known}."
        ),
    )
    parser.add_argument("project", help="project name or slug")
    parser.add_argument("action", help="a catalogued action name")
    parser.add_argument(
        "values",
        nargs="*",
        metavar="key=value",
        help="typed values for the action; anything undeclared is refused",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be asked for and stop, without requesting privilege",
    )
    return parser


def _build_switchyard_rollout_log_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard rollout-log",
        description=(
            "Read what a privileged provisioning or upgrade run recorded. The journal is "
            "root-owned and every role may read it without sudo, which is the point: the "
            "evidence of a run is not owned by the account that run was about. The chained "
            "index is an integrity check -- it catches an entry edited, removed or reordered "
            "without the hashes after it being recomputed -- and not a proof against root, "
            "which owns the whole file and could recompute them."
        ),
    )
    parser.add_argument("project", help="project name or slug")
    parser.add_argument("--attempt", default="", help="one attempt id, or the latest by default")
    parser.add_argument(
        "--output", action="store_true", help="print the captured stdout and stderr as well"
    )
    return parser


def _build_switchyard_cutover_roles_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard cutover-roles",
        description=(
            "Stop a project's roles, move them onto their own Unix accounts, restart them and "
            "verify the uid each role process is actually running as. Rolls back if it does not hold."
        ),
    )
    parser.add_argument("project", help="project name or slug")
    parser.add_argument("--dry-run", action="store_true", help="report what would happen without stopping anything")
    return parser


def _build_switchyard_finish_upgrade_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard finish-upgrade",
        description=(
            "Run the director-owned phase of a Switchyard project upgrade. Must be run by the "
            "director, unprivileged."
        ),
    )
    parser.add_argument("project", help="project name or slug")
    parser.add_argument("--dry-run", action="store_true", help="report what would change without writing")
    parser.add_argument("--deploy-ref", default=None, help="board release ref to deploy (default: the pinned release, else origin/main)")
    parser.add_argument("--source-repo", type=Path, help="Switchyard source checkout or exported release to deploy")
    parser.add_argument("--commit-git-dir", help="git repository path(s) used to verify board commit hashes")
    return parser


def _build_switchyard_add_role_parser() -> argparse.ArgumentParser:
    from scripts import team_launcher as launcher

    parser = argparse.ArgumentParser(prog="switchyard add-role", description="Add a role to an existing Switchyard project.")
    parser.add_argument("project", help="project name or slug")
    parser.add_argument("role", help="new role name")
    parser.add_argument(
        "--cli",
        default="",
        choices=sorted(launcher.SUPPORTED_CONFIG_CLI_NAMES),
        help=(
            "CLI runtime for the role; omit it at a terminal to choose from the "
            "runtimes this host supports (default: codex)"
        ),
    )
    parser.add_argument("--audit", action="store_true", help="add the role as an auditor instead of an implementer")
    parser.add_argument("--slot", type=int, help="visible layout slot; generated layouts append automatically when omitted")
    parser.add_argument("--detached", action="store_true", help="start the role as a headless tmux session")
    parser.add_argument("--relayout", action="store_true", help="replace the existing layout with a generated layout when adding a visible role")
    parser.add_argument("--no-start", action="store_true", help="register the role without starting its pane")
    return parser


def _build_switchyard_set_vcs_close_role_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard set-vcs-close-role",
        description="Set the role that closes a provisioned project after final sign-off.",
    )
    parser.add_argument("project", help="project name or slug")
    parser.add_argument("role", help="existing project role allowed to mark tickets done")
    return parser


def _build_switchyard_replace_window_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard replace-window",
        description=(
            "Replace a Switchyard project's root-owned presentation window with an unprivileged "
            "one. Worker sessions keep running."
        ),
    )
    parser.add_argument("project", nargs="+", help="project name or slug")
    return parser


def _build_switchyard_stop_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="switchyard stop", description="Stop a Switchyard project's tmux pane sessions.")
    parser.add_argument("project", nargs="+", help="project name or slug")
    return parser


def _build_switchyard_recover_display_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard recover-display",
        description=(
            "Reattach a project's Director display after its window slot lost it. Run it from "
            "the desktop session of the operator the project is registered to; it reaches the "
            "project owner over the tenant-control bridge and recovers the Director slot only."
        ),
    )
    parser.add_argument("project", nargs="+", help="project name or slug")
    return parser


def _build_switchyard_start_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="switchyard start")
    parser.add_argument("project", nargs="+")
    return parser


def _build_switchyard_teardown_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard teardown",
        description="Remove Switchyard-created provisioning artifacts for a project.",
    )
    parser.add_argument("project", help="project slug")
    parser.add_argument("--dry-run", action="store_true", help="list exactly what would be removed without changing anything")
    parser.add_argument("--confirm", help="required project slug for the destructive run")
    parser.add_argument("--owner-user", help="owner user for an unregistered partial provision (default: <project>-agent)")
    parser.add_argument(
        "--drop-nonempty-board",
        action="store_true",
        help="allow dropping a board database that still contains tickets",
    )
    parser.add_argument(
        "--destroy-registered-tenant",
        action="store_true",
        help="allow removing a registered launchable tenant; off by default",
    )
    parser.add_argument(
        "--remove-owner-home",
        action="store_true",
        help="also remove /home/<owner>; off by default because it can contain user work",
    )
    parser.add_argument(
        "--remove-owner-user",
        action="store_true",
        help="also remove the owner Unix account; off by default",
    )
    return parser


def _build_switchyard_set_role_runtime_parser() -> argparse.ArgumentParser:
    from scripts import team_launcher as launcher

    parser = argparse.ArgumentParser(
        prog="switchyard set-role-runtime",
        description="Change an existing role's agent runtime and reconnect the panes showing it.",
    )
    parser.add_argument("project", help="registered project name or slug")
    parser.add_argument("role", help="existing role whose runtime changes")
    parser.add_argument(
        "--cli",
        default="",
        choices=sorted(launcher.SUPPORTED_CONFIG_CLI_NAMES),
        help=(
            "agent runtime the role should run from now on; omit it at a terminal "
            "to choose from the runtimes this host supports"
        ),
    )
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "model the role should run, checked against the project owner's own catalog; "
            "pass an empty value to drop it and take the runtime's default. Omit it to keep "
            "the configured model, or to be asked when it is one the owner does not offer"
        ),
    )
    parser.add_argument(
        "--effort",
        default=None,
        help=(
            "reasoning effort the role should run at, checked against what the runtime accepts for "
            "its model in the project owner's account; it replaces any effort setting left in the "
            "role's arguments. Pass an empty value to take the runtime's default; omit it to keep "
            "the role's effort"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="switch even though the role is mid-turn; requires --reason",
    )
    parser.add_argument("--reason", default="", help="why a busy role was interrupted; recorded with the change")
    parser.add_argument("--dry-run", action="store_true", help="run every check, change nothing")
    return parser


def _build_switchyard_present_parser() -> argparse.ArgumentParser:
    from scripts import team_launcher as launcher

    parser = argparse.ArgumentParser(
        prog="switchyard present",
        description="Map persistent project role sessions into stable display slots.",
    )
    parser.add_argument("project", help="registered project name or slug")
    actions = parser.add_subparsers(dest="action", required=True)
    list_parser = actions.add_parser("list", help="show desired and actual display state")
    list_parser.add_argument("--json", action="store_true", help="emit machine-readable presentation state")
    show_parser = actions.add_parser("show", help="move a role into a display slot")
    show_parser.add_argument("role")
    show_parser.add_argument("--slot", type=int, required=True)
    swap_parser = actions.add_parser("swap", help="swap two display slots")
    swap_parser.add_argument("slot_a", type=int)
    swap_parser.add_argument("slot_b", type=int)
    hide_parser = actions.add_parser("hide", help="hide the role in a display slot")
    hide_parser.add_argument("--slot", type=int, required=True)
    focus_parser = actions.add_parser("focus", help="select a display slot in the project viewer")
    focus_parser.add_argument("--slot", type=int, required=True)
    restore_parser = actions.add_parser("restore", help="restore a configured presentation layout")
    restore_parser.add_argument("--layout", default="default")
    bootstrap_parser = actions.add_parser("bootstrap", help="create stable slots and a presentation window")
    # The native window, not the nested tmux viewer. The viewer builds sessions
    # and opens nothing, so bootstrapping into it reported a recovery that
    # nobody could see; the separate layout is one terminal, laid out by the
    # terminal itself, with the project's slots in its tabs (SYRD-65).
    bootstrap_parser.add_argument(
        "--layout", choices=(launcher.LAYOUT_MODE_SEPARATE, launcher.LAYOUT_MODE_VIEWER), default=launcher.LAYOUT_MODE_SEPARATE
    )
    recover_parser = actions.add_parser("recover", help="make one bounded start/resume attempt for a role")
    recover_parser.add_argument("role")
    return parser


def _build_switchyard_attach_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard attach",
        description="Attach this terminal to a project role's live worker, by role name.",
    )
    parser.add_argument("project", help="registered project name or slug")
    parser.add_argument(
        "role",
        nargs="?",
        help="role to attach to; omit to list the project's roles and which are attachable",
    )
    parser.add_argument("--json", action="store_true", help="emit the role listing as JSON")
    return parser
