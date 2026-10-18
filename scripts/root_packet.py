"""What root runs and installs is its own copy, never the one in the tenant's provision directory (SYRD-607).

`switchyard new` writes the operator packet into the project's
`.switchyard/provision`, and `add-role` and `set-vcs-close-role` write the
board unit there. That directory belongs to the tenant whenever the project
lives under an owner who already exists or under a path the owner can write,
so a file root reopened there by path could have been replaced between the
write and the run or install -- a root shell script, or a root unit, with the
tenant's content.

- `stage_root_packet` renders every artifact once, in a mode-0700 directory
  inside root's own provisioning directory, mirrors them there and returns the
  bytes and that directory. The packet root runs, and every companion it reads
  from its own directory, is that mirror.
- `publish_packet_record` writes the same bytes into the tenant's directory as
  a record, then `record_root_use` names, in `root-provenance.json` beside
  them, the digest of each file root ran or installed and where root used it
  from. Replacing or deleting the record changes nothing root does; a record
  that could not be written is reported and is given no provenance entry.
- `root_execute_command` is the only root command a dry run prints: the same
  `new` again with --execute, so root stages and runs its own copy. No packet
  path is ever handed to root, and `new --execute` without root is refused.
  The program it names is `trusted_launcher`, the installed release's, and
  only when `root_controlled` proves every component of it root's -- never a
  script in the checkout the dry run ran from, which its owner can change.

This module imports `team_launcher` only inside the functions, when they run.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Mapping, Sequence

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectBoardProvision

PACKET_NAME = "operator-commands.sh"
RECORD_NAME = "root-provenance.json"


def digest(body: bytes | str) -> str:
    return hashlib.sha256(body.encode("utf-8") if isinstance(body, str) else body).hexdigest()


def stage_root_packet(
    plan: ProjectBoardProvision, *, enable_owner_linger: bool
) -> tuple[dict[str, bytes], Path]:
    """Render once where only root can reach, and mirror there what root runs.

    The rendering directory is made inside root's provisioning directory for
    this project, whose whole ancestry `ensure_privileged_provision_dir` has
    just judged, rather than under a temporary directory somebody else may have
    chosen. Any failure stops here, before anything privileged has run.
    """
    from scripts import team_launcher as launcher

    target = launcher.privileged_provision_dir(plan.project, root=launcher.switchyard_privileged_provision_root())
    try:
        launcher.ensure_privileged_provision_dir(target)
        rendered = launcher.render_privileged_artifacts(
            plan, enable_owner_linger=enable_owner_linger, staging_root=target
        )
        mirrored = launcher.install_privileged_artifacts(plan, rendered)
    except OSError as exc:
        raise SystemExit(
            f"team-launcher: could not stage {plan.project}'s packet where only root can reach it: {exc}. "
            "Nothing was run: root runs only its own copy of the packet."
        ) from exc
    problems = launcher.root_controlled_problems_for(str(mirrored / PACKET_NAME))
    if problems:
        raise SystemExit(
            f"team-launcher: root's copy of {plan.project}'s packet is not root's alone: "
            + "; ".join(problems) + ". Nothing was run."
        )
    return rendered, mirrored


def publish_packet_record(artifact_dir: Path, rendered: Mapping[str, bytes], *, root_copy: Path,
                          owner_user: str = "") -> str:
    """The tenant's copy, from the bytes root rendered; returns what went wrong, or "".

    The old provenance record goes first, so a record that fails half-way is
    never described as matching what root ran. In `owner_user`'s tree -- the
    tenant's provision directory under `switchyard new` -- each file is root's
    own, removed, made and replaced by descriptor from the owner's root, so a
    link the tenant left at a name or at any directory above it is refused,
    never followed (SYRD-611).
    """
    from scripts import owner_writes

    in_tree = owner_writes.root_writes_in(owner_user, artifact_dir)
    try:
        if in_tree:
            owner_writes.remove_root_record(owner_user, artifact_dir / RECORD_NAME)
        else:
            (artifact_dir / RECORD_NAME).unlink(missing_ok=True)
            artifact_dir.mkdir(parents=True, exist_ok=True)
        for name, body in rendered.items():
            mode = 0o755 if name.endswith(".sh") else 0o644
            if in_tree:
                owner_writes.write_root_record(owner_user, artifact_dir / name, body, mode=mode, parents_mode=0o755)
            else:
                _write_record_file(artifact_dir / name, body, mode, exclusive=False)
        record_root_use(
            artifact_dir,
            {name: (body, str(root_copy / name)) for name, body in rendered.items() if name != "plan.json"},
            owner_user=owner_user if in_tree else "",
        )
    except SystemExit as stop:
        return (
            f"could not publish the record of the packet in {artifact_dir}: {stop}. Root ran its own copy "
            f"in {root_copy}; what {artifact_dir} holds is not a record of it"
        )
    except OSError as exc:
        return (
            f"could not publish the record of the packet in {artifact_dir}: {exc}. Root ran its own copy "
            f"in {root_copy}; what {artifact_dir} holds is not a record of it"
        )
    return ""


def record_root_use(directory: Path, used: Mapping[str, tuple[bytes | str, str]], *, owner_user: str = "") -> Path:
    """Name, beside the record, the digest of each file root used and where it used it from.

    Merged into what is there, so an updated unit replaces its own entry and
    leaves the packet's. What is there is read without following a link, and
    is only ever a record: root never reads it to decide anything. As root for
    `owner_user`, it is written as root's own file by descriptor from the
    owner's home, never through a link at any directory above it, and replaces
    the old record whole rather than unlinking it by name (SYRD-608).
    """
    from scripts import team_launcher as launcher

    path = directory / RECORD_NAME
    if owner_user and launcher.root_writes_for(owner_user):
        # By descriptor from the owner's root: nothing reached through a link above the record is carried into it.
        from scripts import owner_writes

        try:
            raw = owner_writes.read_owner_bytes(owner_user, path)
            existing = json.loads(raw.decode("utf-8")) if raw is not None else None
        except (SystemExit, ValueError):
            existing = None
    else:
        existing, _problem = launcher.read_tenant_document_no_follow(path, what="the record of what root used")
    files = existing.get("files") if isinstance(existing, dict) else None
    entries = dict(files) if isinstance(files, dict) else {}
    for name, (body, used_from) in used.items():
        entries[name] = {"sha256": digest(body), "root_used": used_from}
    document = {
        "about": "what root ran or installed for this project; the files beside this are records, not its source",
        "files": dict(sorted(entries.items())),
    }
    body = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if owner_user and launcher.root_writes_for(owner_user):
        from scripts import owner_writes

        owner_writes.write_root_record(owner_user, path, body, mode=0o644)
        return path
    path.unlink(missing_ok=True)
    _write_record_file(path, body, 0o644, exclusive=True)
    return path


def record_unit_install(provision_dir: Path, plan: ProjectBoardProvision, board_unit: str) -> str:
    """After `add-role` or `set-vcs-close-role` installed the unit: its entry in the record, or why not."""
    target = f"/etc/systemd/system/{plan.board_unit}"
    try:
        record_root_use(provision_dir, {plan.board_unit: (board_unit, target)}, owner_user=plan.owner_user)
    except (OSError, SystemExit) as exc:
        return (
            f"installed {target} (sha256 {digest(board_unit)}) from the text root rendered, but could not "
            f"record that in {provision_dir}: {exc}"
        )
    return ""

def _write_record_file(path: Path, body: bytes, mode: int, *, exclusive: bool) -> None:
    """One record file, refusing a link at its name rather than writing through it."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_EXCL if exclusive else os.O_TRUNC)
    descriptor = os.open(path, flags, mode)
    try:
        view = memoryview(body)
        while view:
            view = view[os.write(descriptor, view):]
        os.fchmod(descriptor, mode)
    finally:
        os.close(descriptor)


def root_controlled(path: Path) -> str:
    """Why `path` is not root's from `/` down, or "" when it is.

    The whole-path proof installed releases get (`root_controlled_problems`),
    always for uid 0 and always from `/`: a dry run is read by its caller, and
    a path that caller owns is exactly what root must not run. Nothing in the
    environment relaxes it -- `SWITCHYARD_SHARED_INSTALL_ROOT` may say where to
    look, never whose files count as root's (SYRD-607 audit).
    """
    from scripts.ticket_board.publication_boundary import root_controlled_problems

    problems = root_controlled_problems(str(path), expect_uid=0, base="/")
    return problems[0] if problems else ""


def trusted_launcher() -> tuple[Path | None, str]:
    """The active installed release's team-launcher, proven root's, or why there is none.

    Named through `current` as it resolves now, so the command runs exactly the
    release that was proven here even if `current` moves before it is run.
    """
    from scripts import team_launcher as launcher

    script = launcher.switchyard_shared_install_root() / "current" / "scripts" / "team-launcher"
    if not script.is_file():
        return None, f"no installed release provides {script}"
    resolved = script.resolve(strict=True)
    problem = root_controlled(resolved)
    return (None, f"{resolved} is not root's: {problem}") if problem else (resolved, "")


def root_execute_command(project: str, *, script: Path, options: Sequence[tuple[str, object]]) -> str:
    """The one root command a dry run hands an operator: the same `new`, executing, as root.

    Root then renders, stages and runs its own copy. No packet path is in it:
    whatever the dry run wrote is its caller's to replace before the command is
    run. Paths are absolute, so the directory the command is run from changes
    nothing; `headless` is a desktop policy's name, not a path.
    """
    argv = ["sudo", str(script), project, "new"]
    for flag, value in options:
        if value is None or value == "":
            continue
        if isinstance(value, Path) and str(value) != "headless":
            value = value.expanduser().resolve(strict=False)
        argv += [flag, str(value)]
    return shlex.join([*argv, "--execute"])
