#!/usr/bin/env python3
"""SYRD-784: a fresh `switchyard new` prints none of PostgreSQL's "already exists, skipping" notices; nothing else hides.

The User, creating test13: "also a lot of this" -- `psql:<stdin>:45: NOTICE:  relation "director_rebinds" already
exists, skipping`, hundreds of times. A new board is created from schema.sql, which already holds the latest objects;
the provisioning packet applies schema.sql (whose own `IF NOT EXISTS` steps say it 88 times on a fresh database), then
`ticket-board-migrate` replays every migration over it, and each `CREATE ... IF NOT EXISTS` says it again.

Exactly those notices -- `NOTICE:  ... already exists, skipping` and `... does not exist, skipping` -- are now dropped
from the packet's psql steps and from every psql the migration runner starts. A lower `client_min_messages` was not
used: the pg_cron installer and two migrations report real conditions as NOTICE, and those must still be seen.

Here, on a throwaway cluster:
* schema.sql through the packet's own rendered psql step, and then the real `ticket-board-migrate`, print no
  idempotent-DDL notice; the runner still names each migration it applies and ends with one summary line;
* a migration's own RAISE NOTICE and RAISE WARNING are still shown, an ERROR still stops the run and is printed in
  full, and the failed migration is not recorded;
* the packet's step still fails when psql does;
* the packet and the runner drop the same pattern.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import ticket_board_write_api_test as t  # noqa: E402 - the suites' cluster helpers
from scripts.ticket_board import provision_operator_packet as packet  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

CHECKS = 0
MIGRATE = ROOT / "scripts" / "ticket-board-migrate"
SKIPPING = re.compile(r"NOTICE:  .*(already exists|does not exist), skipping$", re.M)


def check(condition: object, detail: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")


def env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "PGOPTIONS"))}


def packet_step(sql_file: Path, conn: str) -> subprocess.CompletedProcess[str]:
    """The packet's own psql step for `sql_file`, as rendered, run as the packet runs it -- without its sudo."""
    command = packet.postgres_sql_file_command(str(sql_file), database_url=conn)
    command = command.replace("sudo cat ", "cat ", 1).replace("sudo -u postgres ", "", 1)
    return subprocess.run(["bash", "-c", f"set -euo pipefail\n{command}\n"], capture_output=True, text=True,
                          env=env(), timeout=300)


def migrate(conn: str, migrations: Path | None = None) -> subprocess.CompletedProcess[str]:
    extra = {"TICKET_BOARD_MIGRATIONS_DIR": str(migrations)} if migrations else {}
    return subprocess.run(["bash", str(MIGRATE)], capture_output=True, text=True, timeout=600,
                          env={**env(), "TICKET_BOARD_ADMIN_DATABASE_URL": conn, **extra})


def database(cluster: object, name: str) -> str:
    t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", name])
    return t.conninfo(cluster.socket_dir, cluster.port, name)


def fresh_board(cluster: object) -> None:
    conn = database(cluster, "fresh")
    if t.psql(conn, "SELECT count(*) FROM pg_roles WHERE rolname = 'director';") == "0":
        t.create_roles(conn)
    schema = packet_step(ROOT / "scripts" / "ticket_board" / "schema.sql", conn)
    check(schema.returncode == 0, f"schema.sql through the packet's step applies: {schema.stderr[-600:]}")
    check(not SKIPPING.search(schema.stderr),
          f"and says none of the 88 'already exists, skipping' notices: {SKIPPING.findall(schema.stderr)[:3]}")
    rbac = packet_step(ROOT / "scripts" / "ticket_board" / "rbac.sql", conn)
    check(rbac.returncode == 0, f"rbac.sql through it too: {rbac.stderr[-400:]}")
    run = migrate(conn)
    check(run.returncode == 0, f"the real ticket-board-migrate succeeds on it: {run.stderr[-800:]}")
    check(not SKIPPING.search(run.stderr),
          f"and prints no idempotent-DDL notice: {SKIPPING.findall(run.stderr)[:3]}")
    applied = [line for line in run.stderr.splitlines() if line.startswith("[ticket-board-migrate] apply ")]
    summary = [line for line in run.stderr.splitlines() if re.fullmatch(
        r"\[ticket-board-migrate\] applied \d+ migration\(s\); \d+ already recorded", line)]
    check(len(applied) > 100 and summary == [f"[ticket-board-migrate] applied {len(applied)} migration(s); "
                                             f"{run.stderr.count('] skip ')} already recorded"],
          f"each migration it applies is still named, and one line says what changed: {len(applied)} {summary}")
    others = [line for line in run.stderr.splitlines() if "NOTICE:" in line]
    check(all("pg_cron" in line and "not installed" in line for line in others),
          f"the only notices left are the pg_cron installer's real ones, where this cluster lacks it: {others}")
    rerun = migrate(conn)
    check(rerun.returncode == 0 and "applied 0 migration(s)" in rerun.stderr and not SKIPPING.search(rerun.stderr),
          f"an upgrade with nothing to apply says so, quietly: {rerun.stderr[-400:]}")


def loud_migrations(cluster: object) -> None:
    conn = database(cluster, "loud")
    with tempfile.TemporaryDirectory(prefix="syrd784.") as raw:
        migrations = Path(raw)
        (migrations / "pgu1_syrd784_quiet_and_kept.sql").write_text(
            "CREATE TABLE IF NOT EXISTS ticket_board.syrd784 (id int);\n"
            "CREATE TABLE IF NOT EXISTS ticket_board.syrd784 (id int);\n"
            "DO $$ BEGIN RAISE NOTICE 'syrd784: a notice a migration means'; "
            "RAISE WARNING 'syrd784: a warning a migration means'; END $$;\n", encoding="utf-8")
        (migrations / "pgu2_syrd784_fails.sql").write_text("SELECT 1 / 0;\n", encoding="utf-8")
        (migrations / "pgu3_syrd784_never.sql").write_text("CREATE TABLE ticket_board.syrd784_never (id int);\n",
                                                           encoding="utf-8")
        run = migrate(conn, migrations)
    check(run.returncode != 0, f"an ERROR still stops the run: {run.returncode}")
    check("ERROR:  division by zero" in run.stderr and "pgu2_syrd784_fails.sql" in run.stderr,
          f"and is printed in full, after the migration it is in: {run.stderr[-600:]}")
    check("syrd784: a notice a migration means" in run.stderr and "syrd784: a warning a migration means" in run.stderr,
          f"a migration's own notice and warning are still shown: {run.stderr[-600:]}")
    check(not SKIPPING.search(run.stderr), f"only the idempotent-DDL notice is dropped: {SKIPPING.findall(run.stderr)}")
    recorded = t.psql(conn, "SELECT string_agg(name, ',' ORDER BY seq) FROM ticket_board.schema_migrations;")
    check("pgu1_syrd784_quiet_and_kept.sql" in recorded and "pgu2" not in recorded and "pgu3" not in recorded,
          f"the failed migration is not recorded and nothing after it ran: {recorded}")
    with tempfile.TemporaryDirectory(prefix="syrd784.") as raw:
        bad = Path(raw) / "bad.sql"
        bad.write_text("SELECT 1 / 0;\n", encoding="utf-8")
        step = packet_step(bad, conn)
    check(step.returncode != 0 and "ERROR:  division by zero" in step.stderr,
          f"the packet's step still fails, and says why, when psql does: {step.returncode} {step.stderr[-300:]}")


def test_a_fresh_board_is_quiet_and_errors_still_stop() -> None:
    saved = {k: os.environ.pop(k) for k in [k for k in os.environ if k.startswith("TICKET_BOARD_")]}
    try:
        with temporary_cluster(prefix="syrd784-", shutdown="immediate") as cluster:
            fresh_board(cluster)
            loud_migrations(cluster)
    finally:
        os.environ.update(saved)


def test_the_packet_and_the_runner_drop_the_same_lines() -> None:
    script = MIGRATE.read_text(encoding="utf-8")
    runner = re.search(r"^readonly IDEMPOTENT_NOTICE='([^']*)'$", script, re.M)
    command = packet.postgres_sql_file_command("/p/x.sql")
    check(runner is not None and f"grep -Ev '{runner.group(1)}'" in command,
          f"one pattern, in the runner and in the packet's step: {runner and runner.group(1)} {command}")
    check(runner.group(1) == SKIPPING.pattern, "and it is the one this suite looks for")
    check('| drop_idempotent_notices >&2' in script, "every psql the runner starts goes through it")


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"migrate_quiet_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
