#!/usr/bin/env python3
"""SYRD-668: a tenant's grants are the board's own, whichever tenant of its host it is.

pgu928 granted the field audit's direct INSERT, its sequence and three
functions to every role that passed `has_function_privilege` on force_move. In
a fresh provision migrations run before rbac.sql, while every function still
carries PostgreSQL's default PUBLIC EXECUTE, so every non-superuser role in the
cluster passed: PostgreSQL's predefined roles on every tenant, and on a host's
second and later tenant the board's role users too, which rbac.sql creates on
the first. pgu930, pgu935, pgu938, pgu939 and pgu960 found their grantee the
same way, and rbac.sql revoked none of it.

The field audit is written only by director_edit, with definer rights, and
signoff_fields and role_controls_project are called only from definer-rights
functions, so no principal needs direct INSERT on the audit, its sequence, or
those two; director_edit is the board writer's, ticket_board_service's, alone.

Here the packet's own database phase runs as the packet prints it (SYRD-654's
harness) for two tenants, one after the other, in one cluster; then a tenant
built by main's tree, its leaked grants in place, is upgraded the production
way: this tree's ticket-board-migrate, then its rbac.sql. Each role then tries
the audit directly. The before-run is main's code, from a git archive.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKS = 0
SUDO = '#!/bin/bash\n# The packet\'s `sudo` and `sudo -u postgres`, as this user.\nif [ "$1" = "-u" ]; then shift 2; fi\nexec "$@"\n'
#: The board's database principals: rbac.sql's role users, its writer and its listener.
PRINCIPALS = ("director", "user", "ops", "app", "audit", "inspector", "perf", "research", "main",
              "ticket_board_service", "ticket_board_listener")
#: Every object's privileges in the board's schema, and the schema's own; each one's entries in order, so the order
#: grants were made in does not count.
ACL = """
SELECT coalesce(json_agg(row ORDER BY row), '[]')::text FROM (
  SELECT o.object || ' ' || coalesce((SELECT string_agg(x::text, ',' ORDER BY x::text) FROM unnest(o.acl) x), '') AS row
    FROM (SELECT 'schema ticket_board' AS object, nspacl AS acl FROM pg_namespace WHERE nspname = 'ticket_board'
          UNION ALL SELECT 'rel ' || relname, relacl FROM pg_class WHERE relnamespace = 'ticket_board'::regnamespace
          UNION ALL SELECT 'fn ' || oid::regprocedure::text, proacl FROM pg_proc
           WHERE pronamespace = 'ticket_board'::regnamespace) o
) acl;
"""
#: Who holds what, on anything in the schema, outside the board's principals and the object's owner.
STRANGERS = """
SELECT coalesce(json_agg(DISTINCT held ORDER BY held), '[]')::text FROM (
  SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END || ' ' || a.privilege_type
         || ' ' || o.object AS held, a.grantee
    FROM (SELECT 'schema ticket_board' AS object, nspacl AS acl, nspowner AS owner FROM pg_namespace
           WHERE nspname = 'ticket_board'
          UNION ALL SELECT 'rel ' || relname, relacl, relowner FROM pg_class
           WHERE relnamespace = 'ticket_board'::regnamespace
          UNION ALL SELECT 'fn ' || oid::regprocedure::text, proacl, proowner FROM pg_proc
           WHERE pronamespace = 'ticket_board'::regnamespace) o, aclexplode(o.acl) a
   WHERE a.grantee <> o.owner) s
 WHERE grantee = 0 OR pg_get_userbyid(grantee) <> ALL (%s);
"""
#: The SYRD-83 objects' privileges, grantee by grantee.
AUDIT = """
SELECT coalesce(json_agg(held ORDER BY held), '[]')::text FROM (
  SELECT o.object || ' ' || pg_get_userbyid(a.grantee) || ' ' || a.privilege_type AS held
    FROM (SELECT relname::text AS object, relacl AS acl, relowner AS owner FROM pg_class
           WHERE oid IN ('ticket_board.ticket_field_audit'::regclass, 'ticket_board.ticket_field_audit_id_seq'::regclass)
          UNION ALL SELECT proname::text, proacl, proowner FROM pg_proc
           WHERE oid IN ('ticket_board.director_edit(text, jsonb, text)'::regprocedure,
                         'ticket_board.signoff_fields()'::regprocedure,
                         'ticket_board.role_controls_project(text)'::regprocedure)) o, aclexplode(o.acl) a
   WHERE a.grantee <> o.owner) s;
"""
#: What a role could do with the audit directly, each tried on its own.
ATTEMPTS = {
    "insert": "INSERT INTO ticket_board.ticket_field_audit (ticket_id, actor, reason, field, old_value, new_value) "
              "VALUES ('X-9', 'director', 'forged', 'title', '\"a\"', '\"b\"')",
    "nextval": "SELECT nextval('ticket_board.ticket_field_audit_id_seq')",
    "director_edit": "SELECT ticket_board.director_edit('X-9', '{\"title\": \"forged\"}', 'forged')",
    "signoff_fields": "SELECT ticket_board.signoff_fields()",
    "role_controls_project": "SELECT ticket_board.role_controls_project('director')",
}
#: pgu928's own grants as it made them, on a board provisioned while it was the newest migration: the role user and
#: the predefined role here passed its privilege test then. Main's own run no longer leaves the function grants.
PGU928_AS_IT_RAN = "".join(
    f"GRANT EXECUTE ON FUNCTION ticket_board.{fn} TO {who};\n"
    for who in ("ops", "pg_read_all_data")
    for fn in ("director_edit(text, jsonb, text)", "signoff_fields()", "role_controls_project(text)"))
#: And a grant on the schema itself to a role outside the board, as a hand grant would leave one.
HAND_GRANT = "GRANT USAGE ON SCHEMA ticket_board TO pg_monitor;\n"
#: The objects SYRD-668 changes the board principals' grants on; everywhere else they are main's.
SYRD83 = ("rel ticket_field_audit ", "rel ticket_field_audit_id_seq ", "fn ticket_board.director_edit(",
          "fn ticket_board.signoff_fields(", "fn ticket_board.role_controls_project(")


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def scenario(root: Path, work: Path, before_root: Path | None) -> dict:
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "tests"))
    from scripts.ticket_board import project_provision as provision
    from scripts.ticket_board.provision_operator_packet import render_operator_commands
    from temporary_cluster import temporary_cluster

    stubs = work / "stubs"
    stubs.mkdir()
    (stubs / "sudo").write_text(SUDO, encoding="utf-8")
    (stubs / "sudo").chmod(0o755)
    out: dict = {}
    with temporary_cluster(prefix="s668.", shutdown="immediate") as cluster:
        def url(database: str, user: str = "postgres") -> str:
            return f"postgresql:///{database}?host={cluster.socket_dir}&port={cluster.port}&user={user}"

        def sql(database: str, statement: str, user: str = "postgres") -> str:
            done = subprocess.run(["psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", url(database, user), "-c", statement],
                                  capture_output=True, text=True)
            assert done.returncode == 0, done.stderr
            return done.stdout.strip()

        def attempt(database: str, user: str, statement: str) -> str:
            done = subprocess.run(["psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", url(database, user), "-c",
                                   f"BEGIN; {statement}; ROLLBACK;"], capture_output=True, text=True)
            return "ok" if done.returncode == 0 else " ".join(done.stderr.split())[:200]

        def phase(tree: Path, project: str) -> tuple[object, list[str], Path]:
            """The packet's database phase for `project`, as `tree` renders it."""
            base = Path(tempfile.mkdtemp(dir=work, prefix=f"{project}."))
            (base / "board").mkdir()
            (base / "board" / "current").symlink_to(tree)
            plan = provision.build_plan(project=project, owner_user=f"{project}-agent", board_root=base / "board")
            plan = replace(plan, admin_database_url=url(plan.database))
            provision_dir = base / "provision"
            provision.write_artifacts(plan, provision_dir)
            lines = render_operator_commands(plan).splitlines()
            first = next(i for i, line in enumerate(lines) if line.startswith("sudo systemd-tmpfiles --create"))
            last = next(i for i, line in enumerate(lines) if line.startswith("sudo systemctl daemon-reload"))
            return plan, lines[first + 1:last], provision_dir

        def run_lines(lines: list[str], provision_dir: Path) -> None:
            env = {"PATH": f"{stubs}:/usr/bin:/bin", "HOME": str(work), "LANG": "C.UTF-8",
                   "PGHOST": str(cluster.socket_dir), "PGPORT": str(cluster.port), "PGUSER": "postgres"}
            for line in lines:
                done = subprocess.run(["bash", "-c", f"set -euo pipefail\nprovision_dir={provision_dir}\n{line}"],
                                      capture_output=True, text=True, env=env, timeout=600)
                assert done.returncode == 0, (line[:120], done.stderr[-1500:])

        def state(database: str) -> dict:
            return {"acl": json.loads(sql(database, ACL)), "audit": json.loads(sql(database, AUDIT)),
                    "strangers": json.loads(sql(database, STRANGERS % ("ARRAY" + json.dumps(list(PRINCIPALS))
                                                                        .replace('"', "'"))))}

        # A read-only account outside the board, as a backup or monitoring one would be. pg_read_all_data reads every
        # table and uses every schema, so a grant made to it is a grant this account can use.
        sql("postgres", "CREATE ROLE watcher LOGIN IN ROLE pg_read_all_data;")
        # 1. The host's first tenant: no role user exists yet.
        plan_a, lines_a, dir_a = phase(root, "s668a")
        run_lines(lines_a, dir_a)
        out["first"] = state(plan_a.database)
        # 2. Its second, the role users made by the first: up to rbac.sql, then the rest.
        plan_b, lines_b, dir_b = phase(root, "s668b")
        cut = next(i for i, line in enumerate(lines_b) if "/rbac.sql" in line)
        run_lines(lines_b[:cut], dir_b)
        out["second_migrated"] = state(plan_b.database)
        run_lines(lines_b[cut:], dir_b)
        out["second"] = state(plan_b.database)
        import ticket_board_write_api_test as fixture
        fixture.seed_postgres_ticket(url(plan_b.database), "X-9", title="Attempted", state="in_progress", assignee="app")
        out["attempts"] = {who: {name: attempt(plan_b.database, who, statement) for name, statement in ATTEMPTS.items()}
                           for who in ("ops", "director", "ticket_board_listener", "watcher", "ticket_board_service")}
        # 3. The Director's edit, through the board's own handler as the writer, still writes its audit.
        edited_id = f"{plan_b.ticket_prefix}-1"
        # Another implementer's: serial focus would queue a second ticket of App's.
        fixture.seed_postgres_ticket(url(plan_b.database), edited_id, title="Audited", state="in_progress", assignee="main")
        app = fixture.TicketBoardApp(work / "frames", work / "assets", project=plan_b.project,
                                     ticket_prefix=plan_b.ticket_prefix,
                                     database_url=url(plan_b.database, "ticket_board_service"))
        server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            fixture.TEST_WRITE_TOKEN = server.write_token
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                edited = fixture.post_json(base, f"/api/tickets/{edited_id}/actions/director_edit",
                                           {"patch": {"title": "Edited by the Director"}, "reason": "SYRD-668"},
                                           caller="director")
                out["edit"] = edited["ticket"]["title"]
            except AssertionError as refused:
                out["edit"] = " ".join(str(refused).split())[:300]
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        out["edit_audit"] = sql(plan_b.database, "SELECT string_agg(actor || ':' || field || ':' || reason, ',') "
                                                 f"FROM ticket_board.ticket_field_audit WHERE ticket_id = '{edited_id}';")
        # 4. A tenant main's tree built, on the same host, with the leaked grants main makes and pgu928's own as it
        # once made them; upgraded the way a deploy upgrades it: this tree's migrations, then its rbac.sql.
        if before_root is not None:
            plan_c, lines_c, dir_c = phase(before_root, "s668c")
            run_lines(lines_c, dir_c)
            sql(plan_c.database, PGU928_AS_IT_RAN + HAND_GRANT)
            fixture.seed_postgres_ticket(url(plan_c.database), "X-9", title="Attempted", state="in_progress",
                                         assignee="app")
            out["existing"] = state(plan_c.database)
            env = {"PATH": f"{stubs}:/usr/bin:/bin", "HOME": str(work), "LANG": "C.UTF-8",
                   "TICKET_BOARD_ADMIN_DATABASE_URL": url(plan_c.database)}
            for command in (["bash", str(root / "scripts" / "ticket-board-migrate")],
                            ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", url(plan_c.database), "-f",
                             str(root / "scripts" / "ticket_board" / "rbac.sql")]):
                done = subprocess.run(command, capture_output=True, text=True, env=env, timeout=600)
                assert done.returncode == 0, done.stderr[-1500:]
            out["upgraded"] = state(plan_c.database)
            out["upgraded_attempts"] = {who: {name: attempt(plan_c.database, who, statement)
                                              for name, statement in ATTEMPTS.items()}
                                        for who in ("ops", "watcher", "ticket_board_listener")}
            # Run again: nothing changes.
            done = subprocess.run(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", url(plan_c.database), "-f",
                                   str(root / "scripts" / "ticket_board" / "rbac.sql")], capture_output=True, text=True)
            assert done.returncode == 0, done.stderr[-1500:]
            out["upgraded_again"] = state(plan_c.database)
    return out


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "examples", "tests"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def base_commit() -> str:
    """Main as it stood before this ticket: the commit before the one that added this test."""
    found = subprocess.run(["git", "-C", str(ROOT), "log", "--format=%H", "--diff-filter=A", "--",
                            "tests/field_audit_grants_test.py"], check=True, capture_output=True, text=True).stdout.split()
    assert found, "this test is not committed; the run of main needs the commit that added it"
    return found[-1] + "^"


def run(root: Path, before_root: Path | None) -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd668.") as tmp:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "DIRECTORCTL", "TMUX", "PG"))}
        args = [sys.executable, str(Path(__file__).resolve()), "--scenario", str(root), tmp]
        if before_root is not None:
            args.append(str(before_root))
        child = subprocess.run(args, capture_output=True, text=True, env={**env, "PYTHONDONTWRITEBYTECODE": "1"},
                               timeout=1800)
        assert child.returncode == 0, child.stderr[-3000:]
        return json.loads(child.stdout.strip().splitlines()[-1])


def leaked(state: dict, who: str) -> list[str]:
    return [held for held in state["audit"] if f" {who} " in f" {held} "]


def board_view(acl: list[str]) -> list[str]:
    """Each object's grants to the board's principals and owner, outside the SYRD-83 objects."""
    kept = []
    for row in acl:
        if row.startswith(SYRD83):
            continue
        name, _, entries = row.rpartition(" ")
        if "=" not in entries:  # no grants at all: the row is its name
            kept.append(row)
            continue
        ours = [e for e in entries.split(",") if e.split("=", 1)[0].strip('"') in (*PRINCIPALS, "postgres")]
        kept.append(f"{name} {','.join(ours)}")
    return kept


def before(out: dict) -> None:
    check(out["first"]["acl"] != out["second"]["acl"],
          "main: a host's second tenant has other grants than its first")
    writes = {"ticket_field_audit ops INSERT", "ticket_field_audit_id_seq ops USAGE"}
    check(writes <= set(out["second"]["audit"]) and not writes & set(out["first"]["audit"]),
          f"main: on the second, a role user holds the audit's INSERT and its sequence: {leaked(out['second'], 'ops')}")
    check("pg_monitor INSERT rel ticket_field_audit" in out["first"]["strangers"]
          and "pg_monitor SELECT rel publication_requests" in out["first"]["strangers"],
          f"main: on every tenant PostgreSQL's predefined roles hold the audit and the publication requests: "
          f"{out['first']['strangers'][:6]}")
    check(out["attempts"]["ops"]["insert"] == "ok" and out["attempts"]["watcher"]["insert"] == "ok"
          and out["attempts"]["ticket_board_listener"]["insert"] == "ok",
          f"main: a role user, the listener and a read-only pg_read_all_data member forge an audit row: {out['attempts']}")


def candidate(out: dict, main: dict) -> None:
    first, second = out["first"], out["second"]
    check(board_view(first["acl"]) == board_view(main["first"]["acl"]),
          "outside the SYRD-83 objects every board principal holds exactly what it holds on main's first tenant: "
          f"{sorted(set(board_view(first['acl'])) ^ set(board_view(main['first']['acl'])))[:4]}")
    check(first["acl"] == second["acl"], "a host's first and second tenants have the same grants on every object: "
          f"{sorted(set(first['acl']) ^ set(second['acl']))[:4]}")
    check(first["strangers"] == [] and second["strangers"] == [] and out["second_migrated"]["strangers"] == [],
          f"no role outside the board's holds anything, after the migrations or after rbac.sql: "
          f"{out['second_migrated']['strangers'][:6]}")
    check(out["second_migrated"]["audit"] == ["director_edit ticket_board_service EXECUTE"],
          f"the migrations grant the writer director_edit and no other SYRD-83 object, and nobody else any: "
          f"{out['second_migrated']['audit']}")
    readers = sorted(f"ticket_field_audit {who} SELECT" for who in PRINCIPALS)
    check(sorted(second["audit"]) == sorted([*readers, "director_edit ticket_board_service EXECUTE"]),
          f"the audit is read by the board's principals and written by nobody; director_edit is the writer's, and "
          f"its sequence, signoff_fields and role_controls_project are nobody's: {second['audit']}")
    writer = ("ticket_board_service", "director_edit")
    wrong = {f"{who} {name}": result for who, attempts in out["attempts"].items() for name, result in attempts.items()
             if ("permission denied" in result) == ((who, name) == writer)}
    check(not wrong and set(out["attempts"]) == {"ops", "director", "ticket_board_listener", "watcher", writer[0]},
          f"no role user, the listener, a pg_read_all_data member or the writer itself writes the audit, draws its ids or "
          f"calls signoff_fields or role_controls_project; only the writer calls director_edit: {wrong}")
    check(out["edit"] == "Edited by the Director" and out["edit_audit"] == "director:title:SYRD-668",
          f"the Director's edit, through the board's handler, still writes its audit: {out['edit']} {out['edit_audit']}")
    existing, upgraded = out["existing"], out["upgraded"]
    check("ticket_field_audit ops INSERT" in existing["audit"] and "director_edit ops EXECUTE" in existing["audit"]
          and "pg_monitor INSERT rel ticket_field_audit" in existing["strangers"]
          and "pg_read_all_data EXECUTE fn ticket_board.director_edit(text,jsonb,text)" in existing["strangers"]
          and "pg_monitor USAGE schema ticket_board" in existing["strangers"],
          f"main's tenant carries the leaked grants: {existing['audit']} {existing['strangers'][:6]}")
    check(upgraded["acl"] == first["acl"] and upgraded["strangers"] == [] and out["upgraded_again"] == upgraded,
          f"upgraded the way a deploy upgrades it, it has a fresh tenant's grants exactly, and again on a second run: "
          f"{sorted(set(upgraded['acl']) ^ set(first['acl']))[:4]} {upgraded['strangers'][:6]}")
    check(all("permission denied" in result for attempts in out["upgraded_attempts"].values()
              for result in attempts.values()),
          f"... so neither its role users, the listener nor a pg_read_all_data member touch it: {out['upgraded_attempts']}")


def main() -> int:
    if len(sys.argv) in (4, 5) and sys.argv[1] == "--scenario":
        before_root = Path(sys.argv[4]) if len(sys.argv) == 5 else None
        print(json.dumps(scenario(Path(sys.argv[2]), Path(sys.argv[3]), before_root), default=str))
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd668-before.") as tmp:
        main_tree = tree_at(base_commit(), Path(tmp) / "before")
        main_run = run(main_tree, None)
        before(main_run)
        candidate(run(ROOT, main_tree), main_run)
    print(f"field_audit_grants_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
