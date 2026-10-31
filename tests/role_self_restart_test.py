#!/usr/bin/env python3
"""SYRD-713: a lifecycle loop that would end the caller's own role session hands that role to a successor first.

SAPI's Director restarted itself for an upgrade and did not come back: the board kept its dead process registered as
the Director, and nothing outside the ended pane was left to start it. This suite proves, against a fake /proc in the
kernel's own `stat` format and the launcher's real seams:

1. **Whose pane** is decided from kernel ancestry and start time only: the caller's pane root must be the role's tmux
   pane process. A sibling role, a process outside any pane, and a pane whose pid now names another process are none.
2. **Ordering**: the caller's own role is always last.
3. **The board's current view** (`runtime_targets`) admits only registrations whose process is still the one recorded:
   a dead pid, and a reused pid with another start time, are absent; the row itself is untouched.
4. **The successor**: ends the predecessor only after proving it is exactly the pane handed over and after the
   caller has finished; starts the role through the ordinary start path; reports complete only for a live successor
   registration other than the predecessor; retries once; otherwise records the role stopped, with the one
   `switchyard recover-display` action. A handoff for another project or role, or for a pane that is no longer the
   predecessor, is refused before anything is ended, and a second successor for the same role is refused while the
   first holds it.
   **Provenance**: an executor acts only when its own parent is the named caller, still that process, inside the
   predecessor's pane; one started from outside the pane, from another role's pane, or naming a caller that is not
   its parent, is refused before anything is ended, whatever its handoff says.
   **The conversation**: no handoff without a recorded, resumable conversation; a conversation changed or no longer
   resumable before the pane is ended leaves the pane running; a successor that records another conversation, or
   that does not run the provider's resume of this one, is reported failed, never complete.
5. **The launch**: the stale-provider drop and a reload leave the caller's own pane running, every other role is
   handled once, and the launch hands the caller's role over last.
6. **Transaction order**: the successor is armed before the launch stops, starts or changes anything, and is released
   only at its end. Every way arming can fail -- no recorded or resumable conversation, the executor not starting,
   refusing (lock, provenance) or not answering, or dying -- stops an attach-or-start or a reload before any phase
   runs and before any session is touched. A phase that finds the caller's own role needs a restart with no
   successor armed stops before it ends anything. The executor itself ends nothing unless released: cancelled, or
   left without a word, it records the role untouched.
"""

from __future__ import annotations

import fcntl
import io
import json
import os
import sys
import tempfile
import types
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

from scripts import role_self_restart as rsr  # noqa: E402
from scripts import team_launcher as launcher  # noqa: E402
from scripts.ticket_board import app as board_app  # noqa: E402
from scripts.ticket_board import peer_identity  # noqa: E402

CHECKS = 0
#: What the seams answer for a role's recorded conversation, a conversation's resumability and a pane's process tree.
CONVERSATION: dict[str, str] = {}
RESUMABLE: dict[str, tuple[bool, str]] = {}
ARGVS: dict[int, list[list[str]]] = {300: [["bash"], ["claude", "--model", "m", "--resume", "conversation-of-director"]]}


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


class FakeProc:
    """A /proc with only `stat`, in the kernel's format: `pid (comm) state ppid ... starttime ...`."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def add(self, pid: int, ppid: int, start: int, comm: str = "bash") -> None:
        (self.root / str(pid)).mkdir(parents=True, exist_ok=True)
        fields = ["S", str(ppid)] + ["0"] * 17 + [str(start), "0", "0"]
        (self.root / str(pid) / "stat").write_text(f"{pid} ({comm}) {' '.join(fields)}\n")

    def remove(self, pid: int) -> None:
        stat = self.root / str(pid) / "stat"
        if stat.exists():
            stat.unlink()


def role(name: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(role=name, target=f"p713-{name}:0.0", tmux_session=f"p713-{name}", detached=True,
                                 cli=["claude"], resume_mode="flag", resume_flag="--resume")


def project(tmp: Path) -> types.SimpleNamespace:
    return types.SimpleNamespace(project="p713", roles=[role("director"), role("main"), role("ops")],
                                 board_url="http://board.invalid")


@contextmanager
def seams(tmp: Path, panes: dict[str, int], config: types.SimpleNamespace, calls: list):
    """The launcher's seams the module reads, pointed at this test."""
    def runner(argv, **_kwargs):
        calls.append(list(argv))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    patches = {
        "pane_pid_for_role": lambda r, runner=None: panes.get(r.role, 0),
        "role_process_runner_for": lambda cfg, r, runner=None: runner,
        "role_pane_state_dir": lambda cfg, r, base: tmp / "state",
        "role_session_dir": lambda cfg, r: tmp / "sessions" / r.role,
        "session_id_for_role": lambda r, d: CONVERSATION.get(r.role, f"conversation-of-{r.role}"),
        "_resume_preflight_allows_attempt": lambda r, sid, session_dir: RESUMABLE.get(sid, (True, "")),
        "process_tree_argvs": lambda pid: ARGVS.get(pid, []),
        "tmux_kill_session_args": lambda r: ["tmux", "kill-session", "-t", r.tmux_session],
        "pane_command_args": lambda project, r, **kw: ["switchyard-pane", project, r.role, kw.get("mode", "")],
        "switchyard_pane_launcher_for": lambda cfg: tmp / "switchyard-pane",
        "role_run_as_user": lambda cfg, r: "",
        "load_project_config": lambda slug, path: config,
        "_role_by_name": lambda cfg, name: next((r for r in cfg.roles if r.role == name), None),
        "RUNTIME_REGISTRATION_TIMEOUT_SECONDS": 6.0,
        "RUNTIME_REGISTRATION_POLL_SECONDS": 2.0,
    }
    (tmp / "state").mkdir(parents=True, exist_ok=True)
    with mock.patch.multiple(launcher, **patches):
        yield runner


def board(assignments: list[dict]):
    """An opener answering `/api/runtime-assignments/<role>` with each next assignment ({} is a 404)."""
    answers = iter(assignments)

    def opener(url, timeout=None):
        current = next(answers, assignments[-1] if assignments else {})
        if not current:
            raise OSError("404")
        return io.BytesIO(json.dumps({"assignment": current}).encode())
    return opener


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.05)

    def monotonic(self) -> float:
        return self.now


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="s713-"))
    proc = FakeProc(tmp / "proc")
    # tmux server 100; the director's pane 200 (start 5000), a shell 300 and this command 400 inside it;
    # main's pane 210; a process 500 outside every pane.
    proc.add(100, 1, 1000, comm="tmux: server")
    proc.add(200, 100, 5000)
    proc.add(300, 200, 5100)
    proc.add(400, 300, 5200, comm="python3")
    proc.add(210, 100, 6000)
    proc.add(500, 1, 7000)
    # Executors: 450 started by this command (400); 550 by the process outside every pane; 560 by main's pane.
    proc.add(450, 400, 5300, comm="python3")
    proc.add(550, 500, 7100, comm="python3")
    proc.add(560, 210, 6100, comm="python3")
    config = project(tmp)
    director, main_role, ops = config.roles
    panes = {"director": 200, "main": 210, "ops": 220}

    # 1. Whose pane, from the kernel.
    calls: list = []
    with seams(tmp, panes, config, calls) as runner:
        check(rsr.own_pane_role(config, config.roles, runner=runner, pid=400, proc_root=proc.root) is director,
              "a command inside the director's pane is the director's own")
        check(rsr.own_pane_role(config, [main_role, ops], runner=runner, pid=400, proc_root=proc.root) is None,
              "and not a sibling role's, though it can name them")
        check(rsr.own_pane_role(config, config.roles, runner=runner, pid=500, proc_root=proc.root) is None,
              "a process outside every pane is no role's")
        proc.add(200, 100, 5000)  # unchanged
        reused = dict(panes, director=210)  # tmux now reports another process as the director's pane
        with mock.patch.object(launcher, "pane_pid_for_role", lambda r, runner=None: reused.get(r.role, 0)):
            check(rsr.own_pane_role(config, config.roles, runner=runner, pid=400, proc_root=proc.root) is None,
                  "a pane whose pid is now another process is not the caller's")

    # 1b. No environment variable enters it. In the director's pane by kernel ancestry, the caller is the director's
    #     whatever TMUX_PANE and TICKET_BOARD_PROJECT say -- absent, empty, or another project's.
    me = peer_identity.read_process(os.getpid())
    proc.add(os.getpid(), 300, me.start_time, comm="python3")  # this test process, as a command in the director's pane
    scrubbed = ({}, {"TMUX_PANE": "", "TICKET_BOARD_PROJECT": ""}, {"TMUX_PANE": "%9", "TICKET_BOARD_PROJECT": "other"})
    for env in scrubbed:
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(peer_identity, "PROC_ROOT", proc.root), \
                seams(tmp, panes, config, calls) as runner:
            check(rsr.own_pane_role(config, config.roles, runner=runner) is director,
                  f"in the director's pane by ancestry, with environment {env}: still the director's own")
    proc.remove(os.getpid())
    with mock.patch.dict(os.environ, {"TMUX_PANE": "%1", "TICKET_BOARD_PROJECT": "p713"}), \
            mock.patch.object(peer_identity, "PROC_ROOT", proc.root), seams(tmp, panes, config, calls) as runner:
        calls.clear()
        found = rsr.own_pane_role(config, config.roles, runner=runner)
        problem = rsr.arm_for_launch(config, mode="reload", config_path=tmp / "p713.json", owner_home=tmp, runner=runner)
    check(found is None and problem == "" and calls == [],
          f"and a process in no tmux pane by ancestry is no role's, whatever its environment claims: {calls}")

    # 2. Ordering.
    check([r.role for r in rsr.last(config.roles, director)] == ["main", "ops", "director"], "the caller's own role last")
    check([r.role for r in rsr.last(config.roles, None)] == ["director", "main", "ops"], "no caller, the order stands")

    # 3. The board's current view admits live processes only.
    me = peer_identity.read_process(os.getpid())
    rows = [{"role": "director", "process_pid": 2 ** 22 + 7, "process_start_time": 1},          # dead
            {"role": "main", "process_pid": os.getpid(), "process_start_time": me.start_time + 1},  # reused pid
            {"role": "ops", "process_pid": os.getpid(), "process_start_time": me.start_time}]     # live
    fake_conn = mock.MagicMock()
    fake_conn.__enter__.return_value.execute.return_value.fetchall.return_value = rows
    app = board_app.TicketBoardApp.__new__(board_app.TicketBoardApp)
    with mock.patch.object(board_app.TicketBoardApp, "_pg_connect", lambda self: fake_conn):
        current = app.runtime_targets()
    check(set(current) == {"ops"}, f"only the live registration is current: {sorted(current)}")
    check(len(rows) == 3, "and the rows themselves are untouched")

    # 4. The successor.
    def handoff(**over) -> rsr.Handoff:
        fields = dict(project="p713", role="director", config_path=str(tmp / "p713.json"),
                      provider_session="conversation-of-director", predecessor_pid=200, predecessor_start_time=5000,
                      caller_pid=400, caller_start_time=5200, transaction="t713")
        fields.update(over)
        return rsr.Handoff(**fields)

    def run(h, assignments, *, gone_after_kill=True, start_rc=0, prepare=lambda: None, executor=450,
            on_armed=lambda: None, on_kill=lambda: None, word=None):
        calls.clear()
        CONVERSATION.clear()
        RESUMABLE.clear()
        clock = Clock()
        proc.add(200, 100, 5000)
        proc.add(400, 300, 5200, comm="python3")
        (tmp / "state" / "director.successor.json").unlink(missing_ok=True)
        prepare()
        out: list[str] = []

        def runner(argv, **_kwargs):
            calls.append(list(argv))
            if argv[:2] == ["tmux", "kill-session"]:
                on_kill()
                if gone_after_kill:
                    proc.remove(200)
            rc = start_rc if argv and argv[0] == "switchyard-pane" else 0
            return types.SimpleNamespace(returncode=rc, stdout="", stderr="")

        with seams(tmp, panes, config, []), mock.patch.object(launcher, "role_process_runner_for",
                                                              lambda cfg, r, runner=None: runner):
            def armed():
                out.append("armed")
                proc.remove(400)  # the caller finishes once it is told the successor is armed
                on_armed()
            rc = rsr.run_successor(h, runner=runner, opener=board(assignments), sleep=clock.sleep,
                                   monotonic=clock.monotonic, proc_root=proc.root, armed=armed, print_func=out.append,
                                   pid=executor, released=lambda: f"release {h.transaction}" if word is None else word)
        record = json.loads((tmp / "state" / "director.successor.json").read_text()) \
            if (tmp / "state" / "director.successor.json").exists() else {}
        return rc, out, record

    successor = {"process_pid": 300, "process_start_time": 5100}  # a live process other than the predecessor
    rc, out, record = run(handoff(), [{}, {"process_pid": 200, "process_start_time": 5000}, successor])
    check(rc == 0 and record.get("state") == "complete" and record.get("successor") == [300, 5100],
          f"a live successor completes the transaction: {rc} {record}")
    kill = ["tmux", "kill-session", "-t", "p713-director"]
    start = ["switchyard-pane", "p713", "director", "attach-or-start"]
    check(calls.index(kill) < calls.index(start), f"the predecessor is ended before its successor starts: {calls}")
    check(record.get("provider_session") == "conversation-of-director" and "on conversation conversation-of-director"
          in " ".join(out), f"and comes back on the recorded conversation, resumed: {record} {out}")
    check(not any("main" in " ".join(c) or "ops" in " ".join(c) for c in calls), "no neighbouring pane is touched")
    check(out[0] == "armed", "and it armed before ending anything")

    rc, out, record = run(handoff(), [{"process_pid": 200, "process_start_time": 5000}])
    check(rc == 1 and record.get("state") == "stopped" and record.get("recover") == "switchyard recover-display p713",
          f"only the dead predecessor registered: stopped, never complete: {rc} {record}")
    check(calls.count(start) == 2, "after one bounded retry")
    check(sum("recover-display" in line for line in out) == 1, f"the recovery action is said once: {out}")

    rc, out, record = run(handoff(), [successor], start_rc=1)
    check(rc == 1 and record.get("state") == "stopped" and "start 2 exited 1" in record.get("reason", ""),
          f"a start that fails twice is stopped, with why: {record}")

    rc, out, record = run(handoff(), [successor], gone_after_kill=False)
    check(rc == 1 and record.get("state") == "stopped" and not any(c == start for c in calls),
          f"a predecessor that does not end is not restarted over: {record}")

    for label, h in (("another project's", handoff(project="other")), ("another role's", handoff(role="main")),
                     ("another role's, naming that role's own conversation",
                      handoff(role="main", provider_session="conversation-of-main")),
                     ("a stale predecessor", handoff(predecessor_start_time=4999)),
                     ("a replayed one after the pane was replaced", handoff(predecessor_pid=210))):
        (tmp / "state" / "director.successor.json").unlink(missing_ok=True)
        rc, out, record = run(h, [successor])
        check(rc == 2 and not any(c[:2] == ["tmux", "kill-session"] or (c and c[0] == "switchyard-pane") for c in calls),
              f"{label} handoff is refused before anything is ended: {rc} {out}")

    nothing_ended = lambda: kill not in calls and not any(c and c[0] == "switchyard-pane" for c in calls)  # noqa: E731
    # Provenance: the handoff's words are the same; only the executor's real parent differs.
    for label, h, executor in (
            ("an executor started outside every pane", handoff(), 550),
            ("an executor started from another role's pane", handoff(caller_pid=210, caller_start_time=6000), 560),
            ("an executor whose parent is not the caller it names", handoff(caller_pid=300, caller_start_time=5100), 450),
            ("an executor naming its caller with another start time", handoff(caller_start_time=5201), 450)):
        rc, out, record = run(h, [successor], executor=executor)
        check(rc == 2 and nothing_ended() and "armed" not in out and "not started by a command" in " ".join(out),
              f"{label} is refused before anything is armed or ended: {rc} {out}")

    # The conversation.
    rc, out, record = run(handoff(), [successor], prepare=lambda: CONVERSATION.update(director="another"))
    check(rc == 2 and nothing_ended() and "armed" not in out and "now another" in " ".join(out),
          f"a conversation changed before arming is refused, nothing ended: {out}")
    rc, out, record = run(handoff(provider_session=""), [successor])
    check(rc == 2 and nothing_ended() and "no conversation is recorded" in " ".join(out),
          f"a handoff with no recorded conversation is refused: {out}")
    rc, out, record = run(handoff(), [successor],
                          prepare=lambda: RESUMABLE.update({"conversation-of-director": (False, "store missing")}))
    check(rc == 2 and nothing_ended() and "store missing" in " ".join(out),
          f"a conversation the start path would not resume is refused, nothing ended: {out}")
    rc, out, record = run(handoff(), [successor], on_armed=lambda: CONVERSATION.update(director="another"))
    check(rc == 2 and nothing_ended() and record.get("state") == "refused" and record.get("ended") is False,
          f"a conversation changed after arming leaves the pane running: {rc} {record}")
    rc, out, record = run(handoff(), [successor], on_kill=lambda: CONVERSATION.update(director="fresh-one"))
    check(rc == 1 and record.get("state") == "failed" and "records conversation fresh-one" in record.get("reason", "")
          and "recover-display" in record.get("recover", ""),
          f"a successor on a fresh conversation is failed, never complete: {rc} {record}")
    fresh = {"process_pid": 310, "process_start_time": 5150}
    rc, out, record = run(handoff(), [fresh], prepare=lambda: proc.add(310, 200, 5150))
    check(rc == 1 and record.get("state") == "failed" and "does not resume" in record.get("reason", ""),
          f"a successor that records the conversation but did not resume it is failed: {rc} {record}")

    # Nothing is ended unless the launch releases the successor.
    for label, word, state, code in (("cancelled", "cancel t713", "cancelled", 0),
                                     ("left without a word", "", "aborted", 2),
                                     ("released for another transaction", "release other", "aborted", 2)):
        rc, out, record = run(handoff(), [successor], word=word)
        check(rc == code and nothing_ended() and record.get("state") == state and record.get("ended") is False
              and out[0] == "armed", f"an armed successor {label} ends nothing: {rc} {record} {out}")

    held = open(tmp / "state" / "director.successor.lock", "a+")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    rc, out, record = run(handoff(transaction="second"), [successor])
    check(rc == 2 and kill not in calls and "already holds" in " ".join(out),
          f"a second successor for the same role is refused while the first holds it: {out}")
    held.close()

    # 5. The launch: the caller's own pane is kept out of the stale-runtime drop, which ends every other one once.
    stale = [director, main_role, ops]
    calls.clear()
    with seams(tmp, panes, config, calls) as runner, \
            mock.patch.object(launcher, "unreadable_provider_state_roles", lambda cfg, roles: []), \
            mock.patch.object(launcher, "roles_with_stale_provider_runtime",
                              lambda cfg, roles, owner_home: [r for r in roles if r in stale]), \
            mock.patch.object(rsr, "own_pane_role", lambda cfg, roles, runner=None, **kw: director):
        others, own, own_stale = rsr.split_own_stale(config, stale, owner_home=tmp, runner=runner)
        keep, _unreconciled = launcher._drop_roles_with_stale_provider_runtime(
            config, others, owner_home=tmp, runner=runner, print_func=lambda line: None)
        stale.remove(director)
        _others, _own, fresh = rsr.split_own_stale(config, [director, main_role], owner_home=tmp, runner=runner)
    kills = [c for c in calls if c[:2] == ["tmux", "kill-session"]]
    check(own is director and own_stale is director and director not in others,
          f"the caller's own stale pane is kept out of the drop and handed over: {own_stale}")
    check(sorted(k[-1] for k in kills) == ["p713-main", "p713-ops"], f"which ends every other stale role once: {kills}")
    check(_own is director and fresh is None, "and a caller whose own runtime is current is not restarted at all")
    stale = [director, main_role, ops]

    # 5a. P5 itself: with no successor armed, a stale own role stops attach-or-start before the drop ends anyone.
    from scripts import launch_phases

    dropped: list = []
    said = []
    with seams(tmp, panes, config, calls) as runner, \
            mock.patch.object(rsr, "own_pane_role", lambda cfg, roles, runner=None, **kw: director), \
            mock.patch.multiple(launcher, ensure_launcher_checkout_current=lambda *a, **k: None,
                                _running_project_roles=lambda cfg, runner=None: list(stale),
                                roles_with_stale_provider_runtime=lambda cfg, roles, owner_home: list(roles),
                                _drop_roles_with_stale_provider_runtime=lambda *a, **k: dropped.append(a) or ([], set())):
        try:
            exit_code = launch_phases._prepare_launch(
                config, allow_stale_launcher=False, config_path=tmp / "p713.json", dry_run=False,
                effective_pane_state_dir=tmp / "state", mode="attach-or-start", no_launcher_self_deploy=True,
                owner_home=tmp, pane_script_path=tmp / "switchyard-pane", print_func=said.append, runner=runner,
                worktree_runner=runner).exit_code
        except Exception as exc:  # past the refusal P5 runs on into seams this suite does not stand in for
            exit_code = f"went past the refusal: {type(exc).__name__}"
    check(exit_code == 1 and dropped == [] and "no successor was armed" in " ".join(said),
          f"P5 with the caller's own stale role and no armed successor stops before the drop: {exit_code} {dropped} {said}")

    # 5b. A reload from inside the Director's pane: every other role is reloaded once, the caller's own is not.
    from scripts import launch_phases, presentation_controller, worker_admission

    config.roles = [director, main_role, types.SimpleNamespace(**dict(vars(ops), detached=False))]
    reloaded: list[str] = []

    def reload_phase(said=lambda line: None):
        return launch_phases._start_workers_and_present(
            config, allow_stale_launcher=False, config_path=tmp / "p713.json", delegate_role_sessions_to_owner=False,
            effective_pane_state_dir=tmp / "state", failed_roles={}, force_reload=False, konsole_process_launcher=None,
            layout_environ=None, layout_mode="auto", layout_output=None, mode="reload", output_path=tmp / "layout.json",
            pane_script_path=tmp / "switchyard-pane", print_func=said, role_process_runner=runner,
            runner=runner, window_title="p713")

    armed_director = rsr.Successor("director", "t", types.SimpleNamespace(), tmp / "log", tmp / "record")
    with seams(tmp, panes, config, calls) as runner, \
            mock.patch.object(rsr, "own_pane_role", lambda cfg, roles, runner=None, **kw: director), \
            mock.patch.object(worker_admission, "for_launch", lambda *a, **k: {}), \
            mock.patch.object(presentation_controller, "presentation_enabled", lambda *a, **k: False), \
            mock.patch.multiple(
                launcher,
                run_detached_role=lambda r, **kw: reloaded.append(r.role) or 0,
                ensure_visible_role_session_for_viewer=lambda r, **kw: reloaded.append(r.role) or 0,
                visible_roles_for_viewer=lambda cfg: [r for r in cfg.roles if not r.detached] + [director],
                resolve_layout_mode=lambda mode, environ=None, runner=None: launcher.LAYOUT_MODE_VIEWER,
                ensure_owner_state_dirs=lambda *a, **k: None,
                launch_tmux_viewer_session=lambda roles, **kw: 0,
                viewer_session_for_project=lambda project: "p713-viewer",
                running_through_tenant_control=lambda: False):
        said: list[str] = []
        unarmed = reload_phase(said.append)
        check(unarmed == 1 and reloaded == [] and "no successor was armed" in " ".join(said),
              f"a reload from the director's pane with no successor armed stops before reloading anything: {reloaded}")
        with mock.patch.dict(rsr._ARMED, {("p713", "director"): armed_director}):
            started = reload_phase()
    check(sorted(reloaded) == ["main", "ops"] and len(reloaded) == 2,
          f"a reload from the director's pane reloads every other role once and not the director: {reloaded}")
    check(getattr(started, "own_restart", None) is director, "and hands the director back to be restarted last")
    config.roles = [director, main_role, ops]

    # 5c. Stopping: the caller's own session is ended last.
    calls.clear()
    with seams(tmp, panes, config, calls) as runner, \
            mock.patch.object(rsr, "own_pane_role", lambda cfg, roles, runner=None, **kw: director), \
            mock.patch.object(launcher, "tmux_has_session_args", lambda r: ["tmux", "has-session", "-t", r.tmux_session]):
        launcher.stop_role_sessions(config, runner=runner, print_func=lambda line: None)
    order = [c[-1] for c in calls if c[:2] == ["tmux", "kill-session"]]
    check(order == ["p713-main", "p713-ops", "p713-director"], f"a stop from the director's pane ends it last: {order}")

    # 5d. The identity cutover stops every role together: from a role's own pane it is refused before any stop.
    from scripts import role_identity_cutover

    stopped: list = []
    said: list[str] = []
    with mock.patch.object(rsr, "own_pane_role", lambda cfg, roles, runner=None, **kw: director), \
            mock.patch.multiple(launcher, read_pending_identities=lambda cfg: {"main": {"account": "p713-main"}},
                                _role_accounts_ready=lambda cfg: True,
                                staged_role_tooling_problems=lambda *a, **k: [],
                                role_tooling_staging_dir=lambda project: str(tmp / "tooling"),
                                _repo_root=lambda: tmp):
        try:
            rc = role_identity_cutover.cutover_role_identities_command(
                config, config_path=tmp / "p713.json",
                runner=lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=""),
                stopper=lambda cfg: stopped.append(cfg) or 0, launcher=lambda cfg: 0, print_func=said.append)
        except Exception as exc:  # past the preflight it runs on into seams this suite does not stand in for
            rc = f"went past the preflight: {type(exc).__name__}"
    check(rc == 1 and not stopped and "Nothing was stopped" in " ".join(said),
          f"the cutover from the director's own pane is refused and stops nothing: {rc} {said}")

    # 6. Detachment, for real: a child started as `arrange_successor` starts its executor -- its own session, its
    #    authority on stdin -- outlives `kill-session` of the tmux pane that started it.
    import shutil
    import subprocess
    import time

    if shutil.which("tmux"):
        socket_dir = Path(tempfile.mkdtemp(prefix="s713t-", dir="/tmp"))
        tmux = ["tmux", "-S", str(socket_dir / "s")]
        marker = socket_dir / "survived"
        child = (f"import os, sys, time; sys.stdin.readline(); time.sleep(2); open({str(marker)!r}, 'w').write(str(os.getsid(0)))")
        parent = (f"import subprocess, sys, time; "
                  f"p = subprocess.Popen([sys.executable, '-c', {child!r}], stdin=subprocess.PIPE, start_new_session=True, close_fds=True); "
                  f"p.stdin.write(b'handoff\\n'); p.stdin.close(); time.sleep(30)")
        try:
            subprocess.run(tmux + ["new-session", "-d", "-s", "p713", sys.executable, "-c", parent], check=True,
                           env={k: v for k, v in os.environ.items() if not k.startswith("TMUX")})
            time.sleep(0.8)
            subprocess.run(tmux + ["kill-session", "-t", "p713"], check=True)
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.2)
            check(marker.exists(), "a successor started in its own session outlives the pane that started it")
        finally:
            subprocess.run(tmux + ["kill-server"], stderr=subprocess.DEVNULL)
            shutil.rmtree(socket_dir, ignore_errors=True)
    # 7. Arming and releasing, from the caller's side.
    spawned: list = []
    instances: list = []

    class Spawned:
        """An executor stand-in: says `answer` (None: says nothing, keeps its pipe open) on stdout."""

        answer: bytes | None = b"armed " + b"x" * 32 + b"\n"

        def __init__(self, argv, **kwargs):
            spawned.append((argv, kwargs))
            instances.append(self)
            self.stdin = io.BytesIO()
            self.stdin.close = lambda: None
            self.killed = False
            read, self.write_end = os.pipe()
            if self.answer is not None:
                os.write(self.write_end, self.answer)
                os.close(self.write_end)
            self.stdout = os.fdopen(read, "rb")

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            return 0

    def arm(r=None, spawn=Spawned):
        with seams(tmp, panes, config, calls) as runner, mock.patch.object(rsr.secrets, "token_hex", lambda n: "x" * 32):
            return rsr.arm_successor(config, r or director, config_path=tmp / "p713.json", runner=runner, spawn=spawn,
                                     pid=400, proc_root=proc.root, print_func=lambda line: None)

    problem = arm()
    argv, kwargs = spawned[0]
    check(problem == "" and kwargs.get("start_new_session") is True and kwargs.get("close_fds") is True,
          f"the executor is started in its own session: {problem!r} {kwargs}")
    check(argv[-1] == "successor" and "p713" not in " ".join(argv) and "env" not in kwargs,
          f"and its authority is not on its argv or environment: {argv}")
    child = rsr._ARMED[("p713", "director")].child
    check(child.stdin.getvalue().count(b"\n") == 1, "armed, it has been given the handoff and nothing more")
    rsr.release_armed(config, director, print_func=lambda line: None)
    check(child.stdin.getvalue().splitlines()[-1] == b"release " + b"x" * 32 and not rsr._ARMED,
          "the launch's end releases the successor of the role it needs restarted")
    arm()
    child = rsr._ARMED[("p713", "director")].child
    rsr.release_armed(config, None, print_func=lambda line: None)
    check(child.stdin.getvalue().splitlines()[-1] == b"cancel " + b"x" * 32 and not rsr._ARMED,
          "and cancels one the launch turned out not to need")
    count = len(spawned)
    refused = arm(main_role)
    check(refused.startswith("main is not") and len(spawned) == count,
          f"a role that is not the caller's own pane is not handed over at all: {refused!r}")

    class Refusing(Spawned):
        answer = b"switchyard: refused: another successor already holds director's restart\n"

    class Silent(Spawned):
        answer = None

    def broken(argv, **kwargs):
        raise OSError("no such interpreter")

    def no_conversation():
        CONVERSATION.update(director="")

    def not_resumable():
        RESUMABLE.update({"conversation-of-director": (False, "store missing")})

    me = peer_identity.read_process(os.getpid())
    proc.add(os.getpid(), 300, me.start_time, comm="python3")  # this test process, as a command in the director's pane
    kinds = (("no recorded conversation", no_conversation, Spawned), ("no resumable conversation", not_resumable, Spawned),
             ("an executor that cannot start", lambda: None, broken),
             ("an executor that refuses (lock or provenance)", lambda: None, Refusing),
             ("an executor that never answers", lambda: None, Silent),
             ("a real executor that dies before arming", lambda: None, None))
    phases = ("migrate_declarative_director_onboarding", "_launch_runners_and_paths", "upgrade_generated_project_layout",
              "prepare_project_desktop", "_verify_pane_launcher_path", "_prepare_launch", "_write_layout_and_plan",
              "_start_workers_and_present", "_report_launch", "report_kept_worktrees")
    from scripts import project_launch

    def launch(mode, spawn, own_restart=None, stale=True, env=None):
        """launch_project from this test process, which the fake /proc places in the director's pane by ancestry; the
        environment is scrubbed (or `env`), so only the kernel can say whose pane it is."""
        reached: list[str] = []
        session_calls: list = []
        config.role_state_isolation = False
        stubs = {name: (lambda name: lambda *a, **k: reached.append(name))(name) for name in phases}
        stubs.update(
            _launch_runners_and_paths=lambda *a, **k: reached.append("_launch_runners_and_paths") or types.SimpleNamespace(
                worktree_runner=None, role_process_runner=None, delegate_role_sessions_to_owner=False,
                effective_pane_state_dir=tmp / "state", output_path=tmp / "layout.json", window_title="p713",
                should_assign_layout_owner=False, pane_script_path=tmp / "switchyard-pane"),
            upgrade_generated_project_layout=lambda *a, **k: reached.append("upgrade") or types.SimpleNamespace(changed=False),
            prepare_project_desktop=lambda cfg, **k: reached.append("prepare_project_desktop") or cfg,
            migrate_declarative_director_onboarding=lambda *a, **k: reached.append("migrate") and False,
            _prepare_launch=lambda cfg, **k: reached.append("_prepare_launch") or types.SimpleNamespace(
                exit_code=None, config=cfg, failed_roles={}, running_roles=[], reconcile_home=tmp,
                unreconciled_roles=set(), kept_worktrees={}, own_restart=own_restart),
            _start_workers_and_present=lambda cfg, **k: reached.append("_start_workers_and_present") or launch_phases.WorkerStartup(
                0, 0.0, 0, "viewer"),
            _report_launch=lambda *a, **k: reached.append("_report_launch") or 0,
            refuse_restart_on_another_model=lambda *a, **k: False,
            roles_with_stale_provider_runtime=lambda cfg, roles, owner_home: list(roles) if stale else [])

        def session_runner(argv, **kwargs):
            session_calls.append(list(argv))
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        said: list[str] = []
        with seams(tmp, panes, config, []), mock.patch.multiple(launcher, **stubs), \
                mock.patch.object(peer_identity, "PROC_ROOT", proc.root), \
                mock.patch.object(rsr, "ARMED_TIMEOUT_SECONDS", 0.3), \
                mock.patch.dict(os.environ, env or {}, clear=True), \
                mock.patch.object(rsr.secrets, "token_hex", lambda n: "x" * 32), \
                mock.patch.object(rsr, "_spawn", spawn or rsr._spawn):
            rc = project_launch.launch_project(config, config_path=tmp / "p713.json", mode=mode,
                                               script_path=tmp / "switchyard-pane", runner=session_runner,
                                               owner_home=tmp, print_func=said.append)
        return rc, reached, session_calls, said

    for mode in ("attach-or-start", "reload"):
        for label, prepare, spawn in kinds:
            CONVERSATION.clear()
            RESUMABLE.clear()
            prepare()
            before = len(spawned)
            rc, reached, session_calls, said = launch(mode, spawn)
            check(rc == 1 and reached == [] and session_calls == [] and not rsr._ARMED
                  and "no role was stopped, started or changed" in " ".join(said),
                  f"{mode}, {label}: refused before any phase or session is touched: {rc} {reached} {session_calls} {said}")
            if spawn is Refusing or spawn is Silent:
                check(len(spawned) == before + 1 and instances[-1].killed,
                      f"{mode}, {label}: the one executor tried is ended, having ended nothing")
        CONVERSATION.clear()
        RESUMABLE.clear()
        if mode == "attach-or-start":
            before = len(spawned)
            rc, reached, session_calls, said = launch(mode, Spawned, stale=False)
            check(rc == 0 and len(spawned) == before and "_start_workers_and_present" in reached and not rsr._ARMED,
                  f"attach-or-start from a current own pane arms nothing and launches as before: {rc} {reached}")
        for own_restart, word in ((director, b"release"), (None, b"cancel")):
            for env in scrubbed:
                before = len(spawned)
                rc, reached, session_calls, said = launch(mode, Spawned, own_restart=own_restart, env=env)
                lines = instances[-1].stdin.getvalue().splitlines()
                check(rc == 0 and len(spawned) == before + 1 and "_start_workers_and_present" in reached
                      and "_report_launch" in reached and not rsr._ARMED
                      and len(lines) == 2 and lines[-1] == word + b" " + b"x" * 32,
                      f"{mode}, environment {env}: armed by ancestry before any phase, the launch runs and, at its end, "
                      f"says {word!r}: {rc} {reached} {lines[1:]} {said}")

    # The phases themselves, by ancestry alone, with the environment scrubbed: P5 keeps the director's own stale
    # pane out of the drop, and P7's reload leaves it to its armed successor.
    proc.add(os.getpid(), 300, me.start_time, comm="python3")
    for env in scrubbed:
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(peer_identity, "PROC_ROOT", proc.root), \
                seams(tmp, panes, config, calls) as runner, \
                mock.patch.object(launcher, "roles_with_stale_provider_runtime", lambda cfg, roles, owner_home: list(roles)):
            others, own, own_stale = rsr.split_own_stale(config, list(config.roles), owner_home=tmp, runner=runner)
        check(own is director and own_stale is director and director not in others,
              f"P5, environment {env}: the director's own stale pane is kept out of the drop by ancestry")
        reloaded.clear()
        config.roles = [director, main_role, types.SimpleNamespace(**dict(vars(ops), detached=False))]
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(peer_identity, "PROC_ROOT", proc.root), \
                seams(tmp, panes, config, calls) as runner, \
                mock.patch.dict(rsr._ARMED, {("p713", "director"): armed_director}), \
                mock.patch.object(worker_admission, "for_launch", lambda *a, **k: {}), \
                mock.patch.object(presentation_controller, "presentation_enabled", lambda *a, **k: False), \
                mock.patch.multiple(
                    launcher,
                    run_detached_role=lambda r, **kw: reloaded.append(r.role) or 0,
                    ensure_visible_role_session_for_viewer=lambda r, **kw: reloaded.append(r.role) or 0,
                    visible_roles_for_viewer=lambda cfg: [r for r in cfg.roles if not r.detached] + [director],
                    resolve_layout_mode=lambda mode, environ=None, runner=None: launcher.LAYOUT_MODE_VIEWER,
                    ensure_owner_state_dirs=lambda *a, **k: None,
                    launch_tmux_viewer_session=lambda roles, **kw: 0,
                    viewer_session_for_project=lambda project: "p713-viewer",
                    running_through_tenant_control=lambda: False):
            started = reload_phase()
        config.roles = [director, main_role, ops]
        check(sorted(reloaded) == ["main", "ops"] and getattr(started, "own_restart", None) is director,
              f"P7 reload, environment {env}: every other role reloaded once, the director left to its successor: "
              f"{reloaded}")
    proc.remove(os.getpid())

    # 8. The executor's own entry point, on real descriptors: the handoff line is read unbuffered, so the release line
    #    the caller writes only after "armed" is still there to be read; nothing after it is.
    got: list = []
    stdin_r, stdin_w = os.pipe()
    out_r, out_w = os.pipe()
    saved = os.dup(0), os.dup(1)
    os.write(stdin_w, json.dumps(vars(handoff())).encode() + b"\n")

    def fake_run(h, *, armed, released, print_func):
        armed()
        os.write(stdin_w, b"release t713\n")
        os.close(stdin_w)
        got.extend([h.transaction, released()])
        return 0

    try:
        os.dup2(stdin_r, 0)
        os.dup2(out_w, 1)
        with mock.patch.object(rsr, "run_successor", fake_run):
            rc = rsr.main(["successor"])
    finally:
        os.dup2(saved[0], 0)
        os.dup2(saved[1], 1)
        for fd in (*saved, stdin_r, out_w):
            os.close(fd)
    said = os.read(out_r, 4096)
    os.close(out_r)
    check(rc == 0 and got == ["t713", "release t713"] and said == b"armed t713\n",
          f"the entry point reads the handoff, says armed once, then reads the release: {rc} {got} {said!r}")

    print(f"role_self_restart_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
