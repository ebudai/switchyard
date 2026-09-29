"""How the repository-boundary phase is found in a rendered packet and checked before it is run on its own.

The fence that marks the phase (`REPOSITORY_BOUNDARY_BEGIN`,
`REPOSITORY_BOUNDARY_END`), what a line inside it may be
(`REPOSITORY_BOUNDARY_ALLOWED`, `REPOSITORY_BOUNDARY_GUARDS` with `_QUOTED`,
`_SHELL_METACHARACTERS`, `_outside_quotes`, `_is_boundary_line`), and the two
readers the repair uses: `repository_boundary_phase`, which lifts the phase out
of root's own packet or says why it may not be used, and
`repository_boundary_statements`, which groups its lines into the statements the
shell sees (SYRD-175).

Moved out of `scripts/ticket_board/project_provision.py` unchanged (SYRD-472).
`project_provision` imports this module and re-exports every name, so every
module and test that imports them from there, or patches them there, still
reaches the same objects -- including the operator script, which writes the
fence with the same two markers. What they read of `project_provision` -- each
other -- is read through it when they run, so a patch there still reaches them.
This module imports `project_provision` only inside the functions that need it,
when they run, with the same fallback for direct script execution.
"""

from __future__ import annotations

import re


#: The packet's repository-boundary phase, fenced so it can be lifted out of a
#: root-owned packet and applied on its own. A tenant that is already running
#: needs this repair and needs nothing else in the packet -- not a deploy, not
#: the schema, not a unit reload -- and the only way to be sure that is what it
#: gets is to take the lines from root's own artifact rather than render them
#: again beside it (SYRD-175).
REPOSITORY_BOUNDARY_BEGIN = "# >>> switchyard repository boundary"
REPOSITORY_BOUNDARY_END = "# <<< switchyard repository boundary"


#: What a line inside that fence may start with. The fence says where the phase
#: is; this says what a phase is allowed to be, so a packet that grew a deploy
#: inside the markers is refused rather than run.
REPOSITORY_BOUNDARY_ALLOWED = (
    "sudo install -d ",
    "sudo setfacl ",
    "sudo find ",
    "sudo groupadd ",
    "sudo gpasswd ",
)


#: The guards the phase is allowed to put around those commands, exactly as it
#: writes them. A guard is matched whole rather than by prefix: its own line is
#: the one place the phase spells `;`, `>` and `&`, so anything else wearing
#: that shape has to be rejected on sight.
_QUOTED = r"(?:'[^']*'|\"'\")+"
REPOSITORY_BOUNDARY_GUARDS = (
    re.compile(rf"^if (?:! )?getent group {_QUOTED} >/dev/null 2>&1; then$"),
    re.compile(rf"^if \[ -d {_QUOTED} \]; then$"),
)


#: What may appear outside quotes in a command line. The phase quotes every
#: path it names, so a `;`, a `|`, a backtick or a `$(` in the open is a second
#: command riding along on the first.
_SHELL_METACHARACTERS = ";&|`$<>()\n"


def _outside_quotes(line: str) -> str:
    """The part of a line the shell would read as syntax rather than as text."""
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    bare: list[str] = []
    single = double = False
    for character in line:
        if character == "'" and not double:
            single = not single
        elif character == '"' and not single:
            double = not double
        elif not single and not double:
            bare.append(character)
    if single or double:
        return provision._SHELL_METACHARACTERS  # an unbalanced quote is not a line we run
    return "".join(bare)


def _is_boundary_line(line: str) -> bool:
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    stripped = line.strip()
    if stripped == "fi" or stripped.startswith("# "):
        return True
    if any(guard.match(stripped) for guard in provision.REPOSITORY_BOUNDARY_GUARDS):
        return True
    if not stripped.startswith(provision.REPOSITORY_BOUNDARY_ALLOWED):
        return False
    # The one redirection the phase writes -- `gpasswd` reporting the group it
    # just changed -- is part of the command, not a second one.
    stripped = re.sub(r" >/dev/null(?: 2>&1)?$", "", stripped)
    return not any(
        character in provision._SHELL_METACHARACTERS for character in provision._outside_quotes(stripped)
    )


def repository_boundary_statements(phase: list[str]) -> list[list[str]]:
    """Group a boundary phase's lines into the statements the shell sees.

    The phase's guards -- `if [ -d ... ]; then` around the worktree sweep and
    `if getent group ...; then` around the retirement -- are several lines and
    one command. Running the lines one at a time would hand `sh` half an `if`,
    so the grouping the shell would do is done here, and each statement is run
    and reported whole (SYRD-175).
    """
    statements: list[list[str]] = []
    current: list[str] = []
    depth = 0
    for line in phase:
        current.append(line)
        stripped = line.strip()
        if stripped.endswith("; then"):
            depth += 1
        elif stripped == "fi":
            depth -= 1
        if depth <= 0:
            statements.append(current)
            current = []
            depth = 0
    if current:
        statements.append(current)
    return statements


def repository_boundary_phase(packet: str) -> tuple[list[str], str]:
    """The boundary phase of a rendered packet, or why it may not be used.

    Lifted from between the markers rather than re-rendered, so what runs is
    what root installed. Every line is then checked against the shapes a
    boundary phase is made of: this exists to apply an ACL and mode repair to a
    tenant that is already serving, and a phase carrying anything else -- a
    deploy, a psql, a systemctl -- is a packet this will not run at all.
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    lines = packet.splitlines()
    try:
        start = lines.index(provision.REPOSITORY_BOUNDARY_BEGIN)
        end = lines.index(provision.REPOSITORY_BOUNDARY_END, start)
    except ValueError:
        return [], (
            "this packet has no repository boundary phase in it, so it was generated "
            "before that phase existed"
        )
    phase = [line for line in lines[start + 1 : end] if line.strip()]
    for line in phase:
        if not provision._is_boundary_line(line):
            return [], (
                f"the repository boundary phase of this packet contains a line that is not "
                f"part of one: {line.strip()!r}"
            )
    return phase, ""
