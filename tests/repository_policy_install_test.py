#!/usr/bin/env python3
"""Warning-only pre-commit policy: installation, repair, and boundary (SYRD-34)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import repository_hooks, team_launcher  # noqa: E402
from scripts.report_file_size_limit import DEFAULT_LINE_LIMIT  # noqa: E402

THRESHOLD = 1250


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )


def _new_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "Test")
    return repo


def _stage(repo: Path, name: str, lines: int) -> None:
    (repo / name).write_text("x = 1\n" * lines, encoding="utf-8")
    _git(repo, "add", name)


def _run_hook(repo: Path) -> subprocess.CompletedProcess[str]:
    hook = repo / ".git" / "hooks" / "pre-commit"
    return subprocess.run(
        [str(hook)], cwd=str(repo), capture_output=True, text=True
    )


def _install(repo: Path) -> None:
    repository_hooks.install_repository_policy(
        repo, source_root=ROOT, project="switchyard", local_warning=True, main_guard=False
    )


# --- the threshold this ticket exists to pin ---------------------------------


def test_the_threshold_is_the_deliberate_1250() -> None:
    assert DEFAULT_LINE_LIMIT == THRESHOLD


def test_exactly_the_threshold_is_accepted_and_one_more_warns() -> None:
    """The boundary, both sides, through the installed hook rather than the helper."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = _new_repo(Path(tmp))
        _install(repo)

        _stage(repo, "at_limit.py", THRESHOLD)
        accepted = _run_hook(repo)
        _git(repo, "reset", "-q")

        _stage(repo, "over_limit.py", THRESHOLD + 1)
        warned = _run_hook(repo)

    assert "at_limit.py" not in accepted.stderr, accepted.stderr
    assert f"over_limit.py is {THRESHOLD + 1} lines" in warned.stderr, warned.stderr
    assert f"soft limit {THRESHOLD}" in warned.stderr


def test_the_policy_only_ever_warns() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = _new_repo(Path(tmp))
        _install(repo)
        _stage(repo, "huge.py", THRESHOLD + 500)
        result = _run_hook(repo)

    assert result.returncode == 0, result.stderr
    assert "warning:" in result.stderr


def test_each_offending_path_is_reported_once_per_commit() -> None:
    """Once per count means one scan per invocation, not one report per occurrence."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = _new_repo(Path(tmp))
        _install(repo)
        _stage(repo, "one.py", THRESHOLD + 1)
        _stage(repo, "two.py", THRESHOLD + 2)
        _stage(repo, "small.py", 10)
        result = _run_hook(repo)

    assert result.stderr.count("one.py is") == 1, result.stderr
    assert result.stderr.count("two.py is") == 1, result.stderr
    assert "small.py" not in result.stderr


# --- a pre-existing hook keeps working ---------------------------------------


def test_a_pre_existing_hook_is_preserved_with_its_exit_status() -> None:
    """Someone else's hook is not silently replaced, and its refusal still refuses."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = _new_repo(Path(tmp))
        hooks = repo / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        existing = hooks / "pre-commit"
        existing.write_text("#!/bin/sh\necho mine >&2\nexit 3\n", encoding="utf-8")
        existing.chmod(0o755)

        _install(repo)
        _stage(repo, "over.py", THRESHOLD + 1)
        result = _run_hook(repo)
        preserved = (repo / ".git" / "hooks" / "pre-commit.switchyard-upstream").exists()

    assert preserved, "the pre-existing hook was not preserved"
    assert "mine" in result.stderr, result.stderr
    assert result.returncode == 3, "the preserved hook's exit status must survive"
    # The warning still runs alongside it.
    assert "over.py is" in result.stderr


# --- installation, repair, ownership -----------------------------------------


def test_a_fresh_checkout_gets_the_policy_and_only_local_configuration() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = _new_repo(Path(tmp))
        assert not (repo / ".git" / "hooks" / "pre-commit").exists()

        _install(repo)

        hook = repo / ".git" / "hooks" / "pre-commit"
        assert hook.exists() and os.access(hook, os.X_OK)
        local = _git(repo, "config", "--local", "--get", "core.hooksPath").stdout.strip()
        assert local == str(repo / ".git" / "hooks"), local
        # Repository-scoped only: nothing global is touched.
        globally = subprocess.run(
            ["git", "config", "--global", "--get", "core.hooksPath"],
            capture_output=True, text=True,
        )
        assert globally.returncode != 0 or not globally.stdout.strip(), globally.stdout


def test_the_installed_files_belong_to_the_invoking_user() -> None:
    """Hooks and config stay the user's, so they can change them afterwards."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = _new_repo(Path(tmp))
        _install(repo)
        hooks = repo / ".git" / "hooks"
        owned = {
            path.name: (path.stat().st_uid, path.stat().st_gid)
            for path in hooks.iterdir()
            if path.is_file()
        }

    assert owned, "nothing was installed"
    for name, (uid, gid) in owned.items():
        assert uid == os.getuid(), f"{name} is owned by uid {uid}"
        assert gid == os.getgid(), f"{name} is owned by gid {gid}"


def test_upgrade_repairs_a_missing_hook_and_is_idempotent() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo = _new_repo(root)
        config_path = root / "porter.json"
        config_path.write_text(
            json.dumps({"project": "porter", "repository": str(repo)}), encoding="utf-8"
        )
        said: list[str] = []

        first = team_launcher.repair_repository_policy_hooks(
            config_path, source_repo=ROOT, print_func=said.append
        )
        hook = repo / ".git" / "hooks" / "pre-commit"
        installed_body = hook.read_text(encoding="utf-8")

        # A stale hook -- here, simply removed -- is reinstalled by the same call.
        hook.unlink()
        second = team_launcher.repair_repository_policy_hooks(
            config_path, source_repo=ROOT, print_func=said.append
        )
        repaired_body = hook.read_text(encoding="utf-8")

        # And a third run over an already-correct checkout changes nothing.
        third = team_launcher.repair_repository_policy_hooks(
            config_path, source_repo=ROOT, print_func=said.append
        )
        settled_body = hook.read_text(encoding="utf-8")
        upstream_copies = sorted(
            p.name for p in (repo / ".git" / "hooks").iterdir()
            if p.name.endswith(".switchyard-upstream")
        )

    assert first and second and third
    assert repaired_body == installed_body, "repair produced a different hook"
    assert settled_body == installed_body, "a repeated repair changed the hook"
    assert upstream_copies == [], "repeated repairs must not accumulate preserved copies"


def test_repair_is_reported_and_never_fails_the_upgrade() -> None:
    """Warning-only policy: a broken repair warns rather than stopping an upgrade."""
    with tempfile.TemporaryDirectory() as tmp:
        config_path = Path(tmp) / "porter.json"
        config_path.write_text(
            json.dumps({"project": "porter", "repository": str(Path(tmp) / "absent")}),
            encoding="utf-8",
        )
        said: list[str] = []
        installed = team_launcher.repair_repository_policy_hooks(
            config_path, source_repo=ROOT, print_func=said.append
        )

    assert installed == ()
    assert any("could not repair" in line for line in said), said


def test_a_dry_run_repair_reports_without_installing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo = _new_repo(root)
        config_path = root / "porter.json"
        config_path.write_text(
            json.dumps({"project": "porter", "repository": str(repo)}), encoding="utf-8"
        )
        said: list[str] = []
        installed = team_launcher.repair_repository_policy_hooks(
            config_path, source_repo=ROOT, dry_run=True, print_func=said.append
        )
        hook_exists = (repo / ".git" / "hooks" / "pre-commit").exists()

    assert installed == ()
    assert not hook_exists, "a dry run must not install anything"
    assert any("would reinstall" in line for line in said), said


# --- the top-level installer seam --------------------------------------------


def test_the_top_level_installer_installs_the_policy_into_the_source_checkout() -> None:
    """The gap this ticket exists to close: a fresh clone plus ./install had no hook."""
    installer = (ROOT / "install").read_text(encoding="utf-8")
    assert "install_source_checkout_policy" in installer
    # Called on the real path and on the dry-run path, so neither silently skips it.
    assert installer.count("install_source_checkout_policy") >= 3
    assert "--local-only" in installer, "the source checkout takes the warning policy only"
    # The prohibition is on *global* configuration; the installer delegates the
    # repository-local hooksPath to repository_hooks.py. Matching the bare string would
    # only catch the comment that explains this.
    assert "config --global" not in installer
    assert "--global core.hooksPath" not in installer
    for line in installer.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert "core.hooksPath" not in stripped, line


def test_the_installer_dry_run_reports_the_policy_without_installing_it() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(
            [str(ROOT / "install"), "--dry-run"],
            cwd=tmp, capture_output=True, text=True,
            env={**os.environ, "SWITCHYARD_SUDO_BIN": "sudo"},
        )

    assert proc.returncode == 0, proc.stderr
    assert "repository_hooks.py" in proc.stdout, proc.stdout
    assert "--local-only" in proc.stdout


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print("repository_policy_install_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
