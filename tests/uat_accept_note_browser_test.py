#!/usr/bin/env python3
"""SYRD-838, in a real browser: Accept keeps the note typed in the UAT panel's box, and the Director is told it.

A production-built board and the real server, served as production serves it (HTTP acts as the User only), with
Chromium driving the real page, on a declared board and a legacy one:

* main first, from a git archive: Accept with a note in the box records the acceptance, and the note is nowhere --
  no User comment, nothing in the Director's notice;
* Accept with a note asks "Accept this ticket? Your note will be posted as a comment for the Director.", posts
  exactly one User comment with that text, signs off, and the Director's notice for the accepted ticket says
  "the User accepted it, with a note: ..."; the draft is cleared;
* Accept with an empty box asks what it always asked, posts nothing, and its notice carries no note -- not even the
  User's earlier comment on that ticket;
* a comment write that fails leaves the ticket unaccepted, says why in the panel, and keeps the note in the box;
  Accept again, once it can be written, goes through and the refusal is gone;
* a note longer than Ask Director takes is said to fit Accept as it is typed; the comment keeps all of it and the
  notice its first 4,000 characters, pointing at the comment;
* Send to Director's message is not read as an acceptance's note.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path

BEFORE = "99328ed0a1a059f9fee453590e0c831b7a304536"
CHECKS = 0
A = "a" * 40
NOTE = "Accepted. Small follow-up: plain Ctrl-V should paste text too."
EARLIER = "Question for the Director: which terminal, mine or the tenant's?"
REFUSED_NOTE = "Accepted; REFUSE-THIS-NOTE until the store is back."
LONG_NOTE = "Long note. " + "observation " * 400  # past ask_director's 4,000 characters
SENT = "It said stopped after a restart."


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def board_case(root: Path, declared: bool, fixed: bool = True) -> None:
    for extra in (str(root / "tests"), str(root)):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import ticket_board_write_api_test as fixture
    from assigned_implementation_queue_test import build_board, workflow
    from board_ui_user_record import trust_board_ui_users
    from playwright.sync_api import sync_playwright
    from temporary_cluster import temporary_cluster

    from scripts.ticket_board import ui_delegation, uat_instructions

    ui_delegation.TRUST_HEADER_ROLES = False  # production: HTTP is the User's
    phase = ("declared" if declared else "legacy") + ("" if fixed else ", main")
    with temporary_cluster(prefix="b838", shutdown="immediate") as cluster:
        admin = build_board(fixture, cluster, root, "board")
        app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, "board",
                                                                   fixture.SERVICE_ROLE))
        if declared:
            app.apply_workflow(workflow(root), expected_revision=0, dry_run=False, caller_role="director")

        def sql(statement: str) -> str:
            return fixture.psql(admin, statement).strip()

        def enter_review(ticket: str) -> None:
            fixture.seed_postgres_ticket(admin, ticket, title=f"Otto status check {ticket}", state="dat",
                                         assignee="director", needs_audit=True, audit_signoff=True,
                                         needs_user_signoff=True, commit_hash=A)
            uat_instructions.perform(app, "set_uat_instructions", {"do_this": "Run `switchyard status otto`.",
                                                                   "you_should_see": "`otto: running`"},
                                     caller_role="director", ticket_id=ticket)
            if declared:
                app.perform_workflow_action(ticket, "director_dat_sign_off", {}, caller_role="director")
            else:
                app.update_ticket(ticket, {"state": "user_review"}, caller_role="director")
            check(sql(f"SELECT state FROM ticket_board.tickets WHERE id='{ticket}';") == "user_review",
                  f"{phase}: {ticket} is in front of the User")

        def user_comments(ticket: str) -> list[str]:
            listed = sql(f"SELECT string_agg(text, E'\\x1f' ORDER BY position) FROM ticket_board.ticket_comments "
                         f"WHERE ticket_id='{ticket}' AND who='user';")
            return listed.split("\x1f") if listed else []

        def director_notices(ticket: str) -> list[str]:
            listed = sql(f"SELECT string_agg(message, E'\\x1f' ORDER BY id) FROM ticket_board.ticket_notification_queue "
                         f"WHERE ticket_id='{ticket}' AND target_role='director' AND kind='transition';")
            return listed.split("\x1f") if listed else []

        tickets = ["PGU-1"] if not fixed else ["PGU-1", "PGU-2", "PGU-3", "PGU-4", "PGU-5"]
        for ticket in tickets:
            enter_review(ticket)
        if fixed:
            # A comment of the User's from before the decision: never the acceptance's note.
            sql(f"SELECT ticket_board.append_ticket_comment('PGU-2', 'user', $c${EARLIER}$c$);")
            # A comment store that refuses one note, as a failed write would.
            sql("CREATE FUNCTION public.refuse_note() RETURNS trigger LANGUAGE plpgsql AS $f$ BEGIN "
                "IF NEW.text LIKE '%REFUSE-THIS-NOTE%' THEN RAISE EXCEPTION 'the comment store is unavailable'; END IF; "
                "RETURN NEW; END $f$; CREATE TRIGGER refuse_note BEFORE INSERT ON ticket_board.ticket_comments "
                "FOR EACH ROW EXECUTE FUNCTION public.refuse_note();")
        notices_before = {ticket: len(director_notices(ticket)) for ticket in tickets}
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
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                errors: list[str] = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}/", wait_until="domcontentloaded")
                page.locator(".card-id", has_text="PGU-1").first.wait_for(timeout=10000)

                def wait_for(predicate, seconds: float = 10.0) -> bool:
                    end = time.monotonic() + seconds
                    while time.monotonic() < end:
                        if predicate():
                            return True
                        page.wait_for_timeout(100)
                    return predicate()

                def open_ticket(ticket: str) -> str:
                    page.keyboard.press("Escape")
                    page.locator(".card", has=page.locator(".card-id", has_text=ticket)).first.click()
                    panel = f"[data-uat-panel='{ticket}']"
                    page.locator(f"{panel} [data-uat-decision='accept']").wait_for(timeout=10000)
                    return panel

                def decide(panel: str, decision: str, text: str) -> dict:
                    box = page.locator(f"{panel} [data-uat-message]")
                    box.fill(text)
                    page.locator(f"{panel} [data-uat-decision='{decision}']").click()
                    dialog = page.locator(".board-ui-dialog")
                    dialog.wait_for(timeout=5000)
                    seen = {"question": dialog.locator("h3").inner_text(),
                            "summary": dialog.locator("[data-board-ui-summary]").inner_text()}
                    dialog.locator("[data-board-ui-confirm]").click()
                    return seen

                def draft(ticket: str):
                    return page.evaluate("(id) => state.detailDraft && state.detailDraft.ticketId === id"
                                         " ? (state.detailDraft.fields.uatMessage ?? null) : null", ticket)

                # -- Accept with a note --------------------------------------------------------------------------
                panel = open_ticket("PGU-1")
                seen = decide(panel, "accept", NOTE)
                check(wait_for(lambda: sql("SELECT user_signoff FROM ticket_board.tickets WHERE id='PGU-1';") == "t"),
                      f"{phase}: the acceptance is recorded")
                check(wait_for(lambda: len(director_notices("PGU-1")) > notices_before["PGU-1"]),
                      f"{phase}: and the Director is told")
                notice = director_notices("PGU-1")[-1]
                if not fixed:
                    check(user_comments("PGU-1") == [], f"{phase}: the note is posted nowhere: {user_comments('PGU-1')}")
                    check(NOTE not in notice, f"{phase}: and the Director's notice does not carry it: {notice}")
                    browser.close()
                    return
                check(seen["question"] == "Accept this ticket? Your note will be posted as a comment for the Director.",
                      f"{phase}: the confirmation says what happens to the note: {seen['question']!r}")
                check(f"Your note: {NOTE}" in seen["summary"], f"{phase}: and shows it: {seen['summary']!r}")
                check(user_comments("PGU-1") == [NOTE], f"{phase}: exactly one User comment, the note: "
                      f"{user_comments('PGU-1')}")
                check(notice.endswith(f": the User accepted it, with a note: {NOTE}"),
                      f"{phase}: the Director's notice for the accepted ticket carries the note: {notice!r}")
                check(notice.startswith("PGU-1 -- Otto status check PGU-1"), f"{phase}: on its own notice: {notice!r}")
                check(wait_for(lambda: draft("PGU-1") is None), f"{phase}: the draft is cleared: {draft('PGU-1')!r}")

                # -- Accept with an empty box -------------------------------------------------------------------
                panel = open_ticket("PGU-2")
                seen = decide(panel, "accept", "")
                check(seen["question"] == "Accept this ticket?", f"{phase}: an empty box asks as before: {seen['question']!r}")
                check("Your note" not in seen["summary"], f"{phase}: with no note in it: {seen['summary']!r}")
                check(wait_for(lambda: sql("SELECT user_signoff FROM ticket_board.tickets WHERE id='PGU-2';") == "t"),
                      f"{phase}: the acceptance is recorded")
                check(user_comments("PGU-2") == [EARLIER], f"{phase}: and nothing is posted: {user_comments('PGU-2')}")
                check(wait_for(lambda: len(director_notices("PGU-2")) > notices_before["PGU-2"]),
                      f"{phase}: the Director is told")
                notice = director_notices("PGU-2")[-1]
                check("with a note" not in notice and EARLIER not in notice,
                      f"{phase}: with no note, and not the User's earlier comment: {notice!r}")

                # -- a comment write that fails -----------------------------------------------------------------
                panel = open_ticket("PGU-3")
                decide(panel, "accept", REFUSED_NOTE)
                refusal = f"{panel} [data-uat-refusal]"
                check(wait_for(lambda: page.locator(refusal).count() == 1 and page.locator(refusal).is_visible()
                               and "the comment store is unavailable" in page.locator(refusal).inner_text()),
                      f"{phase}: the panel says the acceptance was refused, and why: "
                      f"{page.locator(refusal).all_inner_texts()}")
                check(page.locator(refusal).inner_text() == "Accept refused: the comment store is unavailable",
                      f"{phase}: naming Accept, without the database's trace: {page.locator(refusal).inner_text()!r}")
                check(sql("SELECT state || '/' || user_signoff FROM ticket_board.tickets WHERE id='PGU-3';")
                      == "user_review/false", f"{phase}: the ticket is not accepted")
                check(user_comments("PGU-3") == [], f"{phase}: nothing is posted: {user_comments('PGU-3')}")
                check(len(director_notices("PGU-3")) == notices_before["PGU-3"], f"{phase}: the Director is told nothing")
                check(page.locator(f"{panel} [data-uat-message]").input_value() == REFUSED_NOTE,
                      f"{phase}: the note is still in the box")
                # The board redraws the open ticket on any change; the refusal is still said after it.
                box = f"{panel} [data-uat-message]"
                page.locator(box).evaluate("el => { el.dataset.drawn = 'before'; }")
                sql("SELECT ticket_board.append_ticket_comment('PGU-1', 'director', $c$Another ticket changed.$c$);")
                check(wait_for(lambda: page.locator(box).count() == 1 and page.locator(box).get_attribute("data-drawn") is None),
                      f"{phase}: the open ticket is redrawn when the board changes")
                check(page.locator(refusal).is_visible()
                      and page.locator(refusal).inner_text() == "Accept refused: the comment store is unavailable",
                      f"{phase}: and the refusal is kept across it: {page.locator(refusal).all_inner_texts()}")
                sql("DROP TRIGGER refuse_note ON ticket_board.ticket_comments;")
                # A decision that goes through ends the refusal: Ask Director keeps the ticket, and the panel.
                page.locator(box).fill("Is the comment store back?")
                page.locator(f"{panel} .uat-ask").click()
                page.locator(".board-ui-dialog").wait_for(timeout=5000)
                page.locator(".board-ui-dialog [data-board-ui-confirm]").click()
                check(wait_for(lambda: page.locator(f"{panel} [data-uat-thread]").count() == 1),
                      f"{phase}: a question goes through")
                check(page.locator(refusal).count() == 1 and not page.locator(refusal).is_visible(),
                      f"{phase}: and the refusal is no longer said: {page.locator(refusal).all_inner_texts()}")
                decide(panel, "accept", REFUSED_NOTE)
                check(wait_for(lambda: sql("SELECT user_signoff FROM ticket_board.tickets WHERE id='PGU-3';") == "t"),
                      f"{phase}: Accept again, once the note can be written, goes through")
                check(user_comments("PGU-3") == ["Question for the Director: Is the comment store back?", REFUSED_NOTE],
                      f"{phase}: with the note: {user_comments('PGU-3')}")

                # -- a note longer than a question ----------------------------------------------------------------
                panel = open_ticket("PGU-4")
                page.locator(f"{panel} [data-uat-message]").fill(LONG_NOTE)
                limit = page.locator(f"{panel} [data-uat-limit]")
                check(wait_for(lambda: limit.is_visible() and "Accept and Send to Director take all of it"
                               in limit.inner_text()), f"{phase}: the box says Accept takes a long note: "
                      f"{limit.all_inner_texts()}")
                decide(panel, "accept", LONG_NOTE)
                check(wait_for(lambda: sql("SELECT user_signoff FROM ticket_board.tickets WHERE id='PGU-4';") == "t"),
                      f"{phase}: a long note is accepted")
                check(user_comments("PGU-4") == [LONG_NOTE.strip()], f"{phase}: the comment keeps all of it")
                check(wait_for(lambda: len(director_notices("PGU-4")) > notices_before["PGU-4"]),
                      f"{phase}: the Director is told")
                notice = director_notices("PGU-4")[-1]
                check(f"with a note: {LONG_NOTE.strip()[:4000]} ... (the whole note is the User's comment on the ticket)"
                      in notice and LONG_NOTE.strip() not in notice,
                      f"{phase}: the notice carries its first 4,000 characters and points at the comment: "
                      f"{notice[:120]!r} ... {notice[-120:]!r}")

                # -- Send to Director's message is no note --------------------------------------------------------
                panel = open_ticket("PGU-5")
                decide(panel, "send", SENT)
                check(wait_for(lambda: SENT in user_comments("PGU-5")), f"{phase}: the message is sent")
                check(wait_for(lambda: sql("SELECT state FROM ticket_board.tickets WHERE id='PGU-5';") != "user_review"),
                      f"{phase}: with the ticket")
                sent = director_notices("PGU-5")[notices_before["PGU-5"]:]
                check(sql("SELECT user_signoff FROM ticket_board.tickets WHERE id='PGU-5';") == "f"
                      and not any("accepted it" in message for message in sent),
                      f"{phase}: and it is not read as an acceptance's note: {sent}")
                check(errors == [], f"{phase}: no page errors: {errors}")
                browser.close()
        finally:
            server.shutdown()
            server.server_close()


def schema_mirrors_the_migration(root: Path) -> None:
    """Every board runs every migration, so the migration's copy is what runs; schema.sql's must be the same."""
    import re

    def body(path: Path) -> str:
        text = path.read_text()
        starts = [m.start() for m in re.finditer(r"CREATE OR REPLACE FUNCTION ticket_board\.notify_ticket_state_transition\(\)", text)]
        return text[starts[-1]:text.index("\n$$;", starts[-1]) + 4] if starts else ""

    migrations = sorted((root / "scripts/ticket_board/migrations").glob("pgu*.sql"),
                        key=lambda path: int(re.match(r"pgu(\d+)", path.name).group(1)))
    defining = [path for path in migrations if body(path)]
    check(defining[-1].name == "pgu1049_syrd838_accept_note_in_notice.sql",
          f"the newest definition of the transition trigger is SYRD-838's: {defining[-1].name}")
    check(body(defining[-1]) == body(root / "scripts/ticket_board/schema.sql"),
          "schema.sql carries the migration's copy of the trigger, byte for byte")


def main_first() -> None:
    """Main, from a git archive, in a process of its own: the bug, as the User met it."""
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="t838-") as scratch:
        tree = Path(scratch) / "main"
        tree.mkdir()
        archive = subprocess.run(["git", "-C", str(root), "archive", BEFORE], check=True, capture_output=True).stdout
        archive_path = Path(scratch) / "main.tar"
        archive_path.write_bytes(archive)
        with tarfile.open(archive_path) as bundle:
            bundle.extractall(tree, filter="data")
        result = subprocess.run([sys.executable, "-I", str(Path(__file__).resolve()), "--main", str(tree)],
                                capture_output=True, text=True, timeout=900, env=os.environ.copy())
        check(result.returncode == 0, f"main's case ran: {result.stdout[-2000:]} {result.stderr[-3000:]}")
        check("main: the note is dropped" in result.stdout, f"main dropped the note: {result.stdout[-500:]}")


def main(argv: list[str]) -> int:
    if argv[:1] == ["--main"]:
        board_case(Path(argv[1]), declared=True, fixed=False)
        print(f"main: the note is dropped ({CHECKS} checks)")
        return 0
    root = Path(__file__).resolve().parents[1]
    main_first()
    schema_mirrors_the_migration(root)
    board_case(root, declared=True)
    board_case(root, declared=False)
    print(f"uat_accept_note_browser_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
