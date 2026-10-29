"""Root's record of an adopted conversation: the proof SYRD-694's start trusts, and nobody else can write.

Every role of a tenant runs as the same account, so nothing that account can write tells the control role apart
from a sibling: a well-formed adoption file in the project's own store could have been put there by any of them.
So the record that lets a start resume a conversation filed under another workspace is root's:

- it is written only by the catalogued privileged action `adopt-conversation`, whose root helper first proves the
  caller is the control role's registered pane (`privileged_helper.require_registered_control_caller`) and that
  the role named is that role, and removed only by `withdraw-conversation-adoption`, under the same proof;
- the caller names only the project, role, conversation and the digest of the preview it confirmed. Root runs
  that plan again with the shared release's own code, as the tenant account its registry records
  (`runuser`, a clean environment), and records the plan's own values, only when the plan has no problems,
  is for exactly that project, role and conversation, and has exactly that digest. The friendly command is
  not the boundary; this is;
- it lives at a fixed path, `/var/lib/switchyard/conversation-adoptions/<conversation>.json`, that no
  configuration names;
- each read and write reaches it through descriptors from `/`, refusing a link at every component, and requiring
  every directory and the file to be root's and writable by nobody else. A record the tenant account wrote, or a
  path it could have redirected, is refused, not read.

Root reads no tenant file itself, and takes no path from its caller. The plan is computed as the tenant, over the
tenant's own stores, exactly as the preview was; the workspace and worktree are bound by the sha256 of each
normalized path. This module imports only the standard library, because root runs it from the shared release
before anything else is loaded (`scripts/switchyard`).
"""

from __future__ import annotations

import errno
import json
import os
import re
import stat
import sys
import time
from pathlib import Path
from typing import Any, Sequence

SCHEMA = "switchyard.conversation-adoption.v2"
ROOT = Path("/var/lib/switchyard/conversation-adoptions")
#: Whose every directory and record must be, from `ANCHOR` down. A suite that cannot be root points both at a
#: tree of its own, and `conversation_adoption_test` asserts what ships.
TRUSTED_OWNER_UID = 0
ANCHOR = Path("/")
SHARED_WRITE = stat.S_IWGRP | stat.S_IWOTH
CONVERSATION = re.compile(r"[0-9A-Za-z][0-9A-Za-z-]{7,127}")
MAX_BYTES = 64 << 10
FIELDS = ("project", "role", "target", "runtime", "session_id", "workspace_sha256", "worktree_sha256", "plan_digest")
#: Root's registry of tenants, whose `board_ui.tenant_user` is the account a tenant's plan is computed as.
REGISTRY = Path("/etc/switchyard/projects")
RUNUSER = "/usr/bin/runuser"
PYTHON = "/usr/bin/python3"
#: The shared release's own wrapper, beside this module.
WRAPPER = Path(__file__).resolve().parent / "switchyard"
PLAN_TIMEOUT_SECONDS = 120.0
PLAN_SCHEMA = "switchyard.conversation-adoption-plan.v2"


class RecordError(Exception):
    """The record cannot be trusted, written or removed; the message says which path and why."""


def _owner(owner_uid: int | None) -> int:
    return TRUSTED_OWNER_UID if owner_uid is None else owner_uid


def _check(fd: int, where: str, owner_uid: int, *, directory: bool) -> None:
    info = os.fstat(fd)
    if (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)) is False:
        raise RecordError(f"{where} is not a {'directory' if directory else 'regular file'}")
    if info.st_uid != owner_uid or info.st_mode & SHARED_WRITE:
        raise RecordError(f"{where} is not owned by uid {owner_uid} and writable only by it")


def _open_root(root: Path, owner_uid: int, *, create: bool, anchor: Path | None = None) -> int:
    """A descriptor for `root`, reached from `anchor` (`/` as shipped) without following a link.

    The anchor and every directory below it must be the trusted owner's and writable by nobody else. `create`
    makes the missing directories, mode 0755, as that owner.
    """
    anchor = Path(os.path.abspath(str(anchor or ANCHOR)))
    absolute = Path(os.path.abspath(str(root)))
    if absolute != anchor and anchor not in absolute.parents:
        raise RecordError(f"{absolute} is not under {anchor}")
    fd = os.open(str(anchor), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    walked = anchor
    try:
        _check(fd, str(walked), owner_uid, directory=True)
        for component in absolute.relative_to(anchor).parts:
            walked = walked / component
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            try:
                child = os.open(component, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise RecordError(f"{walked} does not exist") from None
                os.mkdir(component, 0o755, dir_fd=fd)
                child = os.open(component, flags, dir_fd=fd)
                os.fchmod(child, 0o755)  # readable by the tenant's starts whatever root's umask is
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise RecordError(f"{walked} is a link or not a directory") from None
                raise RecordError(f"{walked} cannot be opened ({exc.strerror})") from None
            os.close(fd)
            fd = child
            _check(fd, str(walked), owner_uid, directory=True)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _name(session_id: str) -> str:
    if not CONVERSATION.fullmatch(session_id or ""):
        raise RecordError(f"{session_id!r} is not a conversation id")
    return f"{session_id}.json"


def _load_json(fd: int, name: str, where: str, owner_uid: int) -> Any:
    try:
        file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise RecordError(f"{where} cannot be opened without following a link ({exc.strerror})") from None
    with os.fdopen(file_fd, "rb") as handle:
        _check(handle.fileno(), where, owner_uid, directory=False)
        raw = handle.read(MAX_BYTES)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RecordError(f"{where} is not JSON") from None


def _load(fd: int, name: str, where: str, owner_uid: int) -> dict[str, Any]:
    record = _load_json(fd, name, where, owner_uid)
    if not isinstance(record, dict) or record.get("schema") != SCHEMA or f"{record.get('session_id')}.json" != name:
        raise RecordError(f"{where} is not a {SCHEMA} record for its name")
    return record


def read(session_id: str, *, root: Path | None = None, owner_uid: int | None = None,
         anchor: Path | None = None) -> dict[str, Any] | None:
    """Root's adoption of this conversation; None when there is none, or none that can be trusted."""
    root, owner = root or ROOT, _owner(owner_uid)
    try:
        name = _name(session_id)
        fd = _open_root(root, owner, create=False, anchor=anchor)
    except (RecordError, OSError):
        return None
    try:
        return _load(fd, name, str(root / name), owner)
    except (RecordError, OSError):
        return None
    finally:
        os.close(fd)


def listing(*, root: Path | None = None, owner_uid: int | None = None,
         anchor: Path | None = None) -> list[dict[str, Any]]:
    """Every adoption root has recorded and can be trusted, in name order."""
    root, owner = root or ROOT, _owner(owner_uid)
    try:
        fd = _open_root(root, owner, create=False, anchor=anchor)
    except (RecordError, OSError):
        return []
    try:
        found = []
        for name in sorted(os.listdir(fd)):
            if name.endswith(".json") and CONVERSATION.fullmatch(name.removesuffix(".json")):
                try:
                    found.append(_load(fd, name, str(root / name), owner))
                except (RecordError, OSError):
                    continue
        return found
    finally:
        os.close(fd)


def write(values: dict[str, str], *, root: Path | None = None, owner_uid: int | None = None,
          anchor: Path | None = None, now: float | None = None) -> Path:
    """Record an adoption, never over an existing one; the path written."""
    root, owner = root or ROOT, _owner(owner_uid)
    missing = [key for key in FIELDS if not values.get(key)]
    if missing:
        raise RecordError(f"an adoption needs {', '.join(missing)}")
    name = _name(values["session_id"])
    record = {"schema": SCHEMA, **{key: values[key] for key in FIELDS}, "recorded_at": time.time() if now is None else now,
              "recorded_by": "switchyard-privileged-helper adopt-conversation"}
    fd = _open_root(root, owner, create=True, anchor=anchor)
    tmp = f".{name}.{os.getpid()}.tmp"
    try:
        file_fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644, dir_fd=fd)
        try:
            with os.fdopen(file_fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o644)
                handle.write(json.dumps(record, sort_keys=True) + "\n")
            try:
                os.link(tmp, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
            except FileExistsError:
                raise RecordError(f"{root / name} exists already: withdraw that adoption first") from None
        finally:
            try:
                os.unlink(tmp, dir_fd=fd)
            except FileNotFoundError:
                pass
        return root / name
    finally:
        os.close(fd)


def remove(project: str, role: str, session_id: str, *, root: Path | None = None, owner_uid: int | None = None,
           anchor: Path | None = None) -> Path:
    """Remove this project's adoption of the conversation for this role, and nothing else; the path removed."""
    root, owner = root or ROOT, _owner(owner_uid)
    name = _name(session_id)
    fd = _open_root(root, owner, create=False, anchor=anchor)
    try:
        try:
            record = _load(fd, name, str(root / name), owner)
        except FileNotFoundError:
            raise RecordError(f"there is no adoption of {session_id} ({root / name})") from None
        if (record.get("project"), record.get("role")) != (project, role):
            raise RecordError(f"{root / name} is {record.get('project')}'s adoption for {record.get('role')}, not "
                              f"{project}'s for {role}")
        os.unlink(name, dir_fd=fd)
        return root / name
    finally:
        os.close(fd)


def tenant_account(project: str, *, registry: Path | None = None, owner_uid: int | None = None,
                   anchor: Path | None = None) -> str:
    """The account root's registry records for this tenant (`board_ui.tenant_user`); never the tenant's own say."""
    registry, owner = registry or REGISTRY, _owner(owner_uid)
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", project or ""):
        raise RecordError(f"{project!r} is not a project")
    fd = _open_root(registry, owner, create=False, anchor=anchor)
    try:
        try:
            entry = _load_json(fd, f"{project}.json", str(registry / f"{project}.json"), owner)
        except FileNotFoundError:
            raise RecordError(f"{project} is not registered on this host") from None
    finally:
        os.close(fd)
    tenant = (entry.get("board_ui") or {}).get("tenant_user") if isinstance(entry, dict) else None
    if not isinstance(tenant, str) or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", tenant) or tenant == "root":
        raise RecordError(f"root's registry records no tenant account for {project}")
    return tenant


def plan_argv(tenant: str, home: str, project: str, role: str, session_id: str) -> list[str]:
    """The tenant-side plan, as the tenant, in a clean environment, from the shared release's own wrapper."""
    return [RUNUSER, "-u", tenant, "--", "/usr/bin/env", "-i", f"HOME={home}", "PATH=/usr/bin:/bin",
            "LANG=C.UTF-8", f"USER={tenant}", f"LOGNAME={tenant}", PYTHON, "-I", "-B", str(WRAPPER),
            "adopt-conversation", project, role, session_id, "--plan-json"]


def plan_as_tenant(project: str, role: str, session_id: str, *, tenant: str) -> dict[str, Any]:
    import pwd
    import subprocess

    home = pwd.getpwnam(tenant).pw_dir
    try:
        done = subprocess.run(plan_argv(tenant, home, project, role, session_id), capture_output=True, text=True,
                              timeout=PLAN_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL, cwd="/")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RecordError(f"the plan could not be computed as {tenant}: {exc}") from None
    lines = [line for line in done.stdout.splitlines() if line.startswith("{")]
    try:
        plan = json.loads(lines[-1][:MAX_BYTES]) if lines else None
    except json.JSONDecodeError:
        plan = None
    if not isinstance(plan, dict):
        raise RecordError(f"the plan computed as {tenant} said nothing usable (exit {done.returncode})")
    return plan


def record_from_plan(project: str, role: str, session_id: str, digest: str, *, plan: dict[str, Any],
                     **where: Any) -> Path:
    """Root's record of exactly this plan: refused unless it is the confirmed one, clean, and for what was asked."""
    if plan.get("schema") != PLAN_SCHEMA:
        raise RecordError("the plan is not an adoption plan")
    asked = {"project": project, "role": role, "session_id": session_id}
    planned = {key: plan.get(key) for key in asked}
    if planned != asked:
        raise RecordError(f"the plan is for {planned}, not {asked}")
    if plan.get("problems"):
        raise RecordError("the plan refuses this adoption: " + "; ".join(str(p) for p in plan["problems"]))
    if plan.get("digest") != digest:
        raise RecordError(f"the plan's digest is {plan.get('digest')}, not the confirmed {digest}: something "
                          "changed since the preview")
    values = plan.get("values") if isinstance(plan.get("values"), dict) else {}
    return write({**{key: str(values.get(key) or "") for key in FIELDS}, **asked, "plan_digest": digest}, **where)


def main(argv: Sequence[str]) -> int:
    """The catalogued actions' root operations:

    `conversation-adoption-record record <project> <role> <session> <confirmed plan digest>`
    `conversation-adoption-record withdraw <project> <role> <session>`
    """
    argv = list(argv)
    if os.geteuid() != 0:
        print("switchyard: conversation-adoption-record is root's, run by the privileged helper", file=sys.stderr)
        return 1
    try:
        if argv[:1] == ["record"] and len(argv) == 5:
            project, role, session_id, digest = argv[1:]
            plan = plan_as_tenant(project, role, session_id, tenant=tenant_account(project))
            path = record_from_plan(project, role, session_id, digest, plan=plan)
            print(f"switchyard: recorded {path}")
            return 0
        if argv[:1] == ["withdraw"] and len(argv) == 4:
            path = remove(*argv[1:])
            print(f"switchyard: removed {path}")
            return 0
    except (RecordError, OSError, KeyError) as exc:
        print(f"switchyard: {exc}", file=sys.stderr)
        return 1
    print("usage: switchyard conversation-adoption-record record <project> <role> <session> <digest> | "
          "withdraw <project> <role> <session>", file=sys.stderr)
    return 2
