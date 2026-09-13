"""The rules every privileged Switchyard publication operation shares.

Two root-owned programs use this: `switchyard-publish-ref`, which publishes one
implementer's ref from a board request, and `switchyard-integrate-main`, which
fast-forwards the project's integration branch to a commit the control role
prepared. They hold the only push credential on the host between them, so the
questions they must answer identically live here rather than in two copies that
can drift apart:

* WHO is asking -- the kernel-observed pane process, matched against the live
  runtime the board registered for the role holding control authority, with no
  fallback to any role name;
* WHERE everything comes from -- the root-owned project registry, the root-owned
  board unit, and the root-owned publish grant, never the caller's arguments or
  a checkout the project account can rewrite;
* WITH WHAT -- a credential root owns and the project account cannot read.

Nothing here pushes anything. Each program decides what it is willing to move
and says so itself.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import shlex
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import NoReturn

PROTECTED_REFS = frozenset({"main", "master", "trunk", "release", "head"})
PROJECT_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
REF_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
PROJECT_REGISTRY_DIR = Path("/etc/switchyard/projects")
PROJECT_REGISTRY_SCHEMA = "switchyard.project-registry.v1"
REGISTRY_TEST_ROOT_ENV = "SWITCHYARD_PUBLISH_REGISTRY_ROOT"
#: Root-owned, and the only place a push credential is named.
PUBLISH_GRANT_DIR = Path("/etc/switchyard/publish")
PUBLISH_GRANT_SCHEMA = "switchyard.publish-grant.v1"
PUBLISH_GRANT_TEST_DIR_ENV = "SWITCHYARD_PUBLISH_GRANT_DIR"
#: Root-owned staging. Readable by everyone, writable by root: the project
#: account refreshes its cache from it and cannot alter what it mirrors.
STAGING_ROOT = Path("/var/lib/switchyard/publish")
STAGING_TEST_ROOT_ENV = "SWITCHYARD_PUBLISH_STAGING_ROOT"
BOARD_UNIT_DIR = Path("/etc/systemd/system")
BOARD_UNIT_TEST_DIR_ENV = "SWITCHYARD_PUBLISH_UNIT_DIR"
COMMIT_CACHE_ENVIRONMENT_KEY = "TICKET_BOARD_COMMIT_GIT_DIR"
PROC_ROOT_ENV = "SWITCHYARD_PUBLISH_PROC_ROOT"
#: Test-only redirections for the two locations that are otherwise derived from
#: the real host: the owner's home, and (above) the kernel's process table.
OWNER_TEST_HOME_ENV = "SWITCHYARD_PUBLISH_OWNER_HOME"
PUBLIC_REF_ATTEMPTS = 5
CACHE_FETCH_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 1.0
MAX_ANCESTRY_DEPTH = 128


#: The program speaking. Each entry point names itself, so a refusal says which
#: operation refused rather than which module the rule happens to live in.
PROGRAM = "switchyard-publish-ref"


def speaking_as(program: str) -> None:
    global PROGRAM
    PROGRAM = program


def fail(message: str) -> NoReturn:
    raise SystemExit(f"{PROGRAM}: {message}")


# --------------------------------------------------------------------------
# Who is asking. Kernel-backed, mirroring ticket_board/peer_identity.py, which
# is how the board itself decides which role a local connection belongs to.
# --------------------------------------------------------------------------


def proc_root() -> Path:
    override = os.environ.get(PROC_ROOT_ENV, "").strip()
    return Path(override) if override else Path("/proc")


def read_process(pid: int, *, root: Path | None = None) -> dict | None:
    base = root or proc_root()
    if pid <= 0:
        return None
    try:
        raw = (base / str(pid) / "stat").read_text(encoding="utf-8")
        close = raw.rindex(")")
        open_ = raw.index("(")
        fields = raw[close + 2 :].split()
        return {
            "pid": pid,
            "ppid": int(fields[1]),
            "start_time": int(fields[19]),
            "comm": raw[open_ + 1 : close],
        }
    except (OSError, ValueError, IndexError):
        return None


def process_uid(pid: int, *, root: Path | None = None) -> int | None:
    base = root or proc_root()
    try:
        for line in (base / str(pid) / "status").read_text(encoding="utf-8").splitlines():
            if line.startswith("Uid:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def pane_identity(pid: int, *, root: Path | None = None) -> tuple[int, int, int] | None:
    """The pane-root process this program is running under, or nothing.

    sudo does not disturb this: it is one more process on the same chain, and
    the chain still ends at the pane the launcher started. A caller that
    detaches itself from its pane has no identity here, and is refused rather
    than guessed at.
    """
    base = root or proc_root()
    seen: set[int] = set()
    current = pid
    for _ in range(MAX_ANCESTRY_DEPTH):
        if current <= 1 or current in seen:
            return None
        seen.add(current)
        child = read_process(current, root=base)
        if child is None:
            return None
        parent = read_process(child["ppid"], root=base)
        if parent is not None and parent["comm"].startswith("tmux"):
            uid = process_uid(child["pid"], root=base)
            if uid is None:
                return None
            return (child["pid"], child["start_time"], uid)
        current = child["ppid"]
    return None


# --------------------------------------------------------------------------
# Root-owned data: the registry, the board unit, the push grant.
# --------------------------------------------------------------------------


def validate_project(project: str) -> str:
    name = project.strip()
    if not PROJECT_SLUG.fullmatch(name):
        fail(f"{project!r} is not a valid project name")
    return name


def _contained(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _read_trusted_json(
    path: Path,
    *,
    description: str,
    expected_uid: int,
    expected_gid: int | None = None,
    reject_group_write: bool = False,
) -> dict:
    try:
        info = path.lstat()
    except (OSError, ValueError) as exc:
        fail(f"cannot read {description} {path}: {getattr(exc, 'strerror', None) or exc}")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        fail(f"{description} {path} must be a regular file, not a link")
    if info.st_uid != expected_uid:
        fail(f"{description} {path} is not owned by the trusted account")
    if expected_gid is not None and info.st_gid != expected_gid:
        fail(f"{description} {path} is not in the trusted account group")
    forbidden_write = stat.S_IWOTH | (stat.S_IWGRP if reject_group_write else 0)
    if info.st_mode & forbidden_write:
        fail(f"{description} {path} is writable by an untrusted account")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read {description} {path}: {exc}")
    if not isinstance(document, dict):
        fail(f"{description} {path} is not a JSON object")
    return document


def _require_trusted_owner_path(
    path: Path,
    *,
    projects_root: Path,
    owner_uid: int,
    owner_gid: int,
    directory: bool = False,
) -> Path:
    """A path the project owner controls, reached only through owner or root."""
    if not path.is_absolute():
        fail("registered project configuration path must be absolute")
    lexical_root = Path(os.path.abspath(projects_root))
    lexical_path = Path(os.path.abspath(path))
    try:
        lexical_path.relative_to(lexical_root)
    except ValueError:
        fail(f"registered project configuration escapes {projects_root}: {path}")
    try:
        original = lexical_path.lstat()
    except (OSError, ValueError) as exc:
        fail(
            f"registered project configuration is stale: {lexical_path}: "
            f"{getattr(exc, 'strerror', None) or exc}"
        )
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if stat.S_ISLNK(original.st_mode) or not expected_type(original.st_mode):
        kind = "directory" if directory else "regular file"
        fail(f"registered project path {lexical_path} must be a real {kind}, not a link")
    try:
        resolved = lexical_path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        fail(f"registered project configuration is stale: {path}: {exc}")
    if not _contained(resolved, projects_root):
        fail(f"registered project configuration escapes {projects_root}: {path}")
    current = lexical_path if directory else lexical_path.parent
    while True:
        try:
            info = current.lstat()
        except (OSError, ValueError) as exc:
            fail(
                f"cannot inspect registered project path {current}: "
                f"{getattr(exc, 'strerror', None) or exc}"
            )
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            fail(f"registered project path component {current} is not a real directory")
        if info.st_uid not in {0, owner_uid}:
            fail(f"registered project path component {current} has an untrusted owner")
        if info.st_mode & stat.S_IWOTH:
            fail(f"registered project path component {current} is world-writable")
        if info.st_mode & stat.S_IWGRP and info.st_gid != owner_gid:
            fail(f"registered project path component {current} has an untrusted writable group")
        if current == lexical_root:
            break
        current = current.parent
    return resolved


def registry_root() -> tuple[Path, int]:
    override = os.environ.get(REGISTRY_TEST_ROOT_ENV, "").strip()
    if override:
        return Path(override), os.geteuid()
    return PROJECT_REGISTRY_DIR, 0


def load_project(project: str) -> dict:
    """The project's own configuration, found without trusting the caller.

    The owner is not this program's euid any more -- this runs as root -- so it
    is taken from the configuration file the ROOT-OWNED registry points at. The
    registry decides which file; the file's owner decides which account the rest
    of this trusts.
    """
    root, trusted_uid = registry_root()
    try:
        info = root.lstat()
    except OSError as exc:
        fail(f"cannot inspect project registry {root}: {exc.strerror}")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        fail(f"project registry {root} must be a real directory")
    if info.st_uid != trusted_uid:
        fail(f"project registry {root} is not owned by the trusted account")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        fail(f"project registry {root} is writable by an untrusted account")
    entry_path = root / f"{project}.json"
    if not _contained(entry_path, root):
        fail(f"registry path escapes {root}")
    if not entry_path.exists():
        fail(f"no registered configuration found for project {project}")
    registry = _read_trusted_json(
        entry_path,
        description="project registry entry",
        expected_uid=trusted_uid,
        reject_group_write=True,
    )
    if str(registry.get("schema") or "") != PROJECT_REGISTRY_SCHEMA:
        fail(f"project registry entry {entry_path} has an unsupported schema")
    if str(registry.get("slug") or "") != project:
        fail(f"project registry entry {entry_path} is not for project {project}")
    raw_config_path = registry.get("config_path")
    if not isinstance(raw_config_path, str) or not raw_config_path.strip():
        fail(f"project registry entry {entry_path} has no config_path")
    candidate = Path(raw_config_path)
    try:
        config_info = candidate.lstat()
    except OSError as exc:
        fail(f"registered project configuration is stale: {candidate}: {exc.strerror}")
    if stat.S_ISLNK(config_info.st_mode) or not stat.S_ISREG(config_info.st_mode):
        fail(f"registered project configuration {candidate} must be a regular file, not a link")
    try:
        owner = pwd.getpwuid(config_info.st_uid)
    except KeyError:
        fail(f"registered project configuration {candidate} has no local owner account")
    owner_home = os.environ.get(OWNER_TEST_HOME_ENV, "").strip() or owner.pw_dir
    projects_root = Path(owner_home) / "Projects"
    config_path = _require_trusted_owner_path(
        candidate,
        projects_root=projects_root,
        owner_uid=owner.pw_uid,
        owner_gid=owner.pw_gid,
    )
    document = _read_trusted_json(
        config_path,
        description="project configuration",
        expected_uid=owner.pw_uid,
        expected_gid=owner.pw_gid,
    )
    if str(document.get("project") or "") != project:
        fail(f"{config_path} is not the configuration for project {project}")
    repository = document.get("repository")
    if not isinstance(repository, str) or not repository.strip():
        fail(f"project configuration {config_path} has no owner repository")
    document["repository"] = str(
        _require_trusted_owner_path(
            Path(repository),
            projects_root=projects_root,
            owner_uid=owner.pw_uid,
            owner_gid=owner.pw_gid,
            directory=True,
        )
    )
    document["owner_uid"] = owner.pw_uid
    document["owner_gid"] = owner.pw_gid
    document["owner_user"] = owner.pw_name
    return document


def board_unit(project: str) -> dict:
    """What the tenant's own board unit says: its cache, and where to read it.

    Root owns the unit, so a role cannot choose which board this asks or which
    cache it refreshes. The project configuration names both too, and it is
    owner-writable -- which under one shared account means role-writable.
    """
    override = os.environ.get(BOARD_UNIT_TEST_DIR_ENV, "").strip()
    directory = Path(override) if override else BOARD_UNIT_DIR
    trusted_uid = os.geteuid() if override else 0
    unit_path = directory / f"{project}-ticket-board.service"
    if not _contained(unit_path, directory):
        fail(f"board unit path escapes {directory}")
    try:
        info = unit_path.lstat()
    except OSError as exc:
        fail(f"cannot read board unit {unit_path}: {exc.strerror}")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        fail(f"board unit {unit_path} must be a regular file, not a link")
    if info.st_uid != trusted_uid:
        fail(f"board unit {unit_path} is not owned by the trusted account")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        fail(f"board unit {unit_path} is writable by an untrusted account")
    try:
        lines = unit_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        fail(f"cannot read board unit {unit_path}: {exc.strerror}")

    environment: dict[str, str] = {}
    exec_start = ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("Environment="):
            assignment = stripped[len("Environment=") :].strip().strip('"')
            key, separator, value = assignment.partition("=")
            if separator:
                environment[key.strip()] = value.strip().strip('"')
        elif stripped.startswith("ExecStart="):
            exec_start = stripped[len("ExecStart=") :].strip()
    cache = environment.get(COMMIT_CACHE_ENVIRONMENT_KEY, "").strip()
    if not cache:
        fail(
            f"board unit {unit_path} names no {COMMIT_CACHE_ENVIRONMENT_KEY}; "
            "the trusted commit cache is not configured for this tenant"
        )
    host, port = "127.0.0.1", ""
    try:
        argv = shlex.split(exec_start)
    except ValueError:
        argv = []
    for index, token in enumerate(argv):
        if token == "--host" and index + 1 < len(argv):
            host = argv[index + 1]
        elif token == "--port" and index + 1 < len(argv):
            port = argv[index + 1]
    if not port.isdigit():
        fail(f"board unit {unit_path} does not say which port its board reads on")
    return {
        "unit_path": str(unit_path),
        "commit_cache": cache,
        "board_url": f"http://{host}:{port}",
    }


def publish_grant(project: str) -> dict:
    """The push credential. Root-owned, and the project account cannot read it."""
    override = os.environ.get(PUBLISH_GRANT_TEST_DIR_ENV, "").strip()
    directory = Path(override) if override else PUBLISH_GRANT_DIR
    trusted_uid = os.geteuid() if override else 0
    grant_path = directory / f"{project}.json"
    if not _contained(grant_path, directory):
        fail(f"publish grant path escapes {directory}")
    if not grant_path.exists():
        fail(
            f"no publish grant for {project} at {grant_path}; the control role has no push "
            "credential installed, and nothing else on this host does either"
        )
    grant = _read_trusted_json(
        grant_path,
        description="publish grant",
        expected_uid=trusted_uid,
        reject_group_write=True,
    )
    if str(grant.get("schema") or "") != PUBLISH_GRANT_SCHEMA:
        fail(f"publish grant {grant_path} has an unsupported schema")
    identity = str(grant.get("identity_file") or "").strip()
    if not identity:
        fail(f"publish grant {grant_path} names no identity_file")
    identity_path = Path(identity)
    try:
        info = identity_path.lstat()
    except OSError as exc:
        fail(f"cannot read publish identity {identity_path}: {exc.strerror}")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        fail(f"publish identity {identity_path} must be a regular file, not a link")
    if info.st_uid != trusted_uid:
        fail(f"publish identity {identity_path} is not owned by the trusted account")
    # The whole point: a key any role can read is a key every role can push
    # with. This one is readable by root and nobody else.
    if info.st_mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH):
        fail(
            f"publish identity {identity_path} is readable or writable outside the trusted "
            "account; a credential the project account can read is a credential every role has"
        )
    grant["identity_file"] = str(identity_path)
    known_hosts = str(grant.get("known_hosts") or "").strip()
    if known_hosts:
        grant["known_hosts"] = str(Path(known_hosts))
    return grant


# --------------------------------------------------------------------------
# What the board says.
# --------------------------------------------------------------------------


def board_get(url: str, path: str, *, allow_missing: bool = False) -> dict | None:
    try:
        with urllib.request.urlopen(url + path, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if allow_missing and exc.code == 404:
            return None
        fail(f"cannot read {path} from the board at {url}: {exc}")
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read {path} from the board at {url}: {exc}")


def control_role_of(document: dict) -> str:
    """The role that integrates, by capability rather than by name (SYRD-49).

    Empty when the document declares no active role holding control authority,
    and the caller refuses. There is deliberately no fallback to the name
    `director`: this program holds the only push credential on the host, so a
    document that is absent, empty or malformed would otherwise turn into
    "whatever process registered itself under a familiar name may push", which
    is the authority this whole ticket exists to take away.
    """
    roles = document.get("roles") if isinstance(document, dict) else None
    if isinstance(roles, list):
        for role in sorted(
            (r for r in roles if isinstance(r, dict)), key=lambda r: str(r.get("name") or "")
        ):
            capabilities = role.get("capabilities")
            if not isinstance(capabilities, list) or not role.get("active"):
                continue
            if {"set_manually_controlled", "merge"} <= set(capabilities):
                return str(role.get("name") or "")
    return ""


def require_control_caller(board_url: str) -> str:
    """Refuse anything but the live registered process of the control role."""
    workflow = board_get(board_url, "/api/workflow")
    document = workflow.get("document") if isinstance(workflow, dict) else None
    control_role = control_role_of(document if isinstance(document, dict) else {})
    if not control_role:
        fail(
            "this board declares no role with control authority, so nothing here may publish. "
            "A missing or malformed workflow document is not permission to push"
        )
    listing = board_get(board_url, f"/api/runtime-assignments/{control_role}", allow_missing=True)
    if listing is None:
        fail(
            f"the board has no registered runtime for {control_role}; its session is not running, "
            "so there is no process this publication could belong to"
        )
    if str(listing.get("authority_mode") or "") != "process":
        fail(
            "this board does not run on process authority; publication authority cannot be "
            "established from a shared uid alone"
        )
    assignment = listing.get("assignment")
    if not isinstance(assignment, dict):
        fail(f"the board has no registered runtime for {control_role}")
    caller = pane_identity(os.getpid())
    if caller is None:
        fail(
            "cannot establish which pane this was run from; publication must be run from the "
            "control role's own session, not detached from it"
        )
    registered = (
        int(assignment.get("process_pid") or 0),
        int(assignment.get("process_start_time") or 0),
        int(assignment.get("process_uid") or -1),
    )
    if caller != registered:
        fail(
            f"only {control_role}'s registered process may publish. This ran under process "
            f"{caller[0]} (started {caller[1]}, uid {caller[2]}) and the board registered "
            f"{registered[0]} (started {registered[1]}, uid {registered[2]})"
        )
    # The row can outlive the process it names; a pid is reused, a start time is
    # not, so this is what makes a dead or replaced session fail closed.
    live = read_process(caller[0])
    if live is None or live["start_time"] != caller[1]:
        fail(f"the registered {control_role} process is no longer running")
    return control_role


# --------------------------------------------------------------------------
# Doing the work.
# --------------------------------------------------------------------------


def git(args: list[str], *, ssh_command: str = "", **kwargs) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }
    if ssh_command:
        env["GIT_SSH_COMMAND"] = ssh_command
    else:
        env.pop("GIT_SSH_COMMAND", None)
    return subprocess.run(["git", *args], text=True, capture_output=True, env=env, **kwargs)


def ssh_command_for(grant: dict) -> str:
    parts = [
        "ssh",
        "-i",
        shlex.quote(str(grant["identity_file"])),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "IdentityAgent=none",
        "-o",
        "BatchMode=yes",
    ]
    known_hosts = str(grant.get("known_hosts") or "").strip()
    if known_hosts:
        parts += ["-o", f"UserKnownHostsFile={shlex.quote(known_hosts)}", "-o", "StrictHostKeyChecking=yes"]
    return " ".join(parts)


def remote_url_for(grant: dict) -> str:
    """Where to push, from root-owned data and nowhere else.

    Not from the project checkout. Under one shared account the checkout's
    configuration is role-writable, so a role that could name the remote could
    aim a push -- carrying the project's private history -- at a server of its
    own. The grant pins it, and a grant that pins nothing publishes nothing.
    """
    pinned = str(grant.get("remote") or "").strip()
    if not pinned:
        fail(
            "the publish grant names no remote; pin the destination in the root-owned grant "
            "rather than reading it from a checkout the project account can rewrite"
        )
    if pinned.startswith("ext::") or pinned.startswith("-"):
        fail(f"publish grant remote {pinned!r} would run a command rather than name a repository")
    return pinned


def staging_repository(project: str) -> Path:
    override = os.environ.get(STAGING_TEST_ROOT_ENV, "").strip()
    root = Path(override) if override else STAGING_ROOT
    root.mkdir(parents=True, exist_ok=True)
    # Readable, not writable: the project account refreshes its cache from this
    # mirror and cannot change what the mirror says.
    os.chmod(root, 0o755)
    staging = root / f"{project}.git"
    if not (staging / "HEAD").exists():
        created = git(["init", "--bare", "-q", str(staging)])
        if created.returncode != 0:
            sys.stderr.write(created.stderr)
            fail("could not create the publish staging repository")
    os.chmod(staging, 0o755)
    return staging


def require_owner_bundle(path: Path, owner_uid: int) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        fail(f"cannot read bundle {path}: {exc.strerror}")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        fail(f"bundle {path} must be a regular file, not a link")
    if info.st_uid != owner_uid:
        fail(f"bundle {path} is not owned by the project account")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        fail(f"bundle {path} is writable by an account outside the project")


def refresh_commit_cache(
    cache: Path,
    staging: Path,
    ref: str,
    expected_commit: str,
    *,
    owner_uid: int,
    owner_gid: int,
    sleep=time.sleep,
) -> str | None:
    """Fetch the published ref into the tenant's trusted cache, as the tenant.

    From the local mirror, not the remote: a local fetch needs no credential, so
    refreshing the cache never puts one inside the project account, and the
    objects it writes belong to the account that owns the cache rather than to
    root.
    """
    problem = "the cache was never fetched"
    for attempt in range(CACHE_FETCH_ATTEMPTS):
        if attempt:
            sleep(RETRY_DELAY_SECONDS)
        fetched = run_as(
            owner_uid,
            owner_gid,
            [
                "git",
                "--git-dir",
                str(cache),
                "fetch",
                str(staging),
                f"+refs/heads/{ref}:refs/remotes/origin/{ref}",
            ],
        )
        if fetched.returncode != 0:
            problem = (fetched.stderr or fetched.stdout or "git fetch failed").strip()
            continue
        resolved = run_as(
            owner_uid,
            owner_gid,
            ["git", "--git-dir", str(cache), "rev-parse", f"refs/remotes/origin/{ref}^{{commit}}"],
        )
        if resolved.returncode != 0:
            problem = (resolved.stderr or "cache does not resolve the fetched ref").strip()
            continue
        landed = resolved.stdout.strip()
        if landed != expected_commit:
            return (
                f"the trusted cache resolved {ref} to {landed}, not {expected_commit}; "
                "the public ref changed after this publication"
            )
        return None
    return f"could not refresh the trusted commit cache {cache}: {problem}"


def run_as(uid: int, gid: int, argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run one command as the project account, dropping every privilege first."""

    def drop() -> None:
        os.setgid(gid)
        os.setgroups([gid])
        os.setuid(uid)

    env = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": pwd.getpwuid(uid).pw_dir,
    }
    preexec = drop if os.geteuid() == 0 else None
    return subprocess.run(argv, text=True, capture_output=True, env=env, preexec_fn=preexec)


def remote_ref_commit(remote_url: str, ref: str, ssh_command: str) -> str | None:
    listed = git(["ls-remote", remote_url, f"refs/heads/{ref}"], ssh_command=ssh_command)
    if listed.returncode != 0:
        return None
    for line in listed.stdout.splitlines():
        commit, _, name = line.partition("\t")
        if name.strip() == f"refs/heads/{ref}":
            return commit.strip()
    return ""
