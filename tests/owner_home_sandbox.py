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

Imported once, for its effect, by `team_launcher_test_helpers`, as the tmux bus
isolation and the sandboxed install roots are.
"""

from __future__ import annotations

import os
import pwd
import tempfile

from scripts import owner_writes

SHIPPED_OWNER_HOME = owner_writes.owner_home
INVOKING_ACCOUNT = pwd.getpwuid(os.getuid()).pw_name


def sandboxed_owner_home(owner_user: str) -> str:
    return tempfile.gettempdir() if owner_user == INVOKING_ACCOUNT else SHIPPED_OWNER_HOME(owner_user)


owner_writes.owner_home = sandboxed_owner_home
