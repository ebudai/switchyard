#!/usr/bin/env python3
"""SYRD-298: the provider-screen classifiers' boundary with the launcher they came out of.

The pure provider screen classifiers moved into `scripts/provider_screen.py`
unchanged. This pins what makes that safe:

- **The module is a leaf.** It imports nothing of Switchyard's, so the
  classifiers cannot pick up launcher state, and the launcher can import it at
  the top with no cycle.
- Every name the launcher's foreground session and the suites reach as
  `team_launcher.<name>` is still there and is the very same object, whichever
  module is imported first.
- The classifiers still read the captured provider screens as before. Against
  the fixtures in `tests/fixtures/claude-first-run`: the theme menu, folder
  trust, the sign-in box and the Codex login wait are questions -- one caught
  by each independent reading -- and the ordinary prompt is settled.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0
SCREENS = ROOT / "tests" / "fixtures" / "claude-first-run"

EXPORTED = (
    'PROVIDER_SELECTION_CURSORS',
    'PROVIDER_PENDING_ANSWER_MARKERS',
    '_visible_text',
    '_draws_something',
    '_replaced_frame_starts_at',
    '_screen_is_settled',
    '_TerminalStream',
    '_provider_screen_offers_a_choice',
    'PROVIDER_WAITING_ON_SIGN_IN_MARKERS',
    'provider_is_waiting_for_an_answer',
)


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


def test_the_module_is_a_leaf() -> None:
    result = python(
        "import sys, scripts.provider_screen; "
        "print(sorted(m for m in sys.modules if m.startswith('scripts.') and m != 'scripts.provider_screen'))"
    )
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "[]", f"and loads no other Switchyard module: {result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.provider_screen", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.provider_screen")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.provider_screen as p; "
            f"print(all(getattr(t, n) is getattr(p, n) for n in {EXPORTED!r}))"
        )
        check(result.stdout.strip() == "True",
              f"{' then '.join(order)}: every moved name is the launcher's too: {result.stdout}{result.stderr[-600:]}")


def test_the_captured_screens_read_as_they_did() -> None:
    from scripts import provider_screen

    def screen(name: str) -> str:
        return (SCREENS / f"{name}.txt").read_text()

    # One screen per independent reading, so dropping any one of them is noticed:
    # the sign-in box is caught by its phrase, the theme menu by its choice
    # structure alone, and the Codex login by the sign-in wait alone.
    for name in ("theme-menu", "folder-trust", "sign-in-box", "codex-login-waiting"):
        check(provider_screen.provider_is_waiting_for_an_answer(screen(name)),
              f"{name} is a question the window must not be closed on")
    ordinary = screen("ordinary-prompt")
    check(not provider_screen.provider_is_waiting_for_an_answer(ordinary), "the ordinary prompt is not a question")
    check(provider_screen._screen_is_settled(ordinary, awaiting_redraw=False),
          "and a finished step may be closed at it")
    check(not provider_screen._screen_is_settled(ordinary, awaiting_redraw=True),
          "but not while a redraw is still owed")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"provider_screen_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
