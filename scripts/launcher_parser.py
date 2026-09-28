"""The `team-launcher` entry point's argument parser.

`_build_parser` declares every verb `team-launcher` accepts, with its
arguments, choices, defaults and help, for `main` to parse argv with.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-428). The launcher
imports this module and re-exports it; `main` still builds the parser through
the launcher's name. The names it reads -- the worktree policies, the pane
state directory, the layout modes and the launcher's own file, which the
default script path sits beside -- are read through the launcher when the
parser is built, so a suite that rebinds one there still intercepts it. This
module imports `team_launcher` only inside the function, when it runs.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    from scripts import team_launcher as launcher

    parser = argparse.ArgumentParser(description="Launch or reload a JSON-configured project team.")
    parser.add_argument("project", help="project name, for example pgu")
    parser.add_argument(
        "command",
        nargs="?",
        default="start",
        choices=[
            "start",
            "attach",
            "reload",
            "stop",
            "design",
            "new",
            "provision-runtime",
            "deploy-launcher",
            "upgrade",
            "add-role",
            "set-vcs-close-role",
            "pane",
            "teardown",
        ],
        help="start is idempotent attach-or-start (resumes tracked session ids when relaunching a stopped pane); reload force-restarts running CLIs with tracked resume ids",
    )
    parser.add_argument("pane_mode", nargs="?")
    parser.add_argument("role", nargs="?")
    parser.add_argument("--slot", type=int, help="layout slot for `pane attach-role <role>`")
    parser.add_argument("--config", type=Path, help="project launcher config JSON")
    parser.add_argument("--layout-output", type=Path, help="write generated Konsole layout here")
    parser.add_argument(
        "--layout",
        choices=sorted(launcher.LAYOUT_MODE_CHOICES),
        default=launcher.LAYOUT_MODE_AUTO,
        help="window layout mode: auto detects the invoking desktop, separate keeps the KDE/Konsole path, viewer forces the tmux viewer",
    )
    parser.add_argument("--script-path", type=Path, default=Path(launcher.__file__).resolve().with_name("team-launcher"))
    parser.add_argument("--pane-state-dir", type=Path, help=f"write initial pane idle state here (default: {launcher.DEFAULT_PANE_STATE_DIR})")
    parser.add_argument("--no-attach", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true", help="print launch plan without starting Konsole")
    parser.add_argument("--confirm", help="project slug required for destructive teardown")
    parser.add_argument(
        "--drop-nonempty-board",
        action="store_true",
        help="allow teardown to drop a board database that still contains tickets",
    )
    parser.add_argument(
        "--destroy-registered-tenant",
        action="store_true",
        help="allow teardown to remove a registered launchable tenant; off by default",
    )
    parser.add_argument(
        "--remove-owner-home",
        action="store_true",
        help="teardown may remove /home/<owner>; off by default because it can contain user work",
    )
    parser.add_argument("--remove-owner-user", action="store_true", help="teardown may remove the owner Unix account")
    parser.add_argument("--owner-user", help="new project owner Unix user (default: <project>-agent)")
    parser.add_argument("--port", type=int, help="new project board port; omitted means deterministic allocation")
    parser.add_argument("--database", help="new project PostgreSQL database; omitted means <project>_ticket_board")
    parser.add_argument("--source-repo", type=Path, help="Switchyard source checkout or exported release to deploy")
    parser.add_argument(
        "--commit-git-dir",
        help="git repository path, or colon-separated paths, used to verify board commit hashes",
    )
    parser.add_argument("--repository", type=Path, help="project working checkout opened by generated panes")
    parser.add_argument("--from", dest="from_artifact", type=Path, help="project artifact emitted by the design command")
    parser.add_argument("--new-output-dir", type=Path, help="write new-project artifacts here")
    parser.add_argument("--design-output-dir", type=Path, help="write design document and project artifact here")
    parser.add_argument("--project-artifact", type=Path, help="write project artifact here during design")
    parser.add_argument("--design-document", type=Path, help="write project design document here during design")
    parser.add_argument("--design-title", help="project design document title")
    parser.add_argument("--design-body", help="project design document body")
    parser.add_argument("--remote", help="project git remote name for generated launcher config")
    parser.add_argument("--default-branch", help="project default branch for generated launcher config")
    parser.add_argument("--worktree-policy", choices=sorted(launcher.WORKTREE_POLICIES), help="shared or isolated role worktrees")
    parser.add_argument("--ticket-prefix", help="ticket id prefix for the new board, e.g. OTTO")
    parser.add_argument("--upstream-report-url", help="board URL where tenant reports should be filed")
    parser.add_argument("--upstream-report-token-file", help="0600 file containing the report-only token for --upstream-report-url")
    parser.add_argument("--push-policy", help="reviewable push policy label recorded in the design artifact")
    parser.add_argument("--audit-signoff", action=argparse.BooleanOptionalAction, default=None, help="record whether audit signoff is a project gate")
    parser.add_argument("--needs-inspection", action=argparse.BooleanOptionalAction, default=None, help="record whether inspection is a project gate")
    parser.add_argument("--needs-user-signoff", action=argparse.BooleanOptionalAction, default=None, help="record whether user signoff is a project gate")
    parser.add_argument("--board-service-traversal", action=argparse.BooleanOptionalAction, default=None, help="record whether boardsvc may traverse the owner home")
    parser.add_argument("--supplementary-group", action="append", dest="supplementary_groups", help="owner-user supplementary group to record; repeat as needed")
    parser.add_argument("--linger", action=argparse.BooleanOptionalAction, default=None, help="record whether linger should be enabled")
    parser.add_argument("--owner-shell", help="owner user's shell to record")
    parser.add_argument("--execute", action="store_true", help="execute new-project provisioning after precheck")
    parser.add_argument("--runtime-user", help="local user whose lingering /run/user/<uid> runtime should be provisioned")
    parser.add_argument("--launcher-repo", type=Path, help="launcher checkout to update or verify (default: this script's repo)")
    parser.add_argument("--deploy-ref", default=None, help="board release ref to deploy during upgrade (default: the pinned release, else origin/main)")
    parser.add_argument("--cli", dest="add_role_cli", default="codex", help="CLI runtime for `add-role` (default: codex)")
    parser.add_argument("--audit", dest="add_role_audit", action="store_true", help="add the role as an auditor instead of an implementer")
    parser.add_argument("--detached", action="store_true", help="configure `add-role` as headless instead of visible")
    parser.add_argument("--relayout", action="store_true", help="replace the existing layout with a generated layout when adding a visible role")
    parser.add_argument("--clean-launcher", action="store_true", help="run git clean -fdx after updating --launcher-repo")
    parser.add_argument("--force", action="store_true", help="allow reload to kill/relaunch even if live command validation fails")
    parser.add_argument("--allow-stale-launcher", action="store_true", help="emergency override: warn but proceed when the launcher checkout is provably stale")
    parser.add_argument("--no-launcher-self-deploy", action="store_true", help="restore refuse-only behavior instead of automatically fast-forwarding a stale launcher checkout")
    parser.add_argument("--skip-launcher-check", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--desktop-policy", type=Path, help="approved Wayland JSON policy or headless; installed before launch")
    parser.add_argument("--workflow-config", type=Path, help="declarative roles/stages JSON for the new project")
    return parser
