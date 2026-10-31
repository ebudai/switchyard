#!/usr/bin/env python3
"""SYRD-714: a phone notification's link, `/#ticket=<ID>`, opens that ticket on the board's own page, once.

The real board server and page in headless Chromium, main first (from a git archive of its scripts and tests),
then this tree:

- `/#ticket=PGU-5` opens PGU-5's detail on arrival; on main it opens nothing;
- an ID this board does not hold, or anything not of the form, opens nothing;
- closing the detail and the board refreshing do not reopen it: the link opens once;
- a new link in an open page (a second notification tapped) opens its ticket.
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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "16b8f0681008a1fbd4b4544e21216ffc0c439804"
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def scenario(tree: Path) -> dict:
    sys.path[:0] = [str(tree), str(tree / "tests")]
    import board_card_assignee_browser_test as base
    from scripts.ticket_board import server as server_mod

    assert Path(server_mod.__file__).resolve().is_relative_to(tree.resolve()), server_mod.__file__
    seen: dict = {}
    with tempfile.TemporaryDirectory(prefix="syrd714-link.") as tmp:
        frames, assets = Path(tmp) / "frames", Path(tmp) / "assets"
        frames.mkdir()
        assets.mkdir()
        tickets = [base.ticket_payload("PGU-2", "Implementation task", state="in_progress"),
                   base.ticket_payload("PGU-5", "User task", state="user_review")]
        app = base.StaticBoardApp(tickets, frames, assets)
        server = server_mod.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=base.QuietNotifier())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_port}/"
        opened = "() => document.querySelector('.detail-modal')?.textContent.match(/PGU-[0-9]+/)?.[0] || ''"
        try:
            with base.load_playwright()() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    for name, fragment in (("linked", "#ticket=PGU-5"), ("missing", "#ticket=PGU-99"),
                                           ("garbage", "#ticket=PGU-5%3Cimg%3E"), ("other", "#board=PGU-5"),
                                           ("lowercase", "#ticket=pgu-5")):
                        page = browser.new_page(viewport={"width": 1280, "height": 900})  # a fresh tab, as a tap opens
                        page.goto(url + fragment, wait_until="domcontentloaded")
                        page.locator(".card", has_text="PGU-2").wait_for(timeout=10000)
                        page.wait_for_timeout(300)
                        seen[name] = page.evaluate(opened)
                        page.close()
                    page = browser.new_page(viewport={"width": 1280, "height": 900})
                    page.goto(url + "#ticket=PGU-5", wait_until="domcontentloaded")
                    page.locator(".card", has_text="PGU-2").wait_for(timeout=10000)
                    page.wait_for_timeout(300)
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(300)
                    seen["closed"] = page.evaluate(opened)
                    seen["refreshed"] = page.evaluate("() => requestBoardReload().then(() => true)")
                    page.wait_for_timeout(500)
                    seen["after_refresh"] = page.evaluate(opened)
                    page.evaluate("() => { window.location.hash = '#ticket=PGU-2'; }")
                    page.wait_for_timeout(800)
                    seen["second_link"] = page.evaluate(opened)
                    page.evaluate("() => { window.location.hash = '#ticket=PGU-99'; }")
                    page.wait_for_timeout(800)
                    seen["unknown_link_while_open"] = page.evaluate(opened)
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
    return seen


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "tests"], check=True,
                             capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(tree: Path) -> dict:
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(tree)],
                           capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL,
                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
    return json.loads(child.stdout.strip().splitlines()[-1])


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]))))
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd714-link-before.", dir="/tmp") as tmp:
        main_seen = run(tree_at(BEFORE, Path(tmp) / "before"))
    check(main_seen["linked"] == "", f"main: a ticket link opens nothing: {main_seen}")
    seen = run(ROOT)
    check(seen["linked"] == "PGU-5", f"the link opens its ticket's detail on arrival: {seen}")
    check(seen["missing"] == seen["garbage"] == seen["other"] == seen["lowercase"] == "",
          f"an ID this board does not hold, or anything not of the form, opens nothing: {seen}")
    check(seen["closed"] == "" and seen["refreshed"] is True and seen["after_refresh"] == "",
          f"closed, it stays closed when the board refreshes: the link opens once: {seen}")
    check(seen["second_link"] == "PGU-2", f"a second link in the open page opens its ticket: {seen}")
    check(seen["unknown_link_while_open"] == "PGU-2",
          f"a link to a ticket this board does not hold leaves the open one open: {seen}")
    print(f"ticket_link_browser_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
