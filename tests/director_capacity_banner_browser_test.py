#!/usr/bin/env python3
"""SYRD-730, in a real browser: no Director-capacity alert, no strip above the board; a real one, the banner.

A production-built board and the real server, served as production serves it, with Chromium driving the real page at
desktop size and at a phone's. The alert is the board's own answer from `GET /api/director-capacity`, read from an
exhausted episode written as escalation leaves one. Main runs first, from a git archive of its own scripts, examples
and tests, then this tree; the same steps on each:

- no episode: the endpoint answers exactly {"alert": null, "open": null}, the banner keeps `hidden`, and it takes no
  room: the board starts where the banner would have been;
- every alert class the page has, on an element carrying `hidden`, takes no room either: the shared alert rule
  cannot beat `hidden`;
- an exhausted episode: after a reload the banner shows its title, the attempts and the recovery steps, inside the
  window's width;
- the alert cleared, as a normal Director turn clears it: the page's own refresh hides it again, with no strip left.

On main the hidden banner was an empty dark-red box above the stages.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: Main's commit this ticket is cut from: the before-run's tree.
BEFORE = "79f0b1b08fc662403e6b36ddfaf7b9f04707e9b8"
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
TITLE = "Director stopped: its model is at capacity"
#: The banner, the board after it, and how much room the banner takes.
LAYOUT = """() => {
  const banner = document.getElementById('directorCapacityAlert');
  const box = banner.getBoundingClientRect();
  const board = document.getElementById('board').getBoundingClientRect();
  const next = document.getElementById('errorBox');
  return { hidden: banner.hidden, display: getComputedStyle(banner).display, height: box.height, width: box.width,
           right: box.right, text: banner.textContent, children: banner.children.length,
           // The board starts where the banner sits: nothing between the scroll area's top and the board.
           gap: board.top - banner.parentElement.getBoundingClientRect().top - parseFloat(
             getComputedStyle(banner.parentElement).paddingTop || '0'),
           error: next.hidden, viewport: window.innerWidth };
}"""
#: Each alert class the page uses, on a hidden element in the banner's place: the room each takes.
CLASSES = ["alert", "alert alert-blocked", "alert card-alert", "alert card-alert card-alert-blocked"]
HIDDEN_ROOM = """(classes) => classes.map((name) => {
  const probe = document.createElement('div');
  probe.className = name;
  probe.hidden = true;
  probe.textContent = 'probe';
  document.getElementById('boardScroll').prepend(probe);
  const room = [getComputedStyle(probe).display, probe.getBoundingClientRect().height];
  probe.remove();
  return room;
})"""
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def scenario(root: Path, prefix: str) -> dict:
    for extra in (str(root), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import ticket_board_write_api_test as fixture
    from assigned_implementation_queue_test import build_board, workflow
    from board_ui_user_record import trust_board_ui_users
    from playwright.sync_api import sync_playwright
    from temporary_cluster import temporary_cluster

    from scripts.ticket_board import ui_delegation

    ui_delegation.TRUST_HEADER_ROLES = False  # production: HTTP is the User's
    out: dict = {}
    with temporary_cluster(prefix=prefix, shutdown="immediate") as cluster:
        admin = build_board(fixture, cluster, root, "board")
        app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, "board",
                                                                   fixture.SERVICE_ROLE))
        app.apply_workflow(workflow(root), expected_revision=0, dry_run=False, caller_role="director")
        fixture.seed_postgres_ticket(admin, "PGU-1", title="A ticket in a lane", state="in_progress", assignee="app")

        def sql(statement: str) -> str:
            return fixture.psql(admin, statement).strip()

        trust_board_ui_users(cluster.root / "registry", [{"account": "board-ui-user", "uid": 4242424}], peer_uid=4242424)
        server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        board = f"http://127.0.0.1:{server.server_port}/"
        try:
            with sync_playwright() as playwright:
                launch: dict = {"headless": True}
                if Path("/opt/google/chrome/chrome").is_file():
                    launch["executable_path"] = "/opt/google/chrome/chrome"
                browser = playwright.chromium.launch(**launch)
                errors: list[str] = []
                devices = {"desktop": {"viewport": {"width": 1280, "height": 800}},
                           "phone": {"viewport": {"width": 390, "height": 844}, "is_mobile": True, "has_touch": True}}

                def wait_for(page, predicate, seconds: float = 10.0) -> bool:
                    end = time.monotonic() + seconds
                    while time.monotonic() < end:
                        if predicate():
                            return True
                        page.wait_for_timeout(100)
                    return predicate()

                def answered(page, method: str) -> dict:
                    """The page loaded or refreshed, its banner settled on the board's answer; the layout then."""
                    with page.expect_response(lambda r: r.url.endswith("/api/director-capacity")) as response:
                        if method == "load":
                            page.goto(board, wait_until="domcontentloaded")
                        else:
                            page.evaluate("refreshDirectorCapacity()")
                    page.locator(".card-id", has_text="PGU-1").first.wait_for(state="attached", timeout=15000)
                    page.wait_for_timeout(300)  # the answer applied
                    return {"answer": response.value.json(), **page.evaluate(LAYOUT)}

                pages = {}
                for device, options in devices.items():
                    page = browser.new_context(**options).new_page()
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    pages[device] = page
                    out[f"{device} none"] = answered(page, "load")
                    out[f"{device} classes"] = page.evaluate(HIDDEN_ROOM, CLASSES)
                # An exhausted episode, as escalation leaves it: the fixture's own write.
                sql("INSERT INTO ticket_board.director_capacity_episodes (opened_at, runtime, pane, failures, attempts, "
                    "escalated_at, closed_at, close_reason) VALUES (now() - interval '9 minutes', '{}', '{}', "
                    "jsonb_build_array(jsonb_build_object('turn', extract(epoch FROM now() - interval '9 minutes'), "
                    "'observed_at', now() - interval '9 minutes')), (SELECT jsonb_agg(jsonb_build_object('attempt', n, "
                    "'outcome', 'sent')) FROM generate_series(1, 3) n), now(), now(), 'exhausted');")
                for device, page in pages.items():
                    out[f"{device} alert"] = answered(page, "load")
                # A Director turn that ends normally clears it: the page's own refresh hides the banner.
                sql("UPDATE ticket_board.director_capacity_episodes SET cleared_at = now();")
                for device, page in pages.items():
                    out[f"{device} cleared"] = answered(page, "refresh")
                out["errors"] = errors
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
    return out


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "examples", "tests"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(root: Path, prefix: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(PANE_ENV_PREFIXES)}
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(root), prefix],
                           capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL,
                           env={**env, "PYTHONDONTWRITEBYTECODE": "1"})
    assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
    return json.loads(child.stdout.strip().splitlines()[-1])


def none_answer(seen: dict, side: str) -> None:
    check(seen["answer"] == {"alert": None, "open": None}, f"{side}: the board has no alert: {seen['answer']}")
    check(seen["hidden"] and seen["children"] == 0, f"{side}: and the banner is hidden and empty: {seen}")


def before(got: dict) -> None:
    for device in ("desktop", "phone"):
        none_answer(got[f"{device} none"], f"main, {device}")
        check(got[f"{device} none"]["display"] == "grid" and got[f"{device} none"]["height"] > 0,
              f"main, {device}: yet it is a box above the board: {got[f'{device} none']}")


def candidate(got: dict) -> None:
    for device in ("desktop", "phone"):
        for state in ("none", "cleared"):
            seen = got[f"{device} {state}"]
            none_answer(seen, f"{device}, {state}")
            check(seen["display"] == "none" and seen["height"] == 0 and seen["width"] == 0,
                  f"{device}, {state}: the hidden banner takes no room: {seen}")
            check(abs(seen["gap"]) < 1, f"{device}, {state}: the board starts where the banner would be: {seen}")
        check(got[f"{device} classes"] == [["none", 0]] * len(CLASSES),
              f"{device}: no alert class beats hidden: {dict(zip(CLASSES, got[f'{device} classes']))}")
        shown = got[f"{device} alert"]
        check(shown["answer"]["alert"] and shown["answer"]["alert"]["attempts"] == 3,
              f"{device}: an exhausted episode is the board's alert: {shown['answer']}")
        check(not shown["hidden"] and shown["display"] == "grid" and shown["height"] > 0,
              f"{device}: and the banner shows: {shown}")
        check(shown["text"].startswith(TITLE) and "3 automatic retries did not get through" in shown["text"]
              and "Attach to its pane" in shown["text"] and "This clears when a Director turn ends normally." in shown["text"],
              f"{device}: with its title, the attempts and what to do: {shown['text']}")
        check(shown["right"] <= shown["viewport"], f"{device}: inside the window's width: {shown}")
    check(got["errors"] == [], f"no page errors: {got['errors']}")


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), sys.argv[3])))
        return 0
    if os.environ.get("SYRD730_MUTATION_SKIP_BEFORE") != "1":  # a mutant changes this tree only
        with tempfile.TemporaryDirectory(prefix="syrd730-browser-before.", dir="/tmp") as tmp:
            before(run(tree_at(BEFORE, Path(tmp) / "before"), "b730b"))
    candidate(run(ROOT, "b730a"))
    print(f"director_capacity_banner_browser_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
