"""What the board supervisor runs for one tenant, read from root's own unit for that board (SYRD-586).

A supervised tenant keeps its reviewed system unit, `<project>-ticket-board.service`,
installed exactly as provisioning and the release review wrote it; it is
disabled, and a drop-in makes systemd refuse to start it while the tenant is
supervised. The supervisor reads that unit's `[Service]` section at EVERY start
and runs what it says: the same user and groups, working directory, runtime
directory, command line, environment and filesystem protections. So the unit
stays the one place a board's process is described, a release that changes it
is reviewed the way it always was, and rolling back is starting the unit again.

Anything this reader does not know how to honour is refused, not skipped: a
key it ignored would be a protection or a setting systemd applied and the
supervisor silently did not. Refusals name the tenant, the file and the line.

- `Paths` is every host location the supervisor reads and writes; tests build
  one over a scratch tree.
- `read_tenant_record` and `TenantRecord`: the root-owned file that makes a
  tenant supervised, and names the account allowed to control it.
- `read_unit_spec` and `ChildSpec`: the unit, parsed.
- `child_environment`: the environment one start gets, the way systemd builds it.
- `foreign_drop_ins`: drop-ins the supervisor would not apply, which refuse a start.
"""

from __future__ import annotations

import grp
import json
import os
import pwd
import re
import shlex
import stat
from dataclasses import dataclass, field
from pathlib import Path

SUPERVISOR_UNIT = "switchyard-board-supervisor.service"
DROP_IN_NAME = "50-switchyard-board-supervisor.conf"
TENANT_RECORD_SCHEMA = "switchyard.board-supervisor.tenant.v1"
#: The launcher's own slug rule (`team_launcher.PROJECT_SLUG_RE`), copied so the
#: root supervisor does not import the launcher; a test keeps the two equal.
PROJECT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,39}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
#: What systemd gives a system service before its own `Environment=` lines.
DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"
#: The locale a service inherits from the manager; the supervisor passes on its own.
INHERITED_LOCALE = ("LANG", "LANGUAGE", "LC_CTYPE", "LC_NUMERIC", "LC_TIME", "LC_COLLATE",
                    "LC_MONETARY", "LC_MESSAGES", "LC_ALL")
UNIT_LIMIT_BYTES = 256 * 1024
ENV_FILE_LIMIT_BYTES = 1024 * 1024
RECORD_LIMIT_BYTES = 64 * 1024
#: systemd's defaults when a unit does not say.
DEFAULT_RESTART_SEC = 0.1
DEFAULT_START_LIMIT_INTERVAL = 10.0
DEFAULT_START_LIMIT_BURST = 5
DEFAULT_STOP_TIMEOUT = 90.0

#: Keys whose meaning is honoured exactly. Everything else in [Unit] or
#: [Service] refuses the start.
UNIT_KEYS = frozenset({"Description", "Documentation", "After", "Wants", "StartLimitIntervalSec",
                       "StartLimitBurst"})
SERVICE_KEYS = frozenset({
    "Type", "User", "Group", "SupplementaryGroups", "WorkingDirectory", "RuntimeDirectory",
    "RuntimeDirectoryMode", "ExecStart", "Restart", "RestartSec", "EnvironmentFile", "Environment",
    "NoNewPrivileges", "ProtectSystem", "ProtectHome", "ReadWritePaths", "StandardOutput",
    "StandardError", "TimeoutStopSec",
})
TRUE_WORDS = frozenset({"1", "yes", "true", "on"})
FALSE_WORDS = frozenset({"0", "no", "false", "off"})


class SpecError(Exception):
    """A tenant the supervisor will not start, and exactly why."""


@dataclass(frozen=True)
class Paths:
    """Where the supervisor's records, the units and the runtime directories are."""

    config_root: Path = Path("/etc/switchyard/board-supervisor")
    state_root: Path = Path("/var/lib/switchyard/board-supervisor")
    runtime_root: Path = Path("/run/switchyard-board-supervisor")
    unit_dir: Path = Path("/etc/systemd/system")
    #: Every directory systemd reads a unit's drop-ins from.
    drop_in_dirs: tuple[Path, ...] = (
        Path("/etc/systemd/system"), Path("/run/systemd/system"), Path("/usr/local/lib/systemd/system"),
        Path("/usr/lib/systemd/system"), Path("/lib/systemd/system"),
    )
    #: Where a child's `RuntimeDirectory=` is created.
    run_root: Path = Path("/run")
    #: Who must own the records and the unit for them to be believed.
    owner_uid: int = 0

    def tenants_dir(self) -> Path:
        return self.config_root / "tenants"

    def tenant_record(self, project: str) -> Path:
        return self.tenants_dir() / f"{project}.json"

    def desired_path(self, project: str) -> Path:
        return self.state_root / f"{project}.desired"

    def control_socket(self) -> Path:
        return self.runtime_root / "control.sock"

    def status_path(self) -> Path:
        return self.runtime_root / "status.json"

    def unit_name(self, project: str) -> str:
        return f"{project}-ticket-board.service"

    def unit_path(self, project: str) -> Path:
        return self.unit_dir / self.unit_name(project)

    def drop_in_path(self, project: str) -> Path:
        return self.unit_dir / f"{self.unit_name(project)}.d" / DROP_IN_NAME


def valid_project(project: str) -> bool:
    return bool(PROJECT_SLUG_RE.fullmatch(project or ""))


def read_root_file(path: Path, *, limit: int, owner_uid: int, what: str) -> str:
    """A file root wrote, read without following a link, believed only if nobody else could write it."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError as exc:
        raise SpecError(f"{what} {path} cannot be opened: {exc.strerror or exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SpecError(f"{what} {path} is not a regular file")
        if info.st_uid != owner_uid:
            raise SpecError(f"{what} {path} is owned by uid {info.st_uid}, not {owner_uid}")
        if info.st_mode & 0o022:
            raise SpecError(f"{what} {path} is writable by its group or by others "
                            f"(mode {stat.S_IMODE(info.st_mode):04o})")
        if info.st_size > limit:
            raise SpecError(f"{what} {path} is larger than {limit} bytes")
        data = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SpecError(f"{what} {path} is not UTF-8") from exc


@dataclass(frozen=True)
class TenantRecord:
    project: str
    owner: str
    unit: str
    unit_was_enabled: bool
    unit_was_active: bool
    migrated_at: str

    def as_json(self) -> dict:
        return {
            "schema": TENANT_RECORD_SCHEMA, "project": self.project, "owner": self.owner,
            "unit": self.unit, "unit_was_enabled": self.unit_was_enabled,
            "unit_was_active": self.unit_was_active, "migrated_at": self.migrated_at,
        }


def read_tenant_record(project: str, paths: Paths) -> TenantRecord:
    if not valid_project(project):
        raise SpecError(f"{project!r} is not a project slug")
    path = paths.tenant_record(project)
    text = read_root_file(path, limit=RECORD_LIMIT_BYTES, owner_uid=paths.owner_uid, what="tenant record")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise SpecError(f"tenant record {path} is not JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != TENANT_RECORD_SCHEMA:
        raise SpecError(f"tenant record {path} is not a {TENANT_RECORD_SCHEMA} record")
    if data.get("project") != project or data.get("unit") != paths.unit_name(project):
        raise SpecError(f"tenant record {path} names project {data.get('project')!r} and unit "
                        f"{data.get('unit')!r}, not {project!r} and {paths.unit_name(project)!r}")
    owner = data.get("owner")
    if not isinstance(owner, str) or not owner:
        raise SpecError(f"tenant record {path} names no owner account")
    return TenantRecord(
        project=project, owner=owner, unit=paths.unit_name(project),
        unit_was_enabled=bool(data.get("unit_was_enabled")), unit_was_active=bool(data.get("unit_was_active")),
        migrated_at=str(data.get("migrated_at") or ""),
    )


def supervised_projects(paths: Paths) -> list[str]:
    """The projects with a tenant record, by name; whether each record holds is read later."""
    try:
        names = sorted(entry.name for entry in paths.tenants_dir().iterdir())
    except OSError:
        return []
    return [name[: -len(".json")] for name in names
            if name.endswith(".json") and valid_project(name[: -len(".json")])]


@dataclass(frozen=True)
class ChildSpec:
    """One tenant board's process, as its unit describes it."""

    project: str
    unit_path: Path
    description: str
    user: str
    uid: int
    gid: int
    groups: tuple[int, ...]
    home: str
    shell: str
    working_directory: str
    runtime_directory: str
    runtime_directory_mode: int
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    environment_files: tuple[tuple[str, bool], ...]
    read_write_paths: tuple[str, ...]
    protect_system: str
    protect_home: str
    no_new_privileges: bool
    restart_sec: float
    start_limit_interval: float
    start_limit_burst: int
    stop_timeout: float
    port: int | None = None
    socket: str = ""
    notes: tuple[str, ...] = field(default=())

    def runtime_path(self, paths: Paths) -> Path:
        return paths.run_root / self.runtime_directory

    def release(self) -> str:
        """The release the board runs: what its script path resolves through."""
        script = next((word for word in self.argv[1:] if word.endswith("/ticket-board.py")), "")
        return script[: -len("/scripts/ticket-board.py")] if script.endswith("/scripts/ticket-board.py") else script


def _unescape_specifiers(value: str, *, where: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "%":
            if value[index + 1: index + 2] == "%":
                out.append("%")
                index += 2
                continue
            raise SpecError(f"{where} uses the systemd specifier {value[index:index + 2]!r}, "
                            "which the supervisor does not expand")
        out.append(char)
        index += 1
    return "".join(out)


def parse_duration(value: str, *, where: str) -> float:
    """systemd time spans as these units write them: `30s`, `2`, `5min`, `1min 30s`."""
    units = {"": 1.0, "s": 1.0, "sec": 1.0, "second": 1.0, "seconds": 1.0, "ms": 0.001, "msec": 0.001,
             "m": 60.0, "min": 60.0, "minute": 60.0, "minutes": 60.0, "h": 3600.0, "hr": 3600.0,
             "hour": 3600.0, "hours": 3600.0}
    text = value.strip()
    if text in {"infinity"}:
        raise SpecError(f"{where}: an infinite duration is not supported")
    parts = re.findall(r"(\d+(?:\.\d+)?)\s*([a-z]*)", text)
    if not parts or re.sub(r"(\d+(?:\.\d+)?)\s*([a-z]*)", "", text).strip():
        raise SpecError(f"{where}: {value!r} is not a duration")
    total = 0.0
    for number, unit in parts:
        if unit not in units:
            raise SpecError(f"{where}: unknown time unit {unit!r}")
        total += float(number) * units[unit]
    return total


def _bool(value: str, *, where: str) -> bool:
    word = value.strip().lower()
    if word in TRUE_WORDS:
        return True
    if word in FALSE_WORDS:
        return False
    raise SpecError(f"{where}: {value!r} is not a boolean")


def _logical_lines(text: str) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    pending, start = "", 0
    for number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not pending and (not stripped or stripped[0] in "#;"):
            continue
        if stripped.endswith("\\"):
            pending, start = pending + stripped[:-1] + " ", start or number
            continue
        lines.append((start or number, pending + stripped))
        pending, start = "", 0
    if pending:
        lines.append((start, pending))
    return lines


def parse_unit(text: str, *, where: str) -> dict[str, list[tuple[int, str, str]]]:
    """Section -> [(line, key, value)], keeping repeated keys in order."""
    sections: dict[str, list[tuple[int, str, str]]] = {}
    section = ""
    for number, line in _logical_lines(text):
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            sections.setdefault(section, [])
            continue
        key, separator, value = line.partition("=")
        if not separator or not section:
            raise SpecError(f"{where}:{number}: cannot read {line!r}")
        sections[section].append((number, key.strip(), value.strip()))
    return sections


def _groups_for(user: pwd.struct_passwd, gid: int, supplementary: list[str], *, where: str) -> tuple[int, ...]:
    """systemd's: the user's own memberships (`initgroups`), then `SupplementaryGroups=`."""
    groups = list(os.getgrouplist(user.pw_name, gid))
    for name in supplementary:
        try:
            extra = grp.getgrnam(name).gr_gid
        except KeyError as exc:
            raise SpecError(f"{where}: SupplementaryGroups names unknown group {name!r}") from exc
        if extra not in groups:
            groups.append(extra)
    return tuple(groups)


def read_unit_spec(project: str, paths: Paths) -> ChildSpec:
    """The tenant's reviewed unit, as the child the supervisor would start. Refuses what it cannot honour."""
    if not valid_project(project):
        raise SpecError(f"{project!r} is not a project slug")
    unit_path = paths.unit_path(project)
    text = read_root_file(unit_path, limit=UNIT_LIMIT_BYTES, owner_uid=paths.owner_uid, what="unit")
    where = str(unit_path)
    sections = parse_unit(text, where=where)
    unknown_sections = set(sections) - {"Unit", "Service", "Install"}
    if unknown_sections:
        raise SpecError(f"{where}: sections {sorted(unknown_sections)} are not supported")
    unit: dict[str, list[tuple[int, str]]] = {}
    service: dict[str, list[tuple[int, str]]] = {}
    for target, allowed, name in ((unit, UNIT_KEYS, "Unit"), (service, SERVICE_KEYS, "Service")):
        for number, key, value in sections.get(name, []):
            if key not in allowed:
                raise SpecError(f"{where}:{number}: [{name}] {key}= is not something the supervisor "
                                "applies, so it will not start this board")
            target.setdefault(key, []).append((number, _unescape_specifiers(value, where=f"{where}:{number}")))

    def one(key: str, default: str | None = None) -> str:
        values = service.get(key, [])
        if len(values) > 1:
            raise SpecError(f"{where}:{values[-1][0]}: {key}= is given more than once")
        if not values:
            if default is None:
                raise SpecError(f"{where}: {key}= is required")
            return default
        return values[0][1]

    def unit_one(key: str) -> str:
        values = unit.get(key, [])
        return values[-1][1] if values else ""

    if one("Type", "simple") != "simple":
        raise SpecError(f"{where}: only Type=simple is supported")
    for key in ("StandardOutput", "StandardError"):
        if one(key, "journal") not in {"journal", "inherit"}:
            raise SpecError(f"{where}: {key}= must be journal; the supervisor writes each board's output "
                            "to its own journal, tagged with the tenant")
    user_name = one("User")
    try:
        user = pwd.getpwnam(user_name)
    except KeyError as exc:
        raise SpecError(f"{where}: User={user_name} is not an account on this host") from exc
    if user.pw_uid == 0:
        raise SpecError(f"{where}: User={user_name} is root; the supervisor never runs a board as root")
    group_name = one("Group", "")
    if group_name:
        try:
            gid = grp.getgrnam(group_name).gr_gid
        except KeyError as exc:
            raise SpecError(f"{where}: Group={group_name} is not a group on this host") from exc
    else:
        gid = user.pw_gid
    supplementary = [word for _, value in service.get("SupplementaryGroups", []) for word in value.split()]
    groups = _groups_for(user, gid, supplementary, where=where)

    exec_line = one("ExecStart")
    if exec_line[:1] in {"-", "@", "+", "!", ":"}:
        raise SpecError(f"{where}: ExecStart= prefixes are not supported")
    if "$" in exec_line:
        raise SpecError(f"{where}: ExecStart= variable expansion is not supported")
    try:
        argv = tuple(shlex.split(exec_line))
    except ValueError as exc:
        raise SpecError(f"{where}: ExecStart= cannot be split: {exc}") from exc
    if not argv or not argv[0].startswith("/"):
        raise SpecError(f"{where}: ExecStart= must name an absolute program")

    environment: list[tuple[str, str]] = []
    for number, value in service.get("Environment", []):
        try:
            words = shlex.split(value)
        except ValueError as exc:
            raise SpecError(f"{where}:{number}: Environment= cannot be split: {exc}") from exc
        for word in words:
            name, separator, item = word.partition("=")
            if not separator or not ENV_NAME_RE.fullmatch(name):
                raise SpecError(f"{where}:{number}: {word!r} is not NAME=VALUE")
            environment.append((name, item))
    environment_files = []
    for number, value in service.get("EnvironmentFile", []):
        optional = value.startswith("-")
        path = value[1:] if optional else value
        if not path.startswith("/"):
            raise SpecError(f"{where}:{number}: EnvironmentFile= must be absolute")
        environment_files.append((path, optional))
    read_write = []
    for number, value in service.get("ReadWritePaths", []):
        for word in value.split():
            if word.startswith("-") or not word.startswith("/"):
                raise SpecError(f"{where}:{number}: ReadWritePaths= entry {word!r} must be an absolute path")
            read_write.append(word)

    protect_system = one("ProtectSystem", "no").lower()
    protect_system = {"true": "yes", "1": "yes", "on": "yes", "false": "no", "0": "no", "off": "no"}.get(
        protect_system, protect_system)
    if protect_system not in {"no", "yes", "full"}:
        raise SpecError(f"{where}: ProtectSystem={protect_system} is not supported")
    protect_home = one("ProtectHome", "no").lower()
    if protect_home in FALSE_WORDS:
        protect_home = "no"
    if protect_home not in {"no", "read-only"}:
        raise SpecError(f"{where}: ProtectHome={protect_home} is not supported (read-only is)")
    no_new_privileges = _bool(one("NoNewPrivileges", "no"), where=f"{where}: NoNewPrivileges")

    runtime_directory = one("RuntimeDirectory", "")
    if runtime_directory and (not re.fullmatch(r"[A-Za-z0-9_.-]+", runtime_directory)
                              or runtime_directory in {".", ".."}):
        raise SpecError(f"{where}: RuntimeDirectory={runtime_directory} must be one directory name")
    mode_text = one("RuntimeDirectoryMode", "0755")
    try:
        runtime_mode = int(mode_text, 8)
    except ValueError as exc:
        raise SpecError(f"{where}: RuntimeDirectoryMode={mode_text} is not an octal mode") from exc
    burst_text = unit_one("StartLimitBurst")
    try:
        burst = int(burst_text) if burst_text else DEFAULT_START_LIMIT_BURST
    except ValueError as exc:
        raise SpecError(f"{where}: StartLimitBurst={burst_text} is not a number") from exc
    interval_text = unit_one("StartLimitIntervalSec")
    notes = []
    restart = one("Restart", "no")
    if restart != "always":
        notes.append(f"Restart={restart} is superseded: the supervisor restarts every exit it did not ask for")
    port = socket_path = None
    for index, word in enumerate(argv[:-1]):
        if word == "--port":
            try:
                port = int(argv[index + 1])
            except ValueError:
                port = None
        elif word == "--unix-socket":
            socket_path = argv[index + 1]
    return ChildSpec(
        project=project, unit_path=unit_path, description=unit_one("Description"),
        user=user.pw_name, uid=user.pw_uid, gid=gid, groups=groups, home=user.pw_dir, shell=user.pw_shell,
        working_directory=one("WorkingDirectory", "/"), runtime_directory=runtime_directory,
        runtime_directory_mode=runtime_mode, argv=argv, environment=tuple(environment),
        environment_files=tuple(environment_files), read_write_paths=tuple(read_write),
        protect_system=protect_system, protect_home=protect_home, no_new_privileges=no_new_privileges,
        restart_sec=parse_duration(one("RestartSec", str(DEFAULT_RESTART_SEC)), where=f"{where}: RestartSec"),
        start_limit_interval=(parse_duration(interval_text, where=f"{where}: StartLimitIntervalSec")
                              if interval_text else DEFAULT_START_LIMIT_INTERVAL),
        start_limit_burst=burst,
        stop_timeout=parse_duration(one("TimeoutStopSec", str(DEFAULT_STOP_TIMEOUT)), where=f"{where}: TimeoutStopSec"),
        port=port, socket=socket_path or "", notes=tuple(notes),
    )


def foreign_drop_ins(project: str, paths: Paths) -> list[Path]:
    """Drop-ins for the tenant's unit other than the supervisor's own: systemd would apply them, the supervisor would not."""
    unit = paths.unit_name(project)
    stem = unit[: -len(".service")]
    # systemd also reads `<prefix>-.service.d/` for each dash-separated prefix of
    # the name. Not `service.d/`: that is host-wide policy for every service
    # (some distributions ship one), not something said about this board.
    prefixes = [stem[: index + 1] for index, char in enumerate(stem) if char == "-"]
    names = [f"{unit}.d", *(f"{prefix}.service.d" for prefix in prefixes)]
    found = []
    for directory in paths.drop_in_dirs:
        for candidate_dir in (directory / name for name in names):
            try:
                entries = sorted(candidate_dir.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.name.endswith(".conf") and entry != paths.drop_in_path(project):
                    found.append(entry)
    return found


def parse_environment_file(text: str) -> list[tuple[str, str]]:
    """systemd's `EnvironmentFile=` format: NAME=VALUE lines, # and ; comments, quoted values."""
    assignments: list[tuple[str, str]] = []
    for _, line in _logical_lines(text):
        name, separator, value = line.partition("=")
        name = name.strip()
        if not separator or not ENV_NAME_RE.fullmatch(name):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            quote, value = value[0], value[1:-1]
            if quote == '"':
                value = re.sub(r'\\(["\\$`])', r"\1", value)
        assignments.append((name, value))
    return assignments


def read_environment_file(path: str, *, allowed_owners: set[int]) -> str | None:
    """An env file's text, or None when it is absent. Refuses a link, a non-file, or one someone else could write."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SpecError(f"environment file {path} cannot be opened: {exc.strerror or exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SpecError(f"environment file {path} is not a regular file")
        if info.st_uid not in allowed_owners:
            raise SpecError(f"environment file {path} is owned by uid {info.st_uid}, "
                            f"not by root or the tenant's owner")
        if info.st_mode & 0o022:
            raise SpecError(f"environment file {path} is writable by its group or others "
                            f"(mode {stat.S_IMODE(info.st_mode):04o}); a role could set the board's credentials")
        if info.st_size > ENV_FILE_LIMIT_BYTES:
            raise SpecError(f"environment file {path} is larger than {ENV_FILE_LIMIT_BYTES} bytes")
        return os.read(fd, ENV_FILE_LIMIT_BYTES + 1).decode("utf-8", "replace")
    finally:
        os.close(fd)


def child_environment(spec: ChildSpec, *, owner_uid: int, inherited: dict[str, str], paths: Paths) -> dict[str, str]:
    """What systemd would give this start: its own variables, then `Environment=`, then each env file."""
    env = {name: inherited[name] for name in INHERITED_LOCALE if name in inherited}
    env.update({"PATH": DEFAULT_PATH, "HOME": spec.home, "LOGNAME": spec.user, "USER": spec.user,
                "SHELL": spec.shell})
    if spec.runtime_directory:
        env["RUNTIME_DIRECTORY"] = str(spec.runtime_path(paths))
    env.update(spec.environment)
    for path, optional in spec.environment_files:
        text = read_environment_file(path, allowed_owners={0, owner_uid})
        if text is None:
            if optional:
                continue
            raise SpecError(f"environment file {path} does not exist")
        env.update(parse_environment_file(text))
    return env
