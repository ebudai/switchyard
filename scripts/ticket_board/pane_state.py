"""Pane-state authority and hook storage: where the pane hooks write each pane's state, and whether that is where the listener reads.

`PaneHookStateStore` reads and writes one JSON record per tmux pane -- idle,
busy or blocked, with the hook that wrote it and when the pane's turn began
(`PaneHookState`, `hook_turn_started_at`, `TURN_START_EVENTS`) -- under
`DEFAULT_PANE_STATE_DIR` unless told otherwise. `pane_state_authority` and
`PaneStateAuthority` say whether a directory holds hook state for the roles the
board has registered, so a listener reading the wrong directory is named at
startup instead of deferring every notification in silence (SYRD-95); the
runtime-assignment readers (`registered_pane_targets`, `live_pane_targets`,
`assignment_process_gone`, `_pid_exists`) say which of those roles are live, and
`load_runtime_assignments` and `verify_pane_state_authority` are the offline
check the CLI runs with `--verify-pane-state-authority`.

Moved out of `scripts/ticket_board/notify_listener.py` unchanged (SYRD-479).
`notify_listener` imports this module and re-exports every name, so every module
and test that imports them from there, or patches them there, still reaches the
same objects -- `role_runtime`, `role_pane_entry`, the pane activity gate and
the listener's `main` among them. What the moved code reads of `notify_listener`
when it runs -- each other and `read_process` -- is read through it, so a patch
there still reaches it. At load this module imports the standard library and
`PROC_ROOT`, the default two of its functions bind; it imports
`notify_listener` only inside the functions and methods that need it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .peer_identity import PROC_ROOT


DEFAULT_PANE_STATE_DIR = (
    Path(os.environ["TICKET_BOARD_PANE_STATE_DIR"]).expanduser()
    if os.environ.get("TICKET_BOARD_PANE_STATE_DIR")
    else Path(os.environ["PGU_TICKET_BOARD_PANE_STATE_DIR"]).expanduser()
    if os.environ.get("PGU_TICKET_BOARD_PANE_STATE_DIR")
    else Path(f"/run/user/{os.getuid()}/pgu-ticket-board/pane-state")
)


@dataclass(frozen=True)
class PaneHookState:
    target: str
    state: str
    updated_at: float
    source: str = ""
    #: None when the file predates SYRD-268 and never recorded it.
    turn_started_at: float | None = None
    #: The hook's record of what the runtime said was still running when its
    #: turn ended (SYRD-538), as written; read by background_work. None when
    #: nothing was, or the file predates it.
    background_work: object = None
    #: The pane's latest SessionStart as the hook recorded it -- {session_id,
    #: source, at} -- or None (SYRD-540); read by session_context.
    last_session_start: object = None
    last_checkout: object = None


#: Hook events that mean a turn STARTED in the pane: a prompt was taken and the
#: runtime began working on it. Only a start can witness a notice. A turn that
#: ENDS after a send may be a turn that was already running when the notice
#: was typed into it -- which is the case this exists to catch -- and session
#: starts, launcher writes and Claude's periodic idle notification are not
#: turns at all (SYRD-268).
TURN_START_EVENTS = frozenset({
    "UserPromptSubmit",   # claude, codex
    "PreInvocation",      # gemini / agy
    "pre_llm_call",       # hermes
})


def hook_turn_started_at(
    previous: dict[str, Any] | None, state: str, source: str, now: float
) -> float | None:
    """When the pane's latest turn started, carried across later writes.

    The state file holds only the latest state, and a turn's end overwrites its
    start; without this, a listener that looks a moment late sees only "idle"
    and cannot tell a turn happened. So a turn start sets it, and every other
    write keeps what was there. The hook script writes the same field the
    same way, and a test holds the two to it.
    """
    from . import notify_listener as listener

    if state == "busy" and (source or "").rsplit(".", 1)[-1] in listener.TURN_START_EVENTS:
        return now
    if isinstance(previous, dict):
        try:
            carried = previous.get("turn_started_at")
            return float(carried) if carried is not None else None
        except (TypeError, ValueError):
            return None
    return None


@dataclass(frozen=True)
class PaneStateAuthority:
    """Whether the directory the listener reads is the one the panes write to.

    The listener decides delivery from hook state, and a missing file reads as
    "cannot tell, assume busy" -- which is the right default for one pane and
    the wrong one for all of them at once. Pointed at a directory no hook
    writes to, it defers every notification forever while the process, the
    socket and the runtime assignments all look healthy. That is what a
    process-active check cannot see, so this is the thing to check instead
    (SYRD-95).
    """

    state_dir: Path
    registered: tuple[str, ...]
    with_state: tuple[str, ...]
    without_state: tuple[str, ...]
    # Assignments whose process is provably gone, with the proof. They are
    # not registered roles for this purpose: the listener itself drops them
    # before delivering (refresh_workflow), so no hook state is owed for them
    # and their absence says nothing about which directory is right (SYRD-252).
    stale: tuple[tuple[str, str], ...] = ()

    @property
    def ok(self) -> bool:
        # Nothing registered is not a disagreement: a board with no panes yet
        # has nobody to serve and nothing to be wrong about. Registered roles
        # with hook state for none of them is the failure, because it can only
        # mean the two halves are looking at different directories.
        return not self.registered or bool(self.with_state)

    def describe(self) -> str:
        stale = ""
        if self.stale:
            stale = (
                f"; {len(self.stale)} stale assignment(s) not counted, their process is gone: "
                + ", ".join(f"{target} ({reason})" for target, reason in self.stale)
            )
        if not self.registered:
            if self.stale:
                return (
                    f"pane-state authority: no live registered roles; nothing to serve from "
                    f"{self.state_dir}{stale}"
                )
            return f"pane-state authority: no registered roles; nothing to serve from {self.state_dir}"
        return self._describe_registered() + stale

    def _describe_registered(self) -> str:
        if not self.with_state:
            return (
                f"pane-state authority: {self.state_dir} holds hook state for none of the "
                f"{len(self.registered)} registered roles ({', '.join(self.registered)}). "
                "The listener and the pane hooks are reading and writing different "
                "directories, so every pane reads as busy and nothing is delivered."
            )
        line = (
            f"pane-state authority: {self.state_dir} holds hook state for "
            f"{len(self.with_state)} of {len(self.registered)} registered roles"
        )
        if self.without_state:
            # Not a failure. A pane that has not run a hook yet has no file,
            # and one role being quiet is not the two halves disagreeing.
            line += f"; no state yet for {', '.join(self.without_state)}"
        return line


def pane_state_authority(
    targets: Iterable[str],
    store: "PaneHookStateStore",
    *,
    stale: Iterable[tuple[str, str]] = (),
) -> PaneStateAuthority:
    """Compare the configured directory against the roles actually registered."""
    from . import notify_listener as listener

    registered = tuple(dict.fromkeys(target for target in targets if target))
    with_state = tuple(target for target in registered if store.read(target) is not None)
    without_state = tuple(target for target in registered if target not in set(with_state))
    return listener.PaneStateAuthority(
        state_dir=store.state_dir,
        registered=registered,
        with_state=with_state,
        without_state=without_state,
        stale=tuple(stale),
    )


def _pid_exists(pid: int) -> bool | None:
    """Whether the kernel knows this pid, even when /proc will not show it.

    kill(pid, 0) is not subject to hidepid, so EPERM still proves existence.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def assignment_process_gone(
    assignment: Any,
    *,
    proc_root: Path = PROC_ROOT,
    pid_exists: Callable[[int], bool | None] = _pid_exists,
) -> str:
    """Why an assignment's process is provably gone, or "" if it may be live.

    Only positive proof counts. An assignment with no recorded process, or a
    process that exists but cannot be read, is treated as live -- so the check
    stays fail-closed for anything it cannot see (SYRD-95), and a stopped
    tenant, whose recorded processes simply no longer exist, can still deploy
    (SYRD-252).
    """
    from . import notify_listener as listener

    if not isinstance(assignment, dict):
        return ""
    try:
        pid = int(assignment.get("process_pid") or 0)
    except (TypeError, ValueError):
        return ""
    if pid <= 1:
        return ""
    try:
        recorded_start = int(assignment.get("process_start_time") or 0)
    except (TypeError, ValueError):
        recorded_start = 0
    process = listener.read_process(pid, proc_root=proc_root)
    if process is not None:
        if recorded_start > 0 and process.start_time != recorded_start:
            return f"pid {pid} was reused: started at {process.start_time}, not {recorded_start}"
        return ""
    if proc_root != listener.PROC_ROOT:
        # A substituted /proc has no kernel behind it to ask; absence there is
        # all the evidence there is.
        return "" if (proc_root / str(pid)).exists() else f"pid {pid} no longer exists"
    # Unreadable is not gone: hidepid, or a process this caller may not
    # inspect. Only the kernel saying "no such process" proves it.
    return f"pid {pid} no longer exists" if pid_exists(pid) is False else ""


def live_pane_targets(
    payload: Any, *, proc_root: Path = PROC_ROOT
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    """Split the board's registered targets into possibly-live and provably-stale."""
    from . import notify_listener as listener

    assignments = (payload or {}).get("assignments") if isinstance(payload, dict) else None
    if not isinstance(assignments, dict):
        return (), ()
    live: list[str] = []
    stale: list[tuple[str, str]] = []
    for assignment in assignments.values():
        if not isinstance(assignment, dict):
            continue
        target = str(assignment.get("actual_target") or "").strip()
        if not target:
            continue
        reason = listener.assignment_process_gone(assignment, proc_root=proc_root)
        if reason:
            stale.append((target, reason))
        else:
            live.append(target)
    live_targets = tuple(dict.fromkeys(live))
    return live_targets, tuple((t, r) for t, r in dict.fromkeys(stale) if t not in live_targets)


def registered_pane_targets(payload: Any) -> tuple[str, ...]:
    """The targets a board's runtime assignments say are live.

    Read from the board rather than assumed from a role list, because the
    question is what the listener would actually try to deliver to.
    """

    assignments = (payload or {}).get("assignments") if isinstance(payload, dict) else None
    if not isinstance(assignments, dict):
        return ()
    targets: list[str] = []
    for assignment in assignments.values():
        if not isinstance(assignment, dict):
            continue
        target = str(assignment.get("actual_target") or "").strip()
        if target:
            targets.append(target)
    return tuple(dict.fromkeys(targets))


class PaneHookStateStore:
    def __init__(self, state_dir: str | Path = DEFAULT_PANE_STATE_DIR) -> None:
        self.state_dir = Path(state_dir).expanduser()

    def _target_path(self, target: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in ".-" else "_" for ch in target)
        return self.state_dir / f"{safe}.json"

    def read(self, target: str) -> PaneHookState | None:
        from . import notify_listener as listener

        path = self._target_path(target)
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(parsed, dict):
            return None
        state = str(parsed.get("state") or "").strip().lower()
        if state not in {"idle", "busy", "blocked"}:
            return None
        try:
            updated_at = float(parsed.get("updated_at"))
        except (TypeError, ValueError):
            return None
        turn_started_at: float | None
        try:
            raw_turn = parsed.get("turn_started_at")
            turn_started_at = float(raw_turn) if raw_turn is not None else None
        except (TypeError, ValueError):
            turn_started_at = None
        return listener.PaneHookState(
            target=str(parsed.get("target") or target),
            state=state,
            updated_at=updated_at,
            source=str(parsed.get("source") or ""),
            turn_started_at=turn_started_at,
            background_work=parsed.get("background_work") if state == "idle" else None,
            last_session_start=parsed.get("last_session_start") if isinstance(parsed.get("last_session_start"), dict) else None,
            last_checkout=parsed.get("last_checkout") if isinstance(parsed.get("last_checkout"), dict) else None,
        )

    def write(self, target: str, state: str, *, source: str = "", now: float | None = None) -> Path:
        from . import notify_listener as listener

        normalized_state = state.strip().lower()
        if normalized_state not in {"idle", "busy", "blocked"}:
            raise ValueError("pane hook state must be idle, busy, or blocked")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        written_at = time.time() if now is None else now
        path = self._target_path(target)
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = None
        payload = {
            "target": target,
            "state": normalized_state,
            "updated_at": written_at,
            "source": source,
        }
        turn_started_at = listener.hook_turn_started_at(previous, normalized_state, source, written_at)
        if turn_started_at is not None:
            payload["turn_started_at"] = turn_started_at
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(path)
        return path


def load_runtime_assignments(*, board_url: str, assignments_json: str) -> Any:
    if assignments_json:
        return json.loads(Path(assignments_json).read_text(encoding="utf-8"))
    if not board_url:
        raise SystemExit(
            "ticket notify listener: --verify-pane-state-authority needs --board-url or TICKET_BOARD_URL"
        )
    import urllib.request

    with urllib.request.urlopen(board_url.rstrip("/") + "/api/runtime-assignments", timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def verify_pane_state_authority(args: argparse.Namespace) -> int:
    """Fail closed when the configured directory serves none of the live roles.

    Deliberately not a liveness check. The listener process, its socket and the
    board's runtime assignments were all healthy throughout SYRD-95; the one
    thing that was wrong was which directory it read, and only this comparison
    can see that.
    """
    from . import notify_listener as listener

    payload = listener.load_runtime_assignments(
        board_url=args.board_url, assignments_json=args.assignments_json
    )
    live, stale = listener.live_pane_targets(payload)
    report = listener.pane_state_authority(live, listener.PaneHookStateStore(args.pane_state_dir), stale=stale)
    print(report.describe())
    if report.ok:
        return 0
    print(
        "the listener would defer every notification while looking healthy; "
        "point TICKET_BOARD_PANE_STATE_DIR at the directory the installed pane hooks write to",
        file=sys.stderr,
    )
    return 1
