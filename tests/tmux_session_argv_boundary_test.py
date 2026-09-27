#!/usr/bin/env python3
"""SYRD-314: tmux session argv and live pane-command matching, against the launcher they came out of.

The session argv builders, pane commands and live pane-command matching moved
into `scripts/tmux_session_argv.py` unchanged. This pins what makes that safe:

- **No cycle.** The module imports nothing of Switchyard's at its top; the
  launcher imports it at its top.
- Every name the launcher and the six modules that read through it reach as
  `team_launcher.<name>` is still there and is the very same object,
  whichever module is imported first.
- **The patched argv builders are reached through the launcher.** The suites
  rebind `tmux_has_session_args` and `tmux_kill_session_args` on the launcher;
  every module that uses them reads them there, and nothing calls them past
  it.
- **Live matching asks the launcher** for the pane's pid and the process
  tree when it runs, and falls back to asking tmux through the runner it was
  given.

Nothing here reads a real pane or process: the lookups are patched and the
runner is a recorder.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0

EXPORTED = ('live_command_matches_role', 'pane_command', 'pane_command_args', 'tmux_has_session_args',
            'tmux_kill_session_args')
READ_ELSEWHERE = {
    "tmux_has_session_args": ("live_role_runtime", "provider_resume", "role_identity_cutover", "role_pane_entry",
                              "role_runtime", "role_sessions", "worker_pool"),
    "tmux_kill_session_args": ("live_role_runtime", "role_pane_entry", "role_runtime", "role_sessions",
                               "worker_pool"),
    "live_command_matches_role": ("provider_resume", "role_identity_cutover", "role_pane_entry"),
    "pane_command_args": ("role_sessions", "worker_pool"),
}
PATCHED = ("tmux_has_session_args", "tmux_kill_session_args")


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python(
        "import sys, scripts.tmux_session_argv; "
        "print(sorted(m for m in sys.modules if m.startswith('scripts.') and m != 'scripts.tmux_session_argv'))"
    )
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "[]",
          f"and loads no other Switchyard module, the launcher least of all: {result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.tmux_session_argv", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.tmux_session_argv")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.tmux_session_argv as a; "
            f"print(all(getattr(t, n) is getattr(a, n) for n in {EXPORTED!r}))"
        )
        check(result.stdout.strip() == "True",
              f"{' then '.join(order)}: every moved name is the launcher's too: {result.stdout}{result.stderr[-600:]}")


def test_every_reader_reaches_its_names_through_the_launcher() -> None:
    for name, modules in READ_ELSEWHERE.items():
        check(name in EXPORTED, f"{name} is exported")
        for module in modules:
            tree = ast.parse((ROOT / "scripts" / f"{module}.py").read_text(encoding="utf-8"))
            uses = [n for n in ast.walk(tree) if (isinstance(n, ast.Attribute) and n.attr == name)
                    or (isinstance(n, ast.Name) and n.id == name)]
            through = [n for n in uses if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                       and n.value.id in ("launcher", "team_launcher")]
            check(uses and through == uses, f"{module} reads {name} through the launcher, and only there")


def test_nothing_here_calls_a_patched_builder_past_the_launcher() -> None:
    tree = ast.parse((ROOT / "scripts" / "tmux_session_argv.py").read_text(encoding="utf-8"))
    bare_calls = sorted({n.func.id for n in ast.walk(tree)
                         if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in PATCHED})
    check(bare_calls == [], f"no patched builder is called here past the launcher: {bare_calls}")


def test_live_matching_asks_the_launcher_when_it_runs() -> None:
    from scripts import team_launcher, tmux_session_argv

    role = SimpleNamespace(live_commands=["/opt/syrd-314/bin/syrd314-cli"], cli=["unused"], target="p314:main.0")
    tree_for: list[int] = []

    def no_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"with a live pane pid the runner must not be asked: {args!r}")

    saved = (team_launcher.pane_pid_for_role, team_launcher.process_tree_command_names)
    team_launcher.pane_pid_for_role = lambda role, runner: 4242
    team_launcher.process_tree_command_names = lambda pid: tree_for.append(pid) or {"syrd314-cli"}
    try:
        matched = tmux_session_argv.live_command_matches_role(role, runner=no_runner)
        team_launcher.process_tree_command_names = lambda pid: {"something-else"}
        unmatched = tmux_session_argv.live_command_matches_role(role, runner=no_runner)
    finally:
        team_launcher.pane_pid_for_role, team_launcher.process_tree_command_names = saved
    check(matched is True and tree_for == [4242],
          "a live pane's process tree, from the launcher's lookups as patched, decides the match")
    check(unmatched is False, "and a tree without the command is not a match")

    asked: list[list[str]] = []

    def recorder(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        asked.append(list(args))
        return subprocess.CompletedProcess(args, 0, "syrd314-cli\n", "")

    saved = team_launcher.pane_pid_for_role
    team_launcher.pane_pid_for_role = lambda role, runner: 0
    try:
        fallback = tmux_session_argv.live_command_matches_role(role, runner=recorder)
    finally:
        team_launcher.pane_pid_for_role = saved
    check(fallback is True and asked == [["tmux", "display-message", "-p", "-t", "p314:main.0",
                                          "#{pane_current_command}"]],
          f"without a pid it asks tmux, through the given runner only: {asked!r}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"tmux_session_argv_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
