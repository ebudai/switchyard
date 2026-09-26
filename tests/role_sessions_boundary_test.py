#!/usr/bin/env python3
"""SYRD-312: role session start and stop's boundary with the launcher it came out of.

Starting and stopping a role's tmux session moved into
`scripts/role_sessions.py` unchanged. This pins what makes that safe:

- **No cycle.** The module imports nothing of Switchyard's at its top; the
  launcher imports it at its top.
- Every name the launcher's launch/viewer/stop code, `role_command`,
  `tmux_viewer`, `role_identity_cutover` and the suites reach as
  `team_launcher.<name>` is still there and is the very same object,
  whichever module is imported first.
- **The patched start/stop are still reached.** The suites patch
  `_start_role_sessions_without_a_window` and `stop_role_sessions` on the
  launcher: `stop_project` calls the latter by the launcher's name, and the
  cutover calls both through the launcher.
- `hermes_session_isolation` reads `_uses_fresh_session_per_ticket`'s source
  through the launcher; that source is the moved function's own.
- A stop asks the launcher for each role's runner and for the has/kill argv
  when it runs.

No tmux runs: the runner is a recorder.
"""

from __future__ import annotations

import ast
import inspect
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0

EXPORTED = (
    'record_unverified_resume_for_role',
    '_start_role_session',
    '_start_role_sessions_without_a_window',
    'stop_role_sessions',
    'tmux_has_session_by_name_args',
    'tmux_kill_session_by_name_args',
    '_uses_fresh_session_per_ticket',
)
READ_ELSEWHERE = {
    "scripts/role_command.py": ("_uses_fresh_session_per_ticket",),
    "scripts/tmux_viewer.py": ("tmux_has_session_by_name_args", "tmux_kill_session_by_name_args"),
    "scripts/role_identity_cutover.py": ("_start_role_sessions_without_a_window", "stop_role_sessions"),
}


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python(
        "import sys, scripts.role_sessions; "
        "print(sorted(m for m in sys.modules if m.startswith('scripts.') and m != 'scripts.role_sessions'))"
    )
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "[]",
          f"and loads no other Switchyard module, the launcher least of all: {result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.role_sessions", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.role_sessions")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.role_sessions as s; "
            f"print(all(getattr(t, n) is getattr(s, n) for n in {EXPORTED!r}))"
        )
        check(result.stdout.strip() == "True",
              f"{' then '.join(order)}: every moved name is the launcher's too: {result.stdout}{result.stderr[-600:]}")


def test_what_other_modules_read_is_reached_through_the_launcher() -> None:
    for path, names in READ_ELSEWHERE.items():
        text = (ROOT / path).read_text(encoding="utf-8")
        for name in names:
            check(f"launcher.{name}" in text and name in EXPORTED,
                  f"{path} reads {name} through the launcher, which exports it")


def test_the_patched_start_and_stop_are_called_by_the_launchers_name() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    stops = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == "stop_role_sessions"]
    check(len(stops) == 1 and isinstance(stops[0].func, ast.Name),
          "stop_project calls stop_role_sessions at its one baseline site, by the launcher's patchable name")
    moved = ast.parse((ROOT / "scripts" / "role_sessions.py").read_text(encoding="utf-8"))
    bare = sorted({n.id for n in ast.walk(moved) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                   and n.id in ("_start_role_sessions_without_a_window", "stop_role_sessions")})
    check(bare == [], f"and nothing here calls either past the launcher's patch: {bare}")


def test_the_fresh_session_rule_reads_the_same_through_the_launcher() -> None:
    from scripts import role_sessions, team_launcher

    source = inspect.getsource(team_launcher._uses_fresh_session_per_ticket)
    check(source == inspect.getsource(role_sessions._uses_fresh_session_per_ticket)
          and inspect.getsourcefile(team_launcher._uses_fresh_session_per_ticket).endswith("role_sessions.py"),
          "the source the hermes guard reads through the launcher is the moved function's own")
    check("fresh_session_per_ticket" in source and "hermes" not in source,
          "and it still decides by the setting, not by the CLI's name")


def test_a_stop_asks_the_launcher_for_the_runner_and_argv_when_it_runs() -> None:
    from scripts import role_sessions, team_launcher

    ran: list[list[str]] = []
    handed: list[object] = []

    def role_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        ran.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    def caller_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"the stop ran {args!r} on the caller's runner, not the role's")

    said: list[str] = []
    config = SimpleNamespace(roles=[SimpleNamespace(role="main", tmux_session="p312-main")])
    names = ("role_process_runner_for", "tmux_has_session_args", "tmux_kill_session_args")
    saved = {name: getattr(team_launcher, name) for name in names}
    team_launcher.role_process_runner_for = lambda config, role, runner: handed.append(runner) or role_runner
    team_launcher.tmux_has_session_args = lambda role: ["syrd312-has", role.role]
    team_launcher.tmux_kill_session_args = lambda role: ["syrd312-kill", role.role]
    try:
        code = role_sessions.stop_role_sessions(config, runner=caller_runner, print_func=said.append)
    finally:
        for name, value in saved.items():
            setattr(team_launcher, name, value)
    check(code == 0 and ran == [["syrd312-has", "main"], ["syrd312-kill", "main"]],
          f"the stop used the launcher's per-role runner and argv, as patched: {ran!r}")
    check(handed == [caller_runner], "the caller's runner was handed to the launcher to wrap, not used directly")
    check(said == ["stopped main: p312-main"], f"and reported the stop: {said!r}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"role_sessions_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
