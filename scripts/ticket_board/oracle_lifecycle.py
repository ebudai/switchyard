"""ticket-board-oracle: configure, check, migrate, status and teardown for an Oracle board (SYRD-642).

An Oracle board's database is somebody else's server. These commands work on
the board's own schema and its local files, and nothing else:
* `configure` writes the endpoint configuration and the credentials files. It
  asks which identifier the database is known by, a service name or a SID, and
  takes passwords only at a no-echo prompt.
* `check` and `status` only read.
* `migrate` creates the board's tables in the configured schema, as that
  schema's owner.
* `teardown` names, and with `--execute` removes, the local files the
  configuration consists of. It connects to nothing.
None of them creates or drops a database user, alters, starts or stops a
database, or uses a local PostgreSQL. None prints a password or a user name: the
credentials files are named, never shown.
"""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import os
import socket
import stat
import sys
from pathlib import Path
from typing import Any, Callable

from . import oracle_schema
from .oracle_config import (CONNECT_TIMEOUT_SECONDS, CONFIG_ENV, ConfigError, OracleConfig, connect, load, parse,
                            read_credentials)

#: What the steady-state account may hold beyond its table grants: a session, nothing else.
RUNTIME_SYSTEM_PRIVILEGES = {"CREATE SESSION"}
#: What the bootstrap account needs to create the board in its own schema.
BOOTSTRAP_SYSTEM_PRIVILEGES = {"CREATE SESSION", "CREATE TABLE", "CREATE SEQUENCE"}


def _loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def explain(exc: BaseException, config: OracleConfig) -> str:
    """A connect failure in words an operator can act on, keeping the driver's code; never a user name or secret."""
    text = str(exc)
    first = text.splitlines()[0] if text else type(exc).__name__
    at = f"{config.host}:{config.port}"
    if "DPY-6003" in text or "ORA-12505" in text:
        return (f"the listener at {at} does not know SID {config.sid} (DPY-6003, like ORA-12505); if the database "
                "is known by a service name, configure service_name instead")
    if "DPY-6001" in text or "ORA-12514" in text:
        return (f"the listener at {at} does not know service {config.service_name} (DPY-6001, like ORA-12514); if "
                "the database is known by a SID, configure sid instead")
    if "ORA-01017" in text:
        return f"the {config.account} credentials were refused (ORA-01017): check {config.credentials_file}"
    if "ORA-28000" in text:
        return f"the {config.account} account is locked (ORA-28000)"
    lines = [line for line in text.splitlines() if line.strip()]
    return " ".join(lines[:2]) if lines else first


def reach(config: OracleConfig) -> tuple[bool, str]:
    """Whether a TCP connection to the endpoint opens at all, before any login."""
    try:
        socket.create_connection((config.host, config.port), timeout=CONNECT_TIMEOUT_SECONDS).close()
    except OSError as exc:
        return False, f"cannot reach {config.host}:{config.port}: {exc.strerror or exc}"
    return True, f"reached {config.host}:{config.port}"


def _ledger_readable(cursor: Any) -> bool:
    """Whether this account can see the migration ledger at all (the runtime account, before migrate grants it)."""
    return bool(cursor.execute("SELECT count(*) FROM all_tables WHERE owner = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') "
                               "AND table_name = 'BOARD_SCHEMA_MIGRATIONS'").fetchone()[0])


def _account(config: OracleConfig, connector: Callable[[OracleConfig], Any], *,
             before_migrate: bool = False) -> list[tuple[bool, str]]:
    """One account's findings: it logs in, holds what it needs and no more, and the schema is current.

    `before_migrate`: a board about to be created. Its grants and schema are what migrate makes, so their absence
    is reported as what migrate will do, not as a failure.
    """
    name = config.account
    try:
        conn = connector(config)
    except ConfigError as exc:
        return [(False, f"{name}: {exc}")]
    except Exception as exc:  # noqa: BLE001 -- explained in the driver's own codes
        return [(False, f"{name}: {explain(exc, config)}")]
    findings: list[tuple[bool, str]] = []
    try:
        cursor = conn.cursor()
        user, schema, protocol = cursor.execute(
            "SELECT SYS_CONTEXT('USERENV', 'SESSION_USER'), SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA'), "
            "SYS_CONTEXT('USERENV', 'NETWORK_PROTOCOL') FROM dual").fetchone()
        findings.append((True, f"{name}: logged in to {config.endpoint} (Oracle {conn.version}) over "
                         f"{str(protocol).lower()}, schema {schema}"))
        held = {row[0] for row in cursor.execute("SELECT privilege FROM session_privs")}
        roles = sorted(row[0] for row in cursor.execute("SELECT role FROM session_roles"))
        if name == "bootstrap":
            missing = sorted(BOOTSTRAP_SYSTEM_PRIVILEGES - held)
            owner = user == schema
            findings.append((owner and not missing,
                             "bootstrap: the account owns the board schema and may create its tables" if owner
                             and not missing else f"bootstrap: the account must be the schema owner ({schema}) "
                             f"holding {', '.join(sorted(BOOTSTRAP_SYSTEM_PRIVILEGES))}; "
                             + ("it is another account" if not owner else f"missing: {', '.join(missing)}")))
        else:
            extra = sorted(held - RUNTIME_SYSTEM_PRIVILEGES)
            findings.append((not extra and not roles,
                             "runtime: the account holds a session and table grants only" if not extra and not roles
                             else "runtime: the account holds more than the board needs, so a stolen board "
                             f"credential could do more: {', '.join(extra + roles)}"))
            granted: dict[str, set[str]] = {}
            for table, privilege in cursor.execute(
                    "SELECT table_name, privilege FROM all_tab_privs WHERE grantee = :g AND table_schema = :s",
                    g=user, s=schema):
                granted.setdefault(table.lower(), set()).add(privilege)
            short = []
            for table, wanted in oracle_schema.RUNTIME_GRANTS.items():
                lacking = {p.strip() for p in wanted.split(",")} - granted.get(table, set())
                if lacking:
                    short.append(f"{table} ({', '.join(sorted(lacking))})")
            findings.append((not short or before_migrate,
                             "runtime: the account holds every grant the board uses" if not short
                             else f"runtime: migrate will grant {len(short)} table(s)" if before_migrate
                             else f"runtime: the account lacks grants (run migrate): {'; '.join(short)}"))
        if name == "runtime" and not _ledger_readable(cursor):  # the owner sees its own ledger, or there is none
            findings.append((before_migrate, f"{name}: migrate will create the board's tables" if before_migrate else
                             f"{name}: the migration ledger is not visible to this account, so the schema's "
                             "state is unknown here: run migrate"))
            return findings
        waiting = oracle_schema.pending(cursor)
        findings.append((not waiting or before_migrate, f"{name}: the board schema is up to date" if not waiting
                         else f"{name}: migrate will apply {len(waiting)} step(s)" if before_migrate
                         else f"{name}: {len(waiting)} migration step(s) are pending, from {waiting[0][0]} step "
                         f"{waiting[0][1] + 1}: run migrate"))
    except Exception as exc:  # noqa: BLE001
        findings.append((False, f"{name}: reading {config.endpoint}: {str(exc).splitlines()[0]}"))
    finally:
        conn.rollback()
        conn.close()
    return findings


def check(config: OracleConfig, *, allow_plaintext: bool = False, before_migrate: bool = False,
          connector: Callable[[OracleConfig], Any] = connect) -> list[tuple[bool, str]]:
    """Each finding as (passed, sentence), read-only: reachability, transport, then each configured account."""
    reached, said = reach(config)
    findings = [(reached, said)]
    if not reached:
        return findings
    if config.protocol == "tcps":
        findings.append((True, "the connection is encrypted (tcps)"))
    elif _loopback(config.host):
        findings.append((True, f"the connection is plaintext tcp, to this host only ({config.host})"))
    else:
        findings.append((allow_plaintext, f"the connection to {config.host} is plaintext tcp: use protocol tcps"
                         + (" (allowed by --allow-plaintext)" if allow_plaintext else "")))
    findings += _account(config, connector, before_migrate=before_migrate)
    if config.bootstrap_credentials_file:
        findings += _account(config.as_bootstrap(), connector, before_migrate=before_migrate)
    return findings


def status(config: OracleConfig, *, connector: Callable[[OracleConfig], Any] = connect) -> dict[str, Any]:
    """The board's configuration (no secrets), schema and queue, read-only, as the runtime account."""
    conn = connector(config)
    try:
        cursor = conn.cursor()
        if not _ledger_readable(cursor):
            return {**config.describe(), "migrations": "not visible to this account until migrate grants it"}
        done = oracle_schema.applied_steps(cursor)
        waiting = oracle_schema.pending(cursor)
        counted = {} if not done else {
            "tickets": cursor.execute("SELECT count(*) FROM tickets").fetchone()[0],
            "queued_notices": cursor.execute("SELECT count(*) FROM notification_queue").fetchone()[0],
            "oldest_due_notice": str(cursor.execute("SELECT min(next_attempt_at) FROM notification_queue "
                                                    "WHERE next_attempt_at <= SYSTIMESTAMP").fetchone()[0] or ""),
            "registered_roles": cursor.execute("SELECT count(*) FROM role_runtime_assignments").fetchone()[0],
        }
        return {**config.describe(), "migrations_applied": sorted(done), "steps_pending": len(waiting), **counted}
    finally:
        conn.rollback()
        conn.close()


def teardown(config_path: Path, config: OracleConfig, *, execute: bool = False) -> dict[str, Any]:
    """The configuration and credentials files this board consists of here; removed with `execute`. Connects to nothing."""
    owned = [config_path, config.runtime_credentials_file]
    owned += [config.bootstrap_credentials_file] if config.bootstrap_credentials_file else []
    plan: dict[str, Any] = {
        "removes": [str(p) for p in owned],
        "leaves": [str(p) for p in (config.wallet_location, config.wallet_password_file) if p],
        "never_touches": [f"the Oracle database at {config.endpoint}", f"the schema {config.schema}, its rows, and "
                          "the database accounts"],
        "note": "The database is administered by its owner. To delete the board's data, its DBA drops the schema "
                "there; nothing here does.",
        "removed": [], "refused": [],
    }
    if execute:
        for path in owned:
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                plan["refused"].append(f"{path}: not a regular file owned by this account; left in place")
                continue
            path.unlink()
            plan["removed"].append(str(path))
    return plan


def apply_workflow(config: OracleConfig, *, project: str, document_path: Path) -> str:
    """Apply a declared workflow to the board as the Director: a document, or root's workflow record of one.

    The same document again changes nothing, so provisioning can be run again.
    """
    from .oracle_app import OracleBoardApp
    from .workflow_config import validate

    raw = json.loads(document_path.read_text())
    document = raw["document"] if isinstance(raw, dict) and "document" in raw and "schema" not in raw else raw
    app = OracleBoardApp(Path("/nonexistent"), Path("/nonexistent"), config, project=project)
    try:
        current = app.workflow_document()
        if current["document"] is not None and current["document"] == validate(document, project=project):
            return f"the board already runs this workflow (revision {current['revision']})"
        applied = app.apply_workflow(document, expected_revision=current["revision"], dry_run=False,
                                     caller_role="director")
        return f"applied the declared workflow: revision {applied['revision']}"
    finally:
        app.close()


# -- configure ----------------------------------------------------------------------------------------------------

def write_private(path: Path, text: str) -> None:
    """Create `path` mode 0600 (never replacing a file or following a link), in a directory created 0700."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(text)


class Asker:
    """Interactive questions, or a plain refusal naming the missing option when there is no terminal to ask at."""

    def __init__(self, interactive: bool, ask: Callable[[str], str] = input,
                 ask_secret: Callable[[str], str] = getpass.getpass) -> None:
        self.interactive, self._ask, self._secret = interactive, ask, ask_secret

    def __call__(self, question: str, option: str, default: str = "") -> str:
        if not self.interactive:
            if default:
                return default
            raise ConfigError(f"{option} is required (or run configure in a terminal to be asked)")
        answer = self._ask(f"{question}{f' [{default}]' if default else ''}: ").strip()
        return answer or default

    def choose(self, question: str, choices: dict[str, str], option: str) -> str:
        if not self.interactive:
            raise ConfigError(f"{option} is required (or run configure in a terminal to be asked)")
        while True:
            answer = self._ask(f"{question} [{'/'.join(choices)}]: ").strip().lower()
            if answer in choices:
                return choices[answer]
            print(f"answer one of: {', '.join(choices)}")

    def secret(self, question: str) -> str:
        return self._secret(f"{question}: ")


def _credentials(path: Path, which: str, asker: Asker) -> None:
    """Use the credentials file at `path`, or create it from the answers when there is none."""
    if path.exists() or path.is_symlink():
        read_credentials(path, f"{which}_credentials_file")
        return
    if not asker.interactive:
        raise ConfigError(f"{which} credentials file {path} does not exist: create it mode 0600 holding "
                          '{"user": ..., "password": ...}, or run configure in a terminal to be asked')
    user = asker(f"{which.capitalize()} account user name", f"the {which} user")
    password = asker.secret(f"{which.capitalize()} account password (not echoed)")
    write_private(path, json.dumps({"user": user, "password": password}) + "\n")
    read_credentials(path, f"{which}_credentials_file")


def configure(args: argparse.Namespace, asker: Asker) -> tuple[Path, OracleConfig]:
    if args.sid and args.service_name:
        raise ConfigError("give exactly one of --sid and --service-name, not both")
    output = Path(args.output or asker("Configuration file to write", "--output")).expanduser()
    if not output.is_absolute():
        output = Path.cwd() / output
    if output.exists() and not args.force:
        raise ConfigError(f"{output} already exists; pass --force to replace it")
    host = args.host or asker("Database host (IP address or name)", "--host")
    port = args.port or asker("Database port", "--port", "1521" if asker.interactive else "")
    if args.sid or args.service_name:
        kind, identifier = ("sid", args.sid) if args.sid else ("service_name", args.service_name)
    else:
        kind = asker.choose("Is the database identified by a service name or a SID?",
                            {"service": "service_name", "sid": "sid"}, "--sid or --service-name")
        identifier = asker("SID" if kind == "sid" else "Service name", f"--{kind.replace('_', '-')}")
    protocol = args.protocol or ("tcps" if asker.interactive and asker(
        "Encrypt the connection with TLS (tcps)? y/n", "--protocol", "n").lower().startswith("y") else "tcp")
    schema = args.schema or asker("Schema that owns the board's tables", "--schema")
    runtime = Path(args.runtime_credentials_file or output.parent / "runtime.credentials").expanduser()
    bootstrap = args.bootstrap_credentials_file
    if bootstrap is None and asker.interactive and not args.no_bootstrap:
        if asker("Also keep the schema owner's (bootstrap) credentials here, for migrate? y/n", "", "n").lower() \
                .startswith("y"):
            bootstrap = str(output.parent / "bootstrap.credentials")
    raw: dict[str, Any] = {"host": str(host).strip(), "port": int(port) if str(port).strip().isdigit() else port,
                           kind: identifier, "protocol": protocol, "schema": schema,
                           "runtime_credentials_file": str(runtime)}
    if bootstrap:
        raw["bootstrap_credentials_file"] = str(Path(bootstrap).expanduser())
    for name in ("wallet_location", "wallet_password_file", "ssl_server_cert_dn"):
        if getattr(args, name):
            raw[name] = getattr(args, name)
    config = parse(raw, str(output))  # every field validated before anything is written
    _credentials(config.runtime_credentials_file, "runtime", asker)
    if config.bootstrap_credentials_file:
        _credentials(config.bootstrap_credentials_file, "bootstrap", asker)
    if output.exists():
        output.unlink()
    write_private(output, json.dumps(raw, indent=2) + "\n")
    return output, config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ticket-board-oracle", description=__doc__.split("\n\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (("configure", "write the endpoint configuration and credentials files"),
                       ("check", "verify reachability, transport, both accounts' privileges and the schema, read-only"),
                       ("status", "show the configuration (no secrets), schema and queue, read-only"),
                       ("migrate", "create or upgrade the board's tables, as the schema owner"),
                       ("teardown", "list, and with --execute remove, this board's local files; connects to nothing"),
                       ("apply-workflow", "apply a declared workflow document to the board, as the Director")):
        sub = commands.add_parser(name, help=text)
        if name == "configure":
            sub.add_argument("--output", help="the configuration file to write")
            sub.add_argument("--host", help="IP address or host name of the database listener")
            sub.add_argument("--port", type=int)
            which = sub.add_mutually_exclusive_group()
            which.add_argument("--sid", help="the database's SID (passed as a SID, never as a service name)")
            which.add_argument("--service-name", help="the database's service name")
            sub.add_argument("--protocol", choices=("tcp", "tcps"))
            sub.add_argument("--wallet-location")
            sub.add_argument("--wallet-password-file")
            sub.add_argument("--ssl-server-cert-dn")
            sub.add_argument("--schema", help="the schema that owns the board's tables")
            sub.add_argument("--runtime-credentials-file", help="default: runtime.credentials beside --output")
            sub.add_argument("--bootstrap-credentials-file", help="the schema owner's, for migrate (optional)")
            sub.add_argument("--no-bootstrap", action="store_true", help="do not ask for bootstrap credentials")
            sub.add_argument("--force", action="store_true", help="replace an existing configuration file")
            continue
        sub.add_argument("--config", default=os.environ.get(CONFIG_ENV, ""),
                         help=f"the board's Oracle configuration file ({CONFIG_ENV})")
        sub.add_argument("--json", action="store_true")
        if name == "check":
            sub.add_argument("--allow-plaintext", action="store_true",
                             help="accept an unencrypted connection to another host")
            sub.add_argument("--before-migrate", action="store_true",
                             help="a board about to be created: its grants and schema are migrate's to make")
        if name == "apply-workflow":
            sub.add_argument("--project", required=True)
            sub.add_argument("--document", required=True, type=Path,
                             help="a declared workflow document, or root's workflow record of one")
        if name == "migrate":
            sub.add_argument("--project", default=os.environ.get("TICKET_BOARD_PROJECT", ""))
            sub.add_argument("--ticket-prefix", default=os.environ.get("TICKET_BOARD_TICKET_PREFIX", ""))
        if name == "teardown":
            sub.add_argument("--execute", action="store_true", help="remove the files; without it, only list them")
    args = parser.parse_args(argv)
    try:
        if args.command == "configure":
            written, config = configure(args, Asker(sys.stdin.isatty()))
            print(f"wrote {written}: {config.endpoint}, schema {config.schema}")
            print(f"runtime credentials: {config.runtime_credentials_file}")
            print(f"bootstrap credentials: {config.bootstrap_credentials_file or 'none (migrate runs elsewhere)'}")
            print(f"next: ticket-board-oracle check --config {written}")
            return 0
        if not args.config:
            parser.error(f"--config or {CONFIG_ENV} is required")
        config = load(args.config)
    except ConfigError as exc:
        print(f"ticket-board-oracle: {exc}", file=sys.stderr)
        return 2
    if args.command == "check":
        findings = check(config, allow_plaintext=args.allow_plaintext, before_migrate=args.before_migrate)
        if args.json:
            print(json.dumps([{"ok": ok, "finding": text} for ok, text in findings], indent=2))
        else:
            for ok, text in findings:
                print(f"{'ok  ' if ok else 'FAIL'} {text}")
        return 0 if all(ok for ok, _ in findings) else 1
    if args.command == "status":
        try:
            shown = status(config)
        except Exception as exc:  # noqa: BLE001
            print(f"ticket-board-oracle: {explain(exc, config)}", file=sys.stderr)
            return 1
        print(json.dumps(shown, indent=2) if args.json else "\n".join(f"{k}: {v}" for k, v in shown.items()))
        return 0
    if args.command == "teardown":
        plan = teardown(Path(args.config).absolute(), config, execute=args.execute)
        if args.json:
            print(json.dumps(plan, indent=2))
        else:
            print("\n".join([("Removed:" if args.execute else "Would remove (pass --execute):")]
                            + [f"  {p}" for p in (plan["removed"] if args.execute else plan["removes"])]
                            + [f"  refused: {p}" for p in plan["refused"]]
                            + ["Left in place:"] + [f"  {p}" for p in plan["leaves"] + plan["never_touches"]]
                            + [plan["note"]]))
        return 1 if plan["refused"] else 0
    if args.command == "apply-workflow":
        try:
            print(apply_workflow(config, project=args.project, document_path=args.document))
        except Exception as exc:  # noqa: BLE001 -- refused or unreachable: said, without secrets
            print(f"ticket-board-oracle: {explain(exc, config)}", file=sys.stderr)
            return 1
        return 0
    if not args.project or not args.ticket_prefix:
        print("ticket-board-oracle: migrate needs --project and --ticket-prefix", file=sys.stderr)
        return 2
    try:
        oracle_schema.migrate(config, project=args.project, ticket_prefix=args.ticket_prefix, report=print)
    except ConfigError as exc:
        print(f"ticket-board-oracle: {exc}", file=sys.stderr)
        return 2
    except oracle_schema.MigrationFailed as exc:
        print(f"ticket-board-oracle: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 -- a connect failure, explained
        print(f"ticket-board-oracle: {explain(exc, config.as_bootstrap())}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
