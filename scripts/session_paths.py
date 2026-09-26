"""Where a role's session records and pane state live, and the pane idle state a new pane starts with.

- **Session directories:** `role_session_dir` is where a role's provider
  session records are kept. It is an explicit directory from the
  environment, the account's own session directory (`account_session_dir`),
  or the owner's default (`default_session_dir_for_user`).
  `session_dir_uses_user_runtime` tells whether a directory is under a
  user's runtime dir.
- **Pane-state directories:** `role_pane_state_dir` is where a role's pane
  writes its idle/busy state. It is the environment's, the shared per-project
  one (`shared_pane_state_dir`), or the owner's default
  (`default_pane_state_dir_for_user`).
- **Initial idle state:** `seed_initial_pane_idle_state` writes the state a
  new pane starts with, and `clear_pane_idle_state_for_role` removes it.

The process-wide defaults `DEFAULT_SESSION_DIR` and `DEFAULT_PANE_STATE_DIR`
(and `LIVE_PGU_STATE_DIR_NAME`, which builds the first) stay in
`scripts/team_launcher.py`. The suites rebind them there, and other modules
read them there, so this module reads them there too, when a function runs --
as it does the account and runtime lookups and the JSON writer.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-311). `team_launcher`
imports this module at its top and still exports every name callers read there.
This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig, RoleConfig


def _explicit_session_dir_from_env() -> Path | None:
    from scripts import team_launcher as launcher

    value = launcher._env_first("TICKET_BOARD_PANE_SESSION_DIR", "PGU_TICKET_BOARD_PANE_SESSION_DIR")
    return Path(value).expanduser() if value else None


def _explicit_pane_state_dir_from_env() -> Path | None:
    from scripts import team_launcher as launcher

    value = launcher._env_first("TICKET_BOARD_PANE_STATE_DIR", "PGU_TICKET_BOARD_PANE_STATE_DIR")
    return Path(value).expanduser() if value else None


def default_session_dir_for_user(user_name: str) -> Path:
    from scripts import team_launcher as launcher

    explicit = _explicit_session_dir_from_env()
    if explicit is not None:
        return explicit
    owner_home = launcher.home_dir_for_user(user_name)
    if owner_home is None:
        return launcher.DEFAULT_SESSION_DIR
    return owner_home / ".local" / "state" / launcher.LIVE_PGU_STATE_DIR_NAME / "pane-sessions"


def default_pane_state_dir_for_user(user_name: str, *, project: str) -> Path:
    from scripts import team_launcher as launcher

    explicit = _explicit_pane_state_dir_from_env()
    if explicit is not None:
        return explicit
    user = user_name.strip()
    if not user:
        return launcher.DEFAULT_PANE_STATE_DIR
    uid = launcher.uid_for_user(user)
    if uid is None:
        return launcher.DEFAULT_PANE_STATE_DIR
    return launcher.runtime_dir_for_uid(uid) / f"{project}-ticket-board" / "pane-state"


def account_session_dir(account: str, *, project: str) -> Path:
    """Session records for one account of one project.

    Deliberately ignores the ambient TICKET_BOARD_PANE_SESSION_DIR: that names
    the CURRENT pane's project and user, so honouring it when computing another
    role's or another project's path hands a role a directory belonging to
    something else. It is also project-scoped, which the owner-facing helper is
    not -- that one still hardcodes the legacy pgu state directory (SYRD-39).
    """
    from scripts import team_launcher as launcher

    home = launcher.home_dir_for_user(account)
    if home is None:
        return launcher.DEFAULT_SESSION_DIR
    return home / ".local" / "state" / f"{project}-ticket-board" / "pane-sessions"


def shared_pane_state_dir(project: str) -> Path:
    """Where every role of a project records pane activity.

    Role accounts cannot write the owner's XDG runtime directory, and the notify
    listener cannot read theirs, so per-role state under each role's own
    /run/user is written where nobody reads it. This is the deliberate
    aggregation path: the board's runtime directory, group-owned by the
    project's roles group so every role writes it and the listener reads it
    (SYRD-39).
    """
    return Path("/run") / f"{project}-ticket-board" / "pane-state"


def session_dir_uses_user_runtime(session_dir: Path, user_name: str) -> bool:
    from scripts import team_launcher as launcher

    uid = launcher.uid_for_user(user_name)
    if uid is None:
        return False
    runtime_dir = launcher.runtime_dir_for_uid(uid).resolve(strict=False)
    session_path = session_dir.resolve(strict=False)
    return session_path == runtime_dir or runtime_dir in session_path.parents


def role_session_dir(config: ProjectConfig, role: RoleConfig) -> Path:
    """The role-private resumable store under the project account."""
    from scripts import team_launcher as launcher

    # Each logical role retains an independent resumable store even though all
    # role processes now share the project account.
    if config.role_state_isolation:
        return config.session_dir / "roles" / role.role
    account = launcher.role_run_as_user(config, role)
    if account and account != config.run_as_user:
        return launcher.account_session_dir(account, project=config.project)
    return config.session_dir


def role_pane_state_dir(
    config: ProjectConfig, role: RoleConfig, default: Path | None = None
) -> Path:
    """Where pane hooks aggregate state across the active identity model."""
    from scripts import team_launcher as launcher

    account = launcher.role_run_as_user(config, role)
    if not config.role_state_isolation and account and account != config.run_as_user:
        return shared_pane_state_dir(config.project)
    if default is not None:
        return default
    return default_pane_state_dir_for_user(config.run_as_user, project=config.project)


def seed_initial_pane_idle_state(
    role: RoleConfig,
    *,
    pane_state_dir: Path,
    source: str,
    now: float | None = None,
) -> Path:
    from scripts import team_launcher as launcher

    path = pane_state_dir / launcher.pane_state_file_name(role.target)
    payload = {
        "target": role.target,
        "state": "idle",
        "updated_at": time.time() if now is None else now,
        "source": source,
    }
    launcher._write_json_atomic(path, payload)
    return path


def clear_pane_idle_state_for_role(role: RoleConfig, *, pane_state_dir: Path) -> None:
    from scripts import team_launcher as launcher

    path = pane_state_dir / launcher.pane_state_file_name(role.target)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        print(f"team-launcher: failed to clear pane state for {role.role}: {exc}", file=sys.stderr)
