#!/usr/bin/env python3
"""SYRD-629, in a real browser: what the User needs to do is at the top of a ticket in User Review.

A production-built board and the real server, served as production serves it
(HTTP acts as the User only), with Chromium driving the real page:

* a ticket with a long body and many comments shows the UAT panel first: Do
  this, You should see, Then choose, who prepared them and when, and the Ask
  Director / Accept / Send to Director controls -- all inside the window,
  without scrolling; commands stay literal and copyable;
* Ask Director asks its own question in its confirmation, leaves the ticket in
  User Review, tells the Director once, and the Director's answer shows beside
  the question;
* Send to Director asks "Send this ticket and your message to the Director?",
  says the Director decides next, and the ticket then shows that the Director
  is reviewing the message;
* Accept says where the ticket goes and who acts next;
* a ticket without structured steps shows the Director's latest explicit UAT
  request, or says the Director must prepare them -- it never invents any;
* a legacy board gets the same panel, through its own actions.
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
A = "a" * 40
DO_THIS = "In a terminal, run:\n```\nswitchyard status otto\n```\nthen open the board's Otto column."
SEE = "The line `otto: running` and no error."


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def board_case(declared: bool) -> None:
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import ticket_board_write_api_test as fixture
    from assigned_implementation_queue_test import build_board, workflow
    from board_ui_user_record import trust_board_ui_users
    from playwright.sync_api import sync_playwright
    from temporary_cluster import temporary_cluster

    from scripts.ticket_board import ui_delegation, uat_instructions

    ui_delegation.TRUST_HEADER_ROLES = False  # production: HTTP is the User's
    phase = "declared" if declared else "legacy"
    with temporary_cluster(prefix="b629", shutdown="immediate") as cluster:
        admin = build_board(fixture, cluster, ROOT, "board")
        app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, "board",
                                                                   fixture.SERVICE_ROLE))
        if declared:
            app.apply_workflow(workflow(ROOT), expected_revision=0, dry_run=False, caller_role="director")

        def sql(statement: str) -> str:
            return fixture.psql(admin, statement).strip()

        def enter_review(ticket: str, *, body: str = "", comments: int = 0) -> None:
            fixture.seed_postgres_ticket(admin, ticket, title=f"Otto status check {ticket}", state="dat",
                                         assignee="director", needs_audit=True, audit_signoff=True,
                                         needs_user_signoff=True, commit_hash=A)
            # The fixture's own write, past the board's triggers: it arranges the ticket, it is not a move.
            sql(f"SET session_replication_role = replica; UPDATE ticket_board.tickets SET body = $b${body}$b$ "
                f"WHERE id = '{ticket}'; SET session_replication_role = origin;")
            for n in range(comments):
                sql(f"SELECT ticket_board.append_ticket_comment('{ticket}', 'main', $c$Implementation note {n}: "
                    + "details " * 30 + "$c$);")
            uat_instructions.perform(app, "set_uat_instructions", {"do_this": DO_THIS, "you_should_see": SEE},
                                     caller_role="director", ticket_id=ticket)
            if declared:
                app.perform_workflow_action(ticket, "director_dat_sign_off", {}, caller_role="director")
            else:
                app.update_ticket(ticket, {"state": "user_review"}, caller_role="director")
            check(sql(f"SELECT state FROM ticket_board.tickets WHERE id='{ticket}';") == "user_review",
                  f"{phase}: {ticket} is in front of the User")

        long_body = "\n".join(f"Background paragraph {n}: " + "context " * 20 for n in range(60))
        enter_review("PGU-1", body=long_body, comments=40)
        # A note of the Director's from before the User asks anything: not an answer.
        sql("SELECT ticket_board.append_ticket_comment('PGU-1', 'director', $c$Routing note: app has this.$c$);")
        enter_review("PGU-2")
        # Tickets already in User Review before this change: seeded there, as they were, with no steps.
        fixture.seed_postgres_ticket(admin, "PGU-3", title="Old ticket, no steps", state="user_review",
                                     assignee="user", needs_user_signoff=True, commit_hash=A)
        fixture.seed_postgres_ticket(admin, "PGU-4", title="Old ticket, steps in a comment", state="user_review",
                                     assignee="user", needs_user_signoff=True, commit_hash=A)
        sql("SELECT ticket_board.append_ticket_comment('PGU-4', 'director', "
            "$c$UAT: open the Otto column and check the card says running.$c$);")
        # A Director comment that is no UAT request is not shown as one.
        sql("SELECT ticket_board.append_ticket_comment('PGU-3', 'director', $c$Routed to app, after PGU-1.$c$);")
        fixture.seed_postgres_ticket(admin, "PGU-6", title="Accepted, not yet moved on", state="user_review",
                                     assignee="user", needs_user_signoff=True, user_signoff=True, commit_hash=A)
        # Steps from an earlier round, no longer current: never shown as what to do.
        fixture.seed_postgres_ticket(admin, "PGU-5", title="Steps from an old round", state="user_review",
                                     assignee="user", needs_user_signoff=True, commit_hash=A)
        sql("INSERT INTO ticket_board.ticket_uat_instructions (ticket_id, do_this, you_should_see, then_choose, "
            "prepared_by, prepared_at, for_commit, superseded_at, superseded_reason) VALUES ('PGU-5', "
            "'Run the old command.', 'The old result.', 'Accept.', 'director', now(), '" + A + "', now(), "
            "'the ticket went back to in_progress');")
        registry = cluster.root / "registry"
        trust_board_ui_users(registry, [{"account": "board-ui-user", "uid": 4242424}], peer_uid=4242424)
        server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as playwright:
                launch: dict = {"headless": True}
                if Path("/opt/google/chrome/chrome").is_file():
                    launch["executable_path"] = "/opt/google/chrome/chrome"
                browser = playwright.chromium.launch(**launch)
                height = 800
                page = browser.new_page(viewport={"width": 1280, "height": height})
                errors: list[str] = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}/", wait_until="domcontentloaded")
                page.locator(".card-id", has_text="PGU-1").first.wait_for(timeout=10000)

                def open_ticket(ticket: str) -> None:
                    page.keyboard.press("Escape")
                    page.locator(".card", has=page.locator(".card-id", has_text=ticket)).first.click()
                    page.locator(f"[data-uat-panel='{ticket}']").wait_for(timeout=10000)

                def wait_for(predicate, seconds: float = 10.0) -> bool:
                    end = time.monotonic() + seconds
                    while time.monotonic() < end:
                        if predicate():
                            return True
                        page.wait_for_timeout(100)
                    return predicate()

                def inside(selector: str) -> bool:
                    box = page.locator(selector).first.bounding_box()
                    return box is not None and box["y"] >= 0 and box["y"] + box["height"] <= height

                def confirm(question: str, needle: str) -> None:
                    dialog = page.locator(".board-ui-dialog")
                    dialog.wait_for(timeout=5000)
                    check(dialog.locator("h3").inner_text() == question,
                          f"{phase}: the confirmation asks {question!r}: {dialog.locator('h3').inner_text()}")
                    check(needle in dialog.locator("[data-board-ui-next]").inner_text(),
                          f"{phase}: and says what happens next: {dialog.locator('[data-board-ui-next]').inner_text()}")
                    dialog.locator("[data-board-ui-confirm]").click()

                # -- the panel comes first, whole, on a long ticket -------------------------------------
                open_ticket("PGU-1")
                panel = "[data-uat-panel='PGU-1']"
                page.locator(f"{panel} [data-uat-decision='send']").wait_for(timeout=10000)
                check(page.locator(".detail-modal [data-uat-panel]").evaluate(
                    "el => el.parentElement.firstElementChild === el"), f"{phase}: the panel is the first thing")
                check(page.locator(f"{panel} .uat-title").inner_text() == "UAT: What you need to do",
                      f"{phase}: under its plain title")
                if declared:
                    # The User's review is the stage's shape, as the board's guard reads it, not its name.
                    shapes = page.evaluate("""() => {
                        const saved = state.workflow;
                        state.workflow = { ...saved, stages: saved.stages.map((stage) => stage.name === 'audit'
                            ? { ...stage, owners: ['user'] } : stage) };
                        const seen = ['audit', 'dat', 'user_review', 'user_feedback'].map((name) => ticketIsUserReview({ state: name }));
                        state.workflow = saved;
                        return seen;
                    }""")
                    check(shapes == [True, False, True, False],
                          f"{phase}: any review stage the User owns is where the panel shows: {shapes}")
                for selector in (f"{panel} [data-uat-part='do_this']", f"{panel} [data-uat-part='you_should_see']",
                                 f"{panel} [data-uat-part='then_choose']", f"{panel} [data-uat-message]",
                                 f"{panel} .uat-ask", f"{panel} [data-uat-decision='accept']",
                                 f"{panel} [data-uat-decision='send']"):
                    # Waited for: the board's second load at start redraws the open ticket, detaching it a moment.
                    check(wait_for(lambda: inside(selector), 5), f"{phase}: {selector} is in the window without scrolling: "
                          f"{page.locator(selector).first.bounding_box()}, modal scrolled "
                          f"{page.locator('.detail-modal').evaluate('el => [el.scrollTop, document.activeElement.tagName, document.activeElement.className]')}")
                check(page.locator(f"{panel} [data-uat-part='do_this'] pre code").inner_text() == "switchyard status otto",
                      f"{phase}: a command block stays literal")
                check(page.locator(f"{panel} [data-uat-part='do_this'] .uat-copy").count() == 1,
                      f"{phase}: and can be copied")
                check(page.locator(f"{panel} [data-uat-part='you_should_see'] code").inner_text() == "otto: running",
                      f"{phase}: inline code too")
                meta = page.locator(f"{panel} [data-uat-meta]").inner_text()
                check(meta.startswith("Prepared by Director") and "last updated" in meta,
                      f"{phase}: who prepared them and when: {meta}")
                if declared:
                    generic = page.locator("[data-board-ui-actions='PGU-1'] [data-board-ui-op]").evaluate_all(
                        "els => els.map(e => e.getAttribute('data-board-ui-op'))")
                    check("user_sign_off" not in generic and "user_send_to_director" not in generic,
                          f"{phase}: and not offered a second time lower down: {generic}")

                # -- Ask Director ------------------------------------------------------------------------
                page.locator(f"{panel} [data-uat-message]").fill("Which terminal: mine or the tenant's?")
                page.locator(f"{panel} .uat-ask").click()
                confirm("Send your question to the Director?", "stays in User Review")
                check(wait_for(lambda: page.locator(f"[data-uat-panel='PGU-1'] [data-uat-thread]").count() == 1),
                      f"{phase}: the question shows in the panel")
                check("Waiting for the Director" in page.locator("[data-uat-panel='PGU-1'] [data-uat-thread]").inner_text(),
                      f"{phase}: waiting for an answer")
                check(wait_for(lambda: page.locator("[data-uat-panel='PGU-1'] [data-uat-message]").input_value() == ""),
                      f"{phase}: and the box is empty again, the question sent")
                check(sql("SELECT state FROM ticket_board.tickets WHERE id='PGU-1';") == "user_review",
                      f"{phase}: the ticket stays in User Review")
                check(sql("SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id='PGU-1' "
                          "AND target_role='director' AND payload->>'kind'='uat_question';") == "1",
                      f"{phase}: and the Director is told once")
                sql("SELECT ticket_board.append_ticket_comment('PGU-1', 'director', $c$Yours: the tenant has no terminal.$c$);")
                page.reload(wait_until="domcontentloaded")
                page.locator(".card-id", has_text="PGU-1").first.wait_for(timeout=10000)
                open_ticket("PGU-1")
                check(wait_for(lambda: "the tenant has no terminal" in
                               page.locator("[data-uat-panel='PGU-1'] [data-uat-thread]").inner_text()),
                      f"{phase}: the Director's answer shows beside the question")
                check("Routing note" not in page.locator("[data-uat-panel='PGU-1'] [data-uat-thread]").inner_text(),
                      f"{phase}: and nothing the Director wrote before the question")

                # -- Send to Director ----------------------------------------------------------------------
                page.locator("[data-uat-panel='PGU-1'] [data-uat-decision='send']").wait_for(timeout=10000)
                page.locator("[data-uat-panel='PGU-1'] [data-uat-message]").fill("It said stopped after a restart.")
                # The board redraws an open ticket on any change; what the User is typing survives it.
                page.locator("[data-uat-panel='PGU-1'] [data-uat-message]").evaluate("el => { el.dataset.drawn = 'before'; }")
                sql("SELECT ticket_board.append_ticket_comment('PGU-2', 'director', $c$Another ticket changed.$c$);")
                check(wait_for(lambda: page.locator("[data-uat-panel='PGU-1'] [data-uat-message]").count() == 1
                               and page.locator("[data-uat-panel='PGU-1'] [data-uat-message]").get_attribute("data-drawn") is None),
                      f"{phase}: the open ticket is redrawn when the board changes")
                check(page.locator("[data-uat-panel='PGU-1'] [data-uat-message]").input_value() == "It said stopped after a restart.",
                      f"{phase}: and the message being typed is still there: "
                      f"{page.locator('[data-uat-panel=PGU-1] [data-uat-message]').input_value()!r}")
                page.locator("[data-uat-panel='PGU-1'] [data-uat-decision='send']").click()
                confirm("Send this ticket and your message to the Director?", "The Director reviews your message"
                        if declared else "reviews your message")
                want = "user_feedback" if declared else "analysis"
                check(wait_for(lambda: sql("SELECT state FROM ticket_board.tickets WHERE id='PGU-1';") == want),
                      f"{phase}: the ticket goes to the Director ({want})")
                if declared:
                    check(wait_for(lambda: page.locator("[data-uat-panel='PGU-1'] .uat-title").count() == 1
                                   and page.locator("[data-uat-panel='PGU-1'] .uat-title").inner_text()
                                   == "UAT: The Director is reviewing your message"),
                          f"{phase}: and says plainly the Director is reviewing the message")
                    check("It said stopped after a restart." in page.locator("[data-uat-panel='PGU-1']").inner_text(),
                          f"{phase}: showing it")

                # -- Accept ------------------------------------------------------------------------------------
                open_ticket("PGU-2")
                page.locator("[data-uat-panel='PGU-2'] [data-uat-decision='accept']").wait_for(timeout=10000)
                page.locator("[data-uat-panel='PGU-2'] [data-uat-decision='accept']").click()
                confirm("Accept this ticket?", "acts next" if declared else "completes it")
                check(wait_for(lambda: sql("SELECT user_signoff FROM ticket_board.tickets WHERE id='PGU-2';") == "t"),
                      f"{phase}: the User's acceptance is recorded")
                # On to the Director's final review: nothing is left for the User here.
                check(wait_for(lambda: page.locator("[data-uat-panel='PGU-2']").count() == 0),
                      f"{phase}: and the panel goes, the ticket at the Director's final review")
                # One accepted but still in User Review, as an older row can be: it says so, and offers nothing.
                open_ticket("PGU-6")
                accepted = page.locator("[data-uat-panel='PGU-6']")
                check(accepted.get_attribute("data-uat-state") == "accepted"
                      and "UAT: Accepted ✓" in accepted.text_content() and accepted.locator("button").count() == 0,
                      f"{phase}: an accepted ticket says it is accepted: {accepted.text_content()[:120]}")

                # -- tickets from before ------------------------------------------------------------------------
                open_ticket("PGU-3")
                check(page.locator("[data-uat-panel='PGU-3']").get_attribute("data-uat-state") == "missing"
                      # The DOM's text: inner_text() reads labels as CSS shows them, upper-cased.
                      and "Director must prepare UAT steps" in page.locator("[data-uat-panel='PGU-3']").text_content(),
                      f"{phase}: no steps: the Director must prepare them, nothing invented")
                check("Routed to app" not in page.locator("[data-uat-panel='PGU-3']").text_content(),
                      f"{phase}: and a Director comment that is no UAT request is not shown as one")
                open_ticket("PGU-5")
                check(page.locator("[data-uat-panel='PGU-5']").get_attribute("data-uat-state") == "missing"
                      and "Run the old command" not in page.locator("[data-uat-panel='PGU-5']").text_content(),
                      f"{phase}: steps no longer current are not shown as what to do")
                open_ticket("PGU-4")
                legacy = page.locator("[data-uat-panel='PGU-4']")
                check(legacy.get_attribute("data-uat-state") == "legacy"
                      and "open the Otto column and check the card says running" in legacy.text_content(),
                      f"{phase}: the Director's explicit UAT request is surfaced: {legacy.inner_text()[:200]}")
                check(errors == [], f"{phase}: no page errors: {errors}")
                browser.close()
        finally:
            server.shutdown()
            server.server_close()


def main() -> int:
    board_case(declared=True)
    board_case(declared=False)
    print(f"uat_panel_browser_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
