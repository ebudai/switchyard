"""Change an existing role's agent runtime, and put its panes back.

Switching a live role from one CLI to another touches four things that can each
fail independently: the board's declared workflow, the launcher's projection of
it, the worker tmux session, and the presentation slots showing that worker.

The order here is chosen so a failure is recoverable rather than confusing.
Everything that can be checked is checked while nothing has changed -- including
asking the board to validate the new workflow document without applying it.
Only then is anything written, and every write is journalled so it can be undone
in the reverse order.

The failure this exists to prevent is a silent one. A slot's proxy is an attach
to a worker session; when that session is replaced the proxy's session-closed
hook parks it on a recovery message and nothing re-attaches it. The replacement
worker runs perfectly well behind a blank pane, and the board reports success.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from scripts import presentation_controller, team_launcher
from scripts.ticket_board.write_client import DEFAULT_BOARD_URL, TicketBoardWriteClient

DIRECTOR_ROLE = "director"
JOURNAL_SCHEMA = "switchyard.role-runtime.v1"


class RoleRuntimeRefusal(SystemExit):
    """A refusal that changed nothing. The message names what to do instead."""


@dataclass(frozen=True)
class RuntimePreflight:
    project: str
    role: str
    current_runtime: str
    requested_runtime: str
    visible_slots: tuple[int, ...]
    detached: bool
    busy: bool
    workflow_revision: int
    blockers: tuple[str, ...] = ()

    @property
    def is_noop(self) -> bool:
        return self.current_runtime == self.requested_runtime

    @property
    def ok(self) -> bool:
        return not self.blockers


@dataclass
class RuntimeJournal:
    """Exactly what to undo, in the order it was done."""

    schema: str = JOURNAL_SCHEMA
    project: str = ""
    role: str = ""
    previous_runtime: str = ""
    requested_runtime: str = ""
    previous_workflow_revision: int = 0
    previous_workflow_document: dict[str, Any] = field(default_factory=dict)
    previous_config_bytes: str = ""
    config_path: str = ""
    slots: tuple[int, ...] = ()
    steps_applied: list[str] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def record(self, step: str) -> None:
        self.steps_applied.append(step)

    def write(self, path: Path) -> None:
        payload = asdict(self)
        payload["slots"] = list(self.slots)
        team_launcher._write_private_json_atomic(path, payload)


@dataclass(frozen=True)
class RuntimeSwitchResult:
    project: str
    role: str
    previous_runtime: str
    runtime: str
    configured_changed: bool
    live_session_changed: bool
    reconnected_slots: tuple[int, ...]
    forced: bool = False
    reason: str = ""
    journal_path: str = ""
    recoverable_failure: str = ""

    def describe(self) -> str:
        if not self.configured_changed:
            return f"switchyard: {self.role} already runs {self.runtime}; nothing to change"
        live = "restarted its session" if self.live_session_changed else "left its session untouched"
        slots = (
            "reconnected slot " + ", ".join(str(slot) for slot in self.reconnected_slots)
            if self.reconnected_slots
            else "no display slot showed it"
        )
        return (
            f"switchyard: {self.role} now runs {self.runtime} (was {self.previous_runtime}); "
            f"{live}; {slots}"
        )


def _require_director(config: team_launcher.ProjectConfig, environ: Mapping[str, str]) -> str:
    """Refuse to act as, or across, anyone else.

    The board enforces this too -- only a director may configure the workflow --
    but refusing here means a wrong caller is told so before any local file is
    touched, rather than halfway through.
    """
    actor = (environ.get("TICKET_BOARD_CALLER_ROLE") or "").strip().lower()
    if actor != DIRECTOR_ROLE:
        raise RoleRuntimeRefusal(
            "switchyard: changing a role runtime requires TICKET_BOARD_CALLER_ROLE=director"
        )
    selected = (environ.get("TICKET_BOARD_PROJECT") or "").strip()
    if selected and selected != config.project:
        raise RoleRuntimeRefusal(
            f"switchyard: refusing a cross-project runtime change from {selected!r} to {config.project!r}"
        )
    return actor


def _role_or_refuse(config: team_launcher.ProjectConfig, role_name: str) -> team_launcher.RoleConfig:
    role = next((candidate for candidate in config.roles if candidate.role == role_name), None)
    if role is None:
        known = ", ".join(sorted(candidate.role for candidate in config.roles))
        raise RoleRuntimeRefusal(
            f"switchyard: {config.project} has no role {role_name!r}; it has {known}"
        )
    return role


def _runtime_of(role: team_launcher.RoleConfig) -> str:
    return team_launcher._role_cli_name(role)


def workflow_document(
    *,
    board_url: str,
    opener: Callable[[str], Any] | None = None,
) -> tuple[dict[str, Any], int]:
    """The board's declared workflow and its revision, read over HTTP."""
    from urllib import request as urllib_request

    url = f"{board_url.rstrip('/')}/api/workflow"
    open_url = opener or (lambda target: urllib_request.urlopen(target, timeout=10))
    with open_url(url) as response:
        payload = json.load(response)
    document = payload.get("document")
    if not isinstance(document, dict):
        raise RoleRuntimeRefusal(
            f"switchyard: {url} returned no declared workflow; this project's board is not workflow-driven"
        )
    return document, int(payload.get("revision") or 0)


def document_with_runtime(document: Mapping[str, Any], *, role: str, runtime: str) -> dict[str, Any]:
    """The same workflow document with one role's runtime replaced."""
    updated = copy.deepcopy(dict(document))
    roles = updated.get("roles")
    if not isinstance(roles, list):
        raise RoleRuntimeRefusal("switchyard: declared workflow has no roles list")
    for entry in roles:
        if not isinstance(entry, dict) or entry.get("name") != role:
            continue
        if entry.get("runtime") is None:
            raise RoleRuntimeRefusal(
                f"switchyard: {role} has no runtime in the declared workflow; "
                "only roles the launcher starts have one to change"
            )
        entry["runtime"] = runtime
        return updated
    raise RoleRuntimeRefusal(f"switchyard: {role} is not in the declared workflow")


def _readiness_blockers(
    config: team_launcher.ProjectConfig,
    role: team_launcher.RoleConfig,
    runtime: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> tuple[str, ...]:
    """Whether ``runtime`` could actually start, checked before anything stops.

    Reuses the launcher's own first-run checks rather than a second opinion, so
    a runtime that passes here is one the launcher would also accept.
    """
    owner_user = config.run_as_user or team_launcher.current_user_name()
    owner_home = team_launcher._owner_home_for_auth(owner_user)
    blockers: list[str] = []
    status = team_launcher._cli_auth_status(
        runtime, owner_user=owner_user, owner_home=owner_home, runner=runner
    )
    if status == "not_installed":
        blockers.append(f"{runtime} is not installed for {owner_user}")
    elif status not in {"authenticated", "unknown"}:
        login = " ".join(team_launcher.FIRST_RUN_AUTH_LOGIN_COMMANDS.get(runtime, ()))
        blockers.append(
            f"{runtime} is not logged in for {owner_user}"
            + (f"; run: {login}" if login else "")
        )
    if runtime in team_launcher.FIRST_RUN_TRUST_CLIS and not team_launcher._workdir_is_trusted(
        runtime, owner_home=owner_home, workdir=Path(role.workdir)
    ):
        blockers.append(f"{runtime} does not trust {role.workdir} yet")
    return tuple(blockers)


def _is_busy(
    role: team_launcher.RoleConfig,
    *,
    pane_state_dir: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> bool:
    """Whether switching now would interrupt work in progress.

    A role that is not running has nothing to interrupt. Beyond that this is the
    launcher's own activity gate, which fails closed: a live session whose hooks
    have written no state reads as busy, because the alternative is to kill a
    turn on the strength of an absent record. `--force --reason` is the way past
    it, and it is deliberately not silent.
    """
    from scripts.ticket_board.notify_listener import PaneActivityGate, PaneHookStateStore

    running = runner(
        team_launcher.tmux_has_session_args(role),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0
    if not running:
        return False
    return PaneActivityGate(state_store=PaneHookStateStore(pane_state_dir)).is_busy(role.target)


def preflight(
    config: team_launcher.ProjectConfig,
    *,
    config_path: Path,
    role_name: str,
    runtime: str,
    pane_state_dir: Path | None = None,
    board_url: str = "",
    force: bool = False,
    environ: Mapping[str, str] = os.environ,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    workflow_reader: Callable[..., tuple[dict[str, Any], int]] | None = None,
    busy_check: Callable[..., bool] = _is_busy,
) -> tuple[RuntimePreflight, dict[str, Any]]:
    """Everything that can be decided before anything changes."""
    role = _role_or_refuse(config, role_name)
    current = _runtime_of(role)
    if runtime not in team_launcher.SUPPORTED_CONFIG_CLI_NAMES:
        supported = ", ".join(team_launcher.SUPPORTED_CONFIG_CLI_NAMES)
        raise RoleRuntimeRefusal(f"switchyard: unsupported runtime {runtime!r}; choose one of {supported}")

    read_workflow = workflow_reader or workflow_document
    document, revision = read_workflow(
        board_url=board_url or config.board_url or DEFAULT_BOARD_URL
    )

    slots = presentation_controller.slots_showing_role(
        config, config_path=config_path, role_name=role_name
    ) if presentation_controller.presentation_enabled(config, config_path=config_path) else ()

    state_dir = pane_state_dir or team_launcher.default_pane_state_dir_for_user(
        config.run_as_user or team_launcher.current_user_name(), project=config.project
    )
    busy = busy_check(role, pane_state_dir=state_dir, runner=runner)

    blockers: list[str] = []
    if current != runtime:
        blockers.extend(_readiness_blockers(config, role, runtime, runner=runner))
        if busy and not force:
            blockers.append(
                f"{role.target} is busy; wait for an idle checkpoint, or pass --force with --reason "
                "to interrupt it deliberately"
            )
    return (
        RuntimePreflight(
            project=config.project,
            role=role_name,
            current_runtime=current,
            requested_runtime=runtime,
            visible_slots=slots,
            detached=role.detached,
            busy=busy,
            workflow_revision=revision,
            blockers=tuple(blockers),
        ),
        document,
    )


def _write_runtime_projection(
    config: team_launcher.ProjectConfig,
    *,
    config_path: Path,
    role_name: str,
    runtime: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> team_launcher.ProjectConfig:
    """Point the launcher config at the new runtime, leaving everything else."""
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    roles = raw.get("roles")
    if not isinstance(roles, list):
        raise RoleRuntimeRefusal(f"switchyard: {config_path} must define a roles list")
    for entry in roles:
        if not isinstance(entry, dict) or entry.get("role") != role_name:
            continue
        # `cli` is a list so a role can carry flags; only the program changes.
        existing = entry.get("cli")
        if isinstance(existing, list) and existing:
            entry["cli"] = [runtime, *existing[1:]]
        else:
            entry["cli"] = [runtime]
        for key, table in (
            ("resume_mode", team_launcher.DEFAULT_RESUME_MODE_BY_CLI),
            ("resume_flag", team_launcher.DEFAULT_RESUME_FLAG_BY_CLI),
            ("resume_subcommand", team_launcher.DEFAULT_RESUME_SUBCOMMAND_BY_CLI),
        ):
            # Resume semantics belong to the runtime, so a stale one left behind
            # would try to resume the new CLI with the old CLI's flag.
            entry.pop(key, None)
            if runtime in table:
                entry[key] = table[runtime]
        break
    else:
        raise RoleRuntimeRefusal(f"switchyard: {config_path} has no role {role_name!r}")
    team_launcher._write_json_atomic(config_path, raw)
    team_launcher.ensure_owner_file(config, config_path, runner=runner)
    return team_launcher.load_project_config(config.project, config_path)


def _session_is_live(
    role: team_launcher.RoleConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> bool:
    return runner(
        team_launcher.tmux_has_session_args(role),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def _default_start(
    role: team_launcher.RoleConfig,
    *,
    config: team_launcher.ProjectConfig,
    pane_state_dir: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> int:
    return team_launcher.ensure_visible_role_session_for_viewer(
        role,
        mode="attach-or-start",
        session_dir=config.session_dir,
        pane_state_dir=pane_state_dir,
        bin_user=config.run_as_user,
        runner=runner,
    )


def _restart_worker(
    config: team_launcher.ProjectConfig,
    *,
    role_name: str,
    pane_state_dir: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    start: Callable[..., int] = _default_start,
    on_stopped: Callable[[], None] | None = None,
) -> bool:
    """Replace the role's session with one running the configured runtime.

    The recorded resume id belongs to the runtime being left behind, so it is
    cleared rather than handed to a CLI that cannot read it. Returns whether a
    live session was actually replaced, which is what tells a later reader
    configured state from live state.

    ``on_stopped`` fires between the stop and the start. A stop that is not
    recorded the moment it happens is invisible to rollback if the start then
    fails, which leaves the role down while the command reports the switch
    cleanly undone.
    """
    role = team_launcher._role_by_name(config, role_name)
    existed = _session_is_live(role, runner=runner)
    if existed:
        kill = runner(team_launcher.tmux_kill_session_args(role))
        if kill.returncode != 0:
            raise RuntimeError(f"could not stop {role.tmux_session} (exit {kill.returncode})")
        if on_stopped is not None:
            on_stopped()
    team_launcher.clear_session_record_for_role(role, config.session_dir)
    team_launcher.clear_pane_idle_state_for_role(role, pane_state_dir=pane_state_dir)
    result = start(role, config=config, pane_state_dir=pane_state_dir, runner=runner)
    if result != 0:
        raise RuntimeError(f"{role_name} did not come back up under its new runtime (exit {result})")
    if not _session_is_live(role, runner=runner):
        raise RuntimeError(f"{role_name} reported a successful start but left no live session")
    return existed


def journal_path_for(config: team_launcher.ProjectConfig, *, config_path: Path, role_name: str) -> Path:
    return team_launcher.default_layout_output_path(config, config_path=config_path).parent / (
        f"role-runtime-{role_name}.journal.json"
    )


def _rollback(
    journal: RuntimeJournal,
    *,
    config_path: Path,
    pane_state_dir: Path,
    client: TicketBoardWriteClient,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    start: Callable[..., int] = _default_start,
    print_func: Callable[[str], None] = print,
) -> str:
    """Undo, in reverse, exactly the steps that were applied.

    Returns "" when the prior state is restored, or a description of what is
    still wrong. A partial rollback is reported rather than swallowed: a wrong
    state an operator knows about is recoverable, one they do not is not.
    """
    problems: list[str] = []
    applied = set(journal.steps_applied)

    def attempt(step: str, undo: Callable[[], None]) -> bool:
        try:
            undo()
        except Exception as exc:  # noqa: BLE001 - every failure must be reported, not raised
            problems.append(f"{step}: {exc}")
            print_func(f"switchyard: rollback of {step} failed: {exc}")
            return False
        return True

    # Durable records first, runtime second. Undoing this strictly in reverse
    # would restart the worker while the projection still named the new runtime,
    # bringing the role back up under the CLI that just failed to start.
    if "projection" in applied:
        attempt(
            "projection",
            lambda: Path(journal.config_path).write_text(journal.previous_config_bytes, encoding="utf-8"),
        )
    if "workflow" in applied:
        attempt(
            "workflow",
            lambda: client.configure_workflow(
                journal.previous_workflow_document, expected_revision=0, dry_run=False
            ),
        )

    restored_worker = True
    if "worker_stopped" in applied:
        # The role is down right now. Bringing it back is the whole point of the
        # rollback, so a clean report is only honest once its session is live.
        def restart_previous() -> None:
            config = team_launcher.load_project_config(journal.project, config_path)
            _restart_worker(
                config,
                role_name=journal.role,
                pane_state_dir=pane_state_dir,
                runner=runner,
                start=start,
            )
            role = team_launcher._role_by_name(config, journal.role)
            if not _session_is_live(role, runner=runner):
                raise RuntimeError(f"{journal.role} is still not running {journal.previous_runtime}")

        restored_worker = attempt("worker", restart_previous)

    if restored_worker and (journal.slots or "slots" in applied):
        attempt(
            "slots",
            lambda: presentation_controller.reconnect_role_slots(
                team_launcher.load_project_config(journal.project, config_path),
                config_path=config_path,
                role_name=journal.role,
                runner=runner,
            ),
        )
    return "; ".join(problems)


def switch_role_runtime(
    config: team_launcher.ProjectConfig,
    *,
    config_path: Path,
    role_name: str,
    runtime: str,
    force: bool = False,
    reason: str = "",
    dry_run: bool = False,
    pane_state_dir: Path | None = None,
    environ: Mapping[str, str] = os.environ,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    client: TicketBoardWriteClient | None = None,
    workflow_reader: Callable[..., tuple[dict[str, Any], int]] | None = None,
    start: Callable[..., int] = _default_start,
    busy_check: Callable[..., bool] = _is_busy,
    print_func: Callable[[str], None] = print,
) -> RuntimeSwitchResult:
    _require_director(config, environ)
    if force and not reason.strip():
        raise RoleRuntimeRefusal(
            "switchyard: --force interrupts a working role, so it requires --reason describing why"
        )

    checks, document = preflight(
        config,
        config_path=config_path,
        role_name=role_name,
        runtime=runtime,
        pane_state_dir=pane_state_dir,
        force=force,
        environ=environ,
        runner=runner,
        workflow_reader=workflow_reader,
        busy_check=busy_check,
    )
    if checks.blockers:
        raise RoleRuntimeRefusal(
            f"switchyard: cannot switch {role_name} to {runtime}:\n  - "
            + "\n  - ".join(checks.blockers)
        )
    if checks.is_noop:
        return RuntimeSwitchResult(
            project=config.project,
            role=role_name,
            previous_runtime=checks.current_runtime,
            runtime=runtime,
            configured_changed=False,
            live_session_changed=False,
            reconnected_slots=(),
        )

    board = client or TicketBoardWriteClient(
        board_url=config.board_url or DEFAULT_BOARD_URL, caller_role=DIRECTOR_ROLE
    )
    proposed = document_with_runtime(document, role=role_name, runtime=runtime)
    # The board validates the whole document, so this rejects a change the
    # launcher would happily write and the board would then refuse.
    board.configure_workflow(proposed, expected_revision=checks.workflow_revision, dry_run=True)
    if dry_run:
        return RuntimeSwitchResult(
            project=config.project,
            role=role_name,
            previous_runtime=checks.current_runtime,
            runtime=runtime,
            configured_changed=True,
            live_session_changed=False,
            reconnected_slots=checks.visible_slots,
            forced=force,
            reason=reason,
        )

    state_dir = pane_state_dir or team_launcher.default_pane_state_dir_for_user(
        config.run_as_user or team_launcher.current_user_name(), project=config.project
    )
    journal = RuntimeJournal(
        project=config.project,
        role=role_name,
        previous_runtime=checks.current_runtime,
        requested_runtime=runtime,
        previous_workflow_revision=checks.workflow_revision,
        previous_workflow_document=copy.deepcopy(dict(document)),
        previous_config_bytes=config_path.read_text(encoding="utf-8"),
        config_path=str(config_path),
        slots=checks.visible_slots,
    )
    journal_path = journal_path_for(config, config_path=config_path, role_name=role_name)
    journal.write(journal_path)

    live_changed = False
    reconnected: tuple[int, ...] = ()
    try:
        board.configure_workflow(proposed, expected_revision=checks.workflow_revision, dry_run=False)
        journal.record("workflow")
        journal.write(journal_path)

        updated = _write_runtime_projection(
            config, config_path=config_path, role_name=role_name, runtime=runtime, runner=runner
        )
        journal.record("projection")
        journal.write(journal_path)

        def note_stopped() -> None:
            journal.record("worker_stopped")
            journal.write(journal_path)

        live_changed = _restart_worker(
            updated,
            role_name=role_name,
            pane_state_dir=state_dir,
            runner=runner,
            start=start,
            on_stopped=note_stopped,
        )
        journal.record("worker")
        journal.write(journal_path)

        # Only now: a slot reconnected into the gap between kill and start
        # attaches to nothing and parks itself again.
        if presentation_controller.presentation_enabled(updated, config_path=config_path):
            reconnected = presentation_controller.reconnect_role_slots(
                updated, config_path=config_path, role_name=role_name, runner=runner
            )
            journal.record("slots")
            journal.write(journal_path)
    except Exception as exc:  # noqa: BLE001 - a half-applied switch must not look like success
        print_func(f"switchyard: {role_name} runtime switch failed: {exc}")
        remaining = _rollback(
            journal,
            config_path=config_path,
            pane_state_dir=state_dir,
            client=board,
            runner=runner,
            start=start,
            print_func=print_func,
        )
        if remaining:
            raise RoleRuntimeRefusal(
                f"switchyard: {role_name} is left between runtimes and needs an operator: {remaining}. "
                f"The exact prior state is in {journal_path}."
            ) from exc
        journal_path.unlink(missing_ok=True)
        raise RoleRuntimeRefusal(
            f"switchyard: {role_name} still runs {checks.current_runtime}; the switch was undone: {exc}"
        ) from exc

    journal_path.unlink(missing_ok=True)
    return RuntimeSwitchResult(
        project=config.project,
        role=role_name,
        previous_runtime=checks.current_runtime,
        runtime=runtime,
        configured_changed=True,
        live_session_changed=live_changed,
        reconnected_slots=reconnected,
        forced=force,
        reason=reason,
        journal_path=str(journal_path),
    )
