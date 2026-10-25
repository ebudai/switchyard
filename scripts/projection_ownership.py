"""A tenant's workflow projection, held by the tenant: root gives back what a provision left as its own (SYRD-676).

A Director changes the project's roles, stages and role prompts without root: `ticket-board-workflow` writes the
board, then the projection -- the launcher configuration, its layout, `workflow.json` and `plan.json` beside it in
`.switchyard/provision/`, and `.switchyard/<slug>.project.json`. Those, and the directory they are in, are the
tenant owner's. Root's packet record is published into the same directory as root's own files first, and only a
`switchyard new` that got past its packet handed the directory and the projection over (`assign_projection_owner`).
A provision finished by `resume-provision` never did: PIE's directory, `plan.json` and `workflow.json` stayed
root:root, and every workflow write its Director made was refused.

`restore_projection_ownership` hands back exactly that set, and nothing else: root's record files beside it -- the
units, sudoers, polkit rules, SQL and packet root ran from its own copy in /etc/switchyard/provision -- stay root's.
Each entry is opened once, from the owner's root, without following a link or crossing a mount, and judged on that
descriptor: a directory, or a regular file with one link, that is root's or already the owner's. Anything else --
a link, a FIFO, a hard link, another account's file, a path outside the owner's roots, a root-owned document that is
not this project's -- is refused, and then nothing at all is changed. What is handed is changed on the descriptor it
was judged on, contents untouched, so a workflow's revision and digest are what they were. Run again, it finds
nothing to do; interrupted, the next run finishes it.
"""

from __future__ import annotations

import json
import os
import pwd
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig

#: The narrow repair, as an operator types it.
REPAIR = "sudo switchyard upgrade {project} --only workflow-projection"


def projection_paths(raw: dict[str, Any], config_path: Path) -> list[tuple[Path, str]]:
    """(path, kind) for the directory and every file the declared workflow is projected into -- the set
    `assign_projection_owner` hands over at provisioning, directory first."""
    layout = Path(str(raw.get("layout") or f"{raw['project']}-konsole-layout.json"))
    if not layout.is_absolute():
        layout = config_path.parent / layout
    return [
        (config_path.parent, "directory"),
        (config_path, "config"),
        (layout, "layout"),
        (config_path.parent / "workflow.json", "workflow"),
        (config_path.parent / "plan.json", "plan"),
        (config_path.parent.parent / f"{raw['project']}.project.json", "artifact"),
    ]


def _accepts(kind: str, project: str, content: bytes) -> str:
    """"" when a root-owned `kind` document is this project's to hand back, else why not."""
    try:
        document = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return "is not a JSON document"
    if not isinstance(document, dict):
        return "is not a JSON object"
    if kind == "workflow":
        from scripts.ticket_board.project_provision import workflow_document_digest
        from scripts.ticket_board.workflow_config import validate

        # What root's packet published there -- its own record, `render_workflow_record` -- until the first workflow
        # write projects the bare document over it: either is this project's, nothing else is.
        declared = document
        if set(document) == {"project", "digest", "document"}:
            if document["project"] != project:
                return f"is root's workflow record for {document['project']!r}, not {project!r}"
            if not isinstance(document["document"], dict) or (
                    workflow_document_digest(document["document"]) != document["digest"]):
                return "is a workflow record whose digest is not its document's"
            declared = document["document"]
        try:
            validate(declared, project=project)
        except Exception as exc:  # noqa: BLE001 - not this project's workflow, whatever the reason
            return f"is not a workflow of {project}'s ({exc})"
        return ""
    if kind in ("config", "plan") and document.get("project") != project:
        return f"names project {document.get('project')!r}, not {project!r}"
    return ""


def restore_projection_ownership(
    config: ProjectConfig,
    config_path: Path,
    *,
    apply: bool,
) -> tuple[list[str], list[str]]:
    """(what was -- or, without `apply`, would be -- handed to the owner, problems). Root only.

    With any problem nothing is changed: the old projection stays as it was, readable, and the problems say what
    to look at. Everything is judged before anything is changed, and changed on the descriptor it was judged on.
    """
    from scripts import owner_writes, tenant_paths

    owner = str(config.run_as_user or "").strip()
    project = str(config.project)
    try:
        account = pwd.getpwnam(owner)
    except KeyError:
        return [], [f"{project} records no owner account on this host ({owner!r})"]
    try:
        raw = owner_writes.read_owner_json(owner, config_path)
    except (OSError, ValueError, SystemExit) as exc:
        return [], [f"{config_path} cannot be read as {owner}'s ({exc})"]
    if raw.get("project") != project:
        return [], [f"{config_path} names project {raw.get('project')!r}, not {project!r}"]
    held: list[int] = []
    roots: list[Any] = []
    hand: list[tuple[int, Path]] = []
    problems: list[str] = []
    try:
        for path, kind in projection_paths(raw, config_path):
            if not owner_writes.within_owner_root(owner, path):
                problems.append(f"{path} is outside {owner}'s roots")
                continue
            root = tenant_paths._quiet(owner_writes.owner_root(owner, path))
            roots.append(root)
            try:
                found = root.leaf(root.relative(str(path)))
            except tenant_paths.Refused as refusal:
                problems.append(str(refusal))
                continue
            except (ValueError, OSError, SystemExit) as exc:  # refused below the check above: never a crash
                problems.append(f"{path} cannot be opened as {owner}'s ({exc})")
                continue
            if found is None:
                continue
            parent, fd, display, _status = found
            os.close(parent)
            held.append(fd)
            status = os.fstat(fd)
            if kind == "directory":
                if not stat.S_ISDIR(status.st_mode):
                    problems.append(f"{display} is not a directory")
                    continue
            elif not stat.S_ISREG(status.st_mode):
                problems.append(f"{display} is not a regular file")
                continue
            elif status.st_nlink != 1:
                problems.append(f"{display} has {status.st_nlink} links; root hands over only a file it alone names")
                continue
            if status.st_uid == account.pw_uid:
                continue
            if status.st_uid != 0:
                problems.append(f"{display} is uid {status.st_uid}'s, neither root's nor {owner}'s; root leaves it")
                continue
            if kind != "directory":
                chunks = []
                os.lseek(fd, 0, os.SEEK_SET)
                while chunk := os.read(fd, 1 << 16):
                    chunks.append(chunk)
                why = _accepts(kind, project, b"".join(chunks))
                if why:
                    problems.append(f"{display} is root's and {why}; root leaves it")
                    continue
            hand.append((fd, path))
        if problems or not apply:
            return ([] if problems else [str(path) for _fd, path in hand]), problems
        given: list[str] = []
        for fd, path in hand:
            try:
                os.fchown(fd, account.pw_uid, account.pw_gid)
            except OSError as exc:
                # What was given stays given -- the next run finishes the rest -- and this is said, never raised.
                return given, [f"{path} could not be given to {owner} ({exc.strerror})"]
            given.append(str(path))
        return given, []
    finally:
        for fd in held:
            os.close(fd)
        for root in roots:
            root.close()


def restore_command(
    config: ProjectConfig,
    *,
    config_path: Path,
    dry_run: bool,
    print_func: Callable[[str], None] = print,
) -> int:
    """`switchyard upgrade <project> --only workflow-projection`: give the tenant back its workflow projection, and
    nothing else -- no release resolved, no phase run, root's own files left root's."""
    repair = REPAIR.format(project=config.project)
    if os.geteuid() != 0:
        print_func(f"switchyard: giving {config.project}'s workflow projection back is root's: `{repair}`. "
                   "Nothing was changed.")
        return 1
    handed, problems = restore_projection_ownership(config, config_path, apply=not dry_run)
    for problem in problems:
        print_func(f"switchyard: {problem}")
    if problems:
        print_func(f"switchyard: {config.project}'s workflow projection was left exactly as it was; no upgrade "
                   "phase ran.")
        return 1
    if not handed:
        print_func(f"switchyard: {config.project}'s workflow projection is already {config.run_as_user}'s; nothing "
                   "to do, and no upgrade phase ran.")
        return 0
    verb = "would give" if dry_run else "gave"
    print_func(f"switchyard: {verb} {config.run_as_user} back {', '.join(handed)}"
               + ("; nothing written" if dry_run else "")
               + f". Root's own records beside them stay root's; no upgrade phase ran. {config.project}'s Director "
               "can now change its roles, stages and role prompts without root.")
    return 0


def resume_handoff(config: ProjectConfig, *, config_path: Path) -> list[str]:
    """What `resume-provision` says after handing the projection over, as `switchyard new` would have done."""
    handed, problems = restore_projection_ownership(config, config_path, apply=True)
    if problems:
        return [f"switchyard: {problem}" for problem in problems] + [
            f"switchyard: {config.project}'s workflow projection was left as it was, so its Director cannot change "
            f"its workflow yet; once that is addressed, `{REPAIR.format(project=config.project)}`."]
    if handed:
        return [f"switchyard: gave {config.run_as_user} back its workflow projection ({', '.join(handed)})"]
    return []
