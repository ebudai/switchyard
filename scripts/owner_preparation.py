"""Preparing a new project's owner account and project directory.

- `OwnerUserProvisionResult` and `ExistingOwnerUser` are the answers: what the
  preparation created, and who an existing owner account is.
- `_owner_user_verbatim`, `_existing_owner_user` and
  `_confirm_existing_owner_user` name the owner and, when the account already
  exists, say whose it is and ask before reusing it.
- `_resolve_owner_shell_path` and the `_owner_project_install_*` builders render
  the account creation and the project-directory install; the linger helpers
  render and read the owner's linger.
- `_existing_project_path_is_usable`, `_precheck_project_path_before_mutating`
  and `_verify_project_path_writable_by_owner` check the project path before
  and after, and `_ensure_owner_user_and_project_dir` runs the preparation in
  order.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-368). The launcher
imports this module at its top and re-exports every name, so
`switchyard_new_command` and every suite that reaches these through the
launcher reach the same objects, the classes included. Every launcher facility
these use -- the host-account lookups, the prompt, the group lookup and the
shared capability defaults -- every name defined here that another definition
here reads, and both classes' construction are read from `team_launcher` when
they run, as they were. The provisioning helpers are still imported inside the
functions that use them. The standard-library names are this module's own
imports, the same objects. This module never imports `team_launcher` at its
top.
"""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class OwnerUserProvisionResult:
    created: bool
    linger_enabled: bool
    shell_path: str = ""


def _owner_user_verbatim(value: str) -> str:
    owner = value.strip()
    if not owner:
        raise SystemExit("switchyard: owner user cannot be empty")
    return owner


@dataclass(frozen=True)
class ExistingOwnerUser:
    name: str
    uid: int
    home: Path


def _existing_owner_user(owner_user: str) -> ExistingOwnerUser | None:
    from scripts import team_launcher as launcher

    name = launcher._owner_user_verbatim(owner_user)
    uid = launcher.uid_for_user(name)
    if uid is None:
        return None
    home = launcher.home_dir_for_user(name) or Path("/home") / name
    return launcher.ExistingOwnerUser(name=name, uid=uid, home=home)


def _confirm_existing_owner_user(
    owner_user: str,
    *,
    allow_existing_owner_user: bool,
    input_func: Callable[[str], str],
    print_func: Callable[[str], None],
    agy_credential_source: str = "",
) -> None:
    from scripts import team_launcher as launcher

    existing = launcher._existing_owner_user(owner_user)
    if existing is None:
        return
    detail = f"{existing.name} (uid {existing.uid}, home {existing.home})"
    print_func(f"warning: switchyard: owner user {detail} already exists")
    if agy_credential_source:
        # Reusing an existing account and installing a credential into it are separate
        # things to consent to, so the confirmation has to name both.
        print_func(
            f"warning: switchyard: the agy credential from {agy_credential_source} will be "
            f"installed into {existing.name}, and every role of this project will be able "
            f"to act as the Google account {agy_credential_source} signed in to agy with"
        )
    if allow_existing_owner_user:
        return
    try:
        proceed = launcher._prompt_bool(f"Use existing owner user {detail}", default=False, input_func=input_func)
    except SystemExit as exc:
        if str(exc) == "switchyard: no input available":
            raise SystemExit(
                f"switchyard: owner user {detail} already exists; "
                "pass --allow-existing-owner-user to reuse it"
            ) from None
        raise
    if not proceed:
        raise SystemExit("switchyard: cancelled")


def _resolve_owner_shell_path(shell: str) -> str:
    from scripts import team_launcher as launcher

    requested = shell.strip()
    if not requested:
        raise SystemExit("switchyard: owner shell cannot be empty")
    if "/" in requested:
        if os.access(requested, os.X_OK):
            return requested
        raise SystemExit(
            f"switchyard: owner shell {requested!r} is not executable; "
            "install it or choose an installed shell"
        )
    resolved = shutil.which(requested)
    if resolved:
        return resolved
    if requested == launcher.PROJECT_DESIGN_DEFAULT_CAPABILITY_GRANTS["shell"]:
        for fallback in ("/bin/bash", "/bin/sh"):
            if os.access(fallback, os.X_OK):
                return fallback
        raise SystemExit(
            "switchyard: default owner shell 'fish' is unavailable and no fallback shell "
            "(/bin/bash or /bin/sh) is executable"
        )
    raise SystemExit(
        f"switchyard: owner shell {requested!r} was not found on PATH; "
        "install it or choose an installed shell"
    )


def _owner_project_install_args(owner_user: str, project_dir: Path, *, shell: str = "fish") -> list[list[str]]:
    from scripts import team_launcher as launcher

    shell_path = launcher._resolve_owner_shell_path(shell)
    return [
        ["id", "-u", owner_user],
        ["useradd", "-m", "-s", shell_path, owner_user],
        launcher._owner_project_install_command(owner_user, str(project_dir)),
    ]


def _owner_project_install_command(owner_user: str, directory: str) -> list[str]:
    from scripts.ticket_board.project_provision import TENANT_SOURCE_MODE

    return ["install", "-d", "-m", TENANT_SOURCE_MODE, "-o", owner_user, "-g", owner_user, directory]


def _owner_project_install_commands(
    owner_user: str, project_dir: Path, *, owner_home: Path | None = None
) -> list[list[str]]:
    """One install(1) per directory of the tenant's tree, not just the leaf.

    `install -d` creates every missing component of a path but applies `-m`,
    `-o` and `-g` only to the last one. Intermediates get the caller's umask and
    the caller's ownership, and here the caller is root -- which is how a tenant
    ends up with a root-owned 0755 `Projects` above a checkout owned by the
    tenant, and how a service account granted nothing but traversal on the home
    could still read the source below it (SYRD-156).
    """
    from scripts import team_launcher as launcher

    from scripts.ticket_board.project_provision import owned_ancestor_dirs

    directories: list[str] = []
    if owner_home is not None:
        directories = list(
            owned_ancestor_dirs(str(owner_home), str(project_dir), include_target=True)
        )
    if not directories:
        directories = [str(project_dir)]
    return [launcher._owner_project_install_command(owner_user, directory) for directory in directories]


def _enable_owner_linger_args(owner_user: str) -> list[str]:
    return ["loginctl", "enable-linger", owner_user]


def _owner_linger_show_args(owner_user: str) -> list[str]:
    return ["loginctl", "show-user", owner_user, "-p", "Linger", "--value"]


def _owner_linger_is_enabled(
    owner_user: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> bool:
    from scripts import team_launcher as launcher

    result = runner(
        launcher._owner_linger_show_args(owner_user),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.returncode == 0 and str(getattr(result, "stdout", "") or "").strip() == "yes"


def _existing_project_path_is_usable(project_dir: Path, owner_user: str) -> bool:
    from scripts import team_launcher as launcher

    try:
        info = project_dir.stat()
    except OSError:
        return False
    if not stat.S_ISDIR(info.st_mode):
        return False
    uid = launcher.uid_for_user(owner_user)
    if uid is not None and int(info.st_uid) == uid:
        mask = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
    elif uid is not None and int(info.st_gid) in launcher._group_ids_for_user(owner_user):
        mask = stat.S_IRGRP | stat.S_IWGRP | stat.S_IXGRP
    else:
        mask = stat.S_IROTH | stat.S_IWOTH | stat.S_IXOTH
    return (info.st_mode & mask) == mask


def _precheck_project_path_before_mutating(owner_user: str, project_dir: Path) -> None:
    from scripts import team_launcher as launcher

    if not project_dir.exists():
        return
    if not project_dir.is_dir():
        raise SystemExit(f"switchyard: project path {project_dir} already exists but is not a directory")
    if not launcher._existing_project_path_is_usable(project_dir, owner_user):
        raise SystemExit(
            f"switchyard: project path {project_dir} already exists but is not readable and writable "
            f"by {owner_user}; choose a writable path or fix ownership/permissions first"
        )


def _verify_project_path_writable_by_owner(
    owner_user: str,
    project_dir: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    quoted_path = shlex.quote(str(project_dir))
    result = runner(
        [
            "sudo",
            "-u",
            owner_user,
            "sh",
            "-lc",
            f"test -d {quoted_path} && test -r {quoted_path} && test -w {quoted_path} && test -x {quoted_path}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"switchyard: project path {project_dir} is not readable and writable by {owner_user}; "
            "choose a writable path or fix ownership/permissions first"
        )


def _ensure_owner_user_and_project_dir(
    owner_user: str,
    project_dir: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    shell: str = "fish",
    owner_home: Path | None = None,
) -> OwnerUserProvisionResult:
    from scripts import team_launcher as launcher

    existed = project_dir.exists()
    launcher._precheck_project_path_before_mutating(owner_user, project_dir)
    id_args = ["id", "-u", owner_user]
    install_commands = launcher._owner_project_install_commands(
        owner_user, project_dir, owner_home=owner_home
    )
    id_result = runner(id_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    created_user = False
    linger_enabled = False
    shell_path = ""
    if id_result.returncode != 0:
        _id_args, useradd_args, _install_args = launcher._owner_project_install_args(owner_user, project_dir, shell=shell)
        shell_path = useradd_args[useradd_args.index("-s") + 1]
        useradd_result = runner(useradd_args)
        if useradd_result.returncode != 0:
            raise SystemExit(f"switchyard: failed to create user {owner_user!r}")
        created_user = True
        linger_result = runner(launcher._enable_owner_linger_args(owner_user))
        if linger_result.returncode != 0:
            raise SystemExit(f"switchyard: failed to enable linger for created user {owner_user!r}")
        linger_enabled = True
    elif not launcher._owner_linger_is_enabled(owner_user, runner=runner):
        raise SystemExit(
            f"switchyard: existing user {owner_user!r} does not have linger enabled; "
            "switchyard refuses to modify an existing owner user"
        )
    if not existed:
        for install_args in install_commands:
            install_result = runner(install_args)
            if install_result.returncode != 0:
                raise SystemExit(f"switchyard: failed to create project directory {install_args[-1]}")
        # Each named directory at its own mode rather than one `parents=True`
        # over the whole path, which would leave everything it created at the
        # caller's umask -- root's -- and reopen what naming them just closed
        # (SYRD-156). The mode comes from the same argv, so there is one place
        # to change it. Anything ABOVE the first named directory is still
        # created the old way: for a checkout inside the owner home there is
        # nothing above it but the home itself, and for one outside the home
        # this is not the tenant tree being confined.
        for install_args in install_commands:
            path = Path(install_args[-1])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.mkdir(mode=int(install_args[3], 8), exist_ok=True)
    launcher._verify_project_path_writable_by_owner(owner_user, project_dir, runner=runner)
    return launcher.OwnerUserProvisionResult(created=created_user, linger_enabled=linger_enabled, shell_path=shell_path)
