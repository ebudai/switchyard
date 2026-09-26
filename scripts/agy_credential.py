"""Where a host's agy credential comes from, and seeding it for a project owner.

agy authenticates by browser, so an unattended project owner cannot log in by
itself. The operator names, once per host, an account whose agy token new
projects may copy. That account is `switchyard agy-credential`, recorded in a
root-owned setting. A project may override the choice or opt out.

This module covers:
- reading and writing that setting;
- resolving which source a `switchyard new` uses;
- validating the source token without following links;
- judging the owner's own token;
- copying the token into the owner's home (`_seed_agy_credential_for_owner`)
  with the owner's ownership and private mode.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-287). It stands on the
no-follow primitives and token layout constants in `scripts/role_credentials.py`,
imported by name; that module never imports this one. `team_launcher` imports
this module at its top and still exports the names callers reach there. This
module never imports `team_launcher` at its top: launcher facilities are read
from `scripts.team_launcher` when a function runs, and so are the two
owner-traversal checks, whose patches the suites apply on the launcher.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from scripts.role_credentials import (
    AGY_CREDENTIAL_DIR_NAME,
    AGY_CREDENTIAL_TOKEN_NAME,
    _copy_fd_contents,
    _openat_no_follow,
    _openat_no_follow_keep_parent,
)


# Machine-scoped, so the operator names the account once instead of on every provision.
DEFAULT_AGY_CREDENTIAL_SETTING_PATH = Path("/etc/switchyard/agy-credential.json")


AGY_SOURCE_FROM_HOST = "host_default"


AGY_SOURCE_FROM_OVERRIDE = "project_override"


AGY_SOURCE_FROM_OPT_OUT = "opt_out"


AGY_SOURCE_UNSET = "unset"


def _agy_source_token_path(source_user: str, home_base: Path) -> Path:
    return home_base / source_user / AGY_CREDENTIAL_DIR_NAME / AGY_CREDENTIAL_TOKEN_NAME


def _open_agy_source_token(source_user: str, home_base: Path) -> int:
    """Open the opted-in source token and return a validated read-only fd.

    Opened with O_NOFOLLOW and validated by fstat on the fd rather than by stat on the
    path. Both matter: the source home is writable by an unprivileged account, so a
    path-based check would let that account swap the token for a symlink to any
    root-readable file between the check and a root-run copy. The caller closes the fd.
    """
    from scripts import team_launcher as launcher

    source_token = _agy_source_token_path(source_user, home_base)
    # Resolved before the file is touched, and never optional: without a uid to compare
    # against there is no owner boundary at all, and a deleted or mistyped account whose
    # stale home survives would be accepted on the strength of the filename alone.
    expected_uid = launcher._uid_for_user(source_user)
    if expected_uid is None:
        raise SystemExit(
            f"switchyard: agy_credential_source {source_user!r} is not a user on this machine; "
            "seeding copies a token only from an existing account that owns it"
        )
    try:
        fd = os.open(source_token, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SystemExit(
                f"switchyard: {source_token} is a symlink; agy credential seeding copies only a "
                "regular file from the source user's own home and will not follow a link out of it"
            ) from exc
        raise SystemExit(
            f"switchyard: agy credential seeding was requested from {source_user!r}, but "
            f"{source_token} could not be read ({exc.strerror}); sign in to agy as "
            f"{source_user} first, or clear capability_grants.agy_credential_source"
        ) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SystemExit(
                f"switchyard: {source_token} is not a regular file; refusing to seed from it"
            )
        if info.st_uid != expected_uid:
            raise SystemExit(
                f"switchyard: {source_token} is owned by uid {info.st_uid}, not {source_user} "
                f"(uid {expected_uid}); refusing to copy a token that user does not own"
            )
    except BaseException:
        os.close(fd)
        raise
    return fd


def _validate_agy_credential_source(source_user: str, owner_user: str, home_base: Path) -> None:
    """Reject an unusable credential source before provisioning mutates anything.

    Called ahead of the first mutation so a bad source cannot leave a half-provisioned
    project behind: the owner user would then exist, and seeding deliberately refuses to
    touch an existing owner, so the retry could never reach the requested end state.
    """
    from scripts import team_launcher as launcher

    if not launcher._is_valid_owner_user_name(source_user):
        raise SystemExit(
            f"switchyard: agy_credential_source {source_user!r} is not a plain Unix user name"
        )
    if source_user == owner_user:
        raise SystemExit(
            f"switchyard: agy_credential_source {source_user!r} is the project owner user itself; "
            "seeding needs a different source account"
        )
    os.close(_open_agy_source_token(source_user, home_base))


AGY_CREDENTIAL_INSTALLED = "installed"


AGY_CREDENTIAL_ABSENT = "absent"


AGY_CREDENTIAL_UNUSABLE = "unusable"


def _agy_credential_state(owner_user: str, home_base: Path) -> str:
    """Whether the owner already holds a usable seeded token.

    The seeding decision is made on this, not on whether this run created the owner: a
    seed that failed after owner creation must still be completable by a retry, and a
    retry that skipped seeding would let provisioning finish and record a credential
    source that was never installed.
    """
    from scripts import team_launcher as launcher

    target = home_base / owner_user / AGY_CREDENTIAL_DIR_NAME / AGY_CREDENTIAL_TOKEN_NAME
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return AGY_CREDENTIAL_ABSENT
    except OSError:
        return AGY_CREDENTIAL_UNUSABLE
    try:
        info = os.fstat(fd)
    finally:
        os.close(fd)
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        return AGY_CREDENTIAL_UNUSABLE
    # Ownership is the point of the check, not a refinement of it: a 0600 token owned by
    # root or by any other account is unreadable to the pane owner, so treating it as
    # installed would complete provisioning and record a credential agy still cannot use.
    # Unresolvable owner means no uid to compare, which is not a pass.
    owner_uid = launcher._uid_for_user(owner_user)
    if owner_uid is None or info.st_uid != owner_uid:
        return AGY_CREDENTIAL_UNUSABLE
    return AGY_CREDENTIAL_INSTALLED


def _has_usable_agy_token(user_name: str, home_base: Path) -> bool:
    try:
        os.close(_open_agy_source_token(user_name, home_base))
    except SystemExit:
        return False
    return True


def _resolve_agy_credential_source(
    *,
    override: str | None,
    opt_out: bool,
    artifact_value: str,
    home_base: Path,
    settings_path: Path | None,
    yes: bool,
    input_func: Callable[[str], str],
    print_func: Callable[[str], None],
) -> tuple[str, str]:
    """Decide the credential source and where the decision came from.

    The invoking human is only ever a *proposal*: it is offered interactively and stored
    only once confirmed. A noninteractive run uses a recorded host setting or an explicit
    override and otherwise seeds nothing, so SUDO_USER is never inferred silently.
    """
    from scripts import team_launcher as launcher

    if opt_out:
        return "", AGY_SOURCE_FROM_OPT_OUT
    explicit = (override or "").strip()
    if explicit:
        if not launcher._is_valid_owner_user_name(explicit):
            raise SystemExit(
                f"switchyard: --agy-credential-source must be a plain Unix user name, not "
                f"{explicit!r}"
            )
        return explicit, AGY_SOURCE_FROM_OVERRIDE
    if artifact_value:
        return artifact_value, AGY_SOURCE_FROM_OVERRIDE
    recorded = read_host_agy_credential_source(settings_path)
    if recorded:
        return recorded, AGY_SOURCE_FROM_HOST
    if yes:
        return "", AGY_SOURCE_UNSET
    proposed = launcher.default_gui_user()
    if (
        not proposed
        or proposed == "root"
        or not launcher._is_valid_owner_user_name(proposed)
        or launcher._uid_for_user(proposed) is None
        or not _has_usable_agy_token(proposed, home_base)
    ):
        # Nothing worth offering: no invoking human, or that account has no agy token to
        # share. Offering one anyway would train the operator to accept a prompt that
        # cannot work.
        return "", AGY_SOURCE_UNSET
    print_func(
        f"switchyard: no agy credential source is recorded for this host. Seeding one lets "
        f"each new project's panes use agy without their own login, by sharing the Google "
        f"account that user signed in with."
    )
    if not launcher._prompt_bool(
        f"Record {proposed} as this host's agy credential source", default=False, input_func=input_func
    ):
        print_func(
            "switchyard: continuing without an agy credential source; set one later with "
            "`switchyard agy-credential set USER`"
        )
        return "", AGY_SOURCE_UNSET
    path = write_host_agy_credential_source(proposed, settings_path=settings_path, confirmed_by=proposed)
    print_func(f"switchyard: recorded agy credential source {proposed} ({path})")
    return proposed, AGY_SOURCE_FROM_HOST


def _agy_credential_setting_path(settings_path: Path | None = None) -> Path:
    return settings_path or DEFAULT_AGY_CREDENTIAL_SETTING_PATH


def read_host_agy_credential_source(settings_path: Path | None = None) -> str:
    """The account this host seeds agy credentials from, or empty when unset."""
    from scripts import team_launcher as launcher

    path = _agy_credential_setting_path(settings_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(raw, dict):
        return ""
    source = str(raw.get("source_user") or "").strip()
    if source and not launcher._is_valid_owner_user_name(source):
        raise SystemExit(
            f"switchyard: {path} records source_user {source!r}, which is not a plain Unix "
            "user name; fix or clear it with `switchyard agy-credential clear`"
        )
    return source


def write_host_agy_credential_source(
    source_user: str,
    *,
    settings_path: Path | None = None,
    confirmed_by: str = "",
) -> Path:
    from scripts import team_launcher as launcher

    path = _agy_credential_setting_path(settings_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    launcher._write_json_atomic(
        path,
        {
            "source_user": source_user,
            "confirmed_by": confirmed_by or launcher.current_user_name(),
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )
    return path


def switchyard_agy_credential_command(
    action: str,
    *,
    source_user: str | None = None,
    settings_path: Path | None = None,
    print_func: Callable[[str], None] = print,
) -> int:
    """show / set / clear the host-wide agy credential source."""
    from scripts import team_launcher as launcher

    path = _agy_credential_setting_path(settings_path)
    if action == "show":
        current = read_host_agy_credential_source(settings_path)
        if current:
            print_func(f"switchyard: agy credential source: {current} ({path})")
        else:
            print_func(f"switchyard: no agy credential source recorded ({path})")
        return 0
    if action == "set":
        candidate = (source_user or "").strip()
        if not launcher._is_valid_owner_user_name(candidate):
            raise SystemExit(
                f"switchyard: agy credential source must be a plain Unix user name, not "
                f"{candidate!r}"
            )
        if launcher._uid_for_user(candidate) is None:
            raise SystemExit(f"switchyard: {candidate!r} is not a user on this machine")
        write_host_agy_credential_source(candidate, settings_path=settings_path)
        print_func(f"switchyard: agy credential source set to {candidate} ({path})")
        print_func(
            f"switchyard: new projects will seed from {candidate}, so each project's role "
            "panes can act as the Google account it signed in to agy with"
        )
        return 0
    if action == "clear":
        try:
            path.unlink()
        except FileNotFoundError:
            print_func(f"switchyard: no agy credential source recorded ({path})")
            return 0
        print_func(f"switchyard: cleared agy credential source ({path})")
        return 0
    raise SystemExit(f"switchyard: unknown agy-credential action {action!r}")


def _open_owner_credential_dir(
    owner_user: str,
    home_base: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> int:
    """Create the owner's private credential directory and return a descriptor for it.

    Two things are being defended against. The owner controls this part of the tree, so
    no privileged command is handed the whole path: install(1) follows a symlink that
    names a directory and would apply mode and ownership to whatever it points at.
    Instead each component is created with mkdirat and opened with openat and O_NOFOLLOW
    relative to the descriptor above it, and everything later uses those descriptors.

    And a component that already exists is proven reachable by the owner rather than
    assumed to be, because a directory switchyard did not just create and chown may be
    left over from a failed attempt or belong to someone else entirely.
    """
    from scripts import team_launcher as launcher

    target_dir = home_base / owner_user / AGY_CREDENTIAL_DIR_NAME
    fd = os.open(home_base, os.O_RDONLY | os.O_DIRECTORY)
    try:
        # The home is usually created by useradd, but a Director-approved pre-existing
        # owner may bring one switchyard never made, so its reachability is proven here
        # rather than assumed. Everything below it is owner-controlled.
        fd = _openat_no_follow(fd, owner_user, target_dir)
        launcher._require_owner_home_traversable(fd, owner_user, home_base / owner_user)
        for component in Path(AGY_CREDENTIAL_DIR_NAME).parts:
            created = False
            try:
                os.mkdir(component, 0o700, dir_fd=fd)
                created = True
            except FileExistsError:
                pass
            except OSError as exc:
                raise SystemExit(
                    f"switchyard: failed to create {target_dir} for {owner_user} "
                    f"({exc.strerror})"
                ) from exc
            parent_fd = fd
            child_fd = _openat_no_follow_keep_parent(parent_fd, component, target_dir)
            try:
                if created:
                    os.fchmod(child_fd, 0o700)
                    own = runner(
                        ["chown", f"{owner_user}:{owner_user}", f"/proc/{os.getpid()}/fd/{child_fd}"]
                    )
                    if own.returncode != 0:
                        raise SystemExit(
                            f"switchyard: failed to assign {target_dir} to {owner_user}"
                        )
                else:
                    launcher._require_owner_traversable(child_fd, owner_user, component, target_dir)
            except BaseException:
                os.close(child_fd)
                if created:
                    # Do not leave a directory whose ownership was never established: the
                    # retry would find it existing and, without this, trust it.
                    try:
                        os.rmdir(component, dir_fd=parent_fd)
                    except OSError:
                        pass
                raise
            os.close(parent_fd)
            fd = child_fd
    except BaseException:
        os.close(fd)
        raise
    return fd


def _seed_agy_credential_for_owner(
    *,
    owner_user: str,
    source_user: str,
    home_base: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    print_func: Callable[[str], None] = print,
) -> None:
    """Copy one agy OAuth token into a freshly created owner home.

    Opt-in only, and only for a newly created owner: an existing owner user is never
    modified. The source is re-validated here on the fd actually copied, so this does not
    rely on the earlier precheck still being true.
    """
    _validate_agy_credential_source(source_user, owner_user, home_base)
    target_dir = home_base / owner_user / AGY_CREDENTIAL_DIR_NAME
    target_token = target_dir / AGY_CREDENTIAL_TOKEN_NAME
    dir_fd = _open_owner_credential_dir(owner_user, home_base, runner=runner)
    source_fd = _open_agy_source_token(source_user, home_base)
    created_target = False
    try:
        try:
            target_fd = os.open(
                AGY_CREDENTIAL_TOKEN_NAME,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=dir_fd,
            )
        except FileExistsError as exc:
            raise SystemExit(
                f"switchyard: {target_token} already exists; refusing to overwrite it"
            ) from exc
        except OSError as exc:
            raise SystemExit(
                f"switchyard: failed to seed agy credential into {target_token} ({exc.strerror})"
            ) from exc
        created_target = True
        try:
            os.fchmod(target_fd, 0o600)
            _copy_fd_contents(source_fd, target_fd)
            # Ownership is assigned to the open description, not to the path. Naming the
            # path again here would hand the owner a second chance to substitute a
            # different object between the copy and the chown.
            own_result = runner(
                [
                    "chown",
                    f"{owner_user}:{owner_user}",
                    f"/proc/{os.getpid()}/fd/{target_fd}",
                ]
            )
            if own_result.returncode != 0:
                raise SystemExit(f"switchyard: failed to assign {target_token} to {owner_user}")
        finally:
            os.close(target_fd)
    except BaseException:
        # Remove only the entry proven to have been created, resolved through the same
        # anchored directory rather than by walking the path again.
        if created_target:
            try:
                os.unlink(AGY_CREDENTIAL_TOKEN_NAME, dir_fd=dir_fd)
            except OSError:
                pass
        raise
    finally:
        os.close(source_fd)
        os.close(dir_fd)
    print_func(f"switchyard: seeded agy credential for {owner_user} from {source_user}")
    print_func(
        f"switchyard: {owner_user} can now act as the Google account that {source_user} signed "
        "in to agy with; every role pane of this project shares that one account"
    )
