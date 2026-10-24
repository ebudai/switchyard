#!/usr/bin/env python3
"""The disposable Oracle test service's credentials (SYRD-642): generated here, owner-only, never printed.

usage: oracle_test_credentials.py init           -- create any missing password file; print only the paths
       oracle_test_credentials.py runtime-user   -- (after the service is up) create the steady-state account

Three passwords live in `CREDENTIAL_DIR` (mode 0700), one file each (mode 0600):

* `system.password`    -- the container's SYS/SYSTEM password: the test service's own administrator, used
                          only to create the runtime account below; never a board credential;
* `bootstrap.password` -- the schema owner (`SYRD_BOARD_OWNER`), which creates and migrates the board's
                          objects: the bootstrap/migration credential;
* `runtime.password`   -- the steady-state board account (`SYRD_BOARD_RUNTIME`): CREATE SESSION, and only
                          what the owner grants it on the board's own objects.

The root setup script (oracle-free-test-service.sh) reads the first two to create the container, through
an env file, never a command line. Nothing here prints a secret or passes one in an argument.
"""

from __future__ import annotations

import os
import secrets
import stat
import string
import sys
from pathlib import Path

CREDENTIAL_DIR = Path(os.environ.get("SWITCHYARD_ORACLE_TEST_DIR")
                      or Path.home() / ".config" / "switchyard-oracle-test")
FILES = ("system.password", "bootstrap.password", "runtime.password")
OWNER_USER, RUNTIME_USER = "SYRD_BOARD_OWNER", "SYRD_BOARD_RUNTIME"
DSN = os.environ.get("SWITCHYARD_ORACLE_TEST_DSN", "127.0.0.1:1521/FREEPDB1")
#: Oracle's unquoted-password rules, and the setup script's own check: a letter, then letters and digits.
ALPHABET = string.ascii_letters + string.digits


def generate() -> str:
    return secrets.choice(string.ascii_letters) + "".join(secrets.choice(ALPHABET) for _ in range(23))


def ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
        raise SystemExit(f"{path} is not a directory this account owns")
    os.chmod(path, 0o700)


def read_secret(name: str) -> str:
    path = CREDENTIAL_DIR / name
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise SystemExit(f"{path} must be a regular file owned by this account with mode 0600")
    return path.read_text().strip()


def init() -> None:
    ensure_private_dir(CREDENTIAL_DIR)
    for name in FILES:
        path = CREDENTIAL_DIR / name
        if path.exists():
            continue
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(generate() + "\n")
    for name in FILES:
        read_secret(name)  # every file is what the setup script will accept
        print(CREDENTIAL_DIR / name)


def runtime_user() -> None:
    import oracledb

    with oracledb.connect(user="SYSTEM", password=read_secret("system.password"), dsn=DSN) as conn:
        cursor = conn.cursor()
        exists = cursor.execute("SELECT count(*) FROM dba_users WHERE username = :name", name=RUNTIME_USER).fetchone()[0]
        if not exists:
            # DDL takes no bind variables: the password is the checked alphabet only, quoted.
            password = read_secret("runtime.password")
            if not (password[:1].isalpha() and all(ch in ALPHABET for ch in password)):
                raise SystemExit("runtime.password is not in the expected alphabet")
            cursor.execute(f'CREATE USER {RUNTIME_USER} IDENTIFIED BY "{password}"')
        cursor.execute(f"GRANT CREATE SESSION TO {RUNTIME_USER}")
    print(f"{RUNTIME_USER}: present, with CREATE SESSION only")


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action == "init":
        init()
    elif action == "runtime-user":
        runtime_user()
    else:
        raise SystemExit(__doc__)
