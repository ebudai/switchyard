#!/usr/bin/env python3
"""SYRD-506: board build-ID resolution, with a faked Git and temporary layouts only."""

from __future__ import annotations

import ast
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board import build_identity as owner  # noqa: E402
from scripts.ticket_board import server  # noqa: E402

PUBLIC = ("REPO_ROOT", "RELEASE_SHA_RE", "board_build_id", "build_id_from_file", "build_id_from_release_path")
SHA = "0123456789abcdef0123456789abcdef01234567"


class Git:
    """Stands in for subprocess.run on the shared module object; never runs a process."""

    def __init__(self, returncode: int = 128, stdout: str = "") -> None:
        self.returncode, self.stdout, self.calls = returncode, stdout, []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, "")


def module(root: Path, *parts: str) -> Path:
    path = root.joinpath(*parts, "scripts", "ticket_board", "server.py")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


def resolve(module_path: Path, git: Git, environ: dict[str, str] | None = None, repo: Path | None = None) -> str:
    with patch.object(server.subprocess, "run", git):
        return owner.board_build_id(environ=environ or {}, repo_root=repo or module_path.parent, module_path=module_path)


def test_server_aliases_and_signature_defaults() -> None:
    for name in PUBLIC:
        assert getattr(server, name) is getattr(owner, name), name
    assert server.subprocess is subprocess
    params = inspect.signature(server.board_build_id).parameters
    assert [p.kind for p in params.values()] == [inspect.Parameter.KEYWORD_ONLY] * 3
    assert params["environ"].default is os.environ
    assert params["repo_root"].default is owner.REPO_ROOT == Path(server.__file__).resolve().parents[2] == ROOT
    assert params["module_path"].default == Path(server.__file__), params["module_path"].default
    assert params["module_path"].default.name == "server.py"
    tree = ast.parse(Path(owner.__file__).read_text(encoding="utf-8"))
    imported = {n.module for n in tree.body if isinstance(n, ast.ImportFrom)} | {a.name for n in tree.body if isinstance(n, ast.Import) for a in n.names}
    assert imported == {"__future__", "os", "re", "subprocess", "pathlib", "typing"}, imported


def test_fallback_order() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd506-order.") as tmp:
        root = Path(tmp).resolve()
        release = module(root, "live", "releases", SHA)
        (root / "live" / "BUILD").write_text("file-id\n")
        git = Git(0, "  cafe0123  \n")
        assert resolve(release, git, {"TICKET_BOARD_BUILD_ID": " neutral ", "PGU_TICKET_BOARD_BUILD_ID": "legacy"}) == "neutral"
        assert resolve(release, git, {"TICKET_BOARD_BUILD_ID": "  ", "PGU_TICKET_BOARD_BUILD_ID": " legacy "}) == "legacy"
        assert git.calls == [], "an explicit build id must not run git"
        assert resolve(release, git, {"TICKET_BOARD_BUILD_ID": "  "}, repo=root) == "cafe0123"
        assert git.calls == [(["git", "-C", str(root), "rev-parse", "HEAD"], {"check": False, "capture_output": True, "text": True})]
        assert resolve(release, Git(0, " \n")) == SHA
        assert resolve(release, Git(128, "stale\n")) == SHA
        filed = module(root, "current")
        (root / "current" / "BUILD").write_text("file-id\n")
        assert resolve(filed, Git()) == "file-id"
        assert resolve(module(root, "bare"), Git()) == "unknown"


def test_release_segment_rules() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd506-release.") as tmp:
        root = Path(tmp).resolve()
        cases = {
            ("a", "releases", "ABCDEF1"): "abcdef1",
            ("b", "releases", "abcdef"): "",
            ("c", "releases", "zzzzzzzz"): "",
            ("d", "releases", SHA + "0"): "",
            ("e", "releases", "notasha", "releases", "1234567"): "1234567",
        }
        for parts, expected in cases.items():
            assert owner.build_id_from_release_path(module(root, *parts)) == expected, parts
        last = root / "f" / "releases"
        last.mkdir(parents=True)
        assert owner.build_id_from_release_path(last) == ""
        assert owner.build_id_from_release_path(root / "h" / "releases" / "abcdef12") == "abcdef12"
        # An installed board runs through a `current` symlink; only the resolved path names the release.
        target = module(root, "g", "releases", "fedcba9876")
        (root / "current").symlink_to(root / "g" / "releases" / "fedcba9876")
        via = root / "current" / "scripts" / "ticket_board" / "server.py"
        assert owner.build_id_from_release_path(via) == "fedcba9876" and target.exists()


def test_marker_files() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd506-marker.") as tmp:
        root = Path(tmp).resolve()
        order = module(root, "order")
        for name, value in (("VERSION", "v-version"), (".build-id", "v-dot"), ("BUILD_ID", "v-build-id")):
            (root / "order" / name).write_text(value + "\n")
        assert owner.build_id_from_file(order) == "v-build-id"
        (root / "order" / "BUILD").write_text("v-build\n")
        assert owner.build_id_from_file(order) == "v-build"
        near = module(root, "near")
        (root / "near" / "scripts" / ".build-id").write_text("near-dot\n")
        (root / "near" / "BUILD").write_text("far-build\n")
        assert owner.build_id_from_file(near) == "near-dot"
        empty = module(root, "empty")
        (root / "empty" / "BUILD").write_text("")
        (root / "empty" / "BUILD_ID").write_text("   \n\n")
        (root / "empty" / "VERSION").write_text("\n\n  v3  \nv4\n")
        assert owner.build_id_from_file(empty) == "v3"
        odd = module(root, "odd")
        (root / "odd" / "BUILD").mkdir()
        (root / "odd" / "VERSION").write_bytes(b"\xff\xfeweird\nnext\n")
        assert owner.build_id_from_file(odd) == "��weird"
        assert owner.build_id_from_file(module(root, "none")) == ""


CHILD = """
import inspect, json, subprocess, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
subprocess.run = lambda argv, **kw: subprocess.CompletedProcess(argv, 128, "", "not a repository")
import ticket_board.server as s
default = inspect.signature(s.board_build_id).parameters["module_path"].default
print(json.dumps([s.board_build_id(environ={}), str(default), default == Path(s.__file__), s.LOGGER.name]))
"""


def installed(root: Path, *parts: str) -> list[Any]:
    scripts = root.joinpath(*parts, "scripts")
    shutil.copytree(ROOT / "scripts" / "ticket_board", scripts / "ticket_board", ignore=shutil.ignore_patterns("__pycache__"))
    child = subprocess.run(
        [sys.executable, "-c", CHILD, str(scripts)], capture_output=True, text=True, check=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    return json.loads(child.stdout)


def test_installed_release_layouts_use_the_real_defaults() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd506-installed.") as tmp:
        root = Path(tmp).resolve()
        build_id, default, same, logger = installed(root, "board", "releases", SHA)
        assert build_id == SHA and same and logger == "ticket_board.server"
        assert default == str(root / "board" / "releases" / SHA / "scripts" / "ticket_board" / "server.py")
        (root / "current").mkdir()
        (root / "current" / "BUILD").write_text("marker-build\n")
        build_id, default, same, _ = installed(root, "current")
        assert build_id == "marker-build" and same and default.endswith("/current/scripts/ticket_board/server.py")


def main() -> int:
    count = 0
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            count += 1
    print(f"ticket_board_build_identity_test: {count} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
