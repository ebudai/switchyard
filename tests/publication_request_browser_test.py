#!/usr/bin/env python3
"""SYRD-93: the handoff is legible on the board, and refusable there.

The operations, the CLI and the privileged publisher are covered against a real
cluster and a real remote elsewhere. What this covers is what a human sees: an
implementer's ask is on the ticket, the control role is told exactly what it is
being asked to validate and how to publish it, and refusing takes a reason and
goes out as one authenticated action.

Publishing itself is deliberately absent from this surface. It needs a
credential no browser can reach, which is the whole point of the boundary.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from director_edit_browser_test import (  # noqa: E402
    DirectorEditBoardApp,
    QuietNotifier,
    load_playwright,
)
from scripts.ticket_board.app import iso_now  # noqa: E402
from scripts.ticket_board.workflow_config import validate  # noqa: E402
from scripts.ticket_board.frontend import HTML  # noqa: E402
from scripts.ticket_board.server import TicketBoardServer  # noqa: E402

COMMIT = "9f1c2d3e4b5a69788796a5b4c3d2e1f0a1b2c3d4"
BUNDLE = "/home/agent/.local/state/switchyard/publish-outbox/cerulean/PGU-602-9f1c2d3e4b5a.bundle"


class PublicationBoardApp(DirectorEditBoardApp):
    """The same fake board, declarative, with one ticket waiting to publish.

    Declarative on purpose: publication is admitted by declared capability and
    by nothing else, so a board with no document refuses it -- and a fake that
    had no document would be testing that refusal instead of the panel.
    """

    def __init__(self, frames: Path, assets: Path) -> None:
        super().__init__(frames, assets)
        self.configuration = validate(
            json.loads((ROOT / "examples/workflows/inspection.json").read_text())
        )
        self.resolutions: list[tuple[int, str, str, str | None]] = []
        waiting = self.ticket("PGU-601", "Defer the backlog")
        waiting["assignee"] = "ops"
        waiting["state"] = "in_progress"
        waiting["audit_signoff"] = False
        waiting["publication"] = {
            "id": 12,
            "ticket_id": "PGU-601",
            "requested_by": "ops",
            "ref": "roles/ops/syrd-92-defer-backlog",
            "commit_hash": COMMIT,
            "bundle_path": BUNDLE,
            "state": "requested",
        }
        self.tickets = [waiting]

    def workflow_configuration(self) -> dict:
        return self.configuration

    def workflow_roles(self) -> list[str]:
        return [role["name"] for role in self.configuration["roles"] if role["active"]]

    def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        return next(ticket for ticket in self.tickets if ticket["id"] == ticket_id)

    def resolve_publication(
        self, request_id: int, *, outcome: str, detail: str = "", caller_role: str | None = None
    ) -> dict[str, Any]:
        self.resolutions.append((int(request_id), outcome, detail, caller_role))
        ticket = self.tickets[0]
        ticket["publication"] = {}
        ticket["updated"] = iso_now()
        return {"request": {"id": request_id, "state": outcome}, "ticket": ticket}


def run_browser_check(playwright: object, server_port: int, app: PublicationBoardApp) -> None:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(f"http://127.0.0.1:{server_port}/", wait_until="domcontentloaded")
        page.locator(".card", has=page.locator(".card-id", has_text="PGU-601")).click()
        page.locator(".detail-modal").wait_for(timeout=5000)

        block = page.locator(".detail-modal .publication-request")
        block.wait_for(timeout=5000)
        summary = (block.locator("[data-publication-summary]").text_content() or "")
        # Exactly what needs validating: who, which ref, which commit, and the
        # bundle that carries it.
        assert "ops asks to publish roles/ops/syrd-92-defer-backlog" in summary, summary
        assert COMMIT[:12] in summary, summary
        assert BUNDLE in summary, summary
        command = (block.locator("[data-publication-command]").text_content() or "")
        assert "switchyard-publish PGU-601" in command, command

        reason = block.locator("[data-publication-reject-reason]")
        reject = block.locator("[data-publication-reject]")

        # Refusing takes a reason: without one, nothing is sent.
        reject.click()
        page.wait_for_timeout(200)
        assert app.resolutions == [], app.resolutions

        reason.fill("the bundle carries a commit nobody reviewed")
        reject.click()
        page.wait_for_timeout(400)

        # One authenticated decision, naming the request and the outcome.
        assert len(app.resolutions) == 1, app.resolutions
        request_id, outcome, detail, caller = app.resolutions[0]
        assert request_id == 12, app.resolutions
        assert outcome == "rejected", app.resolutions
        assert detail == "the bundle carries a commit nobody reviewed", app.resolutions
        assert caller == "director", app.resolutions
        # And not the ordinary field decomposition every other control takes.
        assert app.update_calls == [], app.update_calls
        assert app.edits == [], app.edits
    finally:
        browser.close()


def main() -> int:
    # The surface exists in the served document, and publishing is not on it.
    assert "data-publication-reject" in HTML
    assert "'resolve_publication'," in HTML
    assert "a publication decision requires a reason" in HTML
    assert "switchyard-publish " in HTML

    sync_playwright = load_playwright()
    with tempfile.TemporaryDirectory(prefix="publication-browser.") as tmpdir:
        root = Path(tmpdir)
        frames = root / "frames"
        assets = root / "assets"
        frames.mkdir()
        assets.mkdir()
        app = PublicationBoardApp(frames, assets)
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
    print("publication_request_browser_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
