"""Exact-release board units installed by root during ``deploy-release``.

The provisioned units are useful as configuration carriers, but they are not
safe execution authorities: historically they selected code through the
tenant-owned ``current`` link and loaded an environment file the tenant could
turn into Python or dynamic-loader code.  This module makes a deployment unit
from those reviewed, root-owned bytes while fixing every code-bearing value to
one root-owned shared release.

The canary is rendered afresh.  In particular it has no shell and no
``EnvironmentFile``: a tenant may still ask systemd to start the narrowly
granted canary unit, but cannot choose the program that shared ``boardsvc``
executes.
"""

from __future__ import annotations

import shlex
from pathlib import Path


class ReleaseUnitError(ValueError):
    """A provisioned unit cannot be made into an exact-release unit."""


def _one(lines: list[str], key: str) -> str:
    values = [line.split("=", 1)[1] for line in lines if line.startswith(f"{key}=")]
    if len(values) != 1:
        raise ReleaseUnitError(f"the provisioned unit must contain exactly one {key}= directive")
    return values[0]


def _environment(lines: list[str], name: str) -> str:
    prefix = f"Environment={name}="
    values = [line[len(prefix):] for line in lines if line.startswith(prefix)]
    if len(values) != 1:
        raise ReleaseUnitError(f"the provisioned board unit must contain exactly one {prefix} directive")
    return values[0]


def _python_and_board_argv(lines: list[str]) -> tuple[str, list[str]]:
    try:
        argv = shlex.split(_one(lines, "ExecStart"))
    except ValueError as exc:
        raise ReleaseUnitError(f"the provisioned board ExecStart is not parseable: {exc}") from None
    scripts = [at for at, value in enumerate(argv) if value.endswith("/scripts/ticket-board.py")]
    if len(scripts) != 1 or scripts[0] != 1:
        raise ReleaseUnitError("the provisioned board ExecStart does not run exactly one ticket-board.py")
    return argv[0], argv[2:]


def render_board_unit(template: str, *, release: Path, report_credentials: Path | None = None) -> str:
    """Pin a reviewed board unit to ``release`` and remove tenant code inputs."""
    lines = template.splitlines()
    python, board_args = _python_and_board_argv(lines)
    runtime = _one(lines, "RuntimeDirectory")
    directorctl = release / "scripts" / "directorctl"
    argv = [python, "-E", "-s", "-B", str(release / "scripts" / "ticket-board.py"), *board_args]
    if report_credentials is not None:
        argv += ["--report-credentials-file", "%d/board-report-credentials"]

    rendered: list[str] = []
    inserted_state = False
    for line in lines:
        if line.startswith("EnvironmentFile="):
            continue
        if line.startswith("LoadCredential="):
            continue
        if line.startswith("WorkingDirectory="):
            line = f"WorkingDirectory={release}"
        elif line.startswith("ExecStart="):
            line = "ExecStart=" + shlex.join(argv)
        elif line.startswith("Environment=HOME="):
            line = f"Environment=HOME=/var/lib/{runtime}"
        elif line.startswith("Environment=TICKET_BOARD_DIRECTORCTL="):
            line = f"Environment=TICKET_BOARD_DIRECTORCTL={directorctl}"
        rendered.append(line)
        if line.startswith("RuntimeDirectoryMode=") and not inserted_state:
            rendered.extend((f"StateDirectory={runtime}", "StateDirectoryMode=0750"))
            if report_credentials is not None:
                # LoadCredential is deliberately non-optional: if the file is
                # present but unreadable, the board must not silently lose its
                # report authority.  The empty default preserves the old
                # EnvironmentFile=- behaviour when the optional file is
                # absent (systemd.exec(5)).
                rendered.append("SetCredential=board-report-credentials:")
                rendered.append(f"LoadCredential=board-report-credentials:{report_credentials}")
            inserted_state = True
    if not inserted_state:
        raise ReleaseUnitError("the provisioned board unit names no RuntimeDirectoryMode")
    return "\n".join(rendered) + "\n"


def render_canary_unit(board_template: str, canary_template: str, *, release: Path, port: int) -> str:
    """Render the root-selected canary program and all of its writable paths."""
    board_lines, canary_lines = board_template.splitlines(), canary_template.splitlines()
    python, _args = _python_and_board_argv(board_lines)
    user = _one(canary_lines, "User")
    runtime = _one(canary_lines, "RuntimeDirectory")
    database = _environment(board_lines, "TICKET_BOARD_DATABASE_URL")
    project = _environment(board_lines, "TICKET_BOARD_PROJECT")
    argv = [
        python, "-E", "-s", "-B", str(release / "scripts" / "ticket-board.py"),
        "--host", "127.0.0.1", "--port", str(port),
        "--unix-socket", f"/run/{runtime}/ticket-board.sock",
        "--frames", f"/run/{runtime}/frames", "--assets", f"/run/{runtime}/assets",
    ]
    return f"""[Unit]
Description={project} Ticket Board Deploy Canary
After=network.target postgresql.service

[Service]
Type=simple
User={user}
WorkingDirectory={release}
RuntimeDirectory={runtime}
RuntimeDirectoryMode=0750
ExecStartPre=/bin/mkdir -p /run/{runtime}/frames /run/{runtime}/assets
ExecStart={' '.join(shlex.quote(value) for value in argv)}
Restart=no
Environment=PYTHONUNBUFFERED=1
Environment=HOME=/var/lib/{runtime}
Environment=TICKET_BOARD_PROJECT={project}
Environment=TICKET_BOARD_DATABASE_URL={database}
StateDirectory={runtime}
StateDirectoryMode=0750
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/run/{runtime}
"""


def report_credentials_path(template: str) -> Path | None:
    """The old environment file as data only; systemd copies it as a credential."""
    values = [line.split("=", 1)[1].lstrip("-") for line in template.splitlines()
              if line.startswith("EnvironmentFile=")]
    if not values:
        return None
    if len(values) != 1 or not values[0].startswith("/"):
        raise ReleaseUnitError("the provisioned board unit names an invalid EnvironmentFile")
    return Path(values[0])
