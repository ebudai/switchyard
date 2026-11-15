#!/usr/bin/env python3
"""SYRD-783: `switchyard new` says once why its model lists are only recorded ones, not with every role's question.

The User, creating test13 after SYRD-778: "in the middle of it, getting a lot of (UNVERIFIED: recorded 2026-11-15
(catalog v2) and not checked against this account; the test13-agent account does not exist yet, so its own list could
not be read; this choice is confirmed against it after the account is created)". `switchyard new` asks every role's
model before the agent account exists, and that sentence was printed before each role's list -- and Router's, and a
Librarian's.

Now it is said once, before the first model question, in one line; each list is then marked `(recorded)`, so a
recorded choice is still never presented as one the account listed (SYRD-250), and it is still confirmed against the
account once that exists. With an account that exists, nothing changes.

The real role plan (`_prompt_switchyard_role_plan`) runs with Router and a Librarian chosen, scripted and in a child
on a real pty, so the real `input` reads each answer from a terminal.
"""

from __future__ import annotations

import json
import os
import pty
import pwd
import select
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT), str(ROOT / "tests")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from scripts import team_launcher  # noqa: E402
from scripts.ticket_board import runtime_catalog  # noqa: E402

CHECKS = 0
NOBODY = "syrd783-nobody"
NOTE = "model lists come from Switchyard's recorded catalog"


def check(condition: object, detail: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")


def answer(prompt: str) -> str:
    """Enter everywhere, with Router and a Librarian turned on."""
    if prompt.startswith("Router (optional)"):
        return "on"
    if prompt.startswith("Documentation gate"):
        return "librarian"
    return ""


def plan(owner: str) -> tuple[tuple, list[str], list[str]]:
    asked: list[str] = []
    said: list[str] = []
    chosen = team_launcher._prompt_switchyard_role_plan(
        runner=None, owner_user=owner, owner_home=Path("/home") / owner,
        input_func=lambda prompt: asked.append(prompt) or answer(prompt), print_func=said.append)
    return chosen, asked, said


def test_the_note_is_said_once_before_the_first_model_question() -> None:
    chosen, asked, said = plan(NOBODY)
    notes = [line for line in said if NOTE in line]
    check(len(notes) == 1, f"one note for the whole run: {notes}")
    check(notes[0] == f"switchyard: {NOTE} (v{runtime_catalog.CATALOG_VERSION}, {runtime_catalog.CATALOG_RECORDED}); "
                      f"{NOBODY} does not exist yet, so each choice is confirmed against it after the account is created",
          f"in one line, naming the catalog and the account: {notes[0]}")
    first_model = next(i for i, line in enumerate(said) if line.startswith("director model:")
                       or line.startswith("designer model:"))
    check(said.index(notes[0]) < first_model, "said before the first model question")
    check(not any("UNVERIFIED" in line or "does not exist yet, so its own list" in line for line in said),
          f"no list repeats the long sentence: {[line for line in said if 'UNVERIFIED' in line][:2]}")
    marks = [line for line in said if line.strip() == "(recorded)"]
    models = [prompt for prompt in asked if " model [" in prompt]
    check(len(models) == 7 and len(marks) == len(models),
          f"each list -- five roles, Router's and the Librarian's -- is marked recorded, and that is all: "
          f"{len(marks)} marks, {len(models)} model questions")
    roles = {selection.role for selection in chosen}
    check({"designer", "director", "audit", "main", "ops", "router", "librarian"} <= roles,
          f"and every role, Router and the Librarian were still chosen: {sorted(roles)}")


def test_an_account_that_exists_is_unchanged() -> None:
    me = pwd.getpwuid(os.getuid()).pw_name
    _chosen, _asked, said = plan(me)
    check(not any(NOTE in line for line in said) and not any(line.strip() == "(recorded)" for line in said),
          "with an account that exists, no run note and no mark: its lists are said as before")
    check(any("UNVERIFIED: recorded" in line for line in said),
          "a runtime that lists nothing of its own is still said to be unverified there")


def test_a_recorded_list_is_never_presented_as_the_accounts() -> None:
    marked = runtime_catalog.model_catalog("claude", unverified_because=runtime_catalog.SAID_ONCE)
    check(marked.note == "recorded" and not marked.enumerable,
          f"said once, a recorded list is marked recorded, and is not the account's: {marked.note!r}")
    long = runtime_catalog.model_catalog("claude", unverified_because="the x account does not exist yet")
    check(long.note.startswith("UNVERIFIED: recorded") and "the x account does not exist yet" in long.note,
          f"any other reason is still said in full: {long.note!r}")
    live = runtime_catalog.Catalog((runtime_catalog.Choice("m"),), runtime_catalog.PROVENANCE_LIVE, detail="agy models")
    check(live.note == "listed by agy models in this account", f"and a live list is unchanged: {live.note!r}")


def test_the_choice_is_still_confirmed_after_the_account_exists() -> None:
    import owner_model_catalog_test as om

    om.test_the_owner_is_asked_again_once_the_account_exists()
    om.test_a_kept_unlisted_model_is_still_refused()
    check(True, "the post-creation confirmation still asks, and still refuses a model the account does not offer")


CHILD = r'''
import builtins, sys
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/tests"]
from scripts import team_launcher
team_launcher._prompt_switchyard_role_plan(runner=None, owner_user="syrd783-nobody",
                                           owner_home=Path("/home/syrd783-nobody"), input_func=builtins.input)
print("DONE", flush=True)
'''


def test_the_real_prompt_on_a_terminal_says_it_once() -> None:
    lines = "\n\n\n" + "\n" * 15 + "on\n" + "\n" * 3 + "librarian\n" + "\n" * 3
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "DIRECTORCTL", "TMUX"))}
    master, slave = pty.openpty()
    proc = subprocess.Popen([sys.executable, "-c", CHILD, str(ROOT)], stdin=slave, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=env, cwd=str(ROOT))
    os.close(slave)
    os.write(master, lines.encode())
    chunks, deadline = [], time.monotonic() + 120
    while proc.poll() is None and time.monotonic() < deadline:
        ready, _w, _x = select.select([proc.stdout, master], [], [], 0.5)
        if proc.stdout in ready:
            chunks.append(os.read(proc.stdout.fileno(), 65536).decode("utf-8", "replace"))
        if master in ready:
            try:
                os.read(master, 65536)  # the terminal's echo, drained
            except OSError:
                pass
    if proc.poll() is None:
        proc.kill()
    chunks.append(proc.stdout.read().decode("utf-8", "replace"))
    os.close(master)
    out = "".join(chunks)
    check("DONE" in out, f"the run on a terminal finished: {out[-1500:]}")
    check(out.count(NOTE) == 1 and "UNVERIFIED" not in out and out.count("(recorded)") == 7,
          f"on a terminal, the note once and each list marked: {out.count(NOTE)} notes, "
          f"{out.count('(recorded)')} marks, UNVERIFIED {'UNVERIFIED' in out}")


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"catalog_note_once_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
