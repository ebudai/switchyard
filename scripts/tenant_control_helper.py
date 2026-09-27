"""The tenant-control helper: who may drive a tenant without sudo, and whether the programs root runs for it are safe and current.

- `_tenant_control_can_serve`, `_tenant_control_grant` and
  `_tenant_control_operation` decide, narrowly, whether an invocation is one
  the caller's control bridge runs: the lifecycle verbs in
  `TENANT_CONTROL_OPERATIONS`, for a tenant whose root-owned grant under
  `TENANT_CONTROL_ROOT` names this caller.
- `TenantControlHelperState` and `tenant_control_helper_state` verify a staged
  program as a pinned exec target owned by `TENANT_CONTROL_OWNER_UID` (root,
  never whoever is asking), telling absence apart from hostility.
- `tenant_control_repair_command` and `repair_tenant_control_helper` re-stage
  a tenant's tooling through the recorded privileged path, labelled
  `TENANT_CONTROL_REPAIR_LABEL`.
- `PROTOCOL_STAGED_EXECUTABLES`, `staged_protocol_states`,
  `staged_tooling_out_of_date` and `ensure_tenant_control_helper` refuse,
  repair or carry on per protocol program -- hostile, absent, or safe but
  stale -- and verify shape and content again after a repair.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-391), in their original
order. The launcher imports this module and re-exports every name, so its own
callers, the three modules that read these through it, and every suite that
patches or rebinds them there reach the same objects. Every launcher facility
these use -- the slug pattern, the shared install root, the current user, the
rollout recorder, and the two provisioning helpers the launcher imports -- and
every name defined here that another definition here reads when it runs, the
class and constants included, is read from `team_launcher` when it runs, as it
was, so a patch on the launcher still intercepts. The `runner` and
`print_func` defaults are bound when the functions are defined, as they were.
The standard-library names are this module's own imports, the same objects.
This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


def _tenant_control_can_serve(argv: Sequence[str]) -> bool:
    """Whether this exact invocation is one the caller's control bridge runs.

    Deliberately narrow and quiet: an unrecognised shape, an unknown project or
    any error means "no", so the answer can only ever remove an escalation that
    the bridge is about to make unnecessary, never add one.
    """
    from scripts import team_launcher as launcher

    if len(argv) != 2:
        return False
    project = argv[1]
    if argv[0].casefold() not in launcher.TENANT_CONTROL_OPERATIONS:
        return False
    if not launcher.PROJECT_SLUG_RE.fullmatch(project):
        return False
    try:
        grant = launcher._tenant_control_grant(project)
        return bool(grant) and grant.get("authorized_user") == launcher.current_user_name()
    except Exception:  # pragma: no cover - classification must never raise
        return False


#: Lifecycle verbs the tenant control bridge will run. Everything else keeps
#: the operator path: the bridge exists for start/stop/status, not for
#: provisioning, upgrade or teardown.
TENANT_CONTROL_OPERATIONS = {"start", "stop", "status", "recover-display"}
TENANT_CONTROL_ROOT = Path("/usr/local/lib/switchyard")


def _tenant_control_grant(project: str, *, root: Path | None = None) -> dict[str, str]:
    """The root-owned record of who may drive this tenant without sudo.

    Read here only to decide whether to use the bridge and to refuse an
    unauthorized caller without a password prompt. The bridge re-reads and
    re-validates it as root; nothing is trusted on the strength of this read.
    """
    from scripts import team_launcher as launcher

    base = root or launcher.TENANT_CONTROL_ROOT
    try:
        payload = json.loads((base / project / "control-grant.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or payload.get("project") != project:
        return {}
    return {str(key): str(value) for key, value in payload.items()}


def _tenant_control_operation(argv: Sequence[str], project_argument: str) -> str:
    """Which lifecycle verb this invocation is, if the bridge can run it."""
    from scripts import team_launcher as launcher

    if not argv:
        return ""
    head = argv[0].casefold()
    if head == project_argument.casefold():
        # `switchyard <slug>` with nothing else is the ordinary start/attach.
        return "start" if len(argv) == 1 else ""
    if head in launcher.TENANT_CONTROL_OPERATIONS and len(argv) == 2 and argv[1].casefold() == project_argument.casefold():
        return head
    return ""


#: The label the repair leaves in the rollout journal, so an operator reading it
#: can tell a staged-tool repair apart from a provisioning or upgrade run.
TENANT_CONTROL_REPAIR_LABEL = "tenant-control-repair"


#: Root, and never "whoever is asking".
#:
#: These staged paths are root-owned by requirement -- that is the whole point
#: of staging them outside the owner's home -- while this verification runs in
#: the operator's UNPRIVILEGED launcher process. So the entitled identity is a
#: property of the path, not of the caller, and
#: `untrusted_root_executable_reasons` defaulting `owner_uid` to this process's
#: own uid is exactly wrong here: live Zorin UAT rejected a correctly installed
#: tenant because verification ran as uid 1000 and demanded that uid on
#: root-owned files (SYRD-211 kickback).
TENANT_CONTROL_OWNER_UID = 0


@dataclass(frozen=True)
class TenantControlHelperState:
    """What the registered helper actually is, before root is asked to run it.

    Absence and hostility are different answers, and collapsing them is what
    SYRD-211 is. A tenant whose provisioning was interrupted after the grant and
    the sudoers rule were written, or one that predates staging, has a perfectly
    valid registration and no file -- that is recoverable by re-staging. A file
    that exists with the wrong owner, the wrong mode, or a symlink in its path
    is not recoverable by anything this program should do on its own.
    """

    project: str
    path: Path
    present: bool
    reasons: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return self.present and not self.reasons

    @property
    def repairable(self) -> bool:
        return not self.present and not self.reasons


def tenant_control_helper_state(
    project: str,
    *,
    grant: Mapping[str, str] | None = None,
    root: Path | None = None,
    owner_uid: int | None = None,
    name: str = "switchyard-tenant-control",
) -> TenantControlHelperState:
    """Verify the helper as a pinned exec target, not just as a filename.

    The whole path is checked, not the leaf: a root-owned program in a directory
    somebody else can write is a program somebody else can replace, and the
    sudoers rule names the path rather than the bytes. That check already exists
    for exactly this reason (`untrusted_root_executable_reasons`, SYRD-62) and
    is reused here rather than approximated.

    Cross-tenant isolation is decided before any filesystem call. The slug comes
    from the command line, so a slug carrying a separator would otherwise aim
    this -- and the repair that follows -- at another tenant's directory.

    `owner_uid` is the identity entitled to have written all of it, and it
    defaults to root rather than to this process. A sandbox that cannot create
    root-owned files passes its own uid to stand in for root; production never
    does, because the caller here is deliberately unprivileged.
    """
    from scripts import team_launcher as launcher

    entitled = launcher.TENANT_CONTROL_OWNER_UID if owner_uid is None else owner_uid
    base = Path(root) if root is not None else launcher.TENANT_CONTROL_ROOT
    reasons: list[str] = []
    slug = str(project)
    if not slug or slug in {".", ".."} or "/" in slug or "\\" in slug or "\x00" in slug:
        # Refused without touching the disk: this value chooses the directory.
        return launcher.TenantControlHelperState(
            project=slug,
            path=base,
            present=False,
            reasons=(f"{slug!r} is not a project slug this may be aimed at",),
        )
    path = base / slug / name
    if grant:
        # Only a value that is there and disagrees. `_tenant_control_grant`
        # already refuses a grant whose project is not this one and returns an
        # empty mapping, so an absent key here means "no grant was read", which
        # that caller handles -- reading it as a disagreement refuses tenants
        # whose grant simply was not loaded.
        recorded = str(grant.get("project") or "")
        if recorded and recorded != slug:
            reasons.append(
                f"the grant at {base / slug} records project {recorded} rather than {slug}"
            )
    try:
        path.lstat()
        present = True
    except FileNotFoundError:
        present = False
    except OSError as exc:
        return launcher.TenantControlHelperState(
            project=slug, path=path, present=False,
            reasons=(*reasons, f"{path} cannot be inspected: {exc}"),
        )
    if present:
        reasons.extend(
            launcher.untrusted_root_executable_reasons(path, boundary=base, owner_uid=entitled)
        )
        try:
            if not path.lstat().st_mode & 0o111:
                reasons.append(f"{path} is not executable")
        except OSError as exc:  # pragma: no cover - lstat succeeded a moment ago
            reasons.append(f"{path} cannot be inspected: {exc}")
    return launcher.TenantControlHelperState(
        project=slug, path=path, present=present, reasons=tuple(reasons)
    )


def tenant_control_repair_command(
    project: str, *, release_root: str = "", root: Path | None = None
) -> str:
    """The recorded privileged command that re-stages this tenant's tooling.

    Deliberately the whole staging step rather than one `install` of one file.
    That step is the supported way these executables reach the shared path, it
    is idempotent by construction -- each name is installed if the release
    carries it and removed if it does not -- and it is scoped to this project's
    directory, so no other tenant is touched by a repair.
    """
    from scripts import team_launcher as launcher

    release = release_root or str(launcher.switchyard_shared_install_root() / "current")
    commands = launcher.role_tooling_staging_commands(project, release, staging_root=root)
    script = "\n".join(commands)
    recorder = launcher._rollout_recorder_path()
    if recorder is None:
        return f"bash -c {shlex.quote(script)}"
    return " ".join(
        [
            "sudo",
            shlex.quote(str(recorder)),
            shlex.quote(project),
            "--label",
            launcher.TENANT_CONTROL_REPAIR_LABEL,
            "--",
            "bash",
            "-c",
            shlex.quote(script),
        ]
    )


def repair_tenant_control_helper(
    project: str,
    *,
    release_root: str = "",
    root: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> str:
    """Re-stage the missing helper through the recorded privileged path.

    Returns an empty string when the helper is usable afterwards, and the reason
    it is not otherwise. The repair is announced before it runs because it asks
    for a privileged step the operator did not type.
    """
    from scripts import team_launcher as launcher

    command = launcher.tenant_control_repair_command(project, release_root=release_root, root=root)
    print_func(
        f"switchyard: {project}: the tenant control helper is not staged; repairing it from "
        "the current release before continuing"
    )
    result = runner(["bash", "-c", command])
    code = int(getattr(result, "returncode", 1) or 0)
    if code != 0:
        return (
            f"repairing the tenant control helper for {project} failed (exit {code}); "
            "the rollout journal records the attempt"
        )
    return ""


#: The staged programs whose WIRE CONTRACT this launch depends on: the bridge
#: it crosses and the helper each tab of the window runs. Deliberately not every
#: staged executable -- restaging a tenant because its `directorctl` differs
#: would make an ordinary launch privileged on any drift at all, and say nothing
#: about whether the window can be opened (SYRD-211 DAT rejection).
PROTOCOL_STAGED_EXECUTABLES: tuple[str, ...] = (
    "switchyard-tenant-control",
    "switchyard-display-attach",
)


def staged_protocol_states(
    project: str,
    *,
    grant: Mapping[str, str] | None = None,
    root: Path | None = None,
    owner_uid: int | None = None,
) -> dict[str, TenantControlHelperState]:
    """Each protocol program, checked as a pinned exec target in its own right.

    Only the bridge used to be checked, and the other one was then compared with
    `is_file()` and `read_bytes()` -- which follow symlinks and ask nothing
    about ownership or mode. So a world-writable display helper read as ordinary
    version drift and was restaged over, and a symlink whose target happened to
    carry the current bytes read as current and became the pathname root's
    display grant executes (SYRD-211 DAT rejection).

    The grant is checked once, against the tenant, rather than once per file.
    """
    from scripts import team_launcher as launcher

    states: dict[str, TenantControlHelperState] = {}
    for index, name in enumerate(launcher.PROTOCOL_STAGED_EXECUTABLES):
        states[name] = launcher.tenant_control_helper_state(
            project,
            grant=grant if index == 0 else None,
            root=root,
            owner_uid=owner_uid,
            name=name,
        )
    return states


def staged_tooling_out_of_date(
    project: str,
    *,
    release_root: str = "",
    root: Path | None = None,
    only: Sequence[str] | None = None,
) -> list[str]:
    """Staged protocol programs whose bytes are not this release's.

    Answered only for files that have already passed the shape check: comparing
    bytes is reading a file, and reading one root is about to overwrite -- or
    that root's grant is about to execute -- is a question to ask after "is this
    a root-owned regular file at a root-controlled path", not before.

    Present is not current. A tenant repaired or provisioned by an earlier
    release keeps that release's staged copies for ever: nothing restages them
    merely because the shared release moved on, and a shape check is satisfied
    by an executable of the right shape. The preserved Zorin tenant held a
    `switchyard-display-attach` that accepts only numeric slots, so a window
    asking it for `viewer` would have been refused by the tenant's own copy
    however correct the release was (SYRD-211 DAT rejection).
    """
    from scripts import team_launcher as launcher

    release = Path(release_root) if release_root else launcher.switchyard_shared_install_root() / "current"
    staging = Path(root) if root is not None else launcher.TENANT_CONTROL_ROOT
    staging = staging / project
    names = list(only) if only is not None else list(launcher.PROTOCOL_STAGED_EXECUTABLES)
    stale: list[str] = []
    for name in names:
        source = release / "scripts" / name
        if not source.is_file():
            # Not in this release: the staging step removes it, which is that
            # step's business rather than this check's.
            continue
        target = staging / name
        try:
            if target.is_symlink() or not target.is_file():
                stale.append(name)
                continue
            if target.read_bytes() != source.read_bytes():
                stale.append(name)
        except OSError:
            stale.append(name)
    return stale


def ensure_tenant_control_helper(
    project: str,
    *,
    grant: Mapping[str, str] | None = None,
    release_root: str = "",
    root: Path | None = None,
    owner_uid: int | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> None:
    """Every protocol program: refuse, repair, or carry on -- decided per file.

    Three states, kept apart for each of them, because collapsing any two is
    what this ticket keeps finding:

    * **hostile** -- wrong owner, writable by others, a symlink anywhere in its
      path, not a regular executable. Refused, and no privileged step is run.
      Restaging over it would overwrite evidence and reward whoever put it
      there; accepting it hands root's grant a pathname somebody else controls.
    * **absent** -- a valid registration with no file. Re-staged.
    * **safe but stale** -- this release would install different bytes. Also
      re-staged, because present is not current.

    The shape of every protocol file is settled before any of them is read, and
    both shape and content are checked again after a repair (SYRD-211).
    """
    from scripts import team_launcher as launcher

    states = launcher.staged_protocol_states(
        project, grant=grant, root=root, owner_uid=owner_uid
    )
    hostile = {name: state for name, state in states.items() if state.reasons}
    if hostile:
        lines: list[str] = []
        for name, state in hostile.items():
            lines.append(f"switchyard: refusing to run {state.path} as root:")
            lines.extend(f"switchyard:   {reason}" for reason in state.reasons)
        lines.append(
            "switchyard: this is not repaired automatically, and nothing was run in its "
            "place. A staged program somebody else can write is not version drift"
        )
        raise SystemExit("\n".join(lines))

    absent = [name for name, state in states.items() if not state.present]
    safe = [name for name, state in states.items() if state.present]
    stale = launcher.staged_tooling_out_of_date(
        project, release_root=release_root, root=root, only=safe
    )
    if not absent and not stale:
        # Correct, current, and nothing rewritten: a second launch costs a few
        # stats and no privileged step.
        return
    if absent:
        for name in absent:
            print_func(
                f"switchyard: {project}: {name} is not staged; repairing it from the "
                "current release before continuing"
            )
    if stale:
        print_func(
            f"switchyard: {project}'s staged tooling is from an older release "
            f"({', '.join(stale)}); restaging it from the current one before continuing"
        )
    problem = launcher.repair_tenant_control_helper(
        project, release_root=release_root, root=root, runner=runner, print_func=print_func
    )
    if problem:
        raise SystemExit(f"switchyard: {problem}")

    # Shape AND content, for every protocol file, before anything crosses.
    states = launcher.staged_protocol_states(
        project, grant=grant, root=root, owner_uid=owner_uid
    )
    unusable = {name: state for name, state in states.items() if not state.usable}
    remaining = launcher.staged_tooling_out_of_date(
        project,
        release_root=release_root,
        root=root,
        only=[name for name, state in states.items() if state.present],
    )
    if not unusable and not remaining:
        for name in absent:
            print_func(
                f"switchyard: {project}: {name} repaired at {states[name].path}"
            )
        print_func(f"switchyard: {project}'s staged tooling is this release's")
        return
    detail = "; ".join(
        [
            *(
                f"{name}: {'; '.join(state.reasons) or 'still not staged'}"
                for name, state in unusable.items()
            ),
            *(f"{name}: still not this release's" for name in remaining),
        ]
    )
    raise SystemExit(
        f"switchyard: {project}'s staged tooling is still unusable after repair, so it "
        f"could not be brought up to this release: {detail}"
    )
