"""Image filename, geometry and path policy for board attachments.

Functions take current directory paths explicitly. TicketBoardApp keeps file
writes, ticket authorization and database transactions.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


def format_timestamp(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def upload_set_slug(raw: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(raw or "").strip().lower()).strip("-")
    return slug[:80]


def uploaded_filename_slug(raw: str) -> str:
    name = Path(str(raw or "").replace("\\", "/")).name
    suffix = Path(name).suffix.lower()
    stem = name[: -len(suffix)] if suffix else name
    slug = re.sub(r"[^A-Za-z0-9]+", "-", stem.strip().lower()).strip("-")[:120]
    if not slug:
        return ""
    if suffix not in IMAGE_EXTENSIONS:
        suffix = ".png"
    return f"{slug}{suffix}"


def crop_filename_slug(raw: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(raw or "").strip().lower()).strip("-")
    return slug[:80] or "render"


def normalize_crop_rect(raw: dict[str, Any], image_width: int, image_height: int) -> dict[str, int]:
    def number(name: str) -> int:
        try:
            return int(round(float(raw.get(name))))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"crop rect requires numeric {name}") from exc

    x = number("x")
    y = number("y")
    w = number("w")
    h = number("h")
    if w <= 0 or h <= 0:
        raise ValueError("crop width and height must be positive")
    x = max(0, min(x, image_width - 1))
    y = max(0, min(y, image_height - 1))
    w = max(1, min(w, image_width - x))
    h = max(1, min(h, image_height - y))
    return {"x": x, "y": y, "w": w, "h": h}


def next_feedback_number(ticket: dict[str, Any]) -> int:
    highest = 0
    for path in ticket.get("screenshots", []) or []:
        match = re.search(r"(?:^|/)feedback-(\d+)", str(path))
        if match:
            highest = max(highest, int(match.group(1)))
    return min(highest + 1, 999)


def dedupe_asset_path(asset_dir: Path, filename: str) -> Path:
    candidate = asset_dir / filename
    if not candidate.exists():
        return candidate
    suffix = candidate.suffix
    stem = candidate.stem
    for index in range(2, 10000):
        candidate = asset_dir / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise ValueError(f"could not allocate unique upload filename for {filename}")


def image_save_format(suffix: str) -> str:
    normalized = suffix.lower()
    if normalized in {".jpg", ".jpeg"}:
        return "JPEG"
    if normalized == ".webp":
        return "WEBP"
    return "PNG"


def upload_filename_prefix(upload_set: str, set_label: str, attempt_number: str) -> str:
    normalized_set = upload_set_slug(upload_set)
    label_slug = upload_set_slug(set_label)
    if normalized_set in {"", "ungrouped"}:
        return ""
    if normalized_set == "target":
        return "target"
    if normalized_set == "attempt":
        try:
            attempt = int(str(attempt_number).strip())
        except ValueError as exc:
            raise ValueError("attempt upload set requires an attempt number") from exc
        if attempt <= 0 or attempt > 999:
            raise ValueError("attempt upload set requires attempt number 1-999")
        prefix = f"attempt-{attempt:03d}"
        if label_slug:
            prefix = f"{prefix}-{label_slug}"
        return prefix
    if normalized_set == "feedback":
        try:
            feedback = int(str(attempt_number).strip())
        except ValueError as exc:
            raise ValueError("feedback upload set requires a feedback number") from exc
        if feedback <= 0 or feedback > 999:
            raise ValueError("feedback upload set requires feedback number 1-999")
        prefix = f"feedback-{feedback:03d}"
        if label_slug:
            prefix = f"{prefix}-{label_slug}"
        return prefix
    return label_slug or normalized_set


def path_in_allowed_image_dirs(path: Path, frame_dir: Path, asset_dir: Path) -> bool:
    return frame_dir in path.parents or asset_dir in path.parents


def path_in_asset_dir(path: Path, asset_dir: Path) -> bool:
    return asset_dir == path or asset_dir in path.parents


def normalize_image_path(raw: str) -> str:
    return str(Path(raw).expanduser().resolve())
