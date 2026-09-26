"""Doing something as another account: a forked child that drops to it first.

`_run_as_account` runs an action in a child that has become the target
uid/gid (`_drop_to_account`) before touching anything. Root uses it to write
into a tree another account controls. An unprivileged caller can only be
itself, so it refuses.

A leaf: several modules take `_drop_to_account` as a default argument, so it
has to be one object that each of them imports at its top. Moved out of
`scripts/team_launcher.py` unchanged (SYRD-306); `team_launcher` still exports
both names.
"""

from __future__ import annotations

import os
from typing import Callable


def _drop_to_account(uid: int, gid: int) -> None:
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)


def _run_as_account(
    uid: int,
    gid: int,
    action: Callable[[], None],
    *,
    geteuid: Callable[[], int] = os.geteuid,
    drop: Callable[[int, int], None] = _drop_to_account,
) -> bool:
    """Run `action` in a child that has BECOME `uid`/`gid` first.

    For writes root has to make into a tree another account controls. Root
    writing there itself leaves root-owned directories behind that the account
    then cannot write into, and can be steered through a symlink the account
    planted; a child that has dropped to the account first can do neither. An
    unprivileged caller can only ever be itself, so it refuses rather than write
    as somebody it is not. True when the action completed.
    """
    pid = os.fork()
    if pid == 0:  # pragma: no cover - exercised through the exit status
        code = 1
        try:
            if geteuid() == 0:
                drop(uid, gid)
            elif geteuid() != uid:
                os._exit(5)
            action()
            code = 0
        except BaseException:
            code = 4
        os._exit(code)
    _pid, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status) == 0
