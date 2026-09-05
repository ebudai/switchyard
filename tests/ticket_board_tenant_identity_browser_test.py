#!/usr/bin/env python3
"""Behavior checks for tenant identity in the served ticket-board UI."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.app import ASSIGNEES, STATES, iso_now, project_name_for_project, ticket_prefix_for_project
from scripts.ticket_board.frontend import render_html
from scripts.ticket_board.project_provision import build_plan, render_board_unit
from scripts.ticket_board.server import DirectorNotifier, TicketBoardServer


def load_playwright() -> object:
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError:
        candidates = sorted((ROOT / ".venv" / "lib").glob("python*/site-packages"))
        candidates.extend(sorted(Path("/tmp/pgu-playwright-venv/lib").glob("python*/site-packages")))
        if not candidates:
            raise
        sys.path.insert(0, str(candidates[-1]))
        from playwright.sync_api import sync_playwright
    return sync_playwright


class QuietNotifier(DirectorNotifier):
    def __init__(self) -> None:
        super().__init__(sender=lambda payload: None, batch_window_seconds=0.01)


def ticket(ticket_id: str, title: str, *, body: str = "") -> dict[str, Any]:
    return {
        "id": ticket_id,
        "title": title,
        "body": body,
        "assignee": "app",
        "state": "analysis",
        "blocked_by": [],
        "blockers": [],
        "blocked_reason": "",
        "implementation": "",
        "audit_prompt": "",
        "audit_signoff": False,
        "needs_audit": True,
        "needs_inspection": False,
        "inspector_signoff": False,
        "needs_user_signoff": False,
        "user_signoff": False,
        "manually_controlled": False,
        "parked": False,
        "commit_hash": "",
        "commit_exempt": False,
        "parent_id": "",
        "created": "2026-09-05T00:00:00+00:00",
        "updated": "2026-09-05T00:00:00+00:00",
        "comments": [],
        "screenshots": [],
        "screenshots_info": [],
        "screenshot": None,
        "screenshot_available": False,
    }


class TenantBoardApp:
    store_backend = "postgres"

    def __init__(self, frames: Path, assets: Path) -> None:
        self.frame_dir = frames.resolve()
        self.asset_dir = assets.resolve()
        self.revision = 0
        self.update_calls: list[tuple[str, dict[str, Any], str | None]] = []
        self.merge_calls: list[tuple[str, str, str]] = []
        self.configure("pgu", "PGU", "PGU")

    def configure(self, project: str, project_name: str, ticket_prefix: str) -> None:
        self.project = project
        self.project_name = project_name
        self.ticket_prefix = ticket_prefix
        self.revision += 1
        self.tickets = [
            ticket(f"{ticket_prefix}-10", "Ten"),
            ticket(
                f"{ticket_prefix}-2",
                "Two",
                body=(
                    f"Related {ticket_prefix}-10 and pgu:PGU-919"
                    if ticket_prefix != "PGU"
                    else "PGU compatibility"
                ),
            ),
        ]

    def store_signature(self) -> tuple[int, tuple[tuple[str, str, str], ...]]:
        return self.revision, tuple(
            (item["id"], item["updated"], item["parent_id"]) for item in self.tickets
        )

    def snapshot(self) -> dict[str, object]:
        return {
            "project": self.project,
            "project_name": self.project_name,
            "ticket_prefix": self.ticket_prefix,
            "tickets": self.tickets,
            "errors": [],
            "states": list(STATES),
            "assignees": list(ASSIGNEES),
            "screenshots": [],
            "store_backend": self.store_backend,
            "store_path": "postgres",
            "frame_dir": str(self.frame_dir),
            "asset_dir": str(self.asset_dir),
            "refreshed_at": iso_now(),
        }

    def update_ticket(self, ticket_id: str, patch: dict[str, Any], *, caller_role: str | None = None) -> dict[str, Any]:
        self.update_calls.append((ticket_id, patch, caller_role))
        current = next(item for item in self.tickets if item["id"] == ticket_id)
        current.update(patch)
        current["updated"] = iso_now()
        self.revision += 1
        return current

    def merge_tickets(self, source_id: str, target_id: str, *, actor: str) -> dict[str, Any]:
        self.merge_calls.append((source_id, target_id, actor))
        source = next(item for item in self.tickets if item["id"] == source_id)
        target = next(item for item in self.tickets if item["id"] == target_id)
        source["state"] = "done"
        source["updated"] = iso_now()
        self.revision += 1
        return {"source": source, "target": target}


def card_ids(page: object) -> list[str]:
    return page.locator(".card .card-id").all_text_contents()


def open_ticket(page: object, ticket_id: str) -> None:
    page.locator(".card", has=page.locator(".card-id", has_text=ticket_id)).click()
    page.locator(".detail-modal").wait_for(timeout=5000)


def save_evidence(page: object, name: str) -> None:
    output = os.environ.get("SYRD10_SCREENSHOT_DIR", "").strip()
    if output:
        destination = Path(output)
        destination.mkdir(parents=True, exist_ok=True)
        page.wait_for_timeout(250)
        page.screenshot(path=str(destination / name))


def run_browser_check(playwright: object, server: TicketBoardServer, app: TenantBoardApp) -> None:
    launch_options: dict[str, object] = {"headless": True}
    system_chrome = Path("/opt/google/chrome/chrome")
    if system_chrome.is_file():
        launch_options["executable_path"] = str(system_chrome)
    browser = playwright.chromium.launch(**launch_options)
    try:
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        url = f"http://127.0.0.1:{server.server_port}/"

        page.goto(url, wait_until="domcontentloaded")
        page.locator(".card-id", has_text="PGU-2").wait_for(timeout=5000)
        save_evidence(page, "pgu-board.png")
        page.evaluate("localStorage.setItem('pgu-ticket-board:comment-draft:PGU-2', 'legacy PGU draft')")
        open_ticket(page, "PGU-2")
        comment = page.locator("textarea[placeholder='Add a comment or bounce-back note']")
        assert comment.input_value() == "legacy PGU draft"
        assert page.evaluate("localStorage.getItem('ticket-board:pgu:PGU:comment-draft:PGU-2')") == "legacy PGU draft"
        save_evidence(page, "pgu-compatibility.png")

        app.configure("syrd", "Switchyard", "SYRD")
        page.evaluate(
            """() => {
              localStorage.setItem('pgu-ticket-board:pending-write', JSON.stringify({
                path: '/api/tickets/PGU-2/actions/edit_fields', payload: {title: 'wrong tenant'},
                callerRole: 'director', ticketId: 'PGU-2', operation: 'edit_fields'
              }));
              localStorage.setItem('ticket-board:syrd:SYRD:pending-write', JSON.stringify({
                path: '/api/tickets/PGU-2/actions/edit_fields', payload: {title: 'wrong identity'},
                callerRole: 'director', ticketId: 'PGU-2', operation: 'edit_fields',
                project: 'pgu', ticketPrefix: 'PGU'
              }));
              localStorage.setItem(
                'pgu-ticket-board:comment-draft:SYRD-2',
                'existing Switchyard draft before upgrade'
              );
              // ORB-2 is opened only after the app switches to ORB below. This
              // fixture exercises ticket access/call-site isolation, rather than
              // independently pinning the defensive prefix predicate.
              localStorage.setItem(
                'pgu-ticket-board:comment-draft:ORB-2',
                'existing Orbital draft before upgrade'
              );
            }"""
        )
        page.goto(url, wait_until="domcontentloaded")
        page.locator(".card-id", has_text="SYRD-2").wait_for(timeout=5000)
        assert page.title() == "Switchyard Ticket Board"
        assert page.locator("h1").inner_text() == "Switchyard Ticket Board"
        assert page.evaluate(
            """async () => {
              const board = await fetch('/api/board').then((response) => response.json());
              const config = await fetch('/api/client-config').then((response) => response.json());
              return [board.project, board.project_name, board.ticket_prefix,
                      config.project, config.project_name, config.ticket_prefix];
            }"""
        ) == ["syrd", "Switchyard", "SYRD", "syrd", "Switchyard", "SYRD"]
        assert card_ids(page)[:2] == ["SYRD-2", "SYRD-10"]
        assert app.update_calls == []
        save_evidence(page, "switchyard-board.png")

        open_ticket(page, "SYRD-2")
        parent_input = page.locator(
            "xpath=//div[contains(@class, 'detail-modal')]"
            "//div[./div[contains(@class, 'field-label') and normalize-space()='Parent Ticket']]//input"
        )
        assert parent_input.get_attribute("placeholder") == "SYRD-123"
        assert page.get_by_label("Merge target ticket").get_attribute("placeholder") == "SYRD-123"
        assert page.get_by_role("button", name="SYRD-10").count() == 1
        assert page.get_by_role("button", name="pgu:PGU-919").count() == 1
        assert page.get_by_role("button", name="pgu:PGU-919").is_disabled()
        comment = page.locator("textarea[placeholder='Add a comment or bounce-back note']")
        assert comment.input_value() == "existing Switchyard draft before upgrade"
        assert page.evaluate("localStorage.getItem('ticket-board:syrd:SYRD:comment-draft:SYRD-2')") == "existing Switchyard draft before upgrade"
        assert page.evaluate("localStorage.getItem('pgu-ticket-board:comment-draft:SYRD-2')") is None
        assert page.evaluate("localStorage.getItem('pgu-ticket-board:comment-draft:ORB-2')") == "existing Orbital draft before upgrade"
        assert page.evaluate("localStorage.getItem('ticket-board:syrd:SYRD:comment-draft:ORB-2')") is None

        parent_input.fill("syrd-10")
        page.get_by_role("button", name="Save Parent Link").click()
        page.wait_for_timeout(500)
        assert app.update_calls[-1] == ("SYRD-2", {"parent_id": "SYRD-10"}, "director")
        assert parent_input.input_value() == "SYRD-10"
        page.get_by_role("button", name="Clear Parent Link").click()
        page.wait_for_timeout(500)
        assert app.update_calls[-1] == ("SYRD-2", {"parent_id": ""}, "director")

        comment.fill("Switchyard draft survives reload")
        page.reload(wait_until="domcontentloaded")
        page.locator(".card-id", has_text="SYRD-2").wait_for(timeout=5000)
        open_ticket(page, "SYRD-2")
        assert page.locator("textarea[placeholder='Add a comment or bounce-back note']").input_value() == "Switchyard draft survives reload"
        assert page.evaluate("localStorage.getItem('pgu-ticket-board:comment-draft:PGU-2')") is None
        save_evidence(page, "switchyard-identity-and-draft.png")

        page.evaluate(
            """() => {
              localStorage.setItem(
                'ticket-board:syrd:SYRD:comment-draft:SYRD-2',
                'current draft'
              );
              localStorage.setItem(
                'pgu-ticket-board:comment-draft:SYRD-2',
                'STALE pre-upgrade text'
              );
            }"""
        )
        assert page.evaluate(
            "localStorage.getItem('ticket-board:syrd:SYRD:comment-draft:SYRD-2')"
        ) == "current draft"
        assert page.evaluate(
            "localStorage.getItem('pgu-ticket-board:comment-draft:SYRD-2')"
        ) == "STALE pre-upgrade text"

        page.reload(wait_until="domcontentloaded")
        page.locator(".card-id", has_text="SYRD-2").wait_for(timeout=5000)
        open_ticket(page, "SYRD-2")
        comment = page.locator("textarea[placeholder='Add a comment or bounce-back note']")
        assert comment.input_value() == "current draft"
        assert page.evaluate(
            "localStorage.getItem('pgu-ticket-board:comment-draft:SYRD-2')"
        ) == "STALE pre-upgrade text"

        comment.fill("")
        page.wait_for_timeout(300)
        assert page.evaluate(
            "localStorage.getItem('ticket-board:syrd:SYRD:comment-draft:SYRD-2')"
        ) is None
        assert page.evaluate(
            "localStorage.getItem('pgu-ticket-board:comment-draft:SYRD-2')"
        ) is None

        page.reload(wait_until="domcontentloaded")
        page.locator(".card-id", has_text="SYRD-2").wait_for(timeout=5000)
        open_ticket(page, "SYRD-2")
        assert page.locator(
            "textarea[placeholder='Add a comment or bounce-back note']"
        ).input_value() == ""
        assert page.evaluate(
            "localStorage.getItem('ticket-board:syrd:SYRD:comment-draft:SYRD-2')"
        ) is None
        assert page.evaluate(
            "localStorage.getItem('pgu-ticket-board:comment-draft:SYRD-2')"
        ) is None

        page.once("dialog", lambda dialog: dialog.accept())
        page.get_by_label("Merge target ticket").fill("syrd-10")
        page.get_by_role("button", name="Merge Into…").click()
        page.wait_for_function("() => document.querySelector('.detail-ticket-headline strong')?.textContent === 'SYRD-10'")
        assert app.merge_calls == [("SYRD-2", "SYRD-10", "director")]

        app.configure("orbit", "Orbital Ops", "ORB")
        page.goto(url, wait_until="domcontentloaded")
        page.locator(".card-id", has_text="ORB-2").wait_for(timeout=5000)
        assert page.title() == "Orbital Ops Ticket Board"
        assert page.locator("h1").inner_text() == "Orbital Ops Ticket Board"
        assert card_ids(page)[:2] == ["ORB-2", "ORB-10"]
        save_evidence(page, "arbitrary-tenant-board.png")
        open_ticket(page, "ORB-2")
        assert page.get_by_label("Merge target ticket").get_attribute("placeholder") == "ORB-123"
        assert page.locator("textarea[placeholder='Add a comment or bounce-back note']").input_value() == "existing Orbital draft before upgrade"
        assert page.evaluate("localStorage.getItem('ticket-board:orbit:ORB:comment-draft:ORB-2')") == "existing Orbital draft before upgrade"
        assert page.evaluate("localStorage.getItem('pgu-ticket-board:comment-draft:ORB-2')") is None
        save_evidence(page, "arbitrary-tenant.png")
    finally:
        browser.close()


def main() -> int:
    assert project_name_for_project(environ={"PGU_TICKET_BOARD_PROJECT_NAME": "PGU Archive"}) == "PGU Archive"
    assert ticket_prefix_for_project(environ={"PGU_TICKET_BOARD_TICKET_PREFIX": "OLD"}) == "OLD"
    hostile = render_html(project="safe", project_name='A&B </title><script>alert("x")</script>', ticket_prefix="SAFE")
    assert "<script>alert" not in hostile
    assert "A&amp;B &lt;/title&gt;&lt;script&gt;" in hostile
    assert r"A\u0026B \u003c/title\u003e\u003cscript\u003e" in hostile

    plan = build_plan(project="orbit", project_name="Orbital Ops", owner_user="orbit-agent", ticket_prefix="ORB")
    assert plan.project_name == "Orbital Ops"
    assert 'Environment="TICKET_BOARD_PROJECT_NAME=Orbital Ops"' in render_board_unit(plan)

    sync_playwright = load_playwright()
    with tempfile.TemporaryDirectory(prefix="tenant-board-identity.") as tmpdir:
        root = Path(tmpdir)
        frames = root / "frames"
        assets = root / "assets"
        frames.mkdir()
        assets.mkdir()
        app = TenantBoardApp(frames, assets)
        server = TicketBoardServer(("127.0.0.1", 0), app, director_notifier=QuietNotifier())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                run_browser_check(playwright, server, app)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    print("ticket_board_tenant_identity_browser_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
