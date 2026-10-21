#!/usr/bin/env python3
"""A Director's worker-pool restart is not refused for the Director's own pane identity, and never strands a worker.

Live MEFP, during SYRD-626's acceptance: the Director ran

    switchyard worker-pool mefp restart luna-1

from its own pane. The stop half killed luna-1; the start half's pane command
inherited the Director pane's TICKET_BOARD_PANE_SESSION_ID, and the launcher's
cross-pane guard refused it, because that id was not luna-1's recorded session:

    team-launcher: refusing to launch luna-1; ambient pane session id
    TICKET_BOARD_PANE_SESSION_ID=019f9418-... does not match recorded session f79f0f0a-...
    switchyard: luna-1 stopped: session mefp-luna-1 is gone; its identity is not
    switchyard: luna-1 failed to start: the pane command exited 1, and it is stopped

These cases run the real `switchyard worker-pool` command over the sandbox
project of worker_pool_restart_status_test, whose tmux sessions are real
processes. At the pane command, the stand-in asks the launcher's REAL guard
(`_ambient_session_conflict_for_role`) about the environment it was actually
handed, and exits 1 with its message exactly as the pane command does; and it
records the session id the worker's own command line would resume
(`cli_command_for_role`). So a restart that leaks the caller's identity fails
here the way it failed live (SYRD-635).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from scripts import role_pane_entry, team_launcher  # noqa: E402
from scripts.session_records import session_file_name  # noqa: E402

import worker_pool_restart_status_test as restart  # noqa: E402

PROJECT, SESSION = restart.PROJECT, restart.SESSION
WORKER_SESSION = "f79f0f0a-3544-4a56-9e9d-f64b960a284e"
DIRECTOR_SESSION = "019f9418-4802-7cb0-b55e-1d9a8a8d1dfb"
KEYS = ("TICKET_BOARD_PANE_SESSION_ID", "PGU_PANE_SESSION_ID")
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


class Host(restart.Host):
    """The restart suite's host, whose pane command answers as the real launcher's guard does."""

    def __init__(self, tmp: Path, **kwargs) -> None:
        super().__init__(tmp, **kwargs)
        self.role = team_launcher._role_by_name(self.config, "impl-1")
        self.session_dir = Path(self.config.session_dir)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        (self.session_dir / session_file_name(self.role.target)).write_text(
            json.dumps({"target": self.role.target, "session_id": WORKER_SESSION}))
        self.handed: list[dict[str, str] | None] = []
        self.refusals: list[str] = []
        self.resumes: list[str] = []

    def __call__(self, args, **kwargs):
        command = [str(part) for part in args]
        if "pane" in command and "attach-or-start" in command:
            env = kwargs.get("env")
            self.handed.append(None if env is None else {k: env[k] for k in KEYS if k in env})
            conflict = role_pane_entry._ambient_session_conflict_for_role(
                self.role, session_dir=self.session_dir, environ=os.environ if env is None else env)
            if conflict:  # the pane command prints it and exits 1, starting nothing
                self.refusals.append(conflict)
                self.pane_starts.append(command)
                return subprocess.CompletedProcess(command, 1, stdout="", stderr=conflict + "\n")
            argv = team_launcher.cli_command_for_role(self.role, session_dir=self.session_dir, resume=True)
            self.resumes.append(next((part.split("=", 1)[1] for part in argv
                                      if part.startswith("TICKET_BOARD_PANE_SESSION_ID=")), ""))
        return super().__call__(args, **kwargs)


def _sandbox(ambient: dict[str, str]):
    """Run a case as from a pane whose own identity is `ambient` (nothing else of the environment changes)."""
    def wrap(case):
        def run() -> None:
            saved = {key: os.environ.get(key) for key in KEYS}
            for key in KEYS:
                os.environ.pop(key, None)
            os.environ.update(ambient)
            with tempfile.TemporaryDirectory(prefix="syrd635.") as tmp:
                home = os.environ.get("HOME")
                os.environ["HOME"] = tmp
                try:
                    case(Path(tmp))
                finally:
                    os.environ["HOME"] = home if home is not None else tmp
                    for key, value in saved.items():
                        if value is None:
                            os.environ.pop(key, None)
                        else:
                            os.environ[key] = value
        run.__name__ = f"{case.__name__}[{','.join(ambient) or 'no pane identity'}]"
        return run
    return wrap


def restarts_its_own_session(tmp: Path) -> None:
    """The live sequence: the restart replaces the worker, which resumes its own recorded session, not the caller's."""
    host = Host(tmp)
    try:
        before = host.pid(SESSION)
        code, said = host.run("restart")
        text = "\n".join(said)
        check(code == 0 and "impl-1 stopped" in text and "impl-1 started" in text,
              f"the restart stops and starts the worker: {code} {text}")
        check(host.refusals == [], f"the launcher's guard was never met: {host.refusals}")
        check(host.handed == [{}], f"the pane command is handed no pane session identity: {host.handed}")
        check(host.resumes == [WORKER_SESSION], f"the worker resumes its own recorded session: {host.resumes}")
        check(host.alive(SESSION) and host.pid(SESSION) != before, "a fresh worker process is running")
    finally:
        host.close()


CASES = [_sandbox(ambient)(restarts_its_own_session) for ambient in (
    {}, {"TICKET_BOARD_PANE_SESSION_ID": DIRECTOR_SESSION}, {"PGU_PANE_SESSION_ID": DIRECTOR_SESSION},
    {"TICKET_BOARD_PANE_SESSION_ID": DIRECTOR_SESSION, "PGU_PANE_SESSION_ID": DIRECTOR_SESSION},
    # The worker's own id in the caller's pane is not a conflict either, and still not handed on.
    {"TICKET_BOARD_PANE_SESSION_ID": WORKER_SESSION})]


@_sandbox({"TICKET_BOARD_PANE_SESSION_ID": DIRECTOR_SESSION})
def start_alone_is_not_refused_either(tmp: Path) -> None:
    """`start` takes the same path: a stopped worker started from the Director's pane comes up as itself."""
    host = Host(tmp, running=())
    try:
        code, said = host.run("start")
        check(code == 0 and any("impl-1 started" in line for line in said) and host.refusals == []
              and host.handed == [{}] and host.resumes == [WORKER_SESSION], f"started as itself: {code} {said}")
    finally:
        host.close()


@_sandbox({"TICKET_BOARD_PANE_SESSION_ID": DIRECTOR_SESSION})
def a_refused_restart_leaves_the_worker_running(tmp: Path) -> None:
    """What start would refuse is asked first: nothing is stopped, the same worker keeps running."""
    host = Host(tmp)
    try:
        before = host.pid(SESSION)
        refused = "the board skill is not installed"
        from scripts import worker_start_gate

        real = worker_start_gate.start_refusal

        def refusing(pool, member, **kwargs):
            state, _ = real(pool, member, **kwargs)
            return state, refused

        worker_start_gate.start_refusal = refusing
        try:
            code, said = host.run("restart")
        finally:
            worker_start_gate.start_refusal = real
        text = "\n".join(said)
        check(code == 1 and "impl-1 not restarted" in text and refused in text and "nothing was stopped" in text,
              f"the refusal is said, and the restart fails: {code} {text}")
        check(host.kills == [] and host.pane_starts == [] and host.pid(SESSION) == before,
              f"nothing was stopped or started: the same worker is running: {host.kills} {host.pane_starts}")
        check("ready, running" in restart._status_line(host), f"and status agrees: {restart._status_line(host)}")
    finally:
        host.close()


@_sandbox({"TICKET_BOARD_PANE_SESSION_ID": DIRECTOR_SESSION})
def an_unprepared_worker_is_refused_before_its_stop(tmp: Path) -> None:
    """A real readiness blocker, not a stand-in: the worker without its board skill is refused, and left running."""
    host = Host(tmp)
    try:
        before = host.pid(SESSION)
        skills = [p for p in host.home.rglob("SKILL.md")]
        check(skills, "the sandbox owner home carries the board skill to remove")
        for path in skills:
            path.unlink()
        code, said = host.run("restart")
        text = "\n".join(said)
        check(code == 1 and "impl-1 not restarted" in text and "nothing was stopped" in text
              and "impl-1 stopped" not in text, f"refused before the stop: {code} {text}")
        check(host.kills == [] and host.pane_starts == [] and host.alive(SESSION) and host.pid(SESSION) == before,
              f"the same worker is still running: kills {host.kills}, starts {host.pane_starts}")
    finally:
        host.close()


@_sandbox({"TICKET_BOARD_PANE_SESSION_ID": DIRECTOR_SESSION})
def a_forced_restart_goes_past_the_blocker(tmp: Path) -> None:
    """--force is the operator's word, for restart as for start: the blocked worker is restarted, as itself."""
    host = Host(tmp)
    try:
        before = host.pid(SESSION)
        for path in host.home.rglob("SKILL.md"):
            path.unlink()
        said: list[str] = []
        original = team_launcher._owner_home_for_auth
        team_launcher._owner_home_for_auth = lambda _owner: host.home
        try:
            code = team_launcher.switchyard_worker_pool_command(
                PROJECT, action="restart", member="impl-1", force=True,
                config_dir=host.tmp, registry_dir=host.tmp / "registry",
                board_reader=lambda _config: {"document": host.document},
                board_snapshot_reader=lambda _config: host.board, runner=host, print_func=said.append)
        finally:
            team_launcher._owner_home_for_auth = original
        text = "\n".join(said)
        check(code == 0 and "impl-1 stopped" in text and "impl-1 started" in text and "forced past its blockers" in text
              and host.pid(SESSION) != before and host.resumes == [WORKER_SESSION],
              f"a forced restart replaces the worker, resuming its own session: {code} {text}")
    finally:
        host.close()


@_sandbox({"TICKET_BOARD_PANE_SESSION_ID": DIRECTOR_SESSION})
def the_guard_still_refuses_entering_another_session(tmp: Path) -> None:
    """Running a role's pane from a pane with another session's identity is refused, as before."""
    host = Host(tmp, running=())
    try:
        calls: list[list[str]] = []

        def runner(args, **_kwargs):
            calls.append([str(a) for a in args])
            return subprocess.CompletedProcess(args, 1)

        import contextlib
        import io

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = role_pane_entry.run_role_pane(host.role, mode="attach-or-start", session_dir=host.session_dir,
                                                 pane_state_dir=tmp / "panes", runner=runner)
        check(code == 1 and f"ambient pane session id TICKET_BOARD_PANE_SESSION_ID={DIRECTOR_SESSION} does not match "
              f"recorded session {WORKER_SESSION}" in err.getvalue() and all("new-session" not in c for c in calls),
              f"the cross-pane guard is unchanged for a pane that would enter another session: {code} {err.getvalue()}")
        stripped = role_pane_entry.without_ambient_pane_session(
            {"TICKET_BOARD_PANE_SESSION_ID": "a", "PGU_PANE_SESSION_ID": "b", "TMUX": "t", "HOME": "/h"})
        check(stripped == {"TMUX": "t", "HOME": "/h"}, f"only the pane session identity is left behind: {stripped}")
    finally:
        host.close()


def main() -> int:
    for case in [*CASES, start_alone_is_not_refused_either, a_refused_restart_leaves_the_worker_running,
                 an_unprepared_worker_is_refused_before_its_stop, a_forced_restart_goes_past_the_blocker,
                 the_guard_still_refuses_entering_another_session]:
        case()
    print(f"worker_pool_restart_identity_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
