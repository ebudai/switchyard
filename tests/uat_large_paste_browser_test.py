#!/usr/bin/env python3
"""SYRD-741, in a real browser: a large paste in the UAT box leaves every control reachable and working.

A production-built board with the shipped workflow and the real server, served as production serves it (HTTP acts as
the User only), with Chromium driving the real page at the User's desktop (2560x1440 CSS px at scale 1.5), a laptop's
1280x800 and a 390x844 phone, upright and turned (844x390, the shortest window). The pasted text is what a failed
`switchyard new` prints, at length: three megabytes of step lines and tracebacks, with unbroken lines thousands of
characters long, pasted through the clipboard as the User did. Main runs first, from a git archive of its own scripts,
examples and tests, then this tree.

On main, Send to Director's confirmation drew the whole message into a dialog centred on the window: tens of thousands
of pixels tall, its Confirm and Cancel far off-screen and the page behind covered. Escape then closed the ticket under
the dialog, taking the pasted text with it, and left the dialog over the page until a reload. Here, on every viewport:

- the paste lands in the box whole, once, and nothing is sent or redrawn away; the page answers at once;
- Ask Director, Accept and Send to Director are reachable and take a click; nothing is wider than the window;
- the box still resizes by its grip, taller up to half the window and never wider; the controls stay a short
  scroll below it;
- each confirmation fits the window, its long value scrolling inside it, and Confirm and Cancel in view; Escape is
  Cancel, and the ticket and the text stay;
- Send to Director sends all three megabytes, once, and the sent message shows in a bounded, scrolling block;
- a question over ask_director's 4000 characters is said to be too long before anything is sent, and the text stays;
  a question of exactly 4000, unbroken lines and all, is asked and shown in a bounded block;
- Accept, with the large text still in the box, accepts.
"""

from __future__ import annotations

import hashlib
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
BEFORE = "5a890473dbdf1ef8ecf1f144b63601bcba61eccc"
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
A = "a" * 40
VIEWPORTS = {
    "desktop": {"viewport": {"width": 2560, "height": 1440}, "device_scale_factor": 1.5},
    "laptop": {"viewport": {"width": 1280, "height": 800}},
    "phone": {"viewport": {"width": 390, "height": 844}, "is_mobile": True, "has_touch": True},
    "phone, turned": {"viewport": {"width": 844, "height": 390}, "is_mobile": True, "has_touch": True},
}
#: ask_director's limit on a question.
QUESTION_MAX = 4000
CHECKS = 0


def failed_new_output(size: int) -> str:
    """What a failed `switchyard new` prints, `size` characters of it: steps, tracebacks and unbroken lines."""
    blob = "stderr: " + "QmFja2luZyBzdG9yZSBjaGVja3N1bSBtaXNtYXRjaA" * 150
    path = "/home/otto/.local/state/switchyard/projects/otto/" + "/".join(f"segment-{n:04d}" for n in range(300))
    lines: list[str] = [blob]
    total = len(blob)
    n = 0
    while total < size:
        n += 1
        line = f"[new] 2026-11-07T16:{n // 60 % 60:02d}:{n % 60:02d}Z step {n}: provisioning role app-{n % 7} ... ok"
        if n % 40 == 0:
            line += ("\nTraceback (most recent call last):\n  File \"/usr/lib/switchyard/scripts/new_project_command.py\","
                     " line 812, in run\n    raise NewProjectError(step)\nNewProjectError: the role account is not ready")
        if n % 25 == 0:
            line += "\n" + (blob if n % 50 == 0 else path)
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)[:size].strip()


BIG = failed_new_output(3_000_000)
QUESTION = ("Why did new stop here?\n" + BIG)[:QUESTION_MAX].strip()
assert len(QUESTION) == QUESTION_MAX and "\n" in QUESTION and max(map(len, QUESTION.split("\n"))) > 2000

#: Whether an element is wholly in the window, unscrolled, and is what a click at its centre lands on.
HIT = """el => { const b = el.getBoundingClientRect();
  if (!b.width || !b.height || b.x < 0 || b.y < 0 || b.right > innerWidth + 0.5 || b.bottom > innerHeight + 0.5) return false;
  const top = document.elementFromPoint(b.x + b.width / 2, b.y + b.height / 2);
  return !!top && (top === el || el.contains(top)); }"""
RECT = "el => { const b = el.getBoundingClientRect(); return [b.x, b.y, b.width, b.height]; }"
#: Nothing wider than the window: the document, the open ticket's scroller and the UAT panel.
FITS = """() => { const body = document.querySelector('.detail-modal-body'); const panel = document.querySelector('[data-uat-panel]');
  return document.documentElement.scrollWidth <= innerWidth && (!body || body.scrollWidth <= body.clientWidth)
    && (!panel || panel.getBoundingClientRect().right <= innerWidth); }"""


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def scenario(root: Path, prefix: str) -> dict:
    for extra in (str(root), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import ticket_board_write_api_test as fixture
    from assigned_implementation_queue_test import build_board, workflow
    from board_ui_user_record import trust_board_ui_users
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
    from temporary_cluster import temporary_cluster

    from scripts.ticket_board import uat_instructions, ui_delegation

    ui_delegation.TRUST_HEADER_ROLES = False  # production: HTTP is the User's
    out: dict = {}
    with temporary_cluster(prefix=prefix, shutdown="immediate") as cluster:
        admin = build_board(fixture, cluster, root, "board")
        app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, "board",
                                                                   fixture.SERVICE_ROLE))
        app.apply_workflow(workflow(root), expected_revision=0, dry_run=False, caller_role="director")

        def sql(statement: str) -> str:
            return fixture.psql(admin, statement).strip()

        tickets = {}
        for number, device in enumerate(VIEWPORTS):
            tickets[device] = (f"PGU-{2 * number + 1}", f"PGU-{2 * number + 2}")
            for ticket in tickets[device]:
                fixture.seed_postgres_ticket(admin, ticket, title=f"New project check {ticket}", state="dat",
                                             assignee="director", needs_audit=True, audit_signoff=True,
                                             needs_user_signoff=True, commit_hash=A)
                uat_instructions.perform(app, "set_uat_instructions", {
                    "do_this": "Run:\n```\nswitchyard new otto\n```\nand paste what it prints if it fails.",
                    "you_should_see": "The line `otto: created` and no error."}, caller_role="director", ticket_id=ticket)
                app.perform_workflow_action(ticket, "director_dat_sign_off", {}, caller_role="director")
        fixture.seed_postgres_ticket(admin, "PGU-99", title="Another ticket", state="backlog", assignee="director")

        def row(ticket: str) -> list:
            return sql(f"SELECT state || '|' || user_signoff::text FROM ticket_board.tickets WHERE id = '{ticket}';").split("|")

        def user_comments(ticket: str) -> list:
            found = sql(f"SELECT md5(text) FROM ticket_board.ticket_comments WHERE ticket_id = '{ticket}' "
                        "AND who = 'user' ORDER BY id;")
            return found.split("\n") if found else []

        def questions(ticket: str) -> list:
            found = sql(f"SELECT md5(question) FROM ticket_board.ticket_uat_questions WHERE ticket_id = '{ticket}' "
                        "ORDER BY id;")
            return found.split("\n") if found else []

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
                for device, options in VIEWPORTS.items():
                    context = browser.new_context(**options)
                    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=board.rstrip("/"))
                    page = context.new_page()
                    errors: list[str] = []
                    page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
                    touch = bool(options.get("has_touch"))
                    height = options["viewport"]["height"]

                    def wait_for(predicate, seconds: float = 10.0) -> bool:
                        end = time.monotonic() + seconds
                        while time.monotonic() < end:
                            if predicate():
                                return True
                            page.wait_for_timeout(100)
                        return predicate()

                    def press(locator) -> None:
                        locator.tap() if touch else locator.click()

                    def scroll_to(selector: str) -> None:
                        # The board redraws an open ticket on any change, detaching what was found a moment ago.
                        for _ in range(20):
                            try:
                                page.locator(selector).scroll_into_view_if_needed(timeout=2000)
                                return
                            except PlaywrightError:
                                page.wait_for_timeout(200)
                        page.locator(selector).scroll_into_view_if_needed()

                    def hit(selector: str) -> bool:
                        # Within a few seconds: a redraw can catch the control between its old and new copy.
                        def now() -> bool:
                            try:
                                found = page.locator(selector)
                                return found.count() == 1 and found.evaluate(HIT, timeout=2000)
                            except PlaywrightError:
                                return False
                        return wait_for(now, 5)

                    def height_of(selector: str):
                        # Once drawn: a block caught mid-redraw measures nothing.
                        found = page.locator(selector)
                        if not wait_for(lambda: found.count() == 1 and found.evaluate(RECT)[3] > 0, 20):
                            return None
                        page.wait_for_timeout(300)
                        return found.evaluate(RECT)[3]

                    def reachable(panel: str) -> dict:
                        # Each control, scrolled to as the User would scroll the ticket, is in the window and takes
                        # the click.
                        found = {}
                        for name, selector in (("ask", ".uat-ask"), ("accept", "[data-uat-decision='accept']"),
                                               ("send", "[data-uat-decision='send']")):
                            scroll_to(f"{panel} {selector}")
                            found[name] = hit(f"{panel} {selector}")
                        return found

                    def answers_ms() -> float:
                        started = time.monotonic()
                        page.evaluate("() => new Promise((done) => requestAnimationFrame(() => setTimeout(done, 0)))")
                        return round((time.monotonic() - started) * 1000)

                    def open_ticket(ticket: str) -> str:
                        page.goto(board + f"#ticket={ticket}", wait_until="domcontentloaded")
                        panel = f"[data-uat-panel='{ticket}']"
                        page.locator(f"{panel} [data-uat-decision='send']").wait_for(timeout=15000)
                        return panel

                    def paste(panel: str, text: str) -> float:
                        box = page.locator(f"{panel} [data-uat-message]")
                        press(box)
                        page.evaluate("text => navigator.clipboard.writeText(text)", text)
                        page.keyboard.press("ControlOrMeta+A")
                        started = time.monotonic()
                        page.keyboard.press("ControlOrMeta+V")
                        wait_for(lambda: box.evaluate("el => el.value.length") == len(text), 20)
                        return round((time.monotonic() - started) * 1000)

                    def dialog_seen() -> dict:
                        dialog = page.locator(".board-ui-dialog")
                        dialog.wait_for(timeout=10000)
                        page.wait_for_timeout(300)
                        return {
                            "rect": dialog.evaluate(RECT),
                            "confirm": hit(".board-ui-dialog [data-board-ui-confirm]"),
                            "cancel": hit(".board-ui-dialog [data-board-ui-cancel]"),
                            "fits": dialog.evaluate("el => el.scrollWidth <= el.clientWidth")
                            and dialog.evaluate(RECT)[2] <= options["viewport"]["width"],
                            "tallest value": max(page.locator(".board-ui-summary li").evaluate_all(
                                "els => els.map((el) => el.getBoundingClientRect().height)")),
                            # Its last words, scrolled to inside the dialog, are in the window and above its buttons.
                            "all readable": dialog.evaluate("""el => { el.scrollTop = el.scrollHeight;
                                const last = el.querySelector('.board-ui-authority').getBoundingClientRect();
                                const buttons = el.querySelector('.board-ui-actions').getBoundingClientRect();
                                return last.top >= 0 && last.bottom <= Math.min(innerHeight, buttons.top) + 0.5; }"""),
                            # An unbroken line wraps: nothing to scroll sideways, on a phone either.
                            "wraps": page.locator(".board-ui-summary li").evaluate_all(
                                "els => els.every((el) => el.scrollWidth <= el.clientWidth)"),
                            # The message's own lines, as written, not run together.
                            "lines kept": page.locator(".board-ui-summary").evaluate(
                                "el => el.innerText.includes('Traceback (most recent call last):\\n  File')"),
                        }

                    def confirm_dialog() -> str:
                        # Pressed as the User would when they can reach it; otherwise clicked from script, to go on.
                        button = page.locator(".board-ui-dialog [data-board-ui-confirm]")
                        if button.evaluate(HIT):
                            press(button)
                            return "pressed"
                        button.evaluate("el => el.click()")
                        return "unreachable"

                    send_id, ask_id = tickets[device]
                    seen: dict = {}

                    # -- the paste, then Send to Director ------------------------------------------------------------
                    panel = open_ticket(send_id)
                    box = page.locator(f"{panel} [data-uat-message]")
                    seen["paste ms"] = paste(panel, BIG)
                    seen["pasted whole"] = box.input_value() == BIG
                    seen["answers ms"] = answers_ms()
                    # The ticket stays where it was drawn: revealing the caret moves nothing behind it.
                    seen["ticket still"] = page.evaluate(
                        "() => scrollY === 0 && document.querySelector('.detail-modal').getBoundingClientRect().top >= 0")
                    seen["nothing sent"] = row(send_id) == ["user_review", "false"] and user_comments(send_id) == []
                    seen["controls"] = reachable(panel)
                    seen["fits"] = page.evaluate(FITS)
                    # The board redraws an open ticket on any change: the pasted text survives it.
                    box.evaluate("el => { el.dataset.drawn = 'before'; }")
                    sql("SELECT ticket_board.append_ticket_comment('PGU-99', 'director', $c$Another ticket changed.$c$);")
                    seen["redrawn"] = wait_for(lambda: page.locator(f"{panel} [data-uat-message]").count() == 1
                                               and box.get_attribute("data-drawn") is None)
                    seen["kept across redraw"] = box.input_value() == BIG
                    limit = page.locator(f"{panel} [data-uat-limit]")
                    seen["limit after redraw"] = limit.count() == 1 and limit.is_visible()
                    # The grip still resizes the box, a little; dragged far, the box stops at half the window.
                    start = box.evaluate(RECT)

                    def drag(by: float) -> float:
                        # Down, and out to the side: the grip may make the box taller, never wider than the panel. The
                        # box scrolled to the top first, as the User would, so its grip is not under the action row.
                        box.evaluate("el => el.scrollIntoView({ block: 'start' })")
                        x0, y0, w0, h0 = box.evaluate(RECT)
                        page.mouse.move(x0 + w0 - 4, y0 + h0 - 4)
                        page.mouse.down()
                        page.mouse.move(x0 + w0 - 4 + by / 2, y0 + h0 - 4 + by, steps=8)
                        page.mouse.up()
                        return box.evaluate(RECT)[3]

                    seen["resized a little"] = round(drag(60) - start[3])
                    seen["resized far"] = round(drag(4000))
                    seen["widened by"] = round(box.evaluate(RECT)[2] - start[2])
                    seen["half window"] = height / 2
                    seen["controls after resize"] = reachable(panel)
                    # How far below the box the last control is: bounded by what the panel holds, not by the paste.
                    send_at, box_at = page.locator(f"{panel} [data-uat-decision='send']").evaluate(RECT), box.evaluate(RECT)
                    seen["box to send"] = round(send_at[1] + send_at[3] - box_at[1] - box_at[3])
                    seen["fits after resize"] = page.evaluate(FITS)
                    scroll_to(f"{panel} [data-uat-decision='send']")
                    press(page.locator(f"{panel} [data-uat-decision='send']"))
                    seen["send dialog"] = dialog_seen()
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(500)
                    seen["after escape"] = {
                        "dialog": page.locator(".board-ui-overlay").count(),
                        "panel": page.locator(panel).count(),
                        "text kept": page.locator(panel).count() == 1 and box.input_value() == BIG,
                    }
                    if seen["after escape"]["dialog"] or not seen["after escape"]["panel"]:
                        # Stuck: what only a reload undid, then the text pasted again.
                        page.reload(wait_until="domcontentloaded")
                        panel = open_ticket(send_id)
                        box = page.locator(f"{panel} [data-uat-message]")
                        paste(panel, BIG)
                    scroll_to(f"{panel} [data-uat-decision='send']")
                    press(page.locator(f"{panel} [data-uat-decision='send']"))
                    page.locator(".board-ui-dialog").wait_for(timeout=10000)
                    seen["send confirm"] = confirm_dialog()
                    wait_for(lambda: row(send_id)[0] == "user_feedback", 30)
                    seen["sent"] = row(send_id)
                    seen["sent comments"] = user_comments(send_id)
                    sent = page.locator(f"{panel} .uat-message > .uat-part-text")
                    seen["sent block"] = height_of(f"{panel} .uat-message > .uat-part-text")
                    seen["sent scrolls"] = sent.count() == 1 and sent.evaluate("el => el.scrollHeight > el.clientHeight")
                    seen["sent fits"] = page.evaluate(FITS)
                    seen["answers ms after send"] = answers_ms()

                    # -- Ask Director: too long, then exactly the limit; then Accept --------------------------------
                    panel = open_ticket(ask_id)
                    box = page.locator(f"{panel} [data-uat-message]")
                    paste(panel, BIG)
                    limit = page.locator(f"{panel} [data-uat-limit]")
                    seen["limit"] = limit.inner_text() if limit.count() == 1 and limit.is_visible() else None
                    press(page.locator(f"{panel} .uat-ask"))
                    page.wait_for_timeout(800)
                    seen["too long"] = {"dialog": page.locator(".board-ui-overlay").count(), "asked": questions(ask_id),
                                        "text kept": box.input_value() == BIG}
                    seen["limit in view"] = limit.count() == 1 and limit.is_visible() and limit.evaluate(HIT)
                    seen["limit spoken"] = limit.count() == 1 and limit.get_attribute("aria-live") == "polite"
                    if seen["too long"]["dialog"]:
                        page.locator(".board-ui-dialog [data-board-ui-cancel]").evaluate("el => el.click()")
                        wait_for(lambda: page.locator(".board-ui-overlay").count() == 0)
                    paste(panel, QUESTION)
                    seen["limit at the limit"] = limit.count() == 1 and limit.is_visible()
                    press(page.locator(f"{panel} .uat-ask"))
                    seen.update({"ask dialog": None, "ask confirm": None, "question block": None, "question fits": None})
                    if wait_for(lambda: page.locator(".board-ui-dialog").count() == 1, 5):
                        seen["ask dialog"] = dialog_seen()
                        seen["ask confirm"] = confirm_dialog()
                        wait_for(lambda: len(questions(ask_id)) == 1, 20)
                        seen["question block"] = height_of(f"{panel} [data-uat-thread] > .uat-part-text")
                        seen["question fits"] = page.evaluate(FITS)
                        wait_for(lambda: page.locator(f"{panel} [data-uat-message]").count() == 1
                                 and page.locator(f"{panel} [data-uat-message]").input_value() == "")
                    seen["asked"] = questions(ask_id)
                    paste(panel, BIG)
                    scroll_to(f"{panel} [data-uat-decision='accept']")
                    seen["accept in view"] = hit(f"{panel} [data-uat-decision='accept']")
                    press(page.locator(f"{panel} [data-uat-decision='accept']"))
                    seen["accept dialog"] = dialog_seen()
                    seen["accept confirm"] = confirm_dialog()
                    wait_for(lambda: row(ask_id)[1] == "true", 20)
                    seen["accepted"] = row(ask_id)
                    # With no dialog open, Escape closes the ticket again, as it always has.
                    page.keyboard.press("Escape")
                    seen["escape closes the ticket"] = wait_for(lambda: page.locator(".detail-modal [data-uat-panel]").count() == 0
                                                                and page.evaluate("() => !state.detailOpen"), 5)
                    seen["errors"] = errors
                    out[device] = seen
                    try:
                        context.close()
                    except PlaywrightError:
                        pass
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
                           capture_output=True, text=True, timeout=1500, stdin=subprocess.DEVNULL,
                           env={**env, "PYTHONDONTWRITEBYTECODE": "1"})
    assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
    return json.loads(child.stdout.strip().splitlines()[-1])


def before(got: dict) -> None:
    for device in VIEWPORTS:
        seen = got[device]
        check(not seen["send dialog"]["confirm"] and not seen["send dialog"]["cancel"],
              f"main, {device}: Send to Director's Confirm and Cancel are out of reach: {seen['send dialog']}")
        check(seen["after escape"]["dialog"] == 1 and seen["after escape"]["panel"] == 0,
              f"main, {device}: Escape closes the ticket under the dialog and leaves the dialog: {seen['after escape']}")


def candidate(got: dict) -> None:
    sent_md5 = md5(BIG)
    for device, options in VIEWPORTS.items():
        seen = got[device]
        height = options["viewport"]["height"]
        said = f"{device}: {json.dumps(seen)[:1500]}"
        check(seen["pasted whole"] and seen["nothing sent"], f"{device}: the paste lands whole, and sends nothing: {said}")
        check(seen["ticket still"], f"{device}: and the open ticket stays where it was: {said}")
        check(seen["answers ms"] < 3000 and seen["answers ms after send"] < 3000,
              f"{device}: the page answers at once: {seen['answers ms']}, {seen['answers ms after send']} ms")
        check(seen["controls"] == {"ask": True, "accept": True, "send": True} and seen["fits"],
              f"{device}: Ask Director, Accept and Send to Director are reachable, nothing wider: {said}")
        check(seen["redrawn"] and seen["kept across redraw"], f"{device}: the text survives a redraw: {said}")
        check(seen["limit after redraw"], f"{device}: and is still said to be too long for a question")
        check(seen["resized a little"] >= 40, f"{device}: the box still resizes by its grip: {seen['resized a little']}")
        check(seen["resized far"] <= seen["half window"] + 1 and seen["widened by"] == 0,
              f"{device}: up to half the window, and never wider: {seen['resized far']} {seen['widened by']}")
        check(seen["controls after resize"] == {"ask": True, "accept": True, "send": True} and seen["fits after resize"]
              and 0 < seen["box to send"] <= 400,
              f"{device}: and the controls stay a short scroll below it: {seen['controls after resize']} "
              f"{seen['box to send']}")
        check(not seen["limit at the limit"], f"{device}: exactly 4000 characters is not too long")
        for name in ("send dialog", "ask dialog", "accept dialog"):
            dialog = seen[name]
            check(dialog is not None, f"{device}: the {name} opens")
            x, y, w, h = dialog["rect"]
            check(dialog["confirm"] and dialog["cancel"] and dialog["fits"] and dialog["all readable"] and dialog["wraps"]
                  and y >= 0 and y + h <= height + 0.5,
                  f"{device}: the {name} fits the window, Confirm and Cancel in reach: {dialog}")
        check(seen["send dialog"]["tallest value"] < height / 2 and seen["send dialog"]["lines kept"],
              f"{device}: the long message scrolls inside the confirmation, line by line: {seen['send dialog']}")
        check(seen["after escape"] == {"dialog": 0, "panel": 1, "text kept": True},
              f"{device}: Escape is Cancel; the ticket and the text stay: {seen['after escape']}")
        check(seen["send confirm"] == "pressed" and seen["sent"] == ["user_feedback", "false"]
              and seen["sent comments"] == [sent_md5],
              f"{device}: Send to Director sends all of it, once: {seen['send confirm']} {seen['sent']} {seen['sent comments']}")
        check(seen["sent block"] is not None and seen["sent block"] <= height * 0.4 + 1 and seen["sent scrolls"]
              and seen["sent fits"], f"{device}: the sent message shows in a bounded, scrolling block: {seen['sent block']}")
        check(seen["limit"] == f"This is {len(BIG):,} characters. Ask Director takes 4,000 at most; "
              "Send to Director takes all of it.", f"{device}: a question too long says so: {seen['limit']}")
        check(seen["too long"] == {"dialog": 0, "asked": [], "text kept": True} and seen["limit in view"]
              and seen["limit spoken"], f"{device}: before anything is sent, in view, the text kept: {seen['too long']} "
              f"{seen['limit in view']} {seen['limit spoken']}")
        check(seen["ask confirm"] == "pressed" and seen["asked"] == [md5(QUESTION)],
              f"{device}: and is asked: {seen['ask confirm']} {seen['asked']}")
        check(seen["question block"] is not None and seen["question block"] <= height * 0.4 + 1 and seen["question fits"],
              f"{device}: the question shows in a bounded block: {seen['question block']}")
        check(seen["accept in view"] and seen["accept confirm"] == "pressed" and seen["accepted"] == ["director_review", "true"],
              f"{device}: Accept, with the large text in the box, accepts: {seen['accepted']}")
        check(seen["escape closes the ticket"], f"{device}: with no dialog open, Escape closes the ticket again")
        check(seen["errors"] == [], f"{device}: no page errors: {seen['errors']}")


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), sys.argv[3])))
        return 0
    if os.environ.get("SYRD741_MUTATION_SKIP_BEFORE") != "1":  # a mutant changes this tree only
        with tempfile.TemporaryDirectory(prefix="syrd741-browser-before.", dir="/tmp") as tmp:
            before(run(tree_at(BEFORE, Path(tmp) / "before"), "b741b"))
    candidate(run(ROOT, "b741a"))
    print(f"uat_large_paste_browser_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
