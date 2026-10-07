"""A role's recorded session: which one to resume, whether its hooks reported, and seeding a new session store.

Each role's provider session is recorded by its pane under the role's session
directory. The launcher reads those records to decide whether to resume or
start fresh, and to tell whether a launch actually came up:
- **Names:** `session_file_name` and `pane_state_file_name` name the
  records.
- **Choosing the session:** `session_id_for_role` is the session to resume.
  `superseded_session_id_for_role` and `unverified_resume_session_id_for_role`
  are the ones that must not be resumed.
- **Did the launch come up:** `pane_launch_outcome_source_for_role`,
  `pane_runtime_hook_source_for_role` and the hook-freshness rules decide
  whether a pane's runtime hook really reported. They use
  `LAUNCH_RESUME_FALLBACK_*` and `LAUNCH_PANE_STATE_FRESHNESS_SKEW_SECONDS`.
- **Reporting:** `launch_session_record_statuses` collects a
  `LaunchSessionRecordStatus` per role, waiting up to
  `LAUNCH_SESSION_RECORD_TIMEOUT_SECONDS` and polling every
  `LAUNCH_SESSION_RECORD_POLL_SECONDS`. `report_launch_session_records` says
  which roles did not record a session.
- **Reading records:** `_session_record_for_role` and its helpers read them,
  and `clear_session_record_for_role` removes one.
- **Seeding:** `seed_session_dir_from_legacy_sources` and
  `seed_default_session_dir_from_legacy_sources` fill a new session store
  from the legacy runtime directory and the interim backup
  (`DEFAULT_LEGACY_RUNTIME_SESSION_DIR`, `DEFAULT_INTERIM_SESSION_BACKUP_DIR`).

`DEFAULT_SESSION_DIR`, `role_session_dir` and the private-file writers stay in
`scripts/team_launcher.py`. This module reads them from there when a function
runs, so the suites' patches on the launcher reach it. Starting, attaching and
stopping sessions is not here.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-310). `team_launcher`
imports this module at its top and still exports every name callers read there.
This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import json
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig, RoleConfig


DEFAULT_LEGACY_RUNTIME_SESSION_DIR = Path(f"/run/user/{os.getuid()}/pgu-ticket-board/pane-sessions")


DEFAULT_INTERIM_SESSION_BACKUP_DIR = Path.home() / ".local" / "state" / "pgu-ticket-board" / "pane-sessions"


LAUNCH_SESSION_RECORD_TIMEOUT_SECONDS = 10.0


LAUNCH_SESSION_RECORD_POLL_SECONDS = 0.2


# Used only by direct report callers that cannot provide pane-state launch outcomes.
LAUNCH_RESUME_FALLBACK_GRACE_SECONDS = 2.0


LAUNCH_RESUME_FALLBACK_FRESHNESS_SKEW_NS = 100_000_000


LAUNCH_PANE_STATE_FRESHNESS_SKEW_SECONDS = 0.1


@dataclass(frozen=True)
class LaunchSessionRecordStatus:
    role: str
    target: str
    session_id: str
    superseded_session_id: str = ""
    unverified_resume_session_id: str = ""
    pane_state_source: str = ""
    runtime_hook_source: str = ""
    attached_to_running: bool = False

    @property
    def found(self) -> bool:
        return bool(self.session_id)

    @property
    def resume_fallback(self) -> bool:
        return bool(self.superseded_session_id)

    @property
    def unverified_resume(self) -> bool:
        return bool(self.unverified_resume_session_id)

    @property
    def pane_launch_reported(self) -> bool:
        return bool(
            self.attached_to_running
            or self.pane_state_source
            or self.runtime_hook_source
            or self.unverified_resume_session_id
        )

    @property
    def runtime_hook_reported(self) -> bool:
        return bool(self.runtime_hook_source)


def session_file_name(target: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in ".-" else "_" for ch in target)
    return f"{safe}.json"


def pane_state_file_name(target: str) -> str:
    return session_file_name(target)


def session_id_for_role(role: RoleConfig, session_dir: Path) -> str:
    parsed = _session_record_for_role(role, session_dir)
    if parsed is None:
        return ""
    return str(parsed.get("session_id") or "").strip()


def superseded_session_id_for_role(
    role: RoleConfig,
    session_dir: Path,
    *,
    changed_since_ns: int | None = None,
) -> str:
    if changed_since_ns is None:
        return ""
    path = session_dir / f"{session_file_name(role.target)}.superseded"
    try:
        stat = path.stat()
    except OSError:
        return ""
    if stat.st_ctime_ns + LAUNCH_RESUME_FALLBACK_FRESHNESS_SKEW_NS < changed_since_ns:
        return ""
    parsed = _session_record_at_path_for_role(role, path)
    if parsed is None:
        return ""
    return str(parsed.get("session_id") or "").strip()


def unverified_resume_session_id_for_role(
    role: RoleConfig,
    session_dir: Path,
    *,
    changed_since_ns: int | None = None,
) -> str:
    if changed_since_ns is None:
        return ""
    path = session_dir / f"{session_file_name(role.target)}.resume_timeout"
    try:
        stat = path.stat()
    except OSError:
        return ""
    if stat.st_ctime_ns + LAUNCH_RESUME_FALLBACK_FRESHNESS_SKEW_NS < changed_since_ns:
        return ""
    parsed = _session_record_at_path_for_role(role, path)
    if parsed is None:
        return ""
    return str(parsed.get("session_id") or "").strip()


def pane_launch_outcome_source_for_role(
    role: RoleConfig,
    pane_state_dir: Path | None,
    *,
    updated_since: float | None = None,
) -> str:
    if pane_state_dir is None or updated_since is None:
        return ""
    path = pane_state_dir / pane_state_file_name(role.target)
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(parsed, dict):
        return ""
    if str(parsed.get("target") or "") != role.target:
        return ""
    source = str(parsed.get("source") or "").strip()
    if not source.startswith("team_launcher."):
        return ""
    try:
        updated_at = float(parsed.get("updated_at") or 0.0)
    except (TypeError, ValueError):
        return ""
    if updated_at + LAUNCH_PANE_STATE_FRESHNESS_SKEW_SECONDS < updated_since:
        return ""
    return source


def pane_runtime_hook_source_for_role(
    role: RoleConfig,
    pane_state_dir: Path | None,
    *,
    updated_since: float | None = None,
) -> str:
    from scripts import team_launcher as launcher

    if pane_state_dir is None or updated_since is None:
        return ""
    path = pane_state_dir / pane_state_file_name(role.target)
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(parsed, dict):
        return ""
    if str(parsed.get("target") or "") != role.target:
        return ""
    source = str(parsed.get("source") or "").strip()
    expected_prefix = f"{launcher._command_name(role.cli[0])}." if role.cli else ""
    if not expected_prefix or not source.startswith(expected_prefix):
        return ""
    try:
        updated_at = float(parsed.get("updated_at") or 0.0)
    except (TypeError, ValueError):
        return ""
    if updated_at + LAUNCH_PANE_STATE_FRESHNESS_SKEW_SECONDS < updated_since:
        return ""
    return source


def _expects_launch_runtime_hook_probe(role: RoleConfig) -> bool:
    from scripts import team_launcher as launcher

    cli_name = launcher._command_name(role.cli[0]) if role.cli else ""
    return cli_name == "codex"


def _runtime_hook_deferred_until_activity(role: RoleConfig, status: "LaunchSessionRecordStatus") -> bool:
    """A pane whose launch is reported and whose hook cannot report until it is used.

    Interactive Codex defers SessionStart until the first prompt, so a pane it
    has just started writes neither a runtime hook nor a session record however
    long it is watched. The launcher's own outcome for it is the answer there
    is to have. The report already says nothing about this case; the wait uses
    the same rule, so it no longer spends its whole timeout on a record that is
    not coming -- ten seconds of every fresh `switchyard new` (SYRD-248).
    """
    return (
        _expects_launch_runtime_hook_probe(role)
        and status.pane_state_source.startswith("team_launcher.")
        and not status.runtime_hook_reported
        and not status.unverified_resume
        and not status.resume_fallback
    )


def launch_session_record_statuses(
    config: ProjectConfig,
    roles: Sequence[RoleConfig] | None = None,
    *,
    timeout_seconds: float = LAUNCH_SESSION_RECORD_TIMEOUT_SECONDS,
    poll_seconds: float = LAUNCH_SESSION_RECORD_POLL_SECONDS,
    fallback_changed_since_ns: int | None = None,
    fallback_grace_seconds: float = LAUNCH_RESUME_FALLBACK_GRACE_SECONDS,
    pane_state_dir: Path | None = None,
    pane_state_updated_since: float | None = None,
) -> list[LaunchSessionRecordStatus]:
    from scripts import team_launcher as launcher

    roles = tuple(roles or config.roles)
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    pane_outcomes_required = pane_state_dir is not None and pane_state_updated_since is not None
    fallback_deadline = (
        time.monotonic() + min(max(0.0, fallback_grace_seconds), max(0.0, timeout_seconds))
        if fallback_changed_since_ns is not None and not pane_outcomes_required
        else time.monotonic()
    )
    statuses: list[LaunchSessionRecordStatus] = []
    settled_at: float | None = None
    while True:
        statuses = [
            LaunchSessionRecordStatus(
                role=role.role,
                target=role.target,
                # A role records its session under its own account, so reading
                # the project-wide directory reports every isolated role as
                # having no session (SYRD-39).
                session_id=session_id_for_role(role, launcher.role_session_dir(config, role)),
                superseded_session_id=superseded_session_id_for_role(
                    role,
                    launcher.role_session_dir(config, role),
                    changed_since_ns=fallback_changed_since_ns,
                ),
                unverified_resume_session_id=unverified_resume_session_id_for_role(
                    role,
                    launcher.role_session_dir(config, role),
                    changed_since_ns=fallback_changed_since_ns,
                ),
                pane_state_source=pane_launch_outcome_source_for_role(
                    role,
                    pane_state_dir,
                    updated_since=pane_state_updated_since,
                ),
                runtime_hook_source=pane_runtime_hook_source_for_role(
                    role,
                    pane_state_dir,
                    updated_since=pane_state_updated_since,
                ),
            )
            for role in roles
        ]
        now = time.monotonic()
        all_records_found = all(status.found for status in statuses)
        resume_fallback_found = any(status.resume_fallback for status in statuses)
        all_pane_outcomes_reported = not pane_outcomes_required or all(
            status.pane_launch_reported for status in statuses
        )
        all_runtime_hooks_reported = not pane_outcomes_required or all(
            (not _expects_launch_runtime_hook_probe(role)) or status.runtime_hook_reported or status.unverified_resume
            for role, status in zip(roles, statuses, strict=True)
        )
        fallback_grace_elapsed = now >= fallback_deadline
        # Every pane accounted for: a record, or a launch the launcher reported
        # for a runtime that cannot record anything until it is used. A short
        # grace still lets a hook that is merely slow arrive and be reported.
        all_settled = pane_outcomes_required and all_pane_outcomes_reported and all(
            status.found or _runtime_hook_deferred_until_activity(role, status)
            for role, status in zip(roles, statuses, strict=True)
        )
        if all_settled and settled_at is None:
            settled_at = now
        if not all_settled:
            settled_at = None
        settled_grace_elapsed = (
            settled_at is not None and now - settled_at >= max(0.0, fallback_grace_seconds)
        )
        if (
            now >= deadline
            or settled_grace_elapsed
            or (resume_fallback_found and all_pane_outcomes_reported and all_runtime_hooks_reported)
            or (all_records_found and all_pane_outcomes_reported and all_runtime_hooks_reported)
            or (
                all_records_found
                and not pane_outcomes_required
                and fallback_changed_since_ns is not None
                and fallback_grace_elapsed
            )
        ):
            return statuses
        next_deadline = deadline
        if all_records_found and fallback_changed_since_ns is not None and not pane_outcomes_required:
            next_deadline = min(deadline, fallback_deadline)
        if settled_at is not None:
            next_deadline = min(next_deadline, settled_at + max(0.0, fallback_grace_seconds))
        sleep_seconds = min(max(0.01, poll_seconds), max(0.0, next_deadline - now))
        if sleep_seconds <= 0:
            return statuses
        time.sleep(sleep_seconds)


def report_launch_session_records(
    config: ProjectConfig,
    roles: Sequence[RoleConfig] | None = None,
    *,
    timeout_seconds: float = LAUNCH_SESSION_RECORD_TIMEOUT_SECONDS,
    poll_seconds: float = LAUNCH_SESSION_RECORD_POLL_SECONDS,
    fallback_changed_since_ns: int | None = None,
    pane_state_dir: Path | None = None,
    pane_state_updated_since: float | None = None,
    attached_roles: Sequence[RoleConfig] | None = None,
    print_func: Callable[[str], None] = print,
) -> list[LaunchSessionRecordStatus]:
    from scripts import team_launcher as launcher

    selected_roles = tuple(roles or config.roles)
    attached_role_names = {role.role for role in attached_roles or ()}
    attached_by_role = {
        role.role: LaunchSessionRecordStatus(
            role=role.role,
            target=role.target,
            session_id=session_id_for_role(role, launcher.role_session_dir(config, role)),
            attached_to_running=True,
        )
        for role in selected_roles
        if role.role in attached_role_names
    }
    probe_roles = tuple(role for role in selected_roles if role.role not in attached_role_names)
    for role in selected_roles:
        if role.role not in attached_by_role:
            continue
        print_func(f"switchyard: attached to running pane for {role.role} ({role.target})")
    probed_statuses: list[LaunchSessionRecordStatus] = []
    if probe_roles:
        print_func(
            f"switchyard: checking session records for {len(probe_roles)} pane(s) "
            f"(waiting up to {timeout_seconds:g}s)"
        )
        probed_statuses = launch_session_record_statuses(
            config,
            probe_roles,
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
            fallback_changed_since_ns=fallback_changed_since_ns,
            pane_state_dir=pane_state_dir,
            pane_state_updated_since=pane_state_updated_since,
        )
    statuses_by_role = {status.role: status for status in probed_statuses}
    statuses_by_role.update(attached_by_role)
    statuses = [statuses_by_role[role.role] for role in selected_roles if role.role in statuses_by_role]
    roles_by_target = {role.target: role for role in selected_roles}
    codex_runtime_hook_missing_statuses: list[LaunchSessionRecordStatus] = []
    for status in statuses:
        role = roles_by_target.get(status.target)
        codex_runtime_hook_deferred_until_activity = (
            role is not None and _runtime_hook_deferred_until_activity(role, status)
        )
        if status.attached_to_running:
            continue
        if status.found:
            print_func(
                f"switchyard: session record found for {status.role} ({status.target}): {status.session_id}"
            )
        elif not codex_runtime_hook_deferred_until_activity:
            print_func(
                f"warning: switchyard: session record missing for {status.role} "
                f"({status.target}) after {timeout_seconds:g}s; continuing"
            )
        if status.unverified_resume:
            print_func(
                f"warning: switchyard: {status.role} ({status.target}) resume for session "
                f"{status.unverified_resume_session_id} was not verified before startup timeout; "
                "left session record intact and did not mark pane idle"
            )
        if status.resume_fallback:
            print_func(
                f"warning: switchyard: {status.role} ({status.target}) fell back to a fresh session; "
                f"superseded session {status.superseded_session_id}"
            )
        if (
            role is not None
            and _expects_launch_runtime_hook_probe(role)
            and not status.runtime_hook_reported
            and not status.unverified_resume
            and not status.resume_fallback
        ):
            codex_runtime_hook_missing_statuses.append(status)
    if codex_runtime_hook_missing_statuses:
        missing = ", ".join(
            f"{status.role} ({status.target})" for status in codex_runtime_hook_missing_statuses
        )
        print_func(
            f"warning: switchyard: codex runtime hook did not report for "
            f"{len(codex_runtime_hook_missing_statuses)} pane(s): {missing}; expected fresh codex.* "
            "pane state. Interactive Codex may defer SessionStart until the first prompt; hook delivery "
            "is unproven until activity writes codex.UserPromptSubmit or codex.Stop."
        )
    return statuses


def clear_session_record_for_role(role: RoleConfig, session_dir: Path) -> bool:
    path = session_dir / session_file_name(role.target)
    superseded_path = path.with_name(f"{path.name}.superseded")
    previous_superseded_path = path.with_name(f"{path.name}.superseded.1")
    timeout_path = path.with_name(f"{path.name}.resume_timeout")
    if not path.exists():
        return False
    try:
        if superseded_path.exists():
            superseded_path.replace(previous_superseded_path)
        timeout_path.unlink(missing_ok=True)
        path.replace(superseded_path)
    except FileNotFoundError:
        return False
    return True


def _session_record_at_path_for_role(role: RoleConfig, path: Path) -> dict[str, Any] | None:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    if str(parsed.get("target") or "") != role.target:
        return None
    return parsed


def _session_record_for_role(role: RoleConfig, session_dir: Path) -> dict[str, Any] | None:
    return _session_record_at_path_for_role(role, session_dir / session_file_name(role.target))


def _session_payload_model_for_role(role: RoleConfig, session_dir: Path) -> str:
    session = _session_record_for_role(role, session_dir)
    if session is None:
        return ""
    payload = session.get("payload")
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("model") or "").strip()


def _session_dir_has_records(session_dir: Path) -> bool:
    try:
        return any(path.is_file() and path.suffix == ".json" for path in session_dir.iterdir())
    except OSError:
        return False


def _valid_session_record_payload(payload: object) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    target = str(payload.get("target") or "").strip()
    session_id = str(payload.get("session_id") or "").strip()
    if not target or not session_id:
        return None
    return dict(payload)


def _default_session_seed_dirs(session_dir: Path) -> list[Path]:
    candidates = [DEFAULT_LEGACY_RUNTIME_SESSION_DIR, DEFAULT_INTERIM_SESSION_BACKUP_DIR]
    session_resolved = session_dir.expanduser().resolve(strict=False)
    unique: list[Path] = []
    seen: set[str] = {str(session_resolved)}
    for candidate in candidates:
        resolved = str(candidate.expanduser().resolve(strict=False))
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(candidate)
    return unique


def seed_session_dir_from_legacy_sources(
    session_dir: Path,
    *,
    candidates: Sequence[Path] | None = None,
) -> list[Path]:
    from scripts import team_launcher as launcher

    target_dir = session_dir.expanduser()
    if _session_dir_has_records(target_dir):
        launcher._ensure_private_dir(target_dir)
        return []

    copied: list[Path] = []
    for source_dir in candidates if candidates is not None else _default_session_seed_dirs(target_dir):
        source = source_dir.expanduser()
        try:
            source_records = sorted(path for path in source.iterdir() if path.is_file() and path.suffix == ".json")
        except OSError:
            continue
        for source_path in source_records:
            try:
                payload = json.loads(source_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            valid = _valid_session_record_payload(payload)
            if valid is None:
                continue
            target_path = target_dir / session_file_name(str(valid["target"]))
            if target_path.exists():
                continue
            launcher._write_private_json_atomic(target_path, valid)
            copied.append(target_path)
    if not copied:
        launcher._ensure_private_dir(target_dir)
    return copied


def seed_default_session_dir_from_legacy_sources(session_dir: Path) -> list[Path]:
    from scripts import team_launcher as launcher

    if session_dir.expanduser().resolve(strict=False) != launcher.DEFAULT_SESSION_DIR.expanduser().resolve(strict=False):
        return []
    return seed_session_dir_from_legacy_sources(session_dir)
