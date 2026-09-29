"""Which release of the board is running.

An explicit TICKET_BOARD_BUILD_ID (or legacy PGU_TICKET_BOARD_BUILD_ID) wins,
then the checkout's Git HEAD, then a releases/<sha>/ path segment, then a
BUILD, BUILD_ID, VERSION or .build-id file above the module, then "unknown".
The servers resolve it once when they start.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Mapping

REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def build_id_from_release_path(module_path: Path) -> str:
    parts = module_path.resolve().parts
    for index, part in enumerate(parts[:-1]):
        if part != "releases":
            continue
        candidate = parts[index + 1].strip()
        if RELEASE_SHA_RE.fullmatch(candidate):
            return candidate.lower()
    return ""


def build_id_from_file(module_path: Path) -> str:
    for parent in module_path.resolve().parents:
        for name in ("BUILD", "BUILD_ID", "VERSION", ".build-id"):
            candidate_path = parent / name
            if not candidate_path.is_file():
                continue
            try:
                lines = candidate_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
            except OSError:
                continue
            candidate = lines[0].strip() if lines else ""
            if candidate:
                return candidate
    return ""


def board_build_id(
    *,
    environ: Mapping[str, str] = os.environ,
    repo_root: Path = REPO_ROOT,
    # The server module's own path, the default this had when it lived there (SYRD-506).
    module_path: Path = Path(__file__).with_name("server.py"),
) -> str:
    explicit = (
        environ.get("TICKET_BOARD_BUILD_ID", "").strip()
        or environ.get("PGU_TICKET_BOARD_BUILD_ID", "").strip()
    )
    if explicit:
        return explicit
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    value = proc.stdout.strip()
    if proc.returncode == 0 and value:
        return value
    release_id = build_id_from_release_path(module_path)
    if release_id:
        return release_id
    file_id = build_id_from_file(module_path)
    return file_id or "unknown"
