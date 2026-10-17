#!/usr/bin/env python3
"""SYRD-83, narrowed by SYRD-598: the Director's generic edit is the Director pane's, never the board page's.

SYRD-83 put a generic Director Edit on the board page. SYRD-598 makes the page act
as the User only, and the Director ruled the generic edit never delegable: it can
move any field at any stage, which no confirmation of a concrete action bounds.
What this covers is the surface a human actually uses: the page offers no
Director Edit, its own request for one is the User's and refused, and naming the
Director in the request does not make it the Director's. The operation, the API
and the CLI through the Director's own pane are covered against a real cluster
elsewhere.
"""

from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.app import ASSIGNEES, STATES, iso_now
from scripts.ticket_board.frontend import HTML
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


class DirectorEditBoardApp:
    store_backend = "postgres"

    def __init__(self, frames: Path, assets: Path) -> None:
        self.frame_dir = frames.resolve()
        self.asset_dir = assets.resolve()
        self.edits: list[tuple[str, dict[str, Any], str, str | None]] = []
        self.update_calls: list[tuple[str, dict[str, Any], str | None]] = []
        self.tickets = [self.ticket("PGU-601", "Audit stage ticket")]

    def ticket(self, ticket_id: str, title: str) -> dict[str, Any]:
        return {
            "id": ticket_id,
            "title": title,
            "body": "",
            "assignee": "audit",
            "state": "audit",
            "blocked_by": [],
            "blockers": [],
            "blocked_reason": "",
            "implementation": "",
            "audit_prompt": "",
            "audit_signoff": True,
            "needs_audit": True,
            "needs_inspection": False,
            "inspector_signoff": False,
            "needs_user_signoff": False,
            "user_signoff": False,
            "manually_controlled": False,
            "parked": False,
            "regression": False,
            "commit_hash": "",
            "commit_exempt": False,
            "parent_id": "",
            "origin_project": "",
            "external_source_ref": "",
            "queued_for_assignee": "",
            "queued_behind_ticket": "",
            "workflow_flags": {},
            "created": "2026-07-17T00:00:00+00:00",
            "updated": "2026-07-17T00:00:00+00:00",
            "comments": [],
            "screenshots": [],
            "screenshots_info": [],
            "screenshot": None,
            "screenshot_available": False,
        }

    def store_signature(self) -> tuple[tuple[str, str, str], ...]:
        return tuple(
            (ticket["id"], ticket["updated"], str(ticket["audit_signoff"])) for ticket in self.tickets
        )

    def snapshot(self) -> dict[str, object]:
        return {
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
        # Recorded so the test can prove the panel did NOT fall back to the
        # ordinary decomposition for a Director edit.
        self.update_calls.append((ticket_id, patch, caller_role))
        return self.tickets[0]

    def director_edit_ticket(
        self, ticket_id: str, patch: Any, *, reason: str, caller_role: str | None = None
    ) -> dict[str, Any]:
        self.edits.append((ticket_id, dict(patch), reason, caller_role))
        # The database's own boundary, modelled where the UI meets it: a
        # sign-off may be cleared and never raised.
        for field in ("audit_signoff", "inspector_signoff", "user_signoff"):
            if patch.get(field) is True:
                raise ValueError(
                    f"director_edit cannot create sign-off {field}; only its own sign-off action may"
                )
        ticket = self.tickets[0]
        ticket.update(patch)
        ticket["updated"] = iso_now()
        return ticket


def run_browser_check(playwright: object, server_port: int, app: DirectorEditBoardApp) -> None:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(f"http://127.0.0.1:{server_port}/", wait_until="domcontentloaded")
        page.locator(".card", has=page.locator(".card-id", has_text="PGU-601")).click()
        page.locator(".detail-modal").wait_for(timeout=5000)

        page.locator("[data-board-ui-actions='PGU-601']").wait_for(timeout=5000)
        assert page.locator(".detail-modal .director-edit, [data-director-edit-apply]").count() == 0

        def post(caller: str) -> list:
            return page.evaluate("""async (caller) => {
              const headers = { ...ticketWriteHeaders(), 'X-Ticket-Board-Caller-Role': caller };
              const response = await fetch('/api/tickets/PGU-601/actions/director_edit', { method: 'POST', headers,
                body: JSON.stringify({ patch: { assignee: 'ops' }, reason: 'reworking after the audit' }) });
              return [response.status, await response.text()];
            }""", caller)

        # The page's own request is the User's, and the User has no generic edit.
        as_user = post("user")
        assert as_user[0] == 403 and "user cannot call director_edit" in as_user[1], as_user
        # Naming the Director over HTTP does not make the request the Director's.
        as_director = post("director")
        assert as_director[0] == 403 and "over HTTP a request acts as the User, not director" in as_director[1], as_director
        assert app.edits == [] and app.update_calls == [], (app.edits, app.update_calls)
        assert app.tickets[0]["assignee"] == "audit"
    finally:
        browser.close()


def main() -> int:
    # The served document has no generic edit, and no path that would send one.
    assert "DIRECTOR_EDITABLE_FIELDS" not in HTML
    assert "data-director-edit-apply" not in HTML
    assert "'director_edit'," not in HTML
    assert "a Director edit requires a reason" not in HTML
    sync_playwright = load_playwright()
    with tempfile.TemporaryDirectory(prefix="director-edit-browser.") as tmpdir:
        root = Path(tmpdir)
        frames = root / "frames"
        assets = root / "assets"
        frames.mkdir()
        assets.mkdir()

        app = DirectorEditBoardApp(frames, assets)
        server = TicketBoardServer(("127.0.0.1", 0), app, director_notifier=QuietNotifier())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                run_browser_check(playwright, server.server_port, app)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    print("director_edit_browser_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
