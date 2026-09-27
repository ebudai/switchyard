"""Who a tenant's owner is, from root's own record and the kernel -- never from anything the tenant can write.

- `TrustedOwnerIdentity` is the answer: the owner's account, home, uid and gid,
  or the problems that kept root from naming one (`trusted` is their absence).
- `trusted_owner_identity` derives it from root's baseline plan, only once that
  plan is shown to be root-controlled, and from the host's account records, and
  refuses when the two disagree.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-381), in their original
order. The launcher imports this module and re-exports both names, so every
privileged command that reads the trusted owner through the launcher -- GitHub
identity, pane rebind, repository-boundary repair, resume-provision, release
root preparation, workflow adoption -- and every suite that patches it there
reach the same objects. Every launcher facility `trusted_owner_identity` uses,
and the result class it builds, is read from `team_launcher` when it runs, as it
was, so a patch on the launcher still intercepts. `TrustedOwnerIdentity`'s
`dataclass` decorator is bound when this module loads, as it was when the
launcher loaded it. The standard-library names (`json`, `pwd`, `Path`) are this
module's own imports, the same objects. This module never imports
`team_launcher` at its top.
"""

from __future__ import annotations

import json
import pwd
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TrustedOwnerIdentity:
    """Who a tenant's owner is, taken from root's own record and the kernel."""

    owner_user: str
    owner_home: Path
    owner_uid: int
    owner_gid: int
    problems: tuple[str, ...] = ()

    @property
    def trusted(self) -> bool:
        return not self.problems


def trusted_owner_identity(project: str) -> TrustedOwnerIdentity:
    """The owner root will act for, derived from things the tenant cannot write.

    The tenant's configuration and its plan are both writable by the account
    every role runs as, so neither can say whose SSH state a root command
    modifies: a role could point `run_as_user` or `owner_home` somewhere else
    between the operator deciding to run this and the command reading it. Root's
    own baseline plan lives in a directory only root can write, and passwd is the
    kernel's. Both are consulted, and they have to agree (SYRD-100 review).
    """
    from scripts import team_launcher as launcher

    baseline = launcher.privileged_baseline_plan_path(project)
    problems: list[str] = []
    walk = launcher.root_controlled_problems_for(str(baseline))
    if walk:
        return launcher.TrustedOwnerIdentity("", Path(), -1, -1, tuple(
            [f"{baseline} is not root-controlled, so it cannot say who this tenant's owner is"] + walk
        ))
    try:
        recorded = json.loads(baseline.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return launcher.TrustedOwnerIdentity("", Path(), -1, -1, (f"{baseline} could not be read: {exc}",))
    if not isinstance(recorded, dict):
        return launcher.TrustedOwnerIdentity("", Path(), -1, -1, (f"{baseline} is not a plan document",))
    owner_user = str(recorded.get("owner_user") or "").strip()
    recorded_home = str(recorded.get("owner_home") or "").strip()
    if not owner_user:
        problems.append(f"{baseline} records no owner_user")
    if not recorded_home:
        problems.append(f"{baseline} records no owner_home")
    if problems:
        return launcher.TrustedOwnerIdentity("", Path(), -1, -1, tuple(problems))
    owner_uid = launcher.uid_for_user(owner_user)
    owner_home = launcher.home_dir_for_user(owner_user)
    if owner_uid is None or owner_home is None:
        return launcher.TrustedOwnerIdentity(
            "", Path(), -1, -1, (f"{owner_user} is not an account on this host",)
        )
    # The kernel's answer and root's record have to be the same answer. A
    # divergence is not something to pick a winner from.
    if owner_home != Path(recorded_home):
        return launcher.TrustedOwnerIdentity(
            "", Path(), -1, -1,
            (
                f"{baseline} records {owner_user}'s home as {recorded_home}, and this host says "
                f"{owner_home}. Nothing was changed: which one is right is not this command's "
                "to decide.",
            ),
        )
    try:
        owner_gid = int(pwd.getpwuid(owner_uid).pw_gid)
    except KeyError:
        owner_gid = owner_uid
    return launcher.TrustedOwnerIdentity(owner_user, owner_home, owner_uid, owner_gid)
