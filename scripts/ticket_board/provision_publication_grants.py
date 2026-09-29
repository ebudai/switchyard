"""How provisioning grants a tenant's project account publication, and where root keeps what it needs.

The sudoers rule that lets the project account run the two root-owned
publication programs (`publish_sudoers_path`, `publish_sudoers_document`); the
root-owned places for the publication grant, key, pinned hosts and staging
(`publish_grant_root`, `publish_staging_root`, their defaults and the constants
`PUBLISH_GRANT_ROOT`, `PUBLISH_STAGING_ROOT`, `PUBLISH_GRANT_SCHEMA`); and the
operator commands that install the publication credential and pin its remote
(`publish_grant_path`, `publish_identity_path`, `publish_grant_commands`).

Moved out of `scripts/ticket_board/project_provision.py` unchanged (SYRD-468).
`project_provision` imports this module and re-exports every name, so every
module and test that imports them from there, or patches them there, still
reaches the same objects. What they read of `project_provision` -- each other,
`shell_quote` and `TENANT_CONTROL_ROOT` -- is read through it when they run, so
a patch there still reaches them. This module imports `project_provision` only
inside the functions that need it, when they run, with the same fallback for
direct script execution.
"""

from __future__ import annotations

from pathlib import Path


def publish_sudoers_path(project: str, *, root: Path | str | None = None) -> Path:
    """Its own file, separate from the role-control rule.

    An existing tenant has no role accounts, so `render_role_control_sudoers`
    returns only the publisher line and the upgrade would otherwise have to
    rewrite a file whose other purpose it is not responsible for. A rule of its
    own is installed and validated independently, and removing publication does
    not disturb the tmux grants (SYRD-97).
    """
    import os as _os

    configured = _os.environ.get("SWITCHYARD_SUDOERS_ROOT", "").strip()
    directory = Path(root) if root is not None else Path(configured or "/etc/sudoers.d")
    return directory / f"48-{project}-publish"


def publish_sudoers_document(project: str, owner_user: str) -> str:
    """The grants that let the project account reach the root-owned operations.

    Two programs, no arguments of the operator's choosing. Each decides for
    itself whether the process invoking it is the live runtime the board
    registered for the control role, so holding these is not the same as being
    allowed to publish or to integrate.

    The second one is not optional. Publishing a role's ref and refusing `main`
    is only half a boundary: without a way for the control role to integrate,
    the credential cutover leaves reviewed work published and unmergeable, which
    is the deadlock this pair exists to avoid (SYRD-93).
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    publisher = f"{provision.TENANT_CONTROL_ROOT}/{project}/switchyard-publish-ref"
    integrator = f"{provision.TENANT_CONTROL_ROOT}/{project}/switchyard-integrate-main"
    lines = [
        f"# {project}: publication. The project account may run two root-owned",
        "# programs, each of which refuses any caller but the control role's registered",
        "# process: one publishes an implementer's ref and refuses integration branches,",
        "# the other fast-forwards the integration branch and moves nothing else.",
        f"{owner_user} ALL=(root) NOPASSWD: {publisher}",
        f"{owner_user} ALL=(root) NOPASSWD: {integrator}",
    ]
    return "\n".join(lines) + "\n"


DEFAULT_PUBLISH_GRANT_ROOT = "/etc/switchyard/publish"
DEFAULT_PUBLISH_STAGING_ROOT = "/var/lib/switchyard/publish"


def publish_grant_root() -> str:
    """Where root keeps the publication grant, key and pinned hosts.

    Overridable for the same reason the shared install root and the privileged
    provision root are: a suite has to be able to exercise the real privileged
    branch without writing into the host's /etc. Without it these fixtures would
    have created /etc/switchyard/publish on the machine running them.
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    import os as _os

    return _os.environ.get("SWITCHYARD_PUBLISH_ROOT", "").strip() or provision.DEFAULT_PUBLISH_GRANT_ROOT


def publish_staging_root() -> str:
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    import os as _os

    return (
        _os.environ.get("SWITCHYARD_PUBLISH_STAGING_ROOT", "").strip()
        or provision.DEFAULT_PUBLISH_STAGING_ROOT
    )


PUBLISH_GRANT_ROOT = DEFAULT_PUBLISH_GRANT_ROOT
PUBLISH_STAGING_ROOT = DEFAULT_PUBLISH_STAGING_ROOT
PUBLISH_GRANT_SCHEMA = "switchyard.publish-grant.v1"


def publish_grant_path(project: str) -> str:
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    return f"{provision.publish_grant_root()}/{project}.json"


def publish_identity_path(project: str) -> str:
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    return f"{provision.publish_grant_root()}/{project}-publish-key"


def publish_grant_commands(plan: "ProjectBoardProvision") -> list[str]:
    """Install the one credential on this host that may push, owned by root.

    Under one Unix account per project (SYRD-69), a key the project account
    can read is a key every role can push with, which is the same as no
    boundary at all. So the push credential is root's, the publisher that
    uses it is root's, and the project account reaches it only through a sudo
    grant for that one program -- which authorizes the calling PROCESS against
    the board's registered control-role runtime rather than the account
    (SYRD-93).

    Re-runnable: an existing key is never regenerated, because doing so would
    silently break publication until the new public key was registered with
    the forge. The grant document is rewritten, because it is derived.
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    key = provision.publish_identity_path(plan.project)
    grant = provision.publish_grant_path(plan.project)
    known_hosts = f"{provision.PUBLISH_GRANT_ROOT}/known_hosts"
    q_key = provision.shell_quote(key)
    q_grant = provision.shell_quote(grant)
    q_known = provision.shell_quote(known_hosts)
    q_cache = provision.shell_quote(plan.commit_git_dir)
    q_owner = provision.shell_quote(plan.owner_user)
    writer = (
        "import json, os, sys; "
        "print(json.dumps({"
        "'schema': os.environ['PUBLISH_SCHEMA'], "
        "'project': os.environ['PUBLISH_PROJECT'], "
        "'identity_file': os.environ['PUBLISH_KEY'], "
        "'known_hosts': os.environ['PUBLISH_KNOWN_HOSTS'], "
        "'remote': os.environ['PUBLISH_REMOTE']}, indent=2, sort_keys=True))"
    )
    return [
        f"sudo install -d -m 0755 -o root -g root {provision.shell_quote(provision.PUBLISH_GRANT_ROOT)}",
        f"sudo install -d -m 0755 -o root -g root {provision.shell_quote(provision.PUBLISH_STAGING_ROOT)}",
        f"if ! sudo test -f {q_key}; then",
        f"    sudo ssh-keygen -q -t ed25519 -N '' -C {provision.shell_quote(f'switchyard {plan.project} publication')} -f {q_key}",
        f"    sudo chmod 0600 {q_key}",
        f"    sudo chmod 0644 {q_key}.pub",
        f"    echo 'switchyard: register the public key below with the forge as a WRITE key for {plan.project}.'",
        "    echo 'switchyard: then make the project account key read-only -- until you do, every role can still push.'",
        f"    sudo cat {q_key}.pub",
        "fi",
        "# The destination is pinned in root-owned data. The project checkout names",
        "# a remote too, but the project account can rewrite that, and a role that",
        "# can choose the remote can aim a push at a server of its own.",
        f"PUBLISH_REMOTE=\"$(sudo -u {q_owner} git --git-dir {q_cache} remote get-url origin 2>/dev/null || true)\"",
        'if [ -n "$PUBLISH_REMOTE" ]; then',
        '    publish_host="${PUBLISH_REMOTE#*@}"',
        '    publish_host="${publish_host%%:*}"',
        '    publish_host="${publish_host%%/*}"',
        f'    if [ -n "$publish_host" ] && ! sudo grep -qs "$publish_host" {q_known}; then',
        f'        ssh-keyscan -H "$publish_host" 2>/dev/null | sudo tee -a {q_known} >/dev/null',
        f"        sudo chmod 0644 {q_known}",
        "    fi",
        f'    PUBLISH_SCHEMA={provision.shell_quote(provision.PUBLISH_GRANT_SCHEMA)} PUBLISH_PROJECT={provision.shell_quote(plan.project)} '
        f'PUBLISH_KEY={q_key} PUBLISH_KNOWN_HOSTS={q_known} PUBLISH_REMOTE="$PUBLISH_REMOTE" '
        f'/usr/bin/python3 -c {provision.shell_quote(writer)} | sudo install -m 0640 -o root -g root /dev/stdin {q_grant}',
        "else",
        f"    echo 'switchyard: no remote is known for {plan.project}; publication stays refused until one is pinned in {grant}.'",
        "fi",
    ]
