#!/usr/bin/env python3
"""Exercise repository hook Git access across a real ownership boundary.

When run as root this test creates a temporary bare repository, transfers it to
a non-root account, and performs the full guard installation twice. An
unprivileged process cannot create such a fixture; in that mode the test uses a
real foreign-owned bare repository for the Git trust-boundary regression. The
writable installation/idempotency path remains covered by repository_hooks_test.py.
"""

from __future__ import annotations

import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.repository_hooks import MANAGED_MARKER, _git, install_main_guard


def untrusted_git(repository: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "safe.directory=", "-C", str(repository), "rev-parse", "--git-common-dir"],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull},
    )


def assert_narrow_git_access(repository: Path) -> None:
    rejected = untrusted_git(repository)
    assert rejected.returncode != 0, f"fixture is not rejected as foreign-owned: {repository}"
    assert "dubious ownership" in rejected.stderr
    accepted = _git(repository, "rev-parse", "--git-common-dir")
    assert accepted.returncode == 0


def foreign_uid() -> tuple[int, int]:
    excluded = {0, os.geteuid()}
    sudo_uid = os.environ.get("SUDO_UID")
    if sudo_uid and sudo_uid.isdigit():
        excluded.add(int(sudo_uid))
    for entry in pwd.getpwall():
        if entry.pw_uid not in excluded:
            return entry.pw_uid, entry.pw_gid
    raise RuntimeError("no non-root account is available for the ownership fixture")


def chown_tree(path: Path, uid: int, gid: int) -> None:
    for current, directories, files in os.walk(path):
        os.chown(current, uid, gid)
        for name in directories:
            os.chown(Path(current) / name, uid, gid)
        for name in files:
            os.chown(Path(current) / name, uid, gid)


def root_installation_case() -> None:
    with tempfile.TemporaryDirectory(prefix="foreign-owner-hooks.") as raw_temp:
        repository = Path(raw_temp) / "operator.git"
        subprocess.run(["git", "init", "--bare", str(repository)], check=True, capture_output=True, text=True)
        upstream = repository / "hooks" / "update"
        upstream_text = """#!/usr/bin/env bash
set -euo pipefail
readonly STAGE3B_DEMO_HOOK=/tmp/foreign-owner-stage3b
[[ "${1:-}" != refs/heads/main || "${PGU_ALLOW_MAIN_PUSH:-}" == director ]] || exit 1
exit 0
"""
        upstream.write_text(upstream_text, encoding="utf-8")
        upstream.chmod(0o755)
        uid, gid = foreign_uid()
        chown_tree(repository, uid, gid)

        assert_narrow_git_access(repository)
        install_main_guard(repository, project="pgu")
        preserved = repository / "hooks" / "update.switchyard-upstream"
        assert preserved.read_text(encoding="utf-8") == upstream_text
        assert preserved.stat().st_uid == uid

        # Re-running over PGU's partial-rollout shape must retain the original
        # upstream and must never create a second-generation wrapper.
        install_main_guard(repository, project="pgu")
        assert preserved.read_text(encoding="utf-8") == upstream_text
        assert MANAGED_MARKER in upstream.read_text(encoding="utf-8")
        assert not (repository / "hooks" / "update.switchyard-upstream.switchyard-upstream").exists()
        assert upstream.stat().st_uid == uid


def unprivileged_access_case() -> None:
    explicit = os.environ.get("PGU_FOREIGN_GIT_REPOSITORY", "")
    candidates = [Path(explicit)] if explicit else sorted(Path("/data/git").glob("*.git"))
    repository = next(
        (candidate for candidate in candidates if candidate.exists() and candidate.stat().st_uid != os.geteuid()),
        None,
    )
    if repository is None:
        raise RuntimeError(
            "foreign-owned fixture unavailable; run this test as root for the full mutation case "
            "or set PGU_FOREIGN_GIT_REPOSITORY"
        )
    assert_narrow_git_access(repository)
    print(
        "repository_hooks_foreign_owner_test: unprivileged mode exercised real Git ownership rejection; "
        "full foreign-owned installation requires root"
    )


def main() -> int:
    if os.geteuid() == 0:
        root_installation_case()
    else:
        unprivileged_access_case()
    print("repository_hooks_foreign_owner_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
