"""Whether a role's provider session can be resumed, and whether a resume actually happened.

- **Provider session stores:** `claude_session_store_exists`,
  `codex_session_store_exists` and `agy_conversation_store_exists` say
  whether the provider still holds the session a role would resume. The
  `_uses_*` rules say which kind of resume a role uses.
- **Hermes homes:** `hermes_home_for_role`,
  `hermes_shared_home_for_session_dir` and `prepare_hermes_home_for_role`
  give a Hermes role its own home, with the shared entries symlinked in
  (`HERMES_SHARED_HOME_ENTRIES`), the private ones kept apart
  (`HERMES_PRIVATE_HOME_ENTRIES`), and the lock files guarded
  (`HERMES_SHARED_LOCK_GUARDS`).
- **Before a resume:** `_resume_preflight_allows_attempt` decides whether to
  try one, and `clear_unverified_resume_for_role` clears a stale marker.
- **After a resume:** `_resume_launch_status` reports `RESUME_LAUNCH_VERIFIED`,
  `RESUME_LAUNCH_MISSING` or `RESUME_LAUNCH_TIMEOUT`, and
  `_resume_launch_verified` and `_detached_launch_verified` wait for proof.

The timings and roots the suites rebind (`RESUME_STARTUP_TIMEOUT_SECONDS`,
`RESUME_STARTUP_POLL_SECONDS`, `DETACHED_SESSION_STABILITY_SECONDS`,
`AGY_CONVERSATION_ROOT`) stay in `scripts/team_launcher.py`. This module
reads them there, with the session records and tmux argv, when a function
runs.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-313). `team_launcher`
imports this module at its top and still exports every name callers read there.
This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import RoleConfig


CLAUDE_PROJECTS_DIR_NAME = ".claude/projects"


CODEX_SESSIONS_DIR_NAME = ".codex/sessions"


HERMES_HOME_DIR_NAME = ".hermes"


HERMES_SHARED_HOME_ENTRIES = (
    ".env",
    "SOUL.md",
    "auth.json",
    "auth.lock",
    "bin",
    "config.yaml",
    "hooks",
    "models_dev_cache.json",
    "shell-hooks-allowlist.json",
    "shell-hooks-allowlist.json.lock",
    "skills",
)


HERMES_PRIVATE_HOME_ENTRIES = frozenset(
    {
        ".hermes_history",
        ".skills_prompt_snapshot.json",
        ".update_check",
        "audio_cache",
        "cache",
        "cron",
        "image_cache",
        "logs",
        "memories",
        "pairing",
        "sandboxes",
        "sessions",
        "state.db",
        "state.db-shm",
        "state.db-wal",
    }
)


HERMES_SHARED_LOCK_GUARDS = {
    "auth.lock": "auth.json",
    "shell-hooks-allowlist.json.lock": "shell-hooks-allowlist.json",
}


def clear_unverified_resume_for_role(role: RoleConfig, session_dir: Path) -> None:
    from scripts import team_launcher as launcher

    path = session_dir / f"{launcher.session_file_name(role.target)}.resume_timeout"
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        print(f"team-launcher: failed to clear unverified resume marker for {role.role}: {exc}", file=sys.stderr)


def _uses_agy_conversation_resume(role: RoleConfig) -> bool:
    from scripts import team_launcher as launcher

    cli_name = launcher._command_name(role.cli[0]) if role.cli else ""
    return cli_name == "agy" and role.resume_mode == "flag" and role.resume_flag == "--conversation"


def _uses_claude_resume(role: RoleConfig) -> bool:
    from scripts import team_launcher as launcher

    cli_name = launcher._command_name(role.cli[0]) if role.cli else ""
    return cli_name == "claude" and role.resume_mode == "flag" and role.resume_flag == "--resume"


def _uses_codex_resume(role: RoleConfig) -> bool:
    from scripts import team_launcher as launcher

    cli_name = launcher._command_name(role.cli[0]) if role.cli else ""
    return cli_name == "codex" and role.resume_mode == "subcommand" and role.resume_subcommand == "resume"


def _uses_hermes(role: RoleConfig) -> bool:
    from scripts import team_launcher as launcher

    cli_name = launcher._command_name(role.cli[0]) if role.cli else ""
    return cli_name == "hermes"


def agy_conversation_store_exists(session_id: str, *, root: Path | None = None) -> bool:
    from scripts import team_launcher as launcher

    if not session_id:
        return False
    root = root or launcher.AGY_CONVERSATION_ROOT
    return (root / "conversations" / f"{session_id}.db").is_file() or (root / "brain" / session_id).is_dir()


def _home_from_session_dir(session_dir: Path) -> Path:
    expanded = session_dir.expanduser()
    parts = expanded.parts
    for index in range(len(parts) - 1):
        if parts[index] == ".local" and parts[index + 1] == "state":
            return Path(*parts[:index])
    return Path.home()


def hermes_shared_home_for_session_dir(session_dir: Path) -> Path:
    return _home_from_session_dir(session_dir) / HERMES_HOME_DIR_NAME


def hermes_home_for_role(role: RoleConfig, *, session_dir: Path) -> Path:
    from scripts import team_launcher as launcher

    role_home_name = launcher.session_file_name(role.target).removesuffix(".json")
    return session_dir.expanduser().parent / "hermes-homes" / role_home_name


def role_hermes_home(config: Any, role: RoleConfig) -> Path:
    """The one HERMES_HOME a role runs with, whichever entry point starts it (SYRD-563).

    `hermes_home_for_role` follows whatever session directory its caller holds,
    so an entry point that passed the project-wide `config.session_dir` instead
    of the role's own store ran Hermes from a different tree: Otto's `present
    recover` came up in `<p>-ticket-board/hermes-homes/<pane>`, with empty
    memories, beside the live `pane-sessions/roles/hermes-homes/<pane>`. This is
    the role's store as every launch, start and runtime path already names it.
    """
    from scripts import team_launcher as launcher

    return hermes_home_for_role(role, session_dir=launcher.role_session_dir(config, role))


def _same_path(left: Path, right: Path) -> bool:
    return left.expanduser().resolve(strict=False) == right.expanduser().resolve(strict=False)


def _symlink_shared_hermes_entry(source: Path, destination: Path) -> None:
    if destination.is_symlink():
        try:
            if _same_path(destination.resolve(strict=False), source):
                return
        except OSError:
            pass
        destination.unlink()
    elif destination.exists():
        return
    destination.symlink_to(source)


def prepare_hermes_home_for_role(role: RoleConfig, *, session_dir: Path) -> Path | None:
    from scripts import team_launcher as launcher

    if not _uses_hermes(role):
        return None
    hermes_home = hermes_home_for_role(role, session_dir=session_dir)
    launcher._ensure_private_dir(hermes_home)
    shared_home = hermes_shared_home_for_session_dir(session_dir)
    for entry in HERMES_SHARED_HOME_ENTRIES:
        if entry in HERMES_PRIVATE_HOME_ENTRIES:
            continue
        source = shared_home / entry
        # Locks must live with the resource they guard. Hermes derives both
        # of these lock paths from shared JSON files, so role-local locks would
        # leave several panes writing the same JSON without mutual exclusion.
        if not source.exists() and entry in HERMES_SHARED_LOCK_GUARDS:
            guarded = shared_home / HERMES_SHARED_LOCK_GUARDS[entry]
            if guarded.exists():
                shared_home.mkdir(parents=True, exist_ok=True)
                source.touch(mode=0o600, exist_ok=True)
        if not source.exists():
            continue
        _symlink_shared_hermes_entry(source, hermes_home / entry)
    return hermes_home


def _session_record_transcript_path(record: dict[str, Any] | None) -> Path | None:
    if record is None:
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    raw_path = str(payload.get("transcript_path") or "").strip()
    return Path(raw_path).expanduser() if raw_path else None


def _claude_project_dir_for_workdir(workdir: str, *, home: Path) -> Path:
    absolute = str(Path(workdir).expanduser().resolve(strict=False))
    project_key = "-" + absolute.strip("/").replace("/", "-")
    return home / CLAUDE_PROJECTS_DIR_NAME / project_key


def claude_session_store_exists(
    role: RoleConfig,
    session_id: str,
    *,
    session_dir: Path,
    record: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    transcript_path = _session_record_transcript_path(record)
    if transcript_path is not None:
        return transcript_path.is_file(), str(transcript_path)
    store_path = _claude_project_dir_for_workdir(role.workdir, home=_home_from_session_dir(session_dir)) / f"{session_id}.jsonl"
    return store_path.is_file(), str(store_path)


def codex_session_store_exists(
    session_id: str,
    *,
    session_dir: Path,
    record: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    transcript_path = _session_record_transcript_path(record)
    if transcript_path is not None:
        return transcript_path.is_file(), str(transcript_path)
    sessions_root = _home_from_session_dir(session_dir) / CODEX_SESSIONS_DIR_NAME
    if not session_id:
        return False, str(sessions_root)
    try:
        for path in sessions_root.rglob(f"*-{session_id}.jsonl"):
            if path.is_file():
                return True, str(path)
    except OSError:
        pass
    return False, f"{sessions_root}/**/*-{session_id}.jsonl"


def _resume_preflight_allows_attempt(role: RoleConfig, session_id: str, *, session_dir: Path) -> tuple[bool, str]:
    from scripts import team_launcher as launcher

    record = launcher._session_record_for_role(role, session_dir)
    if _uses_claude_resume(role):
        found, location = claude_session_store_exists(role, session_id, session_dir=session_dir, record=record)
        if found:
            return True, ""
        return (
            False,
            (
                f"team-launcher: recorded claude session {session_id} for {role.role} "
                f"is not present at {location}; starting fresh instead of passing claude --resume, "
                "which exits when the conversation is missing"
            ),
        )
    if _uses_codex_resume(role):
        found, location = codex_session_store_exists(session_id, session_dir=session_dir, record=record)
        if found:
            return True, ""
        return (
            False,
            (
                f"team-launcher: recorded codex session {session_id} for {role.role} "
                f"is not present at {location}; starting fresh instead of passing codex resume"
            ),
        )
    if _uses_hermes(role):
        return _hermes_resume_preflight(role, session_id, session_dir=session_dir)
    if not _uses_agy_conversation_resume(role):
        return True, ""
    session_home = _home_from_session_dir(session_dir)
    agy_root = (
        session_home / launcher.AGY_CREDENTIAL_DIR_NAME
        if session_home != Path.home()
        else launcher.AGY_CONVERSATION_ROOT
    )
    if agy_conversation_store_exists(session_id, root=agy_root):
        return True, ""
    return (
        False,
        (
            f"team-launcher: recorded agy conversation {session_id} for {role.role} "
            "is not present in the local Antigravity store; starting fresh instead of relying "
            "on agy --conversation, which silently falls back when the id is missing"
        ),
    )


def hermes_session_in_home(session_id: str, hermes_home: Path) -> bool | None:
    """Whether Hermes would find `session_id` in this home: its `state.db`'s sessions table, read-only.

    The same lookup `hermes --resume` makes (SessionDB.get_session: a row with
    that exact id). False when the database or the row is missing; None when
    the database exists but cannot be read, which is no confirmation either.
    """
    import sqlite3

    database = hermes_home / "state.db"
    if not database.is_file():
        return False
    try:
        connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
        try:
            row = connection.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    return row is not None


def _hermes_resume_preflight(role: RoleConfig, session_id: str, *, session_dir: Path) -> tuple[bool, str]:
    """Pass a recorded Hermes id only if the role's own home holds that session (SYRD-564).

    Otto's recovered pane was started with `--resume <Oct 4 id>` that its home's
    state.db did not have: Hermes printed "Session not found", kept running, and
    answered every prompt the same way, while the launcher saw the process and
    called the resume verified. A missing or unconfirmable id now starts fresh,
    saying where it looked -- and when the id lives only in the project-wide
    home this role no longer runs from, saying that too.
    """
    home = hermes_home_for_role(role, session_dir=session_dir)
    found = hermes_session_in_home(session_id, home)
    if found:
        return True, ""
    why = f"cannot be confirmed: {home / 'state.db'} could not be read" if found is None else f"is not in {home / 'state.db'}"
    elsewhere = ""
    if session_dir.expanduser().parent.name == "roles":
        legacy_home = hermes_home_for_role(role, session_dir=session_dir.expanduser().parent.parent)
        if hermes_session_in_home(session_id, legacy_home):
            elsewhere = f"; it exists only in the project-wide home {legacy_home}, which this role no longer runs from"
    return False, (
        f"team-launcher: recorded hermes session {session_id} for {role.role} {why}{elsewhere}; not passing "
        "hermes --resume, which would answer every prompt with 'Session not found'"
    )


RESUME_LAUNCH_VERIFIED = "verified"


RESUME_LAUNCH_MISSING = "missing"


RESUME_LAUNCH_TIMEOUT = "timeout"


def _resume_launch_status(
    role: RoleConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> str:
    from scripts import team_launcher as launcher

    deadline = time.monotonic() + launcher.RESUME_STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if launcher.live_command_matches_role(role, runner=runner):
            return RESUME_LAUNCH_VERIFIED
        if runner(launcher.tmux_has_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
            return RESUME_LAUNCH_MISSING
        time.sleep(launcher.RESUME_STARTUP_POLL_SECONDS)
    if launcher.live_command_matches_role(role, runner=runner):
        return RESUME_LAUNCH_VERIFIED
    if runner(launcher.tmux_has_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
        return RESUME_LAUNCH_MISSING
    return RESUME_LAUNCH_TIMEOUT


def _resume_launch_verified(
    role: RoleConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> bool:
    return _resume_launch_status(role, runner=runner) == RESUME_LAUNCH_VERIFIED


def _detached_launch_verified(
    role: RoleConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> bool:
    from scripts import team_launcher as launcher

    if not _resume_launch_verified(role, runner=runner):
        return False
    deadline = time.monotonic() + launcher.DETACHED_SESSION_STABILITY_SECONDS
    while time.monotonic() < deadline:
        if runner(launcher.tmux_has_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
            return False
        if not launcher.live_command_matches_role(role, runner=runner):
            return False
        time.sleep(launcher.RESUME_STARTUP_POLL_SECONDS)
    return (
        runner(launcher.tmux_has_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        and launcher.live_command_matches_role(role, runner=runner)
    )
