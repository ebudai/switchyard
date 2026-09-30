"""The board's attachment files: frames, uploads, crops and ticket copies.

Everything here reads or writes image files under the two directories a board
is configured with, the frame directory and the asset directory, which every
function takes explicitly. Filename, geometry and path policy stay in
`image_asset_policy`; ticket authorization and database transactions stay in
`TicketBoardApp` (SYRD-518).
"""

from __future__ import annotations

import time
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

from .image_asset_policy import (
    IMAGE_EXTENSIONS,
    crop_filename_slug,
    dedupe_asset_path,
    format_timestamp,
    image_save_format,
    next_feedback_number,
    normalize_crop_rect,
    normalize_image_path,
    path_in_allowed_image_dirs,
    path_in_asset_dir,
    upload_filename_prefix,
    upload_set_slug,
    uploaded_filename_slug,
)


def list_frames(frame_dir: Path) -> list[dict[str, str]]:
    if not frame_dir.is_dir():
        return []
    items: list[dict[str, str]] = []
    for path in sorted(frame_dir.glob("*.png"), key=lambda item: item.stat().st_mtime, reverse=True):
        items.append(
            {
                "path": str(path.resolve()),
                "name": path.name,
                "modified": format_timestamp(path),
            }
        )
    return items


def resolve_image(raw_path: str, frame_dir: Path, asset_dir: Path) -> Path:
    path = Path(raw_path).expanduser().resolve()
    if not path_in_allowed_image_dirs(path, frame_dir, asset_dir):
        raise FileNotFoundError(f"screenshot path escapes allowed asset roots: {path}")
    if not path.is_file():
        raise FileNotFoundError(f"screenshot not found: {path}")
    if path.suffix.lower() not in IMAGE_EXTENSIONS:
        raise FileNotFoundError(f"unsupported image type: {path.name}")
    return path


def save_uploaded_image(
    asset_dir: Path,
    raw_bytes: bytes,
    *,
    upload_set: str = "",
    set_label: str = "",
    attempt_number: str = "",
    original_filename: str = "",
) -> dict[str, str]:
    if not raw_bytes:
        raise ValueError("uploaded image is empty")
    asset_dir.mkdir(parents=True, exist_ok=True)
    prefix = upload_filename_prefix(upload_set, set_label, attempt_number)
    base_name = uploaded_filename_slug(original_filename) or f"upload_{time.time_ns()}.png"
    if prefix:
        base_name = f"{prefix}__{base_name}"
    path = dedupe_asset_path(asset_dir, base_name)
    with Image.open(BytesIO(raw_bytes)) as image:
        image.load()
        output_format = image_save_format(path.suffix)
        if output_format == "JPEG" and image.mode not in ("RGB", "L"):
            output = image.convert("RGB")
        else:
            output = image if image.mode in ("RGB", "RGBA", "L", "LA", "P") else image.convert("RGBA")
        output.save(path, format=output_format)
    return {
        "path": str(path.resolve()),
        "name": path.name,
        "modified": format_timestamp(path),
    }


def write_crop(
    source: Path,
    asset_dir: Path,
    ticket: dict[str, Any],
    rect: dict[str, Any],
    *,
    feedback_number: int | None,
    set_label: str,
    normalized_source: str,
) -> tuple[Path, dict[str, Any]]:
    """Crop an attached image into the asset store; return the file and its metadata."""
    with Image.open(source) as image:
        image.load()
        source_width, source_height = image.size
        crop_rect = normalize_crop_rect(rect, source_width, source_height)
        cropped = image.crop(
            (
                crop_rect["x"],
                crop_rect["y"],
                crop_rect["x"] + crop_rect["w"],
                crop_rect["y"] + crop_rect["h"],
            )
        )
        if cropped.mode not in ("RGB", "RGBA", "L", "LA", "P"):
            cropped = cropped.convert("RGBA")
        feedback = feedback_number or next_feedback_number(ticket)
        if feedback <= 0 or feedback > 999:
            raise ValueError("feedback crop requires feedback number 1-999")
        label_slug = upload_set_slug(set_label)
        prefix = f"feedback-{feedback:03d}"
        if label_slug:
            prefix = f"{prefix}-{label_slug}"
        source_slug = crop_filename_slug(source.stem)
        crop_suffix = f"x{crop_rect['x']}-y{crop_rect['y']}-w{crop_rect['w']}-h{crop_rect['h']}"
        destination = dedupe_asset_path(asset_dir, f"{prefix}__crop-of-{source_slug}-{crop_suffix}.png")
        cropped.save(destination, format="PNG")

    metadata = {
        "kind": "crop",
        "source_path": normalized_source,
        "source_name": source.name,
        "rect": crop_rect,
        "source_size": {"w": source_width, "h": source_height},
        "feedback_number": feedback,
        "caption": (
            f"crop of {source.name} @ "
            f"{crop_rect['x']},{crop_rect['y']},{crop_rect['w']},{crop_rect['h']}"
        ),
    }
    return destination, metadata


def materialize_edit_field_attachments(
    edit_fields: dict[str, Any], ticket_id: str, current: dict[str, Any], frame_dir: Path, asset_dir: Path
) -> None:
    if "screenshots" in edit_fields:
        edit_fields["screenshots"] = materialize_attachments(
            edit_fields["screenshots"],
            ticket_id,
            current_paths=current.get("screenshots", []),
        frame_dir=frame_dir,
        asset_dir=asset_dir,
        )
        edit_fields["screenshot"] = edit_fields["screenshots"][0] if edit_fields["screenshots"] else ""
    elif "screenshot" in edit_fields:
        paths = materialize_attachments(
            edit_fields["screenshot"],
            ticket_id,
            current_paths=current.get("screenshots", []),
        frame_dir=frame_dir,
        asset_dir=asset_dir,
        )
        edit_fields["screenshots"] = paths
        edit_fields["screenshot"] = paths[0] if paths else ""


def validate_stored_screenshots(
    raw_screenshots: Any, raw_screenshot: Any, frame_dir: Path, asset_dir: Path
) -> list[dict[str, Any]]:
    raw_items: list[Any] = []
    if raw_screenshots not in (None, "", "null"):
        if not isinstance(raw_screenshots, list):
            raise ValueError("screenshots must be a list of paths")
        raw_items.extend(raw_screenshots)
    elif raw_screenshot not in (None, "", "null"):
        raw_items.append(raw_screenshot)

    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_items:
        if not isinstance(item, str):
            raise ValueError("screenshot entries must be path strings")
        normalized = normalize_image_path(item)
        if normalized in seen:
            continue
        path = Path(normalized)
        if not path_in_allowed_image_dirs(path, frame_dir, asset_dir):
            raise ValueError(f"screenshot path escapes allowed asset roots: {path}")
        if path.suffix.lower() not in IMAGE_EXTENSIONS:
            raise ValueError(f"unsupported image type: {path.name}")
        entries.append({"path": normalized, "available": path.is_file()})
        seen.add(normalized)
    return entries


def materialize_attachments(
    raw: Any,
    ticket_id: str,
    current_paths: list[str] | None = None,
    *,
    frame_dir: Path,
    asset_dir: Path,
) -> list[str]:
    if raw in (None, "", "null"):
        return []

    if isinstance(raw, str):
        raw_items = [raw]
    elif isinstance(raw, list):
        raw_items = raw
    else:
        raise ValueError("screenshots must be a path string, list of paths, or null")

    normalized_current = {
        normalize_image_path(path): path
        for path in (current_paths or [])
        if isinstance(path, str) and path
    }
    screenshot_paths: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        if not isinstance(item, str):
            raise ValueError("screenshot entries must be path strings")
        normalized = normalize_image_path(item)
        if normalized in seen:
            continue
        path = Path(normalized)
        if normalized in normalized_current and path_in_asset_dir(Path(normalized_current[normalized]), asset_dir):
            screenshot_paths.append(normalized_current[normalized])
        elif path_in_asset_dir(path, asset_dir):
            resolve_image(normalized, frame_dir, asset_dir)
            screenshot_paths.append(normalized)
        else:
            screenshot_paths.append(copy_attachment(normalized, ticket_id, frame_dir, asset_dir))
        seen.add(normalized)
    return screenshot_paths


def copy_attachment(raw: str, ticket_id: str, frame_dir: Path, asset_dir: Path) -> str:
    source = resolve_image(raw, frame_dir, asset_dir)
    destination = asset_dir / f"{ticket_id}-{time.time_ns()}.png"
    with Image.open(source) as image:
        image.load()
        output = image if image.mode in ("RGB", "RGBA", "L", "LA", "P") else image.convert("RGBA")
        output.save(destination, format="PNG")
    if source.parent == asset_dir and source.name.startswith("upload_"):
        source.unlink(missing_ok=True)
    return str(destination.resolve())


def screenshot_entries(paths: list[Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for item in paths:
        if isinstance(item, dict):
            path = str(item.get("path", ""))
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        else:
            path = str(item)
            metadata = {}
        entry: dict[str, Any] = {"path": path, "available": Path(path).is_file()}
        if metadata:
            entry["metadata"] = metadata
        entries.append(entry)
    return entries


def unique_paths(paths: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if not path or path in seen:
            continue
        unique.append(path)
        seen.add(path)
    return unique


def set_screenshot_fields(ticket: dict[str, Any], entries: list[dict[str, Any]]) -> None:
    ticket["screenshots"] = [entry["path"] for entry in entries]
    ticket["screenshots_info"] = entries
    if entries:
        ticket["screenshot"] = entries[0]["path"]
        ticket["screenshot_available"] = entries[0]["available"]
    else:
        ticket["screenshot"] = None
        ticket["screenshot_available"] = False
