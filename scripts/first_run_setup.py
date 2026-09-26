"""What a new project's owner has to do before its roles can start, and whether it is done.

Before a role's panes come up, each provider it uses needs its own first run
done as the project owner. That means a login, the provider's one-time setup,
and, for Claude, agy and Codex, trusting the role's working directory. This
module covers:
- **Working out those steps** from the configuration and what is already on
  disk (`build_first_run_setup_manifest`).
- **Formatting** them for the operator.
- **Reading each provider's own trust record**, to tell whether a workdir is
  already trusted (`_workdir_is_trusted` and the per-provider probes).

Running the steps, and reading a provider's authentication state, is the auth
phase. That phase stays in `scripts/team_launcher.py` (`run_first_run_auth_phase`,
`_cli_auth_status`, `FIRST_RUN_AUTH_*`, ...), and this module reads what it needs
of it from `scripts.team_launcher` when a function runs.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-296). `team_launcher`
imports this module at its top and still exports every name callers read there.
This module never imports `team_launcher` at its top. `_workdir_is_trusted` is
defined here, but the manifest calls it as `launcher._workdir_is_trusted`,
because the suites patch it on the launcher to observe every trust probe.
"""

from __future__ import annotations

import os
import pwd
import shlex
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

if TYPE_CHECKING:
    from scripts.team_launcher import CodexHookTrustMismatch, OwnerShellIssue, ProjectConfig, RoleConfig


@dataclass(frozen=True)
class FirstRunAuthLoginStep:
    cli: str
    roles: tuple[str, ...]
    command: tuple[str, ...]


@dataclass(frozen=True)
class FirstRunProviderSetupStep:
    """One provider's account-wide first run, collected once for every role.

    Distinct from a login: the account can hold valid credentials and still
    open a theme or welcome flow the first time the CLI runs interactively,
    because that state lives beside the credentials rather than in them. It is
    the owner's, not a role's, so it is asked once however many roles use that
    provider (SYRD-191).
    """

    cli: str
    roles: tuple[str, ...]
    command: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class FirstRunFolderTrustStep:
    cli: str
    role: str
    workdir: Path
    command: tuple[str, ...]
    #: Every role that shares this worktree. Trust is directory-scoped, so one
    #: action covers all of them and the manifest says so rather than listing
    #: the same directory once per role.
    roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class FirstRunSetupManifest:
    owner_user: str
    login_steps: list[FirstRunAuthLoginStep]
    folder_trust_steps: list[FirstRunFolderTrustStep]
    stale_codex_hook_trust: list[CodexHookTrustMismatch]
    missing_cli_roles: dict[str, list[str]]
    owner_shell_issue: OwnerShellIssue | None = None
    provider_setup_steps: list[FirstRunProviderSetupStep] = field(default_factory=list)

    @property
    def has_steps(self) -> bool:
        return bool(
            self.login_steps
            or self.provider_setup_steps
            or self.folder_trust_steps
            or self.stale_codex_hook_trust
            or self.missing_cli_roles
            or self.owner_shell_issue
        )


FIRST_RUN_TRUST_CLIS = frozenset({"agy", "claude", "codex"})


def _owner_shell_issue(owner_user: str) -> OwnerShellIssue | None:
    from scripts import team_launcher as launcher

    try:
        info = pwd.getpwnam(owner_user)
    except KeyError:
        return None
    shell = str(getattr(info, "pw_shell", "") or "").strip()
    if not shell or os.access(shell, os.X_OK):
        return None
    fallback_shell = "/bin/bash" if os.access("/bin/bash", os.X_OK) else "/bin/sh"
    remedy = f"sudo usermod -s {fallback_shell} {shlex.quote(owner_user)}"
    return launcher.OwnerShellIssue(owner_user=owner_user, shell=shell, remedy=remedy)


def _roles_by_first_cli(config: ProjectConfig) -> dict[str, list[RoleConfig]]:
    from scripts import team_launcher as launcher

    grouped: dict[str, list[RoleConfig]] = {}
    for role in config.roles:
        cli = launcher._role_cli_name(role)
        if cli:
            grouped.setdefault(cli, []).append(role)
    return grouped


def _role_names(roles: Sequence[RoleConfig]) -> list[str]:
    return [role.role for role in roles]


def _provider_setup_reason(cli: str) -> str:
    if cli == "claude":
        return (
            "this account has not completed Claude's own first run (theme, then sign-in); "
            "measured on this host, that flow asks to sign in again even when the account "
            "already holds valid credentials, and it is what every pane opens until it is done"
        )
    return f"{cli} has not completed its first run for this account"


def _claude_workdir_is_trusted(owner_home: Path, workdir: Path) -> bool:
    from scripts import team_launcher as launcher

    config = launcher._read_json_object(owner_home / ".claude.json")
    projects = config.get("projects")
    if not isinstance(projects, dict):
        return False
    for path in _trust_path_candidates(workdir):
        entry = projects.get(path)
        if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True:
            return True
    return False


def _agy_workdir_is_trusted(owner_home: Path, workdir: Path) -> bool:
    from scripts import team_launcher as launcher

    settings = launcher._read_json_object(owner_home / ".gemini" / "antigravity-cli" / "settings.json")
    trusted = settings.get("trustedWorkspaces")
    if not isinstance(trusted, list):
        return False
    candidates = set(_trust_path_candidates(workdir))
    return any(str(path) in candidates for path in trusted)


def _git_common_dir_for(workdir: Path) -> Path | None:
    """The repository a directory belongs to, read without running git.

    A linked worktree's `.git` is a file holding `gitdir: <common>/worktrees/
    <name>`; a plain checkout's is the repository directory itself. Read rather
    than shelled out to, because this answers a question about somebody else's
    tree and has no business executing anything in it.
    """
    marker = workdir.expanduser() / ".git"
    try:
        if marker.is_dir():
            return marker.resolve(strict=False)
        text = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    gitdir = Path(text.split(":", 1)[1].strip()).expanduser()
    if not gitdir.is_absolute():
        gitdir = (workdir.expanduser() / gitdir).resolve(strict=False)
    parts = gitdir.parts
    if len(parts) >= 2 and parts[-2] == "worktrees":
        gitdir = Path(*parts[:-2])
    return gitdir


def _trust_path_candidates(workdir: Path) -> list[str]:
    """Every path Claude might have recorded this directory's trust under.

    Measured against Claude 2.1.270 on the live testing tenant: five role
    worktrees, none of them named in `projects`, all opening straight at a
    ready prompt -- because trust is recorded once for the REPOSITORY they are
    linked to, which is the only entry there beside the home:

        /home/testing-agent/.local/state/switchyard/projects/testing/control.git
            hasTrustDialogAccepted = True

    Reading only the worktree path said "untrusted", scheduled a trust step
    Claude never prompts for, and left the bounded watcher waiting for a key
    that was never going to appear (SYRD-191).
    """
    expanded = workdir.expanduser()
    candidates = [str(expanded), str(expanded.resolve(strict=False))]
    common = _git_common_dir_for(expanded)
    if common is not None:
        candidates.extend([str(common), str(common.resolve(strict=False))])
    return list(dict.fromkeys(candidates))


def _codex_trust_path_candidates(workdir: Path) -> list[str]:
    """Every path Codex honours this directory's trust under -- which is not Claude's list.

    Measured against Codex 0.156.1, one startup per case, past sign-in, in a
    throwaway CODEX_HOME (SYRD-279):

    - a linked worktree of an ordinary checkout is trusted by an entry for the
      worktree itself or for the checkout's root, and NOT by one for its `.git`
      directory -- the prompt says trusting "will apply to" the root;
    - a linked worktree of a bare repository -- every Switchyard role worktree,
      hung off `control.git` -- is trusted only by an entry for the worktree
      itself; neither the bare repository nor its parent counts.

    Reusing Claude's candidates, which include the git directory, would call a
    worktree trusted that Codex then stops at "Trust this folder?" for.
    """
    expanded = workdir.expanduser()
    candidates = [str(expanded), str(expanded.resolve(strict=False))]
    common = _git_common_dir_for(expanded)
    if common is not None and common.name == ".git":
        root = common.parent
        candidates.extend([str(root), str(root.resolve(strict=False))])
    return list(dict.fromkeys(candidates))


def _codex_workdir_is_trusted(owner_home: Path, workdir: Path) -> bool:
    """Whether Codex will open `workdir` at a prompt rather than "Trust this folder?".

    Codex records the answer in its own config as
    `[projects."<path>"] trust_level = "trusted"`; "untrusted" is also an answer,
    and not this one.
    """
    import tomllib

    try:
        config = tomllib.loads((owner_home / ".codex" / "config.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return False
    projects = config.get("projects")
    if not isinstance(projects, dict):
        return False
    for path in _codex_trust_path_candidates(workdir):
        entry = projects.get(path)
        if isinstance(entry, dict) and entry.get("trust_level") == "trusted":
            return True
    return False


def _workdir_is_trusted(cli: str, *, owner_home: Path, workdir: Path) -> bool:
    if cli == "claude":
        return _claude_workdir_is_trusted(owner_home, workdir)
    if cli == "agy":
        return _agy_workdir_is_trusted(owner_home, workdir)
    if cli == "codex":
        return _codex_workdir_is_trusted(owner_home, workdir)
    return True


def _first_run_trust_command(role: RoleConfig) -> list[str]:
    # Trust is directory-scoped, so the minimal interactive CLI is enough to collect it.
    # Detached roles do not have a visible pane where the prompt can appear.
    return list(role.cli)


def build_first_run_setup_manifest(
    config: ProjectConfig,
    *,
    owner_user: str,
    owner_home: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> FirstRunSetupManifest:
    from scripts import team_launcher as launcher

    roles_by_cli = _roles_by_first_cli(config)
    login_steps: list[FirstRunAuthLoginStep] = []
    missing_cli_roles: dict[str, list[str]] = {}

    for cli, roles in roles_by_cli.items():
        if cli not in launcher.FIRST_RUN_AUTH_STATUS_COMMANDS:
            continue
        auth_status = launcher._cli_auth_status(cli, owner_user=owner_user, owner_home=owner_home, runner=runner)
        if auth_status == "authenticated":
            continue
        names = _role_names(roles)
        if auth_status == "not_installed":
            missing_cli_roles[cli] = names
            continue
        login_steps.append(
            FirstRunAuthLoginStep(
                cli=cli,
                roles=tuple(names),
                command=tuple(launcher.FIRST_RUN_AUTH_LOGIN_COMMANDS[cli]),
            )
        )

    # One step per provider whose account-wide first run is unfinished, however
    # many roles use it: the theme and welcome flow belong to the owner's
    # account, not to a role, and asking once is what keeps three Claude panes
    # from each opening it (SYRD-191).
    provider_setup_steps: list[FirstRunProviderSetupStep] = []
    for cli, roles in roles_by_cli.items():
        if cli not in launcher.FIRST_RUN_SETUP_CLIS or cli in missing_cli_roles:
            continue
        if launcher._provider_account_setup_complete(cli, owner_home=owner_home):
            continue
        provider_setup_steps.append(
            FirstRunProviderSetupStep(
                cli=cli,
                roles=tuple(_role_names(roles)),
                command=tuple(launcher.FIRST_RUN_AUTH_LOGIN_COMMANDS.get(cli, [cli])[:1]),
                reason=_provider_setup_reason(cli),
            )
        )

    # Every configured role, not only the detached ones, and one action per
    # distinct worktree rather than one per role: trust is directory-scoped, so
    # roles that share a tree share the answer. A visible pane that has never
    # been trusted opens the provider's dialog instead of the ready prompt the
    # User was promised, which is what three Claude panes did (SYRD-191).
    folder_trust_steps: list[FirstRunFolderTrustStep] = []
    trust_index: dict[tuple[str, str], int] = {}
    for role in config.roles:
        cli = launcher._role_cli_name(role)
        if cli not in FIRST_RUN_TRUST_CLIS or cli in missing_cli_roles:
            continue
        workdir = Path(role.workdir)
        if launcher._workdir_is_trusted(cli, owner_home=owner_home, workdir=workdir):
            continue
        key = (cli, str(workdir.expanduser().resolve(strict=False)))
        existing = trust_index.get(key)
        if existing is not None:
            shared = folder_trust_steps[existing]
            folder_trust_steps[existing] = replace(shared, roles=(*shared.roles, role.role))
            continue
        trust_index[key] = len(folder_trust_steps)
        folder_trust_steps.append(
            FirstRunFolderTrustStep(
                cli=cli,
                role=role.role,
                workdir=workdir,
                command=tuple(_first_run_trust_command(role)),
                roles=(role.role,),
            )
        )

    return FirstRunSetupManifest(
        owner_user=owner_user,
        login_steps=login_steps,
        folder_trust_steps=folder_trust_steps,
        stale_codex_hook_trust=launcher.stale_codex_hook_trust_for_roles(config.roles, owner_home=owner_home),
        missing_cli_roles=missing_cli_roles,
        owner_shell_issue=_owner_shell_issue(owner_user),
        provider_setup_steps=provider_setup_steps,
    )


def _format_first_run_setup_manifest(manifest: FirstRunSetupManifest) -> list[str]:
    from scripts import team_launcher as launcher

    if not manifest.has_steps:
        return []
    missing_cli_count = len(manifest.missing_cli_roles)
    login_count = len(manifest.login_steps)
    folder_trust_count = len(manifest.folder_trust_steps)
    hook_trust_count = len(manifest.stale_codex_hook_trust)
    owner_shell_issue_count = 1 if manifest.owner_shell_issue else 0
    provider_setup_count = len(manifest.provider_setup_steps)
    # Every interactive step is counted, because the manifest is what an
    # operator reads to know what they are about to be asked (SYRD-191).
    summary = (
        f"switchyard: first-run setup manifest for owner user {manifest.owner_user}: "
        f"{login_count} login step(s), {provider_setup_count} provider setup step(s), "
        f"{folder_trust_count} folder trust step(s), "
        f"{hook_trust_count} codex hook approval(s), {missing_cli_count} missing CLI(s)"
    )
    if owner_shell_issue_count:
        summary = f"{summary}, {owner_shell_issue_count} owner shell issue(s)"
    lines = [
        summary
    ]
    if manifest.owner_shell_issue:
        issue = manifest.owner_shell_issue
        lines.append(
            f"switchyard: owner shell for {issue.owner_user} is not executable: {issue.shell}; "
            f"repair with `{issue.remedy}`"
        )
    for cli, roles in manifest.missing_cli_roles.items():
        lines.append(
            f"switchyard: missing CLI {cli} (affected roles: {', '.join(roles)}): "
            f"{launcher._missing_cli_install_clause(cli)}"
        )
    if manifest.missing_cli_roles:
        lines.append(launcher._owner_user_cli_reminder(manifest.owner_user))
    for step in manifest.login_steps:
        lines.append(
            f"switchyard: login {step.cli}: roles {', '.join(step.roles)}; "
            f"interactive account setup running {shlex.join(step.command)} as {manifest.owner_user}"
        )
    for step in manifest.provider_setup_steps:
        lines.append(
            f"switchyard: provider setup {step.cli}: roles {', '.join(step.roles)}; {step.reason}; "
            f"interactive first run of {shlex.join(step.command)} as {manifest.owner_user}, once "
            "for every role that uses it"
        )
    for step in manifest.folder_trust_steps:
        covered = ", ".join(step.roles or (step.role,))
        lines.append(
            f"switchyard: folder trust {step.cli}: role{'s' if len(step.roles or (step.role,)) > 1 else ''} "
            f"{covered} at {step.workdir}; "
            "recurs per project/workdir even when the owner user is reused; "
            "interactive repository trust today, not account login"
        )
    if manifest.stale_codex_hook_trust:
        lines.append(
            "switchyard: manual security approval: "
            + launcher._format_codex_hook_trust_report(
                manifest.stale_codex_hook_trust,
                owner_user=manifest.owner_user,
            )
        )
    return lines


def print_first_run_setup_manifest(
    manifest: FirstRunSetupManifest,
    *,
    print_func: Callable[[str], None] = print,
) -> None:
    for line in _format_first_run_setup_manifest(manifest):
        print_func(line)
