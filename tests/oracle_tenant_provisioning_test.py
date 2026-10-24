#!/usr/bin/env python3
"""SYRD-642: an Oracle tenant's provisioning, without a database (the Oracle-backed parts are in oracle_lifecycle_test).

* **The units.** A plan whose board is on Oracle renders its board, listener
  and canary units with the tenant's Oracle record and nothing of PostgreSQL:
  no PG* settings, database URLs, or ordering on postgresql.service. Each
  reader gets its own configuration. A PostgreSQL plan renders exactly as it
  did.
* **The root packet.** No peer authentication, database SQL, schema.sql,
  ticket-board-migrate, workflow seed or rbac.sql. Instead: the runtime
  credentials copied to the board's and the listener's own accounts, 0600, and
  `ticket-board-oracle` check, migrate and apply-workflow, as root, from the
  release.
* **No silent half-conversion.** If the PostgreSQL template ever loses a line
  the rewrite expects, rendering stops. An Oracle board without a declared
  workflow is refused.
* **The record.**
  * Staging is root-only, and its credentials are 0600.
  * Adoption moves it to `/etc/switchyard/oracle/<project>/` and writes each
    reader's configuration.
  * A second adoption for the same project is refused.
  * A run that stops before adopting leaves no credential behind.
* **The deploy script.** On an Oracle tenant it migrates with root's bootstrap
  record, skips rbac.sql, and gives the canary the Oracle record.
* **Teardown** removes only the local record, never the remote database, and
  needs no ticket count.
* **Unsupported operations.** Role add and VCS close role refuse an Oracle
  tenant, changing nothing.
* **`switchyard new`'s first step** refuses an Oracle board without a
  declared workflow, or without a connection when nobody can be asked, and
  refuses a database the host was not installed for.
"""

from __future__ import annotations

import dataclasses
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CHECKS = 0
POSTGRES_WORDS = ("PGHOST", "PGDATABASE", "PGUSER", "DATABASE_URL", "postgresql", "psql", "ticket-board-migrate",
                  "rbac.sql", "schema.sql", "--apply-peer-auth")


def outcome(call):
    """What a call gives, or what it raised: a check compares either, so a crash is a failed check, not a kill."""
    try:
        return call()
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def plan(root: Path, *, oracle: bool, workflow: bool = True):
    from scripts.ticket_board import project_provision as pp

    built = pp.build_plan(project="demo", owner_user="demo-agent", owner_home=Path("/home/demo-agent"), port=34642)
    document = json.loads((ROOT / "examples/workflows/inspection.json").read_text()) if workflow else None
    return dataclasses.replace(built, database_backend="oracle" if oracle else "postgresql", workflow=document)


def rendering(root: Path) -> None:
    from scripts.ticket_board import project_provision as pp
    from scripts.ticket_board import provision_oracle

    os.environ[provision_oracle.ROOT_ENV] = str(root / "oracle")
    record = root / "oracle" / "demo"
    pg, ora = plan(root, oracle=False), plan(root, oracle=True)
    for name, render in (("board", pp.render_board_unit), ("listener", pp.render_listener_unit),
                         ("canary", pp.render_canary_unit)):
        before, after = render(pg), render(ora)
        check(before == render(dataclasses.replace(pg, database_backend="postgresql")) and "PGHOST" in before
              or name == "canary", f"{name}: a PostgreSQL plan renders as it did")
        # The canary's command passes TICKET_BOARD_DATABASE_URL along by name from canary.env; with the Oracle
        # record's TICKET_BOARD_BACKEND=oracle there too, the board never reads it.
        found = [w for w in POSTGRES_WORDS if w in after and not (name == "canary" and w == "DATABASE_URL")]
        check(found == [], f"{name}: the Oracle unit names nothing of PostgreSQL: {found}")
        if name != "canary":
            check(f"Environment=TICKET_BOARD_BACKEND=oracle\nEnvironment=TICKET_BOARD_ORACLE_CONFIG={record}/{name}.json\n"
                  in after, f"{name}: the unit runs on its own Oracle configuration")
        changed = {line for line in before.splitlines()} ^ {line for line in after.splitlines()}
        check(all(any(w in line for w in ("PG", "DATABASE_URL", "postgresql", "TICKET_BOARD_BACKEND",
                                          "TICKET_BOARD_ORACLE_CONFIG", "After=")) for line in changed),
              f"{name}: only the database lines differ: {sorted(changed)}")
    packet_pg, packet = pp.render_operator_commands(pg), pp.render_operator_commands(ora)
    found = [w for w in POSTGRES_WORDS if w in packet and w not in ("postgresql",)]
    check(found == [] and "sudo -u postgres" not in packet,
          f"the Oracle packet runs nothing of PostgreSQL: {found}")
    q = pp.shell_quote
    cli = q(f"{pg.board_current}/scripts/ticket-board-oracle")
    boot = q(f"{record}/bootstrap.json")
    for expected in (f"sudo install -m 0600 -o {q(pg.service_user)} -g {q(pg.service_user)} "
                     f"{q(f'{record}/runtime.credentials')} {q(f'{record}/board-runtime.credentials')}",
                     f"sudo install -m 0600 -o {q(pg.owner_user)} -g {q(pg.owner_user)} "
                     f"{q(f'{record}/runtime.credentials')} {q(f'{record}/listener-runtime.credentials')}",
                     f"sudo install -m 0600 -o {q(pg.owner_user)} -g {q(pg.owner_user)} "
                     f"{q(f'{record}/bootstrap.credentials')} {q(f'{record}/owner-bootstrap.credentials')}",
                     f"{cli} check --config {boot} --before-migrate\n",
                     f"{cli} migrate --config {boot} --project 'demo' --ticket-prefix {q(pg.ticket_prefix)}\n",
                     f"{cli} apply-workflow --config {boot} --project 'demo' "
                     '--document "$provision_dir"/\'workflow.json\'\n'):
        check(expected in packet, f"the Oracle packet: {expected}")
    check("psql" in packet_pg and "--apply-peer-auth" in packet_pg, "the PostgreSQL packet is as it was")
    try:
        pp.render_operator_commands(plan(root, oracle=True, workflow=False))
        said = "rendered"
    except SystemExit as exc:
        said = str(exc)
    check("demo's board is on Oracle, which runs a declared workflow: create it with --workflow-config" in said,
          f"no declared workflow, no Oracle packet: {said}")
    try:
        provision_oracle.for_database(ora, "board", "[Unit]\nAfter=network.target\n")
        said = "rewrote"
    except SystemExit as exc:
        said = str(exc)
    check("the PostgreSQL template changed and this rewrite must follow it" in said,
          f"a template that lost a rewritten line stops the rendering: {said}")


def staging(root: Path) -> None:
    from scripts.ticket_board import provision_oracle

    os.environ[provision_oracle.ROOT_ENV] = str(root / "stage-root")
    staged = provision_oracle.stage({"host": "10.0.0.5", "port": 1522, "sid": "ORCL", "schema": "BOARD_OWNER",
                                     "password": "never"}, ("RUN", "Secret1"), ("OWN", "Secret2"))
    modes = {p.name: stat.S_IMODE(p.stat().st_mode) for p in staged.iterdir()}
    check(stat.S_IMODE(staged.stat().st_mode) == 0o700 and modes["runtime.credentials"] == 0o600
          and modes["bootstrap.credentials"] == 0o600 and "password" not in (staged / "endpoint.json").read_text(),
          f"staging is root-only, its credentials 0600, and only endpoint fields are kept: {modes}")
    check(provision_oracle.adopt_staged("demo") and not staged.exists(), "adoption moves the staging")
    final = root / "stage-root" / "demo"
    board = json.loads((final / "board.json").read_text())
    boot = json.loads((final / "bootstrap.json").read_text())
    check(board["runtime_credentials_file"] == str(final / "board-runtime.credentials")
          and json.loads((final / "listener.json").read_text())["runtime_credentials_file"]
          == str(final / "listener-runtime.credentials")
          and boot["runtime_credentials_file"] == str(final / "runtime.credentials")
          and boot["bootstrap_credentials_file"] == str(final / "bootstrap.credentials")
          and json.loads((final / "owner.json").read_text())["runtime_credentials_file"]
          == str(final / "listener-runtime.credentials")
          and json.loads((final / "owner.json").read_text())["bootstrap_credentials_file"]
          == str(final / "owner-bootstrap.credentials")
          and board["sid"] == "ORCL" and board["port"] == 1522 and provision_oracle.is_oracle("demo"),
          f"each reader has its own configuration, with final paths: {board}")
    provision_oracle.stage({"host": "h", "port": 1, "service_name": "S", "schema": "S"}, ("A", "b"), ("C", "d"))
    try:
        provision_oracle.adopt_staged("demo")
        said = "adopted"
    except (SystemExit, OSError) as exc:  # a refusal, or (wrongly) a failed attempt over the first record
        said = str(exc)
    check("root already holds an Oracle board record for demo" in said, f"a second adoption is refused: {said}")
    os.environ.pop(provision_oracle.STAGED_ENV, None)
    done = subprocess.run([sys.executable, "-c", (
        "import os, sys; sys.path.insert(0, sys.argv[1]); from scripts.ticket_board import provision_oracle as p; "
        "s = p.stage({'host': 'h', 'port': 1, 'sid': 'X', 'schema': 'S'}, ('A', 'b'), ('C', 'd')); print(s)"),
        str(ROOT)], capture_output=True, text=True, env={**os.environ, provision_oracle.ROOT_ENV: str(root / "exit")})
    left = Path(done.stdout.strip())
    check(done.returncode == 0 and str(left).startswith(str(root / "exit")) and not left.exists(),
          f"a run that stops before adopting leaves no credential behind: {done.stdout} {done.stderr[-300:]}")


def deploy_script(root: Path) -> None:
    """The deploy script's Oracle migration and canary environment, run as its own functions."""
    record = root / "deploy" / "demo"
    record.mkdir(parents=True)
    (record / "board.json").write_text("{}")
    for name in ("bootstrap.credentials", "owner-bootstrap.credentials"):
        (record / name).write_text("{}")
    log = root / "deploy" / "calls.log"
    python = root / "deploy" / "python"
    # `status` answers with the pending count the case sets; `-c` is the script's own JSON parse, run for real.
    python.write_text(f'#!/bin/sh\n[ "$1" = -c ] && exec {sys.executable} "$@"\necho "$@" >> {log}\n'
                      'case "$*" in *" status "*) [ -n "$STATUS" ] || exit 1; echo "$STATUS";; esac\n')
    python.chmod(0o755)
    canary_env = root / "deploy" / "canary.env"

    def deploy(status: str = "", *, as_root: bool = False) -> tuple[subprocess.CompletedProcess[str], str]:
        log.unlink(missing_ok=True)
        program = f'''
set -euo pipefail
export SWITCHYARD_ORACLE_TENANT_ROOT={root / "deploy"} TICKET_BOARD_PROJECT=demo TICKET_BOARD_PYTHON={python}
export BOARD_CANARY_ENV_FILE={canary_env} BOARD_ROOT={root / "deploy"} SOURCE_REPO={ROOT}
export TICKET_BOARD_COMMIT_GIT_DIR={root / "deploy" / "commits"}  # the header's own resolver is not what is tested
source <(sed -n '1,/^usage() {{/p' {ROOT}/scripts/ticket-board-service.sh | sed '$d')
eval "$(sed -n '/^die() {{/,/^}}/p; /^log() {{/,/^}}/p; /^apply_database_migrations_for_release() {{/,/^}}/p; /^ensure_database_roles() {{/,/^}}/p' {ROOT}/scripts/ticket-board-service.sh)"
eval "$(sed -n '/^systemd_env_value() {{/,/^}}/p; /^write_canary_env_file() {{/,/^}}/p' {ROOT}/scripts/ticket-board-service-health.sh)"
echo "backend=$TICKET_BOARD_BACKEND config=$TICKET_BOARD_ORACLE_CONFIG"
apply_database_migrations_for_release /release
ensure_database_roles
write_canary_env_file /release 1 /s /f /l /a
'''
        # Root's cases as namespace root, where `id -u` says 0: the script chooses its configuration by it.
        done = subprocess.run([*(["unshare", "-r"] if as_root else []), "bash", "-c", program],
                              capture_output=True, text=True, timeout=60,
                              env={**{k: v for k, v in os.environ.items() if not k.startswith("TICKET_BOARD")},
                                   "STATUS": status})
        return done, log.read_text() if log.exists() else ""

    migrate = "/release/scripts/ticket-board-oracle migrate --config {} --project demo --ticket-prefix DEMO"
    reading = "/release/scripts/ticket-board-oracle status --config {} --json"
    owner, rooted = f"{record}/owner.json", f"{record}/bootstrap.json"

    # The tenant owner: deploy-release and deploy-restart run the deploy as the owner (SYRD-645).
    done, calls = deploy()
    env = canary_env.read_text() if canary_env.exists() else ""
    check(done.returncode == 0 and f"backend=oracle config={record}/board.json" in done.stdout,
          f"the deploy script recognises root's Oracle record: {done.stdout[-300:]} {done.stderr[-600:]}")
    check(calls.strip() == migrate.format(owner),
          f"as the owner it migrates with the owner's own copies, nothing else: {calls}")
    check("TICKET_BOARD_BACKEND=oracle\n" in env and f'TICKET_BOARD_ORACLE_CONFIG="{record}/board.json"\n' in env,
          f"the canary runs on the Oracle record: {env}")
    done, calls = deploy(as_root=True)
    check(done.returncode == 0 and calls.strip() == migrate.format(rooted),
          f"as root it migrates with root's record: {calls} {done.stderr[-300:]}")

    # Without a schema-owner copy, a deploy migrates nothing and reads the schema with its runtime copy.
    (record / "owner-bootstrap.credentials").unlink()
    done, calls = deploy('{"steps_pending": 0, "tickets": 3}')
    check(done.returncode == 0 and calls.strip() == reading.format(owner)
          and "nothing was migrated" in done.stderr + done.stdout,
          f"the owner without its copy, schema current: read, nothing migrated: {calls} {done.stderr[-400:]}")
    done, calls = deploy('{"steps_pending": 2}')
    check(done.returncode != 0 and calls.strip() == reading.format(owner) and "lacks 2 migration step(s)" in done.stderr
          and f"Root applies them with: sudo {migrate.format(rooted)}; then deploy again" in done.stderr,
          f"steps pending, as the owner without its copy: refused, naming what root runs: {calls} {done.stderr[-500:]}")
    (record / "bootstrap.credentials").unlink()
    done, calls = deploy('{"steps_pending": 2}', as_root=True)
    check(done.returncode != 0 and calls.strip() == reading.format(rooted)
          and "The schema owner applies them with: " + migrate.format("<a configuration naming their credentials>")
          + "; then deploy again" in done.stderr,
          f"and as root without root's: refused, naming what the schema owner runs: {calls} {done.stderr[-500:]}")
    done, calls = deploy("")
    check(done.returncode != 0 and "cannot read the Oracle board's schema" in done.stderr and "migrate" not in calls,
          f"a schema that cannot be read refuses the release: {calls} {done.stderr[-300:]}")


def teardown_and_refusals(root: Path) -> None:
    from scripts import team_launcher as launcher
    from scripts.ticket_board import provision_oracle

    os.environ[provision_oracle.ROOT_ENV] = str(root / "teardown")
    (root / "teardown" / "demo").mkdir(parents=True)
    (root / "teardown" / "demo" / "board.json").write_text("{}")
    actions = launcher._switchyard_teardown_actions(plan(root, oracle=True), registry_path=root / "r.json",
                                                    home_base=root / "home", remove_owner_home=False,
                                                    remove_owner_user=False)
    texts = [(a.label, a.command) for a in actions]
    record = root / "teardown" / "demo"
    expected = [(sys.executable, str(provision_oracle.RELEASE_CLI), "teardown", "--execute", "--config",
                 str(record / f"{reader}.json")) for reader in ("listener", "board", "owner", "bootstrap")]
    expected.append(("rm", "-rf", "--", str(record)))
    oracle = [(d, c) for d, c in texts if "Oracle" in d]
    check(not any("drop PostgreSQL database" in d or "DROP DATABASE" in " ".join(c) for d, c in texts)
          and [c for _, c in oracle] == expected
          and all("remote Oracle database, its schema and its rows are kept" in d for d, _ in oracle[:4]),
          f"teardown runs ticket-board-oracle teardown --execute on each configuration, then removes the record, "
          f"and drops nothing remote: {oracle}")
    pg_actions = launcher._switchyard_teardown_actions(dataclasses.replace(plan(root, oracle=False), project="other"),
                                                       registry_path=root / "r.json", home_base=root / "home",
                                                       remove_owner_home=False, remove_owner_user=False)
    check(any("drop PostgreSQL database" in a.label for a in pg_actions), "a PostgreSQL tenant's teardown is as it was")
    try:
        provision_oracle.refuse_unsupported("demo", "adding a role")
        said = "allowed"
    except SystemExit as exc:
        said = str(exc)
    check("adding a role is not supported yet for demo, whose board is on Oracle" in said and "nothing was changed" in said,
          f"role changes refuse an Oracle tenant: {said}")
    provision_oracle.refuse_unsupported("other", "adding a role")
    check(True, "and leave a PostgreSQL tenant alone")


def records_decide(root: Path) -> None:
    """Root's record keeps a tenant on Oracle wherever a fresh plan would say PostgreSQL; teardown counts no tickets."""
    from types import SimpleNamespace
    from scripts import team_launcher as launcher
    from scripts.ticket_board import project_provision as pp
    from scripts.ticket_board import provision_oracle
    from scripts.project_role_add import add_project_role_command
    from scripts.project_vcs_close_role import set_project_vcs_close_role_command

    os.environ[provision_oracle.ROOT_ENV] = str(root / "records")
    (root / "records" / "demo").mkdir(parents=True)
    (root / "records" / "demo" / "board.json").write_text("{}")
    out = root / "rendered"
    pp.write_artifacts(plan(root, oracle=False), out)
    written = json.loads((out / "plan.json").read_text())
    board_unit = (out / plan(root, oracle=False).board_unit).read_text()
    check(written["database_backend"] == "oracle" and "TICKET_BOARD_BACKEND=oracle" in board_unit
          and "PGHOST" not in board_unit,
          "a re-render from a fresh (PostgreSQL) plan keeps a tenant root records on Oracle, in its plan and units")
    counted: list[str] = []
    saved = (launcher._teardown_project_context, launcher._ticket_board_existing_ticket_count)
    try:
        for project in ("demo", "other"):
            launcher._teardown_project_context = lambda project_, **kw: (
                dataclasses.replace(plan(root, oracle=project_ == "demo"), project=project_), None,
                root / "r.json", False)
            launcher._ticket_board_existing_ticket_count = lambda database, *, runner: counted.append(database) or 0
            launcher.switchyard_teardown_command(project, dry_run=True, home_base=root / "home",
                                                 print_func=lambda line: None)
    finally:
        launcher._teardown_project_context, launcher._ticket_board_existing_ticket_count = saved
    check(counted == [plan(root, oracle=False).database],
          f"teardown asks PostgreSQL for a ticket count only for the PostgreSQL tenant: {counted}")
    for command, kwargs in ((add_project_role_command, {"role_name": "extra"}),
                            (set_project_vcs_close_role_command, {"role_name": "ops"})):
        try:
            command(SimpleNamespace(project="demo"), config_path=root / "none.json",
                    runner=lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran a command")), **kwargs)
            said = "went on"
        except BaseException as exc:  # noqa: BLE001 -- the refusal, or (wrongly) whatever going on hit first
            said = f"{type(exc).__name__}: {exc}"
        check("is not supported yet for demo, whose board is on Oracle" in said,
              f"{command.__name__} refuses an Oracle tenant before doing anything: {said}")


def first_step(root: Path) -> None:
    from types import SimpleNamespace
    from scripts import database_capabilities as dc
    from scripts.ticket_board import provision_oracle

    record = root / "databases.json"
    for have, env, args, words in (
            (["oracle"], {}, SimpleNamespace(workflow_config=None, from_artifact=None, yes=False),
             "an Oracle board runs a declared workflow"),
            (["oracle"], {}, SimpleNamespace(workflow_config="w.json", from_artifact=None, yes=False),
             f"set {provision_oracle.NEW_CONFIG_ENV}"),
            (["postgresql"], {provision_oracle.NEW_DATABASE_ENV: "oracle"},
             SimpleNamespace(workflow_config="w.json", from_artifact=None, yes=False),
             "this host was not installed for the Oracle database"),
            (["oracle"], {provision_oracle.NEW_DATABASE_ENV: "postgresql"}, SimpleNamespace(yes=False),
             "installed for the Oracle database only")):
        record.write_text(json.dumps({"databases": have}))
        saved = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            dc.require_new_project_capability(args, interactive=False, path=record,
                                              ask=lambda q: (_ for _ in ()).throw(AssertionError(f"asked {q}")))
            said = "went on"
        except SystemExit as exc:
            said = str(exc)
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        check(words in said and os.environ.get(provision_oracle.STAGED_ENV) is None,
              f"{have} {env}: refused before asking or staging anything: {said}")
    record.write_text(json.dumps({"databases": ["postgresql", "oracle"]}))
    try:
        chosen = dc.require_new_project_capability(SimpleNamespace(yes=False), interactive=False, path=record)
    except SystemExit as exc:
        chosen = str(exc)
    check(chosen == "postgresql", f"a host with both, nobody to ask and nothing given: PostgreSQL, as switchyard new "
          f"always did: {chosen}")
    answers = iter(["3", "1"])
    check(dc.require_new_project_capability(SimpleNamespace(yes=False), interactive=True, path=record,
                                            ask=lambda q: next(answers)) == "postgresql",
          "in a terminal the board database is asked, again after a wrong answer")


def local_teardown(root: Path) -> None:
    """SYRD-645: the teardown steps, really run: the record goes, and nothing is connected to."""
    from scripts.ticket_board import oracle_lifecycle, provision_oracle

    os.environ[provision_oracle.ROOT_ENV] = str(root / "torn")
    record = root / "torn" / "demo"
    record.mkdir(parents=True)
    endpoint = {"host": "192.0.2.1", "port": 1521, "service_name": "FREEPDB1", "schema": "DEMO_OWNER"}
    (record / "endpoint.json").write_text(json.dumps(endpoint))
    for reader, files in (("listener", ("listener-runtime",)), ("board", ("board-runtime",)),
                          ("owner", ("listener-runtime", "owner-bootstrap")), ("bootstrap", ("runtime", "bootstrap"))):
        for name in files:
            (record / f"{name}.credentials").write_text('{"user": "u", "password": "p"}')
            (record / f"{name}.credentials").chmod(0o600)
        (record / f"{reader}.json").write_text(json.dumps({
            **endpoint, "runtime_credentials_file": str(record / f"{files[0]}.credentials"),
            **({"bootstrap_credentials_file": str(record / f"{files[1]}.credentials")} if len(files) > 1 else {})}))
    said = []
    for label, command in provision_oracle.teardown_steps("demo"):
        done = subprocess.run(command, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        said.append((label, done.returncode, done.stdout + done.stderr))
    check(all(code == 0 for _, code, _ in said) and not record.exists(),
          f"every step succeeds against an address nothing answers on, and the record is gone: {said}")
    check(all("192.0.2.1" in out and "never" not in label for label, _, out in said[:4]),
          f"each configuration's teardown says what it leaves: the database at its endpoint: {said[:1]}")

    # As root, a reader's own file in a directory only root can write is root's to remove; nowhere else.
    def st(mode: int, uid: int) -> os.stat_result:
        return os.stat_result((mode, 0, 0, 1, uid, 0, 0, 0, 0, 0))
    removable = oracle_lifecycle._removable
    record_dir, reader_file = st(stat.S_IFDIR | 0o755, 0), st(stat.S_IFREG | 0o600, 977)
    for case, expected in (
            ((st(stat.S_IFREG | 0o600, 1000), None, 1000), True),
            ((reader_file, record_dir, 1000), False),
            ((reader_file, record_dir, 0), True),
            ((reader_file, st(stat.S_IFDIR | 0o775, 0), 0), False),
            ((reader_file, st(stat.S_IFDIR | 0o757, 0), 0), False),
            ((reader_file, st(stat.S_IFDIR | 0o755, 1000), 0), False),
            ((reader_file, None, 0), False),
            ((st(stat.S_IFLNK | 0o777, 0), record_dir, 0), False),
            ((st(stat.S_IFREG | 0o600, 977), st(stat.S_IFLNK | 0o777, 0), 0), False)):
        check(removable(*case) is expected, f"removable{case} should be {expected}")


def database_status(root: Path) -> None:
    """SYRD-645: `switchyard status`'s Oracle line, read as the asking account may."""
    from scripts import project_status
    from scripts.ticket_board import provision_oracle

    os.environ[provision_oracle.ROOT_ENV] = str(root / "status")
    record = root / "status" / "demo"
    record.mkdir(parents=True)
    (record / "board.json").write_text(json.dumps({
        "host": "db.example", "port": 1521, "sid": "ORCL", "schema": "DEMO_OWNER",
        "runtime_credentials_file": str(record / "board-runtime.credentials")}))
    (record / "listener-runtime.credentials").write_text("{}")
    asked: list[list[str]] = []

    def answering(code: int = 0, out: str = "", err: str = ""):
        def run(argv, **kwargs):
            asked.append(list(argv))
            if isinstance(out, BaseException):
                raise out
            return subprocess.CompletedProcess(argv, code, out, err)
        return run

    check(provision_oracle.database_status("other", runner=answering()) is None and not asked,
          "a PostgreSQL tenant adds nothing and runs nothing")
    current = json.dumps({"steps_pending": 0, "tickets": 3, "queued_notices": 1, "registered_roles": 2})
    entry = provision_oracle.database_status("demo", runner=answering(out=current), euid=0)
    line = provision_oracle.database_line(entry)
    check(asked[-1][1:] == [str(provision_oracle.RELEASE_CLI), "status", "--config", str(record / "bootstrap.json"),
                            "--json"]
          and line == "switchyard: demo board database: oracle tcp://db.example:1521 SID ORCL: schema up to date; "
          "3 tickets; 1 queued notices", f"root reads it with its own copy: {asked[-1]} {line}")
    entry = provision_oracle.database_status("demo", runner=answering(out=current), euid=os.geteuid())
    check(asked[-1][4] == str(record / "listener.json") and entry["state"] == "schema up to date",
          f"the tenant owner reads it with the listener's copy: {asked[-1]}")
    before = len(asked)
    entry = provision_oracle.database_status("demo", runner=answering(out=current), euid=os.geteuid() + 4321)
    check(len(asked) == before and entry["state"].startswith("not read: its state is visible to root and to demo's")
          and "db.example:1521" in entry["database"], f"anyone else sees where it is, and nothing is run: {entry}")
    entry = provision_oracle.database_status("demo", runner=answering(out=json.dumps({"steps_pending": 2})), euid=0)
    check(entry["state"] == f"2 migration step(s) pending; see: sudo {provision_oracle.OPERATOR_CLI} check --config "
          f"{record / 'bootstrap.json'}", f"pending steps are named, with the read-only check to run: {entry}")
    entry = outcome(lambda: provision_oracle.database_status("demo", runner=answering(
        1, err="ticket-board-oracle: cannot reach db.example:1521 SID ORCL: timed out\n"), euid=0))
    check(isinstance(entry, dict) and entry["state"] == "unreachable: ticket-board-oracle: cannot reach db.example:1521 SID ORCL: timed out",
          f"an unreachable database is said, as the CLI explains it: {entry}")
    entry = outcome(lambda: provision_oracle.database_status(
        "demo", runner=answering(out=subprocess.TimeoutExpired("x", 120)), euid=0))
    check(isinstance(entry, dict) and entry["state"] == "unreachable: no answer within 120 seconds", f"and a silent one is bounded: {entry}")

    # Through switchyard status itself: one PostgreSQL and one Oracle tenant.
    statuses = [project_status.SwitchyardProjectStatus(name, name, "stopped", None, None, root / "absent.json")
                for name in ("other", "demo")]
    saved = (project_status.switchyard_project_statuses, project_status.supervised_board_entries)
    project_status.switchyard_project_statuses = lambda **kwargs: statuses
    project_status.supervised_board_entries = lambda: {}
    try:
        for as_json in (False, True):
            printed: list[str] = []
            project_status.switchyard_status_command(json_output=as_json, runner=answering(out=current),
                                                     print_func=printed.append)
            text = "\n".join(printed)
            if as_json:
                shown = json.loads(text).get("board_databases") or []
                check([e["project"] for e in shown] == ["demo"] and "state" in shown[0],
                      f"--json lists the Oracle tenant's board database alone: {shown}")
            else:
                check("switchyard: demo board database: oracle tcp://db.example:1521 SID ORCL:" in text
                      and "other board database" not in text, f"the status text names it: {text}")
    finally:
        project_status.switchyard_project_statuses, project_status.supervised_board_entries = saved


def no_postgresql_in_creation(root: Path) -> None:
    """SYRD-645: an Oracle `switchyard new` asks PostgreSQL nothing and installs nothing in it (Oracle-only hosts)."""
    from types import SimpleNamespace
    from scripts.ticket_board import provision_oracle

    ran: list = []
    launcher = SimpleNamespace()
    provision_oracle.precheck_without_postgresql(launcher)
    runner = lambda *a, **k: ran.append(a)  # noqa: E731
    answers = tuple(outcome(call) for call in (
        lambda: launcher._database_exists("orasvc_ticket_board", runner=runner),
        lambda: launcher._ticket_board_table_count("orasvc_ticket_board", runner=runner),
        lambda: launcher._ensure_board_service_peer_auth(SimpleNamespace(database="orasvc_ticket_board"),
                                                         source_repo=root, runner=runner)))
    check(answers == (False, 0, None) and not ran,
          f"no database to find, no tables to count, no peer-auth mapping to install, nothing run: {answers} {ran}")


LISTENER_WITHOUT_PSYCOPG = r"""
import importlib.abc, json, sys
class Refuse(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name == "psycopg" or name.startswith(("psycopg.", "psycopg_")):
            raise ImportError("PostgreSQL's driver is not on this host")
sys.meta_path.insert(0, Refuse())
sys.path[:0] = [sys.argv[1]]
from scripts.ticket_board import notify_listener, oracle_listener
send, busy = oracle_listener.delivery("/nonexistent/directorctl")
try:
    notify_listener.psycopg.connect("dbname=x")
    refused = "connected"
except notify_listener.psycopg.Error as exc:
    refused = str(exc)
print(json.dumps({"delivery": [type(send).__name__, type(busy.__self__).__name__], "refused": refused,
                  "psycopg_loaded": any(m.startswith("psycopg") for m in sys.modules)}))
"""


def listener_without_psycopg(root: Path) -> None:
    """SYRD-645: on an Oracle-only host (no psycopg) the Oracle listener's sender and activity gate build."""
    done = subprocess.run([sys.executable, "-c", LISTENER_WITHOUT_PSYCOPG, str(ROOT)], capture_output=True, text=True,
                          timeout=120, stdin=subprocess.DEVNULL)
    shown = json.loads(done.stdout.strip().splitlines()[-1]) if done.returncode == 0 else {"error": done.stderr[-600:]}
    check(shown == {"delivery": ["DirectorctlSender", "PaneActivityGate"], "refused": "psycopg is not installed: this "
                    "host was installed for Oracle ticket boards only", "psycopg_loaded": False},
          f"the listener starts without PostgreSQL's driver, and PostgreSQL's code refuses plainly if reached: {shown}")


def deploy_check_ends(root: Path) -> None:
    """SYRD-645: the deploy's pane-state check, through the shipped entry point as the deploy runs it, ends and
    answers as the PostgreSQL listener's does. Ignored, it ran the Oracle listener forever and hung the deploy."""
    payload = root / "assignments.json"
    payload.write_text(json.dumps({"assignments": {"director": {"actual_target": "demo-director:0.0"}}}))
    answers = {}
    for case, state_dir in (("empty", root / "pane-state-empty"), ("held", root / "pane-state-held")):
        state_dir.mkdir()
        if case == "held":
            (state_dir / "demo-director_0.0.json").write_text(json.dumps({"state": "idle", "updated_at": time.time(),
                                                                          "source": "hook"}))
        for backend in ("oracle", "postgresql"):
            try:
                done = subprocess.run(["/usr/bin/python3", str(ROOT / "scripts" / "ticket-board-notify-listener"),
                                       "--verify-pane-state-authority", "--pane-state-dir", str(state_dir),
                                       "--assignments-json", str(payload)], capture_output=True, text=True, timeout=60,
                                      stdin=subprocess.DEVNULL, cwd=root,
                                      env={**os.environ, "TICKET_BOARD_BACKEND": backend})
                answers[case, backend] = (done.returncode, done.stdout.strip())
            except subprocess.TimeoutExpired:
                answers[case, backend] = ("did not end", "")
    check(answers["empty", "oracle"] == answers["empty", "postgresql"] and answers["empty", "oracle"][0] not in (0, "did not end"),
          f"a pane-state directory serving no live role fails closed, as on PostgreSQL: {answers}")
    check(answers["held", "oracle"] == answers["held", "postgresql"] and answers["held", "oracle"][0] != "did not end",
          f"and the Oracle listener's check ends with the PostgreSQL listener's answer: {answers}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="syrd642-tenant-") as tmp:
        root = Path(tmp)
        rendering(root)
        staging(root)
        deploy_script(root)
        teardown_and_refusals(root)
        local_teardown(root)
        database_status(root)
        records_decide(root)
        first_step(root)
        no_postgresql_in_creation(root)
        listener_without_psycopg(root)
        deploy_check_ends(root)
    print(f"oracle_tenant_provisioning_test: ok ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
