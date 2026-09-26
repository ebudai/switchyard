#!/usr/bin/env python3
"""SYRD-309: the tmux viewer's boundary with the launcher it came out of.

The viewer's argv builders, layout, re-layout hooks and launch moved into
`scripts/tmux_viewer.py` unchanged. This pins what makes that safe:

- **No cycle.** The module imports nothing of Switchyard's at its top; the
  launcher imports it at its top.
- Every name the launcher's launch and session start, `presentation_controller`,
  `switchyard-viewer-layout` and the suites reach as `team_launcher.<name>` is
  still there and is the very same object, whichever module is imported first.
- **The re-layout helper is the same file.** `viewer_layout_helper_path` is
  resolved from this module's own location; it must still name the
  `switchyard-viewer-layout` beside it in `scripts/`.
- **The launcher's quoting is read when a hook is built,** so the hook a
  viewer installs quotes its command the way the launcher does at that moment.

No tmux is executed: the runner is a recorder.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0

EXPORTED = (
    'configure_tmux_session_options',
    'DEFAULT_VIEWER_COLUMNS',
    'DEFAULT_VIEWER_ROWS',
    'install_viewer_relayout_hook',
    'launch_tmux_viewer_session',
    'tmux_set_history_limit_args',
    'tmux_set_mouse_args',
    'tmux_viewer_observer_hook_args',
    'tmux_viewer_pin_size_args',
    'tmux_viewer_relayout_hook_args',
    'tmux_viewer_select_layout_args',
    'tmux_viewer_set_titles_args',
    'tmux_viewer_set_titles_string_args',
    'tmux_viewer_unpin_size_args',
    'viewer_grid',
    'viewer_layout_helper_path',
    'viewer_layout_string',
    'VIEWER_RELAYOUT_UNAVAILABLE_NOTE',
)

#: What the other modules read through the launcher (checked against their text).
READ_ELSEWHERE = {
    "scripts/presentation_controller.py": (
        "DEFAULT_VIEWER_COLUMNS", "DEFAULT_VIEWER_ROWS", "install_viewer_relayout_hook",
        "tmux_viewer_observer_hook_args", "tmux_viewer_pin_size_args", "tmux_viewer_unpin_size_args",
        "viewer_layout_string",
    ),
    "scripts/switchyard-viewer-layout": ("viewer_layout_string",),
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
        "import sys, scripts.tmux_viewer; "
        "print(sorted(m for m in sys.modules if m.startswith('scripts.') and m != 'scripts.tmux_viewer'))"
    )
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "[]",
          f"and loads no other Switchyard module, the launcher least of all: {result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.tmux_viewer", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.tmux_viewer")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.tmux_viewer as v; "
            f"print(all(getattr(t, n) is getattr(v, n) for n in {EXPORTED!r}))"
        )
        check(result.stdout.strip() == "True",
              f"{' then '.join(order)}: every moved name is the launcher's too: {result.stdout}{result.stderr[-600:]}")


def test_what_other_modules_read_is_exported() -> None:
    for path, names in READ_ELSEWHERE.items():
        text = (ROOT / path).read_text(encoding="utf-8")
        for name in names:
            check(name in text and name in EXPORTED, f"{path} reads {name}, and the launcher exports it")


def test_the_relayout_helper_is_the_one_beside_it() -> None:
    from scripts import tmux_viewer

    helper = tmux_viewer.viewer_layout_helper_path()
    check(helper == (ROOT / "scripts" / "switchyard-viewer-layout").resolve() and helper.is_file(),
          f"the re-layout helper is scripts/switchyard-viewer-layout: {helper}")


def test_a_hook_is_quoted_by_the_launcher_when_it_is_built() -> None:
    from scripts import team_launcher, tmux_viewer

    ran: list[list[str]] = []

    def recorder(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        ran.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    saved = team_launcher._quote_command
    team_launcher._quote_command = lambda argv: "SYRD309-QUOTED " + " ".join(argv[1:3])
    try:
        code = tmux_viewer.install_viewer_relayout_hook("v309", runner=recorder, print_func=lambda line: None)
    finally:
        team_launcher._quote_command = saved
    helper = str((ROOT / "scripts" / "switchyard-viewer-layout").resolve())
    check(code == 0 and len(ran) == 1, f"one hook was installed through the runner: {ran!r}")
    check(ran[0][:6] == ["tmux", "set-hook", "-w", "-t", "v309:0", "window-resized"]
          and "SYRD309-QUOTED" in ran[0][6] and helper in ran[0][6],
          f"the hook's command was quoted by the launcher and names the helper: {ran[0]!r}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"tmux_viewer_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
