#!/usr/bin/env python3
"""SYRD-599: in the board's own web UI, Router's two writes are not offered, and the Director's routes are.

A production-built Router board, the real server and its real page in headless
Chromium. Since SYRD-598 the page is the User's and asserts no role: Router
writes from its own pane, through the board's Unix socket, so a ticket Router
holds shows none of Router's assign-to-owner or return buttons. The Director's
two ways into Implementation -- Route through Router, and Assign directly to a
chosen owner -- are what the board describes for a triage ticket, and the User
confirms each; it then runs through the same HTTP path and checks as the CLI,
under a single-use grant the User's decision is recorded with.
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
from board_ui_user_record import trust_board_ui_users  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

CHECKS = 0
#: The approved board UI user's uid, as root records it; every page connection is taken to be this account's.
UI_UID = 4242599


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


def open_ticket(page, base: str, ticket: str) -> None:
    page.goto(base + "/", wait_until="domcontentloaded")
    settle(page)
    page.locator(".card", has=page.locator(".card-id", has_text=ticket)).first.click()
    page.locator(".detail-modal").wait_for(timeout=5000)
    page.locator(".detail-modal [data-board-ui-op]").first.wait_for(timeout=10000)


def offered(page) -> list[list[str]]:
    """The operations the open ticket's panel offers: operation, variant and label."""
    return page.evaluate("""() => [...document.querySelectorAll('.detail-modal [data-board-ui-op]')].map(
        (b) => [b.getAttribute('data-board-ui-op'), b.getAttribute('data-board-ui-variant') || '', b.textContent.trim()])""")


def confirm(page, variant: str, **choices: str) -> None:
    """Choose the operation, fill its fields, review the concrete values and confirm, as the User does."""
    page.locator(f".detail-modal [data-board-ui-variant='{variant}']").click()
    dialog = page.locator("[data-board-ui-dialog]")
    dialog.locator("[data-board-ui-review], [data-board-ui-confirm]").first.wait_for(timeout=5000)
    for name, value in choices.items():
        dialog.locator(f"[data-board-ui-field='{name}']").select_option(value)
    if dialog.locator("[data-board-ui-review]").count():  # nothing to fill opens on the confirmation itself
        dialog.locator("[data-board-ui-review]").click()
    dialog.locator("[data-board-ui-confirm]").click()
    page.wait_for_function("() => /: done for |refused/.test(document.getElementById('createStatus').textContent)",
                           timeout=10000)


def scenario() -> dict:
    for key in [k for k in os.environ if k.startswith(rb.PANE_ENV_PREFIXES)]:
        os.environ.pop(key)
    seen: dict = {}
    sync_playwright = load_playwright()
    with temporary_cluster(prefix="syrd599b-", shutdown="immediate") as cluster:
        trust_board_ui_users(cluster.root / "registry", [{"account": "syrd599-user", "uid": UI_UID}], peer_uid=UI_UID)
        board = rb.Board(cluster, "browser", rb.example(router=True))
        try:
            for ticket in ("PGU-951", "PGU-953", "PGU-954"):
                rb.fixture.seed_postgres_ticket(board.admin, ticket, title=ticket, state="analysis", assignee="director",
                                                commit_exempt=True)
            board.act("PGU-951", "route", "director", state="in_progress")
            with sync_playwright() as playwright:
                browser = launch(playwright)
                try:
                    page = browser.new_page(viewport={"width": 1400, "height": 1000})
                    errors: list[str] = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    open_ticket(page, board.base, "PGU-951")
                    seen["router_holds"] = board.where("PGU-951")
                    seen["router_held"] = offered(page)
                    seen["router_held_controls"] = enabled_buttons(page)
                    # The Director's two ways into Implementation, as the triage view offers them.
                    open_ticket(page, board.base, "PGU-953")
                    seen["director_triage"] = offered(page)
                    # Confirmed only if offered, so a missing control fails the named check below, not a timeout here.
                    if any(variant == "router" for _op, variant, _label in seen["director_triage"]):
                        confirm(page, "router")
                    seen["through_router"] = board.where("PGU-953")
                    open_ticket(page, board.base, "PGU-954")
                    if any(variant == "direct" for _op, variant, _label in offered(page)):
                        confirm(page, "direct", assignee="ops")
                    seen["assigned_directly"] = board.where("PGU-954")
                    seen["direct_decisions"] = board.sql("SELECT count(*) FROM ticket_board.router_decisions "
                                                         "WHERE ticket_id = 'PGU-954';")
                    seen["grants"] = board.sql(
                        "SELECT string_agg(ticket_id || ':' || decided_by || ':' || authority || ':' "
                        "|| (consumed_at IS NOT NULL)::text, ',' ORDER BY id) FROM ticket_board.ui_delegations;")
                    seen["errors"] = errors
                finally:
                    browser.close()
        finally:
            board.close()
    return seen


def verify(seen: dict) -> None:
    check(seen["router_holds"] == ["in_progress", "router"], f"Router holds the ticket: {seen['router_holds']}")
    router_ops = [entry for entry in seen["router_held"] if "router" in entry[0] or entry[1]]
    labels = set(seen["router_held_controls"])
    check(router_ops == [] and not {label for label in labels if label.startswith("Assign to ")}
          and "Return to triage" not in labels and "Comment author" not in labels,
          f"the page offers none of Router's writes, and no role to write as: {seen['router_held']} {sorted(labels)}")
    triage = {variant: (op, label) for op, variant, label in seen["director_triage"] if variant}
    check(set(triage) == {"router", "direct"} and triage["router"][1] == "Route through Router"
          and triage["direct"][1] == "Assign directly" and triage["router"][0] == triage["direct"][0]
          and not [label for _op, variant, label in seen["director_triage"] if not variant and label == "Route"],
          f"the Director's normal route says Router chooses, beside a distinct direct one: {seen['director_triage']}")
    check(seen["through_router"] == ["in_progress", "router"], f"Route through Router: {seen['through_router']}")
    check(seen["assigned_directly"] == ["in_progress", "ops"] and seen["direct_decisions"] == "0",
          f"Assign directly, to the owner chosen, with no Router decision: {seen['assigned_directly']}")
    check(seen["grants"] == "PGU-953:user:director:true,PGU-954:user:director:true",
          f"each was the User's decision, under a grant of the Director's permission, used once: {seen['grants']}")
    check(seen["errors"] == [], f"the page raised nothing: {seen['errors']}")


def main() -> int:
    verify(scenario())
    print(f"router_role_browser_test: ok ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
