"""A tenant's own upgrade actions, prompt-free for its owner account (SYRD-825).

The shipped policy makes the tenant upgrade actions `allow_active=yes`: no prompt, but only from a process polkit
judges to be in an active session. A Director's CLI can run its commands where polkit does not (a tool sandbox),
so the same upgrade asked a password on one tenant and nothing on another. This per-tenant rule answers YES for:

- one account, the tenant's own owner;
- four actions, the catalogue's tenant upgrade actions (`TENANT_UPGRADE_ACTIONS`), never the shared-release or
  database-auth actions, which stay `auth_admin`;
- one program, the root-owned privileged helper, which pkexec names in the `program` detail.

It widens who REACHES the helper, never who it acts for: the helper still walks its own ancestry to the caller's
pane, refuses any process that is not the project's registered control pane, and journals each run.

`render` is installed beside the board's deploy rule, by provisioning and by the root upgrade (`ensure`).
"""

from __future__ import annotations

import errno
import json
import os
import re
import stat
from pathlib import Path

try:
    from . import privileged_actions, privileged_install
except ImportError:  # pragma: no cover - supports direct script execution
    import privileged_actions
    import privileged_install

#: The catalogue's tenant upgrade actions, each already `allow_active=yes` in the shipped policy.
TENANT_UPGRADE_ACTIONS = ("preview-upgrade", "upgrade-tenant-release", "deploy-release", "upgrade-tenant")
#: An owner name the rule may carry: a plain local account name, so nothing in it can end the JS string it is in.
OWNER_NAME = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
#: A project the rule may name: the launcher's slug rule, so nothing in it can end a comment line or a string of the
#: rule polkitd runs as root (SYRD-830).
PROJECT_SLUG = re.compile(r"[a-z0-9][a-z0-9_]{0,39}")
RULE_MODE = 0o644


def rule_name(project: str) -> str:
    return f"49-{project}-tenant-upgrade.rules"


def refuse_unsafe_names(project: str, owner_user: str) -> None:
    """Refuse, before a polkit rule is written, a project or owner that could leave the comment or string it is in.
    Every per-tenant rule polkitd runs as root is rendered behind this (SYRD-825, SYRD-830)."""
    if not PROJECT_SLUG.fullmatch(project or ""):
        raise ValueError(f"project {project!r} is not a project slug; no polkit rule names it")
    if not OWNER_NAME.fullmatch(owner_user or ""):
        raise ValueError(f"{project}'s owner {owner_user!r} is not a plain account name; no polkit rule names it")


def render(project: str, owner_user: str) -> str:
    """The rule, with the owner and every other value written as a JSON string literal, never spliced raw."""
    refuse_unsafe_names(project, owner_user)
    actions = json.dumps([f"{privileged_actions.ACTION_NAMESPACE}.{name}" for name in TENANT_UPGRADE_ACTIONS])
    return f"""// Let {owner_user} run {project}'s own upgrade actions without a prompt (SYRD-825).
// Only these actions, only through the root-owned privileged helper. The helper still refuses any process that
// is not {project}'s registered control pane. The shared-release and database-auth actions are not here.
polkit.addRule(function(action, subject) {{
    if (subject.user !== {json.dumps(owner_user)}) {{
        return polkit.Result.NOT_HANDLED;
    }}
    if (action.lookup("program") !== {json.dumps(str(privileged_install.helper_path()))}) {{
        return polkit.Result.NOT_HANDLED;
    }}
    if ({actions}.indexOf(action.id) < 0) {{
        return polkit.Result.NOT_HANDLED;
    }}
    return polkit.Result.YES;
}});
"""


def ensure(project: str, owner_user: str, *, rules_dir: Path, dry_run: bool, euid: int | None = None) -> str:
    """Install or confirm the rule as root; say what it found. Never follows a link, never reads more than it writes.

    Run as the tenant it cannot read root's rules directory, so it says the root upgrade installs the rule, and does
    not count that against anything (SYRD-825)."""
    euid = os.geteuid() if euid is None else euid
    if not owner_user:
        return f"switchyard: {project} names no owner account, so no tenant-upgrade polkit rule is installed for it"
    try:
        body = render(project, owner_user).encode()
    except ValueError as exc:
        return f"switchyard: {exc}"
    path = rules_dir / rule_name(project)
    upgrade = f"switchyard privileged-action {project} upgrade-tenant-release commit=<release commit>"
    if not rules_dir.is_dir():
        return f"switchyard: this host has no {rules_dir}, so {project}'s tenant-upgrade polkit rule is not installed"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        current = None
    except OSError as exc:
        if exc.errno == errno.EACCES and euid != 0:
            return (f"switchyard: note: {project}'s tenant-upgrade polkit rule {path} can only be read by root; the "
                    f"root upgrade installs it if it is missing or differs: `{upgrade}`")
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            return f"switchyard: {path} is a symlink; {project}'s tenant-upgrade polkit rule is not written through it"
        return f"switchyard: {path} cannot be read ({exc.strerror}); {project}'s tenant-upgrade rule is unchanged"
    else:
        try:
            info = os.fstat(descriptor)
            current = os.read(descriptor, len(body) + 1) if stat.S_ISREG(info.st_mode) else b""
            current = current if (info.st_uid == 0 and stat.S_IMODE(info.st_mode) == RULE_MODE) else b""
        finally:
            os.close(descriptor)
    if current == body:
        return f"switchyard: {project}'s tenant-upgrade polkit rule is in place: {path}"
    verb = "install" if current is None else "replace"
    if dry_run:
        return f"switchyard: would {verb} {project}'s tenant-upgrade polkit rule {path}; nothing written"
    if euid != 0:
        return (f"switchyard: {project}'s tenant-upgrade polkit rule {path} is missing or differs, and only root can "
                f"write it: `{upgrade}`")
    staged = path.with_name(f".{path.name}.new")
    try:
        staged.unlink()
    except FileNotFoundError:
        pass
    descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(descriptor, body)
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, RULE_MODE)
    finally:
        os.close(descriptor)
    staged.replace(path)
    done = "installed" if verb == "install" else "replaced"
    return (f"switchyard: {done} {project}'s tenant-upgrade polkit rule {path}: its Director's "
            f"{', '.join(TENANT_UPGRADE_ACTIONS)} need no prompt")
