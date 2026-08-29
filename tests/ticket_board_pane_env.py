"""Shared test helpers for isolating ticket-board pane hook state."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Mapping

SESSION_ENV_KEYS = (
    "TICKET_BOARD_PANE_SESSION_ID",
    "PGU_PANE_SESSION_ID",
    "CLAUDE_SESSION_ID",
    "CODEX_SESSION_ID",
    "GEMINI_SESSION_ID",
    "SESSION_ID",
)
HOOK_PATH_ENV_KEYS = (
    "TICKET_BOARD_PANE_SESSION_DIR",
    "PGU_TICKET_BOARD_PANE_SESSION_DIR",
    "TICKET_BOARD_PANE_STATE_DIR",
    "PGU_TICKET_BOARD_PANE_STATE_DIR",
)
HOOK_TARGET_ENV_KEYS = (
    "TICKET_BOARD_PANE_TARGET",
    "PGU_PANE_TARGET",
)
STATE_HOME_ENV_KEYS = ("XDG_STATE_HOME",)
TICKET_BOARD_PANE_ENV_KEYS = (*SESSION_ENV_KEYS, *HOOK_PATH_ENV_KEYS, *HOOK_TARGET_ENV_KEYS, *STATE_HOME_ENV_KEYS)
PANE_STATE_SOURCE_PREFIXES_BY_TARGET = {
    "pgu-director:0.0": ("claude.", "team_launcher."),
    "pgu-main:0.0": ("codex.", "listener.", "team_launcher."),
    "pgu-app:0.0": ("codex.", "listener.", "team_launcher."),
    "pgu-perf:0.0": ("codex.", "listener.", "team_launcher."),
    "pgu-research:0.0": ("claude.", "team_launcher."),
    "pgu-ops:0.0": ("codex.", "listener.", "team_launcher."),
    "pgu-audit:0.0": ("claude.", "team_launcher."),
    "pgu-inspector:0.0": ("gemini.", "team_launcher."),
}
PANE_STATE_STATES = frozenset({"idle", "busy", "blocked"})
PANE_SESSION_RUNTIME_SOURCE_PREFIXES = ("codex.", "claude.", "gemini.")


def strip_ticket_board_pane_env(environ: dict[str, str], *, keep: tuple[str, ...] = ()) -> dict[str, str]:
    saved: dict[str, str] = {}
    keep_set = set(keep)
    for key in TICKET_BOARD_PANE_ENV_KEYS:
        if key in keep_set:
            continue
        if key in environ:
            saved[key] = environ.pop(key)
    return saved


def restore_env(environ: dict[str, str], saved: Mapping[str, str]) -> None:
    for key in TICKET_BOARD_PANE_ENV_KEYS:
        environ.pop(key, None)
    environ.update(saved)


def stripped_ticket_board_pane_env(**extra: str) -> dict[str, str]:
    env = os.environ.copy()
    strip_ticket_board_pane_env(env)
    env.update(extra)
    return env


def _unique_paths(candidates: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        resolved = str(candidate.resolve(strict=False))
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(candidate)
    return unique


def candidate_live_pane_session_paths(environ: Mapping[str, str] | None = None) -> list[Path]:
    env = environ or os.environ
    candidates: list[Path] = []
    for key in ("TICKET_BOARD_PANE_SESSION_DIR", "PGU_TICKET_BOARD_PANE_SESSION_DIR"):
        value = env.get(key, "").strip()
        if value:
            candidates.append(Path(value).expanduser())
    xdg_state_home = env.get("XDG_STATE_HOME", "").strip()
    state_home = Path(xdg_state_home).expanduser() if xdg_state_home else Path.home() / ".local" / "state"
    candidates.append(state_home / "pgu-ticket-board" / "pane-sessions")
    candidates.append(Path(f"/run/user/{os.getuid()}/pgu-ticket-board/pane-sessions"))
    return _unique_paths(candidates)


def candidate_live_pane_state_paths(environ: Mapping[str, str] | None = None) -> list[Path]:
    env = environ or os.environ
    candidates: list[Path] = []
    for key in ("TICKET_BOARD_PANE_STATE_DIR", "PGU_TICKET_BOARD_PANE_STATE_DIR"):
        value = env.get(key, "").strip()
        if value:
            candidates.append(Path(value).expanduser())
    candidates.append(Path(f"/run/user/{os.getuid()}/pgu-ticket-board/pane-state"))
    return _unique_paths(candidates)


def candidate_live_pane_paths(environ: Mapping[str, str] | None = None) -> list[Path]:
    return _unique_paths(
        [*candidate_live_pane_session_paths(environ), *candidate_live_pane_state_paths(environ)]
    )


def snapshot_path(path: Path) -> dict[str, str] | None:
    try:
        if not path.exists():
            return None
        if not path.is_dir():
            return {}
        snapshot: dict[str, str] = {}
        for child in sorted(path.rglob("*")):
            if child.is_file():
                snapshot[str(child.relative_to(path))] = hashlib.sha256(child.read_bytes()).hexdigest()
        return snapshot
    except OSError:
        return {}


def snapshot_paths(paths: list[Path]) -> dict[str, dict[str, str] | None]:
    return {str(path): snapshot_path(path) for path in paths}


def snapshot_path_file_set(path: Path) -> set[str] | None:
    try:
        if not path.exists():
            return None
        if not path.is_dir():
            return set()
        return {str(child.relative_to(path)) for child in path.rglob("*") if child.is_file()}
    except OSError:
        return set()


def snapshot_paths_file_set(paths: list[Path]) -> dict[str, set[str] | None]:
    return {str(path): snapshot_path_file_set(path) for path in paths}


def snapshot_diff(
    before: dict[str, dict[str, str] | None],
    after: dict[str, dict[str, str] | None],
) -> list[str]:
    changed: list[str] = []
    for root in sorted(set(before) | set(after)):
        before_files = before.get(root)
        after_files = after.get(root)
        if before_files is None or after_files is None:
            if before_files != after_files:
                changed.append(root)
            continue
        for name in sorted(set(before_files) | set(after_files)):
            if before_files.get(name) != after_files.get(name):
                changed.append(f"{root}/{name}")
    return changed


def pane_session_record_diff(
    before: dict[str, dict[str, str] | None],
    after: dict[str, dict[str, str] | None],
) -> list[str]:
    changed: list[str] = []
    for root in sorted(set(before) | set(after)):
        before_files = before.get(root)
        after_files = after.get(root)
        if before_files is None or after_files is None:
            if before_files != after_files:
                changed.append(root)
            continue
        for name in sorted(set(before_files) | set(after_files)):
            before_hash = before_files.get(name)
            after_hash = after_files.get(name)
            if before_hash == after_hash:
                continue
            changed_path = f"{root}/{name}"
            if before_hash is None or after_hash is None:
                changed.append(changed_path)
                continue
            if not _pane_session_record_allows_live_rewrite(Path(root) / name):
                changed.append(changed_path)
    return changed


def snapshot_file_set_diff(
    before: dict[str, set[str] | None],
    after: dict[str, set[str] | None],
) -> list[str]:
    changed: list[str] = []
    for root in sorted(set(before) | set(after)):
        before_files = before.get(root)
        after_files = after.get(root)
        if before_files is None or after_files is None:
            if before_files != after_files:
                changed.append(root)
            continue
        for name in sorted(before_files.symmetric_difference(after_files)):
            changed.append(f"{root}/{name}")
    return changed


def pane_state_target_from_file_name(name: str) -> str | None:
    if not name.endswith(".json"):
        return None
    stem = name.removesuffix(".json")
    session, separator, pane = stem.rpartition("_")
    if not separator or not session or not pane:
        return None
    return f"{session}:{pane}"


def pane_state_record_anomalies(paths: list[Path]) -> list[str]:
    anomalies: list[str] = []
    for root in paths:
        try:
            candidates = sorted(path for path in root.rglob("*.json") if path.is_file())
        except OSError:
            continue
        for path in candidates:
            expected_target = pane_state_target_from_file_name(path.name)
            detail = _pane_state_record_anomaly_detail(path, expected_target)
            if detail:
                anomalies.append(f"{path}: {detail}")
    return anomalies


def pane_state_record_anomaly_diff(before: list[str], after: list[str]) -> list[str]:
    return sorted(set(after) - set(before))


def _pane_session_record_allows_live_rewrite(path: Path) -> bool:
    expected_target = pane_state_target_from_file_name(path.name)
    if expected_target is None:
        return False
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(record, dict):
        return False
    if str(record.get("target", "")) != expected_target:
        return False
    if not str(record.get("session_id", "")).strip():
        return False
    source = str(record.get("source", ""))
    return source.startswith(PANE_SESSION_RUNTIME_SOURCE_PREFIXES)


def _pane_state_record_anomaly_detail(path: Path, expected_target: str | None) -> str:
    if expected_target is None:
        return "invalid pane-state filename"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"invalid pane-state json ({exc})"
    if not isinstance(record, dict):
        return "pane-state record is not an object"

    target = str(record.get("target", ""))
    if target != expected_target:
        return f"target {target!r} does not match filename target {expected_target!r}"

    source = str(record.get("source", ""))
    allowed_prefixes = PANE_STATE_SOURCE_PREFIXES_BY_TARGET.get(target)
    if allowed_prefixes is None:
        return f"target {target!r} is not a configured live pgu pane"
    if not source.startswith(allowed_prefixes):
        return f"source {source!r} is not valid for {target!r}"

    state = str(record.get("state", ""))
    if state not in PANE_STATE_STATES:
        return f"state {state!r} is not valid"

    return ""
