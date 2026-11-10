"""`switchyard restart-role <project> <role>`: restart exactly one role's pane, safely (SYRD-750).

An upgrade says panes must be restarted to pick up new hooks and launcher changes, a role-prompt change takes effect
at a role's next start, and an upgrade's report link reaches only panes launched after it. Until now nothing restarted
one role: `reconcile-role-models` restarts only a role whose model drifted, and `stop`/`start` take the whole project
down, the Director included. A Director had to improvise with tmux, or hand the restart to the User.

This restarts one configured role through the launcher's own single-role reload, which ends the old pane process and
starts the replacement with the current release's hooks, the role's current prompt and the environment the launcher
gives it now, and resumes the role's recorded conversation by its own ephemerality and context rules.

Who may ask is decided from the kernel and the board, never from anything the caller says (Audit on dc37fe16): a
process inside the pane of the board's control role -- the role holding its control capabilities -- whose registration
is that pane's own process; or the operator, as root (`sudo switchyard restart-role ...`). Root is never served as
root: it re-runs the command as the project's owner through runuser, and that process proves from /proc that its
parent is exactly root's runuser of exactly this command before anything else. A sibling role's pane, or a process of
the account in no pane, is refused.

Refused, with nothing changed:
- a role not in the project's configuration, or not an active role of the board's workflow;
- a board that does not answer (the project is stopped: `switchyard start`);
- a live pane whose process is not the one the board registered for the role, or that the board has not registered;
- a live turn, or a command still running from the pane (the launcher's fail-closed activity gate). For the caller's
  own pane the turn running this command is that busy state, and the successor waits it out (Audit on 007a5f84), so
  there the gate is no refusal; any other live process in the pane is -- one that is neither an ancestor of this
  command nor its own child, such as a background command the pane's end would kill;
- a running execution lease of the role's;
- a role whose provider is held (a quota hold, or a safety hold on its account): a restart's resume is a request;
- a ticket the role holds, unless `--while-holding <ticket>` names it: its conversation resumes, but the caller must
  know it is restarting a role in the middle of that ticket;
- a second restart of the same role while one, or its successor, is in flight: both hold the role's one lifecycle
  lock, which the command hands to the successor for the successor's whole transaction.

A role whose pane is gone while the board is up is started -- recovered -- the same way.

The caller's own pane (the Director restarting itself) is never ended in-process: SYRD-713's successor is armed for
it, from the kernel's own view of whose pane this is, and released when this command exits; the successor ends the
pane, starts it again, and waits for the board to register the new process on the same conversation. If no successor
can be armed, the command prints the one command for the operator to run from outside the pane, and ends nothing.

After a restart it proves, from the kernel and the board, that every process of the old pane has ended -- gone, its
pid reused, or a zombie whose exit status alone remains -- within a bound, that the pane runs a new process the board
registered, and
that the board variables the launcher gives the role now reached it -- a report link provisioned since the last start
among them. Reopening a presentation window is not a pane restart, and nothing here reports it as one.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from scripts import launch_environment

BOARD_TIMEOUT_SECONDS = 5.0
REGISTER_TIMEOUT_SECONDS = 90.0
POLL_SECONDS = 1.0
ENVIRONMENT_POLLS = 15
OLD_GONE_SECONDS = 20.0  # tmux hangs the old pane up; its processes are given this long to end


class Refused(Exception):
    """A restart that must not happen: said once, nothing changed."""


def _get(url: str, opener: Callable[..., Any]) -> Any:
    with opener(url, timeout=BOARD_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def board_facts(config: Any, role: str, *, opener: Callable[..., Any] = urllib_request.urlopen) -> dict[str, Any]:
    """What the board says about the role: its workflow entry, registration, held ticket, leases and holds."""
    base = str(config.board_url).rstrip("/")
    try:
        workflow = _get(f"{base}/api/workflow", opener)
        assignments = _get(f"{base}/api/runtime-assignments", opener)
        reservations = _get(f"{base}/api/reservations", opener)
        leases = _get(f"{base}/api/execution-leases", opener)
        holds = _get(f"{base}/api/provider-holds", opener)
    except (OSError, ValueError) as exc:
        raise Refused(f"{config.project}'s board at {base} did not answer ({exc}); the project may be stopped -- "
                      f"`switchyard start {config.project}` starts it, and every role with it") from None
    document = workflow.get("document") if isinstance(workflow, dict) else None
    declared = {r.get("name"): r for r in ((document or {}).get("roles") or []) if isinstance(r, dict)}
    held = [h for h in (holds.get("holds") or []) if isinstance(h, dict) and h.get("role") == role and h.get("current")]
    held += [{"account": h.get("runtime")} for h in ((holds.get("accounts") or {}).get("held") or [])
             if isinstance(h, dict) and role in (h.get("roles") or [])]
    control = {name for name, r in declared.items() if {"set_manually_controlled", "merge"} <= set(r.get("capabilities") or [])
               and r.get("active", True)} if document else {"director"}
    every = (assignments.get("assignments") or {}) if isinstance(assignments, dict) else {}
    return {
        "control": control,
        "assignments": every,
        "declared": declared.get(role) if document else {"name": role, "active": True},
        "registration": every.get(role),
        "reservation": ((reservations.get("reservations") or {}).get(role) if isinstance(reservations, dict) else None),
        "leases": [lease for lease in (leases.get("leases") or []) if isinstance(lease, dict)
                   and lease.get("role") == role and not lease.get("ended_at")],
        "holds": held,
    }


def _process(pid: int | None, proc_root: Path) -> tuple[int, int] | None:
    from scripts.ticket_board import peer_identity

    if not pid:
        return None
    process = peer_identity.read_process(int(pid), proc_root=proc_root)
    return None if process is None else (process.pid, process.start_time)


def expected_board_environment(config: Any, role: Any) -> dict[str, str]:
    """The TICKET_BOARD_* variables the launcher gives this role's pane now, from its own command."""
    from scripts import team_launcher as launcher

    command = launcher.cli_command_for_role(role, session_dir=launcher.role_session_dir(config, role))
    found: dict[str, str] = {}
    words = iter(command[1:] if command[:1] == ["env"] else [])
    for word in words:  # `env -u NAME ... KEY=VALUE ... program ...`
        if word == "-u":
            next(words, None)
        elif "=" in word and not word.startswith("-"):
            key, value = word.split("=", 1)
            if key.startswith("TICKET_BOARD_"):
                found[key] = value
        else:
            break
    return found


def environment_of(pid: int, proc_root: Path) -> dict[str, str] | None:
    try:
        raw = (proc_root / str(pid) / "environ").read_bytes()
    except OSError:
        return None
    return dict(item.split("=", 1) for item in raw.decode("utf-8", "replace").split("\0") if "=" in item)


def pane_environment(pane_pid: int, role: str, proc_root: Path, *, depth: int = 4) -> dict[str, str]:
    """The environment the role's command runs with in its pane: the pane root's own when it exec'd the command, else
    the nearest descendant's that carries the role's caller variable (a shell such as fish runs it as a child)."""
    level = [pane_pid]
    for _ in range(depth + 1):
        for pid in level:
            env = environment_of(pid, proc_root) or {}
            if env.get("TICKET_BOARD_CALLER_ROLE") == role:
                return env
        children = []
        for entry in proc_root.iterdir():
            if entry.name.isdigit():
                try:
                    stat = (entry / "stat").read_text()
                    if int(stat[stat.rindex(")") + 2:].split()[1]) in level:
                        children.append(int(entry.name))
                except (OSError, ValueError, IndexError):
                    continue
        if not children:
            break
        level = children
    return environment_of(pane_pid, proc_root) or {}


def _table(proc_root: Path) -> dict[int, tuple[int, int, str]]:
    """Every process the kernel shows: pid -> (parent, start time, state)."""
    rows = {int(entry.name): _table_row(int(entry.name), proc_root) for entry in proc_root.iterdir() if entry.name.isdigit()}
    return {pid: row for pid, row in rows.items() if row is not None}


def _live(state: str) -> bool:
    return state not in {"Z", "X"}  # a zombie has ended: only its exit status waits for a parent to collect it


def _descendants(table: dict[int, tuple[int, int, str]], top: int) -> set[int]:
    """`top` and every process below it, however deep (Audit on c35705da): the walk ends when no unseen descendant is
    left, which a finite snapshot of the kernel's table guarantees -- each pid is taken once."""
    children: dict[int, list[int]] = {}
    for pid, (ppid, _start, _state) in table.items():
        children.setdefault(ppid, []).append(pid)
    seen, todo = {top}, [top]
    while todo:
        for child in children.get(todo.pop(), ()):
            if child not in seen:
                seen.add(child)
                todo.append(child)
    return seen


def pane_tree(pane_pid: int, proc_root: Path) -> dict[int, int]:
    """The pane's live processes -- its root and every live descendant, at any depth -- as pid -> start time."""
    table = _table(proc_root)
    if pane_pid not in table or not _live(table[pane_pid][2]):
        return {}
    return {pid: table[pid][1] for pid in _descendants(table, pane_pid) if _live(table[pid][2])}


def _me() -> int:
    return os.getpid()


def other_work(pane_pid: int, proc_root: Path) -> list[int]:
    """The pane's live processes that are neither this command nor what runs it (its ancestors -- the pane's CLI and
    the shell its tool started) nor this command's own children: work the pane's end would kill."""
    table = _table(proc_root)
    mine, pid = set(), _me()
    while pid in table and pid not in mine:
        mine.add(pid)
        pid = table[pid][0]
    tree = pane_tree(pane_pid, proc_root)
    below = _descendants(table, _me())
    return sorted(p for p in tree if p not in mine and p not in below)


def ended(pid: int, start: int, proc_root: Path) -> bool:
    """Whether this kernel identity has ended: gone, its pid reused, or a zombie."""
    row = _table_row(pid, proc_root)
    return row is None or row[1] != start or not _live(row[2])


def _table_row(pid: int, proc_root: Path) -> tuple[int, int, str] | None:
    try:
        stat = (proc_root / str(pid) / "stat").read_text()
        fields = stat[stat.rindex(")") + 2:].split()
        return int(fields[1]), int(fields[19]), fields[0]
    except (OSError, ValueError, IndexError):
        return None


def _lock(config: Any, role: Any) -> Any:
    """The role's one lifecycle lock -- the same file SYRD-713's successor holds for its whole transaction -- held for
    this command's life: a second restart of the role, by any path, is refused while either is in flight."""
    from scripts import role_self_restart

    path = role_self_restart.lock_path(config, role)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise Refused(f"another restart of {role.role} is in flight; nothing was changed") from None
    return handle


def operator_argv(config: Any, config_path: Path, role_name: str, while_holding: Sequence[str], dry_run: bool) -> list[str]:
    """What root runs, as the project owner, for the operator: this module's own entry point."""
    import sys

    return (["runuser", "-u", str(config.run_as_user), "--", sys.executable, "-I", "-B", str(Path(__file__).resolve()),
             "operator", config.project, str(config_path), role_name]
            + [arg for ticket in while_holding for arg in ("--while-holding", ticket)] + (["--dry-run"] if dry_run else []))


def root_parent_ran_me(proc_root: Path) -> bool:
    """Whether this process was started by root's `runuser`, running exactly this command: the operator's path. From
    the kernel only -- the parent's real uid and its command line -- which no process of the tenant's account forges."""
    import sys

    parent = os.getppid()
    try:
        status = (proc_root / str(parent) / "status").read_text()
        cmdline = (proc_root / str(parent) / "cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    import pwd

    uid = next((line.split()[1] for line in status.splitlines() if line.startswith("Uid:")), "")
    own = [str(word).encode() for word in getattr(sys, "orig_argv", [])]
    me = pwd.getpwuid(os.getuid()).pw_name.encode()  # the account this process runs as, from the kernel
    return uid == "0" and cmdline[:4] == [b"runuser", b"-u", me, b"--"] and [word for word in cmdline[4:] if word] == own


def caller_authority(config: Any, facts: dict[str, Any], *, runner: Callable[..., Any], proc_root: Path) -> str:
    """'' when this process may restart a role, else why not -- decided from the kernel and the board, never from
    anything the caller says: inside the pane of the board's registered control role (its registration the pane's own
    process), or started by root as the operator (`sudo switchyard restart-role ...`)."""
    from scripts import role_self_restart

    own = role_self_restart.caller_pane_root()
    if own is None:
        if root_parent_ran_me(proc_root):
            return ""
        return ("restart-role is the Director's, from its own pane, or the operator's, as root: "
                f"`sudo switchyard restart-role {config.project} <role>`; this process is in no role's pane")
    caller = role_self_restart.own_pane_role(config, config.roles, runner=runner)
    if caller is None:
        return "this process is in a pane that is not one of this project's roles"
    if caller.role not in facts["control"]:
        return (f"{caller.role} may not restart roles: only the board's control role ({', '.join(sorted(facts['control']))}) "
                f"or the operator may")
    registered = facts["assignments"].get(caller.role) or {}
    if (registered.get("process_pid"), registered.get("process_start_time")) != (own.pid, own.start_time):
        return f"{caller.role}'s pane here is not the process the board registered as {caller.role}"
    return ""


def reconnect_slots(config: Any, *, config_path: Path, role_name: str, runner: Callable[..., Any],
                    print_func: Callable[[str], None]) -> str:
    """Re-attach every display slot that showed the role to its replacement, and prove each shows it live, as
    `present list` reads it (SYRD-752: after a restart the User saw the roles as disconnected). '' when every slot is
    back, or none showed it; else what is not, with the exact command that reconnects it."""
    from scripts import presentation_controller, runtime_handover

    if not presentation_controller.presentation_enabled(config, config_path=config_path):
        return ""
    try:
        slots = tuple(presentation_controller.reconnect_role_slots(config, config_path=config_path,
                                                                   role_name=role_name, runner=runner))
        unbound = runtime_handover.slot_binding_problem(config, config_path=config_path, role_name=role_name,
                                                        slots=slots, runner=runner)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - the pane is back; the display is reported, not hidden
        slots = presentation_controller.slots_showing_role(config, config_path=config_path, role_name=role_name)
        unbound = f"its display slots could not be reconnected ({type(exc).__name__}: {exc})"
    if unbound:
        fixes = "; ".join(f"`switchyard present {config.project} show --slot {slot} {role_name}`" for slot in slots)
        print_func(f"switchyard: {role_name} is back, but {unbound}. Reconnect: {fixes or 'no slot shows it'}")
        return unbound
    if slots:
        print_func(f"switchyard: display slot(s) {', '.join(map(str, slots))} show {role_name} again")
    return ""


def refusals(role: Any, facts: dict[str, Any], *, pane: tuple[int, int] | None, busy: bool,
             while_holding: Sequence[str], others: Sequence[int] = ()) -> list[str]:
    """Every reason not to restart this role now; empty when it may be."""
    out: list[str] = []
    declared = facts["declared"]
    if not declared or not declared.get("active", True):
        out.append(f"{role.role} is not an active role of the board's workflow")
    registration = facts["registration"] or {}
    if pane is not None and not registration.get("process_pid"):
        out.append(f"{role.role}'s pane runs pid {pane[0]} (start {pane[1]}), but the board has no registration for "
                   f"{role.role}: a pane the board has not bound to the role is not restarted blind")
    elif pane is not None and \
            (registration.get("process_pid"), registration.get("process_start_time")) != pane:
        out.append(f"{role.role}'s pane runs pid {pane[0]} (start {pane[1]}), but the board registers pid "
                   f"{registration.get('process_pid')} (start {registration.get('process_start_time')}) as {role.role}: "
                   "a pane that is not the registered process is not restarted blind")
    if busy:
        out.append(f"{role.role} is working -- a live turn, or a command still running from its pane; restart it when "
                   "it is idle")
    if others:
        out.append(f"{role.role}'s pane runs work besides this command (pid {', '.join(map(str, others))}), which its "
                   "restart would end; let it finish first")
    for lease in facts["leases"]:
        out.append(f"{role.role} has a running command under lease {lease.get('id')} "
                   f"({str(lease.get('command') or '')[:80]}); let it finish, or interrupt it first")
    for hold in facts["holds"]:
        what = f"its {hold['account']} account is held after a safety refusal" if "account" in hold else \
            "its provider is held for quota"
        out.append(f"{role.role}'s restart would resume its conversation, a provider request, but {what}")
    reservation = facts["reservation"]
    if isinstance(reservation, dict) and reservation.get("ticket") and reservation["ticket"] not in while_holding:
        out.append(f"{role.role} holds {reservation['ticket']} ({reservation.get('state')}); its conversation resumes "
                   f"after the restart, so name it to go ahead: --while-holding {reservation['ticket']}")
    return out


def _command_program(command: Sequence[str]) -> tuple[str, str]:
    """(the PATH the pane command sets, the program it runs) of an `env -u NAME ... KEY=VALUE ... program ...` line."""
    path, index = "", 0
    if command and command[0] == "env":
        index = 1
        while index < len(command):
            word = command[index]
            if word == "-u":
                index += 2
                continue
            name, sep, value = word.partition("=")
            if sep and name and "/" not in name:
                path = value if name == "PATH" else path
                index += 1
                continue
            break
    return path, (command[index] if index < len(command) else "")


def launch_preflight(config: Any, role: Any, *, session_dir: Path, pane_state_dir: Path,
                     role_runner: Callable[..., Any]) -> list[str]:
    """What would make the replacement's start fail, found before the old pane is ended (SYRD-752): a local model
    whose service is stopped, a working directory that is not one, a pane command that cannot be built, or a program
    it runs that is not on the PATH it runs with -- each as the role's own account sees it."""
    from scripts import team_launcher as launcher
    from scripts.hermes_local_profile import start_refusal

    out: list[str] = []
    refusal = start_refusal(role)
    if refusal:
        out.append(refusal.removeprefix("switchyard: "))
    if role_runner(["test", "-d", str(role.workdir)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        out.append(f"its working directory {role.workdir} is not a directory its account can enter")
    try:
        with launch_environment.applied():
            command = launcher.cli_command_for_role(role, session_dir=session_dir, pane_state_dir=pane_state_dir,
                                                    resume=False, bin_user=config.run_as_user)
    except SystemExit as exc:
        return [*out, f"its pane command cannot be built ({exc})"]
    path, program = _command_program(command)
    for name in dict.fromkeys(p for p in (program, str(role.cli[0])) if p):
        found = role_runner(["env", f"PATH={path}", "sh", "-c", 'command -v -- "$1" >/dev/null 2>&1', "sh", name],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        if not found:
            out.append(f"{name}, which its pane runs, is not on the PATH its pane gets ({path or 'none'})")
    return out


def _await_registration(config: Any, role: str, old: tuple[int, int] | None, *, opener: Callable[..., Any],
                        sleep: Callable[[float], None], clock: Callable[[], float], proc_root: Path,
                        session_alive: Callable[[], bool] = lambda: True) -> dict | None:
    """The board's new registration for the role -- a live process other than `old` -- or None at the deadline, or
    as soon as the role's session is gone: a pane that died starting registers nothing."""
    deadline = clock() + REGISTER_TIMEOUT_SECONDS
    while clock() < deadline:
        if not session_alive():
            return None
        try:
            row = (_get(f"{str(config.board_url).rstrip('/')}/api/runtime-assignments", opener).get("assignments")
                   or {}).get(role) or {}
        except (OSError, ValueError):
            row = {}
        now = (row.get("process_pid"), row.get("process_start_time"))
        if row.get("process_pid") and now != old and _process(row.get("process_pid"), proc_root) == now:
            return row
        sleep(POLL_SECONDS)
    return None


def restart_role_command(
    config: Any,
    *,
    config_path: Path,
    role_name: str,
    while_holding: Sequence[str] = (),
    dry_run: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    opener: Callable[..., Any] = urllib_request.urlopen,
    ensure: Callable[..., int] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    proc_root: Path | None = None,
    print_func: Callable[[str], None] = print,
) -> int:
    """Restart one role's pane; 0 when it is back, registered and current, or handed to its successor."""
    from scripts import role_runtime, role_self_restart
    from scripts import team_launcher as launcher
    from scripts.ticket_board import peer_identity

    root = proc_root or peer_identity.PROC_ROOT
    known = {role.role: role for role in config.roles}
    if role_name not in known:
        raise SystemExit(f"switchyard: {config.project} has no role {role_name}; its roles are "
                         f"{', '.join(sorted(known))}. Nothing was changed.")
    role = known[role_name]
    if os.geteuid() == 0:
        # The operator: never served as root. Run as the project's owner, by root's own runuser, which the owner-side
        # process proves from the kernel before it does anything.
        if not config.run_as_user:
            print_func(f"switchyard: {config.project} records no owner account to restart its roles as. Nothing was changed.")
            return 1
        return runner(operator_argv(config, config_path, role_name, while_holding, dry_run)).returncode
    try:
        lock = _lock(config, role)
    except Refused as exc:
        print_func(f"switchyard: {exc}")
        return 1
    try:
        try:
            facts = board_facts(config, role.role, opener=opener)
        except Refused as exc:
            print_func(f"switchyard: refusing to restart {role.role}: {exc}. Nothing was changed.")
            return 1
        denied = caller_authority(config, facts, runner=runner, proc_root=root)
        if denied:
            print_func(f"switchyard: refusing to restart {role.role}: {denied}. Nothing was changed.")
            return 1
        role_runner = launcher.role_process_runner_for(config, role, runner=runner)
        alive = role_runner(launcher.tmux_has_session_args(role), stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL).returncode == 0
        pane = _process(launcher.pane_pid_for_role(role, runner=role_runner), root) if alive else None
        pane_state_dir = launcher.default_pane_state_dir_for_user(
            config.run_as_user or launcher.current_user_name(), project=config.project)
        own = alive and role_self_restart.own_pane_role(config, [role], runner=runner) is not None
        # The caller's own pane is busy with the very turn that runs this command, which the successor waits out; so
        # there its turn is no refusal, but any other process in the pane is -- the pane's end would kill it.
        busy = alive and not own and role_runtime._is_busy(role, pane_state_dir=pane_state_dir, runner=role_runner)
        others = other_work(pane[0], root) if own and pane else []
        problems = refusals(role, facts, pane=pane, busy=busy, while_holding=while_holding, others=others)
        session_dir = launcher.role_session_dir(config, role)
        if not problems:
            # Its own pane too (Audit on ce2e6d4e): the successor ends the pane before its start can fail, so what
            # that start would fail on is refused here, before a successor is armed or released.
            problems = [f"its replacement could not start: {why}" for why in launch_preflight(
                config, role, session_dir=session_dir, pane_state_dir=pane_state_dir, role_runner=role_runner)]
        if problems:
            for problem in problems:
                print_func(f"switchyard: refusing to restart {role.role}: {problem}")
            print_func("switchyard: nothing was changed.")
            return 1
        conversation = launcher.session_id_for_role(role, session_dir) or "none recorded"
        verb = "start" if not alive else "restart"
        if dry_run:
            print_func(f"switchyard: would {verb} {role.role}" + (" through a successor, as it is this command's own "
                       "pane" if own else "") + f"; its recorded conversation ({conversation}) resumes where its "
                       "rules keep it")
            return 0
        if own:
            # The successor takes the role's lifecycle lock for its whole transaction; it is handed over here, and a
            # restart that takes it in between makes the successor refuse to arm -- nothing is ended either way.
            lock.close()
            problem = role_self_restart.arm_successor(config, role, config_path=config_path, runner=runner,
                                                      print_func=print_func)
            if problem:
                print_func(f"switchyard: {problem}")
                print_func(f"switchyard: {role.role} is this command's own pane and no successor could be armed, so "
                           f"nothing was ended. The operator restarts it from outside the pane: "
                           f"`sudo switchyard restart-role {config.project} {role.role}`")
                return 1
            problem = role_self_restart.release_armed(config, role, print_func=print_func)
            if problem:
                print_func(f"switchyard: {problem}")
                return 1
            print_func(f"switchyard: {role.role}'s pane ends once this command exits, and starts again on its "
                       f"recorded conversation ({conversation}); `switchyard status {config.project}` shows it back")
            return 0
        registered = facts["registration"] or {}
        before = pane_tree(pane[0], root) if pane else {}
        if not pane and registered.get("process_pid"):
            before = {int(registered["process_pid"]): int(registered.get("process_start_time") or 0)}
        def session_alive() -> bool:
            return role_runner(launcher.tmux_has_session_args(role), stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL).returncode == 0

        def launch(mode: str) -> int:
            # From an environment built for it: never the caller's pane identity, nor its agent CLI's nesting
            # variables, whatever this pane carries (SYRD-752).
            with launch_environment.applied():
                return (ensure or launcher.ensure_visible_role_session_for_viewer)(
                    role, mode=mode, session_dir=session_dir, pane_state_dir=pane_state_dir,
                    bin_user=config.run_as_user, runner=role_runner)

        old = pane or ((facts["registration"] or {}).get("process_pid"),
                       (facts["registration"] or {}).get("process_start_time"))
        wanted = expected_board_environment(config, role)

        def came_up(result: int) -> tuple[dict | None, list[str], str]:
            """(its registration, board variables it lacks, why it is not up) after one start."""
            row = _await_registration(config, role.role, old, opener=opener, sleep=sleep, clock=clock,
                                      proc_root=root, session_alive=session_alive) if result == 0 else None
            if row is None:
                return None, [], f"launcher exit {result}" if result else "its pane did not stay up and register"
            stale: list[str] = sorted(wanted)
            for _ in range(ENVIRONMENT_POLLS):  # the pane's command may still be starting under its shell
                found = pane_environment(int(row["process_pid"]), role.role, root)
                stale = sorted(key for key, value in wanted.items() if found.get(key) != value)
                if not stale or not session_alive():
                    break
                sleep(POLL_SECONDS)
            if stale and not session_alive():  # registered, then died starting: not up, which a second start meets
                return None, [], "its pane registered, then did not stay up"
            if not stale:
                # Seen running is not up: a CLI that exits as it starts -- as a nested one did -- is seen for an
                # instant. It must still run as the role after the launcher's own stability window for a start.
                settle = clock() + launcher.DETACHED_SESSION_STABILITY_SECONDS
                while clock() < settle and session_alive():
                    sleep(POLL_SECONDS)
                identity = (row.get("process_pid"), row.get("process_start_time"))
                if not session_alive() or _process(row.get("process_pid"), root) != identity or pane_environment(
                        int(row["process_pid"]), role.role, root).get("TICKET_BOARD_CALLER_ROLE") != role.role:
                    return None, [], "its pane started, then did not stay up"
            return row, stale, ""

        result = launch("reload" if alive else "attach-or-start")
        if result != 0 and alive and before and not any(ended(pid, start, root) for pid, start in before.items()):
            # The launcher refused before it ended anything: every process of the old pane still runs.
            print_func(f"switchyard: refusing to restart {role.role}: the launcher refused it (exit {result}) before "
                       "ending anything. Nothing was changed.")
            return 1
        row, stale, first = came_up(result)
        second = ""
        if row is None:
            # The old pane is ended, or was already gone: one bounded second start, from the same environment.
            row, stale, again = came_up(launch("reload" if session_alive() else "attach-or-start"))
            if row is None:
                tried = f"{first}; a second start: {again}"
                still = sorted(pid for pid, start in before.items() if not ended(pid, start, root)) if alive else []
                if still:
                    print_func(f"switchyard: {role.role} was not restarted: its old pane still runs (pid "
                               f"{', '.join(map(str, still))}), and no replacement registered ({tried}); "
                               f"`switchyard status {config.project}` shows where it stands")
                    return 1
                gone = "its old pane was ended and " if alive else ""
                print_func(f"switchyard: {role.role} is stopped: {gone}its replacement did not come up ({tried}). Its "
                           f"recorded conversation ({conversation}) is kept. Once the cause is fixed, `switchyard "
                           f"restart-role {config.project} {role.role}` starts it again; `switchyard status "
                           f"{config.project}` shows where it stands")
                return 1
            second = f" on a second start ({first} the first time)"
        if stale:
            print_func(f"switchyard: {role.role} came back as pid {row['process_pid']} but without the current "
                       f"{', '.join(stale)}; it was not restarted cleanly")
            return 1
        deadline = clock() + OLD_GONE_SECONDS
        lingering = [pid for pid, start in before.items() if not ended(pid, start, root)]
        while lingering and clock() < deadline:
            sleep(POLL_SECONDS)
            lingering = [pid for pid, start in before.items() if not ended(pid, start, root)]
        if lingering:
            print_func(f"switchyard: {role.role} came back as pid {row['process_pid']}, but its old pane's process(es) "
                       f"{', '.join(map(str, lingering))} are still running {OLD_GONE_SECONDS:.0f}s on; it was not "
                       "restarted cleanly -- end them, then check `switchyard status "
                       f"{config.project}`")
            return 1
        print_func(f"switchyard: {verb}ed {role.role}{second}: pid {row['process_pid']} (generation "
                   f"{row.get('generation')}) replaces {old[0] or 'nothing'}{', which has ended' if before else ''}, "
                   f"registered by the board, with the current release's hooks, prompt and {len(wanted)} board "
                   f"variable(s); its recorded conversation ({conversation}) resumes where its rules keep it")
        unbound = reconnect_slots(config, config_path=config_path, role_name=role.role, runner=runner,
                                  print_func=print_func)
        return 1 if unbound else 0
    finally:
        lock.close()


def main(argv: Sequence[str]) -> int:
    """`role_restart.py operator <project> <config> <role> [...]`: the operator's restart, run by root's runuser as the
    project's owner. Refused unless the kernel shows exactly that parent."""
    import argparse

    from scripts import team_launcher as launcher

    parser = argparse.ArgumentParser(prog="role_restart.py")
    parser.add_argument("mode", choices=["operator"])
    parser.add_argument("project")
    parser.add_argument("config")
    parser.add_argument("role")
    parser.add_argument("--while-holding", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    config = launcher.load_project_config(args.project, Path(args.config))
    return restart_role_command(config, config_path=Path(args.config), role_name=args.role,
                                while_holding=args.while_holding, dry_run=args.dry_run)


if __name__ == "__main__":
    import sys

    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    raise SystemExit(main(sys.argv[1:]))
