#!/usr/bin/env python3
"""SYRD-116 review: asking about one credential must not run an upgrade.

Verification started life as a flag on `switchyard upgrade`, so the only way to
ask whether the shared credential could still write was to run the whole tenant
upgrade: releases resolved, root-owned tooling restaged, grants rewritten,
recorded phases advanced. A question should not have side effects that large.

`switchyard publication-status` is that question on its own. The only thing it
can write is the root-owned, non-secret evidence file, and only when asked to
check.
"""

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

from scripts import team_launcher
from scripts.ticket_board import publication_boundary as pb

REMOTE = "git@github-switchyard:ebudai/switchyard.git"
PUBLICATION_FINGERPRINT = "SHA256:publicationpublicationpublicationpublication"
SHARED_FINGERPRINT = "SHA256:sharedsharedsharedsharedsharedsharedshared0"

#: Anything that would mean this command had started doing tenant work.
FORBIDDEN = (
    "install", "systemctl", "psql", "git", "tmux", "ssh-keyscan",
    "visudo", "cp", "rm", "chown", "chmod", "ticket-board-migrate",
)


class Runner:
    """Records every command, and answers only the ones this may legitimately run."""

    def __init__(self, *, push: tuple[int, str, str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.push = push

    def __call__(self, args, **kwargs):
        self.calls.append([str(part) for part in args])
        text = " ".join(str(part) for part in args)
        if args and str(args[0]) == "ssh-keygen":
            path = str(args[-1])
            fingerprint = PUBLICATION_FINGERPRINT if "publish-key" in path else SHARED_FINGERPRINT
            if "missing" in path:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="no such file")
            return subprocess.CompletedProcess(args, 0, stdout=f"256 {fingerprint} key (ED25519)\n", stderr="")
        if args and str(args[0]) == "install" and "/dev/stdin" in text:
            done = subprocess.run(
                [part for part in args if part not in ("-o", "root", "-g")],
                input=kwargs.get("input", ""), text=True, capture_output=True,
            )
            return subprocess.CompletedProcess(args, done.returncode, done.stdout, done.stderr)
        if self.push is not None and args and str(args[0]) == "sudo":
            code, out, err = self.push
            return subprocess.CompletedProcess(args, code, stdout=out, stderr=err)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def tenant(root: Path):
    os.environ["SWITCHYARD_PUBLISH_ROOT"] = str(root / "publish")
    os.environ["SWITCHYARD_PRIVILEGED_PROVISION_ROOT"] = str(root / "provision")
    (root / "publish").mkdir(parents=True, exist_ok=True)
    registration = root / "provision" / "demo"
    registration.mkdir(parents=True, exist_ok=True)
    (registration / "publish-remote").write_text(REMOTE + "\n", encoding="utf-8")

    config = team_launcher.ProjectConfig(
        project="demo", project_name="Demo", ticket_prefix="DEMO",
        layout=root / "layout.json", session_dir=root / "sessions",
        board_url="http://127.0.0.1:8771", board_socket="/run/demo/board.sock",
        upstream_report_url="", upstream_report_token_file="",
        run_as_user=team_launcher.current_user_name(), pane_launcher=None,
        repository=root / "repo", control_repository=root / "repo.git",
        worktree_base=root / "worktrees", worktree_remote="origin",
        worktree_branch="main", roles=[],
    )
    config_path = root / "demo.json"
    config_path.write_text(json.dumps({"project": "demo"}), encoding="utf-8")
    return config, config_path


def status(config, config_path, *, verify: bool = False, runner: Runner | None = None):
    lines: list[str] = []
    used = runner or Runner()
    code = team_launcher.publication_status_command(
        config, config_path=config_path, verify=verify, runner=used, print_func=lines.append
    )
    return code, "\n".join(lines), used


def test_it_reports_the_state_and_touches_nothing() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        config, config_path = tenant(root)
        before = sorted(p.name for p in (root / "publish").iterdir())

        code, text, runner = status(config, config_path)
        assert code == 0, text
        assert "publication cutover: configured-unverified" in text, text
        # Nothing was installed, deployed, migrated, staged or started.
        for call in runner.calls:
            assert call[0] not in FORBIDDEN or call[0] == "ssh-keygen", call
        assert sorted(p.name for p in (root / "publish").iterdir()) == before
        # It names the bounded check as its own command rather than an upgrade.
        assert "switchyard publication-status demo --verify" in text, text
        assert "upgrade" not in text, text


def test_verifying_writes_the_evidence_and_nothing_else() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        config, config_path = tenant(root)
        runner = Runner(push=(128, "", "ERROR: The key you are authenticating with has been marked as read only."))

        code, text, runner = status(config, config_path, verify=True, runner=runner)
        assert code == 0, text
        assert "write authority: read_only" in text, text

        # The one file it may write, and it is not secret.
        written = sorted(p.name for p in (root / "publish").iterdir())
        assert written == ["demo-cutover.json"], written
        document = json.loads((root / "publish" / "demo-cutover.json").read_text())
        assert document["shared_write"] == pb.WRITE_READ_ONLY, document
        assert document["shared_fingerprint"] == SHARED_FINGERPRINT, document

        # The only privileged-looking call is the bounded check itself.
        shell_calls = [c for c in runner.calls if c[0] not in ("ssh-keygen", "install")]
        assert len(shell_calls) == 1, shell_calls
        assert shell_calls[0][:3] == ["sudo", "-u", config.run_as_user], shell_calls[0]
        assert "--dry-run" in shell_calls[0][-1], shell_calls[0]


def test_an_unreadable_shared_key_is_reported_and_cannot_read_as_ready() -> None:
    """The review finding, through the command an operator actually runs."""
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        config, config_path = tenant(root)
        pb.write_cutover_evidence(
            pb.CutoverEvidence(
                project="demo", remote=REMOTE,
                publication_fingerprint=PUBLICATION_FINGERPRINT,
                shared_fingerprint=SHARED_FINGERPRINT,
                publication=pb.CredentialFinding(pb.WRITE_VERIFIED, "2026-09-13T00:00:00+00:00", "published"),
                shared=pb.CredentialFinding(pb.WRITE_READ_ONLY, "2026-09-13T00:00:00+00:00", "refused as read only"),
            ),
            runner=Runner(),
        )
        # With both keys readable this is the ready state.
        code, ready_text, _ = status(config, config_path)
        assert code == 0 and "publication cutover: ready" in ready_text, ready_text

        class Blind(Runner):
            """The shared public key cannot be read, so nothing identifies it."""

            def __call__(self, args, **kwargs):
                if args and str(args[0]) == "ssh-keygen" and "publish-key" not in str(args[-1]):
                    self.calls.append([str(part) for part in args])
                    return subprocess.CompletedProcess(args, 1, stdout="", stderr="no such file")
                return super().__call__(args, **kwargs)

        code, text, _ = status(config, config_path, runner=Blind())
        assert code == 0, text
        assert "could not be identified" in text, text
        assert "no longer applies" in text, text
        assert "publication cutover: ready" not in text, text
        assert "shared credential (unidentified): unknown" in text, text


def test_the_upgrade_no_longer_offers_verification() -> None:
    """It was a flag there, which made a question run a tenant upgrade."""
    parser = team_launcher._build_switchyard_upgrade_parser()
    text = parser.format_help()
    assert "--verify-publication-cutover" not in text, text
    assert "publication-status" in team_launcher.SWITCHYARD_COMMANDS


def main() -> int:
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"publication_status_command_test: {len(tests)} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
