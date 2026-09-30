#!/usr/bin/env python3
"""SYRD-530: a deployed board kept PUBLIC execute on every function added since its install.

PostgreSQL lets PUBLIC execute a function it creates. rbac.sql revokes that,
but only `install` runs rbac.sql; `deploy` and `deploy-restart` run the
migration runner alone. Every function a migration added since the board's
last install therefore stayed executable by any role with USAGE on the schema:
on a board last installed before pgu960 and then deployed, the listener's own
login role could execute service-only writers such as release_external_blocker.
Grants that copy "whoever can execute force_move" also handed PostgreSQL's
predefined roles (pg_read_all_data, pg_monitor, ...) service-only writers on a
fresh board, because they ran while PUBLIC still held force_move.

The same gap ran the other way: a grant rbac.sql gained after a board's
install never reached it. A board installed before SYRD-517 gave its listener
no execute on ticket_turn_is_resolved, and after a deploy the listener raised
"permission denied" on the first unresolved-turn notice.

A release's database step is now its migrations and then its own rbac.sql,
which is one transaction; the runner also makes rbac.sql's schema-scoped
PUBLIC revocation itself, so a failed migration leaves nothing open either.
Everything here goes through the service script's own database verbs:
`ensure-migrations` is what deploy and deploy-restart do to the database, and
install adds `ensure-roles`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

import ticket_board_write_api_test as t  # noqa: E402
from schema_function_drift import rbac_before, schema_before  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

MIGRATIONS = ROOT / "scripts/ticket_board/migrations"
#: The board is installed just before this migration and deployed past it.
INSTALLED_BEFORE = MIGRATIONS / "pgu960_syrd270_external_blockers.sql"
ROLES = ("ticket_board_service", "ticket_board_listener", "director", "main", "app", "ops", "audit",
         "inspector", "user", "perf", "research")
SERVICE_ONLY_WRITER = "ticket_board.release_external_blocker(text,text,text,text)"
NEW_READER = "ticket_board.serial_reservations()"

CHECKS = 0


def errors(run: subprocess.CompletedProcess) -> list[str]:
    return [line for line in run.stderr.splitlines() if "ERROR" in line or "FATAL" in line][:5] or run.stderr.splitlines()[-3:]


def check(condition: bool, message: str) -> None:
    global CHECKS
    assert condition, message
    CHECKS += 1


def public(admin: str) -> list[str]:
    rows = t.psql(admin, "SELECT coalesce(string_agg(p.oid::regprocedure::text, '|' ORDER BY 1), '') FROM pg_proc p "
                         "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'ticket_board' AND p.prokind = 'f' "
                         "AND (p.proacl IS NULL OR EXISTS (SELECT 1 FROM aclexplode(p.proacl) a WHERE a.grantee = 0));").strip()
    return [row for row in rows.split("|") if row]


def matrix(admin: str) -> dict:
    roles = "ARRAY[" + ",".join(f"'{role}'" for role in ROLES) + "]"
    return json.loads(t.psql(admin, f"""
SELECT coalesce(json_object_agg(p.oid::regprocedure::text, (
    SELECT json_object_agg(r, has_function_privilege(r, p.oid, 'EXECUTE')) FROM unnest({roles}::text[]) r)), '{{}}')::text
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'ticket_board' AND p.prokind = 'f';
""").strip())


def may(admin: str, role: str, function: str) -> bool:
    return t.psql(admin, f"SELECT has_function_privilege('{role}', '{function}', 'EXECUTE')::text;").strip() == "true"


def predefined_grants(admin: str) -> int:
    return int(t.psql(admin, "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace, "
                             "aclexplode(p.proacl) a JOIN pg_roles r ON r.oid = a.grantee "
                             "WHERE n.nspname = 'ticket_board' AND r.oid < 16384 AND r.rolname LIKE 'pg\\_%';").strip())


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="syrd530.") as scratch, \
            temporary_cluster(prefix="syrd530-deploy.", shutdown="immediate") as cluster:
        scratch = Path(scratch)
        board_root = scratch / "board"
        board_root.mkdir()
        (board_root / "current").symlink_to(ROOT)
        # Laid out as a release is: pgu589 includes ../schema.sql, the release's own.
        (scratch / "at-install").mkdir()
        (scratch / "at-install/schema.sql").write_text(schema_before(INSTALLED_BEFORE))
        earlier = scratch / "at-install/migrations"
        earlier.mkdir()
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name < INSTALLED_BEFORE.name:
                shutil.copy(path, earlier / path.name)

        def service(verb: str, admin: str, **env: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                ["bash", str(ROOT / "scripts/ticket-board-service.sh"), verb], capture_output=True, text=True, timeout=900,
                env={"PATH": os.environ["PATH"], "HOME": str(scratch), "LANG": "C.UTF-8", "BOARD_ROOT": str(board_root),
                     "TICKET_BOARD_ADMIN_DATABASE_URL": admin, **env})

        def board(db: str) -> str:
            admin = t.conninfo(cluster.socket_dir, cluster.port, db)
            t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", db])
            return admin

        def installed_before(db: str) -> str:
            """A board installed just before INSTALLED_BEFORE: that schema, its migrations, that rbac.sql."""
            admin = board(db)
            t.psql(admin, schema_before(INSTALLED_BEFORE))
            try:
                t.create_roles(admin)
            except AssertionError as exc:
                if "already exists" not in str(exc):
                    raise
            t.psql(admin, rbac_before(INSTALLED_BEFORE))
            # That era's database step: its migrations, then its rbac.sql -- not today's.
            run = subprocess.run(["bash", str(ROOT / "scripts/ticket-board-migrate")], capture_output=True, text=True,
                                 timeout=900, env={"PATH": os.environ["PATH"], "HOME": str(scratch), "LANG": "C.UTF-8",
                                                   "TICKET_BOARD_ADMIN_DATABASE_URL": admin,
                                                   "TICKET_BOARD_MIGRATIONS_DIR": str(earlier)})
            check(run.returncode == 0, f"{db}: the install-time migrations apply: {errors(run)}")
            t.psql(admin, rbac_before(INSTALLED_BEFORE))
            check(public(admin) == [], f"{db}: an installed board starts with nothing PUBLIC-executable")
            return admin

        # 1. DEPLOY. The database step of deploy and deploy-restart, nothing else.
        deployed = installed_before("deployed")
        run = service("ensure-migrations", deployed)
        check(run.returncode == 0, f"the deploy migrates: {errors(run)}")
        check(public(deployed) == [], f"a deployed board leaves no board function PUBLIC-executable: {public(deployed)}")
        check(not may(deployed, "ticket_board_listener", SERVICE_ONLY_WRITER),
              "the listener cannot execute a service-only writer added since the install")
        check(not may(deployed, "main", NEW_READER), "nor can a pane role execute a function added since the install")
        check(may(deployed, "ticket_board_service", SERVICE_ONLY_WRITER) and may(deployed, "ticket_board_service", NEW_READER),
              "the service keeps the grants its migrations name")
        check(may(deployed, "ticket_board_listener", "ticket_board.ticket_turn_is_resolved(text,timestamptz)"),
              "a grant rbac.sql gained after the install (SYRD-517's) reaches a deployed board")
        deployed_matrix = matrix(deployed)
        # The listener, as it runs, on the deployed board: SYRD-517's delivery check needs that grant.
        from scripts.ticket_board.notify_listener import TicketBoardNotifyListener
        t.seed_postgres_ticket(deployed, "PGU-1", title="Stalled", state="in_progress", assignee="main", commit_exempt=True)
        t.psql(deployed, "DELETE FROM ticket_board.ticket_notification_queue;")
        listener_url = t.conninfo(cluster.socket_dir, cluster.port, "deployed", "ticket_board_listener")
        check(t.psql(listener_url, "SELECT ticket_board.notify_unresolved_turn_end('{\"main\": \"turn-1\"}'::jsonb, "
                                   "clock_timestamp(), interval '10 minutes');").strip() == "1", "an unresolved turn is reported")
        sent: list[str] = []
        TicketBoardNotifyListener(conninfo=listener_url, sender=lambda _target, message: sent.append(message),
                                  activity_gate=lambda _target: False, target_exists=lambda _target: True,
                                  poll_seconds=0, project="pgu").listen_once(max_notifications=5)
        check(any("PGU-1 is still yours" in message for message in sent),
              f"and the deployed board's listener delivers the owner's prompt: {sent}")
        # One transaction: every grant rbac.sql makes is committed by the same one.
        xmins = t.psql(deployed, "SELECT count(DISTINCT xmin::text) FROM pg_proc WHERE oid IN ("
                                 "'ticket_board.recover_stalled_ticket(text,text)'::regprocedure, "
                                 "'ticket_board.transition_target_role(text,text)'::regprocedure, "
                                 "'ticket_board.register_role_runtime(text,text,text,text,text,bigint,bigint,bigint,bigint)'::regprocedure);").strip()
        check(xmins == "1", f"rbac.sql's grants, workflow grants included, land in one transaction: {xmins} transactions")

        # 2. INSTALL. The deployed board lacks no grant an install gives.
        installed = installed_before("installed")
        check([service("ensure-migrations", installed).returncode, service("ensure-roles", installed).returncode] == [0, 0],
              "install's database steps succeed")
        check(public(installed) == [], "an installed board leaves nothing PUBLIC-executable")
        installed_matrix = matrix(installed)
        differences = sorted(f"{fn} / {role}" for fn, roles in installed_matrix.items() for role, allowed in roles.items()
                             if allowed != deployed_matrix.get(fn, {}).get(role))
        check(differences == [], f"a deployed board grants exactly what an installed one does: {differences}")

        # 3. DEPLOY AGAIN. Idempotent.
        check(service("ensure-migrations", deployed).returncode == 0, "a second deploy succeeds")
        check(matrix(deployed) == deployed_matrix, "and changes no privilege")

        # 4. A FAILED DEPLOY. The migration creates a function, then fails.
        (scratch / "failing").mkdir()
        shutil.copy(ROOT / "scripts/ticket_board/schema.sql", scratch / "failing/schema.sql")
        failing = scratch / "failing/migrations"
        shutil.copytree(MIGRATIONS, failing)
        (failing / "pgu999_syrd530_fails_after_creating.sql").write_text(
            "CREATE OR REPLACE FUNCTION ticket_board.syrd530_created_then_failed() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$;\n"
            "SELECT 1 / 0;\n")
        failed = installed_before("failed")
        run = service("ensure-migrations", failed, TICKET_BOARD_MIGRATIONS_DIR=str(failing))
        check(run.returncode != 0, "the failing deploy fails")
        check(t.psql(failed, "SELECT to_regprocedure('ticket_board.syrd530_created_then_failed()') IS NOT NULL;").strip() == "t",
              "having created its function")
        check(public(failed) == [], f"and still leaves nothing PUBLIC-executable, the half-made function included: {public(failed)}")
        check(t.psql(failed, "SELECT count(*) FROM ticket_board.schema_migrations "
                             "WHERE name = 'pgu999_syrd530_fails_after_creating.sql';").strip() == "0",
              "the failed migration is not recorded, so the next deploy retries it")

        # 5. FRESH. Provisioning's schema.sql, then install's database steps.
        fresh = board("fresh")
        t.psql(fresh, (ROOT / "scripts/ticket_board/schema.sql").read_text())
        check([service("ensure-migrations", fresh).returncode, service("ensure-roles", fresh).returncode] == [0, 0],
              "a fresh board installs")
        check(public(fresh) == [], "a fresh board leaves nothing PUBLIC-executable")
        check(predefined_grants(fresh) == 0, "and PostgreSQL's predefined roles hold no execute on board functions")
        check(matrix(fresh) == installed_matrix, "a fresh board grants exactly what an upgraded, installed board grants")

        # 6b. THE LISTENER'S OWN SERVICE SCRIPT deploys the same database step.
        by_listener_script = installed_before("deployed_by_listener_script")
        run = subprocess.run(
            ["bash", str(ROOT / "scripts/ticket-board-notify-listener-service.sh"), "ensure-migrations"], capture_output=True,
            text=True, timeout=900,
            env={"PATH": os.environ["PATH"], "HOME": str(scratch), "LANG": "C.UTF-8", "BOARD_ROOT": str(board_root),
                 "TICKET_BOARD_URL": "http://127.0.0.1:9", "TICKET_BOARD_ADMIN_DATABASE_URL": by_listener_script})
        check(run.returncode == 0, f"the listener service script deploys: {errors(run)}")
        check(matrix(by_listener_script) == installed_matrix and public(by_listener_script) == [],
              "and leaves exactly an installed board's grants")

        # 6. A PINNED RELEASE. deploy-restart runs the database step for the
        #    release it is deploying, before `current` points at it; that
        #    release's rbac.sql is the one applied, not the current one.
        release = scratch / "release"
        (release / "scripts/ticket_board").mkdir(parents=True)
        shutil.copy2(ROOT / "scripts/ticket-board-migrate", release / "scripts/ticket-board-migrate")
        shutil.copy(ROOT / "scripts/ticket_board/schema.sql", release / "scripts/ticket_board/schema.sql")
        shutil.copytree(MIGRATIONS, release / "scripts/ticket_board/migrations")
        marker = "GRANT EXECUTE ON FUNCTION ticket_board.normalize_blocker_ref(text) TO research;\n"
        rbac = (ROOT / "scripts/ticket_board/rbac.sql").read_text()
        check(rbac.endswith("\nCOMMIT;\n"), "rbac.sql ends with its one COMMIT")
        (release / "scripts/ticket_board/rbac.sql").write_text(rbac[: -len("COMMIT;\n")] + marker + "COMMIT;\n")
        check(not may(installed, "research", "ticket_board.normalize_blocker_ref(text)"), "the current rbac.sql grants research nothing here")
        run = subprocess.run(
            ["bash", "-c", f'source "{ROOT}/scripts/ticket-board-service.sh"; apply_database_migrations_for_release "$1"', "_",
             str(release)], capture_output=True, text=True, timeout=900,
            env={"PATH": os.environ["PATH"], "HOME": str(scratch), "LANG": "C.UTF-8", "BOARD_ROOT": str(board_root),
                 "TICKET_BOARD_ADMIN_DATABASE_URL": installed})
        check(run.returncode == 0, f"the release's database step succeeds: {errors(run)}")
        check(may(installed, "research", "ticket_board.normalize_blocker_ref(text)"),
              "and applies that release's rbac.sql, not the current link's")
        check(service("ensure-migrations", installed).returncode == 0, "deploying the current release again")
        check(not may(installed, "research", "ticket_board.normalize_blocker_ref(text)"),
              "takes the board back to the current release's grants")

    print(f"ticket_board_deploy_public_execute_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
