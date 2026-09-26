"""The hooks that run in a role's pane: installing and refreshing them, and whether Codex trusts them.

Every role pane runs the board's pane hooks, which report activity and turn
boundaries to the ticket board. This module covers:
- **Installing** a generated project's hooks as the project owner, from the
  staged tooling (`ensure_generated_project_pane_hooks`).
- **Refreshing** each role account's hooks at upgrade
  (`refresh_role_pane_hooks`).
- **Codex hook trust.** Codex runs a command hook only while the hash it
  trusted matches the installed hook. `stale_codex_hook_trust_for_roles` finds
  the Codex roles whose trust is stale, and the report helpers name them in
  first-run setup.

Hashing and reading Codex's own trust records stays in
`scripts/ticket_board/codex_hook_trust.py`, imported here directly. The tmux
hooks that relayout a viewer are presentation, and repository (pre-commit)
hooks are `scripts/repository_hooks.py`.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-294). `team_launcher`
imports this module at its top and still exports every name callers read there.
The suites patch `ensure_generated_project_pane_hooks` and
`refresh_role_pane_hooks` on the launcher, around the launcher's own call
sites. This module never imports `team_launcher` at its top. Launcher
facilities (`uid_for_user`, `runtime_dir_for_uid`, `current_user_name`,
`home_dir_for_user`, `role_run_as_user`, `_staged_tooling_dir`, ...) are read
from `scripts.team_launcher` when a function runs, so the suites' patches there
still reach them.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

from scripts.ticket_board.codex_hook_trust import (
    codex_command_hook_trust_entries as _codex_command_hook_trust_entries,
    codex_trusted_hashes as _codex_trusted_hashes,
)

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig, RoleConfig


def install_generated_project_pane_hooks_args(
    config: ProjectConfig,
    *,
    script_path: Path,
    pane_state_dir: Path,
) -> list[str]:
    from scripts import team_launcher as launcher

    if not config.run_as_user:
        raise ValueError("pane hook repair requires run_as_user")
    owner_home = launcher.home_dir_for_user(config.run_as_user) or Path("/home") / config.run_as_user
    launcher_path = (config.pane_launcher or script_path).expanduser()
    hook_installer = launcher_path.with_name("ticket-board-install-pane-hooks")
    hook_source = launcher_path.with_name("ticket-board-pane-idle-hook")
    hook_bin = owner_home / ".local" / "bin" / "ticket-board-pane-idle-hook"
    uid = launcher.uid_for_user(config.run_as_user)
    if uid is None:
        raise SystemExit(f"team-launcher: cannot install pane hooks for unknown user {config.run_as_user!r}")
    env_args = [
        "env",
        f"XDG_RUNTIME_DIR={launcher.runtime_dir_for_uid(uid)}",
        f"TICKET_BOARD_PROJECT={config.project}",
        f"TICKET_BOARD_PANE_STATE_DIR={pane_state_dir}",
        f"TICKET_BOARD_PANE_SESSION_DIR={config.session_dir}",
        str(hook_installer),
        "install",
        "--home",
        str(owner_home),
        "--hook-source",
        str(hook_source),
        "--bin-path",
        str(hook_bin),
    ]
    if launcher.current_user_name() == config.run_as_user:
        return env_args
    return ["sudo", "-u", config.run_as_user, "-H", *env_args]


def ensure_generated_project_pane_hooks(
    config: ProjectConfig,
    *,
    config_path: Path,
    script_path: Path,
    pane_state_dir: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    from scripts import team_launcher as launcher

    if not config.run_as_user or not launcher._is_generated_project_layout_template(config, config_path=config_path):
        return
    args = install_generated_project_pane_hooks_args(
        config,
        script_path=script_path,
        pane_state_dir=pane_state_dir,
    )
    result = runner(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        reason = launcher._proc_failure_reason(result, f"installer failed with exit {result.returncode}")
        raise SystemExit(f"team-launcher: failed to install pane hooks for {config.project}: {reason}")


@dataclass(frozen=True)
class CodexHookTrustMismatch:
    event: str
    hook_key: str
    expected_hash: str
    trusted_hash: str
    affected_roles: tuple[str, ...]


def stale_codex_hook_trust_for_roles(
    roles: Sequence[RoleConfig],
    *,
    owner_home: Path,
) -> list[CodexHookTrustMismatch]:
    from scripts import team_launcher as launcher

    codex_roles = [role for role in roles if launcher._role_cli_name(role) == "codex"]
    if not codex_roles:
        return []
    hook_entries = _codex_command_hook_trust_entries(owner_home)
    if not hook_entries:
        return []
    trusted_hashes = _codex_trusted_hashes(owner_home)
    affected_roles = tuple(launcher._role_names(codex_roles))
    mismatches: list[CodexHookTrustMismatch] = []
    for event_key, hook_key, expected_hash in hook_entries:
        trusted_hash = trusted_hashes.get(hook_key, "")
        if trusted_hash == expected_hash:
            continue
        mismatches.append(
            CodexHookTrustMismatch(
                event=event_key,
                hook_key=hook_key,
                expected_hash=expected_hash,
                trusted_hash=trusted_hash,
                affected_roles=affected_roles,
            )
        )
    return mismatches


def _codex_hook_trust_reason(mismatch: CodexHookTrustMismatch) -> str:
    if mismatch.trusted_hash:
        return "hooks changed, re-approve"
    return "new project, never trusted"


def _codex_hook_trust_affected_roles(mismatches: Sequence[CodexHookTrustMismatch]) -> list[str]:
    roles: list[str] = []
    for mismatch in mismatches:
        for role in mismatch.affected_roles:
            if role not in roles:
                roles.append(role)
    return roles


def _format_codex_hook_trust_report(
    mismatches: Sequence[CodexHookTrustMismatch],
    *,
    owner_user: str,
) -> str:
    count = len(mismatches)
    plural = "" if count == 1 else "s"
    owner = f"owner user {owner_user}" if owner_user else "the project owner user"
    roles = ", ".join(_codex_hook_trust_affected_roles(mismatches)) or "none"
    approvals = "; ".join(f"{mismatch.event} ({_codex_hook_trust_reason(mismatch)})" for mismatch in mismatches)
    return (
        f"codex hook trust needs {count} approval{plural} for {owner}; "
        f"run /hooks once in any Codex pane as that owner. "
        f"This trust is shared by affected Codex roles: {roles}. "
        f"Approvals: {approvals}"
    )


def tenant_hook_accounts(config: ProjectConfig) -> list[str]:
    """Every account whose CLI configuration this tenant's roles read.

    Modern tenants run every role as the project account, so this is usually
    one name; a tenant still on per-role accounts has one per role.
    """
    from scripts import team_launcher as launcher

    accounts: list[str] = []
    for name in [config.run_as_user or "", *(launcher.role_run_as_user(config, role) for role in config.roles)]:
        name = str(name or "").strip()
        if name and name not in accounts:
            accounts.append(name)
    return accounts


def refresh_role_pane_hooks(
    config: ProjectConfig,
    *,
    staging_root: Path | None = None,
    dry_run: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> list[str]:
    """Re-run the pane-hook installer in every account this tenant's roles use.

    Staging refreshes the root-owned tooling; it does not touch the per-account
    CLI configuration that points at it. Those registrations were written when
    the account was created and never again, so an existing tenant keeps
    exactly the set of hooks its provisioning run happened to write -- and can
    never gain a new one. That is how a tenant would take this release and
    still stall on a permission prompt, with the helper that answers it staged
    and unreferenced (SYRD-234).

    The installer merges rather than replaces and is safe to run repeatedly, so
    this is idempotent by construction: it is the same program provisioning
    runs, pointed at an account that already exists.
    """
    from scripts import team_launcher as launcher

    staged = launcher._staged_tooling_dir(config, staging_root)
    installer = staged / "ticket-board-install-pane-hooks"
    hook_source = staged / "ticket-board-pane-idle-hook"
    problems: list[str] = []
    for account in tenant_hook_accounts(config):
        home = launcher.home_dir_for_user(account)
        if home is None:
            problems.append(f"{account} is not an account on this host, so its pane hooks were not refreshed")
            continue
        if dry_run:
            print_func(
                f"switchyard: would refresh {config.project}'s pane hooks for {account} in {home}, "
                f"from {installer}; nothing written"
            )
            continue
        args = [
            "env",
            f"TICKET_BOARD_PROJECT={config.project}",
            f"TICKET_BOARD_PANE_SESSION_DIR={config.session_dir}",
            str(installer),
            "install",
            "--home",
            str(home),
            "--hook-source",
            str(hook_source),
            "--bin-path",
            str(home / ".local" / "bin" / "ticket-board-pane-idle-hook"),
        ]
        # As the account, never as root: these files are the account's own, and
        # root writing them leaves what the account cannot rewrite afterwards.
        if launcher.current_user_name() != account:
            args = ["sudo", "-u", account, "-H", *args]
        result = runner(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if getattr(result, "returncode", 1) != 0:
            detail = (str(getattr(result, "stderr", "") or "").strip() or "no output")[:400]
            problems.append(
                f"could not refresh {config.project}'s pane hooks for {account} "
                f"(exit {result.returncode}): {detail}"
            )
            continue
        print_func(f"switchyard: refreshed {config.project}'s pane hooks for {account} in {home}")
    return problems
