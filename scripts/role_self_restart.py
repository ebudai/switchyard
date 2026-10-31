"""A lifecycle loop that would end the caller's own role session hands that role to a successor first (SYRD-713).

SAPI's Director restarted itself for an upgrade and did not come back: the loop that ended every stale session ended
its own, and nothing outside that pane was left to start the Director again. The board kept the dead process
registered as the Director, and the one process ordinarily allowed to recover the Director's pane was the one gone.

- **Whose pane.** `own_pane_role` decides, from the kernel alone, whether this process runs inside the pane of one of
  the roles a loop is about to end: the role's tmux pane process must be this process's own pane root
  (`peer_identity.session_identity`), with the same start time. No role, pid, target or session id the caller names
  enters it.
- **The handoff.** Such a role is ended last, after every other role, and never in-process. Before the launch stops
  or starts anything, `arm_for_launch` arms a successor for it -- or the launch stops there, having changed nothing.
  At its end the launch releases the successor (or cancels it, if the role turned out not to need a restart); a
  launch that ends any other way closes the pipe without a word, and the successor ends nothing. `arm_successor`
  starts an executor outside the pane -- its own session, no controlling terminal -- and tells it, over an inherited
  pipe it reads once, what it was asked: this project, this role, its recorded provider session, the predecessor's
  and the caller's pid and start time, and a transaction id. What it says is not the authority. The authority is the
  kernel's: the executor acts only if its own parent is exactly the named caller and that caller runs inside the
  named predecessor's pane, the role's pane, so only a command already running in a role's own pane can have a
  successor end that pane. A process elsewhere that feeds the entry point the same words is refused. The executor
  holds the role's successor lock for its whole life, so a second lifecycle request for the same role is refused,
  and it says it is armed before the caller goes on.
- **The conversation.** A role is handed over only with a recorded conversation the start path will resume. The
  executor checks it is still the recorded one, and still resumable, before it arms and again just before it ends
  the pane; otherwise nothing is ended.
- **The successor.** The executor proves the predecessor is still exactly that pane, ends its session, waits for that
  process to be gone, starts the role through the ordinary start path, and waits for the board to register a live
  process other than the predecessor, recording that same conversation and running the provider's resume of it.
  Only then is the transaction complete. A failed start is retried once; a role still not back is reported stopped, with the one action that
  recovers it, `switchyard recover-display <project>`, said once.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence
from urllib import request as urllib_request

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig, RoleConfig

#: How long the caller waits for the executor to say it is armed, the executor for the caller to finish, and for its
#: predecessor to be gone.
ARMED_TIMEOUT_SECONDS = 20.0
CALLER_FINISH_TIMEOUT_SECONDS = 120.0  # the rest of the caller's command -- a launch's report -- runs meanwhile
PREDECESSOR_GONE_TIMEOUT_SECONDS = 30.0
#: How long an armed executor waits for the launch to release it; the launch's own exit ends the wait sooner.
RELEASE_TIMEOUT_SECONDS = 3600.0
START_ATTEMPTS = 2


@dataclass(frozen=True)
class Handoff:
    """Everything one successor may do, and nothing else."""

    project: str
    role: str
    config_path: str
    provider_session: str
    predecessor_pid: int
    predecessor_start_time: int
    #: The command that handed over, in the predecessor's pane: let finish (boundedly) before the pane is ended.
    caller_pid: int
    caller_start_time: int
    transaction: str


def caller_pane_root(pid: int | None = None, *, proc_root: Path | None = None) -> Any:
    """The tmux pane root among this process's (or `pid`'s) ancestors, from the kernel, or None: the one seam every
    question of "whose pane is this" starts from."""
    from scripts.ticket_board import peer_identity

    return peer_identity.session_identity(os.getpid() if pid is None else pid,
                                          proc_root=proc_root or peer_identity.PROC_ROOT)


def own_pane_role(
    config: ProjectConfig,
    roles: Sequence[RoleConfig],
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    pid: int | None = None,
    proc_root: Path | None = None,
) -> RoleConfig | None:
    """The role among `roles` whose pane this process runs in, by kernel ancestry and start time, or None."""
    from scripts import team_launcher as launcher
    from scripts.ticket_board import peer_identity

    # No environment variable enters this: a command in a role's pane can clear or change any of them. A process with
    # no tmux pane root among its ancestors is in no pane, and then nothing is asked of tmux.
    root = proc_root or peer_identity.PROC_ROOT
    own = caller_pane_root(pid, proc_root=root)
    if own is None:
        return None
    for role in roles:
        pane = launcher.pane_pid_for_role(role, runner=launcher.role_process_runner_for(config, role, runner=runner))
        if pane != own.pid:
            continue
        process = peer_identity.read_process(pane, proc_root=root)
        if process is not None and process.start_time == own.start_time:
            return role
    return None


def split_own_stale(
    config: ProjectConfig,
    running: Sequence[RoleConfig],
    *,
    owner_home: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> tuple[list[RoleConfig], RoleConfig | None, RoleConfig | None]:
    """(the other running roles, the caller's own running role, that role again if its runtime is stale).

    The stale-runtime drop ends every role it is given; the caller's own is kept out of it, kept running, and -- when
    it is stale too -- handed to a successor once the launch is done.
    """
    from scripts import team_launcher as launcher

    own = own_pane_role(config, running, runner=runner)
    if own is None:
        return list(running), None, None
    others = [role for role in running if role.role != own.role]
    stale = launcher.roles_with_stale_provider_runtime(config, [own], owner_home=owner_home)
    return others, own, own if stale else None


def last(roles: Sequence[RoleConfig], own: RoleConfig | None) -> list[RoleConfig]:
    """`roles` in their order, with the caller's own role moved to the end."""
    if own is None:
        return list(roles)
    return [role for role in roles if role.role != own.role] + [role for role in roles if role.role == own.role]


def outcome_path(config: ProjectConfig, role: RoleConfig) -> Path:
    from scripts import team_launcher as launcher

    return launcher.role_pane_state_dir(config, role, launcher.DEFAULT_PANE_STATE_DIR) / f"{role.role}.successor.json"


def lock_path(config: ProjectConfig, role: RoleConfig) -> Path:
    return outcome_path(config, role).with_suffix(".lock")


@dataclass
class Successor:
    """An armed executor, waiting for this command to release it or cancel it."""

    role: str
    transaction: str
    child: Any
    log: Path
    record: Path


#: This process's armed successors, by (project, role): armed before a launch changes anything, released at its end.
_ARMED: dict[tuple[str, str], Successor] = {}


def _spawn(*args: Any, **kwargs: Any) -> Any:
    return subprocess.Popen(*args, **kwargs)


def arm_for_launch(
    config: ProjectConfig,
    *,
    mode: str,
    config_path: Path,
    owner_home: Path | None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> str:
    """Before a launch changes anything: arm the successor of the caller's own role if the launch would end it.

    A reload ends every role; attach-or-start ends a role whose runtime is stale. '' when nothing needs arming or a
    successor is armed; otherwise why not, and the launch must stop before it stops or starts anything.
    """
    from scripts import team_launcher as launcher

    if mode not in {"attach-or-start", "reload"}:
        return ""
    # The kernel first: a process in no tmux pane runs in no role's pane, and nothing of the project is read.
    if caller_pane_root() is None:
        return ""
    own = own_pane_role(config, config.roles, runner=runner)
    if own is None:
        return ""
    if mode == "attach-or-start":
        home = owner_home or launcher._owner_home_for_auth(config.run_as_user or launcher.current_user_name())
        if not launcher.roles_with_stale_provider_runtime(config, [own], owner_home=home):
            return ""
    return arm_successor(config, own, config_path=config_path, runner=runner, print_func=print_func)


def unarmed_problem(config: ProjectConfig, role: RoleConfig) -> str:
    """'' when `role` -- the caller's own, which this launch would end -- has an armed successor, else the refusal."""
    if (config.project, role.role) in _ARMED:
        return ""
    return (f"team-launcher: refusing to go on: {role.role} is this command's own pane and needs a restart, but no "
            f"successor was armed for it before the launch began; nothing more was stopped or started. Run the "
            f"launch again")


def arm_successor(
    config: ProjectConfig,
    role: RoleConfig,
    *,
    config_path: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    spawn: Callable[..., Any] | None = None,
    pid: int | None = None,
    proc_root: Path | None = None,
    print_func: Callable[[str], None] = print,
) -> str:
    """Arm a successor for `role` -- the caller's own -- that waits to be released; '' once armed, else why not.

    On anything but '' nothing has been stopped, and no executor is left waiting.
    """
    from scripts import team_launcher as launcher
    from scripts.ticket_board import peer_identity

    root = proc_root or peer_identity.PROC_ROOT
    own = peer_identity.session_identity(os.getpid() if pid is None else pid, proc_root=root)
    caller = peer_identity.read_process(os.getpid() if pid is None else pid, proc_root=root)
    if own is None or caller is None or own_pane_role(config, [role], runner=runner, pid=pid, proc_root=root) is None:
        return f"{role.role} is not this process's own pane; nothing was handed over"
    conversation = launcher.session_id_for_role(role, launcher.role_session_dir(config, role))
    problem = _conversation_problem(config, role, conversation)
    if problem:
        return f"{role.role} cannot be restarted on its own conversation ({problem}); nothing was handed over"
    handoff = Handoff(
        project=config.project,
        role=role.role,
        config_path=str(config_path),
        provider_session=conversation,
        predecessor_pid=own.pid,
        predecessor_start_time=own.start_time,
        caller_pid=caller.pid,
        caller_start_time=caller.start_time,
        transaction=secrets.token_hex(16),
    )
    log = outcome_path(config, role).with_suffix(".log")
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(log, "ab") as sink:
            child = (spawn or _spawn)(
                [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "successor"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sink, cwd="/",
                start_new_session=True, close_fds=True,
            )
    except OSError as exc:
        return f"the successor executor for {role.role} could not be started ({exc}); nothing was stopped"
    # The handoff line, then the pipe stays open: the executor waits on it for "release" or "cancel", and reads its
    # closing without either -- this command ending early, however it ends -- as "abort, end nothing".
    try:
        child.stdin.write(json.dumps(asdict(handoff)).encode("utf-8") + b"\n")
        child.stdin.flush()
        armed = _read_line(child.stdout, ARMED_TIMEOUT_SECONDS)
    except OSError as exc:
        armed = f"handoff not given ({exc})"
    if armed != f"armed {handoff.transaction}":
        _discard(child)
        return (f"the successor executor for {role.role} did not arm ({armed or 'no answer'}); nothing was stopped. "
                f"See {log}")
    _ARMED[(config.project, role.role)] = Successor(role.role, handoff.transaction, child, log, outcome_path(config, role))
    print_func(f"switchyard: {role.role} is this command's own pane: successor transaction {handoff.transaction} is "
               "armed and waits for every other role to be handled first")
    return ""


def _discard(child: Any) -> None:
    """End an executor that did not arm. It has ended nothing: it ends a pane only once released."""
    try:
        child.stdin.close()
    except OSError:
        pass
    try:
        child.kill()
        child.wait(timeout=5)
    except (OSError, AttributeError, subprocess.TimeoutExpired):
        pass


def release_armed(
    config: ProjectConfig,
    restart: RoleConfig | None,
    *,
    print_func: Callable[[str], None] = print,
) -> str:
    """At the launch's end: release the armed successor of `restart`, cancel any other; '' or why it failed."""
    problems: list[str] = []
    for key in [key for key in _ARMED if key[0] == config.project]:
        successor = _ARMED.pop(key)
        go = restart is not None and restart.role == successor.role
        try:
            successor.child.stdin.write(f"{'release' if go else 'cancel'} {successor.transaction}\n".encode("utf-8"))
            successor.child.stdin.close()
        except OSError as exc:
            problems.append(f"the successor for {successor.role} could not be released ({exc}); "
                            f"{successor.role} was left running")
            continue
        if go:
            print_func(f"switchyard: {successor.role} is handed to successor transaction {successor.transaction}, "
                       f"which ends it, starts it again and records the outcome in {successor.record}")
    return "; ".join(problems)


def _read_line(stream: Any, timeout: float) -> str:
    import select

    deadline = time.monotonic() + timeout
    data = b""
    while time.monotonic() < deadline and not data.endswith(b"\n"):
        ready, _, _ = select.select([stream], [], [], max(0.0, deadline - time.monotonic()))
        if not ready:
            break
        chunk = os.read(stream.fileno(), 256)
        if not chunk:
            break
        data += chunk
    return data.decode("utf-8", "replace").strip()


def _conversation_problem(config: ProjectConfig, role: RoleConfig, expected: str) -> str:
    """'' when `expected` is the role's recorded conversation and the start path will resume it, else why not."""
    from scripts import team_launcher as launcher

    session_dir = launcher.role_session_dir(config, role)
    if not expected:
        return "no conversation is recorded for it"
    recorded = launcher.session_id_for_role(role, session_dir)
    if recorded != expected:
        return f"its recorded conversation is now {recorded or 'none'}, not {expected}"
    resumable, why = launcher._resume_preflight_allows_attempt(role, expected, session_dir=session_dir)
    return "" if resumable else (why or f"conversation {expected} cannot be resumed")


def _resumes(argvs: Sequence[Sequence[str]], resume: Sequence[str]) -> bool:
    """Whether some process's argv carries `resume` -- the provider's resume of one conversation -- in order."""
    width = len(resume)
    return bool(width) and any(list(argv[i:i + width]) == list(resume)
                               for argv in argvs for i in range(len(argv) - width + 1))


def _board_assignment(config: ProjectConfig, role: str, *, opener: Callable[..., Any] = urllib_request.urlopen) -> dict:
    """The board's current assignment for `role` -- live processes only -- or {}."""
    try:
        with opener(f"{config.board_url.rstrip('/')}/api/runtime-assignments/{role}", timeout=10) as response:
            payload = json.load(response)
    except Exception:  # absent (404) and unreachable are both "not registered yet"
        return {}
    assignment = payload.get("assignment") if isinstance(payload, dict) else None
    return assignment if isinstance(assignment, dict) else {}


def run_successor(
    handoff: Handoff,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    opener: Callable[..., Any] = urllib_request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    proc_root: Path | None = None,
    armed: Callable[[], None] = lambda: None,
    released: Callable[[], str] = lambda: "",
    print_func: Callable[[str], None] = print,
    pid: int | None = None,
) -> int:
    """The executor's whole life: prove, arm, end, start, verify, record. 0 only for a verified successor.

    `pid` is this executor's own process (the tests' stand-in for os.getpid()). `released` answers the caller's
    next line: `release <transaction>` once every other role is handled, `cancel <transaction>` when the launch did
    not need the restart after all, and anything else -- the caller gone without either -- ends nothing.
    """
    from scripts import team_launcher as launcher
    from scripts.role_command import _resume_args_for_role
    from scripts.ticket_board import peer_identity

    root = proc_root or peer_identity.PROC_ROOT
    config = launcher.load_project_config(handoff.project, Path(handoff.config_path))
    if config.project != handoff.project:
        print_func(f"switchyard: refused: {handoff.config_path} is {config.project}'s, not {handoff.project}'s")
        return 2
    role = launcher._role_by_name(config, handoff.role)
    if role is None:
        print_func(f"switchyard: refused: {handoff.project} has no role {handoff.role}")
        return 2
    predecessor = peer_identity.SessionIdentity(handoff.predecessor_pid, handoff.predecessor_start_time)
    role_runner = launcher.role_process_runner_for(config, role, runner=runner)
    pane = launcher.pane_pid_for_role(role, runner=role_runner)
    if pane != predecessor.pid or not peer_identity.session_is_live(predecessor, proc_root=root):
        print_func(f"switchyard: refused: {handoff.role}'s pane is not the predecessor this handoff names")
        return 2
    # Provenance, from the kernel: this executor's parent is the named caller, still the same process, and that caller
    # runs inside the predecessor's pane. Its parent cannot be chosen by anything outside that pane, and the caller is
    # still waiting for "armed", so it has not exited and left the executor to be reparented.
    caller = peer_identity.SessionIdentity(handoff.caller_pid, handoff.caller_start_time)
    me = peer_identity.read_process(os.getpid() if pid is None else pid, proc_root=root)
    if (me is None or me.ppid != caller.pid or not peer_identity.session_is_live(caller, proc_root=root)
            or peer_identity.session_identity(caller.pid, proc_root=root) != predecessor):
        print_func(f"switchyard: refused: this executor was not started by a command in {handoff.role}'s own pane")
        return 2
    problem = _conversation_problem(config, role, handoff.provider_session)
    if problem:
        print_func(f"switchyard: refused: {handoff.role} cannot be restarted on its own conversation ({problem})")
        return 2
    lock = open(lock_path(config, role), "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print_func(f"switchyard: refused: another successor already holds {handoff.role}'s restart")
        return 2
    record = outcome_path(config, role)

    def write(state: str, **fields: Any) -> None:
        body = {"transaction": handoff.transaction, "project": handoff.project, "role": handoff.role,
                "predecessor": [predecessor.pid, predecessor.start_time], "state": state, **fields}
        temporary = record.with_name(record.name + ".tmp")
        temporary.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(record)

    write("armed")
    armed()
    word = released()
    if word == f"cancel {handoff.transaction}":
        write("cancelled", ended=False)
        print_func(f"switchyard: {handoff.role} needed no restart after all; transaction {handoff.transaction} "
                   "cancelled and nothing was ended")
        return 0
    if word != f"release {handoff.transaction}":
        write("aborted", reason=f"the launch was not released ({word or 'it ended without a word'})", ended=False)
        print_func(f"switchyard: {handoff.role} is left running: the launch did not release transaction "
                   f"{handoff.transaction}, so nothing was ended")
        return 2
    deadline = monotonic() + CALLER_FINISH_TIMEOUT_SECONDS
    while peer_identity.session_is_live(caller, proc_root=root) and monotonic() < deadline:
        sleep(0.1)
    problem = _conversation_problem(config, role, handoff.provider_session)
    if problem:
        write("refused", reason=problem, ended=False)
        print_func(f"switchyard: refused: {handoff.role} is left running; it cannot be restarted on its own "
                   f"conversation ({problem})")
        return 2
    role_runner(launcher.tmux_kill_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = monotonic() + PREDECESSOR_GONE_TIMEOUT_SECONDS
    while peer_identity.session_is_live(predecessor, proc_root=root) and monotonic() < deadline:
        sleep(0.2)
    if peer_identity.session_is_live(predecessor, proc_root=root):
        write("stopped", reason="the predecessor's process did not end", recover=f"switchyard recover-display {handoff.project}")
        print_func(f"switchyard: {handoff.role} is stopped: its predecessor did not end. "
                   f"Recover it with `switchyard recover-display {handoff.project}`")
        return 1
    reasons: list[str] = []
    for attempt in range(1, START_ATTEMPTS + 1):
        started = runner(
            launcher.pane_command_args(
                config.project, role, config_path=Path(handoff.config_path), mode="attach-or-start",
                script_path=launcher.switchyard_pane_launcher_for(config), skip_launcher_check=True, no_attach=True,
                run_as_user=launcher.role_run_as_user(config, role),
            ),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if started.returncode != 0:
            reasons.append(f"start {attempt} exited {started.returncode}")
            continue
        wait_until = monotonic() + launcher.RUNTIME_REGISTRATION_TIMEOUT_SECONDS
        while monotonic() < wait_until:
            current = _board_assignment(config, handoff.role, opener=opener)
            successor = peer_identity.SessionIdentity(int(current.get("process_pid") or 0),
                                                      int(current.get("process_start_time") or 0))
            if current and successor != predecessor and peer_identity.session_is_live(successor, proc_root=root):
                break
            sleep(launcher.RUNTIME_REGISTRATION_POLL_SECONDS)
        else:
            reasons.append(f"start {attempt} registered no live successor")
            continue
        # The successor is live; it is this role's successor only on the same conversation, resumed.
        recorded = launcher.session_id_for_role(role, launcher.role_session_dir(config, role))
        resumed = _resumes(launcher.process_tree_argvs(successor.pid),
                           _resume_args_for_role(role, handoff.provider_session))
        if recorded == handoff.provider_session and resumed:
            write("complete", successor=[successor.pid, successor.start_time], provider_session=recorded)
            print_func(f"switchyard: {handoff.role} is back as process {successor.pid} on conversation {recorded}; "
                       f"transaction {handoff.transaction} complete")
            return 0
        why = (f"its successor, process {successor.pid}, records conversation {recorded or 'none'} and "
               f"{'resumes' if resumed else 'does not resume'} {handoff.provider_session}")
        write("failed", reason=why, successor=[successor.pid, successor.start_time],
              recover=f"switchyard recover-display {handoff.project}")
        print_func(f"switchyard: {handoff.role} did not come back on its own conversation: {why}. "
                   f"Recover it with `switchyard recover-display {handoff.project}`")
        return 1
    write("stopped", reason="; ".join(reasons), recover=f"switchyard recover-display {handoff.project}")
    print_func(f"switchyard: {handoff.role} is stopped ({'; '.join(reasons)}). "
               f"Recover it with `switchyard recover-display {handoff.project}`")
    return 1


def main(argv: Sequence[str]) -> int:
    """`role_self_restart.py successor`: one handoff on stdin, read once; 'armed <transaction>' on stdout, then quiet."""
    if list(argv) != ["successor"]:
        print("usage: role_self_restart.py successor < handoff", file=sys.stderr)
        return 2
    # Both lines are read from fd 0 by the same unbuffered reader, so no buffer holds one back from the other.
    try:
        handoff = Handoff(**json.loads(_read_line(sys.stdin, ARMED_TIMEOUT_SECONDS)))
    except (TypeError, ValueError) as exc:
        print(f"switchyard: refused: not a handoff ({exc})", file=sys.stderr)
        return 2

    def armed() -> None:
        # Said once, then the pipe is closed: nothing more is written on it.
        os.write(1, f"armed {handoff.transaction}\n".encode("utf-8"))
        os.dup2(2, 1)

    def released() -> str:
        word = _read_line(sys.stdin, RELEASE_TIMEOUT_SECONDS)
        os.close(0)
        return word

    return run_successor(handoff, armed=armed, released=released,
                         print_func=lambda line: print(line, file=sys.stderr, flush=True))


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    raise SystemExit(main(sys.argv[1:]))
