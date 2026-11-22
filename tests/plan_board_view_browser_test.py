#!/usr/bin/env python3
"""SYRD-850, in a real browser: the plan gate on the board page -- each ticket's deliverable, the filter, the plan view.

The plan-gated board is built the production way, as `plan_gate_test.py` builds it: main's schema before the plan
gate, upgraded with this tree's migrations and grants, served by the real server, the plan approved by a plan ticket
taken through Audit, DAT, User Review and Final Sign-Off and closed on its integration commit. Then:

- three milestones: M1 (one deliverable, its only ticket cancelled: re-plan due), M2 (two deliverables, one with a
  ticket in Implementation and one in triage, one with none), M3 (not detailed yet); two unplanned tickets (one in
  Implementation, one cancelled); one ticket with neither, and the plan ticket;
- **the service:** `GET /api/plan` is `plan_status()` exactly; each ticket's JSON carries `plan_scope`, in the ticket
  read and the board read alike;
- **the page, at desktop size and a phone's:** the ticket view says the deliverable with its title and milestone, or
  "Unplanned" and the reason, and nothing for the others; the filter keeps the board to a deliverable's tickets or the
  unplanned ones; the plan view shows the approved commit and plan file, the milestones in order with their
  deliverables counted by stage, "Not detailed yet", "Re-plan due" on M1 only, the unplanned counted apart; a
  deliverable in it opens the board filtered to it; nothing wider than the window;
- **without the gate,** nothing new: the same board after the gate is turned off (its records still there), and a
  board that never ran it: `{"plan": null}`, `plan_scope` null, the controls hidden, no plan line, every ticket shown.

Main runs first, from a git archive: it has no `/api/plan`, no `plan_scope` and none of the page's plan elements.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: Main's commit this ticket is cut from: the before-run's tree.
BEFORE = "2aa8f24ca5900532ff071e9f7a21df21fcc1713a"
MIGRATION = "scripts/ticket_board/migrations/pgu1052_syrd843_plan_gate.sql"
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
MILESTONES = [
    {"id": "M1", "title": "Setup", "detailed": True, "deliverables": [{"id": "D1.1", "title": "Repository"}]},
    {"id": "M2", "title": "Ingest", "detailed": True,
     "deliverables": [{"id": "D2.1", "title": "Reader"}, {"id": "D2.2", "title": "Indexer"}]},
    {"id": "M3", "title": "Query", "detailed": False, "deliverables": [{"id": "D3.1", "title": "Search"}]},
]
OPS_REASON = "An ops one-off: rotate the logs."
#: Each ticket: its deliverable or unplanned reason, then where it goes.
SCOPED = {
    "PGU-920": ("D1.1", None, "cancel"),
    "PGU-921": ("D2.1", None, "app"),
    "PGU-922": ("D2.1", None, ""),
    "PGU-930": (None, OPS_REASON, "main"),
    "PGU-931": (None, "A report from another project.", "cancel"),
}
TICKETS = [*SCOPED, "PGU-940", "PGU-911"]
CARDS = "() => Array.from(document.querySelectorAll('#board .card-id')).map((e) => e.textContent.trim()).sort()"
CONTROLS = """() => {
  const controls = document.getElementById('planControls');
  const input = document.getElementById('planFilterInput');
  if (!controls || !input) return null;
  const box = controls.getBoundingClientRect();
  return { hidden: controls.hidden, display: getComputedStyle(controls).display, right: box.right,
           viewport: window.innerWidth, value: input.value,
           options: Array.from(input.options).map((o) => [o.value, o.textContent,
                                                         o.parentElement.tagName === 'OPTGROUP' ? o.parentElement.label : ''])};
}"""
PLAN_VIEW = """() => {
  const overlay = document.getElementById('planOverlay');
  const content = document.getElementById('planContent');
  const modal = overlay.querySelector('.plan-modal');
  const text = (el) => (el ? el.textContent.trim() : null);
  return {
    hidden: overlay.hidden, approved: text(content.querySelector('.plan-approved')),
    milestones: Array.from(content.querySelectorAll('[data-milestone]')).map((m) => ({
      id: m.dataset.milestone, head: text(m.querySelector('.plan-milestone-head strong')),
      badges: Array.from(m.querySelectorAll('.plan-badge')).map(text),
      deliverables: Array.from(m.querySelectorAll('[data-deliverable]')).map((d) => [
        d.dataset.deliverable, text(d.querySelector('button')), text(d.querySelector('.plan-counts'))]),
    })),
    unplanned: text(content.querySelector('.plan-unplanned .plan-counts')),
    right: modal.getBoundingClientRect().right, viewport: window.innerWidth,
    overflow: content.scrollWidth - content.clientWidth,
  };
}"""
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def get(base: str, path: str) -> tuple[int, object]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(base + path, timeout=60) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, None


def browse(base: str, devices: dict, *, gated: bool) -> dict:
    """The page as each device sees it: the controls, the filter, the ticket view and the plan view."""
    from playwright.sync_api import sync_playwright

    seen: dict = {}
    with sync_playwright() as playwright:
        launch: dict = {"headless": True}
        if Path("/opt/google/chrome/chrome").is_file():
            launch["executable_path"] = "/opt/google/chrome/chrome"
        browser = playwright.chromium.launch(**launch)
        errors: list[str] = []
        for device, options in devices.items():
            page = browser.new_context(**options).new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            out: dict = {}
            try:  # a page that fails is a result: the checks say so, by assertion
                with page.expect_response(lambda r: r.url.endswith("/api/board")):
                    page.goto(base + "/", wait_until="domcontentloaded")
                page.locator(".card-id", has_text="PGU-940").first.wait_for(state="attached", timeout=20000)
                if gated:  # the plan arrives after the board: wait for the page to apply it, if this tree has it
                    page.wait_for_function("() => !document.getElementById('planControls') || "
                                           "!document.getElementById('planControls').hidden", timeout=20000)
                else:
                    page.wait_for_timeout(1500)
                out["controls"] = page.evaluate(CONTROLS)
                out["cards"] = {"": page.evaluate(CARDS)}
                if out["controls"] is not None and not out["controls"]["hidden"]:
                    for value in ("D2.1", "unplanned", "D1.1", ""):
                        page.select_option("#planFilterInput", value)
                        out["cards"][value] = page.evaluate(CARDS)
                out["detail"] = {}
                for ticket in ("PGU-921", "PGU-930", "PGU-940"):
                    # The card's own click handler; on a phone the stage sections start collapsed.
                    page.locator(".card", has=page.locator(".card-id", has_text=ticket)).first.dispatch_event("click")
                    page.locator("#detailOverlay:not([hidden])").wait_for(timeout=10000)
                    page.wait_for_timeout(200)
                    out["detail"][ticket] = page.evaluate(
                        "() => Array.from(document.querySelectorAll('#detailContent .detail-plan-scope')).map((e) => e.textContent)")
                    page.keyboard.press("Escape")
                    page.locator("#detailOverlay").wait_for(state="hidden", timeout=10000)
                out["view"] = None
                if out["controls"] is not None and not out["controls"]["hidden"]:
                    with page.expect_response(lambda r: r.url.endswith("/api/plan")):
                        page.click("#planViewBtn")
                    page.locator("#planOverlay:not([hidden])").wait_for(timeout=10000)
                    out["view"] = page.evaluate(PLAN_VIEW)
                    page.locator("#planContent [data-deliverable='D2.1'] button").click()
                    out["after_view"] = {"hidden": page.evaluate("document.getElementById('planOverlay').hidden"),
                                         "value": page.evaluate("document.getElementById('planFilterInput').value"),
                                         "cards": page.evaluate(CARDS)}
                    page.select_option("#planFilterInput", "")
                    page.click("#planViewBtn")
                    page.locator("#planOverlay:not([hidden])").wait_for(timeout=10000)
                    page.keyboard.press("Escape")
                    out["escape_closes"] = page.evaluate("document.getElementById('planOverlay').hidden")
            except Exception as error:  # noqa: BLE001
                out["failed"] = f"{type(error).__name__}: {str(error)[:600]}"
            seen[device] = out
        seen["errors"] = errors
        browser.close()
    return seen


def scenario(root: Path, prefix: str, before_plan_gate: Path) -> dict:
    for extra in (str(root), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    import plan_gate_test as pgt
    from router_role_board_test import Board, temporary_cluster

    from scripts.ticket_board import plan_gate as pg

    # Main's tree before the plan gate, as plan_gate_test builds it, prepared by the parent: this tree may be an archive.
    pgt.main_tree = lambda into: Path(shutil.copytree(before_plan_gate, into))
    devices = {"desktop": {"viewport": {"width": 1280, "height": 800}},
               "phone": {"viewport": {"width": 390, "height": 844}, "is_mobile": True, "has_touch": True}}
    out: dict = {}
    with temporary_cluster(prefix=prefix, shutdown="immediate") as cluster, \
            tempfile.TemporaryDirectory(prefix=f"{prefix}.", dir="/tmp") as tmp:
        f = pgt.Flow(cluster, Path(tmp))
        try:
            plan = "PGU-911"
            f.seed(plan, "The first plan")
            assert f.act(plan, "set_workflow_flags", "director", plan_change=True, needs_user_signoff=True) == "ok"
            assert f.act(plan, "route", "director", state="in_progress", assignee="main") == "ok"
            files = {pgt.PLAN: pgt.plan_text(MILESTONES), pgt.LOG: "# Decisions\n\n2026-11-22: three milestones.\n"}
            good = f.candidate(plan, files)
            landed = f.store.commit([f.base], files, f"{plan}: integrated")
            f.store.publish("main", landed)
            said = pgt.plan_ticket_to_done(f, plan, good, close_on=landed)
            assert said == "ok", said
            for ticket, (deliverable, reason, then) in SCOPED.items():
                f.seed(ticket, f"Work for {deliverable or 'nobody'}")
                payload = {"deliverable": deliverable} if deliverable else {"unplanned_reason": reason}
                assert f.act(ticket, "set_ticket_deliverable", "director", **payload) == "ok", ticket
                if then == "cancel":
                    assert f.act(ticket, "cancel", "director") == "ok", ticket
                elif then:
                    assert f.act(ticket, "route", "director", state="in_progress", assignee=then) == "ok", ticket
            f.seed("PGU-940", "Not scoped yet")
            f.pass_()
            out["landed"] = landed
            out["sql"] = json.loads(f.board.sql("SELECT ticket_board.plan_status()::text;"))
            out["where"] = {t: f.where(t) for t in TICKETS}

            def answers() -> dict:
                status, plan_answer = get(f.board.base, "/api/plan")
                tickets = {t: get(f.board.base, f"/api/tickets/{t}")[1] for t in TICKETS}
                board = {t["id"]: t for t in get(f.board.base, "/api/board")[1]["tickets"]}
                return {"plan_status": status, "plan": plan_answer,
                        "scope": {t: (tickets[t] or {}).get("plan_scope", "absent") for t in TICKETS},
                        "board_scope": {t: board[t].get("plan_scope", "absent") for t in TICKETS}}

            out["gated"] = {**answers(), "page": browse(f.board.base, devices, gated=True)}
            off = pg.without_plan_gate(f.board.app.workflow_configuration())
            f.board.app.apply_workflow(off, expected_revision=f.board.app.workflow_document()["revision"],
                                       dry_run=False, caller_role="director")
            out["off_rows"] = f.board.sql("SELECT count(*) FROM ticket_board.ticket_deliverables;")
            out["off"] = {**answers(), "page": browse(f.board.base, devices, gated=False)}
        finally:
            f.board.close()
        plain_doc = pg.without_plan_gate(pgt.flow_document())
        plain = Board(cluster, "plain", plain_doc)
        try:
            from router_role_board_test import fixture

            for ticket in ("PGU-921", "PGU-930", "PGU-940", "PGU-911"):
                fixture.seed_postgres_ticket(plain.admin, ticket, title=ticket, state="analysis", assignee="director")
            status, plan_answer = get(plain.base, "/api/plan")
            out["plain"] = {"plan_status": status, "plan": plan_answer,
                            "scope": {t: (get(plain.base, f"/api/tickets/{t}")[1] or {}).get("plan_scope", "absent")
                                      for t in ("PGU-921", "PGU-940")},
                            "page": browse(plain.base, devices, gated=False)}
        finally:
            plain.close()
    return out


def tree_at(commit: str, into: Path, *paths: str) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, *paths], check=True, capture_output=True).stdout
    into.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def before_plan_gate(into: Path) -> Path:
    """Main's scripts before the plan gate's migration joined them: the tree the production upgrade starts from."""
    adding = subprocess.run(["git", "-C", str(ROOT), "log", "--format=%H", "--diff-filter=A", "--", MIGRATION],
                            check=True, capture_output=True, text=True).stdout.split()
    tree = tree_at(f"{adding[-1]}^", into, "scripts")
    check(not (tree / MIGRATION).exists(), "the upgrade starts from a tree without the plan gate")
    return tree


def run(root: Path, prefix: str, start: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(PANE_ENV_PREFIXES)}
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(root), prefix, str(start)],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL,
                           env={**env, "PYTHONDONTWRITEBYTECODE": "1"})
    assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
    return json.loads(child.stdout.strip().splitlines()[-1])


def worked(page: dict, side: str) -> None:
    check("failed" not in page, f"{side}: the page did what the checks ask of it: {page.get('failed')}")


def before(got: dict) -> None:
    check(got["where"]["PGU-911"][0] == "done" and got["sql"]["approved"]["commit"] == got["landed"],
          f"main: the plan gate itself works there: {got['where']} {got['sql'].get('approved')}")
    gated = got["gated"]
    check(gated["plan_status"] == 404 and all(v == "absent" for v in gated["scope"].values()),
          f"main: no /api/plan, and no plan_scope in the ticket JSON: {gated['plan_status']} {gated['scope']}")
    for device in ("desktop", "phone"):
        page = gated["page"][device]
        worked(page, f"main, {device}")
        check(page["controls"] is None and page["view"] is None and all(v == [] for v in page["detail"].values()),
              f"main, {device}: none of the page's plan elements: {page}")


def nothing_new(seen: dict, side: str) -> None:
    check(seen["plan_status"] == 200 and seen["plan"] == {"plan": None}, f"{side}: /api/plan answers no plan: {seen['plan']}")
    check(all(v is None for v in seen["scope"].values()), f"{side}: plan_scope is null on every ticket: {seen['scope']}")
    for device in ("desktop", "phone"):
        page = seen["page"][device]
        check(page["controls"]["hidden"] and page["controls"]["display"] == "none" and page.get("view") is None,
              f"{side}, {device}: the plan controls stay hidden: {page['controls']}")
        worked(page, f"{side}, {device}")
        check(all(v == [] for v in page["detail"].values()), f"{side}, {device}: no plan line: {page['detail']}")
    check(seen["page"]["errors"] == [], f"{side}: no page errors: {seen['page']['errors']}")


def candidate(got: dict) -> None:
    gated, status = got["gated"], got["sql"]
    check(gated["plan_status"] == 200 and gated["plan"] == {"plan": status},
          f"GET /api/plan is plan_status() exactly: {gated['plan']} vs {status}")
    check([m["id"] for m in status["milestones"]] == ["M1", "M2", "M3"] and status["milestones"][0]["replan_due"]
          and not status["milestones"][1]["replan_due"] and status["unplanned"] == {"in_progress": 1, "cancelled": 1},
          f"the board's own plan: M1's re-plan due, two unplanned: {status}")
    want = {t: {"deliverable": d, "unplanned_reason": r} for t, (d, r, _then) in SCOPED.items()}
    want.update({"PGU-940": None, "PGU-911": None})
    check(gated["scope"] == want and gated["board_scope"] == want,
          f"each ticket's plan_scope, in the ticket read and the board read: {gated['scope']} {gated['board_scope']}")
    shown = sorted(t for t in TICKETS if got["where"][t][0] not in ("done", "cancelled"))
    for device in ("desktop", "phone"):
        page = gated["page"][device]
        worked(page, device)
        controls = page["controls"]
        check(not controls["hidden"] and controls["right"] <= controls["viewport"],
              f"{device}: the plan controls show, inside the window: {controls}")
        check(controls["options"] == [["", "All tickets", ""], ["D1.1", "D1.1 Repository", "M1 Setup"],
                                      ["D2.1", "D2.1 Reader", "M2 Ingest"], ["D2.2", "D2.2 Indexer", "M2 Ingest"],
                                      ["D3.1", "D3.1 Search", "M3 Query"], ["unplanned", "Unplanned", ""]],
              f"{device}: the filter offers each deliverable under its milestone, and the unplanned: {controls['options']}")
        check(page["cards"] == {"": shown, "D2.1": ["PGU-921", "PGU-922"], "unplanned": ["PGU-930"], "D1.1": []},
              f"{device}: the filter keeps the board to a deliverable's tickets, or the unplanned: {page['cards']}")
        check(page["detail"] == {"PGU-921": ["Deliverable: D2.1 Reader, of milestone M2 (Ingest)"],
                                 "PGU-930": [f"Unplanned: {OPS_REASON}"], "PGU-940": []},
              f"{device}: the ticket view says the deliverable or why it is unplanned, and nothing else: {page['detail']}")
        view = page["view"]
        check(not view["hidden"] and got["landed"] in view["approved"] and "docs/implementation-plan.md" in view["approved"]
              and "by closing PGU-911" in view["approved"], f"{device}: the approved commit and plan file: {view['approved']}")
        check([(m["id"], m["head"], m["badges"]) for m in view["milestones"]]
              == [("M1", "M1 Setup", ["Re-plan due"]), ("M2", "M2 Ingest", []), ("M3", "M3 Query", ["Not detailed yet"])],
              f"{device}: the milestones in order; re-plan due on M1 only; M3 not detailed: {view['milestones']}")
        check([m["deliverables"] for m in view["milestones"]]
              == [[["D1.1", "D1.1 Repository", "Cancelled 1"]],
                  [["D2.1", "D2.1 Reader", "Analysis 1 · Implementation 1"], ["D2.2", "D2.2 Indexer", "no tickets"]],
                  [["D3.1", "D3.1 Search", "no tickets"]]],
              f"{device}: each deliverable with its tickets counted by stage: {view['milestones']}")
        check(view["unplanned"] == "Implementation 1 · Cancelled 1", f"{device}: the unplanned apart: {view['unplanned']}")
        check(view["right"] <= view["viewport"] and view["overflow"] <= 0, f"{device}: nothing wider than the window: {view}")
        check(page["after_view"] == {"hidden": True, "value": "D2.1", "cards": ["PGU-921", "PGU-922"]},
              f"{device}: a deliverable in the plan view opens the board filtered to it: {page['after_view']}")
        check(page["escape_closes"] is True, f"{device}: Escape closes the plan view")
    check(gated["page"]["errors"] == [], f"no page errors: {gated['page']['errors']}")
    check(int(got["off_rows"]) == len(SCOPED), f"turned off, the records stay: {got['off_rows']}")
    nothing_new(got["off"], "the gate turned off")
    for device in ("desktop", "phone"):
        check(got["off"]["page"][device]["cards"][""] == shown, f"turned off, {device}: every ticket shows")
    nothing_new(got["plain"], "a board that never ran the gate")


def main() -> int:
    if len(sys.argv) == 5 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]))))
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd850-browser.", dir="/tmp") as tmp:
        start = before_plan_gate(Path(tmp) / "start")
        if os.environ.get("SYRD850_MUTATION_SKIP_BEFORE") != "1":  # a mutant changes this tree only
            before(run(tree_at(BEFORE, Path(tmp) / "before", "scripts", "examples", "tests"), "b850b", start))
        candidate(run(ROOT, "b850a", start))
    print(f"plan_board_view_browser_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
