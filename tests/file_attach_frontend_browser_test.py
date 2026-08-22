#!/usr/bin/env python3
"""Regression test: visible file attach control uploads to the selected ticket."""

from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.app import ASSIGNEES, CALLER_ROLES, STATES, format_timestamp, iso_now
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


class MutableAttachApp:
    store_backend = "postgres"

    def __init__(self, ticket: dict[str, Any], frames: Path, assets: Path) -> None:
        self.ticket = ticket
        self.frame_dir = frames.resolve()
        self.asset_dir = assets.resolve()
        self.upload_count = 0

    def store_signature(self) -> tuple[tuple[object, ...], ...]:
        return ((self.ticket["id"], self.ticket["updated"], len(self.ticket["screenshots"])),)

    def snapshot(self) -> dict[str, object]:
        return {
            "tickets": [self.ticket],
            "errors": [],
            "states": list(STATES),
            "assignees": list(ASSIGNEES),
            "caller_roles": list(CALLER_ROLES),
            "screenshots": [],
            "store_backend": self.store_backend,
            "store_path": "postgres",
            "frame_dir": str(self.frame_dir),
            "asset_dir": str(self.asset_dir),
            "refreshed_at": iso_now(),
        }

    def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        if ticket_id != self.ticket["id"]:
            raise FileNotFoundError(ticket_id)
        return self.ticket

    def update_ticket(self, ticket_id: str, patch: dict[str, Any], *, caller_role: str | None = None) -> dict[str, Any]:
        if ticket_id != self.ticket["id"]:
            raise FileNotFoundError(ticket_id)
        screenshots = [str(path) for path in patch.get("screenshots", self.ticket["screenshots"])]
        self.ticket["screenshots"] = screenshots
        self.ticket["screenshots_info"] = [{"path": path, "available": True} for path in screenshots]
        self.ticket["screenshot"] = screenshots[0] if screenshots else None
        self.ticket["screenshot_available"] = bool(screenshots)
        self.ticket["updated"] = iso_now()
        self.ticket["last_caller_role"] = caller_role
        return self.ticket

    def save_uploaded_image(
        self,
        raw_bytes: bytes,
        *,
        upload_set: str = "",
        set_label: str = "",
        attempt_number: str = "",
        original_filename: str = "",
    ) -> dict[str, str]:
        self.upload_count += 1
        self.asset_dir.mkdir(parents=True, exist_ok=True)
        prefix = "target__" if upload_set == "target" else ""
        path = self.asset_dir / f"{prefix}upload_{self.upload_count}.png"
        path.write_bytes(raw_bytes)
        return {"path": str(path), "name": path.name, "modified": format_timestamp(path)}

    def resolve_image(self, raw_path: str) -> Path:
        path = Path(raw_path).resolve()
        if self.asset_dir not in path.parents or not path.is_file():
            raise FileNotFoundError(raw_path)
        return path


def ticket_payload() -> dict[str, Any]:
    return {
        "id": "PGU-476",
        "title": "Visible attach control",
        "body": "Attach an image from disk.",
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
        "regression": False,
        "parent_id": "",
        "created": "2026-07-16T00:00:00+00:00",
        "updated": "2026-07-16T00:00:00+00:00",
        "comments": [],
        "screenshots": [],
        "screenshots_info": [],
        "screenshot": None,
        "screenshot_available": False,
    }


def run_browser_check(playwright: object, server_port: int, image_path: Path) -> None:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(f"http://127.0.0.1:{server_port}/", wait_until="domcontentloaded")
        page.get_by_text("PGU-476", exact=True).wait_for(timeout=5000)
        page.get_by_text("PGU-476", exact=True).click()
        page.locator(".detail-modal").wait_for(timeout=5000)
        detail_attach = page.locator(".detail-modal .detail-attach-panel")
        detail_attach.get_by_role("button", name="Attach image").wait_for(timeout=5000)
        detail_attach.locator("input[type=file]").set_input_files(str(image_path))
        page.locator(".detail-modal .attachment-set-list").wait_for(timeout=5000)
        assert page.locator(".detail-modal .attachment-card", has_text="upload_1.png").count() == 1
        assert page.locator("#createStatus").inner_text() == "Attached image to PGU-476."
    finally:
        browser.close()


def main() -> int:
    sync_playwright = load_playwright()
    with tempfile.TemporaryDirectory(prefix="file-attach-frontend.") as tmpdir:
        root = Path(tmpdir)
        frames = root / "frames"
        assets = root / "assets"
        frames.mkdir()
        assets.mkdir()
        upload_source = root / "source.png"
        Image.new("RGB", (32, 32), color=(80, 150, 210)).save(upload_source)

        app = MutableAttachApp(ticket_payload(), frames, assets)
        server = TicketBoardServer(("127.0.0.1", 0), app, director_notifier=QuietNotifier())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                run_browser_check(playwright, server.server_port, upload_source)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        assert app.ticket["screenshots"] == [str(assets / "upload_1.png")]
        assert app.ticket["last_caller_role"] == "director"

    print("file_attach_frontend_browser_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
