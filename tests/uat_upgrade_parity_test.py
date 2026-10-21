#!/usr/bin/env python3
"""SYRD-629: an upgraded board gets what a fresh one has -- the UAT objects, their grants, and Send to Director.

A board provisioned before pgu1002, running the workflow it shipped with, is
upgraded the way deploy upgrades one: scripts/ticket-board-migrate alone, which
applies pgu1002 and every later migration and never runs rbac.sql, so the
migration's own grants are the ones a deployed board has. It must then carry the same UAT tables, constraints,
triggers, functions and privileges as a board provisioned from today's
schema.sql, and its workflow the same User Review and Director decision stage as
today's shipped document. The pre-fix schema, grants and document are taken
from the parent of the commit that added pgu1002, so this still compares an
upgrade the day after it merges.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()
import ticket_board_write_api_test as t  # noqa: E402
from schema_function_drift import file_before  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

MIGRATION = ROOT / "scripts/ticket_board/migrations/pgu1002_syrd629_uat_instructions.sql"
SHIPPED = json.loads((ROOT / "examples/workflows/inspection.json").read_text())
FUNCTIONS = ("uat_stage", "uat_implementation_stage", "uat_instructions_gap", "require_uat_instructions",
             "supersede_uat_instructions", "set_uat_instructions", "ask_director", "ticket_uat",
             "declared_director_hold", "declared_user_handoff", "enforce_declared_ticket_update")
CHECKS = 0

#: What the two boards must agree on, one query each.
CATALOG = {
    "columns": "SELECT string_agg(concat_ws(':', table_name, column_name, data_type, is_nullable, column_default), ',' "
               "ORDER BY table_name, ordinal_position) FROM information_schema.columns "
               "WHERE table_schema = 'ticket_board' AND table_name LIKE 'ticket_uat%';",
    "constraints": "SELECT string_agg(conrelid::regclass || ':' || conname || ':' || pg_get_constraintdef(oid), ',' "
                   "ORDER BY conrelid::regclass::text, conname) FROM pg_constraint "
                   "WHERE conrelid::regclass::text LIKE 'ticket_board.ticket_uat%';",
    "indexes": "SELECT string_agg(indexdef, ',' ORDER BY indexname) FROM pg_indexes "
               "WHERE schemaname = 'ticket_board' AND tablename LIKE 'ticket_uat%';",
    # The grants a migration owes: the service's and the listener's, which deploy (the runner alone) relies on.
    # The per-role SELECT on every table is install's rbac.sql sweep, which no migration mirrors (pgu997's
    # router_decisions is granted the same way). Compared as sets: an ACL lists entries in grant order.
    "table grants": "SELECT string_agg(c.relname || ':' || coalesce((SELECT string_agg(a::text, ' ' ORDER BY a::text) "
                    "FROM unnest(c.relacl) a WHERE a::text LIKE 'ticket\\_board\\_%'), ''), ',' ORDER BY c.relname) "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'ticket_board' AND c.relname LIKE 'ticket_uat%';",
    "ticket triggers": "SELECT string_agg(tgname || ':' || pg_get_triggerdef(oid), ',' ORDER BY tgname) FROM pg_trigger "
                       "WHERE tgrelid = 'ticket_board.tickets'::regclass AND NOT tgisinternal AND tgname LIKE '%uat%';",
    "functions": "SELECT string_agg(p.oid::regprocedure || ':' || md5(pg_get_functiondef(p.oid)) || ':' "
                 "|| coalesce((SELECT string_agg(a::text, ' ' ORDER BY a::text) FROM unnest(p.proacl) a "
                 "WHERE a::text NOT LIKE 'postgres=%'), ''), ',' "
                 "ORDER BY p.oid::regprocedure::text) FROM pg_proc p "
                 "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'ticket_board' "
                 f"AND p.proname IN ({', '.join(repr(name) for name in FUNCTIONS)});",
}


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def configure(admin: str, document: dict) -> None:
    """The document applied as the Director applies one: through apply_declared_workflow, as the service."""
    literal = json.dumps({"remove_stages": [], **document}).replace("'", "''")
    t.psql(admin, "BEGIN; SET LOCAL ROLE ticket_board_service; "
                  "SELECT set_config('ticket_board.caller_role', 'director', true); "
                  f"SELECT set_config('ticket_board.project', '{document['project']}', true); "
                  f"SELECT ticket_board.apply_declared_workflow('{literal}'::jsonb); COMMIT;")


def stored(admin: str) -> dict:
    return json.loads(t.psql(admin, "SELECT document FROM ticket_board.workflow_configuration WHERE singleton;"))


def user_review_shape(document: dict) -> tuple:
    """The stages in order, and every transition into or out of User Review and the decision stage, by action."""
    def comparable(tr: dict) -> str:
        return json.dumps({k: v for k, v in tr.items() if k != "label"}, sort_keys=True)
    around = {"user_review", "user_feedback"}
    return ([s["name"] for s in document["stages"]],
            next((s for s in document["stages"] if s["name"] == "user_feedback"), None),
            sorted(comparable(tr) for tr in document["transitions"] if {tr["from"], tr["to"]} & around))


def tree_before(into: Path) -> Path:
    """scripts/ as they stood at the parent of the commit that added pgu1002 (clone-based: it must be committed)."""
    def git(*args: str) -> bytes:
        return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True).stdout
    adding = git("log", "--format=%H", "--diff-filter=A", "--", str(MIGRATION.relative_to(ROOT))).decode().split()
    assert adding, f"{MIGRATION.name} is not committed; clone-based checks see only commits"
    with tarfile.open(fileobj=io.BytesIO(git("archive", f"{adding[-1]}^", "scripts"))) as tar:
        tar.extractall(into, filter="data")
    return into


def migrate(admin: str, root: Path) -> None:
    """scripts/ticket-board-migrate from `root`, as deploy runs it: every migration it has not recorded, no rbac.sql."""
    runner = subprocess.run(["bash", str(root / "scripts/ticket-board-migrate")], capture_output=True, text=True,
                            env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": admin}, timeout=600)
    assert runner.returncode == 0, runner.stderr[-1500:]


def install(cluster, db: str, root: Path, *, roles: bool) -> str:
    """A board as install builds one from `root`: schema.sql, roles, rbac.sql, then the runner."""
    admin = t.conninfo(cluster.socket_dir, cluster.port, db)
    t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", db])
    t.psql(admin, (root / "scripts/ticket_board/schema.sql").read_text())
    if roles:
        t.create_roles(admin)
    t.psql(admin, (root / "scripts/ticket_board/rbac.sql").read_text())
    migrate(admin, root)
    return admin


def main() -> int:
    before_document = json.loads(file_before(MIGRATION, "examples/workflows/inspection.json"))
    check(any(tr["action"] == "user_kick_back" for tr in before_document["transitions"])
          and not any(s["name"] == "user_feedback" for s in before_document["stages"]),
          "the pre-fix shipped document is the one whose User returns work")
    with temporary_cluster(prefix="syrd629u-", shutdown="immediate") as cluster, \
            tempfile.TemporaryDirectory(prefix="syrd629u.") as tmp:
        # Installed from today's tree.
        fresh = install(cluster, "fresh", ROOT, roles=True)
        configure(fresh, SHIPPED)
        # Installed before pgu1002, running its era's shipped workflow, then deployed over.
        before = tree_before(Path(tmp))
        upgraded = install(cluster, "upgraded", before, roles=False)
        configure(upgraded, before_document)
        check(t.psql(upgraded, "SELECT to_regclass('ticket_board.ticket_uat_instructions') IS NULL;").strip() == "t",
              "the board before the upgrade has no UAT steps")
        migrate(upgraded, ROOT)
        check(t.psql(upgraded, "SELECT count(*) FROM ticket_board.schema_migrations "
                               f"WHERE name = '{MIGRATION.name}';").strip() == "1", "deployed through pgu1002")

        for name, query in CATALOG.items():
            ours, theirs = t.psql(upgraded, query).strip(), t.psql(fresh, query).strip()
            check(ours and ours == theirs, f"{name}: the upgraded board has what a fresh one has:\n{ours}\n!=\n{theirs}")
        migrated = stored(upgraded)
        check(user_review_shape(migrated) == user_review_shape(stored(fresh)),
              f"and its workflow the same User Review and decision stage as today's shipped document:\n"
              f"{user_review_shape(migrated)}\n!=\n{user_review_shape(stored(fresh))}")
        check(migrated.get("migrations", {}).get("user_feedback") is True
              and t.psql(upgraded, "SELECT actor FROM ticket_board.workflow_revisions ORDER BY revision DESC LIMIT 1;")
              .strip() == "pgu1002", "recorded as the migration's revision")
    print(f"uat_upgrade_parity_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
