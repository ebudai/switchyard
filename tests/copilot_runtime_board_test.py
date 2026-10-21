#!/usr/bin/env python3
"""SYRD-634: every board boundary that names a runtime takes `copilot`, and still takes exactly the other four.

Three places in the board's SQL name the runtimes: the CHECK on
`role_runtime_assignments.runtime`, `validate_declared_workflow`, and
`rebind_declared_pane_roles`. Each is asked on two production-built boards
(schema.sql, companion roles, rbac.sql, the real ticket-board-migrate, rbac.sql
again):

* a fresh board, built from this tree;
* main's board as it shipped before pgu1003, upgraded with this tree's
  migrations, so the migration's copies are what run.

Both are asked the same questions, in SQL directly and, for the declared
workflow, through the board application as the Director applies one. The
function definitions of the two boards are then compared, so the migration's
copy cannot drift from schema.sql's.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

import psycopg  # noqa: E402
import ticket_board_write_api_test as fixture  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

MIGRATION = ROOT / "scripts/ticket_board/migrations/pgu1003_syrd634_copilot_runtime.sql"
FIVE = ("claude", "codex", "agy", "hermes", "copilot")
REFUSED = ("gh-copilot", "Copilot", "gemini", "")
FUNCTIONS = ("validate_declared_workflow", "rebind_declared_pane_roles")
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def document(runtime: str | None = "copilot") -> dict:
    cfg = json.loads((ROOT / "examples/workflows/inspection.json").read_text())
    cfg["project"] = "pgu"
    for role in cfg["roles"]:
        if role.get("target"):
            role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
        if role["name"] == "main" and runtime is not None:
            role["runtime"] = runtime
    return cfg


def migrate(admin: str, source: Path = ROOT) -> None:
    result = subprocess.run(["bash", str(source / "scripts/ticket-board-migrate")], capture_output=True, text=True,
                            env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": admin}, timeout=300)
    assert result.returncode == 0, result.stderr[-1500:]
    fixture.psql(admin, (source / "scripts/ticket_board/rbac.sql").read_text())


def before_tree(into: Path) -> Path:
    """The tree as it stood before pgu1003 joined it."""
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True, text=True).stdout
    adding = git("log", "--format=%H", "--diff-filter=A", "--", str(MIGRATION.relative_to(ROOT))).split()
    assert adding, f"{MIGRATION.name} is not committed; clone-based checks see only commits"
    into.mkdir(parents=True)
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", f"{adding[-1]}^", "scripts"], check=True,
                             capture_output=True)
    subprocess.run(["tar", "-x", "-C", str(into)], input=archive.stdout, check=True)
    assert not (into / "scripts/ticket_board/migrations" / MIGRATION.name).exists()
    return into


def build_board(cluster, dbname: str, *, before: Path | None = None, migrations: bool = True) -> str:
    source = before or ROOT
    admin = fixture.conninfo(cluster.socket_dir, cluster.port, dbname)
    fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", dbname])
    fixture.psql(admin, (source / "scripts/ticket_board/schema.sql").read_text())
    if fixture.psql(admin, "SELECT count(*) FROM pg_roles WHERE rolname = 'director';") == "0":
        fixture.create_roles(admin)
    fixture.psql(admin, (source / "scripts/ticket_board/rbac.sql").read_text())
    if migrations:
        migrate(admin, source)
    if before is not None:
        migrate(admin)  # the upgrade: this tree's migrations, then its rbac.sql
    return admin


def ask(admin: str, statement: str, *args: object) -> dict:
    """One statement in its own transaction, rolled back: its answer, never its effect."""
    try:
        with psycopg.connect(admin, autocommit=False) as conn:
            row = conn.execute(statement, args or None).fetchone()
            conn.rollback()
        # Test boards are SQL_ASCII, where text comes back as bytes.
        return {"ok": tuple(v.decode() if isinstance(v, bytes) else v for v in row) if row is not None else None}
    except psycopg.Error as exc:
        return {"refused": str(exc).splitlines()[0]}


def board_questions(cluster, name: str, admin: str) -> dict:
    seen: dict = {}
    for runtime in (*FIVE, *REFUSED):
        seen[f"validate:{runtime}"] = ask(admin, "SELECT ticket_board.validate_declared_workflow(%s::jsonb)",
                                          json.dumps(document(runtime)))
    seen["constraint"] = ask(admin, "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = "
                                    "'role_runtime_assignments_runtime_check'")["ok"][0]
    seen["constraints"] = ask(admin, "SELECT count(*) FROM pg_constraint WHERE conrelid = "
                                     "'ticket_board.role_runtime_assignments'::regclass AND contype = 'c' "
                                     "AND pg_get_constraintdef(oid) LIKE '%(runtime = ANY%'")["ok"][0]
    # The Director applies a workflow whose main runs copilot, through the board application.
    service = fixture.conninfo(cluster.socket_dir, cluster.port, f"copilot_{name}", fixture.SERVICE_ROLE)
    app = fixture.TicketBoardApp(cluster.root / f"{name}-frames", cluster.root / f"{name}-assets", project="pgu",
                                 ticket_prefix="PGU", database_url=service)
    try:
        app.apply_workflow(document("copilot"), expected_revision=0, dry_run=False, caller_role="director")
        seen["applied"] = True
    except Exception as exc:  # noqa: BLE001 - recorded, and checked below
        seen["applied"] = f"{type(exc).__name__}: {exc}"
    stored = ask(admin, "SELECT revision, document FROM ticket_board.workflow_configuration")["ok"]
    # A refused workflow stores nothing; the questions still run, and verify() names what failed.
    revision, current = stored if stored else (0, document("copilot"))
    seen["applied_runtime"] = next(r for r in current["roles"] if r["name"] == "main").get("runtime") if stored else None
    for runtime in (*FIVE, *REFUSED):
        seen[f"assign:{runtime}"] = ask(
            admin, "INSERT INTO ticket_board.role_runtime_assignments (role, runtime, actual_target, worktree, "
                   "session_dir, process_pid, process_start_time, process_uid) VALUES ('main', %s, 'pgu-main:0.0', "
                   "'/w/main', '/s/main', 4242, 99, 1000) RETURNING runtime", runtime)
        seen[f"rebind:{runtime}"] = ask(
            admin, "SELECT ticket_board.rebind_declared_pane_roles(%s, %s::jsonb, %s::jsonb, 'SYRD-634 check')",
            revision, json.dumps(current),
            json.dumps({"designer": {"runtime": runtime, "target": "pgu-designer:0.0", "slot": None}}))
    seen["functions"] = {fn: ask(admin, "SELECT pg_get_functiondef(p.oid) FROM pg_proc p JOIN pg_namespace n "
                                        "ON n.oid = p.pronamespace WHERE n.nspname = 'ticket_board' AND p.proname = %s",
                                 fn)["ok"][0] for fn in FUNCTIONS}
    return seen


def verify(name: str, seen: dict) -> None:
    for runtime in FIVE:
        check("ok" in seen[f"validate:{runtime}"], f"[{name}] a workflow running {runtime} is valid: {seen[f'validate:{runtime}']}")
        check(seen[f"assign:{runtime}"] == {"ok": (runtime,)}, f"[{name}] {runtime} may be assigned: {seen[f'assign:{runtime}']}")
        check("ok" in seen[f"rebind:{runtime}"], f"[{name}] a pane role may be rebound to {runtime}: {seen[f'rebind:{runtime}']}")
    for runtime in REFUSED:
        check("invalid runtime/target" in seen[f"validate:{runtime}"].get("refused", ""),
              f"[{name}] {runtime!r} is not a runtime: {seen[f'validate:{runtime}']}")
        check("role_runtime_assignments_runtime_check" in seen[f"assign:{runtime}"].get("refused", ""),
              f"[{name}] {runtime!r} cannot be assigned: {seen[f'assign:{runtime}']}")
        check("unknown runtime" in seen[f"rebind:{runtime}"].get("refused", "")
              or (runtime == "" and "refused" in seen[f"rebind:{runtime}"]),
              f"[{name}] nothing may be rebound to {runtime!r}: {seen[f'rebind:{runtime}']}")
    check(seen["constraint"] == "CHECK ((runtime = ANY (ARRAY['claude'::text, 'codex'::text, 'agy'::text, "
                                "'hermes'::text, 'copilot'::text])))" and seen["constraints"] == 1,
          f"[{name}] one runtime CHECK, naming exactly the five: {seen['constraint']} ({seen['constraints']})")
    check(seen["applied"] is True and seen["applied_runtime"] == "copilot",
          f"[{name}] the Director's workflow with a copilot main is applied: {seen['applied']}")


def main() -> int:
    for key in [k for k in os.environ if k.startswith(("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL"))]:
        os.environ.pop(key)
    boards: dict = {}
    with temporary_cluster(prefix="syrd634-", shutdown="immediate") as cluster:
        for name in ("schema", "fresh", "upgraded"):
            before = before_tree(cluster.root / "before") if name == "upgraded" else None
            admin = build_board(cluster, f"copilot_{name}", before=before, migrations=name != "schema")
            boards[name] = board_questions(cluster, name, admin)
    for name, seen in boards.items():
        verify(name, seen)
    for fn in FUNCTIONS:
        check(boards["schema"]["functions"][fn] == boards["fresh"]["functions"][fn] == boards["upgraded"]["functions"][fn],
              f"{fn} is schema.sql's own on every board: schema.sql alone, provisioned, and upgraded")
    print(f"copilot_runtime_board_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
