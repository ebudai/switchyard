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
        "needs_eric_signoff": False,
        "eric_signoff": False,
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


def run_browser_check(playwright: object, server_port: int, first_name: str, second_name: str) -> None:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page(viewport={"width": 1440, "height": 1024})
        page.goto(f"http://127.0.0.1:{server_port}/", wait_until="domcontentloaded")
        page.get_by_text("PGU-420", exact=True).wait_for(timeout=5000)
        page.get_by_text("PGU-420", exact=True).click()

        page.locator(".detail-modal").wait_for(timeout=5000)
        gallery = page.locator(".detail-modal .attachment-gallery")
        gallery.wait_for(timeout=5000)
        assert gallery.locator(".attachment-card-clickable").count() == 2

        page.locator(".detail-modal .attachment-card-clickable", has_text=first_name).click()
        page.locator("#imageLightboxOverlay").wait_for(timeout=5000)
        page.wait_for_function(
            "() => { const img = document.getElementById('imageLightboxImage'); return !!img && img.complete && img.naturalWidth > 0; }"
        )
        assert page.locator("#imageLightboxCaption").inner_text() == first_name
        assert "/api/image/" in (page.locator("#imageLightboxImage").get_attribute("src") or "")
        assert page.locator(".detail-modal").is_visible()

        page.keyboard.press("Escape")
        page.locator("#imageLightboxOverlay").wait_for(state="hidden", timeout=5000)
        assert page.locator(".detail-modal").is_visible()

        page.locator(".detail-modal .attachment-card-clickable", has_text=second_name).click()
        page.locator("#imageLightboxOverlay").wait_for(timeout=5000)
        assert page.locator("#imageLightboxCaption").inner_text() == second_name
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
        first = assets / "spiral-arm.png"
        second = assets / "bulge-core.png"
        write_png(first, (48, 96, 180))
        write_png(second, (180, 120, 48))

        app = StaticBoardApp([ticket_payload("PGU-420", [first, second])], frames, assets)
        server = TicketBoardServer(("127.0.0.1", 0), app, director_notifier=QuietNotifier())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                run_browser_check(playwright, server.server_port, first.name, second.name)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    print("attachment_lightbox_browser_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
