#!/usr/bin/env python3
"""SYRD-706: root's move of the cluster off local trust -- preview, apply by digest, probes, and the way back.

`database_superuser_boundary_test` proves on a real cluster what the rendered files do. This drives the transition
around them (`scripts/database_auth_transition.py`) against a host that answers its commands: a `psql` as `postgres`
reads and writes the cluster's state, and each probe, as each account, is decided by the files the cluster last
reloaded -- the first matching rule, peer by the account's name or its ident map, scram never passing without a
password. Main first: main has neither the command nor its catalogued actions.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "9f5b2a3a7a7d714ec7938d88e0c3c13dcb5379ca"
CHECKS = 0
TRUSTING = """local   alpha_ticket_board   ticket_board_service   peer map=pgu_ticket_board_service
local   beta_ticket_board    ticket_board_service   peer map=pgu_ticket_board_service
# initdb's
local   all             all                                     trust
host    all             all             127.0.0.1/32            trust
host    all             all             ::1/128                 trust
"""
IDENT = "pgu_ticket_board_service   boardsvc   ticket_board_service\n"


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


class Host:
    """The cluster and the accounts, as the transition's commands reach them."""

    def __init__(self, tmp: Path, *, databases=("alpha_ticket_board", "beta_ticket_board"), cron=True) -> None:
        self.hba, self.ident = tmp / "pg_hba.conf", tmp / "pg_ident.conf"
        self.hba.write_text(TRUSTING)
        self.ident.write_text(IDENT)
        for path in (self.hba, self.ident):
            path.chmod(0o640)
        self.loaded = (self.hba.read_text(), self.ident.read_text())
        self.databases = list(databases)
        self.cron = [[13, "ticket_board_due_nudges", "localhost"], [14, "ticket_board_notification_trace_prune",
                                                                   "localhost"]] if cron else None
        self.log: list[str] = []
        self.refuse_reload = False
        self.included_trust = False  # an included file, outside the plan, that still trusts
        self.misbehave: tuple[str, str] = ("", "")  # (a probe, "connect" or "unreachable"): an outcome to get wrong

    def allowed(self, account: str, conninfo: str) -> tuple[bool, str]:
        fields = dict(part.split("=", 1) for part in conninfo.split())
        tcp = not fields["host"].startswith("/")
        database, user = fields["dbname"], fields.get("user", account)
        hba, ident = self.loaded
        from scripts.ticket_board import database_auth

        for _index, rule in database_auth._rules(hba):
            kind, db, role = rule[0], rule[1], rule[2]
            if (kind == "local") == tcp or db not in ("all", database) or role not in ("all", user):
                continue
            method = rule[database_auth._method_index(rule, 0)]
            if method == "trust":
                return True, ""
            if method.startswith("scram"):
                return False, "fe_sendauth: no password supplied"
            maps = [o.split("=", 1)[1] for o in rule if o.startswith("map=")]
            if maps:
                ok = any(line.split()[:3] == [maps[0], account, user] for line in ident.splitlines() if line.strip()
                         and not line.startswith("#"))
            else:
                ok = account == user
            return ok, "" if ok else f'FATAL:  Peer authentication failed for user "{user}"'
        return False, f'FATAL:  no pg_hba.conf entry for user "{user}"'

    def __call__(self, argv, **kwargs):
        assert argv[:2] == ["runuser", "-u"] and argv[3:6] == ["--", "env", "-i"] and kwargs.get("cwd") == "/", argv
        assert "PGPASSFILE=/dev/null" in argv and "-w" in argv, argv
        account, conninfo, sql = argv[2], argv[-3], argv[-1]
        done = lambda out="", code=0, err="": subprocess.CompletedProcess(argv, code, out, err)  # noqa: E731
        if "/var/run/postgresql" not in conninfo:  # a probe, at the server's own socket or over TCP
            ok, why = self.allowed(account, conninfo)
            label = f"{account}@{conninfo}"
            if self.misbehave[0] and self.misbehave[0] in label:
                ok, why = (True, "") if self.misbehave[1] == "connect" else (False, "could not connect to server")
            self.log.append(f"probe {label} -> {'ok' if ok else 'refused'}")
            return done("1" if ok else "", 0 if ok else 2, why)
        ok, why = self.allowed("postgres", conninfo)
        if not ok:
            return done("", 2, why)
        if sql == "SHOW hba_file":
            return done(str(self.hba))
        if sql == "SHOW ident_file":
            return done(str(self.ident))
        if sql == "SHOW unix_socket_directories":
            return done("/run/postgresql")
        if sql.startswith("SELECT datname FROM pg_database"):
            return done("\n".join(self.databases))
        if sql == "SHOW cron.database_name":
            return done("pgu") if self.cron is not None else done("", 1, 'unrecognized configuration parameter')
        if sql.startswith("SELECT coalesce(json_agg(json_build_array(jobid"):
            return done(json.dumps([job for job in self.cron if not job[2].startswith("/")]))
        if sql.startswith("UPDATE cron.job SET nodename"):
            host, jobid = re.match(r"UPDATE cron.job SET nodename = '(.*)' WHERE jobid = (\d+)", sql).groups()
            self.log.append(f"cron {jobid} -> {host}")
            for job in self.cron:
                if job[0] == int(jobid):
                    job[2] = host
            return done("")
        if sql.startswith("SELECT coalesce(json_agg(r), '[]') FROM (SELECT file_name"):
            from scripts.ticket_board import database_auth

            rows = []
            for index, rule in database_auth._rules(self.hba.read_text()):
                rows.append({"file_name": str(self.hba), "line_number": index + 1, "type": rule[0],
                             "auth_method": rule[database_auth._method_index(rule, index + 1)], "error": None})
            if self.included_trust:
                rows.append({"file_name": "included.conf", "line_number": 1, "type": "local", "auth_method": "trust",
                             "error": None})
            self.log.append("read rules")
            return done(json.dumps(rows))
        if sql == "SELECT pg_reload_conf()":
            if self.refuse_reload:
                return done("", 2, "reload refused")
            self.loaded = (self.hba.read_text(), self.ident.read_text())
            self.log.append("reload")
            return done("t")
        if sql == "SELECT 1":
            return done("1")
        raise AssertionError(f"unexpected statement as postgres: {sql}")


def records(tmp: Path, tenants: dict[str, tuple[str, str]], registry: dict[str, str] | None = None) -> tuple[Path, Path]:
    provision, registered = tmp / "provision", tmp / "projects"
    for project, (owner, database) in tenants.items():
        (provision / project).mkdir(parents=True)
        (provision / project / "plan.json").write_text(json.dumps({"owner_user": owner, "database": database}))
    registered.mkdir()
    for project, account in (registry or {}).items():
        (registered / f"{project}.json").write_text(json.dumps({"board_ui": {"tenant_user": account}}))
    return provision, registered


def scenarios(tree: Path) -> dict:
    sys.path[:0] = [str(tree)]
    from scripts.ticket_board import privileged_actions, privileged_operations

    out: dict = {"catalogue": {a.name: [a.authentication, [n for n, _ in a.arguments]] for a in privileged_actions.CATALOGUE
                               if "database" in a.name}}
    if not out["catalogue"]:
        return out
    out["operations"] = {name: privileged_operations.OPERATIONS[name]({"digest": "a" * 64}) for name in out["catalogue"]}
    from scripts import database_auth_transition as t

    t.pwd.getpwnam = lambda name: name  # every owner is an account on this host
    me = os.getuid()
    tenants = {"alpha": ("alice", "alpha_ticket_board"), "beta": ("bob", "beta_ticket_board")}

    def setting(name: str, **host_kwargs):
        tmp = Path(tempfile.mkdtemp(prefix="syrd706-t."))
        host = Host(tmp, **host_kwargs)
        provision, registry = records(tmp, tenants, {"alpha": "alice"})
        return tmp, host, provision, registry

    def plan_of(host, provision, registry):
        return t.build_plan(runner=host, provision_root=provision, registry=registry, owner_uid=me)

    # -- preview ----------------------------------------------------------------------------------------------------
    tmp, host, provision, registry = setting("preview")
    plan = plan_of(host, provision, registry)
    text = t.preview(plan)
    out["preview"] = {"problems": plan.problems, "digest": plan.digest, "has_digest": plan.digest in text,
                      "lines": [line for line in text.splitlines() if line.startswith(("+", "-", "  ", "pg_cron"))][:60],
                      "unchanged": host.hba.read_text() == TRUSTING and host.log.count("reload") == 0,
                      "again": plan_of(host, provision, registry).digest == plan.digest}

    # -- apply: success -------------------------------------------------------------------------------------------
    printed: list[str] = []
    code = t.apply(plan.digest, runner=host, provision_root=provision, registry=registry, owner_uid=me,
                   backup_root=tmp / "backups", print_func=printed.append, clock=lambda: 0)
    backup = tmp / "backups" / "19700101T000000Z"
    out["applied"] = {"code": code, "said": printed, "log": host.log,
                      "hba": host.hba.read_text(), "ident": host.ident.read_text(),
                      "mode": oct(host.hba.stat().st_mode & 0o777), "cron": json.loads(json.dumps(host.cron)),
                      "backup": sorted(p.name for p in backup.iterdir()) if backup.exists() else [],
                      "backup_hba": (backup / "pg_hba.conf").read_text() if backup.exists() else "",
                      "backup_mode": oct(backup.stat().st_mode & 0o777) if backup.exists() else ""}
    out["restored"] = t.restore(backup, runner=host)
    out["after_restore"] = {"hba": host.hba.read_text() == TRUSTING, "ident": host.ident.read_text() == IDENT,
                            "cron": host.cron, "loaded": host.loaded[0] == TRUSTING}

    # -- apply: refusals before anything is written ------------------------------------------------------------------
    tmp, host, provision, registry = setting("stale")
    plan = plan_of(host, provision, registry)
    host.hba.write_text(TRUSTING + "# edited since the preview\n")
    out["stale"] = {"code": t.apply(plan.digest, runner=host, provision_root=provision, registry=registry, owner_uid=me,
                                    backup_root=tmp / "backups", print_func=lambda _l: None),
                    "written": host.hba.read_text() != TRUSTING + "# edited since the preview\n",
                    "backups": (tmp / "backups").exists()}
    tmp, host, provision, registry = setting("legacy", databases=("alpha_ticket_board", "beta_ticket_board", "pgu"))
    plan = plan_of(host, provision, registry)
    out["legacy"] = {"problems": plan.problems,
                     "code": t.apply(plan.digest, runner=host, provision_root=provision, registry=registry,
                                     owner_uid=me, backup_root=tmp / "backups", print_func=lambda _l: None),
                     "unchanged": host.hba.read_text() == TRUSTING}
    tmp = Path(tempfile.mkdtemp(prefix="syrd706-t."))
    host = Host(tmp)
    provision, registry = records(tmp, tenants, {"alpha": "mallory"})
    out["mismatch"] = plan_of(host, provision, registry).problems

    # -- apply: a probe that does not hold, and a refusal that is not authentication ----------------------------------
    for name, wrong in (("probe", ("alice@host=/run/postgresql dbname=beta_ticket_board user=ticket_board_listener",
                                   "connect")),
                        ("not_authentication", ("alice@host=127.0.0.1", "unreachable"))):
        tmp, host, provision, registry = setting(name)
        plan = plan_of(host, provision, registry)
        host.misbehave = wrong
        said: list[str] = []
        code = t.apply(plan.digest, runner=host, provision_root=provision, registry=registry, owner_uid=me,
                       backup_root=tmp / "backups", print_func=said.append, clock=lambda: 0)
        out[name] = {"code": code, "said": said, "hba_back": host.hba.read_text() == TRUSTING,
                     "ident_back": host.ident.read_text() == IDENT, "cron": host.cron,
                     "reloaded_back": host.loaded[0] == TRUSTING, "reloads": host.log.count("reload")}
    tmp, host, provision, registry = setting("included_trust")
    plan = plan_of(host, provision, registry)
    host.included_trust = True
    said = []
    out["included_trust"] = {"code": t.apply(plan.digest, runner=host, provision_root=provision, registry=registry,
                                             owner_uid=me, backup_root=tmp / "backups", print_func=said.append,
                                             clock=lambda: 0),
                             "said": said, "hba_back": host.hba.read_text() == TRUSTING, "log": host.log}
    tmp, host, provision, registry = setting("no_cron", cron=False)
    plan = plan_of(host, provision, registry)
    out["no_cron"] = {"moves": plan.cron_moves,
                      "code": t.apply(plan.digest, runner=host, provision_root=provision, registry=registry,
                                      owner_uid=me, backup_root=tmp / "backups", print_func=lambda _l: None)}

    # -- the command --------------------------------------------------------------------------------------------------
    from scripts import team_launcher as launcher

    os.environ[launcher.PRIVILEGED_PROVISION_ROOT_ENV] = str(provision)  # never this host's own records
    said = []
    t.os.geteuid = lambda: 1000
    out["not_root"] = [t.database_auth_command(["preview"], runner=host, print_func=said.append), said[:]]
    t.os.geteuid = lambda: 0
    said.clear()
    out["outside_backups"] = [t.database_auth_command(["restore", "/etc"], runner=host, print_func=said.append), said[:]]
    return out


def tree_at(commit: str, into: Path) -> Path:
    into.mkdir(parents=True)
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts"], check=True,
                             capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(tree: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "PG"))}
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenarios", str(tree)], capture_output=True,
                           text=True, timeout=300, env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, stdin=subprocess.DEVNULL)
    assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
    return json.loads(child.stdout.strip().splitlines()[-1])


def before(got: dict) -> None:
    check(got == {"catalogue": {}}, f"main: no catalogued way to move the cluster off trust: {got}")


def candidate(got: dict) -> None:
    check(got["catalogue"] == {"preview-database-auth": ["auth_admin", []],
                               "harden-database-auth": ["auth_admin", ["digest"]]},
          f"both halves are catalogued, the User authenticating for each: {got['catalogue']}")
    check({name: command[1:] for name, command in got["operations"].items()}
          == {"preview-database-auth": ["database-auth", "preview"],
              "harden-database-auth": ["database-auth", "apply", "a" * 64]},
          f"each runs root's database-auth with the digest and nothing else: {got['operations']}")
    preview = got["preview"]
    joined = "\n".join(preview["lines"])
    check(preview["problems"] == [] and preview["has_digest"] and preview["unchanged"] and preview["again"],
          f"the preview changes nothing, names its digest, and the same state gives the same digest: {preview}")
    check("+local   all             all                                     peer  # SYRD-706: was trust" in joined
          and "+host    all             all             127.0.0.1/32            scram-sha-256  # SYRD-706: was trust" in joined
          and "+local   alpha_ticket_board   ticket_board_listener   peer map=alpha_ticket_board_listener" in joined
          and "+alpha_ticket_board_listener   alice   ticket_board_listener" in joined
          and "13 ticket_board_due_nudges: localhost -> /run/postgresql" in joined
          and "refused: bob is not alpha's listener" in joined,
          f"the preview is the exact change: both diffs, the jobs moved, every probe: {preview['lines']}")
    applied = got["applied"]
    log = applied["log"]
    check(applied["code"] == 0 and "trust" not in applied["hba"].replace("was trust", "") and applied["mode"] == "0o640"
          and set(job[2] for job in applied["cron"]) == {"/run/postgresql"},
          f"applied: no trust left, each file's mode kept, the jobs through the socket: {applied}")
    check(log.index("read rules") < log.index("cron 13 -> /run/postgresql") < log.index("reload")
          < min(i for i, line in enumerate(log) if line.startswith("probe")),
          f"the cluster reads the files before anything moves, the jobs move, one reload, then the probes: {log}")
    probes = [line for line in log if line.startswith("probe")]
    check(len(probes) == 13 and "probe postgres@host=/run/postgresql dbname=postgres -> ok" in probes
          and "probe alice@host=/run/postgresql dbname=alpha_ticket_board user=ticket_board_listener -> ok" in probes
          and "probe alice@host=/run/postgresql dbname=alpha_ticket_board user=postgres -> refused" in probes
          and "probe alice@host=/run/postgresql dbname=beta_ticket_board user=ticket_board_listener -> refused" in probes
          and "probe alice@host=127.0.0.1 dbname=alpha_ticket_board user=postgres -> refused" in probes
          and "probe boardsvc@host=/run/postgresql dbname=beta_ticket_board user=ticket_board_service -> ok" in probes,
          f"every probe, as every account: {probes}")
    check(applied["backup"] == ["pg_hba.conf", "pg_ident.conf", "record.json"] and applied["backup_hba"] == TRUSTING
          and applied["backup_mode"] == "0o700" and any("way back" in line for line in applied["said"]),
          f"the previous files are root's backup, and the way back is said: {applied['backup']} {applied['said']}")
    check(got["restored"] == [] and all(got["after_restore"][k] for k in ("hba", "ident", "loaded"))
          and set(job[2] for job in got["after_restore"]["cron"]) == {"localhost"},
          f"restore puts the files and the jobs back, and reloads: {got['restored']} {got['after_restore']}")
    check(got["stale"]["code"] == 1 and not got["stale"]["written"] and not got["stale"]["backups"],
          f"a file changed since the preview: refused before anything is written: {got['stale']}")
    legacy = got["legacy"]
    check(legacy["code"] == 1 and legacy["unchanged"] and any(p.startswith("pgu is a database no root record")
                                                               for p in legacy["problems"]),
          f"a board database no root record accounts for is a refusal: {legacy}")
    check(any("root's registry names mallory but its provision record alice" in p for p in got["mismatch"]),
          f"root's two records must agree on the owner: {got['mismatch']}")
    for name in ("probe", "not_authentication"):
        r = got[name]
        check(r["code"] == 1 and r["hba_back"] and r["ident_back"] and r["reloaded_back"] and r["reloads"] == 2
              and set(job[2] for job in r["cron"]) == {"localhost"},
              f"{name}: a probe that does not hold puts the files and jobs back and reloads them: {r}")
    check("connected" in " ".join(got["probe"]["said"]) and "not refused by authentication" in " ".join(
        got["not_authentication"]["said"]), f"and says which probe: {got['probe']['said']} {got['not_authentication']['said']}")
    inc = got["included_trust"]
    check(inc["code"] == 1 and inc["hba_back"] and "still trusts" in " ".join(inc["said"])
          and not any(line.startswith("probe") or line.endswith("-> /run/postgresql") for line in inc["log"]),
          f"the cluster's own reading still trusts something: the files go back before anything moves: {inc}")
    check(got["no_cron"] == {"moves": [], "code": 0}, f"a cluster without pg_cron: nothing to move: {got['no_cron']}")
    check(got["not_root"][0] == 1 and got["not_root"][1] and got["not_root"][1][0].startswith("switchyard: database-auth is root's")
          and got["outside_backups"][0] == 1 and "directly under" in got["outside_backups"][1][0],
          f"root only, and a restore only from root's backups: {got['not_root']} {got['outside_backups']}")


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--scenarios":
        print(json.dumps(scenarios(Path(sys.argv[2])), default=str))
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd706t-before.", dir="/tmp") as tmp:
        if os.environ.get("SYRD706_MUTATION_SKIP_BEFORE") != "1":
            before(run(tree_at(BEFORE, Path(tmp) / "before")))
        candidate(run(ROOT))
    print(f"database_auth_transition_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
