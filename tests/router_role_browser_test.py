#!/usr/bin/env python3
"""SYRD-599: in the board's own web UI, Router can do its two writes and nothing else.

A production-built Router board, the real server and its real page in headless
Chromium. With Router chosen as the author of a ticket Router holds, its
assign-to-owner buttons and `Return to triage` are the only enabled actions;
with the Director chosen, none of Router's are. Each click goes through the
same HTTP path and checks as the CLI.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import router_role_board_test as rb  # noqa: E402
from blocked_by_frontend_browser_test import load_playwright  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def launch(playwright):
    """Playwright's own browser, or the headless shell this host has installed; never a skip."""
    try:
        return playwright.chromium.launch(headless=True)
    except Exception as exc:
        if "Executable doesn't exist" not in str(exc):
            raise
        # A throwaway HOME has no browsers; the account's own home (not $HOME) is where they were installed.
        import pwd
        roots = [Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"])] if os.environ.get("PLAYWRIGHT_BROWSERS_PATH") else []
        roots += [Path.home() / ".cache/ms-playwright", Path(pwd.getpwuid(os.getuid()).pw_dir) / ".cache/ms-playwright"]
        shells = sorted(shell for root in roots for shell in root.glob(
            "chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"))
        if not shells:
            raise
        return playwright.chromium.launch(headless=True, executable_path=str(shells[-1]))


def enabled_buttons(page) -> dict[str, bool]:
    """Every control in the open ticket, by its text or label, and whether it can be used."""
    return page.evaluate("""() => Object.fromEntries([...document.querySelectorAll(
        '.detail-modal button, .detail-modal input, .detail-modal select, .detail-modal textarea')]
        .map((c, i) => [(c.tagName === 'SELECT' ? '' : (c.textContent || '').trim()) || c.getAttribute('aria-label') || c.placeholder
                        || `${c.tagName.toLowerCase()}#${i}`, !c.disabled]))""")


def settle(page) -> None:
    """Wait until the page stops reloading the board: its first load, and the refresh the event stream asks
    for straight after when earlier writes are newer. A click while that re-render swaps the panel is lost."""
    last = {"at": time.monotonic()}

    def note(request) -> None:
        if "/api/board" in request.url:
            last["at"] = time.monotonic()

    page.on("request", note)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() - last["at"] < 1.0 and time.monotonic() < deadline:
            page.wait_for_timeout(100)
    finally:
        page.remove_listener("request", note)


def open_as(page, base: str, ticket: str, author: str) -> None:
    page.goto(base + "/", wait_until="domcontentloaded")
    settle(page)
    page.locator(".card", has=page.locator(".card-id", has_text=ticket)).first.click()
    page.locator(".detail-modal").wait_for(timeout=5000)
    page.get_by_label("Comment author").select_option(author)


def scenario() -> dict:
    for key in [k for k in os.environ if k.startswith(rb.PANE_ENV_PREFIXES)]:
        os.environ.pop(key)
    seen: dict = {}
    sync_playwright = load_playwright()
    with temporary_cluster(prefix="syrd599b-", shutdown="immediate") as cluster:
        board = rb.Board(cluster, "browser", rb.example(router=True))
        try:
            for ticket in ("PGU-951", "PGU-952", "PGU-953", "PGU-954"):
                rb.fixture.seed_postgres_ticket(board.admin, ticket, title=ticket, state="analysis", assignee="director",
                                                commit_exempt=True)
            for ticket in ("PGU-951", "PGU-952"):
                board.act(ticket, "route", "director", state="in_progress")
            with sync_playwright() as playwright:
                browser = launch(playwright)
                try:
                    page = browser.new_page(viewport={"width": 1400, "height": 1000})
                    errors: list[str] = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    open_as(page, board.base, "PGU-951", "router")
                    seen["as_router"] = enabled_buttons(page)
                    page.get_by_label("Comment author").select_option("director")
                    seen["as_director"] = enabled_buttons(page)
                    page.get_by_label("Comment author").select_option("router")
                    page.locator(".detail-modal").get_by_role("button", name="Assign to ops").click()
                    page.wait_for_timeout(500)
                    seen["assigned"] = board.where("PGU-951")
                    open_as(page, board.base, "PGU-952", "router")
                    page.get_by_placeholder("Add a comment or bounce-back note").fill("Needs a Director decision first.")
                    page.locator(".detail-modal").get_by_role("button", name="Return to triage").click()
                    page.wait_for_timeout(500)
                    seen["returned"] = board.where("PGU-952")
                    seen["reason"] = board.sql("SELECT text FROM ticket_board.ticket_comments WHERE ticket_id = 'PGU-952' "
                                               "ORDER BY id DESC LIMIT 1;")
                    # The Director's two ways into Implementation, as the triage view offers them.
                    open_as(page, board.base, "PGU-953", "director")
                    seen["director_triage"] = enabled_buttons(page)
                    # Clicked only if offered, so a missing control fails the named check below, not a timeout here.
                    if seen["director_triage"].get("Route through Router"):
                        page.locator(".detail-modal").get_by_role("button", name="Route through Router").click()
                        page.wait_for_timeout(500)
                    seen["through_router"] = board.where("PGU-953")
                    open_as(page, board.base, "PGU-954", "director")
                    page.get_by_label("Implementation owner").select_option("ops")
                    page.locator(".detail-modal").get_by_role("button", name="Assign directly").click()
                    page.wait_for_timeout(500)
                    seen["assigned_directly"] = board.where("PGU-954")
                    seen["direct_decisions"] = board.sql("SELECT count(*) FROM ticket_board.router_decisions "
                                                         "WHERE ticket_id = 'PGU-954';")
                    seen["errors"] = errors
                finally:
                    browser.close()
        finally:
            board.close()
    return seen


def verify(seen: dict) -> None:
    mine = {"Assign to app", "Assign to main", "Assign to ops", "Return to triage"}
    authoring = {"Comment author", "Add a comment or bounce-back note", "×"}  # and closing the panel
    as_router = seen["as_router"]
    check({label for label, on in as_router.items() if on} == mine | authoring,
          f"Router can use its two writes, and the reason box, and nothing else: "
          f"{sorted(label for label, on in as_router.items() if on)}")
    on_for_director = {label for label, on in seen["as_director"].items() if on}
    check(on_for_director & mine == set() and {"Save Title", "Add Blocker", "Add Comment", "Defer"} <= on_for_director,
          f"none of them is the Director's, whose own controls come back: {sorted(on_for_director)}")
    check(seen["assigned"] == ["in_progress", "ops"], f"the assign button assigns: {seen['assigned']}")
    check(seen["returned"] == ["analysis", "director"]
          and seen["reason"].endswith("Reason: Needs a Director decision first."),
          f"the return button returns, with the reason: {seen['returned']} {seen['reason']}")
    triage = seen["director_triage"]
    check(triage.get("Route through Router") is True and triage.get("Assign directly") is True
          and triage.get("Implementation owner") is True and "Route" not in triage,
          f"the Director's normal route says Router chooses, beside a distinct direct one: {sorted(triage)}")
    check(seen["through_router"] == ["in_progress", "router"], f"Route through Router: {seen['through_router']}")
    check(seen["assigned_directly"] == ["in_progress", "ops"] and seen["direct_decisions"] == "0",
          f"Assign directly, to the owner chosen, with no Router decision: {seen['assigned_directly']}")
    check(seen["errors"] == [], f"the page raised nothing: {seen['errors']}")


def main() -> int:
    verify(scenario())
    print(f"router_role_browser_test: ok ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
