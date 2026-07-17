#!/usr/bin/env python3
"""Regression test: frontend exposes paste-backed attachment galleries."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert "pendingCreateScreenshots" in HTML
    assert ".attachment-gallery" in HTML
    assert ".attachment-set-list" in HTML
    assert ".attach-panel" in HTML
    assert "id=\"createImageInput\" class=\"visually-hidden\" type=\"file\" accept=\"image/*\" multiple" in HTML
    assert "id=\"createAttachImageBtn\" type=\"button\">Attach image</button>" in HTML
    assert "data-attach-set" in HTML
    assert "data-attach-attempt" in HTML
    assert "data-attach-label" in HTML
    assert "function uploadSetQuery(setOptions = {})" in HTML
    assert "function readAttachmentSetOptions(root)" in HTML
    assert "function attachImageFiles(files, context, setOptions = {})" in HTML
    assert "function wireImageDropZone(dropZone, context, getSetOptions = () => ({}))" in HTML
    assert "await attachImageFiles(detailImageInput.files, 'detail', readAttachmentSetOptions(attachmentUpload));" in HTML
    assert "wireImageDropZone(attachmentUpload, 'detail', () => readAttachmentSetOptions(attachmentUpload));" in HTML
    assert "await attachImageFiles(createImageInput.files, 'create', readCreateAttachmentSetOptions());" in HTML
    assert "wireImageDropZone(createAttachDropZone, 'create', readCreateAttachmentSetOptions);" in HTML
    assert "selected ticket changed during image attach" in HTML
    assert "function parseAttachmentSet(path)" in HTML
    assert "function groupAttachmentEntries(entries)" in HTML
    assert "function renderAttachmentSetGroups(container, entries, removeLabel, onRemove, onOpen = null)" in HTML
    assert "filename.match(/^target__(.+)$/i)" in HTML
    assert "filename.match(/^attempt-(\\d+)(?:-(.+?))?__(.+)$/i)" in HTML
    assert "filename.match(/^feedback-(\\d+)(?:-(.+?))?__(.+)$/i)" in HTML
    assert "label: `Feedback #${feedbackNumber}${suffix ? ` - ${suffix}` : ''}`" in HTML
    assert "right.feedbackNumber - left.feedbackNumber" in HTML
    assert "newestFeedback.open = true;" in HTML
    assert "filename.match(/^([a-z0-9][a-z0-9-]*)__(.+)$/i)" in HTML
    assert "newestAttempt.open = true;" in HTML
    assert ".attachment-remove" in HTML
    assert ".attachment-card-clickable" in HTML
    assert "ticketScreenshotEntries(ticket).length" in HTML
    assert "screenshots: ticketScreenshotPaths(ticket).filter((item) => item !== path)" in HTML
    assert "renderAttachmentSetGroups(groups, entries, 'Remove attachment'" in HTML
    assert "state.pendingCreateScreenshots = uniquePaths([...state.pendingCreateScreenshots, uploaded.path]);" in HTML
    assert "imageLightboxOverlay" in HTML
    assert "imageLightboxCloseBtn" in HTML
    assert "openImageLightbox(entry);" in HTML
    assert "Open attachment full size:" in HTML
    assert "Available Frame" not in HTML
    assert "screenshotInput" not in HTML
    assert "screenshotSelect" not in HTML
    print("multi_attachment_frontend_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
