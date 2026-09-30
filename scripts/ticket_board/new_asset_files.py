"""The files one board operation creates in the asset directory (SYRD-520).

Attaching a frame or cropping an image writes a file before the database
records it. If the operation then fails -- a refusal, a failed copy, the
commit itself -- the database rolls back, and the file must go too; but only
that file. So each file is created exclusively (never over an existing one)
and recorded by device and inode, and a failed operation removes exactly the
files it recorded that are still those files. Pre-existing and reused assets,
uploads, and anything another operation wrote are never candidates.

`attachment_store` writes through a `NewAssetFiles` it is handed;
`TicketBoardApp` decides when an operation is kept (its transaction
committed) or discarded.
"""

from __future__ import annotations

import contextlib
import logging
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

LOGGER = logging.getLogger(__name__)
NEW_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class NewAssetFiles:
    def __init__(self) -> None:
        self._created: list[tuple[Path, str, int, int]] = []
        self._consumed: list[Path] = []

    def create(self, asset_dir: Path, name: str) -> BinaryIO:
        """Open a new file `name` in `asset_dir` for writing; FileExistsError if the name is taken."""
        if "/" in name or name in ("", ".", ".."):
            raise ValueError(f"asset file name must be a plain name: {name!r}")
        asset_dir.mkdir(parents=True, exist_ok=True)
        dir_fd = os.open(asset_dir, DIRECTORY_FLAGS)
        try:
            fd = os.open(name, NEW_FILE_FLAGS, 0o666, dir_fd=dir_fd)
        finally:
            os.close(dir_fd)
        info = os.fstat(fd)
        self._created.append((asset_dir, name, info.st_dev, info.st_ino))
        return os.fdopen(fd, "wb")

    def consume_on_keep(self, path: Path) -> None:
        """Remove `path` (a consumed upload) if, and only if, the operation is kept."""
        self._consumed.append(path)

    def keep(self) -> None:
        """The operation succeeded: its files stay, and what it consumed goes."""
        self._created.clear()
        consumed, self._consumed = self._consumed, []
        for path in consumed:
            path.unlink(missing_ok=True)

    def discard(self) -> list[str]:
        """The operation failed: remove the files it created. Returns those it could not."""
        self._consumed.clear()
        created, self._created = self._created, []
        left: list[str] = []
        for asset_dir, name, dev, ino in reversed(created):
            try:
                dir_fd = os.open(asset_dir, DIRECTORY_FLAGS)
            except OSError as error:
                left.append(f"{asset_dir / name} ({error.strerror})")
                continue
            try:
                info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                if (info.st_dev, info.st_ino) != (dev, ino) or not stat.S_ISREG(info.st_mode):
                    left.append(f"{asset_dir / name} (no longer the file this operation created)")
                    continue
                os.unlink(name, dir_fd=dir_fd)
            except FileNotFoundError:
                continue
            except OSError as error:
                left.append(f"{asset_dir / name} ({error.strerror})")
            finally:
                os.close(dir_fd)
        return left


@contextlib.contextmanager
def discarded_on_failure() -> Iterator[NewAssetFiles]:
    """A `NewAssetFiles` kept if the block completes and discarded if it raises.

    The block's own error is what the caller gets. Files that could not be
    removed are named on it and logged -- never silently dropped, and never in
    place of the error.
    """
    new_files = NewAssetFiles()
    try:
        yield new_files
    except BaseException as error:
        left = new_files.discard()
        if left:
            note = "files created by this failed operation were left in place: " + "; ".join(left)
            error.add_note(note)
            LOGGER.warning("%s (after %s: %s)", note, type(error).__name__, error)
        raise
    new_files.keep()
