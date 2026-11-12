"""Who root acts for when it acts for a tenant: the owner root's own records name, never the tenant's file (SYRD-663).

- `root_recorded_owner` is the owner root's records name for a project: its
  provisioning record's `owner_user`, or, for a tenant root registered but holds
  no plan for, the account owning the project directory its registry entry
  names.
- `root_owner_refusal` is why root may not act for a project as the account a
  configuration's `run_as_user` names -- blank, `root`, or anybody but that
  owner -- or nothing.
- `root_checked_plan_identity` holds the tenant's own `plan.json` to the same
  records: its `owner_user`, `owner_home` and `board_root` are root's, or the
  plan is refused (SYRD-664).
- `root_role_account_refusal` is why root may not act as or for an account a
  role's own `run_as_user` names: anything but the owner must be a role account
  root made for this project (SYRD-757).

The command entry points ask `root_recorded_owner` strictly, before the
configuration is read, and `load_project_config` asks `root_owner_refusal` on
every load root makes -- including the loads of a root process a catalogued
action starts, which has no entry point of its own. The launcher imports this
module and re-exports both names; every launcher facility these use is read
from `team_launcher` when they run. This module never imports `team_launcher`
at its top.
"""

from __future__ import annotations

import grp
import json
import os
import pwd
import stat
from pathlib import Path
from typing import Sequence


def root_recorded_owner(project: str, config_path: Path | None = None, *, strict: bool = True) -> tuple[str | None, str]:
    """The owner root's own records name for `project`, and why not; None when they say nothing about it.

    Root's provisioning record names it (`owner_user`), read root-owned and
    without following a link, as resume reads it. A tenant root registered but
    holds no plan for -- a legacy tenant, whose upgrade leaves its artifacts as
    they are -- is named by root's registry entry, which records where its
    configuration is: the owner is the account the kernel says owns that
    project directory, the rule SYRD-608 trusts a registered project root by.
    Neither record is the tenant's to write, and no tenant can make a directory
    another account owns.

    `strict` is a command's entry point asking: a registry entry it cannot read
    as root's, or one naming another configuration, is a refusal. A load asks
    it unstrictly: such an entry says nothing about the file being loaded, so
    the answer is None -- a fixture, or a configuration some other path names,
    is not refused for a record that is not about it. A registry entry naming
    exactly this file whose project directory no account owns is a problem
    either way: a record of this tenant that names nobody, so no account may be
    acted for (Audit of 9bcafe2f).
    """
    from scripts import team_launcher as launcher

    baseline = launcher.privileged_baseline_plan_path(project)
    if os.path.lexists(baseline):
        document, problem = launcher.read_plan_no_follow(baseline, require_root_owned=True)
        if document is None:
            return "", f"root's provisioning record for {project} cannot say whose project it is: {problem}"
        recorded = str(document.data.get("owner_user") or "").strip()
        return recorded, "" if recorded else f"root's provisioning record {baseline} names no owner for {project}"
    from scripts import owner_writes
    from scripts.ticket_board.publication_boundary import root_controlled_problems

    # The host's registry, as SYRD-608 reads it for an owner's project roots: the fixed path, every component root's
    # alone, opened without following a link -- never a registry the caller's environment names.
    registry, base = owner_writes.TRUSTED_REGISTRY
    entry = registry / f"{project}.json"
    if not os.path.lexists(entry):
        return None, ""
    unproven = root_controlled_problems(str(entry), expect_uid=0, base=base)
    raw: object = None
    if not unproven:
        try:
            descriptor = os.open(entry, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                if stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raw = json.loads(os.read(descriptor, 1 << 20).decode("utf-8"))
            finally:
                os.close(descriptor)
        except (OSError, ValueError) as exc:
            unproven = [str(exc)]
    if not isinstance(raw, dict):
        why = "; ".join(unproven) or "it is not a registry document"
        return ("", f"root's registry entry for {project} cannot say whose project it is: {why}") if strict else (None, "")
    registered = Path(str(raw.get("config_path") or "").strip())
    if config_path is not None and registered != Path(config_path):
        return ("", f"root's registry entry for {project} names {registered}, not {config_path}") if strict else (None, "")
    # The owner is the account that owns the project directory the entry names, as SYRD-608 trusts it for that owner's
    # writes: a real directory, not a link, owned by an account that is not root.
    project_dir = registered.parent.parent.parent
    if registered.is_absolute() and registered.parent.name == "provision" and registered.parent.parent.name == ".switchyard":
        try:
            info = os.lstat(project_dir)
            if stat.S_ISDIR(info.st_mode) and info.st_uid != 0:
                return pwd.getpwuid(info.st_uid).pw_name, ""
        except (OSError, KeyError):
            pass
    return "", f"no account owns {project_dir}, the project directory of the configuration root registered for {project}"


def root_owner_refusal(project: str, run_as_user: str, config_path: Path | None = None) -> str:
    """Why root may not act for `project` as the account its configuration names, or "" (SYRD-663).

    The configuration is the tenant's to write, and every root branch that
    writes as or for the owner is chosen by its `run_as_user`. A blank one, or
    `root`, made that owner root, so every one of them took its by-path branch
    as root; another tenant's account would have pointed them at that tenant's
    roots. So the account it names must be the owner root's own records name
    for this configuration (`root_recorded_owner`, asked about this file), on
    every load -- a catalogued action's fresh root process included. Only who
    the owner is is asked here; whether the host agrees about that owner's home
    is `trusted_owner_identity`'s question, for the commands that act on the
    home.

    Nothing is asked of anybody but root, whose writes these are. A
    configuration root's records say nothing about is passed, because `switchyard
    new` loads its own before root's records are installed; the command entry
    points refuse that case themselves, before the configuration is read
    (`_load_switchyard_project_config_for_command`).
    """
    if os.geteuid() != 0:
        return ""
    recorded, problem = root_recorded_owner(project, config_path, strict=False)
    if recorded is None:
        return ""
    named = (run_as_user or "").strip()
    if not named:
        return (f"{project}'s configuration names no run_as_user, so it does not say whose project this is, and "
                "root does not act on it")
    if named == "root":
        return f"{project}'s configuration names 'root' as its owner; root does not act as its own tenant"
    if problem:
        return problem
    if named != recorded:
        return (f"{project}'s configuration names {named!r} as its owner, and root provisioned it for "
                f"{recorded!r}; root acts only for the owner it provisioned")
    return ""


def _account(name: str) -> pwd.struct_passwd | None:
    try:
        return pwd.getpwnam(name)
    except KeyError:
        return None


def _group(name: str) -> grp.struct_group | None:
    try:
        return grp.getgrnam(name)
    except KeyError:
        return None


def _role_account_problem(project: str, role: str, account: str) -> str:
    """Why `account` is not a role account root made for `role` of `project`, or "" (SYRD-757)."""
    from scripts.ticket_board.project_provision import role_account_name, roles_group_name

    if account == "root":
        return "root does not act as its own tenant"
    canonical = role_account_name(project, role)
    if account != canonical:
        return f"the only account root makes for {role} of {project} is {canonical!r}"
    entry = _account(account)
    if entry is None:
        # A legacy configuration names the canonical account even where root never made it; nothing can be run as an
        # account that does not exist, and the repatriation reads nothing for one (`repatriate_role_runtime_state`),
        # so the upgrade can still drop the binding.
        return ""
    if entry.pw_uid == 0:
        return "it is uid 0"
    # The group root's provisioning adds this project's role accounts to (and its owner and board service). The
    # canonical name alone could be another tenant's account -- `<other>-<role>` -- so the account must also be one
    # root put in this project's group: /etc/group is root's record of that, as no tenant can write it.
    group_name = roles_group_name(project)
    group = _group(group_name)
    if group is None:
        return f"root has made no {group_name} group for {project}, so it made no role accounts for it"
    if account not in group.gr_mem:
        return f"it is not in {group_name}, the group root adds {project}'s role accounts to"
    # Root's packet puts the board's service account in that group too, and its name is the operator's choice: one
    # named `<project>-<role>` would pass for a role account and run the tenant's CLI with the board's database
    # authority (Audit of ee692ce8). So it is refused by what root's own records say runs the board.
    services, unknown = _board_service_accounts(project)
    if account in services:
        return f"it is the account {project}'s board service runs as, never a role's"
    if unknown:
        return f"root's records cannot say which account runs {project}'s board ({unknown}), so no role account is"
    return ""


def _board_service_accounts(project: str) -> tuple[set[str], str]:
    """The accounts root's records say run `project`'s board, and why they cannot say, or ""; SYRD-757.

    The default service account; the one root's provisioning record names (`service_user`); and the `User=` of
    root's units for the board and its canary. A record that exists but cannot be read as root's is a reason, and so
    is a project with neither a provisioning record nor a board unit: then root does not know which account it is.
    """
    from scripts import team_launcher as launcher
    from scripts.ticket_board import board_identity
    from scripts.ticket_board.project_provision import DEFAULT_SERVICE_USER

    found, problems, recorded = {DEFAULT_SERVICE_USER}, [], False
    baseline = launcher.privileged_baseline_plan_path(project)
    if os.path.lexists(baseline):
        document, problem = launcher.read_plan_no_follow(baseline, require_root_owned=True)
        if document is None:
            problems.append(f"its provisioning record cannot be read: {problem}")
        else:
            found.add(str(document.data.get("service_user") or "").strip())
            recorded = True
    try:
        units, unreadable = board_identity.unit_service_accounts(project)
    except board_identity.BoardIdentityError as exc:
        units, unreadable = set(), [str(exc)]
    found |= units
    problems.extend(unreadable)
    if not recorded and not units and not problems:
        problems.append("it has neither a provisioning record nor a board unit")
    return found - {""}, "; ".join(problems)


def root_role_account_refusal(project: str, run_as_user: str, roles: Sequence[object]) -> str:
    """Why root may not act as or for an account a role of `project` names, or "" (SYRD-757).

    SYRD-663 holds the project's owner to root's records; a role's own `run_as_user` is the tenant's to write too.
    Root reads it to decide whose home a legacy role's state is repatriated from, which account runs git in a role's
    worktree, which account a pane command re-executes as, and which account a role's pane starts as. Any account
    there but the owner itself must be a role account root made for this project: the canonical
    `<project>-<role>`, not uid 0, in this project's roles group, and not the account root's records say its board
    service runs as. Anything else -- another tenant, `root` or another name for uid 0, another role's account, a
    `<project>-<role>` root did not put in the group, or the board's own service account -- is refused, before
    anything acts on it; so is every role account of a project whose board account root's records cannot name. The canonical name with no account behind it is not refused: a legacy
    configuration names it where root never made it, nothing can run as it, and the repatriation reads nothing for
    it. A blank one is the owner's, which `root_owner_refusal` holds to root's records. Nothing is asked of anybody
    but root.
    """
    if os.geteuid() != 0:
        return ""
    owner = (run_as_user or "").strip()
    problems = []
    for role in roles:
        account = str(getattr(role, "run_as_user", "") or "").strip()
        if not account or account == owner:
            continue
        name = str(getattr(role, "role", "") or "")
        why = _role_account_problem(project, name, account)
        if why:
            problems.append(f"role {name!r} names {account!r} as its account, and {why}")
    if not problems:
        return ""
    return (f"{project}'s configuration: " + "; ".join(problems)
            + ". Root acts only as the project's owner or a role account it made for this project")


#: The fields of the tenant's own plan that decide which account and which trees root acts in (SYRD-664).
PLAN_IDENTITY_FIELDS = ("owner_user", "owner_home", "board_root")


def root_recorded_identity(project: str, config_path: Path | None = None) -> tuple[dict[str, str | None] | None, str]:
    """The owner, home and board root root's own records name for `project`; None when they say nothing about it.

    Root's provisioning record names all three. A tenant root registered but
    holds no plan for has its owner from root's registry entry
    (`root_recorded_owner`) and that account's home from passwd; its board root
    is not recorded anywhere root owns, so it is None, and a plan's board root
    is then held to the owner's own roots instead.
    """
    from scripts import team_launcher as launcher

    baseline = launcher.privileged_baseline_plan_path(project)
    if os.path.lexists(baseline):
        document, problem = launcher.read_plan_no_follow(baseline, require_root_owned=True)
        if document is None:
            return None, f"root's provisioning record for {project} cannot say whose project it is: {problem}"
        recorded = {field: str(document.data.get(field) or "").strip() or None for field in PLAN_IDENTITY_FIELDS}
        if not recorded["owner_user"] or not recorded["owner_home"]:
            return None, f"root's provisioning record {baseline} names no owner or home for {project}"
        return recorded, ""
    owner, problem = root_recorded_owner(project, config_path, strict=False)
    if owner is None:
        return None, ""
    if problem:
        return None, problem
    # The codebase's own home lookup, as `trusted_owner_identity` reads it.
    home = launcher.home_dir_for_user(owner)
    if home is None:
        return None, f"{owner}, the owner root's records name for {project}, is not an account on this host"
    return {"owner_user": owner, "owner_home": str(home), "board_root": None}, ""


def root_checked_plan_identity(project: str, config_path: Path | None, plan_data: dict, config: object = None) -> dict:
    """`plan_data`, the tenant's own plan, with the account and trees root acts in held to root's records.

    The plan lives in the tenant's provision directory and is the tenant's to
    write, and root took its `owner_home` as the home it provisions a GitHub
    identity in and installs the listener unit under, its `board_root` as where
    it restores a release pointer, and its `owner_user` as whose record it
    writes. So as root, a plan naming anything other than root's record is
    refused, and a field it leaves out is root's record's (SYRD-664).
    Unprivileged callers, and a project root holds no record of, are given the
    plan as it is.

    Without a recorded board root, one the plan leaves out is the one `config`
    implies through its pane launcher, held to the owner's roots like a named
    one and put in the plan, so no consumer falls back past this check to the
    configuration's unchecked value (Audit of 4ebc856b).
    """
    from scripts import owner_writes
    from scripts import team_launcher as launcher

    if os.geteuid() != 0:
        return plan_data
    recorded, problem = root_recorded_identity(project, config_path)
    if problem:
        raise SystemExit(f"switchyard: {problem}. Nothing was changed.")
    if recorded is None:
        return plan_data
    checked = dict(plan_data)
    for field in PLAN_IDENTITY_FIELDS:
        named = str(plan_data.get(field) or "").strip()
        expected = recorded[field]
        if expected is None:
            # No record of it, so the owner's own roots bound it -- the plan's, or else the configuration's.
            source = "plan.json names"
            if not named and field == "board_root" and config is not None:
                implied = launcher._tenant_board_root_from_config(config)
                named = str(implied) if implied is not None else ""
                source = "configuration's pane launcher implies"
                if named:
                    checked[field] = named
            if named and not owner_writes.within_owner_root(str(recorded["owner_user"]), Path(named)):
                raise SystemExit(f"switchyard: {project}'s {source} {field} {named!r}, outside "
                                 f"{recorded['owner_user']}'s own roots; root does not act there. Nothing was changed.")
            continue
        differs = named != expected if field == "owner_user" else bool(named) and Path(named) != Path(expected)
        if differs:
            raise SystemExit(f"switchyard: {project}'s plan.json names {field} {named!r}, and root's own record "
                             f"names {expected!r}; root acts only on what its record names. Nothing was changed.")
        checked[field] = expected
    return checked
