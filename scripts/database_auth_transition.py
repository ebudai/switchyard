"""Root's move of a PostgreSQL cluster that trusts every local connection to one that trusts none (SYRD-706).

`switchyard database-auth preview` and `switchyard database-auth apply <digest>`, root only, behind the catalogued
`preview-database-auth` and `harden-database-auth` actions -- both `auth_admin`: the User authenticates for each, no
pane pre-authorizes either.

- **The plan** is read, never guessed: the files the cluster names (`SHOW hba_file`, `SHOW ident_file`), the tenants
  root's own provision records name (owner and database, each checked against root's registry), every board database
  the cluster holds, and the pg_cron jobs that still connect over TCP. `database_auth` renders the replacement. A
  database no root record accounts for -- a legacy board -- is a refusal: its listener would be cut off. Unless the
  cluster refuses every connection to it (`datallowconn = false`): then it is sealed offline, named in the plan, its
  digest and its preview, granted nothing, never connected to, and proved still sealed after apply and restore
  (SYRD-712).
- **The preview** is that plan, exactly: both files' diffs, the jobs moved, the probes, the backup and the way back,
  and the plan's digest, which covers the current files too.
- **The apply** recomputes the plan and refuses unless its digest is the one the User accepted. It backs up both files
  and the jobs' hosts under root's own directory, writes each file atomically with its owner and mode, and has the
  cluster read them (`pg_hba_file_rules`) before reloading: no trust and no error, or nothing is reloaded. It moves
  the jobs to the socket and reloads -- never restarts -- then probes as each account: postgres is the superuser;
  each tenant's owner is its listener on its own board, and is refused, by authentication, as postgres, as the board
  service and on another tenant's board; the board service still reaches each board; TCP as postgres with no
  password is refused. Any failure puts the files and the jobs back, reloads, and says so.
- **The way back** is `switchyard database-auth restore <backup>`, root only.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import pwd
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from scripts.ticket_board import database_auth

SOCKET_DIR = "/var/run/postgresql"
BACKUP_ROOT = Path("/var/lib/switchyard/database-auth")
REGISTRY = Path("/etc/switchyard/projects")
SERVICE_USER = "boardsvc"
TIMEOUT_SECONDS = 30
MAX_FILE_BYTES = 1 << 20


class TransitionError(Exception):
    """The transition cannot proceed as planned; the message says why."""


@dataclass
class Plan:
    hba_file: str
    ident_file: str
    hba_before: str
    ident_before: str
    hba_after: str = ""
    ident_after: str = ""
    socket: str = SOCKET_DIR
    tenants: list[database_auth.Tenant] = field(default_factory=list)
    unaccounted: list[str] = field(default_factory=list)
    #: Databases no root record accounts for that the cluster refuses every connection to (`datallowconn = false`):
    #: preserved evidence, sealed offline. Granted nothing, never connected to, and proved still sealed (SYRD-712).
    sealed: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    cron_database: str = ""
    cron_moves: list[list[Any]] = field(default_factory=list)

    @property
    def digest(self) -> str:
        document = {
            "hba_file": self.hba_file, "ident_file": self.ident_file,
            "hba_before": self.hba_before, "ident_before": self.ident_before,
            "hba_after": self.hba_after, "ident_after": self.ident_after,
            "tenants": [[t.project, t.owner, t.database] for t in self.tenants],
            "cron": [self.cron_database, self.socket, self.cron_moves],
            "sealed": self.sealed,
        }
        return hashlib.sha256(json.dumps(document, sort_keys=True).encode("utf-8")).hexdigest()


def _run(argv: list[str], runner: Callable[..., Any]) -> Any:
    try:
        return runner(argv, cwd="/", stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                      text=True, timeout=TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 255, "", str(exc))


def as_account(account: str, conninfo: str, sql: str) -> list[str]:
    """`psql` as an OS account, its environment emptied: no password file, no service file, never a prompt."""
    return ["runuser", "-u", account, "--", "env", "-i", "PATH=/usr/bin:/bin", "LANG=C.UTF-8", "PGPASSFILE=/dev/null",
            "PGSERVICEFILE=/dev/null", "psql", "-X", "-w", "-At", "-v", "ON_ERROR_STOP=1", conninfo, "-c", sql]


def query(sql: str, *, runner: Callable[..., Any], database: str = "postgres") -> str:
    """As the cluster's own account, over the socket."""
    done = _run(as_account("postgres", f"host={SOCKET_DIR} dbname={database}", sql), runner)
    if done.returncode != 0:
        raise TransitionError(f"as postgres on {database}: {str(done.stderr or '').strip()[-300:] or 'psql failed'}")
    return str(done.stdout or "").strip()


def _read_root_file(path: Path, *, owner_uid: int, limit: int = MAX_FILE_BYTES) -> bytes:
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise TransitionError(f"{path} cannot be read ({exc.strerror})") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise TransitionError(f"{path} is not a regular file owned by uid {owner_uid} and writable only by it")
        data = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise TransitionError(f"{path} is larger than {limit} bytes")
    return data


def root_tenants(*, provision_root: Path, registry: Path = REGISTRY, owner_uid: int = 0,
                 ) -> tuple[list[database_auth.Tenant], list[str]]:
    """Each tenant root's provision records name: its owner and its database, from root's own plan."""
    tenants, problems = [], []
    try:
        projects = sorted(entry.name for entry in provision_root.iterdir() if entry.is_dir() and not entry.is_symlink())
    except OSError as exc:
        return [], [f"root's provision records cannot be listed ({exc.strerror})"]
    for project in projects:
        try:
            plan = json.loads(_read_root_file(provision_root / project / "plan.json", owner_uid=owner_uid))
            tenant = database_auth.Tenant(project, str(plan.get("owner_user") or ""), str(plan.get("database") or ""))
        except (TransitionError, ValueError, database_auth.HbaError) as exc:
            problems.append(f"{project}: root's provision record cannot name its owner and database ({exc})")
            continue
        registered = registry / f"{project}.json"
        if registered.exists():
            try:
                entry = json.loads(_read_root_file(registered, owner_uid=owner_uid))
            except (TransitionError, ValueError) as exc:
                problems.append(f"{project}: root's registry entry cannot be read ({exc})")
                continue
            account = (entry.get("board_ui") or {}).get("tenant_user") if isinstance(entry, dict) else None
            if account and account != tenant.owner:
                problems.append(f"{project}: root's registry names {account} but its provision record {tenant.owner}")
                continue
        tenants.append(tenant)
    return tenants, problems


#: Every database a tenant could own, and whether the cluster accepts connections to it. Read from `postgres`'s own
#: catalogue: a database that accepts none is never connected to, and this is how it is known to be so.
DATABASES = ("SELECT coalesce(json_agg(json_build_array(datname, datallowconn) ORDER BY datname), '[]') "
             "FROM pg_database WHERE NOT datistemplate AND datname <> 'postgres'")


def databases(*, runner: Callable[..., Any]) -> dict[str, bool]:
    """Each database, and whether it accepts connections (`datallowconn`)."""
    return {str(name): allowed is True for name, allowed in json.loads(query(DATABASES, runner=runner) or "[]")}


def unsealed(sealed: Sequence[str], *, runner: Callable[..., Any]) -> list[str]:
    """Each database the plan holds sealed offline that is gone, or that now accepts connections."""
    now = databases(runner=runner)
    return [f"{database} is no longer sealed offline ({'gone' if database not in now else 'it accepts connections'})"
            for database in sealed if now.get(database, True)]


def build_plan(*, runner: Callable[..., Any], provision_root: Path, registry: Path = REGISTRY, owner_uid: int = 0,
               read: Callable[[str], str] | None = None) -> Plan:
    """What the transition would do, from what the cluster and root's records say now. Changes nothing."""
    hba_file, ident_file = query("SHOW hba_file", runner=runner), query("SHOW ident_file", runner=runner)
    socket = (query("SHOW unix_socket_directories", runner=runner).split(",")[0].strip() or SOCKET_DIR)
    reader = read or (lambda path: Path(path).read_text(encoding="utf-8"))
    plan = Plan(hba_file, ident_file, reader(hba_file), reader(ident_file), socket=socket)
    tenants, plan.problems = root_tenants(provision_root=provision_root, registry=registry, owner_uid=owner_uid)
    state = databases(runner=runner)
    plan.tenants = [tenant for tenant in tenants if tenant.database in state]
    accounted = {tenant.database for tenant in plan.tenants}
    for database, connectable in state.items():
        if database in accounted:
            continue
        if not connectable:
            # Nobody reaches it now, as nobody will after: it needs no rule, and its contents are not this
            # transition's to read or change (SYRD-712).
            plan.sealed.append(database)
            continue
        plan.unaccounted.append(database)
        plan.problems.append(f"{database} is a database no root record accounts for: its listener and every other "
                             "account reaching it now would be cut off. Give it root's records (its provisioning or "
                             "cutover) or drop it, then preview again")
    for tenant in plan.tenants:
        try:
            pwd.getpwnam(tenant.owner)
        except KeyError:
            plan.problems.append(f"{tenant.project}'s owner {tenant.owner} is not an account on this host")
    try:
        plan.hba_after = database_auth.harden_hba(plan.hba_before, plan.tenants)
        plan.ident_after = database_auth.harden_ident(plan.ident_before, plan.tenants)
    except database_auth.HbaError as exc:
        plan.problems.append(str(exc))
    try:
        plan.cron_database = query("SHOW cron.database_name", runner=runner)
    except TransitionError:
        plan.cron_database = ""  # pg_cron is not loaded: nothing schedules a connection
    if plan.cron_database:
        rows = query("SELECT coalesce(json_agg(json_build_array(jobid, jobname, nodename) ORDER BY jobid), '[]') "
                     "FROM cron.job WHERE nodename NOT LIKE '/%'", runner=runner, database=plan.cron_database)
        plan.cron_moves = json.loads(rows or "[]")
    return plan


def probes(plan: Plan) -> list[tuple[str, list[str], bool]]:
    """(what, argv, should it connect) -- run as root after the reload."""
    checks: list[tuple[str, list[str], bool]] = [
        ("postgres is the cluster's superuser over the socket",
         as_account("postgres", f"host={plan.socket} dbname=postgres", "SELECT 1"), True),
    ]
    for tenant in plan.tenants:
        own = f"host={plan.socket} dbname={tenant.database}"
        checks += [
            (f"{tenant.owner} is {tenant.project}'s listener", as_account(tenant.owner, own + " user=ticket_board_listener",
                                                                         "SELECT 1"), True),
            (f"{tenant.owner} is not postgres", as_account(tenant.owner, own + " user=postgres", "SELECT 1"), False),
            (f"{tenant.owner} is not the board service", as_account(tenant.owner, own + " user=ticket_board_service",
                                                                   "SELECT 1"), False),
            (f"{tenant.owner} has no password-free TCP superuser",
             as_account(tenant.owner, f"host=127.0.0.1 dbname={tenant.database} user=postgres", "SELECT 1"), False),
            (f"{SERVICE_USER} is still {tenant.project}'s board service",
             as_account(SERVICE_USER, own + " user=ticket_board_service", "SELECT 1"), True),
        ]
        for other in plan.tenants:
            if other.database != tenant.database:
                checks.append((f"{tenant.owner} is not {other.project}'s listener",
                               as_account(tenant.owner, f"host={plan.socket} dbname={other.database} "
                                                        "user=ticket_board_listener", "SELECT 1"), False))
    return checks


#: What a refusal must be to count as one: the server refusing the account, not a connection that never happened.
REFUSED_BY_AUTHENTICATION = ("authentication failed", "no password supplied", "password authentication",
                             "no pg_hba.conf entry")


def run_probes(plan: Plan, *, runner: Callable[..., Any]) -> list[str]:
    failures = []
    for what, argv, connects in probes(plan):
        done = _run(argv, runner)
        said = str(done.stderr or "")
        if connects and done.returncode != 0:
            failures.append(f"{what}: refused ({said.strip()[-200:] or 'psql failed'})")
        elif not connects and done.returncode == 0:
            failures.append(f"{what}: it connected")
        elif not connects and not any(reason in said for reason in REFUSED_BY_AUTHENTICATION):
            failures.append(f"{what}: not refused by authentication ({said.strip()[-200:] or 'no reason given'})")
    return failures


def preview(plan: Plan) -> str:
    lines = [f"database-auth preview, digest {plan.digest}", ""]
    if plan.problems:
        lines += ["REFUSED -- this plan cannot be applied:", *(f"  - {problem}" for problem in plan.problems), ""]
    if plan.sealed:
        lines += ["SEALED OFFLINE -- no root record accounts for these, and the cluster refuses every connection to "
                  "them (datallowconn = false). No rule is granted for them, nothing here connects to, dumps, drops, "
                  "renames or reopens them, and the apply refuses unless each is still sealed before and after:",
                  *(f"  - {database}" for database in plan.sealed), ""]
    lines += [f"tenants (root's records): " + (", ".join(f"{t.project} ({t.owner} -> {t.database})"
                                                         for t in plan.tenants) or "none")]
    for name, before, after in ((plan.hba_file, plan.hba_before, plan.hba_after),
                                (plan.ident_file, plan.ident_before, plan.ident_after)):
        lines += ["", *difflib.unified_diff(before.splitlines(), after.splitlines(), name, f"{name} (hardened)",
                                            lineterm="")]
    if plan.cron_moves:
        lines += ["", f"pg_cron jobs in {plan.cron_database} moved to {plan.socket}:",
                  *(f"  {jobid} {name}: {host} -> {plan.socket}" for jobid, name, host in plan.cron_moves)]
    lines += ["", "then pg_reload_conf() (no restart), and these probes, every one of which must hold:",
              *(f"  {'connects' if ok else 'refused'}: {what}" for what, _argv, ok in probes(plan)),
              "", f"backup: {BACKUP_ROOT}/<time>/ (both files and the jobs' hosts); any failed probe restores it.",
              "way back: sudo switchyard database-auth restore <that backup>",
              f"apply: switchyard privileged-action <project> harden-database-auth digest={plan.digest}"]
    return "\n".join(lines) + "\n"


def _write_like(path: str, text: str) -> None:
    """`text` into `path` atomically, with the owner and mode the file has now."""
    info = os.stat(path)
    directory = os.path.dirname(path)
    fd, temporary = tempfile.mkstemp(prefix=".switchyard-", dir=directory)
    try:
        os.write(fd, text.encode("utf-8"))
        os.fchown(fd, info.st_uid, info.st_gid)
        os.fchmod(fd, stat.S_IMODE(info.st_mode))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)


def _set_cron_hosts(plan: Plan, hosts: Sequence[Sequence[Any]], *, runner: Callable[..., Any]) -> None:
    for jobid, _name, host in hosts:
        query(f"UPDATE cron.job SET nodename = {_literal(str(host))} WHERE jobid = {int(jobid)}", runner=runner,
              database=plan.cron_database)


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def restore(backup: Path, *, runner: Callable[..., Any]) -> list[str]:
    """Put back what a backup holds, reload, and check postgres still connects."""
    try:
        record = json.loads((backup / "record.json").read_text(encoding="utf-8"))
        _write_like(record["hba_file"], (backup / "pg_hba.conf").read_text(encoding="utf-8"))
        _write_like(record["ident_file"], (backup / "pg_ident.conf").read_text(encoding="utf-8"))
        plan = Plan(record["hba_file"], record["ident_file"], "", "", cron_database=record.get("cron_database", ""))
        if plan.cron_database:
            _set_cron_hosts(plan, record.get("cron_moves", []), runner=runner)
        query("SELECT pg_reload_conf()", runner=runner)
        query("SELECT 1", runner=runner)
        # Restoring touches no database, so a sealed one is still sealed; proved, not assumed (SYRD-712).
        return unsealed(record.get("sealed", []), runner=runner)
    except (OSError, ValueError, KeyError, TransitionError) as exc:
        return [f"restoring {backup} failed: {exc}"]


def apply(digest: str, *, runner: Callable[..., Any], provision_root: Path, registry: Path = REGISTRY,
          owner_uid: int = 0, backup_root: Path = BACKUP_ROOT, print_func: Callable[[str], None] = print,
          clock: Callable[[], float] = time.time) -> int:
    plan = build_plan(runner=runner, provision_root=provision_root, registry=registry, owner_uid=owner_uid)
    if plan.digest != digest:
        print_func(f"switchyard: the plan is now {plan.digest}, not the {digest} that was accepted; preview again")
        return 1
    if plan.problems:
        print_func("switchyard: refusing: " + "; ".join(plan.problems))
        return 1
    backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup = backup_root / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(clock()))
    backup.mkdir(mode=0o700)
    (backup / "pg_hba.conf").write_text(plan.hba_before, encoding="utf-8")
    (backup / "pg_ident.conf").write_text(plan.ident_before, encoding="utf-8")
    (backup / "record.json").write_text(json.dumps({
        "digest": plan.digest, "hba_file": plan.hba_file, "ident_file": plan.ident_file,
        "cron_database": plan.cron_database, "cron_moves": plan.cron_moves, "sealed": plan.sealed}, indent=1),
        encoding="utf-8")

    def back(why: str) -> int:
        problems = restore(backup, runner=runner)
        print_func(f"switchyard: {why}; " + ("; ".join(problems) if problems else
                                             f"the previous files and jobs are back from {backup}, and reloaded"))
        return 1

    try:
        _write_like(plan.hba_file, plan.hba_after)
        _write_like(plan.ident_file, plan.ident_after)
        rules = json.loads(query("SELECT coalesce(json_agg(r), '[]') FROM (SELECT file_name, line_number, type, "
                                 "auth_method, error FROM pg_hba_file_rules) r", runner=runner) or "[]")
    except (OSError, ValueError, TransitionError) as exc:
        return back(f"writing the hardened files failed ({exc})")
    remaining = database_auth.remaining_trust(rules)
    if remaining:
        return back("the cluster's own reading of the hardened files: " + "; ".join(remaining))
    try:
        if plan.cron_moves:
            _set_cron_hosts(plan, [[jobid, name, plan.socket] for jobid, name, _host in plan.cron_moves], runner=runner)
        query("SELECT pg_reload_conf()", runner=runner)
    except TransitionError as exc:
        return back(f"moving the jobs or reloading failed ({exc})")
    failures = run_probes(plan, runner=runner)
    try:
        failures += unsealed(plan.sealed, runner=runner)
    except TransitionError as exc:
        failures.append(f"the sealed databases' state could not be read ({exc})")
    if failures:
        return back("a probe did not hold: " + "; ".join(failures))
    print_func(f"switchyard: hardened {plan.hba_file} and {plan.ident_file} ({plan.digest}); every probe held. "
               f"Backup: {backup}; way back: sudo switchyard database-auth restore {backup}")
    return 0


def database_auth_command(argv: Sequence[str], *, runner: Callable[..., Any] = subprocess.run,
                          print_func: Callable[[str], None] = print) -> int:
    """`switchyard database-auth preview | apply <digest> | restore <backup>`, root only."""
    from scripts import team_launcher as launcher

    if os.geteuid() != 0:
        print_func("switchyard: database-auth is root's: run it through `switchyard privileged-action host "
                   "preview-database-auth` (or harden-database-auth), which asks the User")
        return 1
    provision_root = launcher.switchyard_privileged_provision_root()
    try:
        if list(argv) == ["preview"]:
            plan = build_plan(runner=runner, provision_root=provision_root)
            print_func(preview(plan))
            return 1 if plan.problems else 0
        if len(argv) == 2 and argv[0] == "apply":
            return apply(argv[1], runner=runner, provision_root=provision_root, print_func=print_func)
        if len(argv) == 2 and argv[0] == "restore":
            backup = Path(argv[1])
            if backup.parent != BACKUP_ROOT or backup.is_symlink():
                print_func(f"switchyard: a backup is a directory directly under {BACKUP_ROOT}")
                return 1
            problems = restore(backup, runner=runner)
            print_func("; ".join(problems) if problems else f"switchyard: restored {backup} and reloaded")
            return 1 if problems else 0
    except TransitionError as exc:
        print_func(f"switchyard: {exc}")
        return 1
    print_func("usage: switchyard database-auth preview | apply <digest> | restore <backup>")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(database_auth_command(sys.argv[1:]))
