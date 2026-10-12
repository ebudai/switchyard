"""The Git identity a role's pane commits as (SYRD-458).

`_with_project_board_env` gives every role this beside its board environment.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig, RoleConfig


def _git_quote(value: str) -> str:
    """One `GIT_CONFIG_PARAMETERS` word, quoted the way Git's own `-c` writes it."""
    return "'" + value.replace("'", "'\\''") + "'"


def role_git_identity_env(config: ProjectConfig, role: RoleConfig) -> dict[str, str]:
    """The Git author and committer a role's pane commits as (SYRD-458).

    Every worktree of one repository reads that repository's single config, so
    one worker's `git config user.name` became every worker's identity: on MEFP
    each luna worker committed as `MEFP luna-2`. A pane carries its role's
    identity as command-line config instead, the same scope `git -c` sets:
    - above any repository's or user's config, so a shared one cannot rename it;
    - below an explicit `git -c` or `GIT_AUTHOR_*`, so Switchyard's own commits
      (`owner_git`), and anything else that names its identity that way, still
      win;
    - never over what a commit already records: cherry-pick, rebase and amend
      keep the original author, and only the committer is the role that ran
      them.
    The address is under `.invalid`, which never routes. A role's own
    `GIT_CONFIG_PARAMETERS` is kept, after this, so its settings win.
    """
    # Git drops angle brackets and line breaks from an identity by itself. Folding
    # whitespace here keeps a role name readable and its address free of spaces.
    name = " ".join(f"{config.ticket_prefix or config.project.upper()} {role.role}".split())
    email = "".join(f"{role.role}@{config.project}.switchyard.invalid".split())
    words = [_git_quote(f"user.name={name}"), _git_quote(f"user.email={email}")]
    own = role.env.get("GIT_CONFIG_PARAMETERS", "").strip()
    return {"GIT_CONFIG_PARAMETERS": " ".join(words + ([own] if own else []))}
