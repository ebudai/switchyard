#!/usr/bin/env python3
"""SYRD-598, in a real browser: the board UI acts as the User, and a Director operation is a confirmed action.

A production-built board (schema.sql, migrations, rbac.sql), the real app and
the real HTTP server, served as production serves it -- HTTP acts as the User
only -- and Chromium driving the real page: the controls the server describes,
the two-step confirmation, success, a cancelled dialog, a refusal for a ticket
that changed after it was reviewed, and recovery after the reload.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def load_playwright():
    from playwright.sync_api import sync_playwright
    return sync_playwright


def board_case(declared: bool) -> dict:
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import ticket_board_write_api_test as fixture
    from assigned_implementation_queue_test import build_board, workflow
    from temporary_cluster import temporary_cluster

    from scripts.ticket_board import ui_delegation

    ui_delegation.TRUST_HEADER_ROLES = False  # production: HTTP is the User's
    seen: dict = {}
    with temporary_cluster(prefix="b598", shutdown="immediate") as cluster:
        admin = build_board(fixture, cluster, ROOT, "board")
        app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, "board", fixture.SERVICE_ROLE))
        if declared:
            app.apply_workflow(workflow(ROOT), expected_revision=0, dry_run=False, caller_role="director")
        for n, title in ((1, "Route me"), (2, "Change me under the dialog"), (3, "Merge source"), (4, "Merge destination")):
            fixture.seed_postgres_ticket(admin, f"PGU-{n}", title=title, state="analysis", assignee="director",
                                         commit_exempt=True)
        # Root's record of the approved board UI user. Chromium runs as this process's account -- the board
        # service here, never a board UI user -- so its page stands in for the approved account's;
        # board_ui_delegation_test's identity cases connect under real, different uids.
        from board_ui_user_record import trust_board_ui_users
        registry = cluster.root / "registry"
        trust_board_ui_users(registry, [{"account": "board-ui-user", "uid": 4242424}], peer_uid=4242424)
        server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        threading.Thread(target=server.serve_forever, daemon=True).start()

        def sql(statement: str) -> str:
            return fixture.psql(admin, statement).strip()

        def state(ticket: str) -> str:
            return sql(f"SELECT state || '/' || assignee || '/' || manually_controlled FROM ticket_board.tickets WHERE id = '{ticket}';")

        sync_playwright = load_playwright()
        with sync_playwright() as playwright:
            launch: dict = {"headless": True}
            if Path("/opt/google/chrome/chrome").is_file():  # as ticket_board_tenant_identity_browser_test does
                launch["executable_path"] = "/opt/google/chrome/chrome"
            browser = playwright.chromium.launch(**launch)
            page = browser.new_page(viewport={"width": 1280, "height": 1000})
            page.goto(f"http://127.0.0.1:{server.server_port}/", wait_until="domcontentloaded")
            page.locator(".card-id", has_text="PGU-1").first.wait_for(timeout=10000)

            def open_ticket(ticket: str) -> None:
                page.keyboard.press("Escape")
                page.locator(".card", has=page.locator(".card-id", has_text=ticket)).first.click()
                page.locator(f"[data-board-ui-actions='{ticket}']").wait_for(timeout=10000)

            def status_text() -> str:
                return page.locator("#createStatus").inner_text(timeout=2000) if page.locator("#createStatus").count() else ""

            def wait_for(predicate, seconds: float = 10.0) -> bool:
                end = time.monotonic() + seconds
                while time.monotonic() < end:
                    if predicate():
                        return True
                    page.wait_for_timeout(100)
                return predicate()

            open_ticket("PGU-1")
            seen["no_role_picker"] = page.locator("select[aria-label='Comment author']").count() == 0
            seen["comments_as_user"] = page.locator("[data-comment-author='user']").count() == 1
            seen["no_director_edit"] = page.locator("[data-director-edit-apply]").count() == 0
            seen["no_gate_toggles"] = (page.get_by_role("checkbox", name="Needs audit").count() == 0
                                       and page.get_by_role("checkbox", name="Needs inspection").count() == 0)
            page.locator("[data-board-ui-op='route']").wait_for(timeout=10000)
            seen["offered"] = sorted(page.locator("[data-board-ui-actions='PGU-1'] [data-board-ui-op]").evaluate_all(
                "els => els.map(e => e.getAttribute('data-board-ui-op'))"))
            # Route: the fields, then the confirmation of the ticket as it is now, then done as the User.
            page.locator("[data-board-ui-op='route']").click()
            dialog = page.locator("[data-board-ui-dialog='route']")
            dialog.wait_for(timeout=5000)
            if declared:
                dialog.locator("[data-board-ui-field='assignee']").select_option("main")
            else:
                dialog.locator("[data-board-ui-field='state']").select_option("in_progress")
                dialog.locator("[data-board-ui-field='assignee']").select_option("main")
            dialog.locator("[data-board-ui-review]").click()
            summary = dialog.locator("[data-board-ui-summary]")
            summary.wait_for(timeout=5000)
            seen["summary"] = summary.inner_text()
            seen["authority_line"] = dialog.locator(".board-ui-authority").inner_text()
            dialog.locator("[data-board-ui-confirm]").click()
            seen["routed"] = wait_for(lambda: state("PGU-1").startswith("in_progress/main"))
            seen["route_record"] = sql("SELECT who || ': ' || left(text, 60) FROM ticket_board.ticket_comments "
                                       "WHERE ticket_id = 'PGU-1' AND text LIKE 'Through the board UI%';")
            # A cancelled dialog changes nothing and spends nothing.
            open_ticket("PGU-2")
            page.locator("[data-board-ui-op='reassign']").click()
            page.locator("[data-board-ui-dialog='reassign'] [data-board-ui-cancel]").click()
            seen["cancelled"] = [state("PGU-2"), sql("SELECT count(*) FROM ticket_board.ui_delegations WHERE ticket_id = 'PGU-2';")]
            # Reviewed, then the ticket changes before the User confirms: refused plainly, the board reloaded.
            # A panel action, which has nothing else to reload the board when it is refused.
            page.locator("[data-board-ui-op='route']").click()
            stale = page.locator("[data-board-ui-dialog='route']")
            if not declared:
                stale.locator("[data-board-ui-field='state']").select_option("in_progress")
            stale.locator("[data-board-ui-field='assignee']").select_option("main")
            stale.locator("[data-board-ui-review]").click()
            stale.locator("[data-board-ui-summary]").wait_for(timeout=5000)
            fixture.psql(admin, "SET session_replication_role = replica; "
                                "UPDATE ticket_board.tickets SET title = 'Changed under the dialog' WHERE id = 'PGU-2';")
            stale.locator("[data-board-ui-confirm]").click()
            seen["stale_refused"] = wait_for(lambda: "changed since it was shown" in status_text())
            seen["stale_state"] = state("PGU-2")
            # The refusal replaced the stale view: the board shows the ticket as it is now, unasked.
            # (The refusal is shown before the reload lands; nothing else refreshes this board within the wait.)
            seen["stale_view_replaced"] = wait_for(
                lambda: page.locator(".card", has_text="Changed under the dialog").count() > 0, seconds=3.0)
            # After the reload the same action, confirmed again, is done.
            open_ticket("PGU-2")
            page.get_by_role("checkbox", name="Manual control").check()
            hold = page.locator("[data-board-ui-dialog='set_manually_controlled']")
            hold.locator("[data-board-ui-confirm]").click()
            seen["held_after_reload"] = wait_for(lambda: state("PGU-2").endswith("/true"))
            # Merge: confirmed with both tickets by ID and title.
            open_ticket("PGU-3")
            page.locator("[aria-label='Merge target ticket']").fill("PGU-4")
            page.get_by_role("button", name="Merge Into…").click()
            merge = page.locator("[data-board-ui-dialog='merge']")
            merge.locator("[data-board-ui-summary]").wait_for(timeout=5000)
            seen["merge_summary"] = merge.locator("[data-board-ui-summary]").inner_text()
            merge.locator("[data-board-ui-confirm]").click()
            seen["merged"] = wait_for(lambda: state("PGU-3").split("/")[0] in ("done", "cancelled"))
            # The User's own edit needs no confirmation, and the board takes it as the User's.
            open_ticket("PGU-4")
            page.get_by_role("textbox", name="Short issue title").fill("Merge destination, renamed")
            page.get_by_role("button", name="Save Title").click()
            seen["renamed"] = wait_for(lambda: sql("SELECT title FROM ticket_board.tickets WHERE id = 'PGU-4';")
                                       == "Merge destination, renamed")
            # A blocker added, then the last one removed, through the real server and database: the reason
            # is kept while anything is awaited and goes with the last blocker, as the board requires.
            open_ticket("PGU-4")
            page.locator(".detail-modal select", has=page.locator("option[value='PGU-1']")).select_option("PGU-1")
            page.locator(".detail-modal").get_by_role("button", name="Add Blocker").click()
            page.locator("[data-board-ui-dialog='set_blockers'] [data-board-ui-confirm]").click()
            seen["blocked"] = wait_for(lambda: app.get_ticket("PGU-4")["blocked_by"] == ["PGU-1"])
            open_ticket("PGU-4")
            page.locator(".detail-modal").get_by_role("button", name="Remove Blocker PGU-1").click()
            page.locator("[data-board-ui-dialog='set_blockers'] [data-board-ui-confirm]").click()
            seen["unblocked"] = wait_for(lambda: (app.get_ticket("PGU-4")["blocked_by"],
                                                  app.get_ticket("PGU-4")["blocked_reason"]) == ([], ""))
            seen["page_requests_as"] = page.evaluate("ticketWriteHeaders('director')['X-Ticket-Board-Caller-Role']")
            # A page whose account is not a board UI user is offered none of the Director's operations, and is
            # told why; the User's own actions stay.
            trust_board_ui_users(registry, [], peer_uid=4242424)
            page.reload(wait_until="domcontentloaded")
            page.locator(".card-id", has_text="PGU-2").first.wait_for(timeout=10000)
            open_ticket("PGU-2")
            note = page.locator("[data-board-ui-unavailable]")
            note.wait_for(timeout=10000)
            seen["not the user"] = [note.inner_text(),
                                    page.locator("[data-board-ui-actions='PGU-2'] [data-board-ui-op]").count()]
            browser.close()
        server.shutdown()
        server.server_close()
    return seen


def main() -> int:
    for declared in (True, False):
        kind = "declared" if declared else "legacy"
        s = board_case(declared)
        print(f"OBSERVED {kind} " + json.dumps(s), file=sys.stderr)
        check(s["no_role_picker"] and s["comments_as_user"] and s["no_director_edit"] and s["no_gate_toggles"]
              and s["page_requests_as"] == "user",
              f"{kind}: the page comments and acts as the User, with no role picker, Director Edit or gate toggle: {s}")
        check({"route", "reassign"} <= set(s["offered"]) and "force_move" not in s["offered"],
              f"{kind}: the actions are the ones the server describes: {s['offered']}")
        check("Ticket: PGU-1 — Route me" in s["summary"] and "assigned to" in s["summary"] and "main" in s["summary"].lower()
              and "recorded as your decision" in s["authority_line"],
              f"{kind}: the confirmation shows the concrete ticket, action and target, and whose decision it is: "
              f"{s['summary']} / {s['authority_line']}")
        check(s["routed"] and s["route_record"].startswith("user: Through the board UI the User confirmed route"),
              f"{kind}: the confirmed route is done and recorded as the User's: {s['route_record']}")
        check(s["cancelled"] == ["analysis/director/false", "0"],
              f"{kind}: a cancelled dialog changes nothing and mints nothing: {s['cancelled']}")
        check(s["stale_refused"] and s["stale_state"].endswith("/false") and s["held_after_reload"]
              and s["stale_view_replaced"],
              f"{kind}: a ticket that changed after review is refused with a clear message, and the reloaded board "
              f"can do it: {s['stale_refused']}, {s['stale_state']}, {s['held_after_reload']}")
        check(s["merged"] and "PGU-3 (Merge source) into PGU-4 (Merge destination)" in s["merge_summary"],
              f"{kind}: a merge is confirmed with both tickets: {s['merge_summary']}")
        check(s["renamed"], f"{kind}: the User's own edit is taken as the User's, without a confirmation")
        check("no approved board UI user is recorded" in s["not the user"][0] and "an operator must configure"
              in s["not the user"][0] and s["not the user"][1] == 0,
              f"{kind}: a page that is not the board UI user's is offered no Director operation, and says why: "
              f"{s['not the user']}")
        check(s["blocked"] and s["unblocked"],
              f"{kind}: a blocker is added and the last one removed through confirmations the board accepts: "
              f"{s['blocked']}, {s['unblocked']}")
    print(f"board_ui_delegation_browser_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
