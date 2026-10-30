#!/usr/bin/env python3
"""A tenant board database's cluster-superuser work, done by root as the cluster's own account (SYRD-706).

Migrations and `rbac.sql` need a role that can create roles, functions and extensions. They used to run as the
tenant, from the tenant's own release, connecting `user=postgres` -- which worked only because the cluster trusted
every local connection, so any account on the host was the cluster superuser, over every tenant's database.

Here they run as the `postgres` OS account, which PostgreSQL's peer authentication maps to the `postgres` role once
nothing is trusted, and only from root's trusted exact release of the commit being deployed:

- the release is `privileged_operations.trusted_release_root(commit)`, and every file the superuser executes or reads
  -- the migration runner, `rbac.sql`, the migrations directory and each migration -- is root's and writable by no
  one else, so no tenant chooses what the superuser runs;
- the database is named by root's own records (the board unit root renders from its staged provisioning), and must
  be a plain database name;
- the environment is emptied and the working directory is `/`: nothing the caller had reaches the runner.

The tenant's own deploy then skips the work (`TICKET_BOARD_SKIP_MIGRATIONS=1`, honoured by every release since
2026-07), so it needs no database authority beyond its board's.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import urllib.parse
from pathlib import Path
from typing import Any, Callable

POSTGRES_ACCOUNT = "postgres"
SOCKET_DIR = "/var/run/postgresql"
DATABASE = re.compile(r"[a-z_][a-z0-9_]{0,62}")
SHARED_WRITE = stat.S_IWGRP | stat.S_IWOTH
MIGRATION_RUNNER = "scripts/ticket-board-migrate"
RBAC_SQL = "scripts/ticket_board/rbac.sql"
MIGRATIONS = "scripts/ticket_board/migrations"
TIMEOUT_SECONDS = 600


class DatabaseAdminError(Exception):
    """The database work cannot be done as root would do it; the message says why."""


def database_from_unit(unit_text: str) -> str:
    """The database a board unit root rendered connects to: its `TICKET_BOARD_DATABASE_URL`'s path."""
    for line in unit_text.splitlines():
        key, _, value = line.strip().partition("=")
        if key != "Environment":
            continue
        name, _, url = value.partition("=")
        if name != "TICKET_BOARD_DATABASE_URL":
            continue
        database = urllib.parse.urlsplit(url.strip().strip('"')).path.lstrip("/")
        if not DATABASE.fullmatch(database):
            raise DatabaseAdminError(f"the board unit names no plain database ({database!r})")
        return database
    raise DatabaseAdminError("the board unit sets no TICKET_BOARD_DATABASE_URL")


def admin_url(database: str) -> str:
    """The cluster superuser's URL: the local socket, no user and no password -- peer authentication decides."""
    if not DATABASE.fullmatch(database):
        raise DatabaseAdminError(f"{database!r} is not a plain database name")
    return f"postgresql:///{database}?host={SOCKET_DIR}"


def _root_only(path: Path, *, directory: bool = False) -> None:
    # Whose root's releases are: privileged_operations' own, read when this runs, so it is one value everywhere.
    from .privileged_operations import TRUSTED_OWNER_UID

    try:
        info = os.lstat(path)
    except OSError as exc:
        raise DatabaseAdminError(f"{path} cannot be inspected ({exc.strerror})") from None
    kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not kind:
        raise DatabaseAdminError(f"{path} is not a {'directory' if directory else 'regular file'}")
    if info.st_uid != TRUSTED_OWNER_UID or info.st_mode & SHARED_WRITE:
        raise DatabaseAdminError(f"{path} is not owned by uid {TRUSTED_OWNER_UID} and writable only by it, so it "
                                 "cannot be what the cluster superuser runs")


def commands(release: Path, database: str) -> list[list[str]]:
    """What root runs, in order: the release's migrations, then its RBAC, each as the `postgres` account."""
    url = admin_url(database)
    environment = ["env", "-i", "PATH=/usr/bin:/bin", "LANG=C.UTF-8"]
    return [
        ["runuser", "-u", POSTGRES_ACCOUNT, "--", *environment, f"TICKET_BOARD_ADMIN_DATABASE_URL={url}",
         str(release / MIGRATION_RUNNER)],
        ["runuser", "-u", POSTGRES_ACCOUNT, "--", *environment, "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", url,
         "-f", str(release / RBAC_SQL)],
    ]


def administer_release_database(
    release: Path,
    database: str,
    *,
    runner: Callable[..., Any] = subprocess.run,
    trusted_release: Callable[[str], str] | None = None,
) -> list[str]:
    """Migrate `database` and apply its RBAC from root's trusted `release`, as the cluster's own account.

    Returns the problems; empty when both steps succeeded. Nothing runs unless every check passes first.
    """
    if trusted_release is None:
        from .privileged_operations import trusted_release_root as trusted_release
    try:
        expected = Path(trusted_release(release.name))
    except Exception as exc:  # the resolver's refusal is the useful message
        return [str(exc)]
    if release != expected:
        return [f"{release} is not root's trusted release {expected}"]
    try:
        admin_url(database)
        _root_only(release / MIGRATION_RUNNER)
        _root_only(release / RBAC_SQL)
        _root_only(release / MIGRATIONS, directory=True)
        for migration in sorted((release / MIGRATIONS).iterdir()):
            _root_only(migration, directory=migration.is_dir() and not migration.is_symlink())
    except DatabaseAdminError as exc:
        return [str(exc)]
    for command in commands(release, database):
        step ="migrating" if command[-1].endswith(MIGRATION_RUNNER) else "applying the RBAC of"
        try:
            done = runner(command, cwd="/", stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, timeout=TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return [f"{step} {database} from {release} as {POSTGRES_ACCOUNT} did not run ({exc})"]
        if done.returncode != 0:
            detail = (str(done.stderr or "").strip() or str(done.stdout or "").strip() or "no output")[-400:]
            return [f"{step} {database} from {release} as {POSTGRES_ACCOUNT} failed (exit {done.returncode}): {detail}"]
    return []
