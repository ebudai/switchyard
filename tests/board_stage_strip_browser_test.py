#!/usr/bin/env python3
"""SYRD-722, in a real browser: every declared stage of a long workflow strip can be reached.

The User's board at 3840x2160 (scaled 1.5, so a 2560px page) cut off its last stages at the window's edge. The
board declares 15 stages, 12 of them shown by default; at 205px a lane they need 2636px, more than the 2224px the
composer leaves. The strip was its own horizontal scroller, but nothing bounded its height, so its scrollbar sat under
the tallest lane, a page's scroll below the window, and nothing else offered a way across.

A board holding the live SYRD board's declared stages (read from it on 2026-11-01) and cards shaped like its own,
served by the real server, opened in Chromium at the ticket's viewports and the User's own screen. Main runs first,
from a git archive of its own scripts and tests, then this tree; the same steps on each:

- at each viewport: the board stays inside the window, the strip's horizontal scrollbar is on screen even though a
  lane is taller than the window, and both the first and the last stage, header, lane and cards, can be brought fully
  into view beside the composer;
- the wheel scrolls the lanes vertically and a trackpad's sideways swipe scrolls them across;
- a keyboard alone reaches the last stage, through the stage controls and through the strip itself;
- a ticket update, the Show filters and a refresh keep the stage the User was looking at, and a strip made shorter
  clamps to its end;
- a card in the last stage opens, and closing it leaves the strip where it was.
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
#: Main's commit this ticket is cut from: the before-run's tree.
BEFORE = "ee27a35ed5879443e7b1a76e901c847110e16e1e"
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
#: The live SYRD board's declared stages, as its /api/board listed them on 2026-11-01.
COLUMNS = [
    ("draft", "Draft"), ("backlog", "Backlog"), ("analysis", "Triage"), ("in_progress", "Implementation"),
    ("documentation", "Documentation"), ("inspection", "Inspection"), ("audit", "Audit"),
    ("publication", "Publication"), ("publication_failed", "Publication failed"), ("dat", "DAT"),
    ("user_review", "UAT"), ("user_feedback", "Director reviewing the User's message"),
    ("director_review", "Final Sign-Off"), ("done", "Done"), ("cancelled", "Cancelled"),
]
#: (width, height, device scale): the ticket's four viewports, then the User's screen as the browser lays it out.
VIEWPORTS = [(3840, 2160, 1), (2048, 1152, 1), (1440, 900, 1), (1100, 900, 1), (2560, 1440, 1.5)]
#: Either side of where the lanes stop stacking (900px) and of where the composer used to stop stacking (980px).
BOUNDARY = [900, 901, 980, 981]
CHECKS = 0

BLOCKED = (
    "Waiting for the User to approve or decline the replacement lifecycle packet digest "
    "9158bf5ef6be4364ac96920454e675174ca092dad5cfa13e77b9a1ecc69704a3 and choose a time when the host is not using "
    "the GPU for one bounded live qualification. Nothing may start before approval."
)


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def tickets(ticket: object) -> list[dict]:
    """Cards shaped like the live board's: a tall Implementation and UAT, a long Done, two awaiting sign-off."""
    made = [ticket(f"SYRD-{800 + n}", f"Deferred idea {n}: a backlog entry", state="backlog", assignee="director")
            for n in range(13)]
    made.append(ticket("SYRD-600", "Router model canary: measure routing accuracy, abstention, latency and total cost",
                       state="analysis", assignee="director"))
    for n in range(5):
        made.append(ticket(f"SYRD-69{n}", f"Local model slice {n}: qualify and manage one bounded host inference service",
                           state="in_progress", assignee="app" if n % 2 else "ops",
                           blocked_by=["SYRD-691"] if n < 3 else None, blocked_reason=BLOCKED if n < 3 else "",
                           active_work_highlight=n == 0))
    for n in range(14):
        made.append(ticket(f"SYRD-6{50 + n}", f"Fresh project provisioning and first launch {n} need no manual repairs",
                           state="user_review", assignee="user", needs_user_signoff=True))
    made.append(ticket("SYRD-640", "Persistent Director is launched fresh when the launcher config disagrees",
                       state="director_review", assignee="director", needs_user_signoff=True, user_signoff=True))
    made.append(ticket("SYRD-641", "Wayland tenant ACLs keep the desktop runtime directory private",
                       state="director_review", assignee="director", blocked_by=["SYRD-640"],
                       blocked_reason="Closes after SYRD-640 lands on main."))
    made += [ticket(f"SYRD-{100 + n}", f"Finished work {n}", state="done", assignee="director") for n in range(60)]
    made += [ticket(f"SYRD-{300 + n}", f"Withdrawn {n}", state="cancelled", assignee="director") for n in range(12)]
    return made


#: Where the strip, its lanes and the window stand, read off the rendered page.
MEASURE = """
() => {
  const s = document.querySelector('.board-scroll');
  const r = s.getBoundingClientRect();
  const aside = document.querySelector('aside').getBoundingClientRect();
  const cols = [...document.querySelectorAll('.column')].map((c) => {
    const b = c.getBoundingClientRect();
    const parts = [c.querySelector('.column-head'), ...c.querySelectorAll('.card')].map((e) => e.getBoundingClientRect());
    return {label: c.querySelector('.column-title').textContent, left: b.left, right: b.right, height: b.height,
            partsLeft: Math.min(...parts.map((p) => p.left)), partsRight: Math.max(...parts.map((p) => p.right))};
  });
  return {iw: innerWidth, ih: innerHeight, docW: document.documentElement.scrollWidth,
          docH: document.documentElement.scrollHeight, asideRight: aside.right,
          left: r.left, top: r.top, bottom: r.bottom, viewRight: r.left + s.clientLeft + s.clientWidth,
          clientWidth: s.clientWidth, scrollWidth: s.scrollWidth, clientHeight: s.clientHeight,
          scrollHeight: s.scrollHeight, scrollLeft: s.scrollLeft, scrollTop: s.scrollTop,
          windowY: window.scrollY, cols};
}
"""
SETTLE = "() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => done(true))))"
#: The first stage any of whose lane shows in the strip.
LEFTMOST = """
() => {
  const r = document.querySelector('.board-scroll').getBoundingClientRect();
  const col = [...document.querySelectorAll('.column')].find((c) => c.getBoundingClientRect().right > r.left + 2);
  return col ? col.querySelector('.column-title').textContent : '';
}
"""
#: The stage at the strip's left edge, and how far its lane starts from that edge.
LEFT_EDGE = """
() => {
  const r = document.querySelector('.board-scroll').getBoundingClientRect();
  const col = [...document.querySelectorAll('.column')].find((c) => c.getBoundingClientRect().right > r.left + 2);
  return [col.querySelector('.column-title').textContent, Math.round(col.getBoundingClientRect().left - r.left)];
}
"""
#: The stage controls as the User sees them.
NAV = """
() => {
  const n = document.getElementById('stageNav');
  if (!n) return null;
  const prev = document.getElementById('stageNavPrev');
  const next = document.getElementById('stageNavNext');
  return {shown: n.getClientRects().length > 0, status: document.getElementById('stageNavStatus').textContent,
          prev: prev.getAttribute('aria-disabled'), next: next.getAttribute('aria-disabled'),
          prevOpacity: getComputedStyle(prev).opacity, nextOpacity: getComputedStyle(next).opacity};
}
"""
#: Where Later stages is on screen, if it is.
NEXT_BOX = """
() => {
  const b = document.getElementById('stageNavNext');
  if (!b) return null;
  const r = b.getBoundingClientRect();
  return {shown: b.getClientRects().length > 0, top: r.top, bottom: r.bottom};
}
"""
PAD = 18  # .board-scroll's padding: where the first lane starts


def in_view(m: dict, col: dict) -> bool:
    """A whole stage -- lane, header and cards -- inside the strip's visible width, the window and clear of the composer."""
    right = min(m["viewRight"], m["iw"])
    left = max(m["left"], m["asideRight"])
    return min(col["left"], col["partsLeft"]) >= left - 0.5 and max(col["right"], col["partsRight"]) <= right + 0.5


def status(m: dict) -> str:
    """What "Stages a-b of n" should say: every stage any of whose lane shows."""
    shown = [n + 1 for n, col in enumerate(m["cols"]) if col["right"] > m["left"] + 1 and col["left"] < m["viewRight"] - 1]
    return f"Stages {shown[0]}-{shown[-1]} of {len(m['cols'])}"


def cut_right(m: dict) -> str:
    return next((col["label"] for col in m["cols"] if col["right"] > m["viewRight"] + 1 and col["left"] > m["left"] + 1), "")


def cut_left(m: dict) -> str:
    return ([""] + [col["label"] for col in m["cols"] if col["left"] < m["left"] - 1 and col["right"] < m["viewRight"] - 1])[-1]


def scenario(root: Path) -> dict:
    for extra in (str(root), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from board_column_containment_browser_test import ContainmentBoardApp, QuietNotifier, load_playwright
    from scripts.ticket_board.server import TicketBoardServer

    class StripBoardApp(ContainmentBoardApp):
        def __init__(self, frames: Path, assets: Path) -> None:
            super().__init__(frames, assets)
            self.tickets = tickets(self.ticket)

        def snapshot(self) -> dict:
            return {**super().snapshot(), "states": [key for key, _ in COLUMNS],
                    "columns": [{"key": key, "label": label} for key, label in COLUMNS]}

    out: dict = {"viewports": {}, "errors": []}
    with tempfile.TemporaryDirectory(prefix="board-stage-strip.") as tmp:
        (Path(tmp) / "f").mkdir()
        (Path(tmp) / "a").mkdir()
        app = StripBoardApp(Path(tmp) / "f", Path(tmp) / "a")
        server = TicketBoardServer(("127.0.0.1", 0), app, director_notifier=QuietNotifier())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_port}/"
        try:
            with load_playwright()() as playwright:
                browser = playwright.chromium.launch(headless=True)

                def open_page(width: int, height: int, scale: float) -> object:
                    page = browser.new_context(viewport={"width": width, "height": height},
                                               device_scale_factor=scale).new_page()
                    page.on("pageerror", lambda error: out["errors"].append(str(error)))
                    page.goto(url, wait_until="domcontentloaded")
                    page.wait_for_function("() => document.querySelectorAll('.column').length === 12"
                                           " && document.querySelectorAll('.card').length === 22", timeout=15000)
                    page.evaluate(SETTLE)
                    return page

                def at(page: object, left: object) -> dict:
                    page.evaluate(f"() => {{ const s = document.querySelector('.board-scroll'); s.scrollLeft = {left}; }}")
                    page.evaluate(SETTLE)
                    return page.evaluate(MEASURE)

                for width, height, scale in VIEWPORTS:
                    page = open_page(width, height, scale)
                    start = at(page, 0)
                    start_nav = page.evaluate(NAV)
                    end = at(page, "s.scrollWidth")
                    end_nav = page.evaluate(NAV)
                    page.evaluate("() => { const s = document.querySelector('.board-scroll'); s.scrollTop = s.scrollHeight; }")
                    page.evaluate(SETTLE)
                    bottom = page.evaluate(MEASURE)
                    out["viewports"][f"{width}x{height}@{scale}"] = {
                        "start": start, "end": end, "first in view": in_view(start, start["cols"][0]),
                        "last in view": in_view(end, end["cols"][-1]),
                        "first stage": start["cols"][0]["label"], "last stage": end["cols"][-1]["label"],
                        "tallest lane": max(col["height"] for col in start["cols"]),
                        # the last stage's lowest card can be scrolled up into the strip
                        "last lane bottom reached": page.evaluate(
                            "() => { const c = [...document.querySelectorAll('.column')].at(-1).querySelectorAll('.card');"
                            " const r = document.querySelector('.board-scroll').getBoundingClientRect();"
                            " return c.length > 0 && c[c.length - 1].getBoundingClientRect().bottom <= Math.min(r.bottom, innerHeight) + 0.5; }"),
                        "bottom": bottom, "nav": start_nav and start_nav["shown"], "nav start": start_nav, "nav end": end_nav,
                    }
                    page.context.close()

                # Reading the tallest lane to its end, at each side of the breakpoints.
                out["boundary"] = {}
                for width in BOUNDARY:
                    page = open_page(width, 900, 1)
                    loaded = page.evaluate(MEASURE)
                    page.evaluate("() => { const s = document.querySelector('.board-scroll'); s.scrollTop = s.scrollHeight;"
                                  " window.scrollTo(0, document.documentElement.scrollHeight); }")
                    page.evaluate(SETTLE)
                    measured = page.evaluate(MEASURE)
                    out["boundary"][str(width)] = {"m": measured, "loaded": loaded, "next": page.evaluate(NEXT_BOX),
                                                   "across": len({round(col["left"]) for col in measured["cols"]}) > 1}
                    page.context.close()

                page = open_page(1440, 900, 1)
                strip = page.locator(".board-scroll")
                box = strip.bounding_box()
                page.mouse.move(box["x"] + box["width"] / 2, box["y"] + min(box["height"], 900 - box["y"]) / 2)
                page.mouse.wheel(0, 400)
                page.wait_for_timeout(300)
                out["wheel down"] = page.evaluate(MEASURE)
                at(page, 0)
                page.evaluate("() => { document.querySelector('.board-scroll').scrollTop = 0; window.scrollTo(0, 0); }")
                page.mouse.wheel(400, 0)
                page.wait_for_timeout(300)
                out["swipe across"] = page.evaluate(MEASURE)

                # Keyboard alone: Tab from the top of the page to the stage control, then press it to the end.
                at(page, 0)
                page.evaluate("() => { document.activeElement && document.activeElement.blur(); window.scrollTo(0, 0); }")
                found = None
                for presses in range(1, 120):
                    page.keyboard.press("Tab")
                    if page.evaluate("() => document.activeElement && document.activeElement.id") == "stageNavNext":
                        found = presses
                        break
                out["tabs to later stages"] = found
                if found:
                    before_press = page.evaluate(MEASURE)
                    page.keyboard.press("Enter")
                    page.evaluate(SETTLE)
                    out["first later"] = [cut_right(before_press), page.evaluate(LEFT_EDGE)]
                    for _ in range(30):
                        if page.evaluate("() => document.activeElement.getAttribute('aria-disabled')") == "true":
                            break
                        page.keyboard.press("Enter")
                        page.wait_for_timeout(150)
                    page.evaluate(SETTLE)
                    measured = page.evaluate(MEASURE)
                    out["keyboard end"] = {"last in view": in_view(measured, measured["cols"][-1]),
                                           "next disabled": page.evaluate("() => document.activeElement.id + ':' + document.activeElement.getAttribute('aria-disabled')") == "stageNavNext:true",
                                           "prev disabled": page.evaluate("() => document.getElementById('stageNavPrev').getAttribute('aria-disabled')") == "true"}
                    page.keyboard.press("Shift+Tab")
                    hidden_left = cut_left(measured)
                    page.keyboard.press("Enter")
                    page.evaluate(SETTLE)
                    after_press = page.evaluate(MEASURE)
                    landed = next((col for col in after_press["cols"] if col["label"] == hidden_left), None)
                    out["first earlier"] = [hidden_left, landed and round(after_press["viewRight"] - landed["right"])]
                    for _ in range(30):
                        if page.evaluate("() => document.getElementById('stageNavPrev').getAttribute('aria-disabled')") == "true":
                            break
                        page.keyboard.press("Enter")
                        page.wait_for_timeout(150)
                    page.evaluate(SETTLE)
                    measured = page.evaluate(MEASURE)
                    out["keyboard start"] = {"first in view": in_view(measured, measured["cols"][0]),
                                             "scrollLeft": measured["scrollLeft"],
                                             "on": page.evaluate("() => document.activeElement.id")}
                # The strip itself takes focus and its arrow keys.
                at(page, 0)
                page.evaluate("() => { document.activeElement && document.activeElement.blur(); window.scrollTo(0, 0); }")
                strip_tab = None
                for presses in range(1, 120):
                    page.keyboard.press("Tab")
                    if page.evaluate("() => document.activeElement === document.querySelector('.board-scroll')"):
                        strip_tab = presses
                        break
                out["tabs to the strip"] = strip_tab
                if strip_tab:
                    out["strip outline"] = page.evaluate("() => { const s = getComputedStyle(document.activeElement);"
                                                         " return [s.outlineStyle, s.outlineWidth, s.outlineOffset]; }")
                    for _ in range(3):
                        page.keyboard.press("ArrowRight")
                    page.wait_for_timeout(300)
                    out["arrow right"] = page.evaluate(MEASURE)["scrollLeft"]

                # A window made wide enough for every stage hides the controls; narrowed again, they return.
                page.set_viewport_size({"width": 3840, "height": 2160})
                page.evaluate(SETTLE)
                out["widened"] = (page.evaluate(NAV) or {}).get("shown")
                page.set_viewport_size({"width": 1440, "height": 900})
                page.evaluate(SETTLE)
                out["narrowed"] = (page.evaluate(NAV) or {}).get("shown")

                # The stage the User is looking at survives an update, the filters and a refresh.
                def look_at(label: str) -> None:
                    page.evaluate("(label) => { const s = document.querySelector('.board-scroll');"
                                  " const c = [...document.querySelectorAll('.column')].find((e) =>"
                                  " e.querySelector('.column-title').textContent === label);"
                                  " s.scrollLeft += c.getBoundingClientRect().left - s.getBoundingClientRect().left; }", label)
                    page.evaluate(SETTLE)

                look_at("Inspection")
                page.evaluate("() => { document.querySelector('.board-scroll').scrollTop = 300; }")
                out["looking at"] = page.evaluate(LEFTMOST)
                app.tickets.append(app.ticket("SYRD-699", "A ticket the Director just moved to UAT", state="user_review",
                                              assignee="user", needs_user_signoff=True))
                cards = page.evaluate("() => document.querySelectorAll('.card').length")
                page.evaluate("() => requestBoardReload()")
                page.wait_for_function(f"() => document.querySelectorAll('.card').length === {cards + 1}", timeout=10000)
                page.evaluate(SETTLE)
                out["after update"] = page.evaluate(LEFTMOST)
                out["top after update"] = page.evaluate(MEASURE)["scrollTop"]
                for name in ("showDeferredInput", "showDoneInput", "showCancelledInput"):
                    page.click(f"#{name}")
                    page.evaluate(SETTLE)
                    out[f"after {name}"] = page.evaluate(LEFTMOST)
                # Hiding the stage at the left edge: the next stage takes its place.
                look_at("Backlog")
                page.click("#showDeferredInput")
                page.evaluate(SETTLE)
                out["after hiding the left stage"] = page.evaluate(LEFT_EDGE)
                page.click("#showDeferredInput")
                # A refresh resets the Show filters: Backlog goes, so the same offset would show the next stage.
                look_at("Inspection")
                out["looking at before refresh"] = page.evaluate(LEFTMOST)
                page.evaluate("() => performSmartRefresh()")
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_function("() => document.querySelectorAll('.column').length === 12"
                                       " && document.querySelectorAll('.card').length === 23", timeout=15000)
                page.evaluate(SETTLE)
                out["after refresh"] = page.evaluate(LEFTMOST)
                # A shorter strip clamps to its end: with every stage shown, go to the end, then hide the last two.
                for name in ("showDeferredInput", "showDoneInput", "showCancelledInput"):
                    if not page.is_checked(f"#{name}"):
                        page.click(f"#{name}")
                at(page, "s.scrollWidth")
                for name in ("showDoneInput", "showCancelledInput"):
                    page.click(f"#{name}")
                page.evaluate(SETTLE)
                measured = page.evaluate(MEASURE)
                out["clamped"] = {"last": measured["cols"][-1]["label"], "last in view": in_view(measured, measured["cols"][-1]),
                                  "at end": measured["scrollLeft"] >= measured["scrollWidth"] - measured["clientWidth"] - 1}
                # A card in the last stage opens; closing it leaves the strip where it was.
                page.click("#showDeferredInput")
                end = at(page, "s.scrollWidth")
                page.locator(".column").last.locator(".card").first.click()
                page.wait_for_selector("#detailOverlay:not([hidden])", timeout=5000)
                out["opened"] = page.text_content("#detailModalTitle")
                page.click("#detailCloseBtn")
                page.evaluate(SETTLE)
                after = page.evaluate(MEASURE)
                out["closed"] = {"left kept": after["scrollLeft"] == end["scrollLeft"],
                                 "overlay hidden": page.is_hidden("#detailOverlay")}
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


def run(root: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(PANE_ENV_PREFIXES)}
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(root)],
                           capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL,
                           env={**env, "PYTHONDONTWRITEBYTECODE": "1"})
    assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
    return json.loads(child.stdout.strip().splitlines()[-1])


def before(got: dict) -> None:
    user = got["viewports"]["2560x1440@1.5"]
    start = user["start"]
    check(start["scrollWidth"] > start["clientWidth"] and not in_view(start, start["cols"][-1]),
          f"main, on the User's screen: the strip is wider than the board and its last stage is cut off: {start['cols'][-1]}")
    check(start["bottom"] > start["ih"] and start["docH"] > start["ih"],
          f"main: and the strip's scrollbar is below the window, under a lane {user['tallest lane']}px tall: "
          f"strip bottom {start['bottom']}, window {start['ih']}")
    check(user["nav"] is None and got["tabs to later stages"] is None, "main: and there is no other control to reach it")
    for width in BOUNDARY[1:]:
        m = got["boundary"][str(width)]["loaded"]
        check(m["docH"] > m["ih"] and m["bottom"] > m["ih"] and m["scrollWidth"] > m["clientWidth"],
              f"main, {width}px wide: the strip scrolls across, but its scrollbar is at the end of a {m['docH']}px page "
              f"in a {m['ih']}px window: strip bottom {m['bottom']}")
    check(got["after showDeferredInput"] != got["looking at"],
          f"main: showing Backlog moves the strip off the stage the User was looking at: "
          f"{got['looking at']} -> {got['after showDeferredInput']}")


def candidate(got: dict) -> None:
    for name, view in got["viewports"].items():
        start, end = view["start"], view["end"]
        check(start["docW"] <= start["iw"] and start["docH"] <= start["ih"] and start["bottom"] <= start["ih"] + 0.5,
              f"{name}: the board stays inside the window: {start['docW']}x{start['docH']}, strip bottom {start['bottom']}")
        check(view["tallest lane"] > start["ih"] - start["top"] or start["scrollHeight"] > start["clientHeight"],
              f"{name}: with a lane taller than the strip: {view['tallest lane']}")
        check(view["first stage"] == "Draft" and view["first in view"], f"{name}: the first stage is fully in view: {start['cols'][0]}")
        check(view["last stage"] == "Final Sign-Off" and view["last in view"],
              f"{name}: scrolled to the end, the last stage is fully in view: {end['cols'][-1]}, strip to {end['viewRight']}")
        check(view["last lane bottom reached"], f"{name}: and its lowest card scrolls into view")
        overflowing = start["scrollWidth"] > start["clientWidth"]
        check(view["nav"] == overflowing, f"{name}: the stage controls show exactly when the stages do not fit: {view['nav']}")
        if overflowing:
            check(view["nav start"] == {"shown": True, "status": status(start), "prev": "true", "next": "false",
                                        "prevOpacity": "0.45", "nextOpacity": "1"},
                  f"{name}: at the start they say which stages show, and that there is nothing earlier: {view['nav start']}")
            check(view["nav end"] == {"shown": True, "status": status(end), "prev": "false", "next": "true",
                                      "prevOpacity": "1", "nextOpacity": "0.45"},
                  f"{name}: at the end, nothing later: {view['nav end']}")
        check(view["bottom"]["bottom"] <= view["bottom"]["ih"] + 0.5 and view["bottom"]["windowY"] == 0,
              f"{name}: scrolled to the tallest lane's end, the strip and its scrollbar are still in the window")
    check(got["viewports"]["2560x1440@1.5"]["nav"], "on the User's screen the stages do not fit, and the controls show")
    phone = got["boundary"]["900"]
    check(not phone["across"] and phone["m"]["scrollWidth"] <= phone["m"]["clientWidth"] and not phone["next"]["shown"],
          f"900px wide: the lanes stack in one column, there is nothing to scroll across, and no controls: {phone['next']}")
    for width in BOUNDARY[1:]:
        view = got["boundary"][str(width)]
        m, nxt = view["m"], view["next"]
        check(view["across"] and m["scrollWidth"] > m["clientWidth"] and m["scrollTop"] > 0,
              f"{width}px wide: the lanes are a strip wider than the board, and the tallest lane was read to its end")
        check(m["docH"] <= m["ih"] and m["windowY"] == 0 and m["bottom"] <= m["ih"] + 0.5,
              f"{width}px wide: the page stays in the window and the strip's scrollbar is on screen: "
              f"page {m['docH']}, strip bottom {m['bottom']}, window {m['ih']}")
        check(nxt["shown"] and nxt["top"] >= 0 and nxt["bottom"] <= m["ih"],
              f"{width}px wide: and Later stages is on screen: {nxt}")
        check(m["asideRight"] <= m["left"] + 0.5 and m["clientHeight"] >= m["ih"] / 2,
              f"{width}px wide: the composer sits beside the board, which keeps the lanes most of the window's height: "
              f"composer to {m['asideRight']}, strip from {m['left']}, {m['clientHeight']}px tall")
    down, across = got["wheel down"], got["swipe across"]
    check(down["scrollTop"] > 0 and down["scrollLeft"] == 0 and down["windowY"] == 0,
          f"the wheel scrolls the lanes vertically, and only vertically: {down['scrollTop']}, {down['scrollLeft']}")
    check(across["scrollLeft"] > 0 and across["scrollTop"] == 0, f"a sideways swipe scrolls them across: {across['scrollLeft']}")
    check(got["tabs to later stages"], "Tab reaches the Later stages control")
    check(got["keyboard end"] == {"last in view": True, "next disabled": True, "prev disabled": False},
          f"Enter on it reaches the last stage, fully in view, and then it says it is at the end, keeping focus: {got['keyboard end']}")
    check(got["keyboard start"] == {"first in view": True, "scrollLeft": 0, "on": "stageNavPrev"},
          f"Shift+Tab reaches Earlier stages, and it goes back to the first: {got['keyboard start']}")
    check(got["first later"][0] == got["first later"][1][0] and got["first later"][1][1] == PAD,
          f"one press of Later brings the first stage cut off on the right to the left edge: {got['first later']}")
    check(got["first earlier"][1] == PAD, f"one press of Earlier brings the last stage cut off on the left to the right edge: "
          f"{got['first earlier']}")
    check(got["widened"] is False and got["narrowed"] is True, "a resize hides and shows the controls as the stages fit")
    check(got["tabs to the strip"] and got["strip outline"] == ["solid", "2px", "-2px"] and got["arrow right"] > 0,
          f"the strip takes focus, shows it, and its arrow keys scroll it across: {got.get('strip outline')}, {got.get('arrow right')}")
    check(got["looking at"] == "Inspection" and got["after update"] == "Inspection" and got["top after update"] == 300,
          f"a ticket update keeps the stage and the lane position: {got['after update']}, {got['top after update']}")
    check([got[f"after {n}"] for n in ("showDeferredInput", "showDoneInput", "showCancelledInput")] == ["Inspection"] * 3,
          f"Show Deferred, Done and Cancelled keep it: {[got[f'after {n}'] for n in ('showDeferredInput', 'showDoneInput', 'showCancelledInput')]}")
    check(got["after hiding the left stage"] == ["Triage", 0],
          f"hiding the stage at the left edge puts the next one there: {got['after hiding the left stage']}")
    check(got["after refresh"] == got["looking at before refresh"] == "Inspection",
          f"a refresh keeps it: {got['after refresh']}")
    check(got["clamped"] == {"last": "Final Sign-Off", "last in view": True, "at end": True},
          f"a strip made shorter at its end clamps to its new end: {got['clamped']}")
    check(got["opened"].startswith("SYRD-64") and got["closed"] == {"left kept": True, "overlay hidden": True},
          f"a last-stage card opens, and closing it leaves the strip where it was: {got['opened']}, {got['closed']}")
    check(got["errors"] == [], f"no page errors: {got['errors']}")


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]))))
        return 0
    if os.environ.get("SYRD722_MUTATION_SKIP_BEFORE") != "1":  # a mutant changes this tree only
        with tempfile.TemporaryDirectory(prefix="syrd722-strip-before.", dir="/tmp") as tmp:
            before(run(tree_at(BEFORE, Path(tmp) / "before")))
    candidate(run(ROOT))
    print(f"board_stage_strip_browser_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
