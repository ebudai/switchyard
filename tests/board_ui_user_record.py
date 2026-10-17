"""SYRD-598: an approved board UI user, as root provisioning records one, for an in-process test board.

Production reads the approved users from the project's root-owned record (/etc/switchyard/projects/<slug>.json)
(the unit's --board-ui-record) and the connecting account from the kernel. A test cannot write a root-owned record, and its in-process server
runs as the test's own account -- which is the board service there, and so never a board UI user. This writes
the record into a directory the test owns, trusts that directory's owner beside root, and names the accounts
the record lists. Given `peer_uid`, every connection is taken to be that account's: the stand-in for "the
User's browser" in suites about the page and the grant. tests/board_ui_delegation_test.py's identity cases
leave the kernel's answer alone and connect under real, different uids.
"""

from __future__ import annotations

import importlib
import json
import os
import pwd
from pathlib import Path
from typing import Any

SLUG = "pgu"


def trust_board_ui_users(directory: Path, users: list[dict[str, Any]], *, peer_uid: int | None = None,
                         accounts: dict[str, int] | None = None, slug: str = SLUG, mode: int = 0o644,
                         excluded: dict[str, Any] | None = None) -> Path:
    """Record `users` (and root's `excluded` accounts: tenant_user, roles_group, role_accounts); `accounts`
    names the accounts that exist (default: as the record says). The board is pointed at the record as its
    unit's --board-ui-record would."""
    from scripts.ticket_board import ui_user_identity as identity

    importlib.reload(identity)  # its defaults: root's registry, the kernel's answer
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o755)
    record = directory / f"{slug}.json"
    record.write_text(json.dumps({"schema": "switchyard.project-registry.v1", "slug": slug,
                                  identity.RECORD_KEY: {"users": users, **(excluded or {})}}, indent=2) + "\n",
                      encoding="utf-8")
    record.chmod(mode)
    identity.RECORD_PATH = record
    identity.TRUSTED_OWNER_UIDS = frozenset({0, os.getuid()})
    names = accounts if accounts is not None else {
        user["account"]: user["uid"] for user in users if isinstance(user, dict) and "account" in user}
    identity.account_uid = lambda name: names[name] if name in names else pwd.getpwnam(name).pw_uid
    if peer_uid is not None:
        identity.tcp_peer_uid = lambda connection, tables=identity.PROC_TABLES: peer_uid
    return record
