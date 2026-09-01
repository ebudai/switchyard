#!/usr/bin/env python3
"""Regression test: deployed non-git ticket-board releases resolve a build id."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend_script_core import SCRIPT_CORE
from scripts.ticket_board.server import board_build_id, build_id_from_release_path


def main() -> int:
    release_sha = "0123456789abcdef0123456789abcdef01234567"
    file_sha = "89abcdef0123456789abcdef0123456789abcdef"

    with tempfile.TemporaryDirectory(prefix="ticket-board-build-id.") as tmpdir:
        root = Path(tmpdir)
        non_git_repo = root / "not-a-git-export"
        non_git_repo.mkdir()

        release_server = root / "pgu-ticketboard-live" / "releases" / release_sha / "scripts" / "ticket_board" / "server.py"
        release_server.parent.mkdir(parents=True)
        release_server.touch()

        assert build_id_from_release_path(release_server) == release_sha
        assert board_build_id(environ={}, repo_root=non_git_repo, module_path=release_server) == release_sha
        assert (
            board_build_id(
                environ={"PGU_TICKET_BOARD_BUILD_ID": "manual-build-id"},
                repo_root=non_git_repo,
                module_path=release_server,
            )
            == "manual-build-id"
        )
        assert (
            board_build_id(
                environ={"TICKET_BOARD_BUILD_ID": "neutral-build-id"},
                repo_root=non_git_repo,
                module_path=release_server,
            )
            == "neutral-build-id"
        )
        assert (
            board_build_id(
                environ={
                    "TICKET_BOARD_BUILD_ID": "preferred-build-id",
                    "PGU_TICKET_BOARD_BUILD_ID": "legacy-build-id",
                },
                repo_root=non_git_repo,
                module_path=release_server,
            )
            == "preferred-build-id"
        )

        build_file_server = root / "release-with-build-file" / "scripts" / "ticket_board" / "server.py"
        build_file_server.parent.mkdir(parents=True)
        build_file_server.touch()
        (root / "release-with-build-file" / "BUILD").write_text(f"{file_sha}\n", encoding="utf-8")

        assert board_build_id(environ={}, repo_root=non_git_repo, module_path=build_file_server) == file_sha

    assert "String(window.TICKET_BOARD_BUILD_ID || window.PGU_TICKET_BOARD_BUILD_ID || '')" in SCRIPT_CORE
    assert (
        "window.TICKET_BOARD_REFRESH_IDLE_MS ?? window.PGU_TICKET_BOARD_REFRESH_IDLE_MS ?? 2500"
        in SCRIPT_CORE
    )

    print("ticket_board_build_id_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
