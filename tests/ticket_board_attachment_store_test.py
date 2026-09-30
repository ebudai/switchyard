#!/usr/bin/env python3
"""SYRD-518: the attachment file store and the app's calls into it.

`ticket_board/attachment_store.py` owns the board's attachment files: frames,
uploads, crops and the copies a ticket takes. It takes the frame and asset
directories on every call and knows nothing of tickets' authority or the
database. `TicketBoardApp` keeps its four public attachment methods and the
create/update/crop transactions.

This checks what the move could break and the older SYRD-500 suite does not
already pin: the owner's boundary; that the app reads its directories when it
is called, so a rebound directory is the one used on every path, including the
create and update transactions; and that a refused attachment stops before any
database write. The file behaviour itself is covered by
ticket_board_image_asset_policy_test.
"""

from __future__ import annotations

import ast
import inspect
import io
import json
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from scripts.ticket_board import app as app_module  # noqa: E402
from scripts.ticket_board import attachment_store as store  # noqa: E402
from scripts.ticket_board import image_asset_policy as policy  # noqa: E402
from scripts.ticket_board.app import TicketBoardApp  # noqa: E402

OWNER_FUNCTIONS = (
    "list_frames",
    "resolve_image",
    "save_uploaded_image",
    "write_crop",
    "materialize_edit_field_attachments",
    "validate_stored_screenshots",
    "materialize_attachments",
    "copy_attachment",
    "screenshot_entries",
    "unique_paths",
    "set_screenshot_fields",
)
LEFT_THE_APP = (
    "_materialize_edit_field_attachments",
    "_validate_stored_screenshots",
    "_materialize_attachments",
    "_copy_attachment",
    "_build_screenshot_entries",
    "_unique_paths",
    "_set_screenshot_fields",
)
STATES = ("draft", "backlog", "analysis", "in_progress", "inspection", "audit", "done", "cancelled")


def png(size: tuple[int, int] = (5, 4)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", size, (9, 8, 7, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


def refusal(operation: Any) -> str:
    try:
        operation()
    except (ValueError, FileNotFoundError) as exc:
        return str(exc)
    raise AssertionError("expected a refusal")


class Result:
    def __init__(self, one: Any = None) -> None:
        self.one = one

    def fetchone(self) -> Any:
        return self.one

    def fetchall(self) -> list[Any]:
        return []


class Conn:
    """Records every statement; answers the two the create path reads back."""

    def __init__(self, log: list[Any]) -> None:
        self.log = log

    def __enter__(self) -> "Conn":
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.log.append(("close", exc[0].__name__ if exc[0] else ""))
        return False

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.log.append(("begin",))
        yield
        self.log.append(("commit",))

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Result:
        text = " ".join(sql.split())
        self.log.append(("sql", text[:60], params))
        if "ticket_board.create_ticket(" in text:
            return Result({"id": "ABC-7"})
        return Result()


def rebound_app(tmp: Path, log: list[Any]) -> TicketBoardApp:
    """An app constructed on one pair of directories, then pointed at another."""
    for name in ("frames-a", "assets-a", "frames-b", "assets-b", "outside"):
        (tmp / name).mkdir()
    app = TicketBoardApp(tmp / "frames-a", tmp / "assets-a", commit_git_dir=tmp, database_url="unused", project="abc")
    app.frame_dir = (tmp / "frames-b").resolve()
    app.asset_dir = (tmp / "assets-b").resolve()
    app._workflow_states_cache = STATES
    app._pg_connect = lambda: Conn(log)
    app._validate_assignee = lambda value: value
    app._pg_get_ticket = lambda ticket_id, _conn: {"id": ticket_id, "screenshots": [], "state": "analysis",
                                                   "assignee": "main", "blocked_by": [], "blocked_reason": ""}
    return app


def test_the_owner_is_a_file_store_and_the_app_keeps_its_surface() -> None:
    assert [name for name, value in vars(store).items()
            if inspect.isfunction(value) and value.__module__ == store.__name__] == list(OWNER_FUNCTIONS)
    tree = ast.parse(Path(store.__file__).read_text(encoding="utf-8"))
    imported = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)} | {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    assert imported == {"__future__", "time", "io", "pathlib", "typing", "PIL", "image_asset_policy"}, imported
    for name in LEFT_THE_APP:
        assert not hasattr(TicketBoardApp, name), name
    for name in ("list_screenshots", "resolve_image", "save_uploaded_image", "crop_attachment"):
        assert inspect.isfunction(getattr(TicketBoardApp, name)), name
    assert list(inspect.signature(TicketBoardApp.crop_attachment).parameters) == [
        "self", "ticket_id", "source_path", "rect", "feedback_number", "set_label", "caller_role"]
    for name in ("IMAGE_EXTENSIONS", "format_timestamp", "upload_set_slug", "uploaded_filename_slug", "crop_filename_slug"):
        assert getattr(app_module, name) is getattr(policy, name), name
    assert not hasattr(app_module, "Image"), "the app no longer handles image bytes itself"


def test_every_public_path_reads_the_directories_when_called() -> None:
    with tempfile.TemporaryDirectory(prefix="s518.") as tmpdir:
        tmp = Path(tmpdir).resolve()
        log: list[Any] = []
        app = rebound_app(tmp, log)
        (app.frame_dir / "shot.png").write_bytes(png())
        (tmp / "frames-a" / "stale.png").write_bytes(png())
        assert [item["name"] for item in app.list_screenshots()] == ["shot.png"]
        assert app.resolve_image(str(app.frame_dir / "shot.png")) == app.frame_dir / "shot.png"
        assert refusal(lambda: app.resolve_image(str(tmp / "frames-a" / "stale.png"))).startswith(
            "screenshot path escapes allowed asset roots")
        uploaded = app.save_uploaded_image(png(), original_filename="up.png", upload_set="target")
        assert Path(uploaded["path"]).parent == app.asset_dir and uploaded["name"] == "target__up.png", uploaded

        source = app.asset_dir / "target__up.png"
        app.get_ticket = lambda ticket_id: {"id": ticket_id, "screenshots": [str(source)]}
        app.crop_attachment("abc-1", source_path=str(source), rect={"x": 0, "y": 0, "w": 2, "h": 2}, feedback_number=1)
        appended = next(entry for entry in log if entry[0] == "sql" and "append_ticket_attachment" in entry[1])
        assert Path(appended[2][1]).parent == app.asset_dir, appended
        assert not list((tmp / "assets-a").iterdir()), "the directory the app was built with was written to"


def test_create_and_update_materialize_into_the_current_directories() -> None:
    with tempfile.TemporaryDirectory(prefix="s518.") as tmpdir:
        tmp = Path(tmpdir).resolve()
        log: list[Any] = []
        app = rebound_app(tmp, log)
        frame = app.frame_dir / "frame.png"
        frame.write_bytes(png())
        kept = app.asset_dir / "kept.png"
        kept.write_bytes(png((3, 3)))

        app.create_ticket_record(
            title="T", body="", screenshot=None, screenshots=[str(frame), str(kept), str(frame)], state="analysis",
            assignee="main", blocked_by=None, parent_id="", implementation="", audit_prompt="", audit_signoff=False,
            needs_audit=True, needs_inspection=False, inspector_signoff=False, needs_user_signoff=False,
            user_signoff=False, regression=False, comments=[], blocked_reason="", commit_hash="", commit_exempt=False,
            caller_role="main",
        )
        writes = [entry for entry in log if entry[0] == "sql" and "edit_fields" in entry[1]]
        assert len(writes) == 1, log
        fields = json.loads(writes[0][2][1])
        copied = Path(fields["screenshots"][0])
        assert copied.parent == app.asset_dir and copied.name.startswith("ABC-7-"), fields
        assert fields["screenshots"][1:] == [str(kept)] and fields["screenshot"] == str(copied), fields
        assert copied.is_file() and frame.is_file()

        log.clear()
        second = app.frame_dir / "second.png"
        second.write_bytes(png())
        app.update_ticket("ABC-7", {"screenshot": str(second)}, caller_role="main")
        writes = [entry for entry in log if entry[0] == "sql" and "edit_fields" in entry[1]]
        fields = json.loads(writes[0][2][1])
        assert Path(fields["screenshot"]).parent == app.asset_dir and fields["screenshots"] == [fields["screenshot"]], fields
        assert log[-1] == ("close", ""), log

        log.clear()
        outside = tmp / "outside" / "x.png"
        outside.write_bytes(png())
        before = sorted(path.name for path in app.asset_dir.iterdir())
        assert refusal(lambda: app.update_ticket("ABC-7", {"screenshots": [str(outside)]}, caller_role="main")) == (
            f"screenshot path escapes allowed asset roots: {outside}")
        assert not [entry for entry in log if entry[0] == "sql" and "edit_fields" in entry[1]], log
        assert log[-1] == ("close", "FileNotFoundError"), log
        assert sorted(path.name for path in app.asset_dir.iterdir()) == before


def test_moved_behaviour_the_older_suites_leave_unpinned() -> None:
    with tempfile.TemporaryDirectory(prefix="s518.") as tmpdir:
        tmp = Path(tmpdir).resolve()
        log: list[Any] = []
        app = rebound_app(tmp, log)
        dirs = {"frame_dir": app.frame_dir, "asset_dir": app.asset_dir}
        # An attachment the ticket already has is kept as recorded, even when its
        # file is gone, rather than being re-resolved or copied.
        gone = str(app.asset_dir / "." / "gone.png")
        assert store.materialize_attachments([str(app.asset_dir / "gone.png")], "ABC-1", [gone], **dirs) == [gone]
        # A copy is always a PNG, whatever the source was.
        jpeg = app.frame_dir / "photo.jpg"
        Image.new("RGB", (4, 4), (1, 2, 3)).save(jpeg, format="JPEG")
        [copied] = store.materialize_attachments([str(jpeg)], "ABC-1", None, **dirs)
        with Image.open(copied) as saved:
            assert (saved.format, Path(copied).suffix) == ("PNG", ".png"), (saved.format, copied)
        # Stored screenshots are refused by type as well as by place.
        (app.asset_dir / "notes.txt").write_text("x")
        assert refusal(lambda: store.validate_stored_screenshots([str(app.asset_dir / "notes.txt")], None, **dirs)) == (
            "unsupported image type: notes.txt")
        # What a ticket reports about its screenshots: availability of the first,
        # and each entry's metadata.
        here = app.asset_dir / "here.png"
        here.write_bytes(png())
        ticket: dict[str, Any] = {}
        store.set_screenshot_fields(ticket, store.screenshot_entries([
            str(app.asset_dir / "missing.png"), {"path": str(here), "metadata": {"kind": "crop"}}]))
        assert ticket == {
            "screenshots": [str(app.asset_dir / "missing.png"), str(here)],
            "screenshots_info": [{"path": str(app.asset_dir / "missing.png"), "available": False},
                                 {"path": str(here), "available": True, "metadata": {"kind": "crop"}}],
            "screenshot": str(app.asset_dir / "missing.png"),
            "screenshot_available": False,
        }, ticket


def main() -> int:
    test_the_owner_is_a_file_store_and_the_app_keeps_its_surface()
    test_every_public_path_reads_the_directories_when_called()
    test_create_and_update_materialize_into_the_current_directories()
    test_moved_behaviour_the_older_suites_leave_unpinned()
    print("ticket_board_attachment_store_test: 4 cases ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
