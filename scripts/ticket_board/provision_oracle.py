"""A tenant whose board keeps its tickets in a remote Oracle database (SYRD-642).

Root keeps a record of it in `/etc/switchyard/oracle/<project>/` (root's,
0755). The record is what makes the tenant an Oracle tenant: every renderer
and script asks `is_oracle(project)`, so `switchyard new`, a resumed
provisioning, `switchyard upgrade` and `deploy-release` all render the same
units and run the same steps. The record holds:

| File | Owner, mode | What |
|---|---|---|
| `endpoint.json` | root 0644 | the endpoint as `switchyard new` collected it; nothing secret |
| `runtime.credentials` | root 0600 | the steady-state account, for root's own steps |
| `bootstrap.credentials` | root 0600 | the schema owner, for migrate: root's alone |
| `bootstrap.json` | root 0644 | the endpoint, with both root credential files: what root migrates and checks with |
| `board.json` | root 0644 | the endpoint, with `board-runtime.credentials` |
| `board-runtime.credentials` | the board service account 0600 | the board server's copy |
| `listener.json` | root 0644 | the endpoint, with `listener-runtime.credentials` |
| `listener-runtime.credentials` | the tenant owner 0600 | the notification listener's copy |
| `owner.json` | root 0644 | the endpoint, with the owner's two copies: what the tenant's own deploy migrates with |
| `owner-bootstrap.credentials` | the tenant owner 0600, optional | the schema owner, for the tenant's own deploy (SYRD-645) |
| `allow-plaintext` | root 0644, optional | the operator accepted plaintext tcp to another host |

Each reader has its own copy of the runtime credentials, owned by it:
`oracle_config` reads a credentials file only from the account that owns it.
The tenant's board deploy (`deploy-release`, `deploy-restart`) runs as the
tenant owner, and migrates there, so the owner holds its own copy of the schema
owner's credentials. That is the authority a PostgreSQL tenant's owner has over
its own board database. An operator who removes `owner-bootstrap.credentials`
keeps that authority with root alone: the deploy then migrates nothing, and
refuses a release whose steps are pending.

`switchyard new` collects the connection before it asks anything else, checks
reachability and both accounts' privileges, and stages it in a root-only
directory. When the plan's artifacts are first written, `adopt_staged` makes
that staging the project's record. Teardown removes the record, through
`ticket-board-oracle teardown --execute` for each configuration in it, and never
the remote database (SYRD-645).

`switchyard status` reads the database as whoever asks may: root through its
own copy, the tenant owner through the listener's, anyone else the endpoint
only. Without `bootstrap.credentials` (an operator may remove it after
creation), `deploy-release` migrates nothing: it reports the schema's pending
steps and refuses a release that needs them.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT_ENV = "SWITCHYARD_ORACLE_TENANT_ROOT"
DEFAULT_ROOT = Path("/etc/switchyard/oracle")
#: The staging directory this `switchyard new` collected, until the plan adopts it.
STAGED_ENV = "SWITCHYARD_ORACLE_STAGED"
#: Non-interactive `switchyard new`: the board database, and a configuration prepared by `ticket-board-oracle configure`.
NEW_DATABASE_ENV = "SWITCHYARD_NEW_DATABASE"
NEW_CONFIG_ENV = "SWITCHYARD_NEW_ORACLE_CONFIG"
ALLOW_PLAINTEXT_ENV = "SWITCHYARD_ORACLE_ALLOW_PLAINTEXT"
#: What an operator runs: the installed release's CLI, which runs itself in the shared venv.
OPERATOR_CLI = "/opt/switchyard/current/scripts/ticket-board-oracle"
#: This release's own CLI, for the steps Switchyard runs.
RELEASE_CLI = Path(__file__).resolve().parents[1] / "ticket-board-oracle"
ENDPOINT_FIELDS = ("host", "port", "sid", "service_name", "protocol", "wallet_location", "wallet_password_file",
                   "ssl_server_dn_match", "ssl_server_cert_dn", "schema")


class OracleTenantError(SystemExit):
    """Creating or rendering an Oracle tenant cannot go on; nothing secret is in the message."""


def root() -> Path:
    return Path(os.environ.get(ROOT_ENV) or DEFAULT_ROOT)


def record_dir(project: str) -> Path:
    return root() / project


def is_oracle(project: str) -> bool:
    """Whether root holds an Oracle record for this project."""
    path = record_dir(project) / "board.json"
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def config_path(project: str, reader: str) -> str:
    """`board`, `listener` or `bootstrap`: that reader's configuration file."""
    return str(record_dir(project) / f"{reader}.json")


def unit_environment(project: str, reader: str) -> str:
    """The `Environment=` lines that make a board or listener unit run on Oracle."""
    return (f"Environment=TICKET_BOARD_BACKEND=oracle\n"
            f"Environment=TICKET_BOARD_ORACLE_CONFIG={config_path(project, reader)}\n")


def _write(path: Path, text: str, mode: int) -> None:
    """Create `path` with `mode`, never through a link and never over an existing file."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(text)
    os.chmod(path, mode)


def _endpoint(raw: dict[str, Any]) -> dict[str, Any]:
    return {k: raw[k] for k in ENDPOINT_FIELDS if raw.get(k) not in (None, "")}


def _new_staging() -> Path:
    root().mkdir(mode=0o755, parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=".staged-", dir=root()))
    os.chmod(staged, 0o700)
    # Collected but never adopted (the run stopped first): no credential is left behind.
    atexit.register(lambda: os.environ.get(STAGED_ENV) == str(staged) and shutil.rmtree(staged, ignore_errors=True))
    return staged


def stage(endpoint: dict[str, Any], runtime: tuple[str, str], bootstrap: tuple[str, str], *,
          allow_plaintext: bool = False) -> Path:
    """Stage a collected connection in a root-only directory; returns it (and names it in STAGED_ENV)."""
    staged = _new_staging()
    _write(staged / "endpoint.json", json.dumps(_endpoint(endpoint), indent=2) + "\n", 0o644)
    for name, (user, password) in (("runtime", runtime), ("bootstrap", bootstrap)):
        _write(staged / f"{name}.credentials", json.dumps({"user": user, "password": password}) + "\n", 0o600)
    if allow_plaintext:
        _write(staged / "allow-plaintext", "the operator accepted plaintext tcp to another host\n", 0o644)
    _write_configs(staged, staged)
    os.environ[STAGED_ENV] = str(staged)
    return staged


def stage_from_config(path: str) -> Path:
    """Stage a configuration prepared with `ticket-board-oracle configure` (it must name bootstrap credentials)."""
    from .oracle_config import ConfigError, load, read_credentials

    try:
        config = load(path)
        if config.bootstrap_credentials_file is None:
            raise ConfigError(f"{path} names no bootstrap_credentials_file: creating the board needs the schema "
                              "owner's credentials, to migrate it")
        runtime = read_credentials(config.runtime_credentials_file, "runtime_credentials_file")
        bootstrap = read_credentials(config.bootstrap_credentials_file, "bootstrap_credentials_file")
        raw = json.loads(Path(path).read_text())
    except ConfigError as exc:
        raise OracleTenantError(f"switchyard new: {exc}") from None
    return stage(raw, runtime, bootstrap, allow_plaintext=os.environ.get(ALLOW_PLAINTEXT_ENV) == "1")


def collect_interactively(asker: Any) -> Path:
    """Ask for the remote database the way `ticket-board-oracle configure` does, and stage the answers.

    Which identifier the database is known by is asked, never guessed; passwords are read with echo off.
    """
    endpoint: dict[str, Any] = {"host": asker("Oracle database host (IP address or name)", "the host")}
    port = asker("Oracle listener port", "the port", "1521")
    endpoint["port"] = int(port) if str(port).strip().isdigit() else port
    kind = asker.choose("Is the database identified by a service name or a SID?",
                        {"service": "service_name", "sid": "sid"}, "the identifier")
    endpoint[kind] = asker("SID" if kind == "sid" else "Service name", f"the {kind}")
    if asker("Encrypt the connection with TLS (tcps)? y/n", "", "n").lower().startswith("y"):
        endpoint["protocol"] = "tcps"
        wallet = asker("Client wallet directory (empty for none)", "", "-")
        if wallet not in ("", "-"):
            endpoint["wallet_location"] = wallet
    endpoint["schema"] = asker("Schema that owns the board's tables", "the schema")
    runtime = (asker("Steady-state (runtime) account user name", "the runtime user").upper(),
               asker.secret("Steady-state (runtime) account password (not echoed)"))
    bootstrap = (asker("Schema owner (bootstrap) account user name", "the bootstrap user").upper(),
                 asker.secret("Schema owner (bootstrap) account password (not echoed)"))
    return stage(endpoint, runtime, bootstrap, allow_plaintext=os.environ.get(ALLOW_PLAINTEXT_ENV) == "1")


def _write_configs(directory: Path, final: Path) -> None:
    """bootstrap.json, board.json, listener.json and owner.json in `directory`, naming credential files as in `final`."""
    endpoint = json.loads((directory / "endpoint.json").read_text())
    for reader, runtime_file, extra in (
            ("bootstrap", "runtime.credentials", {"bootstrap_credentials_file": str(final / "bootstrap.credentials")}),
            ("board", "board-runtime.credentials", {}),
            ("listener", "listener-runtime.credentials", {}),
            ("owner", "listener-runtime.credentials",
             {"bootstrap_credentials_file": str(final / "owner-bootstrap.credentials")})):
        path = directory / f"{reader}.json"
        if path.exists():
            path.unlink()
        _write(path, json.dumps({**endpoint, "runtime_credentials_file": str(final / runtime_file), **extra},
                                indent=2) + "\n", 0o644)


def adopt_staged(project: str) -> bool:
    """Make this run's staged connection `project`'s record; False when this run staged none."""
    staged = os.environ.get(STAGED_ENV)
    if not staged:
        return False
    final = record_dir(project)
    if final.exists() or final.is_symlink():
        raise OracleTenantError(f"switchyard new: root already holds an Oracle board record for {project} ({final}); "
                                "tear that project down first, or choose another name")
    os.rename(staged, final)
    os.environ.pop(STAGED_ENV, None)
    _write_configs(final, final)
    os.chmod(final, 0o755)
    return True


def allow_plaintext_staged(directory: Path) -> bool:
    """Whether the operator accepted plaintext tcp to another host for this connection."""
    return (directory / "allow-plaintext").is_file()


def allow_plaintext(project: str) -> bool:
    return allow_plaintext_staged(record_dir(project))


def packet_steps(plan: Any, *, python: str, workflow_document: str) -> str:
    """The root packet's database steps for an Oracle tenant: credential copies, check, migrate, workflow.

    Run as root, from the release's own scripts. Nothing here starts, probes or administers PostgreSQL, or
    the remote database beyond the board's own schema.
    """
    try:
        from .project_provision import shell_quote as q
    except ImportError:  # pragma: no cover - supports direct script execution
        from project_provision import shell_quote as q

    directory = record_dir(plan.project)
    cli = q(f"{plan.board_current}/scripts/ticket-board-oracle")
    bootstrap = q(config_path(plan.project, "bootstrap"))
    plaintext = " --allow-plaintext" if allow_plaintext(plan.project) else ""
    return "\n".join([
        "# SYRD-642: this board keeps its tickets in a remote Oracle database. No PostgreSQL step runs.",
        f"sudo install -m 0600 -o {q(plan.service_user)} -g {q(plan.service_user)} "
        f"{q(str(directory / 'runtime.credentials'))} {q(str(directory / 'board-runtime.credentials'))}",
        f"sudo install -m 0600 -o {q(plan.owner_user)} -g {q(plan.owner_user)} "
        f"{q(str(directory / 'runtime.credentials'))} {q(str(directory / 'listener-runtime.credentials'))}",
        # The tenant's own deploy migrates, as the owner (SYRD-645).
        f"sudo install -m 0600 -o {q(plan.owner_user)} -g {q(plan.owner_user)} "
        f"{q(str(directory / 'bootstrap.credentials'))} {q(str(directory / 'owner-bootstrap.credentials'))}",
        f"sudo {q(python)} {cli} check --config {bootstrap} --before-migrate{plaintext}",
        f"sudo {q(python)} {cli} migrate --config {bootstrap} --project {q(plan.project)} "
        f"--ticket-prefix {q(plan.ticket_prefix)}",
        f"sudo {q(python)} {cli} apply-workflow --config {bootstrap} --project {q(plan.project)} "
        f"--document {workflow_document}",  # already a shell word: packet_companion addresses it from the packet
    ])


def _replace_once(text: str, old: str, new: str, what: str) -> str:
    """`old` replaced by `new`, when `old` is there exactly once; otherwise the template moved under us: stop."""
    if text.count(old) != 1:
        raise OracleTenantError(f"rendering an Oracle tenant's {what}: expected exactly one {old.strip()!r}, found "
                                f"{text.count(old)}; the PostgreSQL template changed and this rewrite must follow it")
    return text.replace(old, new)


def for_database(plan: Any, unit: str, text: str) -> str:
    """A rendered board, listener or canary unit, or root packet, for this tenant's database.

    The PostgreSQL rendering is the one template. For an Oracle tenant its PostgreSQL lines, and only those,
    are replaced: the server's environment, the ordering on postgresql.service, peer authentication and the
    database steps of the packet. A line not found exactly once stops the rendering.
    """
    if getattr(plan, "database_backend", "postgresql") != "oracle":  # the plan's own field: rendering looks at no host path
        return text
    if unit in ("board", "listener"):
        role = plan.service_role if unit == "board" else plan.listener_role
        url = plan.board_database_url if unit == "board" else plan.listener_database_url
        text = _replace_once(text, "After=network.target postgresql.service\n", "After=network.target\n", unit)
        if unit == "listener":
            text = _replace_once(text, "Wants=postgresql.service\n", "", unit)
        text = _replace_once(text, "Environment=PGHOST=/var/run/postgresql\n", unit_environment(plan.project, unit),
                             unit)
        for line in (f"Environment=PGDATABASE={plan.database}\n", f"Environment=PGUSER={role}\n",
                     f"Environment=TICKET_BOARD_DATABASE_URL={url}\n"):
            text = _replace_once(text, line, "", unit)
        if unit == "listener":
            text = _replace_once(text, f"Environment=TICKET_BOARD_NOTIFY_DATABASE_URL={url}\n", "", unit)
        return text
    if unit == "canary":
        text = _replace_once(text, "After=network.target postgresql.service\nWants=postgresql.service\n",
                             "After=network.target\n", unit)
        return text
    if unit == "packet":
        try:
            from . import project_provision as provision
        except ImportError:  # pragma: no cover - supports direct script execution
            import project_provision as provision

        text = _replace_once(text, provision.peer_auth_command(plan) + "\n",
                             "# SYRD-642: no PostgreSQL peer authentication: this board's database is Oracle\n", unit)
        database = [provision.postgres_sql_file_command(plan.project + "-database.sql", companion=True),
                    provision.postgres_sql_file_command(plan.board_current + "/scripts/ticket_board/schema.sql",
                                                        database_url=plan.admin_database_url),
                    f"sudo env TICKET_BOARD_ADMIN_DATABASE_URL={provision.shell_quote(plan.admin_database_url)} "
                    f"{provision.shell_quote(plan.board_current + '/scripts/ticket-board-migrate')}"]
        # The PostgreSQL block as the packet renders it: rbac.sql, then the workflow seed (SYRD-654).
        database.append(provision.postgres_sql_file_command(
            plan.board_current + "/scripts/ticket_board/rbac.sql", database_url=plan.admin_database_url))
        if plan.workflow_seed != "pgu-full":
            database.append(provision.postgres_sql_file_command(plan.project + "-workflow.sql",
                                                                database_url=plan.admin_database_url,
                                                                companion=True))
        if plan.workflow is None:
            raise OracleTenantError(f"switchyard new: {plan.project}'s board is on Oracle, which runs a declared "
                                    "workflow: create it with --workflow-config <document> (or --from an artifact "
                                    "that declares one)")
        steps = packet_steps(plan, python=provision.default_ticket_board_python(),
                             workflow_document=provision.packet_companion("workflow.json"))
        return _replace_once(text, "\n".join(database) + "\n", steps + "\n", unit)
    raise ValueError(f"no such unit: {unit}")


def refuse_unsupported(project: str, operation: str) -> None:
    """Stop an operation that would treat an Oracle tenant's board as PostgreSQL; nothing is changed."""
    if is_oracle(project):
        raise OracleTenantError(f"switchyard: {operation} is not supported yet for {project}, whose board is on Oracle "
                                "(docs/syrd-642-oracle-backend.md); nothing was changed")


def teardown_steps(project: str) -> list[tuple[str, tuple[str, ...]]]:
    """Remove root's Oracle record for this project, as (label, command): local files only, connecting to nothing.

    Each configuration goes through `ticket-board-oracle teardown --execute`, which removes it and its credentials
    files and never the remote database; then the endpoint and the directory itself.
    """
    steps: list[tuple[str, tuple[str, ...]]] = [
        (f"remove the Oracle {reader} configuration {config_path(project, reader)} and its credentials (local files "
         "only; the remote Oracle database, its schema and its rows are kept)",
         (sys.executable, str(RELEASE_CLI), "teardown", "--execute", "--config", config_path(project, reader)))
        for reader in ("listener", "board", "owner", "bootstrap")
    ]
    steps.append((f"remove the Oracle board record {record_dir(project)}", ("rm", "-rf", "--", str(record_dir(project)))))
    return steps


def precheck_without_postgresql(launcher: Any) -> None:
    """For this `switchyard new` only: an Oracle tenant has no local PostgreSQL database to look for or map to.

    The precheck reads these two through the launcher when it runs, so rebinding them here is the seam it was
    built with; nothing else in the run asks PostgreSQL anything.
    """
    launcher._database_exists = lambda database, *, runner: False
    launcher._ticket_board_table_count = lambda database, *, runner: 0
    # SYRD-645: nor a pg_hba/pg_ident mapping to install for its board service account. On an Oracle-only host
    # there is no PostgreSQL to install it in, and creating the project failed here.
    launcher._ensure_board_service_peer_auth = lambda plan, *, source_repo, runner: None


def database_status(project: str, *, runner: Any = subprocess.run, euid: int | None = None) -> dict[str, Any] | None:
    """`switchyard status`'s view of this tenant's board database, as the asking account may see it; None if not Oracle.

    Root reads it with its own copy, the tenant owner with the listener's; anyone else sees where it is. Read-only,
    bounded, and through the CLI (python-oracledb is in the shared venv, not where switchyard runs).
    """
    if not is_oracle(project):
        return None
    euid = os.geteuid() if euid is None else euid
    entry: dict[str, Any] = {"project": project, "database": describe(project)}
    reader = None
    if euid == 0:
        reader = "bootstrap"
    else:
        try:
            if (record_dir(project) / "listener-runtime.credentials").lstat().st_uid == euid:
                reader = "listener"
        except OSError:
            pass
    if reader is None:
        entry["state"] = f"not read: its state is visible to root and to {project}'s owner"
        return entry
    try:
        done = runner([sys.executable, str(RELEASE_CLI), "status", "--config", config_path(project, reader), "--json"],
                      capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
    except subprocess.TimeoutExpired:
        entry["state"] = "unreachable: no answer within 120 seconds"
        return entry
    if done.returncode != 0:
        said = (done.stderr or done.stdout or "").strip().splitlines()
        entry["state"] = f"unreachable: {said[-1] if said else f'exit {done.returncode}'}"
        return entry
    shown = json.loads(done.stdout)
    pending = shown.get("steps_pending")
    entry.update({key: shown[key] for key in ("steps_pending", "tickets", "queued_notices", "registered_roles")
                  if key in shown})
    entry["state"] = (shown.get("migrations", "unknown") if pending is None else "schema up to date" if not pending
                      else f"{pending} migration step(s) pending; see: sudo {OPERATOR_CLI} check --config "
                      f"{config_path(project, 'bootstrap')}")
    return entry


def database_line(entry: dict[str, Any]) -> str:
    counts = [f"{entry[key]} {key.replace('_', ' ')}" for key in ("tickets", "queued_notices") if key in entry]
    return f"switchyard: {entry['project']} board database: {entry['database']}: {'; '.join([entry['state'], *counts])}"


def describe(project: str) -> str:
    """One line for status: where this tenant's board database is, nothing secret."""
    try:
        from .oracle_config import load

        return f"oracle {load(config_path(project, 'board')).endpoint}"
    except Exception as exc:  # noqa: BLE001 -- status reports, it does not fail
        return f"oracle (configuration unreadable: {str(exc).splitlines()[0]})"

