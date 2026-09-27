"""Which generated configuration root will register for a tenant, and the record of it.

- `TENANT_CONFIG_RECORD_NAME` and `tenant_config_record_path` name root's record
  of the configuration it verified; `recorded_tenant_config_path` reads it and
  `record_tenant_config_path` writes it where only root can rewrite it.
- `registered_tenant_config_path` is the pointer this host's registry holds,
  read only from a root-owned entry and only if absolute.
- `normalize_tenant_config_mode` takes group and world write off a candidate on
  one no-follow descriptor, before the strict reader runs (SYRD-167).
- `_tenant_config_candidates` orders where the configuration could be,
  `tenant_config_conflicts` names where one disagrees with what root
  provisioned, `board_declared_role_names` reads the running board's declared
  roles, and `verified_tenant_config` returns the one root is willing to
  register, or why not, corroborating added roles fail-closed (SYRD-167,
  SYRD-168).

Moved out of `scripts/team_launcher.py` unchanged (SYRD-362). The launcher
imports this module at its top and re-exports every name, so resume-provision
and the modules that read them through the launcher -- `pane_rebind`,
`workflow_adoption` -- reach the same objects. Every launcher facility these
use, and every name defined here that another definition here reads, is read
from `team_launcher` when it runs, as it was. The standard-library names are
this module's own imports, the same objects. This module never imports
`team_launcher` at its top.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectBoardProvision, ProjectConfig


#: Written beside root's plan record once root has verified which generated
#: configuration belongs to this project. It is the one fact about the tenant
#: that root cannot regenerate: the operator chooses where the project checkout
#: lives, so the path to its configuration is not derivable from the plan.
#: It is re-verified on every read and never believed on its own.
TENANT_CONFIG_RECORD_NAME = "tenant-config.json"


def tenant_config_record_path(slug: str) -> Path:
    from scripts import team_launcher as launcher

    return launcher.privileged_baseline_plan_path(slug).with_name(launcher.TENANT_CONFIG_RECORD_NAME)


def normalize_tenant_config_mode(path: Path, *, permitted_uids: "list[int]") -> list[str]:
    """Take group and world write off a configuration root is about to verify.

    `switchyard new` wrote some of these 0660, and the verifier refuses a
    document anybody in its group could rewrite -- correctly, because that is
    the whole reason it reads the mode. On the tenant this was written for, that
    left the registered configuration unadoptable by the ordinary command and
    the strict check was the thing standing in the way (SYRD-167).

    So the mode is repaired rather than the check relaxed, and only the two bits
    that break the invariant are taken off: nothing else about the file is
    touched, and a file that is already compliant is not written to at all.

    Everything here happens on ONE descriptor. Opened `O_NOFOLLOW`, checked with
    `fstat` on that descriptor and changed with `fchmod` on the same one, so a
    path that becomes a symlink -- or a different file -- between the check and
    the change is not what gets chmodded. Ownership is checked first for the
    same reason the reader checks it: root repairing a file the tenant does not
    own would be root repairing somebody else's file.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        return [f"{path} could not be opened to check its mode: {exc.strerror or exc}"]
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return [f"{path} is not a regular file"]
        if info.st_uid not in permitted_uids:
            # Left alone deliberately: the reader refuses it a moment later and
            # says whose it is, which is the more useful answer.
            return []
        offending = info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        if not offending:
            return []
        try:
            os.fchmod(descriptor, stat.S_IMODE(info.st_mode) & ~(stat.S_IWGRP | stat.S_IWOTH))
        except OSError as exc:
            return [
                f"{path} is mode {stat.S_IMODE(info.st_mode):04o} and could not be tightened: "
                f"{exc.strerror or exc}"
            ]
        return []
    finally:
        os.close(descriptor)


def registered_tenant_config_path(slug: str, *, registry_dir: Path | None = None) -> Path | None:
    """The configuration path this host's registry names for a project.

    `switchyard new` writes this entry, and every ordinary command follows it to
    decide which account to act as and which tree to work in -- so when it
    exists it is the answer to "where is this project's configuration", and
    guessing instead is how `adopt-workflow syrd` came to look for
    `Projects/Switchyard/...` and `Projects/syrd/...` on a host whose registry
    already named `Projects/switchyard/...`. Neither guess exists, and the
    command refused before touching anything (SYRD-167).

    Read the way every other root-owned record here is read: the entry has to be
    root's, reached through a path of root-owned directories, and not a symlink.
    A registry a tenant could rewrite would be a tenant choosing which document
    root adopts, so this returns a POINTER and nothing more -- what it points at
    is put through exactly the same ownership, mode, symlink and
    agrees-with-the-plan checks as a path typed by hand.
    """
    from scripts import team_launcher as launcher

    entry = (registry_dir or launcher.switchyard_registry_dir()) / f"{slug}.json"
    document, _problem = launcher.read_plan_no_follow(entry, require_root_owned=True)
    if document is None:
        return None
    recorded = str(document.data.get("config_path") or "").strip()
    if not recorded:
        return None
    candidate = Path(recorded)
    # Relative would be resolved against whatever directory the command happens
    # to be run from, which is not a promise anybody made (SYRD-149).
    return candidate if candidate.is_absolute() else None


def recorded_tenant_config_path(slug: str) -> Path | None:
    """The configuration path root verified last time, if it verified one."""
    from scripts import team_launcher as launcher

    document, _problem = launcher.read_plan_no_follow(launcher.tenant_config_record_path(slug), require_root_owned=True)
    if document is None:
        return None
    recorded = str(document.data.get("config_path") or "").strip()
    return Path(recorded) if recorded else None


def record_tenant_config_path(slug: str, config_path: Path) -> Path:
    """Remember a verified configuration path where only root can rewrite it."""
    from scripts import team_launcher as launcher

    path = launcher.tenant_config_record_path(slug)
    payload = (
        json.dumps({"config_path": str(config_path), "project": slug}, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    launcher.ensure_privileged_provision_dir(path.parent)
    staged = path.with_name(f".{path.name}.new")
    descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.write(descriptor, payload)
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, launcher.privileged_artifact_mode(path.name))
    finally:
        os.close(descriptor)
    staged.replace(path)
    return path


def _tenant_config_candidates(
    plan: "ProjectBoardProvision",
    slug: str,
    *,
    explicit: Path | None,
    recorded: Path | None,
    registered: Path | None = None,
) -> list[Path]:
    """Where the generated configuration for this project could be.

    An explicit path, or a path root has already verified, is the whole answer.
    Then the registry, which is a RECORD of where this project's configuration
    is rather than a guess about where it might be -- and is the only one of
    the three that a host with a checkout named unlike its slug can answer
    correctly (SYRD-167).

    Only then the conventional layout, and only tried: whatever is found by any
    of these still has to survive every check below before root registers it.
    Being named by the registry buys a candidate a look, not a pass.
    """
    if explicit is not None:
        return [explicit.expanduser()]
    if recorded is not None:
        return [recorded]
    if registered is not None:
        return [registered]
    home = Path(plan.owner_home)
    names = [name for name in (plan.project_name, slug) if name]
    seen: list[Path] = []
    for name in names:
        candidate = home / "Projects" / name / ".switchyard" / "provision" / f"{slug}.json"
        if candidate not in seen:
            seen.append(candidate)
    return seen


def board_declared_role_names(document: dict | None) -> set[str]:
    """The roles the running board's own declared workflow names.

    Root-owned authority in the sense that matters here: the board's copy can
    only have been installed through the write API, and it is what decides every
    transition and capability right now. It is read over the board's own socket,
    and only once everything else about the configuration has already been shown
    to agree with what root provisioned -- so the socket being asked is the one
    root recorded, not one a configuration nominated for itself (SYRD-167).
    """
    if not isinstance(document, dict):
        return set()
    names: set[str] = set()
    for role in document.get("roles") or ():
        if isinstance(role, dict):
            name = str(role.get("name") or "").strip()
            if name:
                names.add(name)
    return names


def tenant_config_conflicts(
    plan: "ProjectBoardProvision",
    config: ProjectConfig,
    *,
    corroborated_roles: "set[str] | frozenset[str] | tuple[str, ...]" = (),
) -> list[str]:
    """Where a generated configuration disagrees with what root provisioned.

    The registry entry is a pointer, and every ordinary command follows it to
    decide which account to act as, which board to talk to and which tree to
    work in. A configuration that says something else is not a configuration
    root may register, however it came to say it: the difference is the whole
    question, and reconciling it would be root adopting a tenant's answer.
    """
    owner_home = Path(plan.owner_home)
    conflicts: list[str] = []

    def disagree(what: str, found: Any, recorded: Any) -> None:
        conflicts.append(f"{what}: the configuration says {found!r}, root provisioned {recorded!r}")

    if config.project != plan.project:
        disagree("project", config.project, plan.project)
    if (config.run_as_user or "") != plan.owner_user:
        disagree("run_as_user", config.run_as_user, plan.owner_user)
    if config.ticket_prefix != plan.ticket_prefix:
        disagree("ticket_prefix", config.ticket_prefix, plan.ticket_prefix)
    if str(config.board_socket) != plan.socket_path:
        disagree("board_socket", str(config.board_socket), plan.socket_path)
    if f":{plan.port}" not in config.board_url:
        disagree("board_url", config.board_url, f"port {plan.port}")
    # control_repository is not compared here: loading the configuration already
    # refuses one that is not under the managed control directory of the account
    # it names, and the account it names is checked above. Comparing it again
    # would be a second, weaker version of a boundary that is already enforced.
    for what, path in (("repository", config.repository), ("session_dir", config.session_dir)):
        if path is None:
            continue
        resolved = Path(path).expanduser()
        if not (resolved == owner_home or resolved.is_relative_to(owner_home)):
            conflicts.append(
                f"{what}: the configuration points at {resolved}, which is outside "
                f"{plan.owner_user}'s home {owner_home}"
            )
    # A project provisioned before a role existed has a baseline that predates
    # it, and syrd is exactly that: its configuration and the board it is
    # running both name `inspector`, and root's old record does not. Refusing
    # that is refusing the tenant for having been provisioned earlier, and
    # deleting the check would be trusting the tenant's own JSON about which
    # roles exist. So an added role is ESTABLISHED instead: it counts when the
    # board's own declared workflow names it too, which is a second source the
    # tenant cannot write (SYRD-167).
    permitted = set(plan.caller_roles) | set(corroborated_roles)
    unknown = sorted({role.role for role in config.roles} - permitted)
    if unknown:
        conflicts.append(
            "roles: the configuration declares "
            + ", ".join(unknown)
            + ", which root did not provision for this project and the board's declared "
            "workflow does not name either"
        )
    return conflicts


def verified_tenant_config(
    plan: "ProjectBoardProvision",
    slug: str,
    *,
    explicit: Path | None = None,
    owner_uid: int | None = None,
    registry_dir: Path | None = None,
    board_reader: "Callable[[ProjectConfig], tuple[dict | None, str]] | None" = None,
    corroborate_roles: bool = True,
) -> tuple[Path | None, ProjectConfig | None, list[str]]:
    """The generated configuration root is willing to register, or why not.

    Owned by the project owner or by root: `switchyard new` writes this
    directory as whichever of the two ran it, and a file root wrote is not a
    file the tenant could have. Anything else -- another account, or a mode
    that lets a group or the world rewrite it -- is not a document root will
    point the registry at.

    Legacy-role corroboration is the DEFAULT rather than something a caller
    remembers to ask for. SYRD-167 added it and wired it into adoption alone, so
    `resume-provision` called this same verifier without a reader and refused
    `inspector` on a tenant where the configuration, the board's declared
    workflow and the live runtime assignments all name it (journal 0068). A
    safety property that every privileged path needs and one path supplies is a
    property that path has, not one the verifier has (SYRD-168).

    It stays fail-closed: the reader is consulted only when every other
    comparison already agrees, an unreachable board corroborates nothing, and a
    role the running board does not name is still refused.
    """
    from scripts import team_launcher as launcher

    recorded = launcher.recorded_tenant_config_path(slug)
    registered = launcher.registered_tenant_config_path(slug, registry_dir=registry_dir)
    candidates = launcher._tenant_config_candidates(
        plan, slug, explicit=explicit, recorded=recorded, registered=registered
    )
    permitted = sorted({launcher.expected_privileged_uid(), *( (owner_uid,) if owner_uid is not None else () )})
    problems: list[str] = []
    for candidate in candidates:
        # Before the reader, not instead of it: the mode is repaired where root
        # may repair it, and then the same strict check runs unchanged
        # (SYRD-167).
        problems.extend(launcher.normalize_tenant_config_mode(candidate, permitted_uids=permitted))
        document, problem = launcher.read_plan_no_follow(
            candidate, require_root_owned=False, require_owner_uids=permitted
        )
        if document is None:
            problems.append(problem)
            continue
        try:
            config = launcher.load_project_config(slug, candidate)
        except (SystemExit, OSError, json.JSONDecodeError) as exc:
            problems.append(f"{candidate} is not a usable launcher configuration: {exc}")
            continue
        conflicts = launcher.tenant_config_conflicts(plan, config)
        reader = board_reader if board_reader is not None else (
            launcher.read_board_declared_workflow if corroborate_roles else None
        )
        if conflicts and reader is not None and all(
            line.startswith("roles: ") for line in conflicts
        ):
            # Only when everything ELSE already agrees. The board is asked over
            # the socket the configuration names, and that name having been
            # shown to match the one root provisioned is precisely what the
            # absence of any other conflict means here -- so a configuration
            # cannot nominate the authority that corroborates it (SYRD-167).
            document, _board_problem = reader(config)
            conflicts = launcher.tenant_config_conflicts(
                plan, config, corroborated_roles=launcher.board_declared_role_names(document)
            )
        if conflicts:
            return None, None, [
                f"{candidate} is not the configuration root provisioned for {slug}:",
                *conflicts,
            ]
        return candidate, config, []
    return None, None, problems
