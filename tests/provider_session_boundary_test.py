#!/usr/bin/env python3
"""SYRD-299: the foreground provider session's boundary with the launcher it came out of.

The foreground first-run session runner and its terminal ownership moved into
`scripts/provider_session.py` unchanged. This pins what makes that safe:

- **The dependency points one way.** The session imports the pure screen
  classifiers from `scripts/provider_screen.py` and nothing else of
  Switchyard's at its top; the classifiers never import the session back, and
  the launcher is never imported at import time, so there is no cycle.
- Every name the launcher's auth phase and the suites reach as
  `team_launcher.<name>` is still there and is the very same object, whichever
  module is imported first. That includes the timeout and the purposes the
  launcher's own defaults bind when its functions are defined.
- The session reads the screen through the very classifier objects, not copies.
- **The launcher seams still reach the moved code.** The owner's command line
  and the scrubbed environment are read from `team_launcher` when a step runs,
  so a patch there is what the step is started with.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0

EXPORTED = (
    'FOREGROUND_COMPLETION_POLL_SECONDS',
    'FOREGROUND_COMPLETION_TIMEOUT_SECONDS',
    'PROVIDER_READY_QUIET_SECONDS',
    'SETUP_PURPOSE_FIRST_RUN',
    'SETUP_PURPOSE_FOLDER_TRUST',
    'SETUP_PURPOSE_SIGN_IN',
    'SETUP_STEP_CLEAR_SCREEN',
    'SETUP_WINDOW_TITLE_DONE',
    'PtyForegroundSession',
    '_countdown',
    '_RawTerminal',
    '_run_provider_first_run',
    'run_provider_first_run_session',
    'set_terminal_title',
    '_TerminalModeLedger',
)

CLASSIFIERS = ('_draws_something', '_replaced_frame_starts_at', '_screen_is_settled', '_TerminalStream')


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


def loaded_after(module: str) -> list[str]:
    result = python(
        f"import sys, {module}; "
        f"print(sorted(m for m in sys.modules if m.startswith('scripts.') and m != {module!r}))"
    )
    check(result.returncode == 0, f"{module} imports on its own: {result.stderr[-600:]}")
    return eval(result.stdout.strip())


def test_the_dependency_points_one_way() -> None:
    check(loaded_after("scripts.provider_session") == ["scripts.provider_screen"],
          "the session loads the screen classifiers and no other Switchyard module, "
          "the launcher least of all")
    check(loaded_after("scripts.provider_screen") == [],
          "and the classifiers never load the session back")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.provider_session", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.provider_session")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.provider_session as s, scripts.provider_screen as c; "
            f"print(all(getattr(t, n) is getattr(s, n) for n in {EXPORTED!r}), "
            f"all(getattr(s, n) is getattr(c, n) for n in {CLASSIFIERS!r}))"
        )
        check(result.stdout.strip() == "True True",
              f"{' then '.join(order)}: every moved name is the launcher's too, and the session's "
              f"classifiers are provider_screen's: {result.stdout}{result.stderr[-600:]}")


def test_the_launcher_seams_reach_the_step() -> None:
    from scripts import provider_session, team_launcher

    seen: list[tuple[list[str], dict[str, object]]] = []
    argv = ["syrd-299-owner-argv", "--from-the-launcher-seam"]
    env = {"SYRD_299_SCRUBBED": "from-the-launcher-seam"}
    saved = (team_launcher._owner_command_env_args, team_launcher._pane_identity_scrubbed_env)
    team_launcher._owner_command_env_args = lambda owner_user, owner_home, command: list(argv)
    team_launcher._pane_identity_scrubbed_env = lambda: dict(env)
    try:
        with tempfile.TemporaryDirectory(prefix="syrd-299-owner-home.") as home:
            finished = provider_session._run_provider_first_run(
                cli="claude",
                owner_user="syrd-299-no-such-user",
                owner_home=Path(home),
                command=["claude"],
                is_complete=lambda: True,
                watching="the account",
                runner=lambda args, **kwargs: seen.append((args, kwargs)),
            )
            check(finished is True, "an injected runner's step reads the account back")
            check(seen == [(argv, {"cwd": home, "env": env})],
                  f"the step ran with the launcher's patched argv and environment: {seen!r}")
    finally:
        team_launcher._owner_command_env_args, team_launcher._pane_identity_scrubbed_env = saved


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"provider_session_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
