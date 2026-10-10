"""Where root keeps a tenant's provisioning records, and the root-only directory that holds them.

- `switchyard_privileged_provision_root` is the root of root's provisioning
  copies, through the documented override that lets the suites exercise the
  real root branch without /etc; `privileged_baseline_plan_path` and
  `workflow_record_path` name root's own plan and workflow record there.
- `recorded_declared_workflow` reads root's workflow record the way root reads
  its other authorities, telling a missing record from an unusable one.
- `PRIVILEGED_PROVISION_DIR_MODE`, `PRIVILEGED_ARTIFACT_MODE`,
  `PRIVILEGED_EXECUTABLE_ARTIFACT_MODE` and `privileged_artifact_mode` are the
  modes the directory and what is in it must have.
- `ensure_privileged_provision_dir` creates or repairs that directory, refusing
  one that is a symlink, not a directory or not root's.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-383), in their original
order. The launcher imports this module and re-exports every name, so the
launcher's own callers, the seventeen privileged modules that read these
through it, and every suite that patches them there reach the same objects.
Every launcher facility these use -- the override's variable, the default
root, the provision-directory builder and the no-follow readers, which stay
elsewhere -- and every name defined here that another definition here reads
when it runs, the modes included, is read from `team_launcher` when it runs,
as it was, so a patch on the launcher still intercepts. The two
`project_provision` names are still imported inside the functions that use
them. The standard-library names are this module's own imports, the same
objects. This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


def switchyard_privileged_provision_root() -> Path:
    """Where root keeps the copies of generated artifacts it installs from.

    Overridable so tests can exercise the real root branch without writing to
    the host's /etc (SYRD-39).
    """
    from scripts import team_launcher as launcher

    configured = os.environ.get(launcher.PRIVILEGED_PROVISION_ROOT_ENV, "").strip()
    return Path(configured).expanduser() if configured else launcher.DEFAULT_PRIVILEGED_PROVISION_ROOT


def privileged_baseline_plan_path(project: str) -> Path:
    """Root's own copy of the plan, the only one it renders privileged artifacts from."""
    from scripts import team_launcher as launcher

    return launcher.privileged_provision_dir(project, root=launcher.switchyard_privileged_provision_root()) / "plan.json"


def workflow_record_path(project: str) -> Path:
    from scripts import team_launcher as launcher

    from scripts.ticket_board.project_provision import WORKFLOW_RECORD_NAME

    return launcher.privileged_baseline_plan_path(project).with_name(WORKFLOW_RECORD_NAME)


def recorded_declared_workflow(project: str) -> tuple[dict | None, str]:
    """The declared workflow root holds for this project, or why it holds none.

    Root's own copy, read the way root reads its other authorities: by fd,
    refusing a symlink at every component, and required to belong to root and
    to be unwritable by anybody else. The tenant's configuration carries this
    document too, and that copy is not consulted here -- it decides which roles
    exist and what each of them may call, so a copy the account every role runs
    as can write is a copy that account could grant itself with (SYRD-165).
    """
    from scripts import team_launcher as launcher

    from scripts.ticket_board.project_provision import workflow_record_document

    path = launcher.workflow_record_path(project)
    try:
        # Absence and unusability are different answers, and asked separately
        # rather than read out of the wording of a refusal.
        path.lstat()
    except FileNotFoundError:
        return None, f"root holds no recorded workflow for {project}"
    except OSError as exc:
        return None, f"root's workflow record for {project} could not be read: {exc}"
    document, problem = launcher.read_plan_no_follow(path, require_root_owned=True)
    if document is None:
        return None, problem
    return workflow_record_document(document.data, project=project)


#: A tenant's root-owned provisioning directory, and what is in it. Together
#: the plan, the operator packet, the database and workflow SQL, the workflow
#: record and the publication remote describe the whole authority model of a
#: tenant -- which accounts exist, what each may call, where the board's socket
#: and database are. None of that is a secret root shares with the accounts it
#: is about, so the directory is root's alone and the files inside it are too:
#: a mode that leans on the directory is one chmod away from being nothing
#: (SYRD-176, syrd rollout journal 0084).
PRIVILEGED_PROVISION_DIR_MODE = 0o700
PRIVILEGED_ARTIFACT_MODE = 0o600
PRIVILEGED_EXECUTABLE_ARTIFACT_MODE = 0o700


def privileged_artifact_mode(name: str) -> int:
    """The least a root-owned artifact needs to be what its consumer runs.

    Root executes the packets and the migration script and reads everything
    else; nobody else does either, so nothing here is readable beyond root.
    """
    from scripts import team_launcher as launcher

    return launcher.PRIVILEGED_EXECUTABLE_ARTIFACT_MODE if name.endswith(".sh") else launcher.PRIVILEGED_ARTIFACT_MODE


def ensure_privileged_provision_dir(target: Path, *, root: Path | None = None) -> list[str]:
    """Create or repair a tenant's root-only provisioning directory.

    Returns what it had to repair, so a caller can tell an operator what
    changed. Every writer under this directory goes through here, because the
    invariant is only as good as the last thing that created the directory --
    it was documented as root-only while one of these writers chmod'd it 0755
    on every run (SYRD-176).

    Refusals are loud. A directory that is a symlink, or that belongs to
    somebody other than root, is not one root may publish a plan into: closing
    it would be closing whatever it points at, and writing to it would be
    writing where its owner can read and replace what root then installs.
    """
    from scripts import team_launcher as launcher

    base = Path(root) if root is not None else launcher.switchyard_privileged_provision_root()
    owner = launcher.expected_privileged_uid()
    # The root's own parents (SYRD-575). On a fresh host /etc/switchyard does
    # not exist until something makes it, and the walk below creates only from
    # the root down, so a first project could not stage anything. The nearest
    # parent that exists -- the one the rest is made in, /etc/switchyard on a
    # host that has a project, /etc on a fresh one -- is judged before anything
    # is made, so a refusal leaves the host as it was; the missing ones are made
    # root's and 0755, as `install -d` makes /etc/switchyard/publish, at that
    # mode from the start. A sticky directory's owner is not asked: it is shared
    # by design, and inside a user namespace root's /tmp is owned by nobody.
    missing: list[Path] = []
    for directory in base.parents:
        try:
            info = directory.lstat()
        except FileNotFoundError:
            missing.append(directory)
            continue
        sticky = bool(info.st_mode & stat.S_ISVTX)
        why = ""
        if stat.S_ISLNK(info.st_mode):
            why = "is a symlink"
        elif not stat.S_ISDIR(info.st_mode):
            why = "is not a directory"
        elif info.st_uid not in (0, os.getuid()) and not sticky:
            why = f"is owned by uid {info.st_uid} rather than by root"
        elif info.st_mode & 0o022 and not sticky:
            why = f"can be written by others (mode {stat.S_IMODE(info.st_mode):04o})"
        if why:
            raise SystemExit(
                f"switchyard: {directory} {why}, so it cannot hold root's provisioning directory {base}: "
                "what root keeps under it could be replaced. Nothing was written."
            )
        break
    for directory in reversed(missing):
        umask = os.umask(0o022)
        try:
            directory.mkdir(mode=0o755, exist_ok=True)
        finally:
            os.umask(umask)
        made = directory.lstat()  # read back: what was made is what is there
        if stat.S_ISLNK(made.st_mode) or not stat.S_ISDIR(made.st_mode):
            raise SystemExit(f"switchyard: {directory} changed while it was being made. Nothing more was written.")
    repaired: list[str] = []
    for directory in (*reversed(target.parents), target):
        private = directory == target
        if not (private or directory.is_relative_to(base)):
            continue
        try:
            info = directory.lstat()
        except FileNotFoundError:
            directory.mkdir(mode=launcher.PRIVILEGED_PROVISION_DIR_MODE if private else 0o755)
            info = directory.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise SystemExit(
                f"switchyard: {directory} is a symlink, so it is not a directory root will "
                f"publish {target.name}'s provisioning artifacts into. Nothing was written."
            )
        if not stat.S_ISDIR(info.st_mode):
            raise SystemExit(
                f"switchyard: {directory} is not a directory, so root's provisioning artifacts "
                "have nowhere to go. Nothing was written."
            )
        if info.st_uid != owner:
            raise SystemExit(
                f"switchyard: {directory} is owned by uid {info.st_uid} rather than by root, so "
                "what root publishes there would be its owner's to read and replace. Nothing "
                "was written."
            )
        wanted = launcher.PRIVILEGED_PROVISION_DIR_MODE if private else 0o755
        if stat.S_IMODE(info.st_mode) != wanted:
            if private:
                repaired.append(
                    f"closed {directory} to root only (was mode "
                    f"{stat.S_IMODE(info.st_mode):04o})"
                )
            directory.chmod(wanted)
        try:
            os.chown(directory, 0, 0)
        except OSError:
            # Attempted, not relied on. What decides whether root publishes here
            # is the ownership check above, which reads the directory back --
            # and through the documented provision-root seam "root" is the
            # caller's own uid, which it cannot chown away from itself.
            pass
    return repaired
