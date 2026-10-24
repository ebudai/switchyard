"""Who root acts for when it acts for a tenant: the owner root's own records name, never the tenant's file (SYRD-663).

- `root_recorded_owner` is the owner root's records name for a project: its
  provisioning record's `owner_user`, or, for a tenant root registered but holds
  no plan for, the account owning the project directory its registry entry
  names.
- `root_owner_refusal` is why root may not act for a project as the account a
  configuration's `run_as_user` names -- blank, `root`, or anybody but that
  owner -- or nothing.

The command entry points ask `root_recorded_owner` strictly, before the
configuration is read, and `load_project_config` asks `root_owner_refusal` on
every load root makes -- including the loads of a root process a catalogued
action starts, which has no entry point of its own. The launcher imports this
module and re-exports both names; every launcher facility these use is read
from `team_launcher` when they run. This module never imports `team_launcher`
at its top.
"""

from __future__ import annotations

import json
import os
import pwd
import stat
from pathlib import Path


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
