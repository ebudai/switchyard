#!/usr/bin/env python3
"""SYRD-729, in a real browser: a Markdown link in the User's UAT steps is a link, on the desktop and on a phone.

A production-built board with the shipped workflow and the real server, served as production serves it (HTTP acts as
the User only), with Chromium driving the real page. The Director's steps are SYRD-690's own Do this, with its
canary-report link, and beside it links that must stay text: one to `javascript:`, one to `data:`, a relative one, one
with a space, an unclosed one, one in inline code and one in a fenced block. Main runs first, from a git archive of
its own scripts, examples and tests, then this tree; the same steps on each, at desktop size and at a phone's:

- the canary link is an anchor whose target is exactly the one the Director wrote, and clicking or tapping it opens
  that target (a stand-in answers for github.com: nothing leaves this host);
- a label written as markup is shown as its text, and a target with balanced parentheses keeps them;
- every other link is the text it was written as, and nothing on the page runs or opens for it;
- the steps are stored as the Director wrote them, in the same three parts;
- Ask Director, the Director's linked answer under the question, Accept and Send to Director still work.

On main the panel showed `[canary report](https://...)` as literal text.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: Main's commit this ticket is cut from: the before-run's tree.
BEFORE = "25a99757a21bcd1804816bf524469ffebfcd5b4e"
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
A = "a" * 40
CANARY = ("https://github.com/ebudai/switchyard/blob/25a99757a21bcd1804816bf524469ffebfcd5b4e/"
          "docs/syrd-693-local-qwen-canary.md")
PARENS = "https://example.com/wiki/Otto_(tenant)"
HEADING = "https://example.com/report#result"
ANSWER = "https://example.com/answer"
INERT = ["[this](javascript:alert(document.domain))", "[that](data:text/html,<b>x</b>)", "[relative](/api/tickets)",
         "[spaced](https://example.com/a b)", "[half](https://example.com/half"]
#: SYRD-690's own Do this, as the Director wrote it, then the links that must stay text.
SYRD690 = ("Run `switchyard local-model preview` and `switchyard local-model status`. Read the \"Board hooks, live\" "
           f"section of the [canary report]({CANARY}). You do not need to start the model.")
DO_THIS = (f"{SYRD690}\nThen run `[not a link](https://example.com/code)`:\n"
           "```\nswitchyard status otto [fenced](https://example.com/fenced)\n```\n"
           f"Do not open {INERT[0]} or {INERT[1]} or {INERT[2]} or {INERT[3]} or {INERT[4]} and see "
           f"[<b>Otto</b>]({PARENS}).")
SEE = f"The report's [result]({HEADING}) and the line `otto: running`."
#: Each anchor in a panel: its text, target, how it opens, and whether it sits in code.
ANCHORS = """el => [...el.querySelectorAll('a')].map((a) => ({ text: a.textContent, href: a.getAttribute('href'),
    target: a.target, rel: a.rel, code: !!a.closest('code, pre'),
    part: a.closest('[data-uat-part]') ? a.closest('[data-uat-part]').getAttribute('data-uat-part') : null }))"""
#: The middle of where `needle` is written under `selector`, for a click on that text itself.
SPOT = """([selector, needle]) => {
  const walker = document.createTreeWalker(document.querySelector(selector), NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const at = node.data.indexOf(needle);
    if (at >= 0) {
      const range = document.createRange();
      range.setStart(node, at);
      range.setEnd(node, at + needle.length);
      const box = range.getClientRects()[0];
      return box ? { x: box.x + box.width / 2, y: box.y + box.height / 2 } : null;
    }
  }
  return null;
}"""
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
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
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

        for ticket in ("PGU-1", "PGU-2"):
            fixture.seed_postgres_ticket(admin, ticket, title=f"Canary report {ticket}", state="dat", assignee="director",
                                         needs_audit=True, audit_signoff=True, needs_user_signoff=True, commit_hash=A)
            uat_instructions.perform(app, "set_uat_instructions", {"do_this": DO_THIS, "you_should_see": SEE},
                                     caller_role="director", ticket_id=ticket)
            app.perform_workflow_action(ticket, "director_dat_sign_off", {}, caller_role="director")
        out["stored"] = sql("SELECT md5(do_this) || ' ' || md5(you_should_see) FROM ticket_board.ticket_uat_instructions "
                            "WHERE ticket_id = 'PGU-1' AND superseded_at IS NULL;")

        def row(ticket: str) -> list:
            return sql(f"SELECT state || '|' || user_signoff::text FROM ticket_board.tickets WHERE id = '{ticket}';").split("|")

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
                dialogs: list[str] = []
                opened: list[str] = []

                def context(**options):
                    made = browser.new_context(**options)
                    # The link's own target, answered here: a stand-in for github.com and example.com.
                    made.route(re.compile(r"^https?://(github\.com|example\.com)/"), lambda route: route.fulfill(
                        status=200, content_type="text/plain", body="stand-in"))
                    made.on("page", lambda page: opened.append(page.url))
                    page = made.new_page()
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.dismiss()))
                    return made, page

                def wait_for(page, predicate, seconds: float = 10.0) -> bool:
                    end = time.monotonic() + seconds
                    while time.monotonic() < end:
                        if predicate():
                            return True
                        page.wait_for_timeout(100)
                    return predicate()

                def panel(page, ticket: str) -> dict:
                    selector = f"[data-uat-panel='{ticket}']"
                    page.locator(selector).wait_for(timeout=15000)
                    wait_for(page, lambda: page.locator(f"{selector} [data-uat-decision], {selector} .soft-note").count() > 0)
                    return {
                        "parts": page.locator(f"{selector} [data-uat-part]").evaluate_all(
                            "els => els.map(e => e.getAttribute('data-uat-part'))"),
                        "anchors": page.locator(selector).evaluate(ANCHORS),
                        "do": page.locator(f"{selector} [data-uat-part='do_this'] .uat-part-text").text_content(),
                        "code": page.locator(f"{selector} [data-uat-part='do_this'] code").all_text_contents(),
                        "markup": page.locator(f"{selector} b").count(),
                        "decisions": page.locator(f"{selector} [data-uat-decision]").evaluate_all(
                            "els => els.map(e => e.getAttribute('data-uat-decision'))"),
                        "ask": page.locator(f"{selector} .uat-ask").count(),
                        # Nothing wider than the panel: a long target wraps, on a phone too.
                        "fits": page.locator(selector).evaluate("el => el.scrollWidth <= el.clientWidth"),
                        # Drawn in the panel's accent, as its labels are: a link reads as one on the dark page.
                        "accent": page.locator(selector).evaluate("""el => [...el.querySelectorAll('a')].map((a) =>
                            getComputedStyle(a).color === getComputedStyle(el.querySelector('.uat-part-label')).color)"""),
                    }

                def follow(made, page, ticket: str, how: str) -> str | None:
                    """Click or tap the canary link; the URL of the page it opened."""
                    link = page.locator(f"[data-uat-panel='{ticket}'] a", has_text="canary report")
                    if link.count() != 1:
                        return None
                    try:
                        with made.expect_page(timeout=10000) as popup:
                            getattr(link, how)()
                    except PlaywrightTimeout:
                        # Opened in the board's own page, if anywhere: say where, and come back to the ticket.
                        target = f"same page: {page.url}"
                        page.goto(board + f"#ticket={ticket}", wait_until="domcontentloaded")
                        panel(page, ticket)
                        return target
                    popup.value.wait_for_load_state()
                    target = popup.value.url
                    popup.value.close()
                    return target

                def confirm(page) -> None:
                    dialog = page.locator(".board-ui-dialog")
                    dialog.wait_for(timeout=5000)
                    dialog.locator("[data-board-ui-confirm]").click()

                # -- the desktop ---------------------------------------------------------------------------
                made, page = context(viewport={"width": 1280, "height": 800})
                page.goto(board + "#ticket=PGU-1", wait_until="domcontentloaded")
                out["desktop"] = panel(page, "PGU-1")
                out["desktop opens"] = follow(made, page, "PGU-1", "click")
                # The inert links are text: clicking where they are written opens nothing and runs nothing.
                out["inert clicked"], before_clicks = 0, len(opened)
                for text in INERT:
                    spot = page.evaluate(SPOT, ["[data-uat-panel='PGU-1'] [data-uat-part='do_this']", text])
                    if spot:
                        page.mouse.click(spot["x"], spot["y"])
                        out["inert clicked"] += 1
                page.wait_for_timeout(1000)
                out["inert opened"] = opened[before_clicks:]
                page.locator("[data-uat-panel='PGU-1'] [data-uat-message]").fill(f"Is [this one]({CANARY}) current?")
                page.locator("[data-uat-panel='PGU-1'] .uat-ask").click()
                confirm(page)
                wait_for(page, lambda: page.locator("[data-uat-panel='PGU-1'] [data-uat-thread]").count() == 1)
                sql(f"SELECT ticket_board.append_ticket_comment('PGU-1', 'director', $c$Yes; see [the answer]({ANSWER}).$c$);")
                page.reload(wait_until="domcontentloaded")
                panel(page, "PGU-1")
                thread = "[data-uat-panel='PGU-1'] [data-uat-thread]"
                wait_for(page, lambda: "Yes; see" in (page.locator(thread).text_content() or ""))
                out["thread"] = page.locator(thread).evaluate(ANCHORS)
                page.locator("[data-uat-panel='PGU-1'] [data-uat-message]").fill("The report opened.")
                page.locator("[data-uat-panel='PGU-1'] [data-uat-decision='send']").click()
                confirm(page)
                wait_for(page, lambda: row("PGU-1")[0] == "user_feedback")
                out["PGU-1"] = row("PGU-1")

                # -- the phone -----------------------------------------------------------------------------
                made, page = context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
                page.goto(board + "#ticket=PGU-2", wait_until="domcontentloaded")
                out["phone"] = panel(page, "PGU-2")
                out["phone opens"] = follow(made, page, "PGU-2", "tap")
                if out["phone"]["decisions"]:
                    page.locator("[data-uat-panel='PGU-2'] [data-uat-decision='accept']").tap()
                    confirm(page)
                    wait_for(page, lambda: row("PGU-2")[1] == "true")
                out["PGU-2"] = row("PGU-2")
                out["dialogs"] = dialogs
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


def stored(got: dict, side: str) -> None:
    want = " ".join(hashlib.md5(text.encode()).hexdigest() for text in (DO_THIS, SEE))
    check(got["stored"] == want, f"{side}: the steps are stored as the Director wrote them")


def before(got: dict) -> None:
    stored(got, "main")
    for device in ("desktop", "phone"):
        check(got[device]["anchors"] == [] and f"[canary report]({CANARY})" in got[device]["do"],
              f"main, {device}: the canary link is literal text, not a link: {got[device]}")
    check(got["desktop opens"] is None and got["phone opens"] is None, "main: so nothing opens it")


def candidate(got: dict) -> None:
    stored(got, "here")
    links = [{"text": "canary report", "href": CANARY, "target": "_blank", "rel": "noopener noreferrer", "code": False,
              "part": "do_this"},
             {"text": "<b>Otto</b>", "href": PARENS, "target": "_blank", "rel": "noopener noreferrer", "code": False,
              "part": "do_this"},
             {"text": "result", "href": HEADING, "target": "_blank", "rel": "noopener noreferrer", "code": False,
              "part": "you_should_see"}]
    for device in ("desktop", "phone"):
        seen = got[device]
        check(seen["parts"] == ["do_this", "you_should_see", "then_choose"], f"{device}: the three parts: {seen['parts']}")
        check(seen["anchors"] == links, f"{device}: the safe links, and only they, are anchors: {seen['anchors']}")
        check(seen["markup"] == 0, f"{device}: a label written as markup is shown as its text")
        check(all(text in seen["do"] for text in INERT),
              f"{device}: javascript:, data:, relative, spaced and unclosed links stay the text written: {seen['do']}")
        check(seen["do"].startswith("Run switchyard local-model preview and switchyard local-model status. Read the "
                                    "\"Board hooks, live\" section of the canary report. You do not need to start the "
                                    "model.\nThen run") and seen["do"].endswith("and see <b>Otto</b>."),
              f"{device}: each link shows its label, with the text around it: {seen['do']}")
        check(seen["code"] == ["switchyard local-model preview", "switchyard local-model status",
                               "[not a link](https://example.com/code)",
                               "switchyard status otto [fenced](https://example.com/fenced)"],
              f"{device}: links in inline and fenced code stay literal: {seen['code']}")
        check(seen["fits"], f"{device}: nothing wider than the panel")
        check(seen["accent"] == [True] * len(links), f"{device}: links in the panel's accent: {seen['accent']}")
        check(seen["ask"] == 1 and seen["decisions"] == ["accept", "send"],
              f"{device}: with Ask Director, Accept and Send to Director: {seen}")
    check(got["desktop opens"] == CANARY, f"desktop: clicking the link opens its exact target: {got['desktop opens']}")
    check(got["phone opens"] == CANARY, f"phone: tapping it opens its exact target: {got['phone opens']}")
    check(got["inert clicked"] == len(INERT) and got["inert opened"] == [], f"clicking the inert links' text opens nothing: {got['inert opened']}")
    check(got["dialogs"] == [], f"and runs nothing: {got['dialogs']}")
    check([link["href"] for link in got["thread"]] == [CANARY, ANSWER],
          f"the User's question and the Director's answer link too: {got['thread']}")
    check(got["PGU-1"] == ["user_feedback", "false"], f"Send to Director still sends: {got['PGU-1']}")
    check(got["PGU-2"] == ["director_review", "true"], f"Accept, on the phone, still accepts: {got['PGU-2']}")
    check(got["errors"] == [], f"no page errors: {got['errors']}")


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), sys.argv[3])))
        return 0
    if os.environ.get("SYRD729_MUTATION_SKIP_BEFORE") != "1":  # a mutant changes this tree only
        with tempfile.TemporaryDirectory(prefix="syrd729-browser-before.", dir="/tmp") as tmp:
            before(run(tree_at(BEFORE, Path(tmp) / "before"), "b729b"))
    candidate(run(ROOT, "b729a"))
    print(f"uat_link_browser_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
