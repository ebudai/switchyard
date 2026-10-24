#!/usr/bin/env python3
"""SYRD-642: ticket-board-oracle against a real Oracle, driven as an operator drives it.

The endpoint is reached by service name, and by SID at an IPv4 address on a
non-default port: a local TCP relay stands in front of the test service, and
the database is known by SID only. Covered:
* `configure`, with flags and interactively under a real terminal: it asks
  whether a service name or a SID is meant, and reads the password with echo
  off;
* `check` diagnostics for an unreachable host, an unknown SID, an unknown
  service, refused credentials, an over-privileged runtime account, a missing
  grant and pending migrations;
* `check` and `status` only read;
* `migrate` as the schema owner only;
* `teardown`, which removes only this board's local files and connects to
  nothing.
No command prints a password or the runtime account's name.

Without the test service or python-oracledb the suite says it is skipped, and
why. It never passes without having run.
"""

from __future__ import annotations

import dataclasses
import json
import os
import select
import stat
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests"), str(ROOT / "tests" / "oracle")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import oracle_fixture  # noqa: E402
from oracle_board_test import OracleBoard, Proxy, act, example, owner_sql  # noqa: E402

CHECKS = 0
SAID: list[str] = []  # everything any command printed, to search for secrets at the end


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def cli(*args: str) -> tuple[int, str]:
    done = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/ticket-board-oracle"), *args], cwd=ROOT,
                          capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL,
                          env={**os.environ, "PYTHONPATH": str(ROOT / "scripts")})
    SAID.append(done.stdout + done.stderr)
    return done.returncode, done.stdout + done.stderr


def variant(private: Path, name: str, **change) -> Path:
    """A copy of the board configuration with some fields changed (None removes one)."""
    raw = {**json.loads((private / "board.json").read_text()), **change}
    path = private / f"{name}.json"
    path.write_text(json.dumps({k: v for k, v in raw.items() if v is not None}))
    return path


def by_service(root: Path) -> None:
    """The service-name path: diagnostics before and after migrate, read-only checks, grants repaired."""
    from scripts.ticket_board import oracle_lifecycle
    from scripts.ticket_board.oracle_config import connect

    with oracle_fixture.disposable_schema("Y") as (bootstrap, runtime, private):
        runtime_user = runtime.credentials()[0]
        config = str(private / "board.json")
        rc, out = cli("check", "--config", config)
        check(rc == 1 and "runtime: the migration ledger is not visible to this account" in out
              and "bootstrap: 0 migration step(s)" not in out and "bootstrap: " in out and "pending" in out,
              f"before migrate: the runtime account says it cannot know, the owner says what is pending: {out}")
        only_runtime = variant(private, "runtime-only", bootstrap_credentials_file=None)
        rc, out = cli("migrate", "--config", str(only_runtime), "--project", "pgu", "--ticket-prefix", "PGU")
        check(rc == 2 and "no bootstrap_credentials_file" in out, f"migrate needs the schema owner's credentials: {out}")
        rc, out = cli("migrate", "--config", config, "--project", "pgu", "--ticket-prefix", "PGU")
        from scripts.ticket_board import oracle_schema
        check(rc == 0 and out.count(": applied") == sum(len(v) for _n, v in oracle_schema.MIGRATIONS),
              f"migrate applies every step, once, and says so: {out}")
        statements: list[str] = []

        def recorded(cfg):
            conn = connect(cfg)

            class Cursor:
                def __init__(self, inner):
                    self.inner = inner

                def execute(self, sql, *a, **k):
                    statements.append(sql)
                    self.inner.execute(sql, *a, **k)
                    return self

                def __getattr__(self, name):
                    return getattr(self.inner, name)

                def __iter__(self):
                    return iter(self.inner)

            class Conn:
                version = conn.version

                def cursor(self):
                    return Cursor(conn.cursor())

                def __getattr__(self, name):
                    return getattr(conn, name)
            return Conn()

        findings = oracle_lifecycle.check(runtime, connector=recorded)
        oracle_lifecycle.status(runtime, connector=recorded)
        check(all(ok for ok, _ in findings) and len(findings) == 9, f"a migrated board checks clean: {findings}")
        check(statements and all(sql.lstrip().upper().startswith("SELECT") for sql in statements),
              f"check and status only read: {[s for s in statements if not s.lstrip().upper().startswith('SELECT')]}")
        remote = dataclasses.replace(runtime, host="203.0.113.7")
        reached = (True, "reached")
        original = oracle_lifecycle.reach
        oracle_lifecycle.reach = lambda _c: reached  # the transport rule for another host; the login is real
        try:
            refused = oracle_lifecycle.check(remote, connector=lambda c: connect(dataclasses.replace(c, host="127.0.0.1")))
            allowed = oracle_lifecycle.check(remote, allow_plaintext=True,
                                             connector=lambda c: connect(dataclasses.replace(c, host="127.0.0.1")))
        finally:
            oracle_lifecycle.reach = original
        check([ok for ok, text in refused if "plaintext" in text] == [False]
              and [ok for ok, text in allowed if "plaintext" in text] == [True],
              f"plaintext to another host fails the check unless allowed explicitly: {refused[1]}")
        with oracle_fixture._system() as conn:
            conn.cursor().execute(f"GRANT CREATE TABLE TO {runtime_user}")
        owner_sql(bootstrap, f"REVOKE UPDATE ON tickets FROM {runtime_user}")
        rc, out = cli("check", "--config", config)
        check(rc == 1 and "could do more: CREATE TABLE" in out and "lacks grants (run migrate): tickets (UPDATE)" in out
              and out.count("FAIL") == 2, f"an over-privileged runtime account and a missing grant are each named: {out}")
        with oracle_fixture._system() as conn:
            conn.cursor().execute(f"REVOKE CREATE TABLE FROM {runtime_user}")
        rc, _out = cli("migrate", "--config", config, "--project", "pgu", "--ticket-prefix", "PGU")
        rc2, out = cli("check", "--config", config)
        check(rc == 0 and rc2 == 0 and "FAIL" not in out, f"migrate restores a revoked grant: {out}")
        rc, out = cli("status", "--config", config, "--json")
        shown = json.loads(out) if rc == 0 else {}
        check(shown.get("steps_pending") == 0 and shown.get("tickets") == 0 and shown.get("service_name") == "FREEPDB1"
              and shown.get("sid") == "", f"status reads the board as the runtime account: {out}")
        unknown = variant(private, "unknown-service", service_name="NOPE")
        rc, out = cli("check", "--config", str(unknown))
        check(rc == 1 and "does not know service NOPE (DPY-6001, like ORA-12514); if the database is known by a SID" in out,
              f"an unknown service is named as such, with the SID hint: {out}")
        SAID.append(runtime_user)  # the runtime account's name, to be searched for (not as output)
        SECRETS.update({runtime.credentials()[1], bootstrap.credentials()[1]})
        RUNTIME_USERS.add(runtime_user)


SECRETS: set[str] = set()
RUNTIME_USERS: set[str] = set()


def by_sid(root: Path) -> None:
    """An IPv4 address, a non-default port and a SID: configured, checked, migrated, and a board run over it."""
    import oracledb
    from scripts.ticket_board.oracle_config import load

    proxy = Proxy(("127.0.0.1", 1521))
    port = proxy.start()
    check(port != 1521, "the relay listens on a non-default port")
    try:
        with oracle_fixture.disposable_schema("D", by_sid=True, port=port) as (bootstrap, runtime, private):
            runtime_user = runtime.credentials()[0]
            RUNTIME_USERS.add(runtime_user)
            SECRETS.update({runtime.credentials()[1], bootstrap.credentials()[1]})
            written = private / "configured.json"
            rc, out = cli("configure", "--output", str(written), "--host", "127.0.0.1", "--port", str(port), "--sid",
                          "FREE", "--schema", runtime.schema, "--runtime-credentials-file",
                          str(private / "runtime.credentials"), "--bootstrap-credentials-file",
                          str(private / "bootstrap.credentials"))
            saved = json.loads(written.read_text()) if rc == 0 else {}
            check(rc == 0 and saved.get("sid") == "FREE" and "service_name" not in saved and saved.get("port") == port
                  and saved.get("host") == "127.0.0.1" and not {"user", "password"} & set(saved)
                  and stat.S_IMODE(written.stat().st_mode) == 0o600,
                  f"configure keeps the endpoint fields only, the SID as a SID, owner-only: {out} {saved}")
            config = load(written)
            descriptor = oracledb.ConnectParams(**config.endpoint_params()).get_connect_string()
            check(f"(HOST=127.0.0.1)(PORT={port})" in descriptor and "(CONNECT_DATA=(SID=FREE))" in descriptor
                  and "SERVICE_NAME" not in descriptor, f"the driver is given a SID, not a service name: {descriptor}")
            check("(RETRY_COUNT=2)(RETRY_DELAY=1)(TRANSPORT_CONNECT_TIMEOUT=10)" in descriptor,
                  f"a connect is bounded: a timeout per attempt and a bounded retry: {descriptor}")
            rc, out = cli("migrate", "--config", str(written), "--project", "pgu", "--ticket-prefix", "PGU")
            check(rc == 0 and ": applied" in out, f"migrate over the SID endpoint: {out}")
            rc, out = cli("check", "--config", str(written))
            check(rc == 0 and f"logged in to tcp://127.0.0.1:{port} SID FREE" in out and "FAIL" not in out,
                  f"check passes over IPv4, a non-default port and a SID: {out}")
            board = OracleBoard(config, root / "sid", root / "store")
            try:
                applied = act(board, None, "configure_workflow", "director", document=example(), expected_revision=0,
                              dry_run=False)
                made = act(board, None, "create_ticket", "director", title="By SID", body="b", state="analysis",
                           assignee="director")
                t = made.get("ok", {}).get("ticket", {}).get("id", "")
                routed = act(board, t, "route", "director", state="in_progress", assignee="app")
                check("ok" in applied and "ok" in routed and board.ticket(t)["assignee"] == "app"
                      and board.ticket(t)["store_backend"] == "oracle",
                      f"a board runs over the SID endpoint: {made} {routed}")
            finally:
                board.close()
            service_as_sid = variant(private, "service-as-sid", sid="FREEPDB1")
            rc, out = cli("check", "--config", str(service_as_sid))
            check(rc == 1 and f"the listener at 127.0.0.1:{port} does not know SID FREEPDB1 (DPY-6003, like ORA-12505)"
                  in out, f"a service name given as a SID is refused by the listener, not quietly used: {out}")
            wrong = private / "wrong.credentials"
            oracle_fixture.write_credentials(wrong, runtime_user, "Wrongpassword123456")
            rc, out = cli("check", "--config", str(variant(private, "wrong", runtime_credentials_file=str(wrong))))
            check(rc == 1 and f"runtime: the runtime credentials were refused (ORA-01017): check {wrong}" in out
                  and "Wrongpassword" not in out, f"refused credentials are named by their file: {out}")
            rc, out = cli("configure", "--output", str(private / "both.json"), "--host", "127.0.0.1", "--port",
                          str(port), "--sid", "FREE", "--service-name", "FREEPDB1", "--schema", runtime.schema)
            check(rc == 2 and "not allowed with argument" in out and not (private / "both.json").exists(),
                  f"configure refuses a SID and a service name together: {out}")
            rc, out = cli("configure", "--output", str(private / "neither.json"), "--host", "127.0.0.1", "--port",
                          str(port), "--schema", runtime.schema)
            check(rc == 2 and "--sid or --service-name is required" in out and not (private / "neither.json").exists(),
                  f"without a terminal, configure names the missing identifier rather than guessing: {out}")
            proxy.stop()
            rc, out = cli("check", "--config", str(written))
            check(rc == 1 and f"FAIL cannot reach 127.0.0.1:{port}: Connection refused" in out and out.count("\n") == 1,
                  f"an unreachable endpoint is said plainly, before any login: {out}")
    finally:
        proxy.stop()


def interactive_configure(root: Path) -> None:
    """In a terminal, configure asks which identifier is meant, and reads the password with echo off."""
    import fcntl
    import pty  # noqa: F401 -- the platform has ptys
    import termios

    private = root / "interactive"
    output = private / "board.json"
    password = "Pty" + os.urandom(9).hex()
    master, slave = os.openpty()

    def controlling_terminal() -> None:
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    proc = subprocess.Popen([sys.executable, "-B", str(ROOT / "scripts/ticket-board-oracle"), "configure", "--output",
                             str(output), "--host", "192.0.2.10", "--port", "1599", "--schema", "BOARD_OWNER"],
                            stdin=slave, stdout=slave, stderr=slave, cwd=ROOT, start_new_session=True,
                            preexec_fn=controlling_terminal, env={**os.environ, "PYTHONPATH": str(ROOT / "scripts")})
    os.close(slave)
    seen = b""

    def until(text: str) -> None:
        nonlocal seen
        deadline = time.monotonic() + 30
        while text.encode() not in seen and time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.5)
            if ready:
                try:
                    seen += os.read(master, 4096)
                except OSError:
                    break
        check(text.encode() in seen, f"configure asked {text!r}: {seen.decode(errors='replace')}")

    try:
        for question, answer in (("Is the database identified by a service name or a SID? [service/sid]", "maybe"),
                                 ("answer one of: service, sid", "sid"), ("SID:", "ORCL"),
                                 ("Encrypt the connection with TLS (tcps)? y/n [n]", ""),
                                 ("for migrate? y/n [n]", "n"), ("Runtime account user name:", "board_runtime"),
                                 ("Runtime account password (not echoed):", password)):
            until(question)
            os.write(master, (answer + "\n").encode())
        until("next: ticket-board-oracle check")
        proc.wait(30)
    finally:
        if proc.poll() is None:
            proc.kill()
        os.close(master)
    saved = json.loads(output.read_text())
    credentials = private / "runtime.credentials"
    check(proc.returncode == 0 and saved == {"host": "192.0.2.10", "port": 1599, "sid": "ORCL", "protocol": "tcp",
                                             "schema": "BOARD_OWNER", "runtime_credentials_file": str(credentials)},
          f"the answers became the endpoint configuration, the SID as a SID: {saved}")
    check(json.loads(credentials.read_text()) == {"user": "board_runtime", "password": password}
          and stat.S_IMODE(credentials.stat().st_mode) == 0o600 and stat.S_IMODE(private.stat().st_mode) == 0o700,
          "the credentials went to an owner-only file in an owner-only directory")
    check(password.encode() not in seen, "the password was not echoed to the terminal")


def teardown(root: Path) -> None:
    """teardown removes this board's configuration and credentials files only, and connects to nothing."""
    place = root / "teardown"
    place.mkdir(mode=0o700)
    for name in ("runtime.credentials", "bootstrap.credentials"):
        oracle_fixture.write_credentials(place / name, "SOMEONE", "Secret123456789")
    wallet = place / "wallet"
    wallet.mkdir()
    (wallet / "ewallet.pem").write_text("not a real wallet\n")
    config = place / "board.json"
    config.write_text(json.dumps({"host": "198.51.100.4", "port": 9, "service_name": "GONE", "protocol": "tcps",
                                  "wallet_location": str(wallet), "schema": "BOARD_OWNER",
                                  "runtime_credentials_file": str(place / "runtime.credentials"),
                                  "bootstrap_credentials_file": str(place / "bootstrap.credentials")}))
    rc, out = cli("teardown", "--config", str(config), "--json")
    plan = json.loads(out) if rc == 0 else {}
    check(plan.get("removes") == [str(config), str(place / "runtime.credentials"), str(place / "bootstrap.credentials")]
          and plan.get("leaves") == [str(wallet)] and plan.get("removed") == [] and config.exists()
          and "the Oracle database at tcps://198.51.100.4:9 service GONE" in plan.get("never_touches", []),
          f"without --execute, teardown lists what it would remove and removes nothing: {out}")
    started = time.monotonic()
    rc, out = cli("teardown", "--config", str(config), "--execute")
    check(rc == 0 and not config.exists() and not (place / "runtime.credentials").exists()
          and not (place / "bootstrap.credentials").exists() and (wallet / "ewallet.pem").exists()
          and time.monotonic() - started < 20,
          f"--execute removes the board's own files, leaves the wallet, and contacts no database: {out}")
    other = place / "elsewhere.credentials"
    oracle_fixture.write_credentials(other, "SOMEONE", "Secret123456789")
    (place / "linked.credentials").symlink_to(other)
    config.write_text(json.dumps({"host": "198.51.100.4", "port": 9, "sid": "GONE", "schema": "BOARD_OWNER",
                                  "runtime_credentials_file": str(place / "linked.credentials")}))
    rc, out = cli("teardown", "--config", str(config), "--execute")
    check(rc == 1 and "linked.credentials: not a regular file owned by this account; left in place" in out
          and other.exists() and (place / "linked.credentials").is_symlink(),
          f"a credentials path that is a link is refused, and nothing behind it is touched: {out}")


def tenant_creation(root: Path) -> None:
    """`switchyard new`'s Oracle step and the root packet's Oracle steps, against a fresh schema, in their order."""
    import shutil
    from types import SimpleNamespace
    from scripts import database_capabilities as dc
    from scripts.ticket_board import provision_oracle
    from scripts.ticket_board.oracle_app import OracleBoardApp
    from scripts.ticket_board.oracle_config import load

    tenants = root / "tenants"
    host_record = root / "databases.json"
    host_record.write_text(json.dumps({"databases": ["oracle"]}))
    workflow = root / "workflow.json"
    document = example()
    document["project"] = "demo"
    for role in document["roles"]:
        if role.get("target"):
            role["target"] = role["target"].replace("pgu-", "demo-", 1)
    workflow.write_text(json.dumps(document))
    args = SimpleNamespace(workflow_config=str(workflow), from_artifact=None, yes=True)
    saved = {k: os.environ.get(k) for k in (provision_oracle.ROOT_ENV, provision_oracle.NEW_CONFIG_ENV)}
    with oracle_fixture.disposable_schema("W") as (bootstrap, runtime, private):
        RUNTIME_USERS.add(runtime.credentials()[0])
        SECRETS.update({runtime.credentials()[1], bootstrap.credentials()[1]})
        os.environ[provision_oracle.ROOT_ENV] = str(tenants)
        try:
            wrong = private / "wrong.credentials"
            oracle_fixture.write_credentials(wrong, runtime.credentials()[0], "Wrongpassword123456")
            os.environ[provision_oracle.NEW_CONFIG_ENV] = str(variant(private, "wrong-new", runtime_credentials_file=str(wrong)))
            try:
                dc.require_new_project_capability(args, interactive=False, path=host_record)
                said = "went on"
            except SystemExit as exc:
                said = str(exc)
            check("did not check out" in said and "nothing collected was kept" in said
                  and not any(tenants.glob(".staged-*")) and os.environ.get(provision_oracle.STAGED_ENV) is None,
                  f"refused credentials refuse the board before anything is created, and nothing is kept: {said}")
            os.environ[provision_oracle.NEW_CONFIG_ENV] = str(private / "board.json")
            # As switchyard new runs it: on the system Python, which has no python-oracledb; the check runs in the
            # shared venv (SWITCHYARD_PYTHON_VENV), as the shipped CLI arranges.
            venv = Path(sys.executable).parent.parent
            done = subprocess.run(["/usr/bin/python3", "-c", (
                "import json, os, sys; sys.path.insert(0, sys.argv[1]); from types import SimpleNamespace; "
                "from pathlib import Path; from scripts import database_capabilities as dc; "
                "from scripts.ticket_board import provision_oracle as p; "
                "print('chose', dc.require_new_project_capability(SimpleNamespace(workflow_config=sys.argv[2], "
                "from_artifact=None, yes=True), interactive=False, path=Path(sys.argv[3]))); "
                "staged = os.environ[p.STAGED_ENV]; os.environ.pop(p.STAGED_ENV); print('staged', staged); "
                "from scripts import team_launcher as t; "
                "print('probes', t._database_exists('x', runner=None), t._ticket_board_table_count('x', runner=None))"),
                str(ROOT), str(workflow), str(host_record)], capture_output=True, text=True, timeout=300,
                stdin=subprocess.DEVNULL, env={**os.environ, "SWITCHYARD_PYTHON_VENV": str(venv)})
            system_has_driver = subprocess.run(["/usr/bin/python3", "-c", "import oracledb"],
                                               capture_output=True).returncode == 0
            staged = Path(next((l.split(" ", 1)[1] for l in done.stdout.splitlines() if l.startswith("staged ")),
                               "/nonexistent"))
            check(done.returncode == 0 and "chose oracle" in done.stdout and staged.is_dir()
                  and "migrate will apply 14 step(s)" in done.stdout and not system_has_driver
                  and "probes False 0" in done.stdout,
                  f"on the system Python (no python-oracledb), a fresh schema checks out for creation through the "
                  f"venv, and what migrate will make is not a failure: {done.stdout[-800:]} {done.stderr[-800:]}")
            os.environ[provision_oracle.STAGED_ENV] = str(staged)
            check(provision_oracle.adopt_staged("demo") and provision_oracle.is_oracle("demo"), "the plan adopts it")
            record = tenants / "demo"
            for copy in ("board-runtime.credentials", "listener-runtime.credentials"):  # the packet's `install` lines
                shutil.copy2(record / "runtime.credentials", record / copy)
            boot = str(record / "bootstrap.json")
            steps = [cli("check", "--config", boot, "--before-migrate"),
                     cli("migrate", "--config", boot, "--project", "demo", "--ticket-prefix", "DEMO"),
                     cli("apply-workflow", "--config", boot, "--project", "demo", "--document", str(workflow)),
                     cli("apply-workflow", "--config", boot, "--project", "demo", "--document", str(workflow)),
                     cli("check", "--config", boot)]
            check([rc for rc, _ in steps] == [0, 0, 0, 0, 0] and "applied the declared workflow: revision 1" in steps[2][1]
                  and "the board already runs this workflow (revision 1)" in steps[3][1] and "FAIL" not in steps[4][1],
                  f"the packet's steps in order: check, migrate, workflow, workflow again unchanged, check: {steps}")
            app = OracleBoardApp(root / "f", root / "a", load(record / "board.json"), project="demo")
            try:
                board = app.snapshot()
            finally:
                app.close()
            check(board["store_backend"] == "oracle" and board["ticket_prefix"] == "DEMO" and board["workflow"]
                  and board["errors"] == [], "a board on the record's own configuration serves the declared workflow")
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def main() -> int:
    why = oracle_fixture.available()
    if why:
        print(f"oracle_lifecycle_test: SKIPPED, not run: {why}")
        return 0
    import tempfile

    with tempfile.TemporaryDirectory(prefix="syrd642-life-") as tmp:
        root = Path(tmp)
        (root / "store").mkdir()
        by_service(root)
        by_sid(root)
        interactive_configure(root)
        teardown(root)
        tenant_creation(root)
    printed = "\n".join(text for text in SAID if text not in RUNTIME_USERS)
    check(SECRETS and not any(secret in printed for secret in SECRETS), "no command printed a password")
    check(RUNTIME_USERS and not any(user in printed for user in RUNTIME_USERS),
          "no command printed the runtime account's name")
    print(f"oracle_lifecycle_test: ok ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
