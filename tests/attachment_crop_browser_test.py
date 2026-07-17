#!/usr/bin/env python3
"""Regression test: browser crop tool attaches feedback crops with provenance."""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.ticket_board_ui_harness import BoardHarness, load_playwright, ticket_payload, wait_for_ticket_field


def write_source_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (2000, 1500), color=(20, 30, 40))
    for x in range(2000):
        for y in range(1500):
            image.putpixel((x, y), (x * 2 % 255, y * 3 % 255, (x + y) % 255))
    image.save(path)


def run_browser_check(playwright: object) -> None:
    with BoardHarness([]) as harness:
        source = harness.assets / "attempt-001__render-original.png"
        write_source_image(source)
        harness.app.tickets["PGU-531"] = ticket_payload(
            "PGU-531",
            "Crop render feedback",
            state="in_progress",
            assignee="ops",
            screenshots=[str(source)],
        )
        harness.app._refresh_blockers()

        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(harness.url, wait_until="domcontentloaded")
            page.get_by_text("PGU-531", exact=True).wait_for(timeout=5000)
            page.get_by_text("PGU-531", exact=True).click()
            page.locator(".detail-modal .attachment-card-clickable", has_text="render-original.png").click()
            page.locator("#imageLightboxOverlay").wait_for(timeout=5000)
            page.wait_for_function(
                "() => { const img = document.getElementById('imageLightboxImage'); return !!img && img.complete && img.naturalWidth === 2000 && img.naturalHeight === 1500; }"
            )
            displayed = page.evaluate(
                """() => {
                  const img = document.getElementById('imageLightboxImage');
                  const rect = img.getBoundingClientRect();
                  return {
                    naturalWidth: img.naturalWidth,
                    naturalHeight: img.naturalHeight,
                    displayedWidth: rect.width,
                    displayedHeight: rect.height,
                  };
                }"""
            )
            assert displayed["displayedWidth"] < displayed["naturalWidth"], displayed
            assert displayed["displayedHeight"] < displayed["naturalHeight"], displayed

            page.get_by_role("button", name="Crop for feedback").click()
            drag = page.evaluate(
                """() => {
                  const img = document.getElementById('imageLightboxImage');
                  const rect = img.getBoundingClientRect();
                  return {
                    startX: rect.left + rect.width * 0.20,
                    startY: rect.top + rect.height * 0.25,
                    endX: rect.left + rect.width * 0.70,
                    endY: rect.top + rect.height * 0.75,
                  };
                }"""
            )
            page.mouse.move(drag["startX"], drag["startY"])
            page.mouse.down()
            page.mouse.move(drag["endX"], drag["endY"])
            page.mouse.up()
            page.get_by_role("button", name="Attach crop").click()
            wait_for_ticket_field(
                page,
                harness.url,
                "PGU-531",
                "ticket.screenshots_info.some((entry) => entry.metadata && entry.metadata.kind === 'crop')",
            )

            ticket = harness.ticket("PGU-531")
            crop_entry = next(entry for entry in ticket["screenshots_info"] if entry.get("metadata", {}).get("kind") == "crop")
            metadata = crop_entry["metadata"]
            crop_name = Path(crop_entry["path"]).name
            assert crop_name.startswith("feedback-001__crop-of-")
            assert "x400-y375-w1000-h750" in crop_name
            assert metadata["source_path"] == str(source)
            assert metadata["source_name"] == source.name
            assert metadata["rect"] == {"x": 400, "y": 375, "w": 1000, "h": 750}
            assert metadata["caption"] == "crop of attempt-001__render-original.png @ 400,375,1000,750"
            assert Path(crop_entry["path"]).is_file()
            with Image.open(crop_entry["path"]) as crop_image:
                assert crop_image.size == (1000, 750)

            page.evaluate(
                """() => {
                  for (const details of document.querySelectorAll('.detail-modal .attachment-set')) {
                    const summary = details.querySelector('.attachment-set-summary');
                    if (summary && summary.textContent.includes('Feedback #1')) {
                      details.open = true;
                    }
                  }
                }"""
            )
            page.wait_for_function(
                "() => Array.from(document.querySelectorAll('.detail-modal .attachment-card-clickable')).some((node) => (node.getAttribute('aria-label') || '').includes('x400-y375-w1000-h750'))",
                timeout=5000,
            )
            crop_card = page.locator(".detail-modal .attachment-card-clickable[aria-label*='x400-y375-w1000-h750']")
            assert crop_card.count() >= 1
            crop_card.nth(0).dispatch_event("click")
            assert "crop of attempt-001__render-original.png @ 400,375,1000,750" in page.locator("#imageLightboxCaption").inner_text()
        finally:
            browser.close()


def main() -> int:
    sync_playwright = load_playwright()
    with sync_playwright() as playwright:
        run_browser_check(playwright)
    print("attachment_crop_browser_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
