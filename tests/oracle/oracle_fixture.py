"""Disposable Oracle board schemas on the SYRD-642 test service, for suites (never a real tenant's database).

`available()` says whether the test service and its credentials are here. When
they are not, a suite reports itself skipped rather than passing: Oracle
behaviour is only ever claimed from a real Oracle.

`disposable_schema()` creates two accounts on the test service:
* a schema owner, which can create tables and sequences in its own schema;
* a runtime account, with CREATE SESSION only.
It writes their generated passwords to owner-only files in a private temp
directory, yields the bootstrap and steady-state `OracleConfig`s, and drops
both accounts (CASCADE) afterwards.
"""

from __future__ import annotations

import json
import os
import secrets
import string
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import oracle_test_credentials as credentials  # noqa: E402


def available() -> str:
    """'' when the test service can be used, else why not."""
    try:
        import oracledb  # noqa: F401
    except ImportError:
        return "python-oracledb is not installed"
    if not (credentials.CREDENTIAL_DIR / "system.password").exists():
        return f"no Oracle test service credentials in {credentials.CREDENTIAL_DIR}"
    try:
        with _system() as conn:
            conn.cursor().execute("SELECT 1 FROM dual").fetchone()
    except Exception as exc:  # noqa: BLE001
        return f"the Oracle test service at {credentials.DSN} is not reachable: {str(exc).splitlines()[0]}"
    return ""


#: The test service's container database, reached by SID: Oracle Free's pluggable database has no SID of its own.
CDB_SID = "FREE"


@contextmanager
def _system(*, by_sid: bool = False):
    import oracledb

    endpoint = {"host": "127.0.0.1", "port": 1521, "sid": CDB_SID} if by_sid else {"dsn": credentials.DSN}
    conn = oracledb.connect(user="SYSTEM", password=credentials.read_secret("system.password"), **endpoint)
    try:
        yield conn
    finally:
        conn.close()


def _password() -> str:
    return secrets.choice(string.ascii_letters) + "".join(
        secrets.choice(string.ascii_letters + string.digits) for _ in range(23))


def write_credentials(path: Path, user: str, password: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump({"user": user, "password": password}, handle)


@contextmanager
def disposable_schema(tag: str = "T", *, by_sid: bool = False, host: str = "127.0.0.1",
                      port: int = 1521) -> Iterator[tuple[object, object, Path]]:
    """(bootstrap config, runtime config, private dir) for a fresh owner and runtime account; dropped after.

    `by_sid` puts the board in the container database, reached by SID, with common (C##) accounts.
    `host` and `port` say where the written configuration points (a relay in front of the service, say).
    """
    from scripts.ticket_board.oracle_config import load

    suffix = secrets.token_hex(3).upper()
    common = "C##" if by_sid else ""
    owner, runtime = f"{common}SYRD_{tag}{suffix}_OWN", f"{common}SYRD_{tag}{suffix}_RUN"
    passwords = {owner: _password(), runtime: _password()}
    scope = " CONTAINER=CURRENT" if by_sid else ""
    with tempfile.TemporaryDirectory(prefix="syrd642-oracle-") as tmp:
        root = Path(tmp)
        os.chmod(root, 0o700)
        write_credentials(root / "bootstrap.credentials", owner, passwords[owner])
        write_credentials(root / "runtime.credentials", runtime, passwords[runtime])
        with _system(by_sid=by_sid) as conn:
            cursor = conn.cursor()
            for name, value in passwords.items():
                cursor.execute(f'CREATE USER {name} IDENTIFIED BY "{value}" QUOTA UNLIMITED ON USERS'
                               + (" CONTAINER=ALL" if by_sid else ""))
                cursor.execute(f"GRANT CREATE SESSION TO {name}{scope}")
            cursor.execute(f"GRANT CREATE TABLE, CREATE SEQUENCE TO {owner}{scope}")
        try:
            endpoint = {"sid": CDB_SID} if by_sid else {"service_name": credentials.DSN.rsplit("/", 1)[1]}
            (root / "board.json").write_text(json.dumps({
                "host": host, "port": port, **endpoint, "schema": owner,
                "runtime_credentials_file": str(root / "runtime.credentials"),
                "bootstrap_credentials_file": str(root / "bootstrap.credentials")}))
            config = load(root / "board.json")
            yield config.as_bootstrap(), config, root
        finally:
            drop(by_sid, runtime, owner)


def drop(by_sid: bool, *names: str) -> None:
    """Drop these disposable accounts, ending any session of theirs still closing (a relayed one can linger)."""
    import oracledb

    with _system(by_sid=by_sid) as conn:
        cursor = conn.cursor()
        for name in names:
            for attempt in range(10):
                for sid, serial in cursor.execute("SELECT sid, serial# FROM v$session WHERE username = :u",
                                                  u=name).fetchall():
                    try:
                        cursor.execute(f"ALTER SYSTEM KILL SESSION '{sid},{serial}' IMMEDIATE")
                    except oracledb.DatabaseError:
                        pass  # it ended by itself meanwhile
                try:
                    cursor.execute(f"DROP USER {name} CASCADE")
                    break
                except oracledb.DatabaseError as exc:
                    if "ORA-01940" not in str(exc) or attempt == 9:
                        raise
                    time.sleep(1)
