#!/usr/bin/env python3
"""SYRD-706: no tenant account is the cluster superuser, and nothing a tenant deploys needs it to be.

On this host `initdb` had been run with no authentication method, so the cluster trusted every local socket and
loopback connection: from any account, `psql "postgresql:///<db>?host=/var/run/postgresql&user=postgres"` was the
superuser, over every tenant's board database. Three things leaned on that: each tenant's deploy migrated and
granted as `user=postgres` from the tenant's own release, each tenant's notify listener reached
`ticket_board_listener` with no mapping, and pgu's pg_cron jobs reached `postgres` over TCP 127.0.0.1.

Main first, from a git archive, then this tree:

- the cluster bootstrap: `ensure-postgres-cluster`'s `initdb` (stubbed host) names peer and scram, never trust;
- root's database work (`database_admin`): as `postgres`, `env -i`, `cwd=/`, no `user=`, only from root's trusted
  release whose runner, RBAC and migrations are root's alone, for a plain database name -- refused otherwise;
- the listener's peer mapping, from `ticket-board-boardsvc-setup.sh --apply-peer-auth` (stubbed root) and from both
  of its callers: the tenant's own map, its own database, never root or postgres;
- a real cluster: the trusting cluster lets this account be `postgres`; hardened as `database_auth` renders it, the
  cluster's own reading of the files has no trust and no error, and after a reload this account -- a tenant owner --
  is refused as `postgres`, as the board service and on another tenant's database, and is its listener on its own;
- pg_cron, preloaded in a real cluster: the board's jobs connect through the server's socket, on a fresh board and
  on main's board upgraded by this tree's migration.
"""

from __future__ import annotations

import getpass
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: Main's commit this ticket is cut from: the before-run's tree.
BEFORE = "9f5b2a3a7a7d714ec7938d88e0c3c13dcb5379ca"
SHA = "f" * 40
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def connect(cluster, database: str, user: str) -> str:
    """`current_user` as this account connecting over the cluster's socket, or the refusal; never a password."""
    done = subprocess.run(["psql", "-X", "-At", "-w", f"host={cluster.socket_dir} port={cluster.port} dbname={database} "
                           f"user={user}", "-c", "SELECT current_user"], capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, timeout=30)
    return done.stdout.strip() if done.returncode == 0 else "refused: " + done.stderr.strip()[-160:]


def admin(cluster, sql: str, database: str = "postgres") -> str:
    """As the fixture's superuser, while the cluster still lets this account be it."""
    done = subprocess.run(["psql", "-X", "-At", "-v", "ON_ERROR_STOP=1", f"host={cluster.socket_dir} port={cluster.port} "
                           f"dbname={database} user=postgres", "-c", sql], capture_output=True, text=True, timeout=120,
                          env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def bootstrap(tree: Path) -> list[str]:
    """`initdb` as `ensure-postgres-cluster` runs it on an empty Arch host, the host stubbed."""
    import postgres_cluster_readiness_test as readiness

    host = readiness.Host().with_empty_data_parent()
    try:
        done = host.run(script=tree / "scripts" / "ensure-postgres-cluster")
        assert done.returncode == 0, (done.stdout, done.stderr)
        return host.ran("initdb")
    finally:
        host.close()


def peer_setup(tree: Path, scratch: Path, listener_user: str) -> dict:
    """`--apply-peer-auth` with root, the cluster and the account tools stubbed: the files it leaves."""
    stubs, conf = scratch / f"stubs-{listener_user}", scratch / f"conf-{listener_user}"
    stubs.mkdir()
    conf.mkdir()
    (conf / "pg_hba.conf").write_text("local   all   all   peer\nhost all all 127.0.0.1/32 scram-sha-256\n")
    (conf / "pg_ident.conf").write_text("# maps\n")
    scripts = {
        "id": '#!/bin/sh\n[ "$1" = "-u" ] && { echo 0; exit 0; }\nexit 0\n',
        "useradd": "#!/bin/sh\nexit 0\n",
        "runuser": ('#!/bin/sh\ncase "$*" in\n'
                    f'  *"SHOW hba_file"*) echo {conf}/pg_hba.conf ;;\n'
                    f'  *"SHOW ident_file"*) echo {conf}/pg_ident.conf ;;\n'
                    '  *"SHOW server_version_num"*) echo 180006 ;;\n'
                    '  *pg_reload_conf*) echo t ;;\n  *) exit 1 ;;\nesac\n'),
    }
    for name, body in scripts.items():
        (stubs / name).write_text(body)
        (stubs / name).chmod(0o755)
    done = subprocess.run(
        [str(tree / "scripts" / "ticket-board-boardsvc-setup.sh"), "--apply-peer-auth"], capture_output=True, text=True,
        env={"PATH": f"{stubs}:/usr/bin:/bin", "LANG": "C.UTF-8", "TICKET_BOARD_PROJECT": "alpha",
             "PG_DATABASE": "alpha_ticket_board", "PG_IDENT_MAP": "pgu_ticket_board_service", "SERVICE_USER": "boardsvc",
             "SERVICE_ROLE": "ticket_board_service", "LISTENER_USER": listener_user,
             "LISTENER_IDENT_MAP": "alpha_ticket_board_listener"}, timeout=60)
    return {"code": done.returncode, "stderr": done.stderr[-300:],
            "hba": "".join(p.read_text() for p in sorted(conf.glob("*pg_hba.conf"))),
            "ident": "".join(p.read_text() for p in sorted(conf.glob("*pg_ident.conf")))}


def callers(scratch: Path) -> dict:
    """What `switchyard new` and the operator packet hand the peer-auth setup."""
    from scripts import board_services
    from scripts.ticket_board.project_provision import build_plan
    from scripts.ticket_board.provision_operator_packet import peer_auth_command

    plan = build_plan(project="alpha", owner_user="alice", owner_home=scratch / "home", board_root=scratch / "board",
                      asset_dir=scratch / "assets", frame_dir=scratch / "frames", commit_git_dir=scratch / "cache.git")
    seen: dict = {}

    def runner(argv, **kwargs):
        seen["env"] = {k: v for k, v in kwargs.get("env", {}).items() if k.startswith(("LISTENER_", "PG_", "SERVICE_"))}
        return subprocess.CompletedProcess(argv, 0, "", "")

    board_services._ensure_board_service_peer_auth(plan, source_repo=scratch, runner=runner)
    return {"packet": peer_auth_command(plan), "new": seen.get("env", {})}


def root_database_work(tree: Path, scratch: Path) -> dict:
    """`database_admin` against a trusted release built here, every command recorded, nothing run."""
    try:
        from scripts.ticket_board import database_admin, privileged_operations
    except ImportError:
        return {"absent": True}
    privileged_operations.TRUSTED_OWNER_UID = os.getuid()  # the suite's tree stands in for root's
    releases = scratch / "releases"
    release = releases / SHA
    (release / "scripts" / "ticket_board" / "migrations").mkdir(parents=True)
    (release / "scripts" / "ticket-board-migrate").write_text("#!/bin/sh\nexit 0\n")
    (release / "scripts" / "ticket_board" / "rbac.sql").write_text("SELECT 1;\n")
    (release / "scripts" / "ticket_board" / "migrations" / "pgu1_a.sql").write_text("SELECT 1;\n")
    for path in release.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    trusted = lambda commit: str(releases / commit)  # noqa: E731

    def attempt(database: str = "alpha_ticket_board", *, of: Path = release, resolver=trusted, fail: str = "") -> dict:
        ran: list = []

        def runner(argv, **kwargs):
            ran.append({"argv": argv, "cwd": kwargs.get("cwd"), "env": kwargs.get("env")})
            code = 1 if fail and argv[-1].endswith(fail) else 0
            return subprocess.CompletedProcess(argv, code, "", "simulated" if code else "")

        problems = database_admin.administer_release_database(of, database, runner=runner, trusted_release=resolver)
        return {"problems": problems, "ran": ran}

    out = {"ok": attempt(), "bad_name": attempt("alpha; DROP DATABASE x"),
           "untrusted": attempt(resolver=lambda commit: str(scratch / "elsewhere" / commit)),
           "migrate_fails": attempt(fail="ticket-board-migrate")}
    writable = release / "scripts" / "ticket_board" / "migrations" / "pgu1_a.sql"
    writable.chmod(0o666)
    out["writable_migration"] = attempt()
    writable.chmod(0o644)
    linked = release / "scripts" / "ticket_board" / "migrations" / "pgu2_link.sql"
    linked.symlink_to(scratch / "anything.sql")
    out["linked_migration"] = attempt()
    linked.unlink()
    unit = ("[Service]\nEnvironment=TICKET_BOARD_DATABASE_URL=postgresql:///alpha_ticket_board?host=/run/postgresql"
            "&user=ticket_board_service\n")
    out["unit_database"] = database_admin.database_from_unit(unit)
    try:
        database_admin.database_from_unit(unit.replace("alpha_ticket_board?", "alpha%3B%20x?"))
        out["unit_bad"] = "accepted"
    except database_admin.DatabaseAdminError as exc:
        out["unit_bad"] = str(exc)
    return out


def hardened_cluster(tree: Path) -> dict:
    """A trusting cluster, then the same cluster as `database_auth` renders it."""
    from temporary_cluster import temporary_cluster

    me = getpass.getuser()
    out: dict = {"me": me}
    with temporary_cluster(prefix="syrd706-pg-") as cluster:
        admin(cluster, f'CREATE ROLE "{me}" SUPERUSER LOGIN')  # under peer: the cluster's own account, by name
        for role in ("ticket_board_listener", "ticket_board_service"):
            admin(cluster, f"CREATE ROLE {role} LOGIN")
        for database in ("alpha_ticket_board", "beta_ticket_board"):
            admin(cluster, f"CREATE DATABASE {database}")
        out["before"] = {"as_postgres": connect(cluster, "alpha_ticket_board", "postgres"),
                         "other_listener": connect(cluster, "beta_ticket_board", "ticket_board_listener")}
        try:
            from scripts.ticket_board import database_auth
        except ImportError:
            out["absent"] = True
            return out
        hba, ident = cluster.data_dir / "pg_hba.conf", cluster.data_dir / "pg_ident.conf"
        tenants = [database_auth.Tenant("alpha", me, "alpha_ticket_board"),
                   database_auth.Tenant("beta", "bob", "beta_ticket_board")]
        hba.write_text(database_auth.harden_hba(hba.read_text(), tenants))
        ident.write_text(database_auth.harden_ident(ident.read_text(), tenants))
        rules = json.loads(admin(cluster, "SELECT coalesce(json_agg(r), '[]') FROM (SELECT file_name, line_number, type, "
                                          "auth_method, error FROM pg_hba_file_rules) r") or "[]")
        out["remaining"] = database_auth.remaining_trust(rules)
        out["rules"] = len(rules)
        admin(cluster, "SELECT pg_reload_conf()")
        out["after"] = {
            "as_postgres": connect(cluster, "alpha_ticket_board", "postgres"),
            "own_listener": connect(cluster, "alpha_ticket_board", "ticket_board_listener"),
            "other_listener": connect(cluster, "beta_ticket_board", "ticket_board_listener"),
            "as_service": connect(cluster, "alpha_ticket_board", "ticket_board_service"),
            "as_itself": connect(cluster, "postgres", me),
        }
    out["unreadable"] = database_auth.remaining_trust([{"file_name": "x.conf", "line_number": 3, "error": "bad",
                                                       "auth_method": "peer", "type": "local"}])
    try:
        database_auth.harden_hba('local all "all" trust\n', [])
        out["quoted"] = "rendered"
    except database_auth.HbaError as exc:
        out["quoted"] = str(exc)
    try:
        database_auth.harden_hba("local all all trust trust\n", [])
        out["twice"] = "rendered"
    except database_auth.HbaError as exc:
        out["twice"] = str(exc)
    try:
        database_auth.Tenant("alpha", "postgres", "alpha_ticket_board")
        out["postgres_tenant"] = "accepted"
    except database_auth.HbaError as exc:
        out["postgres_tenant"] = str(exc)
    return out


def cron(tree: Path) -> dict:
    """pg_cron preloaded: the jobs a board's schema schedules, and after this tree's migration."""
    from temporary_cluster import temporary_cluster

    out: dict = {}
    with temporary_cluster(prefix="syrd706-cron-", settings=("shared_preload_libraries=pg_cron",
                                                              "cron.database_name=cronboard")) as cluster:
        out["socket"] = str(cluster.socket_dir)
        admin(cluster, "CREATE DATABASE cronboard")
        done = subprocess.run(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", f"host={cluster.socket_dir} port={cluster.port} "
                               "dbname=cronboard user=postgres", "-f", str(tree / "scripts/ticket_board/schema.sql")],
                              capture_output=True, text=True, timeout=300, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
        assert done.returncode == 0, done.stderr[-2000:]
        jobs = "SELECT coalesce(json_object_agg(jobname, nodename), '{}') FROM cron.job"
        out["fresh"] = json.loads(admin(cluster, jobs, "cronboard"))
        migration = ROOT / "scripts/ticket_board/migrations/pgu1020_syrd706_cron_over_the_socket.sql"
        done = subprocess.run(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", f"host={cluster.socket_dir} port={cluster.port} "
                               "dbname=cronboard user=postgres", "-f", str(migration)],
                              capture_output=True, text=True, timeout=120, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
        assert done.returncode == 0, done.stderr[-2000:]
        out["migrated"] = json.loads(admin(cluster, jobs, "cronboard"))
        # Re-provisioned: the tree's schema applied again, which schedules the jobs again.
        done = subprocess.run(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", f"host={cluster.socket_dir} port={cluster.port} "
                               "dbname=cronboard user=postgres", "-f", str(tree / "scripts/ticket_board/schema.sql")],
                              capture_output=True, text=True, timeout=300, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
        assert done.returncode == 0, done.stderr[-2000:]
        out["rescheduled"] = json.loads(admin(cluster, jobs, "cronboard"))
    return out


def scenario(tree: Path, scratch: Path) -> dict:
    sys.path[:0] = [str(ROOT / "tests")]
    # The suite's helpers first: importing them puts this checkout on the path, and `scripts` is a namespace package
    # that follows the path. Then the path is the tree's alone, so every product module below is the tree's.
    import postgres_cluster_readiness_test  # noqa: F401
    import temporary_cluster  # noqa: F401

    sys.path[:] = [str(tree)] + [p for p in sys.path if p and Path(p).resolve() != ROOT.resolve()]
    for name in [name for name in sys.modules if name == "scripts" or name.startswith("scripts.")]:
        del sys.modules[name]
    from scripts import board_services

    assert Path(board_services.__file__).resolve().is_relative_to(tree.resolve()), board_services.__file__
    return {
        "initdb": bootstrap(tree),
        "peer": peer_setup(tree, scratch, "alice"),
        "peer_root": peer_setup(tree, scratch, "root"),
        "callers": callers(scratch),
        "admin": root_database_work(tree, scratch),
        "cluster": hardened_cluster(tree),
        "cron": cron(tree),
    }


def tree_at(commit: str, into: Path) -> Path:
    into.mkdir(parents=True)
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "examples", "skills"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(tree: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd706.", dir="/tmp") as scratch:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "PGU_TICKET_BOARD_", "PG"))}
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(tree), scratch],
                               capture_output=True, text=True, timeout=900, cwd=scratch, stdin=subprocess.DEVNULL,
                               env={**env, "PYTHONDONTWRITEBYTECODE": "1"})
    assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
    return json.loads(child.stdout.strip().splitlines()[-1])


def common(got: dict) -> None:
    before = got["cluster"]["before"]
    check(before["as_postgres"] == "postgres" and before["other_listener"] == "ticket_board_listener",
          f"a cluster that trusts local connections: this account is postgres, and any tenant's listener: {before}")
    check(got["callers"]["new"].get("SERVICE_USER") == "boardsvc" and "SERVICE_USER='boardsvc'" in got["callers"]["packet"],
          f"both callers still map the board service: {got['callers']}")


def before(got: dict) -> None:
    common(got)
    check(len(got["initdb"]) == 1 and "--auth" not in got["initdb"][0],
          f"main: the bootstrap's initdb names no method, so it trusts everything: {got['initdb']}")
    check(got["peer"]["code"] == 0 and "ticket_board_listener" not in got["peer"]["hba"],
          f"main: the listener gets no rule of its own; it reaches its role only through trust: {got['peer']}")
    check("LISTENER_USER" not in got["callers"]["packet"] and "LISTENER_USER" not in got["callers"]["new"],
          f"main: neither caller says whose listener it is: {got['callers']}")
    check(got["admin"] == {"absent": True} and got["cluster"].get("absent"),
          "main: no root-side database work and nothing to render the cluster without trust")
    check(set(got["cron"]["fresh"].values()) == {"localhost"} and len(got["cron"]["fresh"]) == 2,
          f"main: pg_cron's jobs connect over TCP localhost, as postgres: {got['cron']['fresh']}")
    socket = got["cron"]["socket"]
    check(set(got["cron"]["migrated"].values()) == {socket},
          f"main's board, upgraded by this tree's migration: through the socket: {got['cron']}")


def candidate(got: dict) -> None:
    common(got)
    check(len(got["initdb"]) == 1 and "--auth-local=peer" in got["initdb"][0]
          and "--auth-host=scram-sha-256" in got["initdb"][0] and "trust" not in got["initdb"][0],
          f"the bootstrap's initdb: peer on the socket, scram over TCP: {got['initdb']}")
    peer = got["peer"]
    check(peer["code"] == 0
          and "local   alpha_ticket_board   ticket_board_listener   peer map=alpha_ticket_board_listener" in peer["hba"]
          and "alpha_ticket_board_listener   alice   ticket_board_listener" in peer["ident"]
          and "local   alpha_ticket_board   ticket_board_service   peer map=pgu_ticket_board_service" in peer["hba"],
          f"the setup maps the owner to its listener, on its own database, through its own map: {peer}")
    check(got["peer_root"]["code"] != 0 and "LISTENER_USER must be the tenant owner" in got["peer_root"]["stderr"]
          and "ticket_board_listener" not in got["peer_root"]["hba"],
          f"root is never a listener's owner: {got['peer_root']}")
    callers_ = got["callers"]
    check("LISTENER_USER='alice'" in callers_["packet"]
          and "LISTENER_IDENT_MAP='alpha_ticket_board_listener'" in callers_["packet"]
          and callers_["new"].get("LISTENER_USER") == "alice"
          and callers_["new"].get("LISTENER_IDENT_MAP") == "alpha_ticket_board_listener",
          f"both callers name the owner and the tenant's own map: {callers_}")
    work = got["admin"]
    ran = work["ok"]["ran"]
    url = "postgresql:///alpha_ticket_board?host=/var/run/postgresql"
    check(work["ok"]["problems"] == [] and len(ran) == 2 and all(r["cwd"] == "/" and r["env"] is None for r in ran)
          and all(r["argv"][:7] == ["runuser", "-u", "postgres", "--", "env", "-i", "PATH=/usr/bin:/bin"] for r in ran),
          f"root's database work runs as postgres, environment emptied, from /: {ran}")
    check(ran[0]["argv"][-1].endswith(f"/releases/{SHA}/scripts/ticket-board-migrate")
          and f"TICKET_BOARD_ADMIN_DATABASE_URL={url}" in ran[0]["argv"]
          and ran[1]["argv"][-2:-1] == ["-f"] and ran[1]["argv"][-1].endswith(f"/releases/{SHA}/scripts/ticket_board/rbac.sql")
          and url in ran[1]["argv"] and not any("user=" in part for r in ran for part in r["argv"]),
          f"the release's migrations, then its RBAC, over the socket with no user named: {ran}")
    for name, said in (("bad_name", "not a plain database name"), ("untrusted", "is not root's trusted release"),
                       ("writable_migration", "writable only by it"), ("linked_migration", "is not a regular file")):
        check(work[name]["ran"] == [] and any(said in p for p in work[name]["problems"]),
              f"{name}: refused before the superuser runs anything: {work[name]}")
    check(len(work["migrate_fails"]["ran"]) == 1 and "migrating alpha_ticket_board" in work["migrate_fails"]["problems"][0],
          f"a failed migration stops before the RBAC: {work['migrate_fails']}")
    check(work["unit_database"] == "alpha_ticket_board" and "no plain database" in work["unit_bad"],
          f"the database comes from root's unit, and only a plain name: {work['unit_database']} {work['unit_bad']}")
    cluster = got["cluster"]
    after = cluster["after"]
    check(cluster["remaining"] == [] and cluster["rules"] > 0,
          f"the cluster's own reading of the hardened files: no trust, no error: {cluster['remaining']}")
    check(after["as_postgres"].startswith("refused") and after["as_service"].startswith("refused")
          and after["other_listener"].startswith("refused"),
          f"a tenant owner is refused as postgres, as the board service and on another tenant's database: {after}")
    check(after["own_listener"] == "ticket_board_listener" and after["as_itself"] == cluster["me"],
          f"and is its own listener, on its own board; the cluster's own account is still its superuser: {after}")
    check(cluster["unreadable"] == ["x.conf:3 does not parse: bad"],
          f"a line the cluster cannot read is a problem, whatever its method: {cluster['unreadable']}")
    check("quotes a field" in cluster["quoted"] and "more than once" in cluster["twice"]
          and "not a tenant owner" in cluster["postgres_tenant"],
          f"anything not rendered with certainty is refused: {cluster['quoted']} | {cluster['twice']} | "
          f"{cluster['postgres_tenant']}")
    socket = got["cron"]["socket"]
    for name in ("fresh", "migrated", "rescheduled"):
        check(set(got["cron"][name].values()) == {socket} and len(got["cron"][name]) == 2,
              f"pg_cron's jobs connect through the server's own socket ({name}): {got['cron'][name]}")


def mirrors() -> None:
    """schema.sql's last definition of each of pgu1020's functions is pgu1020's, byte for byte."""
    migration = (ROOT / "scripts/ticket_board/migrations/pgu1020_syrd706_cron_over_the_socket.sql").read_text()
    body = migration.split("BEGIN;\n\n", 1)[1].rsplit("\nCOMMIT;\n", 1)[0]
    schema = (ROOT / "scripts/ticket_board/schema.sql").read_text()
    at = schema.rfind(body)
    heads = [f"CREATE OR REPLACE FUNCTION ticket_board.{name}(" for name in
             ("local_socket_directory", "cron_over_the_socket")]
    check(at >= 0 and all(at <= schema.rfind(h) < at + len(body) for h in heads),
          "schema.sql's last definitions of pgu1020's functions are pgu1020's, byte for byte")


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), Path(sys.argv[3])), default=str))
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd706-before.", dir="/tmp") as tmp:
        main_tree = tree_at(BEFORE, Path(tmp) / "before")
        if os.environ.get("SYRD706_MUTATION_SKIP_BEFORE") != "1":
            before(run(main_tree))
        mirrors()
        candidate(run(ROOT))
    print(f"database_superuser_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
