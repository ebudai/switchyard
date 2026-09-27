"""Whether a crossing presentation window's tabs can reach this tenant's display sessions, and installing the bridge when they cannot.

- **The verdict:** `display_bridge_state` reads root's control grant and the
  tenant's sudoers rule and returns a `DisplayBridgeState`: not needed,
  present, install, refuse, or unverified. `sudoers_rule_state` judges the
  rule itself -- opened without following, owned by root, never writable by
  anyone else, in `SUDOERS_RULE_MODE` -- and never offers to overwrite a file
  an untrusted account could have put there.
- **At launch:** `display_bridge_launch_problem` says why every tab of a
  crossing window would be refused, as far as a process that cannot read the
  rule can tell.
- **At upgrade:** `ensure_display_bridge` installs the bridge the way
  provisioning does, through the runner it is given, and reads it back.

The grant name, the privileged root and the rule's text come from
`scripts/ticket_board/project_provision.py`, imported where they are used;
the current user is read from `scripts/team_launcher.py` when a function
runs. The suites patch `display_bridge_launch_problem` on the launcher, and
its callers reach it there.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-323). `team_launcher`
imports this module at its top and still exports every name callers read there.
This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig



@dataclass(frozen=True)
class DisplayBridgeState:
    """Whether the desktop account's tabs can reach this tenant's display sessions.

    A presentation window that crosses accounts opens one tab per slot, and each
    tab runs `sudo -n switchyard-display-attach <project> <slot>`. That program
    admits its caller only when root's `control-grant.json` names them, and sudo
    lets them run it without a password only when `49-<project>-tenant-control`
    says so. Provisioning installs both; a tenant older than the bridge has
    neither, and every tab of its window dies with "a password is required"
    (SYRD-233, live on mefp).
    """

    #: "not needed", "present", "install", "refuse", or "unverified" -- the rule
    #: exists but only root can read it, and this process is not root.
    action: str
    detail: str = ""
    commands: tuple[str, ...] = ()


#: The mode `install -m 0440` leaves, and the only one this rule is left in.
SUDOERS_RULE_MODE = 0o440


def sudoers_rule_state(
    rule: Path, *, expected: str, root_uid: int = 0, root_gid: int = 0
) -> tuple[str, str]:
    """Whether a sudoers rule is the one we mean, and safe for sudo to load.

    Text alone is not the question. sudo ignores a file in sudoers.d that is
    group- or world-writable and will not follow one that is not root's, so a
    rule whose bytes are exactly right can still leave every tab asking for a
    password -- and reporting it present is how an upgrade would declare a
    tenant ready over exactly that (SYRD-233 DAT).

    Three answers. "refuse" for anything an untrusted account could have put
    there or could still change -- a symlink, another owner, a writable mode --
    which is never replaced, because overwriting somebody else's file is not a
    repair and writing through their symlink is worse. "install" for a rule
    that is root's and not writable by anyone else but whose bytes or mode have
    drifted: that is ours to normalize. "present" only for the exact bytes in
    the exact mode.

    The file is opened without following, and its metadata read from the open
    descriptor, so what is checked and what is read are the same file.
    """
    try:
        descriptor = os.open(rule, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return "install", ""
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            return "refuse", (
                f"{rule} is a symlink; sudo will not load it and replacing it would write "
                "wherever it points"
            )
        if exc.errno == errno.EACCES and os.geteuid() != 0:
            # Not a finding about the rule: it is root's 0440 file, exactly as
            # it should be, and this process is not root. Calling that a refusal
            # stopped MEFP's Director's unprivileged upgrade dry run with "every
            # tab refused" (SYRD-283). Nor is it a pass: nothing here read it.
            return "unverified", (
                f"{rule} can only be read by root, and this process is uid {os.geteuid()}"
            )
        return "refuse", f"{rule} cannot be read ({exc.strerror})"
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return "refuse", f"{rule} is not a regular file"
        if info.st_uid != root_uid:
            return "refuse", (
                f"{rule} is owned by uid {info.st_uid}, not by uid {root_uid}; sudo will not "
                "load it and it is not this upgrade's to replace"
            )
        if info.st_mode & 0o022:
            return "refuse", (
                f"{rule} is mode {stat.S_IMODE(info.st_mode):04o}, which lets an account other "
                "than root rewrite it; sudo ignores such a file. Remove or repair it deliberately"
            )
        try:
            current = os.read(descriptor, len(expected.encode("utf-8")) + 1).decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return "refuse", f"{rule} cannot be read ({exc})"
    finally:
        os.close(descriptor)
    if current != expected:
        return "install", "its text has drifted"
    if stat.S_IMODE(info.st_mode) != SUDOERS_RULE_MODE or info.st_gid != root_gid:
        return "install", (
            f"it is root's but mode {stat.S_IMODE(info.st_mode):04o} group {info.st_gid}, "
            f"not {SUDOERS_RULE_MODE:04o} group {root_gid}"
        )
    return "present", ""


def display_bridge_state(
    config: "ProjectConfig",
    *,
    gui_user: str,
    grant_root: Path | None = None,
    sudoers_dir: Path = Path("/etc/sudoers.d"),
    grant_owner_uid: int = 0,
    grant_owner_gid: int = 0,
) -> DisplayBridgeState:
    """Read root's grant and sudoers rule and say what, if anything, is owed.

    `grant_owner_uid` is the account the grant has to belong to: root's, on a
    host. It is a parameter rather than the caller's own uid because the
    question is about the file, not about who is asking (and a sandbox would
    otherwise make every file look like root's).
    """
    from scripts import team_launcher as launcher

    from scripts.ticket_board.project_provision import (
        TENANT_CONTROL_GRANT_NAME,
        TENANT_CONTROL_ROOT,
        tenant_control_commands,
        tenant_control_sudoers_document,
    )

    owner = config.run_as_user or launcher.current_user_name()
    user = (gui_user or "").strip()
    if not user or user == owner:
        return DisplayBridgeState("not needed", "the window and the sessions belong to one account")
    # The fixed root, not the staging seam: the install commands write there and
    # switchyard-display-attach reads from there, and a check that looked
    # anywhere else could report a bridge the helper will never see.
    grant = Path(grant_root or TENANT_CONTROL_ROOT) / config.project / TENANT_CONTROL_GRANT_NAME
    install = DisplayBridgeState(
        "install",
        f"{user} may attach this project's display tabs: {grant} and "
        f"{sudoers_dir / f'49-{config.project}-tenant-control'}",
        tuple(tenant_control_commands(config.project, owner, user)),
    )
    try:
        info = os.lstat(grant)
    except FileNotFoundError:
        return install
    except OSError as exc:
        return DisplayBridgeState("refuse", f"{grant} cannot be inspected ({exc.strerror})")
    if not stat.S_ISREG(info.st_mode) or info.st_uid != grant_owner_uid or info.st_mode & 0o022:
        return DisplayBridgeState(
            "refuse",
            f"{grant} is not a root-owned, root-writable file, so it is not an authority "
            "and will not be overwritten without an operator looking at it",
        )
    try:
        payload = json.loads(grant.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return DisplayBridgeState("refuse", f"{grant} is unreadable ({exc})")
    recorded = str(payload.get("authorized_user") or "").strip() if isinstance(payload, dict) else ""
    if not isinstance(payload, dict) or payload.get("project") != config.project:
        return DisplayBridgeState("refuse", f"{grant} names a different project")
    if str(payload.get("owner") or "").strip() != owner:
        return DisplayBridgeState(
            "refuse", f"{grant} names owner {payload.get('owner')!r}, and this tenant's owner is {owner}"
        )
    if recorded != user:
        # Never taken over: the grant decides who may cross into the owner
        # account, and a second person running an upgrade must not quietly
        # become that person (SYRD-50).
        return DisplayBridgeState(
            "refuse",
            f"{grant} authorizes {recorded or 'nobody'}, and the desktop policy names {user}; "
            "the window's tabs would be refused. Decide which account controls this tenant "
            "and record that one",
        )
    rule = sudoers_dir / f"49-{config.project}-tenant-control"
    expected = tenant_control_sudoers_document(config.project, user) + "\n"
    verdict, detail = sudoers_rule_state(
        rule, expected=expected, root_uid=grant_owner_uid, root_gid=grant_owner_gid
    )
    if verdict == "refuse":
        return DisplayBridgeState("refuse", detail)
    if verdict == "unverified":
        return DisplayBridgeState("unverified", detail)
    if verdict == "install":
        return replace(install, detail=f"{install.detail} ({detail})") if detail else install
    return DisplayBridgeState("present", f"{user} already holds the display bridge")


def display_bridge_launch_problem(
    config: "ProjectConfig",
    *,
    gui_user: str,
    grant_root: Path | None = None,
    grant_owner_uid: int = 0,
) -> str:
    """Why a crossing window's tabs would all be refused, as far as a launch can tell.

    The grant is world-readable and the sudoers rule is not, so this judges the
    half it can see: a missing grant, or one naming somebody else, means every
    tab dies with "a password is required" (SYRD-233). A grant that is right is
    not second-guessed over a rule this process cannot read.
    """
    from scripts import team_launcher as launcher

    state = launcher.display_bridge_state(
        config, gui_user=gui_user, grant_root=grant_root, grant_owner_uid=grant_owner_uid,
        sudoers_dir=Path("/nonexistent-sudoers-unreadable-here"),
    )
    if state.action == "refuse":
        return state.detail
    if state.action == "install":
        from scripts.ticket_board.project_provision import TENANT_CONTROL_GRANT_NAME, TENANT_CONTROL_ROOT

        grant = Path(grant_root or TENANT_CONTROL_ROOT) / config.project / TENANT_CONTROL_GRANT_NAME
        if not grant.exists():
            return (
                f"no display bridge is installed for {gui_user}: {grant} does not exist, so "
                "`sudo -n switchyard-display-attach` in each tab asks for a password and exits"
            )
    return ""


def ensure_display_bridge(
    config: "ProjectConfig",
    *,
    gui_user: str,
    dry_run: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
    root_check: str = "",
    **locations: Any,
) -> bool:
    """Install the display bridge a crossing window needs, the way provisioning does.

    The same commands `switchyard new` and the role-account migration run --
    visudo before the rule is live, the grant before the rule -- so a repaired
    tenant and a fresh one hold the same bytes. Returns whether the window can
    work.
    """
    from scripts import team_launcher as launcher

    state = launcher.display_bridge_state(config, gui_user=gui_user, **locations)
    if state.action in {"not needed", "present"}:
        return True
    if state.action == "refuse":
        print_func(
            f"switchyard: {config.project}'s presentation window would open with every tab "
            f"refused: {state.detail}. This upgrade stops before declaring it ready. Nothing "
            "was changed."
        )
        return False
    if state.action == "unverified":
        # The grant -- the half this process CAN read -- has already been judged
        # and is right. The rule is root's to read, so the check that remains
        # is root's, through the approved boundary rather than any widening of
        # that file (SYRD-283). Not ready, and not refused either.
        print_func(
            f"switchyard: {config.project}'s display bridge could not be verified from here: "
            f"{state.detail}. Its grant is correct; the sudoers rule beside it can only be "
            "checked by root. Run the same upgrade check as root through the approved "
            f"boundary: {root_check or 'switchyard privileged-action ' + config.project + ' preview-upgrade commit=<release commit>'}. "
            "This run does not declare the tenant ready. Nothing was changed."
        )
        return False
    if dry_run:
        print_func(f"switchyard: would install the display bridge so {state.detail}; nothing written")
        return True
    script = "\n".join(line for line in state.commands if line and not line.startswith("#"))
    result = runner(["sh", "-euc", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        print_func(
            f"switchyard: could not install {config.project}'s display bridge for {gui_user} "
            f"(exit {result.returncode}): {(str(result.stderr).strip() or 'no output')[:400]}"
        )
        return False
    # Read back rather than trusted: an install that exited 0 and left nothing
    # behind is the failure that would otherwise reach the window.
    after = launcher.display_bridge_state(config, gui_user=gui_user, **locations)
    if after.action != "present":
        print_func(
            f"switchyard: installed {config.project}'s display bridge, but it does not read "
            f"back as present: {after.detail}"
        )
        return False
    print_func(f"switchyard: installed the display bridge so {state.detail}")
    return True
