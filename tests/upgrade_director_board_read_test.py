#!/usr/bin/env python3
"""SYRD-774: `switchyard upgrade`, as root, reads the Director phase from the board root's unit names, and shows it inert.

`upgrade_phases` asks `project_onboarding.director_onboarding_state` whether the Director's onboarding migration is
done, and records the answer as the "director" phase in root's upgrade record. It read `/api/workflow` from the
tenant configuration's `board_url`, which the tenant can point at a board it controls, and printed a failure's raw
exception text. Now, as root, it asks through `board_identity.board_opener`, as SYRD-769's readers do: the unit's own
socket, proven by the kernel, its fixed route only. Its failure text is plain.

SYRD-769's rig (`root_board_readers_test.Rig`) serves the boards: the unit's on the socket root's unit names, a foreign
one on the configured socket and another on the configured URL. Here the unit's board carries the migration marker and
the foreign ones do not, so the answer says which board was asked.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import root_board_readers_test as rbr  # noqa: E402 - SYRD-769's rig: root's unit tree, real sockets, kernel peer check
from scripts import project_onboarding  # noqa: E402
from scripts.ticket_board import board_identity  # noqa: E402

CHECKS = 0
MARKED = {"migrations": {"director_onboarding": True}}
#: Every source says the tenant declares a workflow, so the phase applies and the board is the question.
PRESENCE = SimpleNamespace(non_declarative_by_design=False, declared_somewhere=True, legacy_without_workflow=False,
                           config_unreadable="")


def check(condition: object, detail: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")


_plain_answers = rbr.answers


def answers(name: str) -> dict:
    """The rig's answers, the unit's workflow document carrying the migration marker and nobody else's."""
    got = _plain_answers(name)
    got["/api/workflow"] = {"document": {**({"roles": []}), **(MARKED if name == "unit" else {}), "said_by": name},
                            "revision": 3}
    return got


rbr.answers = answers


def state(rig: rbr.Rig, *, board_url: str = "") -> object:
    path = rig.tree / "p769.json"
    path.write_text(json.dumps({"project": rbr.PROJECT, "workflow": MARKED}), encoding="utf-8")
    config = rig.config(board_url_override=board_url)
    return rbr.caught(lambda: project_onboarding.director_onboarding_state(config, config_path=path, presence=PRESENCE))


def test_root_asks_the_unit_board_once_and_never_the_configured_one() -> None:
    with rbr.Rig() as rig:
        answer = state(rig)
        check(answer == ("done", ""), f"as root, the unit's board's document -- marked -- decides it: {answer!r}")
        check(rig.unit.asked == ["/api/workflow"], f"the unit's board is asked /api/workflow once: {rig.unit.asked}")
        check(rig.foreign_asked() == [], f"nothing the configuration names is connected to: {rig.foreign_asked()}")


def test_not_root_asks_the_configured_board_as_before() -> None:
    with rbr.Rig() as rig:
        board_identity.geteuid = lambda: 1000
        answer = state(rig)
        check(answer == ("pending", "the board's workflow document does not carry the migration marker")
              and rig.unit.asked == [] and rig.foreign_url.asked == ["/api/workflow"],
              f"not root, the configured URL, as before: {answer!r} {rig.unit.asked} {rig.foreign_url.asked}")


def test_a_tenant_url_cannot_choose_the_route() -> None:
    for suffix in ("/api/image/{tree}/b.png?", "/api/image/{tree}/b.png#", "/?x=", "/api/tickets"):
        with rbr.Rig() as rig:
            url = rig.foreign_url.where + suffix.format(tree=rig.tree)
            answer = state(rig, board_url=url)
            check(rig.unit.asked == ["/api/workflow"] and rig.foreign_asked() == [] and answer == ("done", ""),
                  f"board_url ...{suffix}: the unit's board asked exactly /api/workflow, and it decides: "
                  f"{rig.unit.asked} {rig.foreign_asked()} {answer!r}")


def test_a_refused_or_foreign_answerer_is_unknown_not_done() -> None:
    with rbr.Rig() as rig:
        rig.main_pid = 1
        answer = state(rig)
        check(isinstance(answer, tuple) and answer[0] == "unknown" and "main process" in answer[1]
              and rig.foreign_asked() == [],
              f"the unit's socket answered by another process: unknown, saying why, and nobody else asked: {answer!r}")


def test_a_raw_status_line_or_reason_phrase_prints_inert() -> None:
    with rbr.Rig(unit_raw=rbr.BAD_STATUS) as rig:
        answer = state(rig)
        check(isinstance(answer, tuple) and answer[0] == "unknown" and rbr.controls(answer) == [],
              f"as root, a malformed answer is unknown, said as plain text: {answer!r}")
    for raw in (rbr.BAD_STATUS, rbr.BAD_REASON):
        with rbr.Rig() as rig:
            board_identity.geteuid = lambda: 1000
            rig.foreign_url.raw = raw
            answer = state(rig)
            check(isinstance(answer, tuple) and answer[0] == "unknown" and rbr.controls(answer) == [],
                  f"not root, the board's raw status line or reason phrase is plain text: {answer!r}")


def test_upgrade_asks_it() -> None:
    source = (ROOT / "scripts" / "upgrade_phases.py").read_text(encoding="utf-8")
    check("launcher.director_onboarding_state(" in source,
          "switchyard upgrade asks director_onboarding_state, the reader this ticket fixes")


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"upgrade_director_board_read_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
