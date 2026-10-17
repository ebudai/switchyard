"""Root's writes and reads of a tenant's generated files: by descriptor from the owner's home (SYRD-608).

As root on a tenant's behalf the launcher wrote the tenant's generated files
by path, handed them over with `chown o:o PATH` and read some back by path.
Each followed a link the tenant left at the file or at any directory above
it, so a planted link had root overwrite, give away or read back a file of
the tenant's choosing. Everything here goes through `scripts/tenant_paths.py`
from the owner's home instead: no link at any component is followed, a
component swapped after root looked is refused, and a path outside the home
-- an absolute `layout` in the tenant's own config, a `..` -- is refused, never
normalised in. A caller acting for itself (not root, or the owner) is
unchanged.

The home is the trusted root, and so is a project directory root itself chose
for that owner somewhere else (`new --project-path`, SYRD-607): the one `new`
was handed (`trust_project_root`), or one the root-owned registry names. Either
is used only while it is a real directory the owner owns, never one derived from
the tenant's own documents.

The launcher re-exports these, and they read the launcher's names -- the home
lookup and the current user -- through it when they run, so a patch there
reaches them. This module imports `team_launcher` only inside the functions.
"""

from __future__ import annotations

import json
import os
import pwd
import stat
from pathlib import Path, PurePosixPath
from typing import Any


def owner_home(owner_user: str) -> str:
    """The owner's home: the only trusted starting point for root's work on the owner's files."""
    from scripts import team_launcher as launcher

    home = launcher.home_dir_for_user(owner_user) if owner_user else None
    if home is None:
        raise ValueError("file ownership repair requires run_as_user")
    return str(home)


#: Project directories root chose for an owner in this process (what `new` was handed), beside the home.
TRUSTED_PROJECT_ROOTS: dict[str, set[str]] = {}


def trust_project_root(owner_user: str, project_dir: Path) -> None:
    """Root's own choice of where an owner's project lives: a root for writes on the owner's behalf."""
    TRUSTED_PROJECT_ROOTS.setdefault(owner_user, set()).add(str(project_dir))


def _registered_project_roots() -> list[str]:
    from scripts import team_launcher as launcher

    roots = []
    for entry in launcher._registry_project_entries():
        provision = Path(entry.config_path).parent
        if provision.name == "provision" and provision.parent.name == ".switchyard":
            roots.append(str(provision.parent.parent))
    return roots


def _owned_directory(path: str, owner_user: str) -> bool:
    try:
        status = os.lstat(path)
        return stat.S_ISDIR(status.st_mode) and status.st_uid == pwd.getpwnam(owner_user).pw_uid
    except (OSError, KeyError):
        return False


def owner_root(owner_user: str, path: Path) -> str:
    """The trusted root `path` is written under for `owner_user`: the home, or a project directory root chose.

    A path in none of them gets the home, which `tenant_paths` then refuses as
    outside it. Only a normalised absolute path is ever matched.
    """
    home = owner_home(owner_user)
    text = str(path)
    if text != str(PurePosixPath(text)) or not text.startswith("/") or "/../" in f"{text}/":
        return home
    candidates = [*sorted(TRUSTED_PROJECT_ROOTS.get(owner_user, ())), *_registered_project_roots()]
    for root in [home, *candidates]:
        inside = text == root or text.startswith(root.rstrip("/") + "/")
        if inside and (root == home or _owned_directory(root, owner_user)):
            return root
    return home


def owner_handoff_args(owner_user: str, path: Path) -> list[str]:
    """The argv that gives `path` to `owner_user`: the descriptor helper's `attrs`, rooted at the owner's root."""
    from scripts.ticket_board.provision_tenant_paths import tenant_paths_argv

    return tenant_paths_argv(owner_root(owner_user, path), "attrs", "--owner", owner_user, "--group", owner_user, str(path))


def root_writes_for(owner_user: str) -> bool:
    """Whether a file written now for `owner_user` is written by root on the owner's behalf."""
    from scripts import team_launcher as launcher

    return bool(owner_user) and os.geteuid() == 0 and launcher.current_user_name() != owner_user


def write_owner_file(owner_user: str, path: Path, content: bytes | str, *, mode: int | None = None,
                     parents_mode: int = 0o755, parent_mode: int | None = None) -> None:
    """Publish a generated file the owner is to own: whole, and as root by descriptor from the owner's home.

    As root (and not the owner), the file is `tenant_paths.write_file`d under
    the owner's home: never through a link at any component, never outside
    the home, made owner:owner-group with `mode` (None keeps the replaced
    file's, else 0600) before it is renamed into place; a missing directory on
    the way is made the owner's with `parents_mode`, and `parent_mode` sets the
    parent's own. Otherwise it is the caller's own file, written as before.
    """
    from scripts import tenant_paths

    data = content.encode("utf-8") if isinstance(content, str) else content
    if not root_writes_for(owner_user):
        path.write_bytes(data)
        return
    try:
        account = pwd.getpwnam(owner_user)
    except KeyError:
        raise SystemExit(f"switchyard: refusing to write {path}: {owner_user} is not an account on this host") from None
    try:
        tenant_paths.write_file(owner_root(owner_user, path), str(path), data, uid=account.pw_uid, gid=account.pw_gid,
                                mode=mode, parents=(parents_mode, account.pw_uid, account.pw_gid),
                                parent_mode=parent_mode)
    except tenant_paths.Refused as refusal:
        raise SystemExit(f"switchyard: refusing to write {path} for {owner_user}: {refusal}. Nothing was written.") from None
    except SystemExit as stop:
        raise SystemExit(f"switchyard: refusing to write {path} for {owner_user}: {stop}") from None


def write_root_record(owner_user: str, path: Path, content: bytes, *, mode: int) -> None:
    """A record root keeps in a tenant's directory, as root's own file: written as `write_owner_file` writes.

    By descriptor from the owner's home, staged where only root can reach it
    and renamed into place; owned by the process's own (kernel) identity, as
    a record root made, not handed to the tenant.
    """
    from scripts import tenant_paths

    try:
        tenant_paths.write_file(owner_root(owner_user, path), str(path), content, uid=os.getresuid()[1],
                                gid=os.getresgid()[1], mode=mode)
    except tenant_paths.Refused as refusal:
        raise SystemExit(f"switchyard: refusing to write {path} for {owner_user}: {refusal}. Nothing was written.") from None
    except SystemExit as stop:
        raise SystemExit(f"switchyard: refusing to write {path} for {owner_user}: {stop}") from None


def read_owner_bytes(owner_user: str, path: Path) -> bytes | None:
    """A tenant's file that root is about to act on, read without following a link; None when it is missing.

    Root reading it by name would follow a link the tenant planted and carry
    whatever root can read into a file the tenant owns. As root (and not the
    owner) it is read by descriptor from the owner's home; otherwise it is the
    caller's own, read as before.
    """
    from scripts import tenant_paths

    if not root_writes_for(owner_user):
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None
    try:
        return tenant_paths.read_file(owner_root(owner_user, path), str(path))
    except tenant_paths.Refused as refusal:
        raise SystemExit(f"switchyard: refusing to read {path} for {owner_user}: {refusal}") from None
    except SystemExit as stop:
        raise SystemExit(f"switchyard: refusing to read {path} for {owner_user}: {stop}") from None


def read_owner_json(owner_user: str, path: Path) -> dict[str, Any]:
    """`read_owner_bytes` for a JSON object; missing is `FileNotFoundError`, as `_load_json` has it."""
    from scripts import team_launcher as launcher

    if not root_writes_for(owner_user):
        return launcher._load_json(path)
    data = read_owner_bytes(owner_user, path)
    if data is None:
        raise FileNotFoundError(2, "No such file or directory", str(path))
    parsed = json.loads(data.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise SystemExit(f"{path} must contain a JSON object")
    return parsed
