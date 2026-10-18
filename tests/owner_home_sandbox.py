"""SYRD-608: the owner home these suites' own tenants are written in, when root writes for them.

As root on a tenant's behalf the launcher writes, hands over and reads back the
tenant's files by descriptor from the owner's home
(`scripts/owner_writes.owner_home`), and refuses anything outside it. The
suites that drive root's branch with a stood-in euid make tenants owned by the
account running them, kept in temporary directories -- never in that account's
real home, which no suite may write into. So, for that account only, the
temporary directory is the owner's home for root's writes. Any other owner, and
every other home lookup, is untouched; the anchor itself is exercised against a
home of its own by `tests/owner_file_writes_test.py`, which does not import this.

Root stages what it writes in a directory only it can change, the home's parent
on a host (`tenant_paths.Root._stage_parent`). A suite's home in the temporary
directory has `/` or the shared, sticky temporary directory as its parent,
neither of which is that, so for any home in the temporary directory the staging
directory is a private one of this process's, on the same filesystem.

Suites that stand in an owner home of their own in the temporary directory, and
do not load `team_launcher_test_helpers`, import this for the staging alone.

Imported once, for its effect, by `team_launcher_test_helpers`, as the tmux bus
isolation and the sandboxed install roots are.
"""

from __future__ import annotations

import atexit
import os
import pwd
import shutil
import tempfile

from scripts import owner_writes, tenant_paths

SHIPPED_OWNER_HOME = owner_writes.owner_home
try:
    INVOKING_ACCOUNT = pwd.getpwuid(os.getuid()).pw_name
except KeyError:  # a uid with no account, as some suites run their children: nobody's home to stand in
    INVOKING_ACCOUNT = ""


def sandboxed_owner_home(owner_user: str) -> str:
    return tempfile.gettempdir() if owner_user and owner_user == INVOKING_ACCOUNT else SHIPPED_OWNER_HOME(owner_user)


owner_writes.owner_home = sandboxed_owner_home

SHIPPED_STAGE_PARENT = tenant_paths.Root._stage_parent
#: Made on first use, so a process that never stages leaves nothing behind; removed when it exits.
STAGE: dict[str, object] = {}


def stage_descriptor() -> int:
    if "fd" not in STAGE:
        path = tempfile.mkdtemp(prefix="switchyard-owner-stage.")
        STAGE.update(path=path, fd=os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC))
        atexit.register(shutil.rmtree, path, True)
    return STAGE["fd"]  # type: ignore[return-value]


def sandboxed_stage_parent(root: tenant_paths.Root) -> int | None:
    temporary = tempfile.gettempdir()
    inside = root.home == temporary or root.home.startswith(temporary.rstrip("/") + "/")
    return stage_descriptor() if inside else SHIPPED_STAGE_PARENT(root)


tenant_paths.Root._stage_parent = sandboxed_stage_parent


def hold_owner_root(owner_user: str, root: Path) -> None:
    """For a suite whose stand-in owner keeps its state in a scratch directory: that directory is its home here."""
    shipped = owner_writes.owner_home
    owner_writes.owner_home = lambda user: str(root) if user == owner_user else shipped(user)
