#!/usr/bin/env python3
"""SYRD-777: as root, a release close reads the board's build id from `/api/build`, so a board of any size closes.

On 2026-11-15 syrd deployed `95600e79`, served it, and could not close its release phase: the close, as root, read the
build id from the whole `/api/board` -- 26,773,697 bytes on syrd, nearly all of it tickets -- through SYRD-769's
`board_identity.board_opener`, whose answers are capped at 8 MiB, and recorded the phase blocked ("syrd's board answered
/api/board with more than 8388608 bytes"). The board has answered `/api/build`, the build alone, at any size since
SYRD-703. Now the close asks that, over the same kernel-proven unit socket and fixed route, and `/api/board` is no
longer one of root's routes.

Here the real close (`team_launcher.close_release_phase`, as root, with no opener) runs against
`release_phase_journal_test`'s disposable tenant and SYRD-769's rig (`root_board_readers_test.Rig`): the unit's board on
the socket root's unit names, proven by SO_PEERCRED, and foreign boards on the configured socket and URL. Every board's
`/api/board` is over the cap.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import release_phase_journal_test as journal  # noqa: E402 - SYRD-117's disposable tenant, journals and pin
import root_board_readers_test as rbr  # noqa: E402 - SYRD-769's rig: root's unit tree, real sockets, kernel peer check
from scripts import release_alignment, team_launcher  # noqa: E402
from scripts.ticket_board import board_identity  # noqa: E402

CHECKS = 0
DEPLOYED, OTHER = journal.DEPLOYED, journal.OTHER
journal.PROJECT = rbr.PROJECT  # one tenant: the rig's unit names the project the tenant's configuration does
#: syrd's shape: a snapshot over root's answer cap, nearly all tickets, its build id the board's own.
WHOLE_BOARD = {"build_id": DEPLOYED,
               "tickets": [{"id": f"SYRD-{n}", "body": "x" * 1000} for n in range(9 * 1024)]}
#: What each board answers on `/api/build`; a test sets it.
BUILD: dict[str, object] = {}


def check(condition: object, detail: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")


def answers(name: str) -> dict:
    return {
        "/api/build": BUILD.get(name, {"build_id": DEPLOYED}),
        "/api/board": WHOLE_BOARD,
        "/api/workflow": {"document": {"roles": [], "migrations": {"director_onboarding": True}, "said_by": name},
                          "revision": 3},
        "/api/runtime-assignments": {"project": rbr.PROJECT, "authority_mode": "process", "assignments": {}},
    }


rbr.answers = answers


def close(rig: rbr.Rig, tmp: str) -> tuple[int, str, str, dict]:
    """The real close, as root with no opener given: its exit, what it printed, root's release phase, its record."""
    tenant = journal.tenant_in(tmp)
    payload = json.loads(tenant.config_path.read_text(encoding="utf-8"))
    payload.update(board_url=rig.foreign_url.where, board_socket=rig.foreign_socket.where)
    tenant.config_path.write_text(json.dumps(payload), encoding="utf-8")
    tenant.config = team_launcher.load_project_config(rbr.PROJECT, tenant.config_path)
    tenant.pin(DEPLOYED)
    printed: list[str] = []
    code = journal.as_root(team_launcher.close_release_phase, tenant.config, config_path=tenant.config_path,
                           print_func=printed.append)
    return code, "\n".join(printed), tenant.trusted_release(), tenant.trusted()["phases"]["release"]


def test_the_whole_board_is_over_the_cap() -> None:
    size = len(json.dumps(WHOLE_BOARD).encode())
    check(size > board_identity.MAX_ANSWER_BYTES, f"the stand-in's /api/board is over root's cap: {size} bytes")


def test_a_large_board_closes_against_its_build() -> None:
    BUILD.clear()
    with rbr.Rig() as rig, tempfile.TemporaryDirectory(prefix="syrd777.") as tmp:
        code, said, phase, record = close(rig, tmp)
        check(code == 0 and phase == "done", f"a board over the cap closes its release phase: {code} {phase} {said}")
        check(DEPLOYED in record.get("detail", "") and "live board build" in record.get("detail", ""),
              f"against the build the board reports: {record}")
        check("/api/build" in rig.unit.asked and "/api/board" not in rig.unit.asked,
              f"the unit's board is asked /api/build, never the whole /api/board: {rig.unit.asked}")
        check(rig.foreign_asked() == [], f"nothing the configuration names is asked: {rig.foreign_asked()}")


def test_a_wrong_build_answer_still_refuses() -> None:
    cases = [
        ("another build", {"build_id": OTHER}, "not serving what was deployed", OTHER),
        ("no build id", {}, "did not report a build id", "reported no build id"),
        ("a build id that is not text", {"build_id": 7}, "did not report a build id", "reported no build id"),
        ("not a mapping", [DEPLOYED], "did not report a build id", "reported no build id"),
        ("an escape-laden build id", {"build_id": rbr.ESCAPE + DEPLOYED}, "not serving what was deployed", DEPLOYED),
    ]
    for label, answer, said_here, recorded in cases:
        BUILD.clear()
        BUILD["unit"] = answer
        with rbr.Rig() as rig, tempfile.TemporaryDirectory(prefix="syrd777.") as tmp:
            code, said, phase, record = close(rig, tmp)
            check(code == 1 and phase == "blocked" and said_here in said,
                  f"{label}: the close refuses and records it blocked: {code} {phase} {said}")
            check(recorded in record.get("detail", ""), f"{label}: saying why: {record}")
            shown = rbr.controls([*said.splitlines(), record.get("detail", "")])
            check(shown == [], f"{label}: as plain text, line by line: {shown}")
            check(rig.unit.asked.count("/api/build") >= 1 and "/api/board" not in rig.unit.asked
                  and rig.foreign_asked() == [],
                  f"{label}: and nothing else is asked instead: {rig.unit.asked} {rig.foreign_asked()}")
    BUILD.clear()
    no_route = b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
    with rbr.Rig(unit_raw=no_route) as rig, tempfile.TemporaryDirectory(prefix="syrd777.") as tmp:
        code, said, phase, record = close(rig, tmp)
        check(code == 1 and phase == "blocked" and "answered /api/build with HTTP 404" in said
              and "/api/board" not in rig.unit.asked and rig.foreign_asked() == [],
              f"a board that does not answer /api/build is refused, not asked the whole board instead: "
              f"{code} {phase} {said} {rig.unit.asked}")


def test_not_root_reads_the_configured_board_build() -> None:
    BUILD.clear()
    with rbr.Rig() as rig:
        board_identity.geteuid = lambda: 1000
        got = release_alignment._live_board_build_id(rig.config())
        check(got == (DEPLOYED, "") and rig.foreign_url.asked == ["/api/build"] and rig.unit.asked == [],
              f"not root, the configured url's /api/build, as before but for the route: {got} {rig.foreign_url.asked}")


def test_root_asks_no_whole_board() -> None:
    check("/api/board" not in board_identity.ROOT_READ_ROUTES and board_identity.BUILD_PATH in board_identity.ROOT_READ_ROUTES,
          f"root's routes are bounded ones: {board_identity.ROOT_READ_ROUTES}")
    with rbr.Rig() as rig:
        got = rbr.caught(lambda: board_identity.board_opener(rig.config())("http://x/api/board"))
        check("refusing to ask" in str(got) and rig.unit.asked == [],
              f"as root, the whole board is refused by name on any board, and nothing is asked: {got!r}")


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"release_close_build_route_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
