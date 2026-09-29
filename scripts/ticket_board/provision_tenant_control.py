"""Who may drive a tenant's lifecycle, and the grant and sudoers rule that let that one human do it.

The control grant and its paths (`TENANT_CONTROL_GRANT_NAME`,
`tenant_control_grant_path`, `tenant_control_grant_name`,
`tenant_control_grant_document`, `tenant_control_grant`); the recorded human
(`resolve_control_user`, `invoking_human`); the sudoers rule for the two
root-owned helpers (`tenant_control_sudoers_path`, `tenant_control_helper_path`,
`display_attach_helper_path`, `tenant_control_sudoers_document`,
`tenant_control_sudoers`); and the operator commands that install both
(`tenant_control_commands`).

Moved out of `scripts/ticket_board/project_provision.py` unchanged (SYRD-469).
`project_provision` imports this module and re-exports every name, so every
module and test that imports them from there, or patches them there, still
reaches the same objects. What they read of `project_provision` -- each other,
`shell_quote`, `TENANT_CONTROL_ROOT` and `TENANT_CONTROL_LAUNCHER`, which stays
there because it is bound from `SHARED_RELEASE_CURRENT` when `project_provision`
loads -- is read through it when they run, so a patch there still reaches them.
This module imports `project_provision` only inside the functions that need it,
when they run, with the same fallback for direct script execution.
"""

from __future__ import annotations

import json
import os
import pwd
from pathlib import Path


TENANT_CONTROL_GRANT_NAME = "control-grant.json"


def tenant_control_grant_path(project: str) -> str:
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    return f"{provision.TENANT_CONTROL_ROOT}/{project}/{provision.TENANT_CONTROL_GRANT_NAME}"


def tenant_control_sudoers_path(project: str) -> str:
    return f"/etc/sudoers.d/49-{project}-tenant-control"


def resolve_control_user(
    project: str,
    *,
    invoking_user: str = "",
    owner_user: str = "",
    root: Path | None = None,
) -> str:
    """Who may drive this tenant's lifecycle, decided by root and nobody else.

    An installed grant wins, so re-running provisioning, an upgrade or a repair
    reinstalls the human already recorded rather than whoever happens to be at
    the keyboard this time -- that is what makes repair idempotent, and it also
    means a second operator cannot quietly take the grant over by running it.

    With no grant yet, the human who is provisioning is recorded. The tenant's
    own accounts are never recorded: the owner already is the owner, and giving
    a role account a bridge into the owner would erase the separation the role
    UIDs exist to create.
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    base = Path(root) if root is not None else Path(provision.TENANT_CONTROL_ROOT)
    path = base / project / provision.TENANT_CONTROL_GRANT_NAME
    try:
        info = path.stat()
        # Only a root-owned, root-writable grant is an authority. Anything else
        # is a file the tenant could have written to name its own controller.
        if info.st_uid == 0 and not info.st_mode & 0o022:
            payload = json.loads(path.read_text(encoding="utf-8"))
            recorded = str(payload.get("authorized_user") or "").strip()
            if recorded and payload.get("project") == project:
                return recorded
    except (OSError, ValueError):
        pass
    candidate = invoking_user.strip()
    if not candidate or candidate == "root" or candidate == owner_user.strip():
        return ""
    if candidate.startswith(f"{project}-"):
        return ""
    return candidate


def invoking_human(environ: dict[str, str] | None = None) -> str:
    """The person at the keyboard, through sudo or not.

    SUDO_UID is sudo's own answer rather than a string the caller supplied, and
    resolving the name from it is what stops a provisioning run recording an
    account that merely claims to be the operator.
    """
    env = os.environ if environ is None else environ
    raw = (env.get("SUDO_UID") or "").strip()
    if raw.isdigit() and int(raw) != 0:
        try:
            return pwd.getpwuid(int(raw)).pw_name
        except KeyError:
            return ""
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return ""


def tenant_control_commands(
    project: str,
    owner_user: str,
    control_user: str,
) -> list[str]:
    """Install or refresh the control bridge for one recorded human.

    The contents are written here rather than fetched at run time, so a repair
    and a fresh provision of the same tenant install the same bytes and a rerun
    changes nothing. The sudoers file is parsed by visudo before it is moved
    into place, so a bad render can never leave sudoers.d unreadable and lock
    everyone out of sudo. A tenant with no recorded control user emits nothing.
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    if not control_user:
        return []
    grant_dir = f"{provision.TENANT_CONTROL_ROOT}/{project}"
    grant = provision.tenant_control_grant_path(project)
    sudoers = provision.tenant_control_sudoers_path(project)
    grant_body = provision.tenant_control_grant_document(
        project=project,
        owner_user=owner_user,
        control_user=control_user,
    )
    sudoers_body = provision.tenant_control_sudoers_document(project, control_user)
    return [
        f"# Lifecycle control for {control_user}, the human who provisioned {project}.",
        "# Re-runnable: both files are rewritten to exactly these bytes.",
        f"sudo install -d -m 0755 -o root -g root {provision.shell_quote(grant_dir)}",
        f"printf '%s\\n' {provision.shell_quote(grant_body)} "
        f"| sudo install -m 0644 -o root -g root /dev/stdin {provision.shell_quote(grant)}",
        f"printf '%s\\n' {provision.shell_quote(sudoers_body)} "
        f"| sudo install -m 0440 -o root -g root /dev/stdin {provision.shell_quote(sudoers + '.staged')}",
        f"sudo visudo -c -f {provision.shell_quote(sudoers + '.staged')}",
        f"sudo mv {provision.shell_quote(sudoers + '.staged')} {provision.shell_quote(sudoers)}",
    ]


def tenant_control_helper_path(project: str) -> str:
    return f"/usr/local/lib/switchyard/{project}/switchyard-tenant-control"


def display_attach_helper_path(project: str) -> str:
    """The program one presentation tab runs to reach one display session."""
    return f"/usr/local/lib/switchyard/{project}/switchyard-display-attach"


def tenant_control_grant_document(
    *,
    project: str,
    owner_user: str,
    control_user: str,
) -> str:
    """Everything the bridge needs, written by root and chosen by nobody else.

    The bridge takes only a project and an operation from its caller; the owner
    account, the launcher it may run and the human allowed to run it come from
    here. Keeping them out of the caller's hands is what stops the grant being
    aimed at another tenant, another program or another identity.

    It is world-readable on purpose: the public wrapper reads it to refuse an
    unauthorized caller locally, without a password prompt. A username is not a
    secret, and nothing else is recorded -- there is no field here anyone could
    replay.
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    if not control_user:
        return ""
    return json.dumps(
        {
            "project": project,
            "owner": owner_user,
            "authorized_user": control_user,
            "launcher": provision.TENANT_CONTROL_LAUNCHER,
        },
        indent=2,
        sort_keys=True,
    )


def tenant_control_sudoers_document(project: str, control_user: str) -> str:
    """One command, one caller, no password, no other reachable program.

    A sudoers entry cannot express which arguments are acceptable, so it names a
    program that validates its own: see switchyard-tenant-control. What this
    file decides is only that this human may run that one program as root
    without a password -- and nothing else, which is why it is not a general
    sudo grant on the owner account.
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    if not control_user:
        return ""
    return "\n".join(
        [
            f"# {project}: lifecycle control for the human who provisioned it.",
            "# One program, which validates its own arguments and drops to the owner.",
            f"{control_user} ALL=(root) NOPASSWD: {provision.tenant_control_helper_path(project)}",
            "# And one tab of the presentation window, per display slot. The alternative",
            "# is a password prompt in each of six tabs as the window opens, or a blanket",
            "# grant on the owner account -- this program takes a slot number and can only",
            "# ever attach to a display session of this project (SYRD-65).",
            f"{control_user} ALL=(root) NOPASSWD: {provision.display_attach_helper_path(project)}",
        ]
    )


def tenant_control_grant(plan: ProjectBoardProvision) -> str:
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    document = provision.tenant_control_grant_document(
        project=plan.project,
        owner_user=plan.owner_user,
        control_user=plan.control_user,
    )
    return f"{document}\n" if document else ""


def tenant_control_sudoers(plan: ProjectBoardProvision) -> str:
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    document = provision.tenant_control_sudoers_document(plan.project, plan.control_user)
    return f"{document}\n" if document else ""


def tenant_control_grant_name(project: str) -> str:
    """The grant's name inside the provision directory.

    It is a privileged artifact: root installs it, and the copy root installs
    from lives in the root-owned mirror, because a tenant able to edit its own
    grant would choose its own owner, launcher and controller.
    """
    return f"{project}-control-grant.json"
