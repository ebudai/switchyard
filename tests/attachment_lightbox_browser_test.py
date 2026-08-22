#!/usr/bin/env python3
"""Regression test: detail attachments open into an inline lightbox viewer."""

from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.app import ASSIGNEES, STATES, iso_now
from scripts.ticket_board.server import DirectorNotifier, TicketBoardServer


def load_playwright() -> object:
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError:
        repo_venv = Path.home() / "Projects" / "pgu" / ".venv" / "lib"
        candidates = sorted(repo_venv.glob("python*/site-packages"))
        if not candidates:
            raise
        sys.path.insert(0, str(candidates[-1]))
        from playwright.sync_api import sync_playwright
    return sync_playwright


class QuietNotifier(DirectorNotifier):
    def __init__(self) -> None:
        super().__init__(sender=lambda payload: None, batch_window_seconds=0.01)


class StaticBoardApp:
    store_backend = "postgres"

    def __init__(self, tickets: list[dict[str, object]], frames: Path, assets: Path) -> None:
        self.tickets = tickets
        self.frame_dir = frames.resolve()
        self.asset_dir = assets.resolve()

    def store_signature(self) -> tuple[tuple[str, str], ...]:
        return tuple((str(ticket["id"]), str(ticket["updated"])) for ticket in self.tickets)

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

    def resolve_image(self, raw_path: str) -> Path:
        path = Path(raw_path).resolve()
        if self.asset_dir not in path.parents:
            raise ValueError(f"invalid image path: {raw_path}")
        return path


def write_png(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 48), color=color).save(path)


def ticket_payload(ticket_id: str, screenshots: list[Path]) -> dict[str, object]:
    screenshot_paths = [str(path) for path in screenshots]
    return {
        "id": ticket_id,
        "title": "Ticket detail screenshots should open inline",
        "body": "Open the attachment from ticket detail and inspect it inline.",
        "assignee": "ops",
        "state": "analysis",
        "blocked_by": [],
        "blocked_reason": "",
        "implementation": "",
        "audit_prompt": "",
        "audit_signoff": False,
        "needs_inspection": False,
        "inspector_signoff": False,
        "needs_user_signoff": False,
        "user_signoff": False,
        "manually_controlled": False,
        "parked": False,
        "commit_hash": "",
        "commit_exempt": False,
        "parent_id": "",
        "created": "2026-07-14T00:00:00+00:00",
        "updated": "2026-07-14T00:00:00+00:00",
        "comments": [],
        "screenshots": screenshot_paths,
        "screenshots_info": [{"path": path, "available": True} for path in screenshot_paths],
        "screenshot": screenshot_paths[0],
        "screenshot_available": True,
    }


def run_browser_check(playwright: object, server_port: int) -> None:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page(viewport={"width": 1440, "height": 1024})
        page.goto(f"http://127.0.0.1:{server_port}/", wait_until="domcontentloaded")
        page.get_by_text("PGU-420", exact=True).wait_for(timeout=5000)
        page.get_by_text("PGU-420", exact=True).click()

        page.locator(".detail-modal").wait_for(timeout=5000)
        set_list = page.locator(".detail-modal .attachment-set-list")
        set_list.wait_for(timeout=5000)
        groups = page.locator(".detail-modal .attachment-set")
        assert groups.count() == 4
        assert page.locator(".detail-modal .attachment-set-summary").nth(0).inner_text() == "Target (1 image)"
        assert page.locator(".detail-modal .attachment-set-summary").nth(1).inner_text() == "Render Attempt 002 - UAT Rework (1 image)"
        assert page.locator(".detail-modal .attachment-set-summary").nth(2).inner_text() == "Render Attempt 001 - Phase 1 (1 image)"
        assert page.locator(".detail-modal .attachment-set-summary").nth(3).inner_text() == "Ungrouped (1 image)"
        assert groups.nth(0).evaluate("node => node.open") is False
        assert groups.nth(1).evaluate("node => node.open") is True
        assert groups.nth(2).evaluate("node => node.open") is False
        assert groups.nth(3).evaluate("node => node.open") is False
        assert page.locator(".detail-modal .attachment-card-clickable").count() == 4

        page.locator(".detail-modal .attachment-card-clickable", has_text="render-new.png").click()
        page.locator("#imageLightboxOverlay").wait_for(timeout=5000)
        page.wait_for_function(
            "() => { const img = document.getElementById('imageLightboxImage'); return !!img && img.complete && img.naturalWidth > 0; }"
        )
        assert page.locator("#imageLightboxCaption").inner_text() == "render-new.png"
        assert "/api/image/" in (page.locator("#imageLightboxImage").get_attribute("src") or "")
        assert page.locator(".detail-modal").is_visible()

        page.keyboard.press("Escape")
        page.locator("#imageLightboxOverlay").wait_for(state="hidden", timeout=5000)
        assert page.locator(".detail-modal").is_visible()

        page.locator(".detail-modal .attachment-set-summary", has_text="Target").click()
        page.locator(".detail-modal .attachment-card-clickable", has_text="reference.png").click()
        page.locator("#imageLightboxOverlay").wait_for(timeout=5000)
        assert page.locator("#imageLightboxCaption").inner_text() == "reference.png"
        page.locator("#imageLightboxOverlay").click(position={"x": 8, "y": 8})
        page.locator("#imageLightboxOverlay").wait_for(state="hidden", timeout=5000)
    finally:
        browser.close()


def main() -> int:
    sync_playwright = load_playwright()
    with tempfile.TemporaryDirectory(prefix="attachment-lightbox.") as tmpdir:
        root = Path(tmpdir)
        frames = root / "frames"
        assets = root / "assets"
        frames.mkdir()
        assets.mkdir()
        target = assets / "target__reference.png"
        first_attempt = assets / "attempt-001-phase1__render-old.png"
        newest_attempt = assets / "attempt-002-uat_rework__render-new.png"
        legacy = assets / "legacy-flat.png"
        write_png(target, (48, 96, 180))
        write_png(first_attempt, (120, 72, 180))
        write_png(newest_attempt, (180, 120, 48))
        write_png(legacy, (90, 120, 80))

        app = StaticBoardApp([ticket_payload("PGU-420", [target, first_attempt, newest_attempt, legacy])], frames, assets)
        server = TicketBoardServer(("127.0.0.1", 0), app, director_notifier=QuietNotifier())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                run_browser_check(playwright, server.server_port)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    print("attachment_lightbox_browser_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
