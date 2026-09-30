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
from scripts.ticket_board import runtime_catalog
from scripts.tmux_session_argv import tmux_current_command_args
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
    #: What the role runs today, and what the caller asked for. `None` means
    #: the caller had nothing to say about the model, which is how every
    #: pre-SYRD-250 caller behaves.
    current_model: str = ""
    requested_model: str | None = None
    #: What the board declares the role to run, when its document names the
    #: role. It can differ from the launcher's `current_runtime`: a switch
    #: whose rollback could not undo its workflow write leaves exactly that
    #: split, and a request for the launcher's runtime is then the repair, not
    #: a no-op (SYRD-525).
    board_runtime: str | None = None

    @property
    def is_noop(self) -> bool:
        """Nothing to do only when NEITHER half of the choice is changing.

        The model used to be invisible here, so `set-role-runtime` on a role
        already using that runtime returned "no change" and kept a model the
        account does not recognise -- leaving no supported way to repair
        test2's audit role at all (SYRD-250 DAT).
        """
        if self.current_runtime != self.requested_runtime:
            return False
        if self.board_runtime is not None and self.board_runtime != self.requested_runtime:
            return False
        return self.requested_model is None or self.requested_model == self.current_model

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
    #: The revision the forward workflow write created. Undoing that write is
    #: an apply on top of it; using the revision read before the switch, or 0,
    #: is refused by the board every time (SYRD-486).
    applied_workflow_revision: int = 0
    #: What the rollback could not undo, and the one supported way on, kept in
    #: the journal an operator is pointed at rather than only on a terminal.
    rollback_problems: list[str] = field(default_factory=list)
    recovery: str = ""
    #: What the board, the launcher config and the worker actually showed when
    #: the rollback stopped -- read, never inferred from what was requested
    #: (SYRD-486 Director review). A value that could not be read says so.
    observed: dict[str, Any] = field(default_factory=dict)

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
    #: What the board declared before, when that was not what the launcher ran.
    previous_board_runtime: str = ""

    @property
    def was(self) -> str:
        """What the role ran before, as both halves had it."""
        if self.previous_board_runtime and self.previous_board_runtime != self.previous_runtime:
            return f"{self.previous_runtime}, while the board declared {self.previous_board_runtime}"
        return self.previous_runtime

    def describe(self) -> str:
        if not self.configured_changed:
            return f"switchyard: {self.role} already runs {self.runtime}; nothing to change"
        live = (
            "restarted its session"
            if self.live_session_changed
            else f"it was not running, and will start as {self.runtime} at the next launch"
        )
        slots = (
            "reconnected slot " + ", ".join(str(slot) for slot in self.reconnected_slots)
            if self.reconnected_slots
            else "no display slot showed it"
        )
        return (
            f"switchyard: {self.role} now runs {self.runtime} (was {self.was}); "
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


def _verifiable_projection_blockers(
    config_path: Path, role_name: str, runtime: str, *, model: str | None
) -> list[str]:
    """Refuse, before anything stops, a switch its own start check could not confirm.

    The start verifier accepts exactly the projected entry's live commands. If
    they would not name the new runtime -- or would still name another -- a
    healthy new session reads as absent and the working session has already
    been stopped for nothing (SYRD-486). Computed with the same projection the
    switch writes, on a copy.
    """
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"cannot read {config_path} to check the projection: {exc}"]
    entry = next(
        (item for item in raw.get("roles") or [] if isinstance(item, dict) and item.get("role") == role_name),
        None,
    )
    if entry is None:
        return [f"{config_path} has no role {role_name!r}"]
    projected = copy.deepcopy(entry)
    project_role_runtime(projected, runtime=runtime, model=model)
    accepted = projected_live_commands(projected)
    others = sorted(accepted & set(team_launcher.SUPPORTED_CONFIG_CLI_NAMES) - {runtime})
    if runtime not in accepted or others:
        return [
            f"the launcher projection for {role_name} would accept {sorted(accepted)} as its live "
            f"process, so a {runtime} session could not be confirmed after the switch"
        ]
    return []


def preflight(
    config: team_launcher.ProjectConfig,
    *,
    config_path: Path,
    role_name: str,
    runtime: str,
    #: The model the role should run. `None` leaves whatever is configured and
    #: keeps this a runtime-only decision.
    model: str | None = None,
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

    current_model = str(getattr(role, "model", "") or "")
    board_runtime = _declared_runtime(document, role_name)
    # A model-only change restarts the role exactly as a runtime change does,
    # so it earns the same readiness and busy checks. Asking "is the runtime
    # changing" skipped both for the one repair this ticket exists to enable.
    # So does a board that declares something else: the launcher alone is not
    # the whole of what the role runs (SYRD-525).
    changing = (
        current != runtime
        or (board_runtime is not None and board_runtime != runtime)
        or (model is not None and model != current_model)
    )

    blockers: list[str] = []
    if changing:
        blockers.extend(_readiness_blockers(config, role, runtime, runner=runner))
        blockers.extend(_verifiable_projection_blockers(config_path, role_name, runtime, model=model))
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
            current_model=current_model,
            requested_model=model,
            board_runtime=board_runtime,
        ),
        document,
    )


def _declared_runtime(document: Mapping[str, Any] | None, role_name: str) -> str | None:
    """The runtime the board's document declares for the role, or None when it names none."""
    entry = next(
        (item for item in (document or {}).get("roles") or [] if isinstance(item, Mapping) and item.get("name") == role_name),
        None,
    )
    declared = (entry or {}).get("runtime")
    return str(declared) if declared else None


def project_role_runtime(entry: dict[str, Any], *, runtime: str, model: str | None = None) -> None:
    """Rewrite one role's launcher entry, in place, for a new runtime.

    Every field that belongs to the runtime moves with it; everything else
    stays. The switch and its preflight both call this, so what preflight
    checks is exactly what the switch will write.
    """
    # `cli` is a list so a role can carry flags; only the program changes.
    existing = entry.get("cli")
    if isinstance(existing, list) and existing:
        entry["cli"] = [runtime, *existing[1:]]
    else:
        entry["cli"] = [runtime]
    # The names the start verifier accepts as this role's live process. Every
    # generator writes the runtime's own name here, and the switch never
    # rewrote it: a role moved to Codex kept `live_commands: ["claude"]`, so
    # the verifier called a healthy fresh Codex session absent, the switch
    # "failed", and its rollback stranded the worker (SYRD-388, SYRD-447,
    # SYRD-485, SYRD-486). A runtime's name is only ever the runtime's, so any
    # supported runtime named here is replaced by the new one -- which also
    # repairs a list a previous switch left stale -- while a custom command
    # name is kept, and the new runtime is always accepted. No list stays no
    # list: the verifier already derives it from `cli`.
    live = entry.get("live_commands")
    if isinstance(live, list) and live:
        runtimes = set(team_launcher.SUPPORTED_CONFIG_CLI_NAMES)
        kept = [
            str(command) for command in live
            if team_launcher._command_name(str(command)) not in runtimes
        ]
        entry["live_commands"] = list(dict.fromkeys([runtime, *kept]))
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
    # A model belongs to the runtime exactly as resume semantics do, and it
    # was the one thing left behind: a role moved from Codex to Claude kept
    # `gpt-5.5`, so the new runtime was started with the old one's model
    # name. `model=None` means the caller had nothing to say and the value
    # stays; `model=""` means it does not belong here any more (SYRD-115).
    if model is not None:
        entry.pop("model", None)
        if model:
            entry["model"] = model
    if not runtime_catalog.runtime_takes_effort(runtime):
        # agy drops an effort level before it reaches the command line, so
        # one recorded for the runtime being left is now noise at best.
        entry.pop("effort", None)


def projected_live_commands(entry: Mapping[str, Any]) -> set[str]:
    """What the start verifier will accept for this entry -- its own rule, applied."""
    live = entry.get("live_commands")
    cli = entry.get("cli") if isinstance(entry.get("cli"), list) else []
    configured = [str(c) for c in live] if isinstance(live, list) and live else [str(c) for c in cli[:1]]
    return {team_launcher._command_name(c) for c in configured if team_launcher._command_name(c)}


def _write_runtime_projection(
    config: team_launcher.ProjectConfig,
    *,
    config_path: Path,
    role_name: str,
    runtime: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    model: str | None = None,
) -> team_launcher.ProjectConfig:
    """Point the launcher config at the new runtime, leaving everything else."""
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    roles = raw.get("roles")
    if not isinstance(roles, list):
        raise RoleRuntimeRefusal(f"switchyard: {config_path} must define a roles list")
    for entry in roles:
        if not isinstance(entry, dict) or entry.get("role") != role_name:
            continue
        project_role_runtime(entry, runtime=runtime, model=model)
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
        session_dir=team_launcher.role_session_dir(config, role),
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
    """Replace the role's session, if it has one, with the configured runtime.

    A role that is not running stays not running. Changing a runtime is a
    configuration change; starting a role nobody asked to start would alter the
    running shape of the team as a side effect, and an operator who wanted it up
    launches the project. It will come up under the new runtime when it next
    does.

    Either way the recorded resume id is cleared: it belongs to the runtime being
    left behind, and a stopped role that kept it would have the old CLI's session
    handed to a new CLI that cannot read it at the next launch.

    ``on_stopped`` fires between the stop and the start. A stop that is not
    recorded the moment it happens is invisible to rollback if the start then
    fails, which leaves the role down while the command reports the switch
    cleanly undone.

    Returns whether a live session was actually replaced, which is what tells a
    later reader configured state from live state.
    """
    role = team_launcher._role_by_name(config, role_name)
    was_live = _session_is_live(role, runner=runner)
    if was_live:
        kill = runner(team_launcher.tmux_kill_session_args(role))
        if kill.returncode != 0:
            raise RuntimeError(f"could not stop {role.tmux_session} (exit {kill.returncode})")
        if on_stopped is not None:
            on_stopped()
    team_launcher.clear_session_record_for_role(
        role, team_launcher.role_session_dir(config, role)
    )
    team_launcher.clear_pane_idle_state_for_role(role, pane_state_dir=pane_state_dir)
    if not was_live:
        return False
    _start_and_prove(role, config=config, pane_state_dir=pane_state_dir, runner=runner, start=start)
    return True


def _start_and_prove(
    role: team_launcher.RoleConfig,
    *,
    config: team_launcher.ProjectConfig,
    pane_state_dir: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    start: Callable[..., int] = _default_start,
) -> None:
    """Start the role and confirm it is really up.

    A start's exit code says the launcher was happy, not that a session exists.
    Trusting it is how a blank pane gets reported as a success.
    """
    result = start(role, config=config, pane_state_dir=pane_state_dir, runner=runner)
    if result != 0:
        raise RuntimeError(f"{role.role} did not come up under {team_launcher._role_cli_name(role)} (exit {result})")
    if not _session_is_live(role, runner=runner):
        raise RuntimeError(f"{role.role} reported a successful start but left no live session")


def journal_path_for(config: team_launcher.ProjectConfig, *, config_path: Path, role_name: str) -> Path:
    return team_launcher.default_layout_output_path(config, config_path=config_path).parent / (
        f"role-runtime-{role_name}.journal.json"
    )


def _journal_role(journal: RuntimeJournal, config_path: Path):
    """The launcher's config and the journal's role, as the config says now."""
    config = team_launcher.load_project_config(journal.project, config_path)
    return config, team_launcher._role_by_name(config, journal.role)


def _restore_projection(journal: RuntimeJournal) -> None:
    Path(journal.config_path).write_text(journal.previous_config_bytes, encoding="utf-8")


def _observe(
    journal: RuntimeJournal,
    *,
    config_path: Path,
    read_board: Callable[[], tuple[dict[str, Any], int]],
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> dict[str, Any]:
    """What the board, the launcher config and the worker say right now.

    Read, one source at a time, so a source that cannot be read is recorded as
    unknown and the others are still reported. Nothing here changes anything.
    """
    observed: dict[str, Any] = {}
    try:
        document, revision = read_board()
        entry = next(
            (item for item in (document or {}).get("roles") or [] if item.get("name") == journal.role),
            None,
        )
        observed["board_revision"] = revision
        observed["board_runtime"] = (entry or {}).get("runtime")
    except Exception as exc:  # noqa: BLE001 - an unreadable board is a finding, not a crash
        observed["board_error"] = f"{type(exc).__name__}: {exc}"
    role = None
    try:
        raw = json.loads(Path(journal.config_path).read_text(encoding="utf-8"))
        entry = next(item for item in raw.get("roles") or [] if item.get("role") == journal.role)
        cli = entry.get("cli") if isinstance(entry.get("cli"), list) else []
        observed["launcher_runtime"] = team_launcher._command_name(str(cli[0])) if cli else None
        observed["launcher_live_commands"] = entry.get("live_commands")
        _config, role = _journal_role(journal, config_path)
    except Exception as exc:  # noqa: BLE001
        observed["launcher_error"] = f"{type(exc).__name__}: {exc}"
    if role is not None:
        try:
            live = _session_is_live(role, runner=runner)
            observed["worker_live"] = live
            if live:
                shown = runner(
                    tmux_current_command_args(role),
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                )
                observed["worker_command"] = str(shown.stdout or "").strip() if shown.returncode == 0 else None
                # The start check itself, not a name comparison: it accepts a
                # wrapper whose process tree runs a declared command. A worker
                # it rejects, and one nothing could be read from, are not the
                # same finding, and neither is a healthy worker.
                if team_launcher.live_command_matches_role(role, runner=runner):
                    observed["worker_check"] = "passes"
                else:
                    observed["worker_check"] = "fails" if observed["worker_command"] else "unknown"
        except Exception as exc:  # noqa: BLE001
            observed["worker_error"] = f"{type(exc).__name__}: {exc}"
    return observed


def _recovery_for(journal: RuntimeJournal, observed: Mapping[str, Any]) -> str:
    """Guidance consistent with what was observed, naming only commands that run."""
    project, role = journal.project, journal.role
    board = observed.get("board_runtime")
    launcher = observed.get("launcher_runtime")
    facts = []
    facts.append(
        f"the board declares {role} as {board} at workflow revision {observed.get('board_revision')}"
        if "board_error" not in observed
        else f"the board could not be read ({observed['board_error']})"
    )
    facts.append(
        f"the launcher config names {launcher} (live_commands {observed.get('launcher_live_commands')})"
        if "launcher_error" not in observed
        else f"the launcher config could not be read ({observed['launcher_error']})"
    )
    check = observed.get("worker_check")
    if observed.get("worker_live") is False:
        facts.append("it has no live session")
    elif observed.get("worker_live") is True:
        shows = observed.get("worker_command") or "a command that could not be read"
        against = f" for {launcher}" if launcher else ""
        verdict = {
            "passes": f"it passes the start check{against}",
            "fails": f"it does not pass the start check{against}",
        }.get(check, "whether it passes the start check could not be decided"
                     + (f" ({observed['worker_error']})" if "worker_error" in observed else ""))
        facts.append(f"its session is live and shows {shows}; {verdict}")
    else:
        facts.append(f"its session could not be checked ({observed.get('worker_error', 'no role to check')})")
    stated = (
        f"The switch from {journal.previous_runtime} to {journal.requested_runtime} did not finish, and its "
        f"rollback stopped at the step that failed without changing anything after it. Now "
        + "; ".join(facts) + "."
    )
    if "board_error" in observed or "launcher_error" in observed or not board or not launcher:
        return stated + f" Read both before acting; the prior state is in this journal."
    if board == launcher:
        if observed.get("worker_live") is False:
            return stated + f" They agree; bring the worker up with `switchyard present {project} recover {role}`."
        if observed.get("worker_live") is True and check == "passes":
            return stated + " They agree, and the worker passes its start check; nothing needs repairing."
        look = f"`switchyard attach {project} {role}` shows what its session is running"
        if observed.get("worker_live") is True and check == "fails":
            # Recovery attaches to a live session rather than replacing it, so
            # it is not a repair for a worker the start check rejects.
            return stated + (
                f" They agree, but the live session is not a healthy {launcher} worker. Look before acting: "
                f"{look}; `switchyard present {project} recover {role}` would attach to that session, not "
                f"replace it, so it only helps once the session has been stopped."
            )
        return stated + (
            f" They agree, but the worker's health is unverified. Observe it before acting: {look}, and "
            f"if it has no live session, bring it up with `switchyard present {project} recover {role}`."
        )
    # They disagree. Aligning the launcher with the board is one supported
    # switch, and it runs because the launcher does not already name it.
    bring_up = (
        f" A stopped worker stays stopped; bring it up with `switchyard present {project} recover {role}`."
        if observed.get("worker_live") is False
        else ""
    )
    return stated + (
        f" They disagree. `switchyard set-role-runtime {project} {role} --cli {board}` makes the launcher "
        f"follow the board (it restarts the worker if one is running); to end on {launcher} instead, run "
        f"`switchyard set-role-runtime {project} {role} --cli {launcher}` after it." + bring_up
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
    read_board: Callable[[], tuple[dict[str, Any], int]] | None = None,
) -> str:
    """Undo exactly the steps that were applied: board, projection, then worker.

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

    # The board's declaration first, then the launcher's projection, then the
    # worker -- and each only once the one before it holds. The projection used
    # to be restored first; when the workflow undo was then refused, the config
    # named the old runtime while the board still declared the new one, and
    # the old worker's restart was refused against that mismatch. MEFP Audit
    # was left with no session at all (SYRD-485).
    workflow_restored = True
    if "workflow" in applied:
        workflow_restored = attempt(
            "workflow",
            lambda: client.configure_workflow(
                journal.previous_workflow_document,
                expected_revision=journal.applied_workflow_revision,
                dry_run=False,
            ),
        )
    def stop_here() -> str:
        # Nothing after a failed step runs, and what is left is described from
        # what the three sources actually show -- never from what was asked for.
        # The board may have been moved by another writer, and the projection
        # may never have been written at all.
        journal.rollback_problems = list(problems)
        journal.observed = (
            _observe(journal, config_path=config_path, read_board=read_board, runner=runner)
            if read_board is not None
            else {"board_error": "no board reader was available"}
        )
        journal.recovery = _recovery_for(journal, journal.observed)
        return "; ".join(problems)

    if not workflow_restored:
        # The board did not take the undo, so neither the projection nor the
        # worker is rolled back: restoring either would put them against a
        # board that may declare anything. An original worker the switch never
        # stopped is left exactly as it is.
        return stop_here()
    if "projection" in applied and not attempt("projection", lambda: _restore_projection(journal)):
        # The worker would be started from whatever the config says now, which
        # is still the switch's projection. Stop before touching it.
        return stop_here()

    restored_worker = True
    if "worker_stopped" in applied:
        # The role is down right now. Bringing it back is the whole point of the
        # rollback, so a clean report is only honest once its session is live.
        def restart_previous() -> None:
            # Unlike the forward path, this is not "replace it if it is running":
            # the journal records that it *was* running, and bringing it back is
            # the whole point of the rollback.
            config, role = _journal_role(journal, config_path)
            # The original session was stopped by this switch, so any session
            # under the role's name now is the attempt being undone -- live even
            # when its check said otherwise (SYRD-447). Starting "attach or
            # start" over it would call the old runtime restored while the new
            # one kept the pane.
            if _session_is_live(role, runner=runner):
                kill = runner(team_launcher.tmux_kill_session_args(role))
                if kill.returncode != 0:
                    raise RuntimeError(
                        f"could not stop the failed {journal.requested_runtime} attempt in "
                        f"{role.tmux_session} (exit {kill.returncode})"
                    )
            team_launcher.clear_session_record_for_role(
                role, team_launcher.role_session_dir(config, role)
            )
            _start_and_prove(
                role, config=config, pane_state_dir=pane_state_dir, runner=runner, start=start
            )

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
    #: The model the role should run on the NEW runtime. None leaves whatever
    #: is configured; "" drops it, which is what a move between runtimes means
    #: for a model name that belonged to the one being left (SYRD-115).
    model: str | None = None,
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
        model=model,
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
            previous_board_runtime=checks.board_runtime or "",
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
    if checks.board_runtime is not None and checks.board_runtime != checks.current_runtime:
        # Said before anything else, because "from codex to codex" alone reads
        # as nothing to do: the board is the half that moves (SYRD-525).
        print_func(
            f"switchyard: the board declares {role_name} as {checks.board_runtime} while the launcher "
            f"runs {checks.current_runtime}; this switch sets both to {runtime}"
        )
    if dry_run:
        return RuntimeSwitchResult(
            project=config.project,
            role=role_name,
            previous_runtime=checks.current_runtime,
            previous_board_runtime=checks.board_runtime or "",
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
        applied = board.configure_workflow(proposed, expected_revision=checks.workflow_revision, dry_run=False)
        journal.applied_workflow_revision = int((applied or {}).get("revision") or 0)
        journal.record("workflow")
        journal.write(journal_path)

        updated = _write_runtime_projection(
            config, config_path=config_path, role_name=role_name, runtime=runtime,
            runner=runner, model=model,
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
        if live_changed and presentation_controller.presentation_enabled(updated, config_path=config_path):
            reconnected = presentation_controller.reconnect_role_slots(
                updated, config_path=config_path, role_name=role_name, runner=runner
            )
            journal.record("slots")
            journal.write(journal_path)
    except Exception as exc:  # noqa: BLE001 - a half-applied switch must not look like success
        print_func(f"switchyard: {role_name} runtime switch failed: {exc}")
        read_workflow = workflow_reader or workflow_document
        remaining = _rollback(
            journal,
            config_path=config_path,
            pane_state_dir=state_dir,
            client=board,
            runner=runner,
            start=start,
            print_func=print_func,
            read_board=lambda: read_workflow(board_url=config.board_url or DEFAULT_BOARD_URL),
        )
        if remaining:
            if not journal.rollback_problems:
                journal.rollback_problems = remaining.split("; ")
            journal.write(journal_path)
            recovery = f" {journal.recovery}" if journal.recovery else ""
            raise RoleRuntimeRefusal(
                f"switchyard: {role_name} is left between runtimes and needs an operator: {remaining}.{recovery} "
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
        previous_board_runtime=checks.board_runtime or "",
        runtime=runtime,
        configured_changed=True,
        live_session_changed=live_changed,
        reconnected_slots=reconnected,
        forced=force,
        reason=reason,
        journal_path=str(journal_path),
    )
