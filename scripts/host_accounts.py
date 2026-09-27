"""Host account lookups with no dependencies, for modules below the launcher.

`home_dir_for_user` moved here unchanged from `scripts/team_launcher.py`
(SYRD-288). `scripts/upstream_report.py` binds it as a default argument when its
functions are defined, which needs it before the launcher exists. `team_launcher`
imports it from here, so `team_launcher.home_dir_for_user` is the same object,
and patches on the launcher still reach every launcher caller.

`local_account_exists` moved here unchanged from `scripts/team_launcher.py`
(SYRD-355) for the same reason: `scripts/project_role_add.py` binds it as
`add_project_role_command`'s `account_exists` default. The launcher imports it
from here, so `team_launcher.local_account_exists` is the same object and the
launcher's own callers still read it through the launcher.
"""

from __future__ import annotations

import pwd
from pathlib import Path


def home_dir_for_user(user_name: str) -> Path | None:
    user = user_name.strip()
    if not user:
        return None
    try:
        return Path(pwd.getpwnam(user).pw_dir)
    except KeyError:
        return Path("/home") / user


def local_account_exists(account: str) -> bool:
    """Whether a Unix account exists on this host."""
    name = (account or "").strip()
    if not name:
        return False
    try:
        pwd.getpwnam(name)
    except KeyError:
        return False
    return True
