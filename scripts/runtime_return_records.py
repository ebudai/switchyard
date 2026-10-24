"""A role that returns to a runtime it ran before resumes the conversation it left there (SYRD-622).

Changing a role's runtime sets its record aside as `.superseded`, and the
store keeps only the last two set-asides (`.superseded`, `.superseded.1`).
Live on syrd, 2026-10-21 to 10-24: Inspector went from agy to Copilot for
SYRD-634's acceptance, and back. The switch set aside its agy record
(`ac53bccd`, filed by agy under Inspector's own worktree); Copilot's restarts
each set another aside, and by the switch back both places held Copilot
records. Nothing remembered the agy conversation. Inspector started fresh at
the next two launches, and the User resumed one by hand: Audit's (`f4e8f4e1`),
which agy's map still named for Inspector's worktree after SYRD-638's
incident. SYRD-622's map adoption rightly declined that map, and had nothing
else to go on.

So the switch keeps the record it leaves as `<record>.runtime-<runtime>`: the
conversation the role had on that runtime, replaced only by a later switch
away from the same runtime. When the role next starts on that runtime:

* wanting a resume and with no record, the kept record becomes its record, and
  the checks every record goes through -- SYRD-638's provenance, then the
  resume preflight -- decide; one they refuse is set aside as any is, and the
  kept one is gone, so it is not tried again;
* otherwise -- a record of its own already, or a fresh start asked for -- the
  kept record is dropped: the role has moved on from it on that runtime.

A change of model on the same runtime keeps nothing: that set-aside means a
fresh start, as it always did. Hermes keeps each role's sessions in its own
home, and nothing is kept for it. Each decision is kept in the pane's resume
history.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.session_records import _session_record_at_path_for_role

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig, RoleConfig

KEPT_SUFFIX = ".runtime-"
#: The runtimes whose conversations a launch resumes by id, and so are worth keeping.
KEPT_RUNTIMES = frozenset({"agy", "claude", "codex", "copilot"})


def kept_path(role: RoleConfig, session_dir: Path, runtime: str) -> Path:
    from scripts import team_launcher as launcher

    return session_dir / f"{launcher.session_file_name(role.target)}{KEPT_SUFFIX}{runtime}"


def keep_left_record(config: ProjectConfig, role: RoleConfig, session_dir: Path, record: Path, *,
                     account: str = "") -> None:
    """Before a runtime switch sets `record` aside, keep it as the role's conversation on the runtime it leaves.

    `role` is the role as now configured. `account`, when root writes for the owner, is whose tree it is: the
    record is then read and the copy written by descriptor from that root, never through a link (SYRD-612).
    """
    from scripts import owner_writes, tenant_paths
    from scripts import team_launcher as launcher
    from scripts.session_provenance import role_runtime, source_runtime

    if account:
        try:
            raw = tenant_paths.read_file(owner_writes.owner_root(account, record), str(record))
            parsed = json.loads(raw.decode("utf-8")) if raw is not None else None
        except (tenant_paths.Refused, SystemExit, UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(parsed, dict) or str(parsed.get("target") or "") != role.target:
            return
    else:
        parsed = _session_record_at_path_for_role(role, record)
    if parsed is None or not str(parsed.get("session_id") or "").strip():
        return
    left = source_runtime(parsed)
    if left not in KEPT_RUNTIMES or left == role_runtime(role):
        return
    launcher._write_private_json_atomic(kept_path(role, session_dir, left), parsed, owner_user=account or None)
    if not account:
        from scripts.role_sessions import _decided

        _decided(role, session_dir, "", "kept_for_return",
                 f"team-launcher: {role.role} leaves {left} with session {parsed['session_id']}; kept to resume "
                 f"if it returns to {left} (SYRD-622)", str(parsed["session_id"]))


def settle_return_record(role: RoleConfig, session_dir: Path, *, resume: bool) -> None:
    """At a start on a runtime the role left before: its kept conversation becomes its record, or is dropped."""
    from scripts import team_launcher as launcher
    from scripts.role_sessions import _decided
    from scripts.session_provenance import role_runtime

    runtime = role_runtime(role)
    if not runtime:
        return
    kept = kept_path(role, session_dir, runtime)
    try:
        info = kept.lstat()
    except OSError:
        return
    record = _session_record_at_path_for_role(role, kept) if stat.S_ISREG(info.st_mode) else None
    session_id = str((record or {}).get("session_id") or "").strip()
    current = launcher.session_id_for_role(role, session_dir)
    if resume and not current and session_id:
        os.replace(kept, session_dir / launcher.session_file_name(role.target))
        _decided(role, session_dir, "", "adopted_return_record",
                 f"team-launcher: {role.role} returns to {runtime} with no record; resuming {session_id}, the "
                 f"session it left {runtime} with, if it is still {role.role}'s (SYRD-622)", session_id)
        return
    kept.unlink(missing_ok=True)
    why = ("it is not a record" if not session_id else f"it has its own record {current}" if current
           else "a fresh session was asked for")
    _decided(role, session_dir, "", "return_record_dropped",
             f"team-launcher: {role.role}'s kept {runtime} session {session_id or kept.name} was dropped: {why}",
             session_id)
