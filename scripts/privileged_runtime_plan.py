"""Root's runtime baseline for a tenant, and what the tenant's projection may add to it.

The privileged half of refreshing a tenant's generated runtime artifacts, as
the facts root decides it from:

- **Owner evidence.** `_provision_owner` takes the tenant from the owner of its
  provision directory. For a legacy directory root owns,
  `legacy_owner_from_host_records` takes it from root-controlled host records
  instead -- the board unit (`SYSTEMD_SYSTEM_UNIT_DIR`, read through
  `_root_controlled_record` and `_unit_environment`), the registry entry and
  the board root -- and `repair_legacy_provision_ownership` gives that
  directory and its named files back without following a link (SYRD-39,
  SYRD-227).
- **The baseline.** `_privileged_baseline_plan` uses root's own stored plan,
  or `reconstruct_privileged_baseline` regenerates one from root's facts. The
  tenant's document contributes only validated values that decide nothing root
  runs, judged as first read (SYRD-39, SYRD-52, SYRD-226).
- **The projection.** `authoritative_refresh_plan` accepts only canonical
  accounts for roles the baseline does not know. `plan_for_current_identities`
  empties the per-role table for a tenant on its project account
  (`_tenant_runs_on_project_account`).
- **Privacy.** `privileged_provision_privacy_problems` reports what root's
  provisioning directory still gives away, and `close_privileged_artifacts`
  brings the named artifacts down to their least mode (SYRD-176).

Moved out of `scripts/team_launcher.py` unchanged (SYRD-353). The launcher
imports this module at its top and re-exports every name, so
`refresh_generated_project_runtime_artifacts` and the suites reach the same
objects. Every launcher facility these use -- the plan builder and renderer
paths, root's provision and registry roots, the no-follow walk, the shared
validators -- and every name defined here that another definition here calls
is read from `team_launcher` when it runs, as it was: suites patch
`_provision_owner` and `SYSTEMD_SYSTEM_UNIT_DIR` there. The standard-library
modules are this module's own imports, the same module objects. This module
never imports `team_launcher` at its top.
"""

from __future__ import annotations

import errno
import json
import os
import pwd
import shlex
import stat
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectBoardProvision, ProjectConfig


def _provision_owner(provision_dir: Path) -> tuple[str, str]:
    """The tenant's identity, taken from the filesystem rather than from its own document.

    Only root can change a file's owner, so the account that owns the provision
    directory is a fact about the host, not a claim the tenant can make. Every
    other tenant-derived value below is anchored on this (SYRD-39).
    """
    try:
        info = os.stat(provision_dir, follow_symlinks=False)
    except OSError as exc:
        return "", f"cannot inspect {provision_dir} ({exc.strerror})"
    if info.st_uid == 0:
        return "", f"{provision_dir} is owned by root and so does not identify a tenant"
    try:
        return pwd.getpwuid(info.st_uid).pw_name, ""
    except KeyError:
        return "", f"{provision_dir} is owned by uid {info.st_uid}, which is not a local account"


#: Where root's systemd units are read from when establishing a legacy tenant's
#: owner. A constant rather than an environment variable: this feeds a
#: decision root makes, and nothing in the caller's environment may steer it.
SYSTEMD_SYSTEM_UNIT_DIR = Path("/etc/systemd/system")


def _root_controlled_record(path: Path) -> tuple[bytes | None, str]:
    """A file only root could have written: root-owned, not writable by others.

    Read without following a link at the file itself, so a record is what it
    says it is.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        return None, f"{path} cannot be read ({exc.strerror})"
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return None, f"{path} is not a regular file"
        if info.st_uid != 0:
            return None, f"{path} is not owned by root"
        if info.st_mode & 0o022:
            return None, f"{path} is writable by accounts other than root"
        chunks = []
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks), ""
    finally:
        os.close(descriptor)


def _unit_environment(unit_text: str) -> dict[str, list[str]]:
    """Every `Environment=` assignment in a unit, by name, in order."""
    found: dict[str, list[str]] = {}
    for line in unit_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("Environment="):
            continue
        for assignment in shlex.split(stripped[len("Environment="):]):
            name, sep, value = assignment.partition("=")
            if sep:
                found.setdefault(name, []).append(value)
    return found


def legacy_owner_from_host_records(
    project: str,
    provision_dir: Path,
    *,
    unit_dir: Path | None = None,
    registry_dir: Path | None = None,
) -> tuple[str, list[str], str]:
    """The owner of a registered tenant whose provision directory root owns.

    An older release created mefp's provision directory as root, and ownership
    of that directory was the only owner fact reconstruction trusted -- so a
    registered, running tenant could not be upgraded in place (SYRD-227).
    Nothing the tenant can edit is consulted here, and no account is inferred
    from a pathname. The owner comes from root-controlled host records:

    * the tenant's board unit, root-owned: its `TICKET_BOARD_TENANT_USER` when
      it names one, and its `HOME`, resolved through the passwd database to the
      one account whose home it is;
    * the registry entry, root-owned, which must name THIS provision directory,
      inside that account's home;
    * the board root the unit runs from, whose owner -- set only by root --
      must be that account when the directory exists.

    Every present source must agree and at least one must name the account.
    Returns (owner, the evidence that established it, why not).
    """
    from scripts import team_launcher as launcher

    units = unit_dir or launcher.SYSTEMD_SYSTEM_UNIT_DIR
    registry = registry_dir or launcher.switchyard_registry_dir()
    evidence: list[str] = []
    unit_path = units / f"{project}-ticket-board.service"
    unit_bytes, problem = launcher._root_controlled_record(unit_path)
    if unit_bytes is None:
        return "", evidence, f"its board unit is not a root-controlled record: {problem}"
    environment = launcher._unit_environment(unit_bytes.decode("utf-8", "replace"))
    candidates: dict[str, str] = {}
    tenant_users = {value.strip() for value in environment.get("TICKET_BOARD_TENANT_USER", []) if value.strip()}
    if len(tenant_users) > 1:
        return "", evidence, f"{unit_path} names more than one tenant user: {sorted(tenant_users)}"
    if tenant_users:
        candidates["TICKET_BOARD_TENANT_USER"] = next(iter(tenant_users))
    homes = {value.strip() for value in environment.get("HOME", []) if value.strip()}
    if len(homes) > 1:
        return "", evidence, f"{unit_path} sets more than one HOME: {sorted(homes)}"
    if homes:
        home = next(iter(homes))
        holders = sorted(entry.pw_name for entry in pwd.getpwall() if entry.pw_dir.rstrip("/") == home.rstrip("/"))
        if len(holders) != 1:
            return "", evidence, (
                f"{unit_path} sets HOME={home}, which is the home of "
                + (f"{len(holders)} accounts ({', '.join(holders)})" if holders else "no account")
            )
        candidates["HOME"] = holders[0]
    if not candidates:
        return "", evidence, f"{unit_path} names neither a tenant user nor a HOME"
    if len(set(candidates.values())) != 1:
        return "", evidence, (
            f"{unit_path} is contradictory: "
            + ", ".join(f"{source} -> {owner}" for source, owner in sorted(candidates.items()))
        )
    owner = next(iter(candidates.values()))
    evidence.extend(f"{unit_path} {source} -> {owner}" for source, owner in sorted(candidates.items()))
    try:
        account = pwd.getpwnam(owner)
    except KeyError:
        return "", evidence, f"{owner}, named by {unit_path}, is not a local account"
    if account.pw_uid == 0:
        return "", evidence, f"{unit_path} names root as the tenant owner"
    owner_home = Path(account.pw_dir).resolve(strict=False)

    registry_path = registry / f"{project}.json"
    registry_bytes, problem = launcher._root_controlled_record(registry_path)
    if registry_bytes is None:
        return "", evidence, f"it is not a registered tenant: {problem}"
    try:
        record = json.loads(registry_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return "", evidence, f"{registry_path} is not a registry record: {exc}"
    registered = Path(str(record.get("config_path") or "")).expanduser()
    if str(record.get("slug") or "") != project or not registered.is_absolute():
        return "", evidence, f"{registry_path} does not register {project} at an absolute path"
    if registered.parent.resolve(strict=False) != provision_dir.resolve(strict=False):
        return "", evidence, (
            f"{registry_path} registers {registered.parent}, not {provision_dir}"
        )
    if owner_home not in registered.resolve(strict=False).parents:
        return "", evidence, (
            f"{registry_path} registers {registered}, which is not inside {owner}'s home {owner_home}"
        )
    evidence.append(f"{registry_path} registers {registered}, inside {owner}'s home")

    board_root = Path(account.pw_dir) / f"{project}-ticketboard-live"
    try:
        info = os.stat(board_root, follow_symlinks=False)
    except OSError:
        info = None
    if info is not None:
        if info.st_uid != account.pw_uid:
            return "", evidence, (
                f"{board_root} is owned by uid {info.st_uid}, not by {owner}"
            )
        evidence.append(f"{board_root} is owned by {owner}")
    return owner, evidence, ""


def repair_legacy_provision_ownership(
    provision_dir: Path,
    owner: str,
    names: Sequence[str],
    *,
    dry_run: bool,
) -> tuple[list[str], str]:
    """Give a root-owned legacy provision directory back to its owner.

    Bounded to the directory itself and the generated files named, never
    recursive: anything else in it is left exactly as found and listed. Every
    path component is opened without following a link, and a file is changed
    only if it is a regular file with one link, so a symlink or a hard link the
    tenant planted cannot redirect root's chown (SYRD-227).

    Returns (what was -- or, in a dry run, would be -- changed, why not).
    """
    from scripts import team_launcher as launcher

    account = pwd.getpwnam(owner)
    relative = Path(str(provision_dir).lstrip("/")) / "_"
    dir_fd, problem = launcher._walk_no_follow(Path(provision_dir.anchor or "/"), relative)
    if dir_fd < 0:
        return [], f"refusing to repair {provision_dir}: {problem}"
    changes: list[str] = []
    try:
        info = os.fstat(dir_fd)
        if info.st_uid != 0:
            return [], f"{provision_dir} is not root-owned; nothing to repair"
        changes.append(f"{provision_dir}/ -> {owner}")
        targets: list[tuple[str, int]] = []
        for name in sorted(set(names)):
            if "/" in name or name in ("", ".", ".."):
                return [], f"refusing to repair {provision_dir}: {name!r} is not a file name"
            try:
                descriptor = os.open(
                    name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd
                )
            except FileNotFoundError:
                continue
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    return [], f"refusing to repair {provision_dir / name}: it is a symbolic link"
                return [], f"refusing to repair {provision_dir / name}: {exc.strerror}"
            entry = os.fstat(descriptor)
            if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
                os.close(descriptor)
                return [], (
                    f"refusing to repair {provision_dir / name}: not a regular file with one link"
                )
            targets.append((name, descriptor))
            changes.append(f"{provision_dir / name} -> {owner}")
        try:
            if not dry_run:
                os.fchown(dir_fd, account.pw_uid, account.pw_gid)
                for _name, descriptor in targets:
                    os.fchown(descriptor, account.pw_uid, account.pw_gid)
        finally:
            for _name, descriptor in targets:
                os.close(descriptor)
    except OSError as exc:
        return [], f"could not repair {provision_dir}: {exc}"
    finally:
        os.close(dir_fd)
    return changes, ""


def reconstruct_privileged_baseline(
    project: str,
    provision_dir: Path,
    tenant_data: dict[str, Any],
    *,
    source_repo: Path | None = None,
    operator_commit_git_dir: bool = False,
    established_owner: str = "",
) -> tuple[ProjectBoardProvision | None, str]:
    """Build root's baseline from facts root holds, not from the tenant's document.

    A tenant that predates the root-owned baseline still has to get one, but
    adopting its plan.json would mean root installing a file the tenant wrote.
    Checking that document field by field is a list of the attacks one happened
    to think of -- role_accounts renders a sudoers grant, owner_home anchors
    every path check that would be made against it. So nothing is adopted.

    The identity comes from the owner of the provision directory, the home from
    the passwd database, and everything root installs -- the account the service
    runs as, every generated name, every path, and the whole role-to-account
    table -- is regenerated by the same build_plan provisioning uses. The
    document contributes only typed, individually validated values that decide
    nothing root runs: the display name, the port, the ticket prefix, whether a
    designer or auditor exists, and role NAMES (SYRD-39).
    """
    from scripts import team_launcher as launcher

    # A legacy tenant whose provision directory root owns has its owner
    # established from root-controlled host records instead (SYRD-227).
    owner_user, problem = (established_owner, "") if established_owner else launcher._provision_owner(provision_dir)
    if not owner_user:
        return None, problem
    try:
        owner_home = Path(pwd.getpwnam(owner_user).pw_dir)
    except KeyError:
        return None, f"{owner_user} has no passwd entry, so its home cannot be established"
    if not owner_home.is_absolute():
        return None, f"the passwd entry for {owner_user} has no absolute home"
    objections: list[str] = []
    resolved_source_repo = (source_repo or launcher._repo_root()).expanduser().resolve(strict=False)
    reference = launcher.build_plan(
        project=project,
        owner_user=owner_user,
        owner_home=owner_home,
        control_user=launcher.resolve_control_user(project, owner_user=owner_user),
        source_repo=resolved_source_repo,
    )
    port = tenant_data.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
        objections.append(f"port {port!r} is not a usable unprivileged port")
        port = None
    traversal = tenant_data.get("board_service_traversal", True)
    if not isinstance(traversal, bool):
        objections.append(f"board_service_traversal {traversal!r} is not a boolean")
        traversal = True
    implementer_roles = launcher._validated_role_names(tenant_data.get("implementer_roles"), "implementer_roles", objections)
    audit_roles = launcher._validated_role_names(tenant_data.get("audit_roles"), "audit_roles", objections)
    draft_roles = launcher._validated_role_names(tenant_data.get("draft_roles"), "draft_roles", objections)
    vcs_close_role = str(tenant_data.get("vcs_close_role") or "").strip().lower()
    if vcs_close_role and not launcher.ROLE_RE.fullmatch(vcs_close_role):
        objections.append(f"vcs_close_role {vcs_close_role!r} is not a role name")
        vcs_close_role = ""
    project_name = str(tenant_data.get("project_name") or "").strip() or None
    ticket_prefix = str(tenant_data.get("ticket_prefix") or "").strip() or None
    if objections:
        return None, (
            f"switchyard: cannot establish a root-owned baseline for {project} from "
            f"{provision_dir / 'plan.json'}: " + "; ".join(objections)
            + ". Re-provision the project so root generates its own."
        )
    try:
        plan = launcher.build_plan(
            project=project,
            project_name=project_name,
            owner_user=owner_user,
            owner_home=owner_home,
            port=port,
            source_repo=resolved_source_repo,
            ticket_prefix=ticket_prefix,
            implementer_roles=implementer_roles or None,
            include_designer=bool(draft_roles),
            include_audit=bool(audit_roles),
            audit_roles=audit_roles or None,
            board_service_traversal=traversal,
            vcs_close_role=vcs_close_role or None,
        )
    except SystemExit as exc:
        return None, (
            f"switchyard: cannot establish a root-owned baseline for {project}: {exc}. "
            "Re-provision the project so root generates its own."
        )
    diverged = launcher._regenerated_field_divergence(
        plan, tenant_data, skip=("commit_git_dir",) if operator_commit_git_dir else ()
    )
    if diverged:
        return None, (
            f"switchyard: cannot establish a root-owned baseline for {project}: it was provisioned "
            "with values root regenerates rather than trusts, and installing the regenerated ones "
            "would change what runs:\n  "
            + "\n  ".join(diverged)
            + "\nRe-provision the project so root generates its own baseline."
        )
    worktree_base = launcher._new_project_worktree_base(project, owner_user)
    plan = replace(
        plan,
        role_worktrees=tuple((role, str(worktree_base / role)) for role, _account in plan.role_accounts),
    )
    plan, workflow_problem = launcher.plan_workflow_from_root(
        plan, declares_workflow=tenant_data.get("workflow") is not None
    )
    if workflow_problem:
        return None, f"switchyard: {workflow_problem}"
    return plan, ""


def _privileged_baseline_plan(
    config: ProjectConfig,
    provision_dir: Path,
    tenant_data: dict[str, Any],
    *,
    source_repo: Path | None = None,
    operator_commit_git_dir: bool = False,
    supplied: Mapping[str, str] | None = None,
    established_owner: str = "",
) -> tuple[ProjectBoardProvision | None, str]:
    """Root's baseline: its own stored copy, or one it reconstructs for a legacy tenant.

    The tenant document is passed in as it was read at the start of the
    refresh, not re-read here: by this point the tenant's own copies have been
    republished, and judging the rewritten file would let a value the refresh
    corrected slip past the divergence refusal (SYRD-52).
    """
    from scripts import team_launcher as launcher

    baseline_path = launcher.privileged_baseline_plan_path(config.project)
    if baseline_path.is_file():
        try:
            # Root's own copy can be as old as the tenant's. What the operator
            # supplied is the operator's, not the tenant's, so it may fill a
            # field root's copy predates (SYRD-226).
            return launcher._project_board_provision_from_json(baseline_path, supplied=supplied), ""
        except SystemExit as exc:
            return None, f"switchyard: {config.project} root-owned runtime baseline {baseline_path} is unusable: {exc}"
    return launcher.reconstruct_privileged_baseline(
        config.project,
        provision_dir,
        tenant_data,
        source_repo=source_repo,
        operator_commit_git_dir=operator_commit_git_dir,
        established_owner=established_owner,
    )


def plan_for_current_identities(
    plan: ProjectBoardProvision, config: ProjectConfig
) -> ProjectBoardProvision:
    """The plan as the identities actually running require it to be rendered.

    Module level rather than a closure so the contract regression can drive the
    decision itself instead of the rendering that follows it.
    """
    from scripts import team_launcher as launcher

    if not launcher._tenant_runs_on_project_account(config):
        return plan
    if not plan.role_accounts:
        return plan
    return replace(plan, role_accounts=())


def _tenant_runs_on_project_account(config: ProjectConfig) -> bool:
    """Whether this tenant has crossed to the one-project-account runtime.

    Both halves, because either alone is ambiguous: the flip is recorded on the
    configuration, and every role must actually name the project account. A
    configuration that still gives a role its own account has not crossed,
    whatever the flag says, and is left on its per-role table.
    """
    if not config.role_state_isolation:
        return False
    if not config.roles:
        return False
    owner = (config.run_as_user or "").strip()
    if not owner:
        return False
    for role in config.roles:
        account = (getattr(role, "run_as_user", "") or "").strip()
        if account and account != owner:
            return False
    return True


def authoritative_refresh_plan(
    baseline: ProjectBoardProvision, tenant_data: dict[str, Any]
) -> tuple[ProjectBoardProvision, list[str], list[str]]:
    """Apply the one thing an unprivileged projection is allowed to say.

    A workflow document can introduce a role, and that role needs its account in
    the table the board resolves uids through. Nothing else crosses: not the
    account for a role root already knows, not a non-canonical account, and not
    any other field of the plan (SYRD-39).
    """
    from scripts import team_launcher as launcher

    known = {role: account for role, account in baseline.role_accounts}
    accepted: list[tuple[str, str]] = []
    refused: list[str] = []
    entries = tenant_data.get("role_accounts")
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            refused.append(repr(entry))
            continue
        role, account = str(entry[0]).strip(), str(entry[1]).strip()
        if role in known:
            if account != known[role]:
                refused.append(f"{role}={account}")
            continue
        canonical = launcher.role_account_name(baseline.project, role)
        if not launcher.ROLE_RE.fullmatch(role) or role in launcher.NON_PROCESS_ROLES or account != canonical:
            refused.append(f"{role}={account}")
            continue
        accepted.append((role, canonical))
    if not accepted:
        return baseline, [], refused
    worktree_base = launcher._new_project_worktree_base(baseline.project, baseline.owner_user)
    worktrees = list(baseline.role_worktrees)
    if worktrees:
        worktrees.extend((role, str(worktree_base / role)) for role, _account in accepted)
    return (
        replace(
            baseline,
            role_accounts=(*baseline.role_accounts, *accepted),
            role_worktrees=tuple(worktrees),
        ),
        [role for role, _account in accepted],
        refused,
    )


def close_privileged_artifacts(target: Path, names: Iterable[str]) -> list[str]:
    """Bring the artifacts root regenerates down to the least mode they need.

    Only the names the current render owns. A tenant's directory also holds an
    operator's own scripts from years of rollouts, and closing the directory is
    not a licence to rewrite what is in it (SYRD-176).
    """
    from scripts import team_launcher as launcher

    closed: list[str] = []
    for name in sorted(names):
        path = target / name
        try:
            info = path.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            continue
        wanted = launcher.privileged_artifact_mode(name)
        if stat.S_IMODE(info.st_mode) != wanted:
            path.chmod(wanted)
            closed.append(name)
    return [f"closed {len(closed)} artifact(s) in {target}: {', '.join(closed)}"] if closed else []


def privileged_provision_privacy_problems(
    project: str, *, root: Path | None = None
) -> list[str]:
    """What a tenant's root-owned provisioning directory still gives away.

    Asked of the filesystem, so it answers for a directory an earlier version
    opened rather than for what the current code would have created.
    """
    from scripts import team_launcher as launcher

    target = launcher.privileged_provision_dir(project, root=root or launcher.switchyard_privileged_provision_root())
    try:
        info = target.lstat()
    except OSError:
        return []
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return [f"{target} is not a directory root owns"]
    problems: list[str] = []
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077:
        problems.append(
            f"{target} is mode {mode:04o}, so {project}'s plan, operator packet, SQL and "
            "publication record are readable beyond root"
        )
    if info.st_uid != launcher.expected_privileged_uid():
        problems.append(f"{target} is owned by uid {info.st_uid} rather than by root")
    return problems
