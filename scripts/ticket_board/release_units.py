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


def is_exact_release_unit(text: str) -> bool:
    """Whether an installed board unit is one of these exact-release renders, not a legacy unit (SYRD-700).

    Every render runs Python ``-E -s -B`` from a commit-named root release and loads no ``EnvironmentFile``; the
    legacy units do neither. ``board_identity.effective_unit`` is the proof; this only says which kind a file is.
    """
    lines = text.splitlines()
    exec_start = [line for line in lines if line.startswith("ExecStart=")]
    return (len(exec_start) == 1 and " -E -s -B " in exec_start[0] and "/scripts/ticket-board.py" in exec_start[0]
            and not any(line.startswith("EnvironmentFile=") for line in lines))


def renders_template(installed: str, template: str) -> bool:
    """Whether ``installed`` is ``template`` itself, or its exact render for the release its own ExecStart names.

    So a check that a host's board unit is "this plan's" holds for the unit root installs now as well as the
    legacy one it used to (SYRD-700).
    """
    if installed == template:
        return True
    if not is_exact_release_unit(installed):
        return False
    exec_start = next(line for line in installed.splitlines() if line.startswith("ExecStart="))
    try:
        argv = shlex.split(exec_start[len("ExecStart="):])
        release = Path(argv[4]).parent.parent
        rendered = render_board_unit(template, release=release, report_credentials=report_credentials_path(template))
    except (ValueError, IndexError):  # ReleaseUnitError is a ValueError
        return False
    return installed == rendered


#: Where root's installed board units are; a suite points it at a directory of its own.
SYSTEM_UNIT_DIR = Path("/etc/systemd/system")


def installed_release(text: str) -> Path | None:
    """The release an exact-release unit runs, or None for any other unit."""
    if not is_exact_release_unit(text):
        return None
    exec_start = next(line for line in text.splitlines() if line.startswith("ExecStart="))
    try:
        return Path(shlex.split(exec_start[len("ExecStart="):])[4]).parent.parent
    except (ValueError, IndexError):
        return None


def exact_board_unit_rewrite_problem(board_unit: str, command: str) -> str:
    """Why ``command`` cannot rewrite this board's unit, or ''; asked before it changes anything.

    `add-role` and `set-vcs-close-role` change what the board's unit says. Over root's exact-release unit they
    install the exact render of the refreshed template for the release that unit already runs, never the legacy
    template, which would run the tenant's own tree (SYRD-702). That needs root's trusted copy of that release,
    able to run under the render; without it nothing is changed. A unit that is still legacy is rewritten as
    before, and repaired one tenant at a time (SYRD-701).
    """
    if not board_unit:
        return ""
    try:
        text = (SYSTEM_UNIT_DIR / board_unit).read_text(encoding="utf-8")
    except OSError:
        return ""
    release = installed_release(text)
    if release is None:
        return ""
    from .privileged_operations import NoTrustedSource, trusted_release_root

    def refused(why: str) -> str:
        return f"team-launcher: {command} re-renders {board_unit} for the release it runs, {release}: {why}. Nothing was changed."

    try:
        trusted = Path(trusted_release_root(release.name))
    except NoTrustedSource as exc:
        return refused(str(exc))
    if trusted != release:
        return refused(f"root's trusted release of {release.name} is {trusted}")
    if not (release / "scripts" / "ticket_board" / "release_units.py").is_file():
        return refused("it predates exact root-release board units (SYRD-699)")
    return ""


def report_credentials_path(template: str) -> Path | None:
    """The old environment file as data only; systemd copies it as a credential."""
    values = [line.split("=", 1)[1].lstrip("-") for line in template.splitlines()
              if line.startswith("EnvironmentFile=")]
    if not values:
        return None
    if len(values) != 1 or not values[0].startswith("/"):
        raise ReleaseUnitError("the provisioned board unit names an invalid EnvironmentFile")
    return Path(values[0])


def main(argv: list[str] | None = None) -> int:
    """``release_units.py <board template> <canary template> <release> <out dir>``: root's exact renders of both units
    for ``release``, named as their templates, for the operator packet. Dependency-free, so it runs as
    ``python3 -I -B`` from the release itself (SYRD-700). The canary's port is chosen by each deploy-release."""
    import sys

    args = sys.argv[1:] if argv is None else argv
    if len(args) != 4:
        print("usage: release_units.py <board template> <canary template> <release> <out dir>", file=sys.stderr)
        return 2
    board_template, canary_template, release, out = (Path(arg) for arg in args)
    board_text = board_template.read_text(encoding="utf-8")
    board = render_board_unit(board_text, release=release, report_credentials=report_credentials_path(board_text))
    canary = render_canary_unit(board_text, canary_template.read_text(encoding="utf-8"), release=release, port=0)
    (out / board_template.name).write_text(board, encoding="utf-8")
    (out / canary_template.name).write_text(canary, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
