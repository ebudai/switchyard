#!/usr/bin/env python3
"""SYRD-617: board migrations run in number order past pgu999, and nothing ambiguous runs at all.

`ticket-board-migrate` used a plain `sort -z`, so `pgu1000_...` would have run
before `pgu367_...` -- before the tables and functions it changes existed. Order
is now the number, read by `scripts/ticket_board/migration_order.py`, which the
runner and the test helpers share. Every name in the directory is judged before
the database is touched.

What this proves, with the real runner on real clusters:

* pgu998, pgu999, pgu1000 and pgu1001 run in that order, whatever order the
  files were written in and whatever the locale, and a rerun applies nothing;
* a number used twice -- differently named files, either spelling, or a new
  file joining one of the six historical shared numbers -- is refused, as is a
  malformed or ambiguous name or an entry that is not a regular file, with
  nothing applied and nothing recorded;
* a board the OLD text-order runner built upgrades past pgu999 applying only the
  new files: every historical file in its directory is rigged to fail if run;
* that upgraded board and a fresh install of the same tree have the same
  functions, columns and migration history;
* a failing pgu1000 is reported by name, rolled back and not recorded, nothing
  after it runs, and the next run resumes there.
"""

from __future__ import annotations

import os
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()
import ticket_board_write_api_test as t  # noqa: E402
from scripts.ticket_board import migration_order  # noqa: E402
from scripts.ticket_board.migration_order import MigrationOrderError, ordered, ordered_files  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

RUNNER = ROOT / "scripts/ticket-board-migrate"
MIGRATIONS = ROOT / "scripts/ticket_board/migrations"
SCHEMA = ROOT / "scripts/ticket_board/schema.sql"
CHECKS = 0


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def refusal(names: list[str]) -> str:
    try:
        ordered(names)
    except MigrationOrderError as exc:
        return str(exc)
    return ""


# --- the order, without a database ----------------------------------------------------------


def test_numbers_order_past_999() -> None:
    want = ["pgu998_a.sql", "pgu999_b.sql", "pgu1000_c.sql", "pgu1001_d.sql", "pgu10000_e.sql"]
    shuffled = list(want)
    for seed in range(20):
        random.Random(seed).shuffle(shuffled)
        check(ordered(shuffled) == want, f"by number, from any starting order: {shuffled} -> {ordered(shuffled)}")
    check(sorted(want) != want, "which is not the text order it replaces")
    check(ordered(["pgu1000_c.sql", "287_legacy.sql", "pgu367_x.sql"]) == ["287_legacy.sql", "pgu367_x.sql", "pgu1000_c.sql"],
          "the unprefixed legacy files and pgu files share one number line")


def test_the_tree_keeps_the_order_it_was_built_in() -> None:
    """Every board recorded these by name in text order; a fresh install must still apply them in it."""
    names = [path.name for path in ordered_files(MIGRATIONS)]
    upto_999 = [name for name in names if migration_order.number(name) <= 999]
    check(upto_999 == sorted(upto_999, key=lambda name: name.encode()),
          "through pgu999 the order is exactly the old byte order")
    check(names[:len(upto_999)] == upto_999, "and anything numbered past 999 comes after all of it")
    check(len(upto_999) >= 169, f"the whole history was read: {len(upto_999)}")


def test_a_number_is_one_files() -> None:
    message = refusal(["pgu999_b.sql", "pgu1000_c.sql", "pgu1000_other.sql"])
    check("migration number 1000 is used by more than one file: pgu1000_c.sql, pgu1000_other.sql" in message,
          f"two names, one number, refused by name: {message}")
    message = refusal(["270_ready_to_in_progress.sql", "pgu270_shadow.sql"])
    check("migration number 270 is used by more than one file: 270_ready_to_in_progress.sql, pgu270_shadow.sql" in message,
          f"the two spellings are one number: {message}")
    historical = sorted(migration_order.SHARED_NUMBERS[759])
    message = refusal([*historical, "pgu759_round6_new.sql"])
    check("migration number 759 is used by more than one file" in message and "pgu759_round6_new.sql" in message,
          f"no new file joins a historical shared number: {message}")
    message = refusal(["pgu1000_a.sql", "pgu1000_b.sql", "pgu0999_c.sql", "pgu1001-d.sql"])
    check(all(part in message for part in ("pgu0999_c.sql: the number has a leading zero", "pgu1001-d.sql: a migration must",
                                            "migration number 1000 is used by more than one file")),
          f"every problem is reported at once: {message}")


def test_the_historical_shared_numbers_keep_their_place() -> None:
    for value, names in migration_order.SHARED_NUMBERS.items():
        got = ordered([*sorted(names, reverse=True), f"pgu{value + 1}_after.sql", f"pgu{value - 1}_before.sql"])
        check(got == [f"pgu{value - 1}_before.sql", *sorted(names), f"pgu{value + 1}_after.sql"],
              f"pgu{value}'s files stay together, byte-ordered, between their neighbours: {got}")
        check(ordered([max(names)]) == [max(names)], f"and one of them alone is fine: pgu{value}")
    on_disk = {path.name for path in MIGRATIONS.glob("*.sql")}
    shared = {name for names in migration_order.SHARED_NUMBERS.values() for name in names}
    check(shared <= on_disk, f"each historical shared name is a real file: {sorted(shared - on_disk)}")


def test_malformed_and_ambiguous_names_are_refused() -> None:
    cases = {
        "pgu0999_x.sql": "leading zero, so it is ambiguous",
        "01000_x.sql": "leading zero, so it is ambiguous",
        "pgu00_x.sql": "leading zero, so it is ambiguous",
        "PGU1000_x.sql": "must be named pgu<number>",
        "pgu1000_X.sql": "must be named pgu<number>",
        "pgu1000-x.sql": "must be named pgu<number>",
        "pgu1000.sql": "must be named pgu<number>",
        "pgu_x.sql": "must be named pgu<number>",
        "pgu1000_x.sql.sql": "must be named pgu<number>",
        "pgu+1000_x.sql": "must be named pgu<number>",
        "pgu 1000_x.sql": "must be named pgu<number>",
        "pgu１０００_x.sql": "must be named pgu<number>",
        "pgu1000_.sql": "must be named pgu<number>",
        "x1000_y.sql": "must be named pgu<number>",
    }
    for name, why in cases.items():
        message = refusal(["pgu999_b.sql", name])
        check(message.startswith(f"{name}: ") and why in message, f"{name!r} is refused ({why}): {message!r}")
    check(ordered(["pgu0_zero.sql", "pgu1_one.sql"]) == ["pgu0_zero.sql", "pgu1_one.sql"], "0 itself has no leading zero")


def test_entries_that_are_not_regular_files_are_refused() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd617-entries.") as scratch:
        directory = Path(scratch)
        (directory / "pgu999_b.sql").write_text("SELECT 1;\n")
        (directory / "README").write_text("not a migration\n")
        (directory / "notes.txt").write_text("not a migration\n")
        check([path.name for path in ordered_files(directory)] == ["pgu999_b.sql"], "only *.sql entries are migrations")
        (directory / "pgu1000_link.sql").symlink_to(directory / "pgu999_b.sql")
        (directory / "pgu1001_dir.sql").mkdir()
        try:
            ordered_files(directory)
            message = ""
        except MigrationOrderError as exc:
            message = str(exc)
        check(message == "not a regular file: pgu1000_link.sql, pgu1001_dir.sql",
              f"a link or a directory named like a migration is refused, not skipped: {message!r}")


def test_the_command_line_is_the_runners_interface() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd617-cli.") as scratch:
        directory = Path(scratch)
        for name in ("pgu1001_d.sql", "pgu998_a.sql", "pgu1000_c.sql", "pgu999_b.sql"):
            (directory / name).write_text("SELECT 1;\n")
        outputs = set()
        for locale in ("C", "C.UTF-8", "en_US.UTF-8", "tr_TR.UTF-8"):
            run = subprocess.run(["python3", "-I", str(ROOT / "scripts/ticket_board/migration_order.py"), str(directory)],
                                 capture_output=True, text=True, env={"PATH": os.environ["PATH"], "LC_ALL": locale})
            check(run.returncode == 0, f"{locale}: {run.stderr}")
            outputs.add(run.stdout)
        check(outputs == {"pgu998_a.sql\npgu999_b.sql\npgu1000_c.sql\npgu1001_d.sql\n"},
              f"one name per line, by number, in every locale: {outputs}")
        (directory / "pgu1000_again.sql").write_text("SELECT 1;\n")
        run = subprocess.run(["python3", "-I", str(ROOT / "scripts/ticket_board/migration_order.py"), str(directory)],
                             capture_output=True, text=True)
        check(run.returncode == 1 and run.stdout == ""
              and f"refusing to order {directory}: migration number 1000 is used by more than one file" in run.stderr,
              f"a refusal prints nothing to apply and says why: {run.returncode} {run.stdout!r} {run.stderr!r}")


# --- the real runner ----------------------------------------------------------------------------


def conninfo(cluster, db: str) -> str:
    return f"host={cluster.socket_dir} port={cluster.port} dbname={db} user=postgres"


def createdb(cluster, db: str) -> str:
    subprocess.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", db],
                   check=True, capture_output=True)
    return conninfo(cluster, db)


def psql(conn: str, sql: str) -> str:
    run = subprocess.run(["psql", "-X", "-v", "ON_ERROR_STOP=1", "-tA", conn], input=sql, text=True, capture_output=True)
    assert run.returncode == 0, run.stderr
    return run.stdout.strip()


def migrate(conn: str, directory: Path, runner: Path = RUNNER, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(runner)], capture_output=True, text=True, timeout=900,
                          env={"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/"), "LANG": "C.UTF-8",
                               "TICKET_BOARD_ADMIN_DATABASE_URL": conn, "TICKET_BOARD_MIGRATIONS_DIR": str(directory), **env})


def applied(run: subprocess.CompletedProcess) -> list[str]:
    return [line.split("apply ", 1)[1] for line in run.stderr.splitlines() if line.startswith("[ticket-board-migrate] apply ")]


def history(conn: str) -> list[str]:
    return psql(conn, "SELECT name FROM ticket_board.schema_migrations ORDER BY seq;").splitlines()


def chained(step: int, previous: int | None) -> str:
    """A migration that fails unless `previous` ran first, then leaves its own mark."""
    guard = "" if previous is None else (
        f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM ticket_board.order_probe WHERE step = {previous}) THEN "
        f"RAISE EXCEPTION 'pgu{step} ran before pgu{previous}'; END IF; END $$;\n")
    return ("BEGIN;\nCREATE SCHEMA IF NOT EXISTS ticket_board;\n"
            "CREATE TABLE IF NOT EXISTS ticket_board.order_probe (step int PRIMARY KEY, at timestamptz DEFAULT clock_timestamp());\n"
            f"{guard}INSERT INTO ticket_board.order_probe(step) VALUES ({step});\nCOMMIT;\n")


def test_the_runner_applies_past_999_in_number_order(cluster, scratch: Path) -> None:
    directory = scratch / "ordered"
    directory.mkdir()
    steps = [(998, None), (999, 998), (1000, 999), (1001, 1000)]
    for step, previous in reversed(steps):  # written newest first: the filesystem's order is not the runner's
        (directory / f"pgu{step}_order_step.sql").write_text(chained(step, previous))
    conn = createdb(cluster, "ordered")
    for label, locale in (("first", "C"), ("again", "tr_TR.UTF-8")):
        run = migrate(conn, directory, LC_ALL=locale)
        check(run.returncode == 0, f"{label} run succeeds: {run.stderr}")
        if label == "first":
            check(applied(run) == [f"pgu{s}_order_step.sql" for s, _ in steps], f"applied 998, 999, 1000, 1001: {applied(run)}")
        else:
            check(applied(run) == [], f"a rerun applies nothing: {run.stderr}")
    check(psql(conn, "SELECT string_agg(step::text, ',' ORDER BY at) FROM ticket_board.order_probe;") == "998,999,1000,1001",
          "each ran once, after the one before it")
    check(history(conn) == [f"pgu{s}_order_step.sql" for s, _ in steps], f"and history records them in that order: {history(conn)}")


def test_nothing_ambiguous_is_applied(cluster, scratch: Path) -> None:
    cases = {
        "duplicate": ("pgu1000_c.sql", "pgu1000_other.sql", "migration number 1000 is used by more than one file"),
        "spellings": ("pgu999_b.sql", "999_other.sql", "migration number 999 is used by more than one file"),
        "leading-zero": ("pgu0999_c.sql", None, "pgu0999_c.sql: the number has a leading zero"),
        "malformed": ("pgu1000_Upper.sql", None, "pgu1000_Upper.sql: a migration must be named"),
    }
    for index, (label, (bad, second, why)) in enumerate(cases.items()):
        directory = scratch / f"refused-{label}"
        directory.mkdir()
        (directory / "pgu998_a.sql").write_text(chained(998, None))
        (directory / bad).write_text(chained(1000, None))
        if second:
            (directory / second).write_text(chained(1001, None))
        conn = createdb(cluster, f"refused_{index}")
        run = migrate(conn, directory)
        check(run.returncode != 0 and why in run.stderr and "no migration was applied" in run.stderr,
              f"{label}: refused with its reason: {run.returncode} {run.stderr}")
        check(psql(conn, "SELECT to_regnamespace('ticket_board') IS NULL;") == "t",
              f"{label}: before the database was touched -- not even the tracking table, nor the valid pgu998")
    # On a board that already has history, a refusal changes none of it.
    directory = scratch / "refused-on-a-board"
    directory.mkdir()
    (directory / "pgu998_a.sql").write_text(chained(998, None))
    conn = createdb(cluster, "refused_board")
    check(migrate(conn, directory).returncode == 0, "the board takes pgu998")
    before = psql(conn, "SELECT string_agg(name || seq || applied_at, ',' ORDER BY seq) FROM ticket_board.schema_migrations;")
    (directory / "pgu999_b.sql").write_text(chained(999, 998))
    (directory / "pgu1000_c.sql").write_text(chained(1000, 999))
    (directory / "pgu1000_c2.sql").write_text(chained(1000, 999))
    run = migrate(conn, directory)
    check(run.returncode != 0 and applied(run) == [], f"refused, applying nothing: {run.stderr}")
    check(psql(conn, "SELECT string_agg(name || seq || applied_at, ',' ORDER BY seq) FROM ticket_board.schema_migrations;") == before
          and psql(conn, "SELECT count(*) FROM ticket_board.order_probe;") == "1",
          "not even the valid pgu999 ran, and the history is unchanged")


def test_a_failed_migration_is_reported_rolled_back_and_resumed(cluster, scratch: Path) -> None:
    directory = scratch / "recovery"
    directory.mkdir()
    (directory / "pgu998_a.sql").write_text(chained(998, None))
    (directory / "pgu999_b.sql").write_text(chained(999, 998))
    (directory / "pgu1000_c.sql").write_text(
        "BEGIN;\nCREATE TABLE ticket_board.half_made (id int);\nSELECT 1 / 0;\nCOMMIT;\n")
    (directory / "pgu1001_d.sql").write_text(chained(1001, 1000))
    conn = createdb(cluster, "recovery")
    run = migrate(conn, directory)
    check(run.returncode != 0 and applied(run) == ["pgu998_a.sql", "pgu999_b.sql", "pgu1000_c.sql"]
          and "division by zero" in run.stderr,
          f"the failure is reported at pgu1000, by name and cause: {run.returncode} {run.stderr}")
    check(history(conn) == ["pgu998_a.sql", "pgu999_b.sql"], f"pgu1000 is not recorded, so it is retried: {history(conn)}")
    check(psql(conn, "SELECT to_regclass('ticket_board.half_made') IS NULL;") == "t", "its transaction was rolled back")
    check(psql(conn, "SELECT string_agg(step::text, ',' ORDER BY step) FROM ticket_board.order_probe;") == "998,999",
          "and pgu1001 never ran")
    (directory / "pgu1000_c.sql").write_text(chained(1000, 999))
    run = migrate(conn, directory)
    check(run.returncode == 0 and applied(run) == ["pgu1000_c.sql", "pgu1001_d.sql"],
          f"the next run resumes at pgu1000 and applies nothing twice: {run.stderr}")
    check(history(conn) == ["pgu998_a.sql", "pgu999_b.sql", "pgu1000_c.sql", "pgu1001_d.sql"], f"history: {history(conn)}")


def test_history_recorded_at_one_moment_keeps_its_order(cluster, scratch: Path) -> None:
    """Rows recorded by one statement share a time; renumbering must keep their order, not re-sort their names."""
    directory = scratch / "one-moment"
    directory.mkdir()
    steps = [998, 999, 1000, 1001]
    for step in steps:
        (directory / f"pgu{step}_moment.sql").write_text(chained(step, None if step == 998 else step - 1))
    conn = createdb(cluster, "one_moment")
    psql(conn, "CREATE SCHEMA ticket_board; CREATE SEQUENCE ticket_board.schema_migrations_seq AS bigint; "
               "CREATE TABLE ticket_board.schema_migrations (name text PRIMARY KEY, "
               "seq bigint NOT NULL DEFAULT nextval('ticket_board.schema_migrations_seq'), "
               "applied_at timestamptz NOT NULL DEFAULT now()); "
               "CREATE UNIQUE INDEX schema_migrations_seq_idx ON ticket_board.schema_migrations (seq); "
               "INSERT INTO ticket_board.schema_migrations(name) VALUES "
               + ", ".join(f"('pgu{step}_moment.sql')" for step in steps) + ";")
    run = migrate(conn, directory)
    check(run.returncode == 0 and applied(run) == [], f"the runner keeps a history recorded at one moment: {run.stderr[-800:]}")
    check(history(conn) == [f"pgu{step}_moment.sql" for step in steps], f"in the order it was recorded: {history(conn)}")


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True, text=True).stdout


def text_order_runner(scratch: Path) -> Path:
    """The runner as it was before SYRD-617, out of the commit before the one that added migration_order.py."""
    adding = git("log", "--format=%H", "--diff-filter=A", "--", "scripts/ticket_board/migration_order.py").split()
    source = git("show", f"{adding[-1]}^:scripts/ticket-board-migrate" if adding else "HEAD:scripts/ticket-board-migrate")
    check("sort -z" in source and "migration_order" not in source, "the old runner is the text-order one")
    runner = scratch / "old/scripts/ticket-board-migrate"
    runner.parent.mkdir(parents=True)
    runner.write_text(source)
    runner.chmod(0o755)
    return runner


def probes() -> dict[str, str]:
    """Two probes numbered after the newest real migration, each failing unless the one before it was recorded,
    so running early is an error, not a quiet difference. (pgu1000 itself became a real migration, SYRD-626.)"""
    newest = ordered_files(MIGRATIONS)[-1].name
    first, second = migration_order.number(newest) + 1, migration_order.number(newest) + 2
    made, before = {}, newest
    for value in (first, second):
        name = f"pgu{value}_syrd617_probe.sql"
        made[name] = ("DO $$ BEGIN IF NOT EXISTS (SELECT FROM ticket_board.schema_migrations "
                      f"WHERE name = '{before}') THEN RAISE EXCEPTION 'pgu{value} ran before {before}'; END IF; END $$;\n"
                      "CREATE OR REPLACE FUNCTION ticket_board.syrd617_probe() RETURNS int LANGUAGE sql "
                      f"AS $f$ SELECT {value} $f$;\n")
        before = name
    return made

CATALOG = """
SELECT string_agg(sig || ' ' || md5(def), E'\\n' ORDER BY sig) FROM (
    SELECT p.oid::regprocedure::text AS sig, pg_get_functiondef(p.oid) AS def
      FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'ticket_board') f;
SELECT string_agg(c, E'\\n' ORDER BY c) FROM (
    SELECT table_name || '.' || column_name || ' ' || data_type || ' ' || coalesce(column_default, '') AS c
      FROM information_schema.columns WHERE table_schema = 'ticket_board') t;
"""


def test_a_historical_board_upgrades_past_999_and_matches_a_fresh_one(cluster, scratch: Path) -> None:
    # The text-order runner only ever saw migrations up to pgu999; everything past it arrives with the upgrade.
    real = ordered_files(MIGRATIONS)
    later = [path for path in real if migration_order.number(path.name) > 999]
    new_files = {**{path.name: path.read_text() for path in later}, **probes()}
    tree = scratch / "tree/migrations"
    tree.mkdir(parents=True)
    for path in real:
        if migration_order.number(path.name) <= 999:
            shutil.copy(path, tree / path.name)
    shutil.copy(SCHEMA, tree.parent / "schema.sql")  # laid out as a release: pgu589 includes ../schema.sql
    old_runner = text_order_runner(scratch)

    historical = createdb(cluster, "historical")
    # Cluster-wide: the roles the board's migrations grant to.
    t.create_roles(historical)
    psql(historical, "CREATE ROLE ticket_board_service LOGIN; CREATE ROLE ticket_board_listener LOGIN;")
    psql(historical, SCHEMA.read_text())
    run = migrate(historical, tree, runner=old_runner)
    check(run.returncode == 0, f"the text-order runner builds the board it built: {run.stderr[-2000:]}")
    # Names and times in sequence order. The values of `seq` are not compared: every run of either runner
    # renumbers them densely, closing the gap pgu589's re-included schema.sql leaves.
    built = psql(historical, "SELECT string_agg(name || '@' || applied_at, ',' ORDER BY seq) "
                             "FROM ticket_board.schema_migrations;")

    # Every historical file now fails if it is run again, and the new ones arrive.
    for path in tree.glob("*.sql"):
        path.write_text(f"DO $$ BEGIN RAISE EXCEPTION 'replayed {path.name}'; END $$;\n" + path.read_text())
    for name, body in new_files.items():
        (tree / name).write_text(body)
    run = migrate(historical, tree)
    check(run.returncode == 0, f"the upgrade past 999 succeeds: {run.stderr[-2000:]}")
    check(applied(run) == list(new_files) and len(new_files) >= 2,
          f"applying exactly what is past 999, in number order, and replaying nothing: {applied(run)}")
    names = ", ".join(f"'{name}'" for name in new_files)
    kept = psql(historical, "SELECT string_agg(name || '@' || applied_at, ',' ORDER BY seq) "
                            f"FROM ticket_board.schema_migrations WHERE name NOT IN ({names});")
    check(kept == built, "the history it had is untouched: same names, order and times: "
          f"{[(a, b) for a, b in zip(built.split(','), kept.split(',')) if a != b][:3]}")
    check(history(historical)[-len(new_files):] == list(new_files), "and the new files are appended to it, in order")
    check(psql(historical, "SELECT ticket_board.syrd617_probe();") == str(migration_order.number(list(new_files)[-1])),
          "the last probe's body is the one left")
    rerun = migrate(historical, tree)
    check(rerun.returncode == 0 and applied(rerun) == [], f"a second upgrade applies nothing: {rerun.stderr[-1000:]}")

    fresh_tree = scratch / "fresh/migrations"
    shutil.copytree(MIGRATIONS, fresh_tree)
    shutil.copy(SCHEMA, fresh_tree.parent / "schema.sql")
    for name, body in probes().items():
        (fresh_tree / name).write_text(body)
    fresh = createdb(cluster, "fresh")
    psql(fresh, SCHEMA.read_text())
    run = migrate(fresh, fresh_tree)
    check(run.returncode == 0, f"a fresh install of the same tree succeeds: {run.stderr[-2000:]}")
    check(applied(run)[-len(new_files):] == list(new_files) and applied(run) == [p.name for p in ordered_files(fresh_tree)
                                                                                  if p.name in applied(run)],
          "applying in number order, everything past 999 last")
    check(history(fresh) == history(historical), "fresh and upgraded boards record the same history, in the same order")
    fresh_catalog, upgraded_catalog = psql(fresh, CATALOG), psql(historical, CATALOG)
    check(fresh_catalog.count("\n") > 300, f"the catalog was read: {fresh_catalog.count(chr(10))} lines")
    check(fresh_catalog == upgraded_catalog, "and have the same functions and columns")


def main() -> int:
    test_numbers_order_past_999()
    test_the_tree_keeps_the_order_it_was_built_in()
    test_a_number_is_one_files()
    test_the_historical_shared_numbers_keep_their_place()
    test_malformed_and_ambiguous_names_are_refused()
    test_entries_that_are_not_regular_files_are_refused()
    test_the_command_line_is_the_runners_interface()
    if not all(shutil.which(tool) for tool in ("initdb", "pg_ctl", "createdb", "psql")):
        print("migration_numeric_order_test: PostgreSQL tools missing; database cases skipped")
        return 1
    with tempfile.TemporaryDirectory(prefix="syrd617.") as scratch_dir, \
            temporary_cluster(prefix="syrd617-order.", shutdown="immediate") as cluster:
        scratch = Path(scratch_dir)
        test_the_runner_applies_past_999_in_number_order(cluster, scratch)
        test_nothing_ambiguous_is_applied(cluster, scratch)
        test_a_failed_migration_is_reported_rolled_back_and_resumed(cluster, scratch)
        test_history_recorded_at_one_moment_keeps_its_order(cluster, scratch)
        test_a_historical_board_upgrades_past_999_and_matches_a_fresh_one(cluster, scratch)
    print(f"migration_numeric_order_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
