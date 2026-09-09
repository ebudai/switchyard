#!/usr/bin/env python3
"""SYRD-83: the Director's generic edit is reachable, and bounded, from the board.

The operation, the API and the CLI are covered against a real cluster elsewhere.
What this covers is the surface a human actually uses: that the panel offers one
Director-only control, that using it sends exactly one authenticated
`director_edit` action carrying the fields and the reason -- rather than the
decomposition into workflow actions every other field in that panel goes through
-- and that a refusal from the database is surfaced rather than swallowed.
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

        control = page.locator(".detail-modal .director-edit")
        control.wait_for(timeout=5000)
        select = control.locator("select")
        value = control.locator("[data-director-edit-value]")
        reason = control.locator("[data-director-edit-reason]")
        apply_button = control.locator("[data-director-edit-apply]")

        # A reason is not optional, and nothing is sent without one.
        select.select_option("assignee")
        value.fill("ops")
        apply_button.click()
        page.wait_for_timeout(200)
        assert app.edits == [], app.edits

        reason.fill("reworking after the audit found the wrong scope")
        apply_button.click()
        page.wait_for_function(
            "() => document.querySelectorAll('.detail-modal .director-edit').length > 0",
            timeout=5000,
        )
        page.wait_for_timeout(300)

        # Exactly one authenticated action, carrying the field and the reason --
        # and not the decomposition every other field in this panel takes.
        assert len(app.edits) == 1, app.edits
        ticket_id, patch, why, caller = app.edits[0]
        assert ticket_id == "PGU-601", app.edits
        assert patch == {"assignee": "ops"}, patch
        assert why == "reworking after the audit found the wrong scope", why
        assert caller == "director", caller
        assert app.update_calls == [], app.update_calls
        assert app.tickets[0]["assignee"] == "ops"

        # And the boundary holds through this surface too: raising a sign-off is
        # refused, the ticket is unchanged, and the refusal is shown.
        select.select_option("audit_signoff")
        value.fill("true")
        reason.fill("trying to approve my own rework")
        apply_button.click()
        page.wait_for_timeout(400)
        assert len(app.edits) == 2, app.edits
        assert app.edits[1][1] == {"audit_signoff": True}, app.edits[1]
        assert app.tickets[0]["audit_signoff"] is True, app.tickets[0]
        status = page.locator(".create-status, .status, [data-status]").first
        assert "sign-off" in (status.text_content() or "").lower(), status.text_content()
    finally:
        browser.close()


def main() -> int:
    # The control exists in the served document, and it is the generic edit
    # rather than another workflow action wearing a field's clothes.
    assert "DIRECTOR_EDITABLE_FIELDS" in HTML
    assert "data-director-edit-apply" in HTML
    assert "'director_edit'," in HTML
    assert "a Director edit requires a reason" in HTML
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
