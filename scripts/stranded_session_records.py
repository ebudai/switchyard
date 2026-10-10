"""A role's own store takes back the session record its pane left in the project's shared one (SYRD-569).

With `role_state_isolation`, the launcher reads each role's resumable session
from `<session_dir>/roles/<role>/` (`role_session_dir`). The provider hooks are
installed once per account, though, with `--session-dir <session_dir>`: they
cannot name a role, and the hook let that flag beat the pane's own
`TICKET_BOARD_PANE_SESSION_DIR`. So every role recorded its conversation in the
shared parent, where no launch looked:

* a project created isolated (Otto) found nothing at every launch and started
  every role fresh;
* a repatriated one (SYRD) kept the copies made on the day it was repatriated,
  so a role resumed only while it still used that same conversation, and one
  whose copy had been set aside (Main, Audit) started fresh every time. Its new
  conversation was then refused by the hook as foreign to the stale parent
  record, so only the User's manual resume ever put it back.

The hook now records where its own pane says. Records already in the parent
are taken back here, when a launch is about to resume a role: the newer of the
two stores wins, so the conversation the role last used is the one the normal
resume preflight then validates.

Nothing is lost: the parent's record is left exactly where it is, and a
record of the role's own that it replaces is set aside as `.superseded`, the
way a failed resume sets one aside. A record already set aside at the same time
or later is never taken back again, so a conversation that failed its
preflight is not retried at every launch.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from scripts.session_records import _session_record_at_path_for_role

if TYPE_CHECKING:
    from scripts.team_launcher import RoleConfig



def shared_session_dir(role: RoleConfig, session_dir: Path) -> Path | None:
    """The project's shared store when `session_dir` is this role's isolated one (`role_session_dir`), else None."""
    if session_dir.name != role.role or session_dir.parent.name != "roles":
        return None
    return session_dir.parent.parent


def _recorded_at(record: dict[str, Any], path: Path) -> float:
    try:
        return float(record["updated_at"])
    except (KeyError, TypeError, ValueError):
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0


def _read(role: RoleConfig, path: Path) -> dict[str, Any] | None:
    record = _session_record_at_path_for_role(role, path)
    if record is None or not str(record.get("session_id") or "").strip():
        return None
    return record


def adopt_stranded_session_record(role: RoleConfig, session_dir: Path) -> None:
    """Take the role's record from the shared store when it is newer than anything its own store holds."""
    from scripts import team_launcher as launcher

    shared = shared_session_dir(role, session_dir)
    if shared is None:
        return
    name = launcher.session_file_name(role.target)
    stranded = _read(role, shared / name)
    if stranded is None:
        return
    session_id = str(stranded["session_id"]).strip()
    current = _read(role, session_dir / name)
    if current is not None and str(current["session_id"]).strip() == session_id:
        return
    # Its current record, and the last one it set aside: older set-asides are older still.
    own = [(record, path) for path in (session_dir / name, session_dir / f"{name}.superseded")
           if (record := _read(role, path)) is not None]
    if any(_recorded_at(stranded, shared / name) <= _recorded_at(record, path) for record, path in own):
        return
    launcher.clear_session_record_for_role(role, session_dir)
    launcher._write_private_json_atomic(session_dir / name, stranded)
    print(
        f"team-launcher: {role.role}'s last session {session_id} was recorded in the shared store {shared}; "
        f"resuming from a copy in {session_dir} (SYRD-569); the shared record is left in place",
        file=sys.stderr,
    )
