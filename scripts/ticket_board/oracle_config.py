"""An Oracle board's connection configuration, and connecting with it (SYRD-642).

A board chooses its backend explicitly: `TICKET_BOARD_BACKEND=oracle` (PostgreSQL
is the default and is unchanged), with `TICKET_BOARD_ORACLE_CONFIG` naming a JSON
file. That file holds the endpoint and nothing secret:

    {
      "host": "10.20.30.40",                 # an IPv4 or IPv6 address, or a host name; never assumed local
      "port": 1522,
      "sid": "ORCL",                          # exactly one of "sid" and "service_name"
      "protocol": "tcps",                     # optional: "tcp" (default) or "tcps"
      "wallet_location": "/etc/switchyard/oracle/wallet",               # optional, tcps only
      "wallet_password_file": "/etc/switchyard/oracle/wallet.password", # optional, tcps only
      "ssl_server_dn_match": true,            # optional, default true
      "ssl_server_cert_dn": "CN=db.example.com",                        # optional
      "schema": "BOARD_OWNER",                # the schema that owns the board's objects
      "runtime_credentials_file": "/etc/switchyard/oracle/runtime.credentials",
      "bootstrap_credentials_file": "/etc/switchyard/oracle/bootstrap.credentials"  # optional
    }

A SID is passed in the driver's SID field and a service name in its service-name
field. Neither is ever rewritten as the other or looked up as a TNS alias: a
database known by SID only is reached by SID.

**Credentials** are kept in separate files, one per account, each holding
`{"user": "...", "password": "..."}`:
* The **runtime** account is the board service and listener. It holds rows on
  the board's tables only.
* The optional **bootstrap** account is the schema owner. Only
  `ticket-board-oracle migrate` uses it: it creates and migrates the objects,
  and grants the runtime account.
A credentials file must be a regular file, not a link, readable by its owner
alone (`mode & 0o077 == 0`), and owned by the account reading it, so a board
cannot be pointed at a credential somebody else can change.

Credentials are read at connect time only. Neither a user name nor a password is
kept in this configuration, printed, logged, or put in an argument or an
environment variable.

**Connecting** uses python-oracledb's thin mode, with no Oracle client libraries
on the host. A connect attempt is bounded: `CONNECT_TIMEOUT_SECONDS` per attempt,
retried `CONNECT_RETRIES` times `RETRY_DELAY_SECONDS` apart. A statement is never
retried, so a write is never applied twice.

The board server borrows sessions from a pool (`create_pool`): at most
`POOL_MAX_SESSIONS`, each pinged as it is borrowed, so a session an outage
broke is replaced rather than failing the next request, and a board recovers by
itself once its database is back. The listener and the lifecycle commands hold
one connection of their own (`connect`).
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

BACKEND_ENV = "TICKET_BOARD_BACKEND"
CONFIG_ENV = "TICKET_BOARD_ORACLE_CONFIG"
BACKENDS = ("postgres", "oracle")
#: An Oracle identifier as the board names schemas and accounts: unquoted, so case-insensitive and safe in DDL.
IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]{0,127}$")
#: A SID or service name: letters, digits and . _ - $ #, as listeners register them.
SERVICE = re.compile(r"^[A-Za-z][A-Za-z0-9._$#-]{0,63}$")
HOSTNAME = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,62})(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}))*$")
FIELDS = {"host", "port", "sid", "service_name", "protocol", "wallet_location", "wallet_password_file",
          "ssl_server_dn_match", "ssl_server_cert_dn", "schema", "runtime_credentials_file",
          "bootstrap_credentials_file"}
CONNECT_TIMEOUT_SECONDS = 10.0
CONNECT_RETRIES = 2
RETRY_DELAY_SECONDS = 1
#: The board server's sessions: at most this many, and a request waits at most POOL_WAIT_MS for one.
POOL_MAX_SESSIONS = 10
POOL_WAIT_MS = 10_000


class ConfigError(ValueError):
    """The Oracle configuration is unusable; the message never contains a secret."""


def backend(environ: dict[str, str] | None = None) -> str:
    """The board's storage backend: `postgres` unless `TICKET_BOARD_BACKEND` names another."""
    chosen = (os.environ if environ is None else environ).get(BACKEND_ENV, "").strip().lower() or "postgres"
    if chosen not in BACKENDS:
        raise ConfigError(f"{BACKEND_ENV} must be one of {', '.join(BACKENDS)}, not {chosen!r}")
    return chosen


def private_file(path: Path, what: str) -> Path:
    """`path`, when it is a regular file only its owner, the reading account, can read or change."""
    if not path.is_absolute():
        raise ConfigError(f"{what} must be an absolute path: {path}")
    try:
        info = path.lstat()
    except OSError as exc:
        raise ConfigError(f"{what} {path} cannot be read: {exc.strerror}") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ConfigError(f"{what} {path} must be a regular file, not a link")
    if info.st_mode & 0o077:
        raise ConfigError(f"{what} {path} is readable or writable by others (mode {stat.S_IMODE(info.st_mode):o}); "
                          "make it mode 0600")
    if info.st_uid != os.geteuid():
        raise ConfigError(f"{what} {path} is not owned by the account reading it")
    return path


def read_credentials(path: Path, what: str) -> tuple[str, str]:
    """(user, password) from a private credentials file; errors name the file, never its contents."""
    text = private_file(path, what).read_text()  # its own refusal, never re-worded as a format error below
    try:
        raw = json.loads(text)
    except ValueError:
        raise ConfigError(f'{what} {path} must be a JSON object {{"user": ..., "password": ...}}') from None
    if not isinstance(raw, dict) or set(raw) != {"user", "password"}:
        raise ConfigError(f'{what} {path} must hold exactly "user" and "password"')
    user, password = raw["user"], raw["password"]
    if not isinstance(user, str) or not IDENTIFIER.fullmatch(user):
        raise ConfigError(f"{what} {path}: user must be an unquoted Oracle identifier "
                          "(a letter, then letters, digits, _ $ #)")
    if not isinstance(password, str) or not password or any(c in password for c in "\n\r\x00"):
        raise ConfigError(f"{what} {path}: password must be one non-empty line")
    return user.upper(), password


def valid_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return bool(HOSTNAME.fullmatch(host))


@dataclass(frozen=True)
class OracleConfig:
    host: str
    port: int
    schema: str
    runtime_credentials_file: Path
    sid: str = ""
    service_name: str = ""
    protocol: str = "tcp"
    wallet_location: Path | None = None
    wallet_password_file: Path | None = None
    ssl_server_dn_match: bool = True
    ssl_server_cert_dn: str = ""
    bootstrap_credentials_file: Path | None = None
    #: Which credentials this configuration connects with: "runtime", or "bootstrap" for migrate.
    account: str = "runtime"
    source: str = ""

    def __repr__(self) -> str:  # names files, never what is in them
        return (f"OracleConfig(endpoint={self.endpoint!r}, schema={self.schema!r}, account={self.account!r}, "
                f"credentials_file={str(self.credentials_file)!r})")

    @property
    def endpoint(self) -> str:
        """The endpoint, as diagnostics name it: host, port, and which identifier is which."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        named = f"SID {self.sid}" if self.sid else f"service {self.service_name}"
        return f"{self.protocol}://{host}:{self.port} {named}"

    @property
    def credentials_file(self) -> Path:
        if self.account == "bootstrap":
            if self.bootstrap_credentials_file is None:
                raise ConfigError("this configuration has no bootstrap_credentials_file; migrate runs as the schema "
                                  "owner")
            return self.bootstrap_credentials_file
        return self.runtime_credentials_file

    def as_bootstrap(self) -> "OracleConfig":
        """The same endpoint, connecting as the schema owner (refused when no bootstrap credentials are configured)."""
        bootstrap = replace(self, account="bootstrap")
        bootstrap.credentials_file  # noqa: B018 -- refuses now, not at connect time
        return bootstrap

    def credentials(self) -> tuple[str, str]:
        return read_credentials(self.credentials_file, f"{self.account}_credentials_file")

    def runtime_user(self) -> str:
        """The steady-state account the bootstrap account grants to."""
        return read_credentials(self.runtime_credentials_file, "runtime_credentials_file")[0]

    def describe(self) -> dict[str, Any]:
        """What status and diagnostics may show: the endpoint and file names, no account names or secrets."""
        return {"backend": "oracle", "endpoint": self.endpoint, "host": self.host, "port": self.port,
                "sid": self.sid, "service_name": self.service_name, "protocol": self.protocol,
                "wallet": bool(self.wallet_location), "schema": self.schema,
                "runtime_credentials_file": str(self.runtime_credentials_file),
                "bootstrap_credentials_file": str(self.bootstrap_credentials_file or ""), "source": self.source}

    def endpoint_params(self) -> dict[str, Any]:
        """The driver's endpoint fields: SID in `sid`, service name in `service_name`, never one as the other."""
        params: dict[str, Any] = {"host": self.host, "port": self.port, "protocol": self.protocol,
                                  "tcp_connect_timeout": CONNECT_TIMEOUT_SECONDS, "retry_count": CONNECT_RETRIES,
                                  "retry_delay": RETRY_DELAY_SECONDS}
        params["sid" if self.sid else "service_name"] = self.sid or self.service_name
        if self.protocol == "tcps":
            params["ssl_server_dn_match"] = self.ssl_server_dn_match
            if self.ssl_server_cert_dn:
                params["ssl_server_cert_dn"] = self.ssl_server_cert_dn
            if self.wallet_location:
                params["wallet_location"] = str(self.wallet_location)
        return params

    def connect_kwargs(self) -> dict[str, Any]:
        user, password = self.credentials()
        kwargs = {**self.endpoint_params(), "user": user, "password": password}
        if self.protocol == "tcps" and self.wallet_password_file:
            kwargs["wallet_password"] = private_file(self.wallet_password_file,
                                                     "wallet_password_file").read_text().strip()
        return kwargs


def parse(raw: Any, source: str = "") -> OracleConfig:
    """A validated configuration from its JSON object; refuses unknown fields and unusable values, echoing no secret."""
    where = f"Oracle configuration {source}".rstrip()
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a JSON object")
    unknown = sorted(set(raw) - FIELDS)
    if unknown:
        hint = ""
        if any(word in name for name in unknown for word in ("password", "user")):
            hint = "; user names and passwords belong in the credentials files, never in the configuration"
        elif "dsn" in unknown:
            hint = "; give host, port and exactly one of sid or service_name"
        raise ConfigError(f"{where} has unknown fields: {', '.join(unknown)}{hint}")
    host = raw.get("host")
    if not isinstance(host, str) or not valid_host(host.strip()):
        raise ConfigError(f"{where} needs host: an IPv4 or IPv6 address, or a host name")
    port = raw.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigError(f"{where} needs port: a number from 1 to 65535")
    named = [name for name in ("sid", "service_name") if str(raw.get(name) or "").strip()]
    if len(named) != 1:
        raise ConfigError(f"{where} needs exactly one of sid and service_name"
                          + (" (it gives both)" if len(named) == 2 else " (it gives neither)"))
    identifier = str(raw[named[0]]).strip()
    if not SERVICE.fullmatch(identifier):
        raise ConfigError(f"{named[0]} must be letters, digits and . _ - $ #, starting with a letter")
    protocol = str(raw.get("protocol") or "tcp").strip().lower()
    if protocol not in ("tcp", "tcps"):
        raise ConfigError("protocol must be tcp or tcps")
    schema = raw.get("schema")
    if not isinstance(schema, str) or not IDENTIFIER.fullmatch(schema):
        raise ConfigError(f"{where} needs schema: an unquoted Oracle identifier (a letter, then letters, digits, "
                          "_ $ #)")
    paths: dict[str, Path] = {}
    for name in ("runtime_credentials_file", "bootstrap_credentials_file", "wallet_location", "wallet_password_file"):
        value = raw.get(name)
        if value in (None, ""):
            continue
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ConfigError(f"{name} must be an absolute path")
        paths[name] = Path(value)
    if "runtime_credentials_file" not in paths:
        raise ConfigError(f"{where} needs runtime_credentials_file")
    if protocol == "tcp" and ({"wallet_location", "wallet_password_file"} & set(paths) or raw.get("ssl_server_cert_dn")):
        raise ConfigError("a wallet or server certificate DN is used only with protocol tcps")
    return OracleConfig(host=host.strip(), port=port, schema=schema.upper(),
                        runtime_credentials_file=paths["runtime_credentials_file"],
                        sid=identifier if named[0] == "sid" else "",
                        service_name=identifier if named[0] == "service_name" else "", protocol=protocol,
                        wallet_location=paths.get("wallet_location"),
                        wallet_password_file=paths.get("wallet_password_file"),
                        ssl_server_dn_match=bool(raw.get("ssl_server_dn_match", True)),
                        ssl_server_cert_dn=str(raw.get("ssl_server_cert_dn") or ""),
                        bootstrap_credentials_file=paths.get("bootstrap_credentials_file"), source=source)


def load(path: str | Path) -> OracleConfig:
    source = Path(path)
    try:
        raw = json.loads(source.read_text())
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Oracle configuration {source} cannot be read as JSON: {exc}") from None
    return parse(raw, str(source))


def from_environment(environ: dict[str, str] | None = None) -> OracleConfig:
    environ = dict(os.environ if environ is None else environ)
    path = environ.get(CONFIG_ENV, "").strip()
    if not path:
        raise ConfigError(f"{BACKEND_ENV}=oracle needs {CONFIG_ENV} to name the Oracle configuration file")
    return load(path)


def _prepare(connection: Any, config: OracleConfig, user: str) -> None:
    """A new session works in the board's schema, on UTC."""
    with connection.cursor() as cursor:
        if config.schema != user:
            cursor.execute(f"ALTER SESSION SET CURRENT_SCHEMA = {config.schema}")
        cursor.execute("ALTER SESSION SET TIME_ZONE = 'UTC'")


def connect(config: OracleConfig) -> Any:
    """One thin-mode connection, in the board's schema, on UTC."""
    import oracledb

    oracledb.defaults.fetch_lobs = False
    kwargs = config.connect_kwargs()
    connection = oracledb.connect(**kwargs)
    _prepare(connection, config, kwargs["user"])
    return connection


def create_pool(config: OracleConfig, *, max_sessions: int = POOL_MAX_SESSIONS) -> Any:
    """The board server's session pool: bounded, every session prepared once and pinged when borrowed.

    The credentials are read once, here; a changed password takes effect when the board restarts.
    """
    import oracledb

    oracledb.defaults.fetch_lobs = False
    kwargs = config.connect_kwargs()
    return oracledb.create_pool(min=0, max=max_sessions, increment=1, getmode=oracledb.POOL_GETMODE_TIMEDWAIT,
                                wait_timeout=POOL_WAIT_MS, ping_interval=0,
                                session_callback=lambda connection, _tag: _prepare(connection, config, kwargs["user"]),
                                **kwargs)
