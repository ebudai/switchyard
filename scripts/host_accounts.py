"""Host account lookups with no dependencies, for modules below the launcher.

`home_dir_for_user` moved here unchanged from `scripts/team_launcher.py`
(SYRD-288). `scripts/upstream_report.py` binds it as a default argument when its
functions are defined, which needs it before the launcher exists. `team_launcher`
imports it from here, so `team_launcher.home_dir_for_user` is the same object,
and patches on the launcher still reach every launcher caller.
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
