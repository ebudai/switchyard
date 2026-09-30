#!/usr/bin/env python3
"""SYRD-500: image filename, geometry and path policy, with temporary assets only."""

from __future__ import annotations

import ast
import io
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board import app as app_module  # noqa: E402
from scripts.ticket_board import attachment_store  # noqa: E402
from scripts.ticket_board import image_asset_policy as policy  # noqa: E402
from scripts.ticket_board.app import TicketBoardApp  # noqa: E402

PUBLIC = ("IMAGE_EXTENSIONS", "format_timestamp", "upload_set_slug", "uploaded_filename_slug", "crop_filename_slug")
MOVED_METHODS = (
    "_normalize_crop_rect",
    "_next_feedback_number",
    "_dedupe_asset_path",
    "_image_save_format",
    "_upload_filename_prefix",
    "_path_in_allowed_image_dirs",
    "_path_in_asset_dir",
    "_normalize_image_path",
)


def png_bytes(mode: str = "RGBA", size: tuple[int, int] = (4, 3)) -> bytes:
    buffer = io.BytesIO()
    image = Image.new(mode, size, (21, 89, 155, 128) if mode == "RGBA" else 0)
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def temp_app(root: Path) -> TicketBoardApp:
    (root / "assets").mkdir()
    (root / "frames").mkdir()
    (root / "outside").mkdir()
    app = TicketBoardApp.__new__(TicketBoardApp)
    app.asset_dir = (root / "assets").resolve()
    app.frame_dir = (root / "frames").resolve()
    return app


def refusal(operation: Any) -> str:
    try:
        operation()
    except (ValueError, FileNotFoundError) as exc:
        return str(exc)
    raise AssertionError("expected a refusal")


def test_app_keeps_public_names_and_no_forwarders() -> None:
    for name in PUBLIC:
        assert getattr(app_module, name) is getattr(policy, name), name
    for name in MOVED_METHODS:
        assert not hasattr(TicketBoardApp, name), name
    tree = ast.parse(Path(policy.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    assert imported == {"__future__", "re", "datetime", "pathlib", "typing"}, imported


def test_filename_prefix_slug_and_format() -> None:
    assert policy.upload_filename_prefix("", "x", "") == ""
    assert policy.upload_filename_prefix("Ungrouped", "x", "") == ""
    assert policy.upload_filename_prefix("target", "ignored", "") == "target"
    assert policy.upload_filename_prefix("attempt", "First Pass", " 2 ") == "attempt-002-first-pass"
    assert policy.upload_filename_prefix("feedback", "", "12") == "feedback-012"
    assert policy.upload_filename_prefix("Other Set", "", "") == "other-set"
    assert policy.upload_filename_prefix("Other Set", "Label!", "") == "label"
    assert refusal(lambda: policy.upload_filename_prefix("attempt", "", "x")) == "attempt upload set requires an attempt number"
    assert refusal(lambda: policy.upload_filename_prefix("attempt", "", "0")) == "attempt upload set requires attempt number 1-999"
    assert refusal(lambda: policy.upload_filename_prefix("feedback", "", "1000")) == "feedback upload set requires feedback number 1-999"
    assert policy.uploaded_filename_slug("C:\\dir\\My Photo.JPG") == "my-photo.jpg"
    assert policy.uploaded_filename_slug("../../evil.svg") == "evil.png"
    assert policy.uploaded_filename_slug("..") == ""
    assert policy.crop_filename_slug("  ") == "render"
    assert [policy.image_save_format(s) for s in (".JPG", ".jpeg", ".WebP", ".png", ".gif")] == ["JPEG", "JPEG", "WEBP", "PNG", "PNG"]
    assert policy.next_feedback_number({"screenshots": ["/a/feedback-003-x.png", "feedback-010.png", "/a/xfeedback-50.png"]}) == 11
    assert policy.next_feedback_number({"screenshots": ["/a/feedback-999.png"]}) == 999
    assert policy.next_feedback_number({"screenshots": None}) == 1


def test_crop_rect_clamps_and_refuses() -> None:
    assert policy.normalize_crop_rect({"x": 2.2, "y": 1.6, "w": 20, "h": 5}, 4, 3) == {"x": 2, "y": 2, "w": 2, "h": 1}
    assert policy.normalize_crop_rect({"x": -5, "y": 99, "w": 1, "h": 1}, 4, 3) == {"x": 0, "y": 2, "w": 1, "h": 1}
    assert policy.normalize_crop_rect({"x": 99, "y": -1, "w": 9, "h": 9}, 4, 3) == {"x": 3, "y": 0, "w": 1, "h": 3}
    assert refusal(lambda: policy.normalize_crop_rect({"x": 0, "y": 0, "w": 0, "h": 1}, 4, 3)) == "crop width and height must be positive"
    assert refusal(lambda: policy.normalize_crop_rect({"x": "a", "y": 0, "w": 1, "h": 1}, 4, 3)) == "crop rect requires numeric x"
    assert refusal(lambda: policy.normalize_crop_rect({"x": 0, "y": 0, "w": 1}, 4, 3)) == "crop rect requires numeric h"


def test_dedupe_and_path_policy_create_nothing() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd500-policy.") as tmp:
        root = Path(tmp).resolve()
        missing = root / "not-created"
        assert policy.dedupe_asset_path(missing, "a.png") == missing / "a.png"
        assert not missing.exists()
        assets = root / "assets"
        frames = root / "frames"
        assets.mkdir()
        (assets / "a.png").write_bytes(b"x")
        (assets / "a-2.png").write_bytes(b"x")
        assert policy.dedupe_asset_path(assets, "a.png") == assets / "a-3.png"
        assert sorted(p.name for p in root.rglob("*")) == ["a-2.png", "a.png", "assets"]
        assert policy.path_in_allowed_image_dirs(assets / "x.png", frames, assets)
        assert policy.path_in_allowed_image_dirs(frames / "sub" / "x.png", frames, assets)
        assert not policy.path_in_allowed_image_dirs(assets, frames, assets)
        assert not policy.path_in_allowed_image_dirs(root / "assets-other" / "x.png", frames, assets)
        assert policy.path_in_asset_dir(assets, assets)
        assert not policy.path_in_asset_dir(frames / "x.png", assets)
        dotted = f"{assets}/../outside/x.png"
        assert policy.normalize_image_path(dotted) == str(root / "outside" / "x.png")
        assert not policy.path_in_allowed_image_dirs(Path(policy.normalize_image_path(dotted)), frames, assets)


def test_app_refuses_symlink_and_dotdot_escapes() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd500-escape.") as tmp:
        root = Path(tmp).resolve()
        app = temp_app(root)
        escaped = root / "outside" / "escape.png"
        escaped.write_bytes(png_bytes())
        (app.asset_dir / "link.png").symlink_to(escaped)
        expected = f"screenshot path escapes allowed asset roots: {escaped}"
        for raw in (str(app.asset_dir / "link.png"), f"{app.asset_dir}/../outside/escape.png"):
            assert refusal(lambda: app.resolve_image(raw)) == expected
            assert refusal(lambda: attachment_store.validate_stored_screenshots([raw], None, app.frame_dir, app.asset_dir)) == expected
            assert refusal(lambda: attachment_store.materialize_attachments(
                [raw], "SYRD-1", frame_dir=app.frame_dir, asset_dir=app.asset_dir)) == expected
        inside = app.asset_dir / "in.png"
        inside.write_bytes(png_bytes())
        assert app.resolve_image(str(inside)) == inside
        assert refusal(lambda: app.resolve_image(str(app.asset_dir / "none.png"))).startswith("screenshot not found")


def test_materialize_keeps_assets_and_copies_frames_once() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd500-materialize.") as tmp:
        app = temp_app(Path(tmp).resolve())
        frame = app.frame_dir / "frame.png"
        frame.write_bytes(png_bytes())
        kept = app.asset_dir / "kept.png"
        kept.write_bytes(png_bytes())
        paths = attachment_store.materialize_attachments(
            [str(frame), str(frame), str(kept)], "SYRD-1", frame_dir=app.frame_dir, asset_dir=app.asset_dir)
        assert len(paths) == 2 and paths[1] == str(kept), paths
        copied = Path(paths[0])
        assert copied.parent == app.asset_dir and copied.name.startswith("SYRD-1-") and copied.suffix == ".png"
        assert sorted(p.name for p in app.asset_dir.iterdir()) == sorted([copied.name, "kept.png"])
        assert frame.is_file()


def test_upload_converts_names_and_dedupes() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd500-upload.") as tmp:
        app = temp_app(Path(tmp).resolve())
        raw = png_bytes()
        kwargs = {"upload_set": "attempt", "set_label": "First Pass", "attempt_number": "2", "original_filename": "My Photo.JPG"}
        first = app.save_uploaded_image(raw, **kwargs)
        second = app.save_uploaded_image(raw, **kwargs)
        assert [first["name"], second["name"]] == ["attempt-002-first-pass__my-photo.jpg", "attempt-002-first-pass__my-photo-2.jpg"]
        with Image.open(first["path"]) as saved:
            assert (saved.format, saved.mode, saved.size) == ("JPEG", "RGB", (4, 3))
        webp = app.save_uploaded_image(raw, upload_set="target", original_filename="shot.webp")
        with Image.open(webp["path"]) as saved:
            assert (webp["name"], saved.format, saved.mode) == ("target__shot.webp", "WEBP", "RGBA")
        before = sorted(p.name for p in app.asset_dir.iterdir())
        assert refusal(lambda: app.save_uploaded_image(raw, upload_set="attempt", attempt_number="0")) == "attempt upload set requires attempt number 1-999"
        assert sorted(p.name for p in app.asset_dir.iterdir()) == before


class Conn:
    def __init__(self, events: list[Any]) -> None:
        self.events = events

    def __enter__(self) -> Conn:
        self.events.append(("begin",))
        return self

    def __exit__(self, *_args: Any) -> bool:
        self.events.append(("end",))
        return False


def test_crop_proves_source_then_writes_then_records_in_order() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd500-crop.") as tmp:
        app = temp_app(Path(tmp).resolve())
        source = app.asset_dir / "Source Shot.png"
        source.write_bytes(png_bytes())
        events: list[Any] = []
        ticket = {"id": "SYRD-1", "screenshots": [str(source), str(app.asset_dir / "feedback-003-old.png")]}
        app.get_ticket = lambda ticket_id: events.append(("get", ticket_id)) or ticket
        app._pg_connect = lambda: Conn(events)
        app._pg_set_caller_role = lambda _conn, role: events.append(("caller", role))

        def call(_conn: Any, sql: str, params: tuple[Any, ...]) -> None:
            events.append(("append", sql, params[0], Path(params[1]).name, Path(params[1]).is_file(), json.loads(params[2])))

        app._pg_call = call
        app._pg_get_ticket = lambda ticket_id, _conn: events.append(("readback", ticket_id)) or {"id": ticket_id}

        stray = app.asset_dir / "stray.png"
        stray.write_bytes(png_bytes())
        assert refusal(lambda: app.crop_attachment("syrd-1", source_path=str(stray), rect={"x": 0, "y": 0, "w": 1, "h": 1})) == (
            "crop source must already be attached to the ticket"
        )
        assert events == [("get", "SYRD-1")]
        assert not list(app.asset_dir.glob("feedback-*__crop-*"))
        events.clear()

        result = app.crop_attachment("syrd-1", source_path=str(source), rect={"x": 2.2, "y": 1.3, "w": 20, "h": 5}, set_label="Check", caller_role="main")
        name = "feedback-004-check__crop-of-source-shot-x2-y1-w2-h2.png"
        assert result == {"id": "SYRD-1"}
        assert [event[0] for event in events] == ["get", "begin", "caller", "append", "readback", "end"]
        assert events[2] == ("caller", "main")
        _, sql, ticket_id, recorded, existed, metadata = events[3]
        assert (sql, ticket_id, recorded, existed) == ("SELECT ticket_board.append_ticket_attachment(%s, %s, %s::jsonb);", "SYRD-1", name, True)
        assert metadata == {
            "kind": "crop",
            "source_path": str(source),
            "source_name": "Source Shot.png",
            "rect": {"x": 2, "y": 1, "w": 2, "h": 2},
            "source_size": {"w": 4, "h": 3},
            "feedback_number": 4,
            "caption": "crop of Source Shot.png @ 2,1,2,2",
        }
        with Image.open(app.asset_dir / name) as saved:
            assert (saved.format, saved.size) == ("PNG", (2, 2))

        events.clear()
        app.crop_attachment("SYRD-1", source_path=str(source), rect={"x": 2, "y": 1, "w": 2, "h": 2}, feedback_number=4, set_label="Check")
        assert events[2] == ("caller", "director")
        assert events[3][3] == "feedback-004-check__crop-of-source-shot-x2-y1-w2-h2-2.png"
        assert refusal(lambda: app.crop_attachment("SYRD-1", source_path=str(source), rect={"x": 0, "y": 0, "w": 1, "h": 1}, feedback_number=1000)) == (
            "feedback crop requires feedback number 1-999"
        )


def main() -> int:
    count = 0
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            count += 1
    print(f"ticket_board_image_asset_policy_test: {count} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
