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


def _disposable_clone(root: Path) -> Path:
    """A real clone of this repository, carrying the working tree under test.

    `git clone` copies the committed tree, so a clone alone would exercise HEAD rather
    than the change being tested -- which is how the first version of this test silently
    ran the old installer. The tracked files are overlaid from the working tree
    afterwards so the clone is what this checkout actually says.
    """
    import shutil

    clone = root / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(ROOT), str(clone)], check=True,
        capture_output=True, text=True,
    )
    tracked = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True, check=True
    ).stdout.split()
    for relative in tracked:
        source = ROOT / relative
        if not source.is_file():
            continue
        destination = clone / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return clone


def _stub(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_a_fresh_clone_and_top_level_install_ends_up_with_the_policy() -> None:
    """The seam this ticket exists to close, run rather than read.

    A disposable clone is installed into with the two privileged sub-installers stubbed
    out, then the checkout itself is inspected. Reading the installer's source cannot
    show this: the policy step is deliberately warning-only, so a broken or no-op command
    would leave the text unchanged and the hook absent.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        clone = _disposable_clone(root)
        assert not (clone / ".git" / "hooks" / "pre-commit").exists()

        stubs = root / "stubs"
        stubs.mkdir()
        recorder = root / "sudo-invocations"
        fake_sudo = _stub(
            stubs / "sudo",
            "#!/bin/sh\n"
            f'printf "%s\\n" "$*" >> {recorder}\n'
            '# Drop the "-u <user>" prefix and run the command, standing in for the\n'
            '# de-escalation this test cannot really perform.\n'
            'shift 2\n'
            'exec "$@"\n',
        )
        user = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()

        proc = subprocess.run(
            [str(clone / "install")],
            cwd=str(root), capture_output=True, text=True,
            env={
                **os.environ,
                "SWITCHYARD_INSTALL_PREREQS_SCRIPT": str(_stub(stubs / "prereqs")),
                "SWITCHYARD_INSTALL_SWITCHYARD_SCRIPT": str(_stub(stubs / "install-switchyard")),
                "SWITCHYARD_INSTALL_TEST_ASSUME_ROOT": "1",
                "SWITCHYARD_INSTALL_ORIGINAL_USER": user,
                "SWITCHYARD_SUDO_BIN": str(fake_sudo),
            },
        )

        hook = clone / ".git" / "hooks" / "pre-commit"
        installed = hook.exists()
        hooks_path = subprocess.run(
            ["git", "-C", str(clone), "config", "--local", "--get", "core.hooksPath"],
            capture_output=True, text=True,
        ).stdout.strip()
        owners = {
            path.name: (path.stat().st_uid, path.stat().st_gid)
            for path in (clone / ".git" / "hooks").iterdir()
            if path.is_file() and not path.name.endswith(".sample")
        }
        recorded = recorder.read_text(encoding="utf-8") if recorder.exists() else ""
        body = hook.read_text(encoding="utf-8") if installed else ""

    assert proc.returncode == 0, proc.stderr
    assert installed, f"the installer left the clone without a hook\n{proc.stdout}\n{proc.stderr}"
    # Repository-local configuration, pointing inside the clone and nowhere else.
    assert hooks_path == str(clone / ".git" / "hooks"), hooks_path
    # Installed as the original invoking user, not left owned by the privileged caller.
    for name, (uid, gid) in owners.items():
        assert uid == os.getuid() and gid == os.getgid(), f"{name} owned by {uid}:{gid}"
    # The exact root-to-original-user invocation, not merely that something ran.
    assert f"-u {user}" in recorded, recorded
    assert "repository_hooks.py" in recorded and "--local-only" in recorded, recorded
    assert f"--repository {clone}" in recorded, recorded
    # And the policy that landed is the real one.
    assert f"FILE_SIZE_LIMIT={THRESHOLD}" in body, body


def test_a_dry_run_top_level_install_leaves_a_fresh_clone_untouched() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        clone = _disposable_clone(root)
        proc = subprocess.run(
            [str(clone / "install"), "--dry-run"],
            cwd=str(root), capture_output=True, text=True,
            env={**os.environ, "SWITCHYARD_SUDO_BIN": "sudo"},
        )
        installed = (clone / ".git" / "hooks" / "pre-commit").exists()
        hooks_path = subprocess.run(
            ["git", "-C", str(clone), "config", "--local", "--get", "core.hooksPath"],
            capture_output=True, text=True,
        ).stdout.strip()

    assert proc.returncode == 0, proc.stderr
    assert "repository_hooks.py" in proc.stdout
    assert not installed, "a dry run installed the hook"
    assert hooks_path == "", hooks_path


def test_the_installer_never_writes_global_git_configuration() -> None:
    """The one claim behaviour cannot show: absence across branches never taken.

    The end-to-end tests above prove what the installer does; this proves what it must
    never do, including on paths a single run does not reach.
    """
    installer = (ROOT / "install").read_text(encoding="utf-8")
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
