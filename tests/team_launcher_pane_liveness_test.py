#!/usr/bin/env python3
"""SYRD-169: six live panes reported absent, because their argv had moved on.

Live SYRD-146 journal 0074. Both boards had converged, all six worker tmux
sessions existed, the board's runtime-assignment endpoint listed
app/audit/director/inspector/main/ops with their original pids, and
`switchyard resume-provision syrd` said every pane was missing.

`_role_has_pane_process` searched `ps -eo args` for
`TICKET_BOARD_PANE_TARGET=<target>`. That marker belongs to the env wrapper that
STARTED the pane; a long-running CLI has exec'd past it, so the marker is simply
not in the argv any more. The panes were fine. The proof was not.

These cases are about the replacement proof: the owner's own tmux server, and
the board's runtime assignment checked against /proc by pid, start time and uid.
`/proc` is a directory built here, so a reused pid and a dead pid are real
inputs rather than mocked answers.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import team_launcher as launcher  # noqa: E402

TENANT = "syrd-agent"
OWNER_UID = 1006
CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    assert condition, message
    CHECKS += 1


def config_for(roles: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        project="syrd",
        run_as_user=TENANT,
        board_socket="/run/syrd-ticket-board/ticket-board.sock",
        roles=[SimpleNamespace(role=name, target=f"syrd-{name}:0.0") for name in roles],
    )


def fake_proc(root: Path, pid: int, *, start: int, uid: int = OWNER_UID) -> None:
    """One process, with the two fields the proof reads.

    The comm is deliberately hostile -- a name with a space and a closing
    parenthesis in it -- because /proc/<pid>/stat is parsed by splitting on the
    LAST `)`, and a parser that splits on whitespace reads the wrong field for
    any process whose name looks like this.
    """
    entry = root / str(pid)
    entry.mkdir(parents=True)
    # Laid out the way the kernel lays it out: starttime is field 22 overall,
    # and the state character after the comm is field 3 -- so it is the
    # nineteenth field after the state, not the nineteenth field of the tail.
    fields = ["0"] * 50
    fields[18] = str(start)
    entry.joinpath("stat").write_text(
        f"{pid} (claude (pane)) S " + " ".join(fields) + "\n", encoding="utf-8"
    )
    entry.chmod(0o755)
    # st_uid of /proc/<pid> is what the real kernel answers with; here the
    # directory this process just made is owned by whoever runs the suite, so
    # the uid the case wants is supplied through the same accessor the code uses.


def liveness(config, role, *, targets, assignments, proc_root, owner_uid=OWNER_UID, uids=None):
    with patch.object(launcher, "process_owner_uid", lambda pid, proc_root=None: (uids or {}).get(pid, owner_uid)):
        return launcher.pane_liveness(
            config, role,
            tmux_targets=targets,
            assignments=assignments,
            owner_uid=owner_uid,
            proc_root=proc_root,
        )


def test_a_legacy_pane_with_no_argv_marker_is_live() -> None:
    """The live case: the pane has exec'd past the wrapper and is still running."""
    config = config_for(["app"])
    role = config.roles[0]
    with tempfile.TemporaryDirectory(prefix="syrd169-proc.") as tmp:
        proc_root = Path(tmp)
        fake_proc(proc_root, 6149, start=9394)
        state = liveness(
            config, role,
            targets={"syrd-app:0.0"},
            assignments={"app": {
                "actual_target": "syrd-app:0.0", "process_pid": 6149,
                "process_start_time": 9394, "process_uid": OWNER_UID,
            }},
            proc_root=proc_root,
        )
    check(state.live, f"a live legacy pane is live: {state.why}")
    check("6149" in state.why, f"and says what established it: {state.why}")

    # The old proof, on the same pane: its argv no longer carries the marker.
    argv_says = launcher._role_has_pane_process(
        role, ["/usr/bin/claude --dangerously-skip-permissions", "tmux -CC attach"]
    )
    check(not argv_says, "which is exactly what the argv search could not see")


def test_a_newly_launched_pane_is_live_before_it_registers() -> None:
    """Registration is asynchronous, and the wait after this is what checks it."""
    config = config_for(["app"])
    with tempfile.TemporaryDirectory(prefix="syrd169-new.") as tmp:
        state = liveness(
            config, config.roles[0], targets={"syrd-app:0.0"}, assignments={}, proc_root=Path(tmp)
        )
    check(state.live, f"a started pane with no assignment yet is live: {state.why}")
    check("no assignment yet" in state.why, f"and says so: {state.why}")


def test_every_disagreement_is_fail_closed() -> None:
    """A tmux pane is necessary and not sufficient: the board's row has to hold up."""
    config = config_for(["app"])
    role = config.roles[0]
    with tempfile.TemporaryDirectory(prefix="syrd169-closed.") as tmp:
        proc_root = Path(tmp)
        fake_proc(proc_root, 6149, start=9394)
        good = {"actual_target": "syrd-app:0.0", "process_pid": 6149,
                "process_start_time": 9394, "process_uid": OWNER_UID}

        # No pane in the owner's tmux server at all.
        state = liveness(config, role, targets=set(), assignments={"app": good}, proc_root=proc_root)
        check(not state.live and "tmux server" in state.why, f"missing target: {state.why}")

        # The board assigns this role to a different pane.
        state = liveness(
            config, role, targets={"syrd-app:0.0"},
            assignments={"app": dict(good, actual_target="syrd-ops:0.0")}, proc_root=proc_root,
        )
        check(not state.live and "syrd-ops:0.0" in state.why, f"target mismatch: {state.why}")

        # The pid is gone.
        state = liveness(
            config, role, targets={"syrd-app:0.0"},
            assignments={"app": dict(good, process_pid=4242)}, proc_root=proc_root,
        )
        check(not state.live and "is gone" in state.why, f"dead pid: {state.why}")

        # The pid exists but is a DIFFERENT process now.
        state = liveness(
            config, role, targets={"syrd-app:0.0"},
            assignments={"app": dict(good, process_start_time=1)}, proc_root=proc_root,
        )
        check(not state.live and "reused" in state.why, f"reused pid: {state.why}")

        # The process is somebody else's.
        state = liveness(
            config, role, targets={"syrd-app:0.0"}, assignments={"app": good},
            proc_root=proc_root, uids={6149: 0},
        )
        check(not state.live and "uid 0" in state.why, f"wrong owner: {state.why}")

        # The row names no process at all.
        state = liveness(
            config, role, targets={"syrd-app:0.0"},
            assignments={"app": dict(good, process_pid=0)}, proc_root=proc_root,
        )
        check(not state.live and "names no process" in state.why, f"empty assignment: {state.why}")


def test_the_owner_tmux_server_is_the_one_asked() -> None:
    """Root's own tmux server is a different server, and answers about nothing."""
    config = config_for(["app"])
    seen: list[list[str]] = []

    def recording(args, **_kwargs):
        seen.append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout="syrd-app:0.0\nsyrd-ops:0.0\n", stderr="")

    with patch.object(launcher, "current_user_name", lambda: "root"):
        targets, problem = launcher.owner_tmux_targets(config, runner=recording)
    check(targets == {"syrd-app:0.0", "syrd-ops:0.0"}, f"the owner's panes: {targets}")
    check(problem == "", f"and no complaint: {problem}")
    check(seen and seen[0][:3] == ["sudo", "-u", TENANT],
          f"asked as the tenant, not as root: {seen}")
    check("tmux" in seen[0], f"of tmux: {seen}")

    # Already the owner: no dispatch, same question.
    seen.clear()
    with patch.object(launcher, "current_user_name", lambda: TENANT):
        launcher.owner_tmux_targets(config, runner=recording)
    check(seen and seen[0][0] == "tmux", f"no needless sudo when already the owner: {seen}")


def test_a_tmux_that_cannot_be_asked_is_not_an_empty_answer() -> None:
    config = config_for(["app"])

    def failing(args, **_kwargs):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="no server running on /tmp/tmux-1006/default\n")

    targets, problem = launcher.owner_tmux_targets(config, runner=failing)
    check(targets == set() and "could not be asked" in problem, f"said, not assumed: {problem}")
    check("no server running" in problem, f"with what tmux said: {problem}")


def test_readiness_reports_the_reason_and_waits_only_for_live_panes() -> None:
    """The states feed both the problem list and the registration wait."""
    config = config_for(["app", "ops"])
    states = [
        launcher.PaneLiveness("app", True, "tmux holds syrd-app:0.0 and pid 6149 still registered it"),
        launcher.PaneLiveness("ops", False, "process 6202 is gone"),
    ]
    waited: dict[str, object] = {}

    def fake_wait(_config, roles, *, alive, timeout_seconds, print_func):
        waited["alive"] = {role.role: alive(role) for role in roles}
        return launcher.RuntimeRegistrationWait(missing=(), exited=(), problem="", waited_seconds=0.0)

    with tempfile.TemporaryDirectory(prefix="syrd169-ready.") as tmp:
        registry = Path(tmp) / "syrd.json"
        registry.write_text('{"slug": "syrd", "config_path": "%s"}' % (Path(tmp) / "c.json"), encoding="utf-8")
        with patch.object(launcher, "await_runtime_registration", fake_wait):
            problems = launcher.recovery_readiness_problems(
                SimpleNamespace(project="syrd"),
                config,
                Path(tmp) / "c.json",
                registry_path=registry,
                pane_liveness_states=states,
                completion=launcher.PacketCompletion(problems=()),
                print_func=lambda _line: None,
            )
    check(any("ops has no running pane: process 6202 is gone" in line for line in problems),
          f"the reason is carried, not replaced by 'absent': {problems}")
    check(not any(line.startswith("app has no running pane") for line in problems),
          f"and a live pane is not reported missing: {problems}")
    check(waited.get("alive") == {"app": True, "ops": False},
          f"the wait is told which panes are actually up: {waited}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"team_launcher_pane_liveness_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
